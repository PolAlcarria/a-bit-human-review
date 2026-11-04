"""Build a review page: find the reviewed repository (the session's clone, a sibling clone, else a cached one), the
commit and the PR's diff, read the findings with Claude when asked (extract.py), load the code at each finding, and
write the page with the current template. ``launch_render`` runs it in a detached process (``finding_viewer.py
render``); opening the page in the browser is the caller's (finding_viewer.py).
"""
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import config
import extract
import parse
import paths
import reviews
import store
from store import git

CONTEXT_LINES = 12
MAX_FILE_LINES = 30000
KEEP_PAGES = 40
# A diff review embeds every changed file whole, so it keeps to a size the page can hold.
MAX_DIFF_FILES = 300
MAX_DIFF_LINES = 120000
MAX_UNTRACKED_BYTES = 1_000_000

GITHUB_REMOTE_RE = re.compile(r'^(?:git@github\.com:|https://github\.com/|ssh://git@github\.com/)([^/]+)/(.+?)(?:\.git)?/?$')

LANGS = {
    'py': 'python', 'pyi': 'python', 'ts': 'typescript', 'tsx': 'typescript', 'js': 'javascript',
    'jsx': 'javascript', 'mjs': 'javascript', 'cjs': 'javascript', 'sql': 'sql', 'md': 'markdown',
    'yml': 'yaml', 'yaml': 'yaml', 'json': 'json', 'sh': 'bash', 'bash': 'bash', 'zsh': 'bash',
    'html': 'xml', 'xml': 'xml', 'css': 'css', 'scss': 'scss', 'toml': 'ini', 'ini': 'ini', 'cfg': 'ini',
    'go': 'go', 'rb': 'ruby', 'java': 'java', 'kt': 'kotlin', 'rs': 'rust', 'dockerfile': 'dockerfile',
}


log = paths.log()


def repo_root(cwd: str | Path) -> Path | None:
    out = git(cwd, 'rev-parse', '--show-toplevel')
    return Path(out.strip()) if out else None


def remote_slugs(root: Path) -> set[str]:
    """owner/repo (lowercase) of every GitHub remote of a clone."""
    slugs = set()
    for line in (git(root, 'remote', '-v') or '').splitlines():
        parts = line.split()
        m = GITHUB_REMOTE_RE.match(parts[1]) if len(parts) > 1 else None
        if m:
            slugs.add(f'{m.group(1)}/{m.group(2)}'.lower())
    return slugs


def locate_repo(cwd: str, slug: str | None, notes: list[str]) -> Path:
    """The clone to show code from: the session's repo when it is the PR's repo, else a local clone of the PR's repo
    (next to the session's folder, or in config repo_dirs, empty by default), else a cached clone."""
    root = repo_root(cwd)
    if not slug or (root and slug.lower() in remote_slugs(root)):
        return root or Path(cwd)
    name = slug.split('/')[-1]
    dirs = config.get('repo_dirs')
    bases = [Path(cwd), (root or Path(cwd)).parent, *(Path(d).expanduser() for d in dirs if d)]
    for cand in dict.fromkeys(b / name for b in bases):
        if (cand / '.git').exists() and slug.lower() in remote_slugs(cand):
            return cand
    return cached_clone(slug, notes) or root or Path(cwd)


def cached_clone(slug: str, notes: list[str]) -> Path | None:
    """A clone under clones/, made with gh's credentials: no checkout, and files over 1 MB fetched only when read.

    Smaller files are fetched up front: `git grep` in a clone without them fetches each file on its own, which takes
    minutes."""
    path = paths.CLONES / slug.lower()
    if (path / '.git').is_dir():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(['gh', 'repo', 'clone', slug, str(path), '--', '--filter=blob:limit=1m', '--no-checkout', '--quiet'],
                           capture_output=True, text=True, timeout=300)
        error = r.stderr.strip()[-300:] if r.returncode else None
    except (OSError, subprocess.TimeoutExpired) as exc:
        error = repr(exc)
    if error is not None:
        log(f'clone {slug}: {error}')
        shutil.rmtree(path, ignore_errors=True)
        notes.append(f'{slug} is not cloned on this machine and could not be cloned with gh, so its code is not shown.')
        return None
    return path


