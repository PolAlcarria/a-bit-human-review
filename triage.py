#!/usr/bin/env python3
"""The finding viewer's localhost server: it serves the review pages and the list of reviews, stores the triage
(``out/<page>/decisions.json``), and starts, shows and stops the jobs on a page (side chats, Draft comments and posting,
re-reviews of new commits, Send fixes). While a re-review runs on a page (rereview.py), every write to that page is
refused with 423. ``cmd_job`` is where a detached job worker starts.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import chat
import config
import freshness
import draft
import jobs
import paths
import render
import rereview
import reviews
import sessions
import store
from handoff import mark_handed, page_state, run_send, send_activity, send_fixes, send_state, summarize
from store import decisions_path

PORT = config.get('port')
ORIGIN = f'http://127.0.0.1:{PORT}'
ALLOWED_HOSTS = {f'127.0.0.1:{PORT}', f'localhost:{PORT}'}
ALLOWED_ORIGINS = {ORIGIN, f'http://localhost:{PORT}'}
# Scripts run only with the response's fresh nonce: the inline one and the three cdnjs libraries, which also carry SRI.
CSP_VIEWER = ("default-src 'none'; script-src 'nonce-{nonce}'; "
              "style-src 'unsafe-inline' https://cdnjs.cloudflare.com; connect-src 'self'; "
              "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
CSP_INDEX = ("default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; connect-src 'self'; "
             "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
# A string here pins the version: code changes no longer restart a running server until it is None again.
SERVER_VERSION_OVERRIDE: str | None = None


def _listed_files(found) -> list:
    """The files in ``found``. An entry that cannot be stat'ed is skipped; a listing that fails part-way keeps what it
    found before the failure."""
    files = []
    try:
        for path in found:
            try:
                if path.is_file():
                    files.append(path)
            except OSError:
                continue
    except OSError:
        pass
    return files


def source_version(root: Path = paths.CODE) -> str:
    """A SHA-1 over the server's code: every *.py directly in ``root`` (the modules; tests/ and the data folder's
    links are not code the server runs) and every file under ``root``/web whose path has no dot name (.DS_Store,
    editor swap files), sorted by path, each with its path. Any change to them gives another version, so
    ensure_server replaces a server still running the old code. An entry that cannot be listed or read is left out
    rather than stopping the import (the hooks import this module)."""
    web = root / 'web'
    files = _listed_files(root.glob('*.py'))
    files += [p for p in _listed_files(web.rglob('*'))
              if not any(part.startswith('.') for part in p.relative_to(web).parts)]
    digest = hashlib.sha1()
    for path in sorted(files):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        digest.update(path.relative_to(root).as_posix().encode() + b'\0' + data + b'\0')
    return digest.hexdigest()[:12]


SERVER_VERSION = SERVER_VERSION_OVERRIDE or source_version()
IDLE_SECONDS = 8 * 3600
MAX_BODY = 2_000_000
# No leading dot: a page is a folder in out/, so '..' or a .staging folder must never name one.
NAME_RE = re.compile(r'^(?!\.)[\w.-]{1,200}$')

_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


log = paths.log()


def page_url(page_file: Path) -> str:
    """The page's address; ``page_file`` is its page.html (render.new_out_path)."""
    return f'{ORIGIN}/{page_file.parent.name}.html'


