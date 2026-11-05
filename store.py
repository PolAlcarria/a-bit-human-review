"""The review data on disk. Every page is a folder ``out/<page>/``: ``page_data`` reads its ``review.json`` (cached by
mtime: the one way review data is read), and the folder holds the page's triage and side chat files too. Also the
version a hand-off records, and a checkout of exactly the reviewed code.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import config
import paths

KEEP_WORKTREES = 3
PR_TITLE_RE = re.compile(r'PR #(\d+)')

_page_cache: dict[str, tuple[float, dict]] = {}

# review_checkout's log lines keep the prefix they had in chat.py.
log = paths.log('chat: ')


def page_dir(page: str) -> Path:
    """The page's folder, out/<page>/: review.json, page.html, the triage, chats and job states, and locks/."""
    return paths.OUT / page


def review_path(page: str) -> Path:
    """The page's review data, as render.build_page returned it: what the server reads and serves."""
    return page_dir(page) / 'review.json'


def html_path(page: str) -> Path:
    """The page as one HTML file, written at render for opening it as file://."""
    return page_dir(page) / 'page.html'


def exists(page: str) -> bool:
    """Whether there is such a page: its folder holds page.html."""
    return html_path(page).is_file()


def pages() -> list[str]:
    """The name of every stored page, in no order: the folders in out/ named by a date that hold a page.html."""
    try:
        return [p.name for p in paths.OUT.iterdir()
                if p.name.startswith('2') and not p.is_symlink() and (p / 'page.html').is_file()]
    except OSError:
        return []


def chat_path(page: str) -> Path:
    return page_dir(page) / 'chat.json'