def commit_exists(root: Path, sha: str) -> bool:
    return git(root, 'cat-file', '-e', f'{sha}^{{commit}}') is not None


def resolve_pr(root: Path, pr: int, notes: list[str], slug: str | None = None) -> tuple[str | None, str | None, str | None]:
    """Return (head sha, base ref, author login) for a PR, fetching the head commit if this clone lacks it."""
    try:
        r = subprocess.run(['gh', 'pr', 'view', str(pr), '--json', 'headRefOid,baseRefName,author',
                            *(['--repo', slug] if slug else [])],
                           cwd=root, capture_output=True, text=True, timeout=20)
        info = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        info = None
    if not info:
        notes.append(f'Could not look up PR #{pr} with gh; showing the working tree.')
        return None, None, None
    sha, author = info['headRefOid'], (info.get('author') or {}).get('login')
    if not commit_exists(root, sha):
        git(root, 'fetch', '--quiet', '--no-tags', 'origin', f'pull/{pr}/head', timeout=90)
    if not commit_exists(root, sha):
        notes.append(f'PR #{pr} head {sha[:8]} is not available locally; showing the working tree.')
        return None, None, author
    base = f"origin/{info['baseRefName']}"
    return sha, base if git(root, 'rev-parse', '--verify', '--quiet', base) is not None else None, author


def branch_pr(cwd: str) -> int | None:
    """The PR of the branch checked out in cwd, as gh finds it (including branches made by `gh pr checkout`)."""
    try:
        r = subprocess.run(['gh', 'pr', 'view', '--json', 'number'], cwd=cwd, capture_output=True, text=True, timeout=20)
        return int(json.loads(r.stdout)['number']) if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return None


def pr_for_commit(root: Path, sha: str, slug: str) -> int | None:
    """The PR that holds this commit: the one whose head it is, else the only PR that holds it, since the author may
    have pushed since the review. Skips PRs from the default branch, such as a develop → main release PR."""
    if not slug:
        return None
    try:
        r = subprocess.run(['gh', 'api', f'repos/{slug}/commits/{sha}/pulls'], cwd=root, capture_output=True, text=True,
                           timeout=20)
        pulls = json.loads(r.stdout) if r.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pulls = []
    heads = [(p, p.get('head') or {}) for p in pulls]
    candidates = [(p, head) for p, head in heads if head.get('ref') != (head.get('repo') or {}).get('default_branch')]
    for p, head in candidates:
        if (head.get('sha') or '').startswith(sha.lower()):
            return p['number']
    # No head match: the author pushed after the review. One PR still holding the commit is the reviewed one.
    return candidates[0][0]['number'] if len(candidates) == 1 else None