# ---------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    server_version = 'finding-viewer'
    last_seen = time.time()

    def log_message(self, *args) -> None:
        pass

    def _send(self, code: int, body: bytes, ctype: str = 'application/json', csp: str | None = None) -> None:
        self.send_response(code)
        self.send_header('Content-Type', f'{ctype}; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        if csp:
            self.send_header('Content-Security-Policy', csp)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj).encode())

    def _host_ok(self) -> bool:
        # Rejecting foreign Host headers blocks DNS-rebinding pages from reading the notes.
        return self.headers.get('Host') in ALLOWED_HOSTS

    def _write_ok(self) -> bool:
        # The custom header forces a CORS preflight for any other origin, which this server never approves.
        origin = self.headers.get('Origin')
        return self._host_ok() and self.headers.get('X-Finding-Viewer') == '1' and (origin is None or origin in ALLOWED_ORIGINS)

    def do_GET(self) -> None:
        Handler.last_seen = time.time()
        if not self._host_ok():
            return self._json(403, {'error': 'forbidden host'})
        path = urlparse(self.path).path
        if path == '/api/ping':
            return self._json(200, {'app': 'finding-viewer', 'version': SERVER_VERSION, 'pid': os.getpid()})
        if path.startswith('/api/decisions/'):
            name = path.rsplit('/', 1)[-1]
            if not NAME_RE.match(name):
                return self._json(400, {'error': 'bad page name'})
            p = decisions_path(name)
            return self._send(200, p.read_bytes()) if p.is_file() else self._json(200, {})
        if path == '/api/settings':
            return self._json(200, chat.settings_payload())
        if path.startswith('/api/freshness/'):
            name = path.rsplit('/', 1)[-1]
            if not NAME_RE.match(name):
                return self._json(400, {'error': 'bad page name'})
            return self._json(200, freshness.check(name, force='force=1' in (urlparse(self.path).query or '')))
        if path.startswith('/api/chat/'):
            name = path.rsplit('/', 1)[-1]
            if not NAME_RE.match(name):
                return self._json(400, {'error': 'bad page name'})
            return self._json(200, chat.snapshot(name))
        if path.startswith('/api/handoff/'):
            name = path.rsplit('/', 1)[-1]
            if not NAME_RE.match(name):
                return self._json(400, {'error': 'bad page name'})
            return self._json(200, handoff(name))
        if path.startswith('/api/activity/'):
            name = path.rsplit('/', 1)[-1]
            if not NAME_RE.match(name):
                return self._json(400, {'error': 'bad page name'})
            return self._json(200, activity(name))
        if path == '/api/reviews':
            return self._json(200, {'reviews': reviews.list_reviews()})
        if path in ('/', '/index.html'):
            nonce = secrets.token_urlsafe(16)
            body = reviews.index_page(nonce).encode()
            return self._send(200, body, 'text/html', csp=CSP_INDEX.format(nonce=nonce))
        name = path.lstrip('/')
        if NAME_RE.match(name) and name.endswith('.html'):
            page = name[:-len('.html')]
            if page == 'latest':  # out/latest.html links to the newest page's page.html
                try:
                    real = reviews.LATEST.resolve(strict=True)
                except OSError:
                    real = None
                page = real.parent.name if real and real.name == 'page.html' and real.parent.parent == paths.OUT.resolve() else ''
            folder = store.page_dir(page) if page else None
            if (folder and not folder.is_symlink() and folder.resolve().parent == paths.OUT.resolve()
                    and not store.html_path(page).is_symlink() and store.exists(page)):
                nonce = secrets.token_urlsafe(16)
                body = reviews.page_html(page, nonce)
                # no review.json (a page rendered by an old version without a data block): served as is, without a CSP
                if body is None:
                    return self._send(200, store.html_path(page).read_bytes(), 'text/html')
                return self._send(200, body, 'text/html', csp=CSP_VIEWER.format(nonce=nonce))
        self._json(404, {'error': 'not found'})

    def do_PUT(self) -> None:
        Handler.last_seen = time.time()
        if not self._write_ok():
            return self._json(403, {'error': 'forbidden'})
        path = urlparse(self.path).path
        if path == '/api/settings':
            length = int(self.headers.get('Content-Length') or 0)
            try:
                body = json.loads(self.rfile.read(length)) if 0 < length <= MAX_BODY else None
            except ValueError:
                body = None
            if not isinstance(body, dict):
                return self._json(400, {'error': 'expected {"model": ..., "context": ...}'})
            return self._json(*chat.save_settings(body))
        name = path.rsplit('/', 1)[-1]
        if path.startswith('/api/archive/') and NAME_RE.match(name):
            length = int(self.headers.get('Content-Length') or 0)
            try:
                body = json.loads(self.rfile.read(length)) if 0 < length <= MAX_BODY else None
            except ValueError:
                body = None
            if not isinstance(body, dict) or not isinstance(body.get('archived'), bool):
                return self._json(400, {'error': 'expected {"archived": true|false}'})
            # Archiving covers every render of the PR: none of them may be under a re-review. Their locks are taken in
            # name order (nothing else holds two), and held while the archive is written.
            with contextlib.ExitStack() as held:
                pages = sorted({name, *reviews.group_pages(name)})
                running = next(filter(None, (held.enter_context(rereview.guard(p)) for p in pages)), None)
                archived = None if running else reviews.set_archived(name, body['archived'])
            if running:
                return self._json(423, rereview.locked_error(running))
            if not archived:
                return self._json(404, {'error': 'unknown page'})
            return self._json(200, {'ok': True})
        if not path.startswith('/api/decisions/') or not NAME_RE.match(name) or not store.exists(name):
            return self._json(404, {'error': 'unknown page'})
        length = int(self.headers.get('Content-Length') or 0)
        if not 0 < length <= MAX_BODY:
            return self._json(413, {'error': 'body too large'})
        try:
            data = json.loads(self.rfile.read(length))
        except ValueError:
            return self._json(400, {'error': 'invalid json'})
        if not isinstance(data, dict) or not isinstance(data.get('findings'), dict):
            return self._json(400, {'error': 'expected {"findings": {...}}'})
        with rereview.guard(name) as running:
            if running:
                return self._json(423, rereview.locked_error(running))
            if stale := rereview.stale(name, data):
                return self._json(409, {'error': stale})
            data['saved_at'] = dt.datetime.now().isoformat(timespec='seconds')
            target = decisions_path(name)
            tmp = target.with_suffix('.tmp')
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
            tmp.replace(target)
        self._json(200, {'ok': True, 'saved_at': data['saved_at']})

    def do_POST(self) -> None:
        Handler.last_seen = time.time()
        if not self._write_ok():
            return self._json(403, {'error': 'forbidden'})
        parts = urlparse(self.path).path.strip('/').split('/')
        if parts == ['api', 'shutdown']:
            self._json(200, {'ok': True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if len(parts) == 4 and parts[:2] == ['api', 'chat'] and NAME_RE.match(parts[2]) and NAME_RE.match(parts[3]):
            length = int(self.headers.get('Content-Length') or 0)
            if not 0 < length <= MAX_BODY:
                return self._json(413, {'error': 'body too large'})
            try:
                message = json.loads(self.rfile.read(length)).get('message', '')
            except (ValueError, AttributeError):
                return self._json(400, {'error': 'expected {"message": "..."}'})
            with rereview.guard(parts[2]) as running:
                if running:
                    return self._json(423, rereview.locked_error(running))
                code, body = chat.start(parts[2], parts[3], message, note=freshness.heads_up(parts[2]))
            return self._json(code, body)
        if parts[:2] == ['api', 'stop'] and len(parts) >= 4 and all(NAME_RE.match(p) for p in parts[2:]):
            if parts[3:] == ['draft']:
                return self._json(*draft.stop(parts[2]))
            if parts[3:] == ['rereview']:
                return self._json(*rereview.stop(parts[2]))
            if len(parts) == 5 and parts[3] == 'chat':
                return self._json(*chat.stop(parts[2], parts[4]))
        if len(parts) == 3 and parts[:2] == ['api', 'rereview'] and NAME_RE.match(parts[2]) and store.exists(parts[2]):
            length = int(self.headers.get('Content-Length') or 0)
            if length > MAX_BODY:
                return self._json(413, {'error': 'body too large'})
            try:
                payload = json.loads(self.rfile.read(length)) if length else {}
            except ValueError:
                return self._json(400, {'error': 'expected {"discard_drafts": true|false}'})
            return self._json(*start_rereview(parts[2], payload if isinstance(payload, dict) else {}))
        if len(parts) in (3, 4) and parts[:2] == ['api', 'draft'] and NAME_RE.match(parts[2]) and store.exists(parts[2]):
            if len(parts) == 3:
                with rereview.guard(parts[2]) as running:
                    answer = (423, rereview.locked_error(running)) if running else draft_review(parts[2])
                return self._json(*answer)
            if parts[3] == 'post':
                length = int(self.headers.get('Content-Length') or 0)
                if not 0 < length <= MAX_BODY:
                    return self._json(413, {'error': 'body too large'})
                try:
                    payload = json.loads(self.rfile.read(length))
                except ValueError:
                    return self._json(400, {'error': 'expected the review as JSON'})
                with rereview.guard(parts[2]) as running:
                    answer = (423, rereview.locked_error(running)) if running else \
                        post_drafts(parts[2], payload if isinstance(payload, dict) else {})
                return self._json(*answer)
        if len(parts) == 3 and parts[:2] == ['api', 'send-fixes'] and NAME_RE.match(parts[2]) and store.exists(parts[2]):
            with rereview.guard(parts[2]) as running:
                answer = (423, rereview.locked_error(running)) if running else start_send_fixes(parts[2])
            return self._json(*answer)
        self._json(404, {'error': 'not found'})

    def do_DELETE(self) -> None:
        Handler.last_seen = time.time()
        if not self._write_ok():
            return self._json(403, {'error': 'forbidden'})
        parts = urlparse(self.path).path.strip('/').split('/')
        if len(parts) == 3 and parts[:2] == ['api', 'draft'] and NAME_RE.match(parts[2]):
            with rereview.guard(parts[2]) as running:
                answer = (423, rereview.locked_error(running)) if running else draft.discard(parts[2])
            return self._json(*answer)
        if len(parts) != 3 or parts[:2] != ['api', 'reviews'] or not NAME_RE.match(parts[2]):
            return self._json(404, {'error': 'not found'})
        with rereview.guard(parts[2]) as running:
            if running:
                answer = 423, rereview.locked_error(running)
            # A running worker writes into the page's files and would leave them behind.
            elif activity(parts[2])['running']:
                answer = 409, {'error': 'Something still runs on this review (a draft, a post or a side chat). '
                                        'Delete it once that ends.'}
            else:
                answer = (200, {'ok': True}) if reviews.delete_review(parts[2]) else (404, {'error': 'unknown page'})
        self._json(*answer)


def serve() -> int:
    paths.migrate_if_needed()
    paths.migrate_layout()
    paths.OUT.mkdir(parents=True, exist_ok=True)
    try:
        server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    except OSError as exc:
        log(f'serve: cannot bind {ORIGIN}: {exc!r}')
        return 1

    def stop_when_idle() -> None:
        while True:
            time.sleep(60)
            if time.time() - Handler.last_seen > IDLE_SECONDS:
                server.shutdown()
                return

    threading.Thread(target=stop_when_idle, daemon=True).start()
    log(f'serve: listening on {ORIGIN} (pid {os.getpid()})')
    server.serve_forever()
    return 0


def _call(method: str, path: str, timeout: float = 0.6) -> dict | None:
    req = urllib.request.Request(ORIGIN + path, method=method, headers={'X-Finding-Viewer': '1'})
    try:
        with _NO_PROXY.open(req, timeout=timeout) as r:
            return json.loads(r.read() or b'{}')
    except (OSError, ValueError, http.client.HTTPException):
        return None


def ensure_server() -> bool:
    info = _call('GET', '/api/ping')
    if info and info.get('app') == 'finding-viewer' and info.get('version') == SERVER_VERSION:
        return True
    if info and info.get('app') == 'finding-viewer':
        _call('POST', '/api/shutdown')
        time.sleep(0.5)
    with paths.LOG.open('a') as logfh:
        subprocess.Popen([sys.executable, str(paths.CODE / 'triage.py'), 'serve'], stdin=subprocess.DEVNULL,
                         stdout=logfh, stderr=logfh, start_new_session=True, close_fds=True)
    for _ in range(30):
        time.sleep(0.1)
        info = _call('GET', '/api/ping', timeout=0.3)
        if info and info.get('version') == SERVER_VERSION:
            return True
    return False


def replace_old_server() -> None:
    """Replace a server that runs other code (``ensure_server``); start none when no server answers. The layout
    migration calls it: a server still running the old code reads the files it just moved."""
    info = _call('GET', '/api/ping')
    if info and info.get('app') == 'finding-viewer' and info.get('version') != SERVER_VERSION:
        ensure_server()


def activity(page: str) -> dict:
    """The page's Running panel: what runs now (drafting, posting and telling, side chats, Send fixes, Check again), what
    ended in the last ten minutes, and the Claude sessions behind them, with the command to resume each one not running
    now."""
    data = store.page_data(page) or {}
    st, chat_state, rr = draft.state(page), chat.settled(page), rereview.state(page)
    work = (draft.activity(st) + chat.activity(chat_state) + rereview.activity(rr) + send_activity(send_state(page))
            + jobs.checks(page))
    now = time.time()
    running = [j for j in work if j['state'] == 'running']
    ended = sorted((j for j in work if j['state'] != 'running' and now - j['ended'] < jobs.RECENT_SECONDS),
                   key=lambda j: j['ended'], reverse=True)
    busy, lives = {j['session'] for j in running if j.get('session')}, sessions.live_sessions()
    kept = None if config.get('delete_forks') else 'kept: delete_forks is off'
    idle_note = {'review': 'closed', 'fork': kept or 'deleted when you post, re-draft or discard',
                 'rereview': kept or 'deleted when the re-review ends'}

    def session(kind: str, label: str, sid: str, cwd: str | None) -> dict:
        # A session that runs now (open in Claude Code, or used by a job here) gets no resume command.
        live = lives.get(sid)
        note = (f"open in {live.get('name') or 'Claude Code'}" if live else 'in use by a running job' if sid in busy
                else idle_note.get(kind))
        return {'kind': kind, 'label': label, 'id': sid, 'name': (live or {}).get('name'), 'note': note,
                'cwd': (cwd or '').replace(str(Path.home()), '~', 1) or None,
                'resume': None if live or sid in busy else sessions.resume_command(cwd, sid)}

    rows = []
    if rs := sessions.review_session(data):
        rows.append(session('review', 'Review session', rs[0], rs[1]))
    if st and sessions.transcript(st.get('fork')):
        rows.append(session('fork', 'Draft fork', st['fork'], st.get('cwd')))
    if rr and sessions.transcript(rr.get('fork')):
        fork = session('rereview', 'Re-review fork', rr['fork'], rr.get('cwd'))
        if rr.get('status') != 'reviewing':
            # The re-review ended with its fork open in Claude Code, so the fork was kept.
            fork['note'] = (f"kept: open in {fork['name'] or 'Claude Code'}" if rr['fork'] in lives
                            else kept or 'kept: it was open in Claude Code when the re-review ended')
        rows.append(fork)
    for fid, th in sorted((chat_state.get('threads') or {}).items()):
        sid = (th.get('pending') or {}).get('session') or th.get('claude_session')
        if sid and sessions.transcript(sid):
            rows.append(session('chat', f'Chat on {fid}', sid, th.get('cwd')))
    # The page locks itself while a re-review runs, and offers one when the PR moved on (rereview.view).
    return {'running': running, 'ended': ended, 'sessions': rows, 'rereview': rereview.view(data, rr)}


_LOGIN_FAILED = [0.0]  # when the gh login last failed: own_code() does not ask again for 5 minutes


def own_code(data: dict) -> bool:
    """Whether the reviewed code is the reviewer's own, so a finding can be fixed before anyone else reviews it: a review
    with no PR (a local branch or the working tree), or a PR the gh user opened."""
    if not data.get('pr'):
        return True
    author = data.get('pr_author')
    if not author or (not freshness.LOGIN and time.time() - _LOGIN_FAILED[0] < 300):
        return False
    login = freshness.viewer_login()
    if not login:
        _LOGIN_FAILED[0] = time.time()
    return author == login


def handoff(page: str) -> dict:
    """The review session (its name when open), whether the page can draft and post, whether the code is the reviewer's
    own, the drafts so far, and the last Send fixes."""
    data = store.page_data(page) or {}
    _dec, _chat, owner, version = page_state(page)
    owner = owner or data.get('session_id')
    live = sessions.live_session(owner) if owner and sessions.SESSION_RE.match(owner) else None
    session = {'id': owner, 'name': live.get('name'), 'status': live.get('status'),
               'cwd': (live.get('cwd') or '').replace(str(Path.home()), '~', 1) or None} if live else None
    return {'session': session, 'can_draft': draft.can_draft(data), 'own_code': own_code(data),
            'draft': draft.state(page), 'send': send_state(page), 'version': version}


def draft_review(page: str) -> tuple[int, dict]:
    """Draft comments: a fork of the review session drafts the review from the triage (see draft.py)."""
    data = store.page_data(page)
    if not data:
        return 404, {'error': 'unknown page'}
    dec, chat_state, _owner, version = page_state(page)
    fixed = rereview.fixed(data)  # nothing to post for a finding the new commits fixed
    if not any(v.get('status') == 'post' and k not in fixed for k, v in (dec.get('findings') or {}).items()):
        return 400, {'error': 'Mark at least one finding Post first.'}
    notes = [n for n in (freshness.heads_up(page), freshness.pr_note(page)) if n]
    prompt = draft.PROMPT + summarize(page, dec, chat_state, for_claude=False) + ''.join(f'\n{n}' for n in notes)
    own = {k: {'body': v['note'].strip(), 'anchor': v.get('anchor')} for k, v in (dec.get('findings') or {}).items()
           if v.get('status') == 'post' and v.get('mode') == 'comment' and str(v.get('note') or '').strip()
           and k not in fixed}
    return draft.start(page, data, prompt, version, own)


def start_rereview(page: str, payload: dict) -> tuple[int, dict]:
    """Review the new commits: a fork of the review session re-checks the findings at the PR's new head, and the page
    is brought up to date with the triage carried over (see rereview.py)."""
    data = store.page_data(page)
    if not data:
        return 404, {'error': 'unknown page'}
    dec, chat_state, _owner, _version = page_state(page)
    return rereview.start(page, data, summarize(page, dec, chat_state, for_claude=False),
                          payload.get('discard_drafts') is True)


def post_drafts(page: str, payload: dict) -> tuple[int, dict]:
    """Approve & post: post the edited drafts (the worker then calls ``mark_handed``)."""
    data = store.page_data(page)
    if not data:
        return 404, {'error': 'unknown page'}
    return draft.post(page, data, payload)


def start_send_fixes(page: str) -> tuple[int, dict]:
    """Send fixes to session: the triage goes to the open review session now, not with its next prompt (see
    handoff.send_fixes). Only on the reviewer's own code, where nothing is posted."""
    data = store.page_data(page)
    if not data:
        return 404, {'error': 'unknown page'}
    if not own_code(data):
        return 409, {'error': 'Send fixes is for a review of your own code: no PR, or a PR you opened.'}
    return send_fixes(page, data)


def cmd_job(spec_path: str) -> int:
    """A detached worker (``finding_viewer.py job``): drafting, posting + telling, a side chat reply, a re-review of the
    PR's new commits, or sending fixes to the review session (see jobs.py)."""
    path = Path(spec_path)
    spec = json.loads(path.read_text(encoding='utf-8'))
    try:
        if spec['kind'] == 'draft':
            draft.run_draft(spec)
        elif spec['kind'] == 'post':
            draft.run_post(spec, mark_handed)
        elif spec['kind'] == 'chat':
            chat.run(spec)
        elif spec['kind'] == 'rereview':
            rereview.run(spec, render.build_page, render.write_page)
        elif spec['kind'] == 'send':
            run_send(spec)
    finally:
        path.unlink(missing_ok=True)
    return 0


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'serve':
        sys.exit(serve())
    print('usage: triage.py serve', file=sys.stderr)
    sys.exit(2)