def load_chat(page: str) -> dict:
    try:
        chat = json.loads(chat_path(page).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return chat if isinstance(chat, dict) else {}


def page_data(page: str) -> dict | None:
    path = review_path(page)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    cached = _page_cache.get(page)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if data and isinstance(data, dict):
        # Pages rendered by older versions lack these fields.
        pr = PR_TITLE_RE.search(data.get('title') or '')
        data = {'page': page, 'pr': int(pr.group(1)) if pr else None, 'session_id': None, **data}
        _page_cache[page] = (mtime, data)
        return data
    return None


def find_finding(page: str, data: dict, fid: str) -> dict | None:
    """The finding ``fid`` of a page: one of its review's findings, or, on a diff review (no findings of its own), a
    thread the reviewer started on selected lines, which the triage holds (``decisions.json``, ``user``: true)."""
    found = next((f for f in data.get('findings') or [] if f['id'] == fid), None)
    if found or data.get('kind') != 'diff':
        return found
    rec = ((load_decisions(decisions_path(page)) or {}).get('findings') or {}).get(fid)
    if not isinstance(rec, dict) or rec.get('user') is not True:
        return None
    locations = []
    for loc in rec.get('locations') or []:
        if isinstance(loc, dict) and isinstance(loc.get('path'), str) and all(
                isinstance(loc.get(k), int) and not isinstance(loc.get(k), bool) and loc[k] >= 1 for k in ('start', 'end')):
            locations.append({'raw': f"{loc['path']}:{loc['start']}-{loc['end']}", 'path': loc['path'],
                              'start': loc['start'], 'end': max(loc['start'], loc['end']), 'ambiguous': []})
    if not locations:
        return None
    return {'id': fid, 'severity': 'finding', 'title': str(rec.get('title') or fid), 'body': str(rec.get('body') or ''),
            'recurring': False, 'meta': {}, 'locations': locations}


def reviewed_ref(data: dict) -> str | None:
    if 'ref' in data:
        return data['ref']
    return data.get('blob_ref') if str(data.get('ref_label', '')).startswith('commit') else None


def git(root: Path | str, *args: str, timeout: int = 20) -> str | None:
    """stdout of ``git -C root args``, None when git failed or timed out."""
    try:
        r = subprocess.run(['git', '-C', str(root), *args], capture_output=True, encoding='utf-8',
                           errors='replace', timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def default_base(root: Path | str) -> str | None:
    """The branch a review's changes are measured from: config default_base when the repo has it, else origin/HEAD,
    else the first of develop, main and master that exists (the remote one first)."""
    head = git(root, 'symbolic-ref', '--quiet', 'refs/remotes/origin/HEAD')
    configured = config.get('default_base')
    cands = ([configured] if configured else []) + ([head.strip()] if head else [])
    for cand in cands + ['origin/develop', 'develop', 'origin/main', 'main', 'origin/master', 'master']:
        if git(root, 'rev-parse', '--verify', '--quiet', f'{cand}^{{commit}}') is not None:
            return cand
    return None


def decisions_path(page: str) -> Path:
    return page_dir(page) / 'decisions.json'


def load_decisions(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def page_version(dec: dict, chat_state: dict) -> str:
    """Changes whenever the triage or a side chat changes; a hand-off records the version Claude got."""
    return f"{dec.get('saved_at')}|{chat_state.get('updated')}"


def review_checkout(data: dict) -> tuple[str, bool]:
    """Directory holding exactly the reviewed code: the repo for working-tree reviews, else a worktree of the reviewed
    commit. On a PR page that worktree is on the PR's head branch when git allows it safely (``_branch_args``), else
    detached."""
    root, ref = data.get('repo_root') or str(Path.home()), reviewed_ref(data)
    if not ref:
        return root, False
    path = paths.WORKTREES / ref[:12]
    if (path / '.git').exists():
        os.utime(path)
        return str(path), True
    paths.WORKTREES.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', '-C', root, 'worktree', 'prune'], capture_output=True)
    args, branch = ['--detach', str(path), ref], None
    if data.get('pr'):
        args, branch = _branch_args(root, data, ref, path)
    r = subprocess.run(['git', '-C', root, 'worktree', 'add', *args], capture_output=True, text=True, timeout=180)
    if r.returncode and branch:
        # A local `feat` blocks `feat/x`, or another checkout took the branch meanwhile: still give the reviewed code.
        log(f'worktree {ref[:12]} is detached: checking out {branch} failed: {r.stderr.strip()[:300]}')
        branch = None
        r = subprocess.run(['git', '-C', root, 'worktree', 'add', '--detach', str(path), ref],
                           capture_output=True, text=True, timeout=180)
    if r.returncode:
        log(f'worktree add {ref[:12]} failed: {r.stderr.strip()[:300]}')
        return root, False
    if branch and git(root, 'rev-parse', '--verify', '--quiet', f'refs/remotes/origin/{branch}') is not None \
            and git(root, 'rev-parse', '--verify', '--quiet', f'{branch}@{{upstream}}') is None:
        git(root, 'branch', '--quiet', f'--set-upstream-to=origin/{branch}', branch)
    _prune_worktrees(root, keep=path)
    return str(path), True


def _pr_branch(root: str, data: dict) -> tuple[str | None, str]:
    """(the PR's head branch, '') or (None, why not). Pages rendered before 0.5.1 lack head_branch: gh looks it up."""
    branch, cross = data.get('head_branch'), data.get('cross_repo')
    if 'head_branch' not in data:
        github = data.get('github') or ''
        repo = ['--repo', github[len('https://github.com/'):]] if github.startswith('https://github.com/') else []
        try:
            r = subprocess.run(['gh', 'pr', 'view', str(data['pr']), '--json', 'headRefName,isCrossRepository', *repo],
                               cwd=root, capture_output=True, text=True, timeout=20)
            info = json.loads(r.stdout) if r.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            info = None
        if not isinstance(info, dict):
            return None, f"could not look up the head branch of PR #{data['pr']} with gh"
        branch, cross = info.get('headRefName'), info.get('isCrossRepository')
    if cross:
        return None, f"PR #{data['pr']} is cross-repository"
    if not branch or git(root, 'check-ref-format', '--branch', branch) is None:
        return None, f"PR #{data['pr']} has no usable head branch name"
    return branch, ''


def _worktree_holding(root: str, branch: str) -> str | None:
    """The folder that has ``branch`` checked out (the repo itself or one of its worktrees), if any."""
    current = None
    for line in (git(root, 'worktree', 'list', '--porcelain') or '').splitlines():
        if line.startswith('worktree '):
            current = line[len('worktree '):]
        elif line == f'branch refs/heads/{branch}':
            return current
    return None


def _branch_args(root: str, data: dict, ref: str, path: Path) -> tuple[list[str], str | None]:
    """``git worktree add`` arguments for a PR page, and the branch they check out (None: detached). The branch is
    checked out only when that loses nothing: it is created at the reviewed commit, already there, or fast-forwarded
    to it, and taken from an older worktree of this tool only when that one has no uncommitted changes. Anything
    else is detached, with one viewer.log line saying why."""
    detached = ['--detach', str(path), ref]
    branch, why = _pr_branch(root, data)
    if branch:
        why = _prepare_branch(root, branch, ref)
    if why:
        log(f'worktree {ref[:12]} is detached: {why}')
        return detached, None
    if git(root, 'rev-parse', '--verify', '--quiet', f'refs/heads/{branch}') is None:
        return ['-b', branch, str(path), ref], branch
    return [str(path), branch], branch


def _prepare_branch(root: str, branch: str, ref: str) -> str:
    """Make the local ``branch`` ready to check out at ``ref``, or say why it cannot be ('' when ready, which includes
    a branch that does not exist yet)."""
    # A merged PR's branch is often deleted on the remote: then the fetch fails, and nothing needs it.
    git(root, 'fetch', '--quiet', '--no-tags', 'origin', f'+refs/heads/{branch}:refs/remotes/origin/{branch}', timeout=90)
    local = (git(root, 'rev-parse', '--verify', '--quiet', f'refs/heads/{branch}') or '').strip()
    if not local:
        return ''
    if local != ref and git(root, 'merge-base', '--is-ancestor', local, ref) is None:
        return f'{branch} has commits that {ref[:12]} lacks'
    holder = _worktree_holding(root, branch)
    if holder:
        if Path(holder).resolve().parent != paths.WORKTREES.resolve():
            return f'{branch} is checked out in {holder}'
        status = git(holder, 'status', '--porcelain')
        if status is None or status.strip():
            return f'{branch} is checked out in {holder}, which has uncommitted changes'
        # Same commit, so its files stay as they are.
        if git(holder, 'switch', '--quiet', '--detach') is None:
            return f'could not detach {holder} from {branch}'
    if local != ref and git(root, 'branch', '--force', branch, ref) is None:
        return f'could not fast-forward {branch} to {ref[:12]}'
    return ''


def _prune_worktrees(root: str, keep: Path) -> None:
    """Remove all but the KEEP_WORKTREES newest worktrees (``keep`` counts as one), never one that would lose work
    (``_unsaved_work``). Local branches stay: their commits are safe. The folder holds worktrees of every reviewed repo,
    so each is removed through its own repository."""
    trees = sorted((p for p in paths.WORKTREES.iterdir() if p.is_dir() and p != keep), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in trees[KEEP_WORKTREES - 1:]:
        repo = root
        if (old / '.git').exists():
            why = _unsaved_work(old)
            if why:
                log(f'kept worktree {old}: {why}')
                continue
            repo = (git(old, 'rev-parse', '--path-format=absolute', '--git-common-dir') or '').strip() or root
        if subprocess.run(['git', '-C', repo, 'worktree', 'remove', '--force', str(old)], capture_output=True).returncode:
            shutil.rmtree(old, ignore_errors=True)
            subprocess.run(['git', '-C', repo, 'worktree', 'prune'], capture_output=True)


def _unsaved_work(tree: Path) -> str:
    """Why removing the worktree ``tree`` would lose work, or '': uncommitted changes (ignored files do not count), or
    commits on a detached HEAD that no branch, tag or remote ref holds. A HEAD still at the commit the folder is named
    for is the reviewed commit itself, which is on GitHub."""
    status = git(tree, 'status', '--porcelain')
    if status is None:
        return 'git status failed, so it may hold uncommitted changes'
    if status.strip():
        return 'it has uncommitted changes'
    head = (git(tree, 'rev-parse', '--verify', '--quiet', 'HEAD') or '').strip()
    if head and not head.startswith(tree.name) and git(tree, 'symbolic-ref', '--quiet', 'HEAD') is None \
            and not (git(tree, 'for-each-ref', '--count=1', '--contains', head) or '').strip():
        return f'its detached HEAD {head[:12]} has commits no branch holds'
    return ''