def pr_patch(root: Path, pr: int, slug: str | None = None) -> dict | None:
    try:
        r = subprocess.run(['gh', 'pr', 'diff', str(pr), '--color=never', *(['--repo', slug] if slug else [])], cwd=root,
                           capture_output=True, encoding='utf-8', errors='replace', timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_patch(r.stdout) if r.returncode == 0 and r.stdout.strip() else None


def parse_patch_detail(patch: str) -> dict[str, dict]:
    """Map each file in a unified diff to what changed in it, all line numbers on the new side:

    - ``added``: the added lines, sorted;
    - ``removed``: {line after which lines were removed: the text of those lines}; ``0`` is before the first line;
    - ``hunks``: the hunk ranges, context lines included (GitHub accepts inline comments only there);
    - ``status``: ``added`` (a new file), ``deleted`` or ``modified``.

    A deleted file has no new side: all its lines are in ``removed[0]``.
    """
    files: dict[str, dict] = {}
    cur: dict | None = None
    old_target: str | None = None
    in_hunk, new_line = False, 0
    for line in patch.splitlines():
        if line.startswith('diff --git '):
            in_hunk, cur, old_target = False, None, None
            continue
        if not in_hunk and line.startswith('--- '):
            old_target = line[4:].strip()
            continue
        if not in_hunk and line.startswith('+++ '):
            target = line[4:].strip()
            if target == '/dev/null':
                path = (old_target[2:] if old_target.startswith('a/') else old_target) if old_target else None
                status = 'deleted'
            else:
                path = target[2:] if target.startswith('b/') else target
                status = 'added' if old_target == '/dev/null' else 'modified'
            cur = files.setdefault(path, {'added': set(), 'removed': {}, 'hunks': [], 'status': status}) if path else None
            continue
        m = re.match(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@', line)
        if m:
            in_hunk, new_line = True, int(m.group(1))
            count = 1 if m.group(2) is None else int(m.group(2))
            if cur is not None and count:
                cur['hunks'].append([new_line, new_line + count - 1])
            continue
        if not in_hunk or cur is None:
            continue
        tag = line[:1]
        if tag == '+':
            cur['added'].add(new_line)
            new_line += 1
        elif tag == '-':
            cur['removed'].setdefault(max(0, new_line - 1), []).append(line[1:])
        elif tag == ' ':
            new_line += 1
    return {path: {**d, 'added': sorted(d['added'])} for path, d in files.items()}


def parse_patch(patch: str) -> dict[str, tuple[list[int], dict[int, int], list[list[int]]]]:
    """Map each file in a unified diff to (added lines, {line after which N lines were removed}, hunk ranges).

    All line numbers are new-side. Hunk ranges include context lines: that is where GitHub accepts inline comments.
    A deleted file is left out (``parse_patch_detail`` keeps it).
    """
    return {path: (d['added'], {k: len(v) for k, v in d['removed'].items()}, d['hunks'])
            for path, d in parse_patch_detail(patch).items() if d['status'] != 'deleted'}


def resolve_path(raw: str, root: Path, files: set[str], by_name: dict[str, list[str]], changed: set[str]):
    p = raw.strip()
    if p.startswith(str(root) + '/'):
        p = p[len(str(root)) + 1:]
    while p.startswith('./'):
        p = p[2:]
    p = p.lstrip('/')
    for cand in (p, f'src/{p}'):
        if cand in files:
            return cand, []
    matches = [f for f in by_name.get(p.rsplit('/', 1)[-1], []) if f.endswith('/' + p)]
    if not matches:
        return None, []
    in_diff = [f for f in matches if f in changed]
    pool = sorted(in_diff or matches, key=len)
    return pool[0], (matches if len(matches) > 1 and len(in_diff) != 1 else [])


def github_base(root: Path) -> str | None:
    m = GITHUB_REMOTE_RE.match((git(root, 'remote', 'get-url', 'origin') or '').strip())
    return f'https://github.com/{m.group(1)}/{m.group(2)}' if m else None


def render(spec: dict) -> Path | None:
    """Build the page; None when Claude found no findings in the text and the parser found none either."""
    built = build_page(spec)
    if built is None:
        return None
    out, data = built
    write_page(out, data)
    latest = paths.OUT / 'latest.html'
    try:
        latest.unlink(missing_ok=True)
        latest.symlink_to(out)
    except OSError:
        pass
    prune()
    return out


def write_page(out: Path, data: dict) -> None:
    """Write the page's folder: ``out`` (page.html, the page as one file, for file://) and review.json beside it (the
    data the server reads), each in one step: a re-review (rereview.py) replaces a page that is open, and nobody may
    read half. review.json goes last."""
    out.parent.mkdir(parents=True, exist_ok=True)
    for path, text in ((out, reviews.fill_template(data)),
                       (out.with_name('review.json'), json.dumps(data, ensure_ascii=False))):
        tmp = path.with_suffix('.tmp')
        tmp.write_text(text, encoding='utf-8')
        tmp.replace(path)


def build_page(spec: dict) -> tuple[Path, dict] | None:
    """The page's path and review data: the repo, the code to show, the findings and their files. None when Claude
    found no findings in the text and the parser found none either. A spec of kind ``diff`` builds a diff review
    instead (``build_diff_page``, which raises ``DiffError`` when it cannot)."""
    if spec.get('kind') == 'diff':
        return build_diff_page(spec)
    notes: list[str] = []
    slug = spec.get('repo')
    root = locate_repo(spec['cwd'], slug, notes)
    is_repo = repo_root(root) is not None
    ref, base, pr_head, pr_author = None, None, None, None
    if spec.get('reviewed_sha') and not spec.get('pr'):
        # A saved report pins a commit but names no PR: review it as the PR that holds that commit.
        spec['pr'] = pr_for_commit(root, spec['reviewed_sha'], slug or (github_base(root) or '').removeprefix('https://github.com/'))
        if spec['pr']:
            log(f"render: reviewed commit {spec['reviewed_sha'][:8]} belongs to PR #{spec['pr']}")
    mentioned = spec.get('mentioned_pr')
    if mentioned and not spec.get('pr'):
        spec['pr'] = branch_pr(spec['cwd'])
        if spec['pr']:
            spec['title'] = f"Code review · PR #{spec['pr']}"
        if spec['pr'] != mentioned:
            log(f"render: PR #{mentioned} was only mentioned; the reviewed branch is "
                + (f"PR #{spec['pr']}" if spec['pr'] else 'not a PR'))
            notes.append(f"PR #{mentioned} came up in the session but was never fetched; this page shows the "
                         f"session's branch" + (f", PR #{spec['pr']}." if spec['pr'] else ', which has no PR.'))
    if spec.get('pr'):
        # Also fetches pull/<n>/head, which brings in the PR's older commits when a reviewed commit is pinned.
        pr_head, base, pr_author = resolve_pr(root, spec['pr'], [] if spec.get('reviewed_sha') else notes, slug)
    if spec.get('reviewed_sha'):
        if commit_exists(root, spec['reviewed_sha']):
            ref = (git(root, 'rev-parse', f"{spec['reviewed_sha']}^{{commit}}") or spec['reviewed_sha']).strip()
        else:
            notes.append(f"Reviewed commit {spec['reviewed_sha'][:8]} is not in this clone; showing the working tree.")
    else:
        ref = pr_head
    if not ref and root.is_relative_to(paths.CLONES):
        # A cached clone has no working tree: show its default branch.
        ref = (git(root, 'rev-parse', 'HEAD') or '').strip() or None
    patch = pr_patch(root, spec['pr'], slug) if spec.get('pr') and ref and ref == pr_head else None
    base = base or store.default_base(root)
    head_sha = (git(root, 'rev-parse', ref or 'HEAD') or '').strip()
    merge_base = (git(root, 'merge-base', base, ref or 'HEAD') or '').strip() if base else ''
    if not merge_base:
        notes.append('No base branch found; changed-line markers are off.')

    if ref:
        files = set(filter(None, (git(root, 'ls-tree', '-r', '--name-only', '-z', ref) or '').split('\0')))
        changed = set(filter(None, (git(root, 'diff', '--name-only', '-z', merge_base, ref) or '').split('\0'))) if merge_base else set()
        untracked: set[str] = set()
    else:
        files = set(filter(None, (git(root, 'ls-files', '-z', '-co', '--exclude-standard') or '').split('\0')))
        untracked = set(filter(None, (git(root, 'ls-files', '-z', '-o', '--exclude-standard') or '').split('\0')))
        changed = set(filter(None, (git(root, 'diff', '--name-only', '-z', merge_base) or '').split('\0'))) if merge_base else set()
        changed |= untracked
    if patch is not None:
        changed = set(patch)

    if spec.get('text') and extract.enabled():
        got = extract.run(spec['text'], root, ref, sorted(changed), spec.get('request', ''),
                          slug or (github_base(root) or '').removeprefix('https://github.com/') or str(root), log)
        if got and got['findings']:
            spec['findings'] = parse.assign_ids(got['findings'])
            spec['subtitle'] = f"{spec.get('subtitle', '')} · findings read by Claude ({got['model']})".lstrip(' ·')
            log(f"extract: {len(got['findings'])} findings for {spec['title']} (${got['cost'] or 0:.3f})")
        elif got is None and spec['findings']:
            notes.append('Claude could not read the findings (see viewer.log), so they come from the text parser.')
        if not spec['findings']:
            log(f"render: no findings in {spec['title']}; no page opened")
            return None
    if not is_repo:
        notes.append(f'{root} is not a git repository and the reviewed repository was not found, so no code is shown.')

    by_name: dict[str, list[str]] = {}
    for f in files:
        by_name.setdefault(f.rsplit('/', 1)[-1], []).append(f)

    page_files: dict[str, dict] = {}
    page_findings = []
    for f in spec['findings']:
        locations, seen = [], set()
        for a in f.get('anchors', []):
            path, ambiguous = resolve_path(a['path'], root, files, by_name, changed)
            key = (path or a['path'], a['start'], a['end'])
            if key in seen:
                continue
            seen.add(key)
            loc = {'raw': a['raw'], 'path': path, 'start': a['start'], 'end': a['end'], 'ambiguous': ambiguous[:6]}
            if path and path not in page_files:
                page_files[path] = load_file(root, path, ref, merge_base, path in untracked, path in changed, patch)
            if path and page_files[path] is None:
                loc['path'] = None
            locations.append(loc)
            if len(locations) >= parse.MAX_LOCATIONS:
                break
        page_findings.append({'id': f['id'], 'severity': f['severity'], 'title': f['title'], 'body': f.get('body', ''),
                              'recurring': f.get('recurring', False), 'meta': f.get('meta', {}), 'locations': locations,
                              # What a re-review found for it: still applies, changed, fixed or new (rereview.py).
                              **({'rereview': f['rereview']} if f.get('rereview') else {})})
    page_files = {k: v for k, v in page_files.items() if v}

    ref_label = f'commit {ref[:8]}' if ref else f'working tree ({(git(root, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()})'
    out = Path(spec.get('out') or new_out_path(spec))
    data = {
        'page': out.parent.name, 'session_id': spec.get('session_id'), 'pr': spec.get('pr'), 'pr_author': pr_author, 'ref': ref,
        'review_session': spec.get('review_session') or spec.get('session_id'),
        'title': spec['title'], 'subtitle': spec.get('subtitle', ''), 'repo_root': str(root),
        'repo_name': root.name, 'ref_label': ref_label, 'merge_base': merge_base[:8],
        'base': f"PR #{spec['pr']} diff" if patch is not None else base,
        'github': github_base(root), 'blob_ref': head_sha, 'notes': notes,
        'generated': dt.datetime.now().strftime('%Y-%m-%d %H:%M'), 'findings': page_findings, 'files': page_files,
        # Each re-review of the page's new commits: from which commit to which, when, and what it found.
        **({'rereviewed': spec['rereviewed']} if spec.get('rereviewed') else {}),
    }
    return out, data


def load_file(root: Path, path: str, ref: str | None, merge_base: str, untracked: bool, changed: bool,
              patch: dict | None) -> dict | None:
    if ref:
        text = git(root, 'show', f'{ref}:{path}')
    else:
        try:
            text = (root / path).read_text(encoding='utf-8', errors='replace')
        except OSError:
            text = None
    if text is None:
        return None
    lines = text.split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    truncated = len(lines) > MAX_FILE_LINES
    lines = lines[:MAX_FILE_LINES]
    added: list[int] = []
    removed: dict[int, int] = {}
    hunks: list[list[int]] = []
    if untracked:
        added = list(range(1, len(lines) + 1))
        hunks = [[1, len(lines)]] if lines else []
    elif patch is not None:
        added, removed, hunks = patch.get(path, ([], {}, []))
    elif merge_base and changed:
        diff_args = ['diff', '--no-color', '--no-ext-diff', merge_base] + ([ref] if ref else []) + ['--', path]
        added, removed, hunks = parse_patch(git(root, *diff_args) or '').get(path, ([], {}, []))
    return {'lines': lines, 'lang': lang_of(path), 'added': added, 'removed': removed, 'hunks': hunks,
            'changed': changed, 'truncated': truncated}


def lang_of(path: str) -> str | None:
    """highlight.js's name for the language of a file, from its extension (or its name, for a Dockerfile)."""
    name = path.rsplit('/', 1)[-1]
    return LANGS.get(name.rsplit('.', 1)[-1].lower() if '.' in name else name.lower())


class DiffError(Exception):
    """A diff review that cannot be built. The message says why, for the person who asked for it."""


def read_text(root: Path, path: str, ref: str | None, max_bytes: int | None = None) -> str | None:
    """A file's text at ``ref``, or in the working tree when ``ref`` is None. None when it is not text (a NUL in its
    first 8000 bytes), a symlink (it could point outside the repository), too large for ``max_bytes`` or unreadable."""
    if ref:
        text = git(root, 'show', f'{ref}:{path}')
    else:
        target = root / path
        try:
            if target.is_symlink() or (max_bytes is not None and target.stat().st_size > max_bytes):
                return None
            text = target.read_text(encoding='utf-8', errors='replace')
        except OSError:
            return None
    return None if text is None or '\0' in text[:8000] else text


def build_diff_page(spec: dict) -> tuple[Path, dict]:
    """A diff review: every change the branch holds since it left its base, committed or not, to read line by line.

    The base is ``spec['base']``, else the default base (``store.default_base``); the diff runs from the merge-base,
    as the changed-line markers of a review do. With uncommitted or untracked changes the page shows the working tree
    (like a review of it); on a clean branch it is pinned to HEAD, so the chat reads exactly what the page shows. It
    has no findings: the reviewer starts threads on the lines they select (see docs/behaviour.md, Diff review).
    Raises ``DiffError`` when there is nothing to show."""
    root = repo_root(spec['cwd'])
    if root is None:
        raise DiffError(f"{spec['cwd']} is not inside a git repository.")
    base = (spec.get('base') or '').strip() or store.default_base(root)
    if not base:
        raise DiffError('No base branch found. Name one: /findings diff origin/main')
    # A name that starts with a dash would be read by git as an option.
    if base.startswith('-') or git(root, 'rev-parse', '--verify', '--quiet', f'{base}^{{commit}}') is None:
        raise DiffError(f'{base} is not a branch or commit of {root.name}.')
    head = (git(root, 'rev-parse', 'HEAD') or '').strip()
    merge_base = (git(root, 'merge-base', base, 'HEAD') or '').strip()
    if not head or not merge_base:
        raise DiffError(f'{base} and HEAD share no history, so there is no branch to compare.')
    branch = (git(root, 'rev-parse', '--abbrev-ref', 'HEAD') or '').strip()
    branch = head[:8] if branch in ('', 'HEAD') else branch
    untracked = sorted(filter(None, (git(root, 'ls-files', '-z', '-o', '--exclude-standard') or '').split('\0')))
    uncommitted = bool(untracked) or bool((git(root, 'status', '--porcelain', '--untracked-files=no') or '').strip())
    ref = None if uncommitted else head
    # No rename detection: a moved file reads as a deleted one and an added one, each of which the page can show.
    diff = ['-c', 'core.quotepath=false', 'diff', '--no-ext-diff', '--no-renames']
    span = [merge_base, *([ref] if ref else [])]
    patch = git(root, *diff, '--no-color', *span, timeout=60)
    names = git(root, *diff, '--name-only', '-z', *span)
    if patch is None or names is None:
        raise DiffError(f'git could not diff {base} against {branch}.')
    detail = parse_patch_detail(patch)
    changed = set(filter(None, names.split('\0')))
    if not changed and not untracked:
        raise DiffError(f'No changes against {base} (merge-base {merge_base[:8]}): {branch} has nothing new, '
                        'committed or not.')

    files: dict[str, dict] = {}
    left = MAX_DIFF_LINES
    skipped: list[str] = []
    no_text = sorted(changed - set(detail))  # binary files and mode changes have no hunks
    for path, d in [(p, detail[p]) for p in sorted(detail)] + [(p, None) for p in untracked if p not in detail]:
        if len(files) >= MAX_DIFF_FILES:
            skipped.append(path)
            continue
        status = 'added' if d is None else d['status']
        lines: list[str] = []
        if status != 'deleted':
            text = read_text(root, path, ref, MAX_UNTRACKED_BYTES if d is None else None)
            if text is None:
                no_text.append(path)
                continue
            lines = text.split('\n')
            if lines and lines[-1] == '':
                lines.pop()
            if len(lines) > left:
                skipped.append(path)
                continue
        truncated = len(lines) > MAX_FILE_LINES
        lines = lines[:MAX_FILE_LINES]
        left -= len(lines)
        added = list(range(1, len(lines) + 1)) if d is None else [n for n in d['added'] if n <= len(lines)]
        removed = {} if d is None else d['removed']
        files[path] = {
            'lines': lines, 'lang': lang_of(path), 'added': added, 'removed': {k: len(v) for k, v in removed.items()},
            'hunks': [[1, len(lines)]] if d is None and lines else [] if d is None else d['hunks'],
            'changed': True, 'truncated': truncated, 'status': status,
            # The text of the removed lines, by the new line they follow: the page shows them in the diff.
            'removed_text': {str(k): v for k, v in removed.items()},
        }
    notes: list[str] = []
    if skipped:
        notes.append(f'{len(skipped)} changed files are not shown: the page keeps at most {MAX_DIFF_FILES} files and '
                     f'{MAX_DIFF_LINES} lines. Not shown: ' + ', '.join(skipped[:8]) + (', …' if len(skipped) > 8 else '') + '.')
    if no_text:
        notes.append(f'{len(no_text)} changed files have no text diff here (binary, a mode change, a link or too '
                     'large): ' + ', '.join(sorted(no_text)[:8]) + (', …' if len(no_text) > 8 else '') + '.')
    if not files:
        raise DiffError('The branch changed no file this page can show as text.')

    total_add = sum(len(f['added']) for f in files.values())
    total_del = sum(len(t) for f in files.values() for t in f['removed_text'].values())
    title = spec['title'] = spec.get('title') or f'Diff review · {branch}'
    out = Path(spec.get('out') or new_out_path(spec))
    data = {
        'page': out.parent.name, 'kind': 'diff', 'session_id': spec.get('session_id'),
        'review_session': spec.get('session_id'), 'pr': None, 'pr_author': None, 'ref': ref, 'title': title,
        'subtitle': spec.get('subtitle') or f'{root.name} · {branch} against {base} · {phrase(len(files), "file")}, '
                    f'+{total_add} −{total_del}',
        'repo_root': str(root), 'repo_name': root.name,
        'ref_label': f'commit {head[:8]} ({branch})' if ref else f'working tree ({branch})',
        'merge_base': merge_base[:8], 'base': base, 'github': github_base(root), 'blob_ref': head, 'notes': notes,
        'generated': dt.datetime.now().strftime('%Y-%m-%d %H:%M'),
        # No findings: the reviewer's threads live in the triage (decisions.json), one per selection of lines.
        'findings': [], 'files': files,
    }
    return out, data


def phrase(n: int, one: str) -> str:
    return f'{n} {one}' + ('' if n == 1 else 's')


def new_out_path(spec: dict) -> Path:
    """Where a new page goes: out/<YYYYmmdd-HHMMSS>-<slug>/page.html; the page's name is its folder's."""
    slug = re.sub(r'[^a-z0-9]+', '-', spec['title'].lower()).strip('-')[:50] or 'review'
    return store.html_path(f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{slug}")


def prune() -> None:
    for page in sorted(store.pages(), reverse=True)[KEEP_PAGES:]:
        reviews.delete_review(page)
    for p in sorted((paths.OUT / 'specs').glob('*.json'), reverse=True)[KEEP_PAGES:]:
        p.unlink(missing_ok=True)


def launch_render(spec: dict) -> Path:
    """Render in a detached process (reading the findings with Claude takes up to a few minutes); return the page path."""
    out = new_out_path(spec)
    spec['out'] = str(out)
    specs = paths.OUT / 'specs'
    specs.mkdir(parents=True, exist_ok=True)
    spec_path = specs / (out.parent.name + '.json')
    spec_path.write_text(json.dumps(spec))
    with paths.LOG.open('a') as logfh:
        subprocess.Popen([sys.executable, str(paths.CODE / 'finding_viewer.py'), 'render', str(spec_path)],
                         stdin=subprocess.DEVNULL, stdout=logfh, stderr=logfh, start_new_session=True, close_fds=True)
    return out


def latest_report(cwd: str) -> Path | None:
    """The newest saved review report in the session's repository: a file matching config report_glob."""
    root = repo_root(cwd) or Path(cwd)
    try:
        reports = [p for p in root.glob(config.get('report_glob')) if p.is_file() and p.name != '_learnings.md']
    except (ValueError, NotImplementedError):  # a pattern pathlib refuses
        return None
    return max(reports, key=lambda p: p.stat().st_mtime) if reports else None
