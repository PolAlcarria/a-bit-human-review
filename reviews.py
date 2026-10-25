"""The stored review pages: list them for the index, serve them with the current template, archive and delete them.

Each page is a folder ``out/<page>/``. Its page.html keeps the template it was rendered with, but the server always
serves the page's ``review.json`` inside the current template, so changes to the page reach old reviews too.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import shutil
import threading
from pathlib import Path

import chat
import freshness
import draft
import jobs
import paths
import rereview
import sessions
import store

WEB = paths.CODE / 'web'
LATEST = paths.OUT / 'latest.html'
ARCHIVE = paths.OUT / 'archived.json'
_archive_lock = threading.Lock()


def _web(name: str) -> str:
    return (WEB / name).read_text(encoding='utf-8')


def _inline(page: str, **parts: str) -> str:
    """``page`` with each ``/*__NAME__*/`` placeholder replaced by the web/ file ``parts`` names for it."""
    for name, file in parts.items():
        page = page.replace(f'/*__{name}__*/', _web(file).rstrip('\n'))
    return page


def viewer_template() -> str:
    """The review page's template as one file: web/viewer.html with shared.css, viewer.css and viewer.js put in its
    <style> and <script> blocks, so the served page and page.html need nothing beside them."""
    return _inline(_web('viewer.html'), SHARED_CSS='shared.css', CSS='viewer.css', JS='viewer.js')


def index_page(nonce: str) -> str:
    """The list of reviews as one file: web/index.html with shared.css put in, its script marked with ``nonce``."""
    return _inline(_web('index.html'), SHARED_CSS='shared.css').replace('__NONCE__', nonce)


def fill_template(data: dict, nonce: str = '') -> str:
    """The page for this review data. ``nonce`` marks the inline script for the server's CSP; a file on disk gets ''."""
    payload = json.dumps(data, ensure_ascii=False).replace('<', '\\u003c')
    # The nonce goes in before any review text, so text that happens to contain the placeholder is left alone.
    page = (viewer_template().replace('__NONCE__', nonce)
            .replace('__TITLE__', html.escape(data['title'])))
    return page.replace('/*__DATA__*/', payload)


def page_html(page: str, nonce: str = '') -> bytes | None:
    """The page's review data inside the current template, or None for a page without review data (review.json)."""
    data = store.page_data(page)
    return fill_template(data, nonce).encode() if data else None


def _created(page: str) -> str:
    try:
        when = dt.datetime.strptime(page[:15], '%Y%m%d-%H%M%S')
    except ValueError:
        when = dt.datetime.fromtimestamp(store.html_path(page).stat().st_mtime)
    return when.isoformat(timespec='seconds')


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _handed(page: str, st: dict | None, owner: str | None, version: str, seen: dict) -> dict | None:
    """Where the triage went: drafts on the page (drafting, ready, posted…) or Claude got it with a message there."""
    if st:
        return {'draft': st['status'], 'error': st.get('error'), 'stopped': bool(st.get('stopped')),
                'url': (st.get('posted') or {}).get('url'), 'at': (st.get('posted') or {}).get('at')}
    if owner and page in (seen.get(owner) or {}):
        return {'draft': None, 'current': (seen.get(owner) or {}).get(page) == version}
    return None


def group_key(data: dict) -> str:
    """The list shows one card per PR in a repo, or per title for reviews without a PR."""
    repo, pr = data.get('repo_name') or '', data['pr']
    return f'{repo}#{pr}' if pr else f"{repo}:{data['title']}"


def group_pages(page: str) -> list[str]:
    """Every stored render in the page's card (the same PR in the same repo); [] for an unknown page."""
    data = store.page_data(page) if store.exists(page) else None
    if not data:
        return []
    key = group_key(data)
    return [p for p in store.pages() if (d := store.page_data(p)) and group_key(d) == key]


def set_archived(page: str, on: bool) -> bool:
    """Archive the page's card (every stored render in its group), or bring it back. False for an unknown page.

    A render made later is not archived, so a new review of the same PR brings the card back by itself."""
    if not store.exists(page) or not store.page_data(page):
        return False
    pages = group_pages(page)
    with _archive_lock:
        state = {k: v for k, v in _load_json(ARCHIVE).items() if store.exists(k)}
        now = dt.datetime.now().isoformat(timespec='seconds')
        for p in pages:
            if on:
                state.setdefault(p, now)
            else:
                state.pop(p, None)
        tmp = ARCHIVE.with_suffix('.tmp')
        tmp.write_text(json.dumps(state, indent=1), encoding='utf-8')
        tmp.replace(ARCHIVE)
    return True


def list_reviews() -> list[dict]:
    """Every stored page, newest first, with its triage, chat, last freshness check, whether Claude got it, what runs
    on it now and whether it is archived."""
    rows = []
    seen = _load_json(paths.INJECT_STATE)
    archived = _load_json(ARCHIVE)
    for page in sorted(store.pages(), reverse=True):
        data = store.page_data(page)
        if not data:
            continue
        dec, chat_state, st = store.load_decisions(store.decisions_path(page)) or {}, chat.settled(page), draft.state(page)
        rr = rereview.state(page)
        decided, fixed = dec.get('findings') or {}, rereview.fixed(data)
        # A finding the new commits fixed is not counted: there is nothing left to decide or post for it.
        statuses = [(decided.get(f['id']) or {}).get('status') for f in data.get('findings', []) if f['id'] not in fixed]
        threads = (chat_state.get('threads') or {}).values()
        owner = dec.get('session_id') or chat_state.get('session_id') or data.get('session_id')
        repo, pr, ref = data.get('repo_name') or '', data['pr'], store.reviewed_ref(data)
        rows.append({
            'page': page, 'title': data['title'], 'subtitle': data.get('subtitle', ''), 'repo': repo, 'pr': pr,
            'pr_url': f"{data['github']}/pull/{pr}" if pr and data.get('github') else None,
            'pr_author': data.get('pr_author'),
            'group': group_key(data), 'archived': page in archived,
            'created': _created(page), 'reviewed': (ref or '')[:8], 'checkable': bool(ref),
            'findings': len(statuses), 'post': statuses.count('post'), 'skip': statuses.count('skip'),
            'fix': statuses.count('fix'), 'fixed': len(fixed), 'chats': sum(1 for t in threads if t.get('messages')),
            'running': [j['label'] for j in draft.activity(st) + chat.activity(chat_state) + rereview.activity(rr)
                        + jobs.checks(page) if j['state'] == 'running'],
            # A re-review runs on it: Archive and Delete wait (the server refuses them with 423 meanwhile).
            'locked': bool(rr and rr.get('status') == 'reviewing'),
            'freshness': freshness.load_cached(page),
            'handed': _handed(page, st, owner, store.page_version(dec, chat_state), seen),
        })
    return rows


def delete_review(page: str) -> bool:
    """Remove a page's folder, with everything stored for it, and its specs. False when there is no such page."""
    folder = store.page_dir(page)
    if folder.is_symlink() or not store.exists(page):
        return False
    st = draft.state(page) or {}
    sessions.delete_session(st.get('fork'), keep=st.get('review_session'))
    shutil.rmtree(folder, ignore_errors=True)
    (paths.OUT / 'specs' / f'{page}.json').unlink(missing_ok=True)
    jobs.drop_specs(page)
    if LATEST.is_symlink() and not LATEST.exists():
        LATEST.unlink()
        # Page names start with their creation time, so the greatest name is the newest page.
        newest = max(store.pages(), default=None)
        if newest:
            LATEST.symlink_to(store.html_path(newest))
    return not store.exists(page)
