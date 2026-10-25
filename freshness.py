"""Is the reviewed code still what the PR (or branch) holds?

Compares the page's reviewed commit with the current PR head (via gh) or, for branch reviews, with the local HEAD,
lists the commits added since, and works out which findings sit on code those commits changed. The result is cached
in ``out/<page>/freshness.json`` so the hand-off hook and the side chat can mention it without calling GitHub. One
check per page runs at a time (``jobs.check_once``): Check again clicked five times, or a poll arriving meanwhile,
waits for the running check and gets its result.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

import jobs
import store

CACHE_SECONDS = 60
SEVERITY_ORDER = {'deleted': 3, 'lines': 2, 'file': 1}


def cache_path(page: str) -> Path:
    return store.page_dir(page) / 'freshness.json'


def load_cached(page: str) -> dict | None:
    try:
        data = json.loads(cache_path(page).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _git(root: str, *args: str, timeout: int = 30) -> str | None:
    try:
        r = subprocess.run(['git', '-C', root, *args], capture_output=True, encoding='utf-8', errors='replace',
                           timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def has_commit(root: str, sha: str) -> bool:
    return _git(root, 'cat-file', '-e', f'{sha}^{{commit}}') is not None


def check(page: str, force: bool = False) -> dict:
    """The cached check while it is under a minute old (unless forced) and none is running; else a check, shared
    with any request that arrives while it runs."""
    cached = load_cached(page)
    if cached and not force and time.time() - cached.get('checked_at', 0) < CACHE_SECONDS and not jobs.checking(page):
        return cached
    return jobs.check_once(page, force, lambda: _check(page))


def _check(page: str) -> dict:
    data = store.page_data(page)
    if not data:
        return {'status': 'unknown', 'reason': 'Unknown page.'}
    ref, root = store.reviewed_ref(data), data.get('repo_root') or ''
    if not ref:
        result = {'status': 'unknown', 'reason': 'This page shows the working tree, so there is no commit to compare.'}
    elif data.get('pr'):
        result = _check_pr(root, data['pr'], ref, data.get('github'))
    else:
        result = _check_branch(root, ref, data.get('github'))
    result.update({'ref': ref, 'checked_at': time.time()})
    if result['status'] in ('behind', 'rewritten') and has_commit(root, result['head']):
        result['affected'] = _affected(root, ref, result['head'], data.get('findings') or [])
    # A re-review (rereview.py) may have moved the page to the new head meanwhile: then this answer is about the old
    # one. Checked and written under the page's lock, which a re-review holds while it moves the page.
    with jobs.page_lock(page):
        if store.reviewed_ref(store.page_data(page) or {}) == ref:
            cache_path(page).write_text(json.dumps(result))
    return result


def _commit_row(sha: str, title: str, author: str, date: str, url: str | None) -> dict:
    return {'sha': sha, 'title': title, 'author': author, 'date': date, 'url': url}


LOGIN: list[str] = []


def viewer_login() -> str | None:
    """The GitHub login gh is signed in as, looked up once per process (failures are retried next time)."""
    if not LOGIN:
        try:
            r = subprocess.run(['gh', 'api', 'user', '--jq', '.login'], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode != 0 or not r.stdout.strip():
            return None
        LOGIN.append(r.stdout.strip())
    return LOGIN[0]


def _my_review(reviews: list[dict], oids: list[str], head: str) -> dict | None:
    """Your standing review on the PR: the latest approval or change request, as GitHub counts it."""
    login = viewer_login()
    last = None
    for rv in reviews:
        if login and (rv.get('author') or {}).get('login') == login and rv.get('state') in (
                'APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'):
            last = rv
    if not last or last['state'] == 'DISMISSED':
        return None
    commit = (last.get('commit') or {}).get('oid') or ''
    return {'state': last['state'], 'at': last.get('submittedAt'), 'commit': commit,
            'new_since': len(oids) - oids.index(commit) - 1 if commit in oids else None,
            'covers_head': not commit or commit == head}


def _check_pr(root: str, pr: int, ref: str, github: str | None) -> dict:
    try:
        repo = ['--repo', github.removeprefix('https://github.com/')] if github else []
        r = subprocess.run(['gh', 'pr', 'view', str(pr), '--json', 'headRefOid,state,url,commits,reviews', *repo],
                           cwd=root, capture_output=True, text=True, timeout=30)
        info = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        info = None
    if not info:
        return {'status': 'unknown', 'reason': f'Could not reach GitHub for PR #{pr}.'}
    head, commits = info['headRefOid'], info.get('commits') or []
    oids = [c['oid'] for c in commits]
    rows = [_commit_row(c['oid'], c.get('messageHeadline', ''),
                        ((c.get('authors') or [{}])[0].get('login') or (c.get('authors') or [{}])[0].get('name') or ''),
                        c.get('committedDate', ''), f"{info['url']}/commits/{c['oid']}") for c in commits]
    if head == ref:
        status, new = 'current', []
    elif ref in oids:
        status, new = 'behind', rows[oids.index(ref) + 1:]
    else:
        status, new = 'rewritten', []
    if head != ref and not has_commit(root, head):
        _git(root, 'fetch', '--quiet', '--no-tags', 'origin', f'pull/{pr}/head', timeout=90)
    mine = _my_review(info.get('reviews') or [], oids, head)
    state = info.get('state')
    return {'status': status, 'state': state, 'head': head, 'new_commits': new, 'pr': pr, 'mine': mine,
            # Nothing left to do on the PR: it is merged or closed, or you approved what it holds now.
            'done': state in ('MERGED', 'CLOSED') or bool(mine and mine['state'] == 'APPROVED' and mine['covers_head']),
            'compare_url': f'{github}/compare/{ref}...{head}' if github and head != ref else None}


def _check_branch(root: str, ref: str, github: str | None) -> dict:
    head = (_git(root, 'rev-parse', 'HEAD') or '').strip()
    if not head or head == ref:
        return {'status': 'current', 'head': head or ref, 'new_commits': []}
    if _git(root, 'merge-base', '--is-ancestor', ref, head) is None:
        return {'status': 'unknown', 'head': head,
                'reason': 'The checked-out branch does not contain the reviewed commit, so there is nothing to compare.'}
    log = _git(root, 'log', '--reverse', '--format=%H%x1f%s%x1f%an%x1f%cI', f'{ref}..{head}') or ''
    new = []
    for line in log.splitlines():
        sha, title, author, date = (line.split('\x1f') + ['', '', '', ''])[:4]
        new.append(_commit_row(sha, title, author, date, f'{github}/commit/{sha}' if github else None))
    return {'status': 'behind', 'head': head, 'new_commits': new,
            'compare_url': f'{github}/compare/{ref}...{head}' if github else None}


def old_side_hunks(patch: str) -> dict[str, dict]:
    """Per old-side path: the old line ranges each hunk replaced, the new-side ranges they became (``new``, in the
    same order), the path on the new side when the file was renamed (``to``), and whether the file was deleted."""
    files: dict[str, dict] = {}
    current: dict | None = None
    # Lines the current hunk still has on each side: inside a hunk, a removed "-- comment" reads "--- comment" and is
    # no file header.
    old_left = new_left = 0

    def entry(path: str) -> dict:
        return files.setdefault(path, {'ranges': [], 'new': [], 'deleted': False})

    for line in patch.splitlines():
        if old_left > 0 or new_left > 0:
            tag = line[:1]
            if tag in ('-', ' '):
                old_left -= 1
            if tag in ('+', ' '):
                new_left -= 1
            continue
        if line.startswith('diff --git '):
            current = None
        elif line.startswith('rename from '):
            # Comes before the ---/+++ lines, and is all a pure rename has.
            current = entry(line[len('rename from '):].strip())
        elif line.startswith('rename to ') and current is not None:
            current['to'] = line[len('rename to '):].strip()
        elif line.startswith('--- '):
            target = line[4:].strip()
            current = None if target == '/dev/null' else entry(target[2:] if target.startswith('a/') else target)
        elif line.startswith('+++ ') and current is not None and line[4:].strip() == '/dev/null':
            current['deleted'] = True
        elif current is not None:
            m = re.match(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', line)
            if m:
                start, count = int(m.group(1)), 1 if m.group(2) is None else int(m.group(2))
                added = 1 if m.group(4) is None else int(m.group(4))
                current['ranges'].append((start, count))
                current['new'].append((int(m.group(3)), added))
                old_left, new_left = count, added
    return files


def u0_diff(root: str, ref: str, head: str, *paths: str) -> str | None:
    """``git diff -U0`` from ref to head (renames found, paths as written: non-ASCII names unquoted), for
    ``old_side_hunks``; None when git fails."""
    return _git(root, '-c', 'core.quotePath=false', 'diff', '-U0', '-M', '--no-color', '--no-ext-diff', ref, head,
                *(['--', *paths] if paths else []))


def map_line(changed: dict[str, dict], path: str, line: int) -> tuple[str, int] | None:
    """Where a line of the reviewed commit sits at the new head, through ``old_side_hunks`` of a ``-U0`` diff: its path
    there (renamed or not) and its line, or None when that line itself changed or its file was deleted."""
    info = changed.get(path)
    if not info:
        return path, line
    if info['deleted']:
        return None
    shift = 0
    for (a, count), (_c, added) in zip(info['ranges'], info['new']):
        if count == 0:
            # Pure insertion after old line a: it pushes the line down when it lands above it.
            if a < line:
                shift += added
        elif a <= line <= a + count - 1:
            return None
        elif a + count - 1 < line:
            shift += added - count
    return info.get('to') or path, line + shift


def hits(ranges: list[tuple[int, int]], start: int, end: int) -> bool:
    for a, count in ranges:
        if count == 0:
            # Pure insertion after old line a: it lands inside the flagged block only between its lines.
            if start <= a < end:
                return True
        elif a <= end and a + count - 1 >= start:
            return True
    return False


def _affected(root: str, ref: str, head: str, findings: list[dict]) -> dict:
    paths = sorted({loc['path'] for f in findings for loc in f.get('locations', []) if loc.get('path')})
    if not paths:
        return {}
    changed = old_side_hunks(u0_diff(root, ref, head, *paths) or '')
    affected = {}
    for f in findings:
        worst, where = None, []
        for loc in f.get('locations', []):
            info = changed.get(loc.get('path') or '')
            if not info:
                continue
            kind = 'deleted' if info['deleted'] else 'lines' if hits(info['ranges'], loc['start'], loc['end']) else 'file'
            where.append({'path': loc['path'], 'kind': kind})
            if worst is None or SEVERITY_ORDER[kind] > SEVERITY_ORDER[worst]:
                worst = kind
        if worst:
            affected[f['id']] = {'kind': worst, 'locations': where}
    return affected


def heads_up(page: str) -> str | None:
    """One-paragraph warning for Claude when the PR has moved past the reviewed code, from the cached check."""
    cached = load_cached(page)
    if not cached or cached.get('status') not in ('behind', 'rewritten'):
        return None
    head = (cached.get('head') or '')[:10]
    if cached['status'] == 'rewritten':
        lead = f'Heads-up: the PR was force-pushed since the review; the reviewed commit {cached["ref"][:10]} is no longer in it (head is now {head}).'
    else:
        n = len(cached.get('new_commits') or [])
        lead = f'Heads-up: the PR has {n} newer commit{"s" * (n != 1)} than the reviewed code ({cached["ref"][:10]} → {head}).'
    affected = cached.get('affected') or {}
    if affected:
        parts = [f"{fid} ({'flagged lines changed' if a['kind'] == 'lines' else 'file deleted' if a['kind'] == 'deleted' else 'same file changed'})"
                 for fid, a in affected.items()]
        lead += ' Findings on changed code: ' + ', '.join(parts) + '. Re-check them against the new head before posting.'
    else:
        lead += " None of the findings' files changed."
    return lead


def pr_note(page: str) -> str | None:
    """One line for Claude when the PR is already settled: merged, closed, or reviewed by you, from the cached check."""
    cached = load_cached(page)
    if not cached or not cached.get('pr'):
        return None
    pr, mine, state = cached['pr'], cached.get('mine'), cached.get('state')
    if state in ('MERGED', 'CLOSED'):
        return f'PR #{pr} is already {state.lower()}, so any comment lands on a finished PR. Ask the reviewer before posting.'
    if not mine:
        return None
    at = f" on {mine['at'].replace('T', ' ').removesuffix('Z')} UTC" if mine.get('at') else ''
    where = 'the current head' if mine.get('covers_head') else (
        f"{mine['new_since']} commit{'s' * (mine['new_since'] != 1)} ago" if mine.get('new_since') is not None
        else 'a commit the PR no longer holds')
    if mine['state'] == 'APPROVED':
        return (f"The reviewer already approved PR #{pr}{at} (commit {mine['commit'][:10]}, {where}). Post comments as a "
                'plain comment review (event COMMENT); do not approve again or request changes unless the reviewer asks.')
    return (f"The reviewer already requested changes on PR #{pr}{at} (commit {mine['commit'][:10]}, {where}). A comment "
            'review (event COMMENT) keeps that standing; ask before approving or requesting changes again.')
