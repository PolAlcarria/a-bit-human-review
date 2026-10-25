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
    """The PR whose head is this commit. Skips PRs from the default branch, such as a develop → main release PR."""
    if not slug:
        return None
    try:
        r = subprocess.run(['gh', 'api', f'repos/{slug}/commits/{sha}/pulls'], cwd=root, capture_output=True, text=True,
                           timeout=20)
        pulls = json.loads(r.stdout) if r.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pulls = []
    for p in pulls:
        head = p.get('head') or {}
        if (head.get('sha') or '').startswith(sha.lower()) and head.get('ref') != (head.get('repo') or {}).get('default_branch'):
            return p['number']
    return None


def pr_patch(root: Path, pr: int, slug: str | None = None) -> dict | None:
    try:
        r = subprocess.run(['gh', 'pr', 'diff', str(pr), '--color=never', *(['--repo', slug] if slug else [])], cwd=root,
                           capture_output=True, encoding='utf-8', errors='replace', timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_patch(r.stdout) if r.returncode == 0 and r.stdout.strip() else None


def parse_patch(patch: str) -> dict[str, tuple[list[int], dict[int, int], list[list[int]]]]:
    """Map each file in a unified diff to (added lines, {line after which N lines were removed}, hunk ranges).

    All line numbers are new-side. Hunk ranges include context lines: that is where GitHub accepts inline comments.
    """
    files: dict[str, tuple[set[int], dict[int, int], list[list[int]]]] = {}
    added: set[int] | None = None
    removed: dict[int, int] = {}
    hunks: list[list[int]] = []
    in_hunk, new_line = False, 0
    for line in patch.splitlines():
        if line.startswith('diff --git '):
            in_hunk, added = False, None
            continue
        if not in_hunk and line.startswith('+++ '):
            target = line[4:].strip()
            if target != '/dev/null':
                added, removed, hunks = files.setdefault(target[2:] if target.startswith('b/') else target, (set(), {}, []))
            continue
        m = re.match(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@', line)
        if m:
            in_hunk, new_line = True, int(m.group(1))
            count = 1 if m.group(2) is None else int(m.group(2))
            if added is not None and count:
                hunks.append([new_line, new_line + count - 1])
            continue
        if not in_hunk or added is None:
            continue
        tag = line[:1]
        if tag == '+':
            added.add(new_line)
            new_line += 1
        elif tag == '-':
            removed[new_line - 1] = removed.get(new_line - 1, 0) + 1
        elif tag == ' ':
            new_line += 1
    return {path: (sorted(a), r, h) for path, (a, r, h) in files.items()}


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
    found no findings in the text and the parser found none either."""
    notes: list[str] = []
    slug = spec.get('repo')
    root = locate_repo(spec['cwd'], slug, notes)
    is_repo = repo_root(root) is not None
    ref, base, pr_head, pr_author = None, None, None, None
    if spec.get('reviewed_sha') and not spec.get('pr'):
        # A saved report pins a commit but names no PR: review it as the PR whose head is that commit.
        spec['pr'] = pr_for_commit(root, spec['reviewed_sha'], slug or (github_base(root) or '').removeprefix('https://github.com/'))
        if spec['pr']:
            log(f"render: reviewed commit {spec['reviewed_sha'][:8]} is the head of PR #{spec['pr']}")
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
    ext = path.rsplit('.', 1)[-1].lower() if '.' in path.rsplit('/', 1)[-1] else path.rsplit('/', 1)[-1].lower()
    return {'lines': lines, 'lang': LANGS.get(ext), 'added': added, 'removed': removed, 'hunks': hunks,
            'changed': changed, 'truncated': truncated}


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
