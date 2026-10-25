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
    """Directory holding exactly the reviewed code: the repo for working-tree reviews, else a detached worktree."""
    root, ref = data.get('repo_root') or str(Path.home()), reviewed_ref(data)
    if not ref:
        return root, False
    path = paths.WORKTREES / ref[:12]
    if (path / '.git').exists():
        os.utime(path)
        return str(path), True
    paths.WORKTREES.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', '-C', root, 'worktree', 'prune'], capture_output=True)
    r = subprocess.run(['git', '-C', root, 'worktree', 'add', '--detach', str(path), ref],
                       capture_output=True, text=True, timeout=180)
    if r.returncode:
        log(f'worktree add {ref[:12]} failed: {r.stderr.strip()[:300]}')
        return root, False
    _prune_worktrees(root, keep=path)
    return str(path), True


def _prune_worktrees(root: str, keep: Path) -> None:
    trees = sorted((p for p in paths.WORKTREES.iterdir() if p.is_dir() and p != keep), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in trees[KEEP_WORKTREES - 1:]:
        if subprocess.run(['git', '-C', root, 'worktree', 'remove', '--force', str(old)], capture_output=True).returncode:
            shutil.rmtree(old, ignore_errors=True)
            subprocess.run(['git', '-C', root, 'worktree', 'prune'], capture_output=True)
