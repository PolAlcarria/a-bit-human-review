"""Background jobs on a review page: drafting, posting + telling, side chats, re-reviews of new commits, Send fixes, and
Check again.

Drafting, posting + telling, side chats, re-reviews and Send fixes each run as their own detached process
(``finding_viewer.py job <spec>``, in a session of its own like ``render``), so a server restart or shutdown never kills
them. The worker writes its state file itself (``drafts.json``, ``chat.json``, ``rereview.json``, ``send.json``); the
server only starts it, reads its state and stops it on request.
The server and several workers share those files, so every read-modify-write holds an ``fcntl.flock`` on a sidecar
lock file in the page folder's ``locks/``, and every write replaces the file atomically.

A state that says running records its worker's pid: a worker that died (killed, crashed) reads as failed at once, and
a live one is never given up on, whatever the clock says (a laptop that slept mid-draft); each worker has its own
watchdog. Check again runs inside a request, so the server's memory is enough to track it, and one check per page runs
at a time.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Iterator

import paths

SPECS = paths.OUT / 'specs' / 'jobs'
RECENT_SECONDS = 600
STOP_GRACE_SECONDS = 3
START_GRACE_SECONDS = 30
CHECK_WAIT_SECONDS = 300

# Freshness checks by page: the one running now, or one that ended in the last RECENT_SECONDS, for single-flight and
# the Running panel.
_checks: dict[str, dict] = {}
_flights: dict[str, dict] = {}
_checks_lock = threading.Lock()


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    """Hold an exclusive lock for a read-modify-write of ``path``, between processes and between threads alike. The lock
    is a sidecar in the folder's ``locks/``: ``out/<page>/locks/chat.lock`` for ``out/<page>/chat.json``."""
    lock = path.parent / 'locks' / f'{path.stem}.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


_held = threading.local()


@contextlib.contextmanager
def page_lock(page: str) -> Iterator[None]:
    """The page's own lock (``out/<page>/locks/rereview.lock``): a re-review holds it while it changes the page, and every
    write to the page takes it (rereview.py). Re-entrant within a thread, so code holding it can call code that takes
    it; a second ``flock`` on a new descriptor in the same process would wait for itself."""
    held = _held.__dict__.setdefault('pages', set())
    if page in held:
        yield
        return
    with locked(paths.OUT / page / 'rereview.json'):
        held.add(page)
        try:
            yield
        finally:
            held.discard(page)


def write_json(path: Path, data: dict, **dump: object) -> None:
    """Replace ``path`` atomically, so a reader never sees half a file. Call it holding ``locked(path)``."""
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, **dump), encoding='utf-8')
    tmp.replace(path)


def new_id() -> str:
    """A job's id: its spec file's name, and the mark a worker checks before it writes, so a stopped job stays quiet."""
    return secrets.token_hex(6)


def alive(pid: int) -> bool:
    """Whether the process exists; another user's process counts, the way ``sessions.live_session`` reads it."""
    if not isinstance(pid, int) or pid <= 0:  # kill(0, …) would ask about this process's own group
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def lost(rec: dict) -> bool:
    """Whether a state that says running has no worker behind it: its pid is gone; or it has none, because it predates
    the workers (no job id: a server before version 17 ran it in a thread) or its server died before recording one."""
    if rec.get('pid'):
        return not alive(rec['pid'])
    return not rec.get('job') or time.time() - rec.get('started', 0) > START_GRACE_SECONDS


def end_group(pid: int | None) -> None:
    """A dead worker's ``claude`` child can outlive it in the worker's process group: end it too. The group id is the
    dead worker's pid, which no new process gets while that group still exists, so nothing else is hit."""
    if pid:
        with contextlib.suppress(OSError):
            os.killpg(pid, signal.SIGTERM)


def spawn(spec: dict) -> int:
    """Start a detached worker for ``spec`` (it needs ``job``; see ``triage.cmd_job``) and return its pid."""
    SPECS.mkdir(parents=True, exist_ok=True)
    path = SPECS / f"{spec['job']}.json"
    path.write_text(json.dumps(spec), encoding='utf-8')
    try:
        with paths.LOG.open('a') as logfh:
            proc = subprocess.Popen([sys.executable, str(paths.CODE / 'finding_viewer.py'), 'job', str(path)],
                                    stdin=subprocess.DEVNULL, stdout=logfh, stderr=logfh, start_new_session=True,
                                    close_fds=True)
    except OSError:
        path.unlink(missing_ok=True)
        raise
    # Reaped as soon as it ends: an unreaped worker lingers as a zombie, which still answers kill(pid, 0).
    threading.Thread(target=proc.wait, daemon=True).start()
    return proc.pid


def drop_spec(job: str | None) -> None:
    """Remove a job's spec once its worker is gone without removing it itself (stopped, killed)."""
    if job and all(c in '0123456789abcdef' for c in job):
        (SPECS / f'{job}.json').unlink(missing_ok=True)


def drop_specs(page: str) -> None:
    """Remove every job spec left for a page that is being deleted."""
    for path in SPECS.glob('*.json'):
        try:
            if json.loads(path.read_text(encoding='utf-8')).get('page') == page:
                path.unlink(missing_ok=True)
        except (OSError, ValueError):
            continue


def is_worker(pid: int, job: str) -> bool:
    """Whether the process is still that job's worker (its command line names the job's spec), not a reused pid."""
    try:
        r = subprocess.run(['ps', '-o', 'command=', '-p', str(pid)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return True  # cannot tell: the pid was recorded for this job, so trust it
    return f'{job}.json' in r.stdout


def kill_group(pid: int, job: str | None = None) -> None:
    """Stop a worker and everything it started: SIGTERM to its process group, then SIGKILL if it is still there.

    The worker leads its own session, so its ``claude`` child is in the same group. With ``job``, nothing is signalled
    unless the pid is still that job's worker."""
    if job and not is_worker(pid, job):
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + STOP_GRACE_SECONDS
    while time.time() < deadline:
        try:
            os.killpg(pid, 0)
        except OSError:
            return
        time.sleep(0.1)
    with contextlib.suppress(OSError):
        os.killpg(pid, signal.SIGKILL)


# ---------------------------------------------------------------- Check again

def checking(page: str) -> bool:
    return page in _flights


def check_once(page: str, force: bool, run: Callable[[], dict]) -> dict:
    """Run one freshness check for the page, unless one is running already: then wait for it and return its result.

    However many clicks, tabs or polls ask, one gh / git run per page happens at a time. A forced request (Check again)
    shows in the Running panel, also when it joins a check the periodic poll started."""
    with _checks_lock:
        _forget_old_checks()
        flight = _flights.get(page)
        lead = flight is None
        if lead:
            flight = _flights[page] = {'done': threading.Event(), 'result': None}
            _checks[page] = {'id': 'check', 'kind': 'check', 'label': 'Check again', 'state': 'running',
                             'started': time.time(), 'shown': force}
        elif force and not _checks[page].get('shown'):
            _checks[page] = {**_checks[page], 'shown': True}
    if not lead:
        if flight['done'].wait(CHECK_WAIT_SECONDS) and flight['result'] is not None:
            return flight['result']
        return {'status': 'unknown', 'reason': 'Another check of this review is still running.'}
    state, error = 'done', None
    try:
        result = run()
    except Exception as exc:  # noqa: BLE001 — the requests waiting for this check need an answer too
        state, error = 'failed', repr(exc)
        result = {'status': 'unknown', 'reason': f'The check failed: {error}'}
    with _checks_lock:
        flight['result'] = result
        del _flights[page]
        _checks[page] = {**_checks[page], 'state': state, 'error': error, 'ended': time.time()}
    flight['done'].set()
    return result


def _forget_old_checks() -> None:
    """Drop the record of each check that ended before the Running panel's window; call it holding _checks_lock."""
    for page, rec in list(_checks.items()):
        if rec['state'] != 'running' and time.time() - rec['ended'] > RECENT_SECONDS:
            del _checks[page]


def checks(page: str) -> list[dict]:
    """The page's Check again for the Running panel: running, or ended lately, as long as someone forced it."""
    with _checks_lock:
        _forget_old_checks()
        rec = _checks.get(page)
    return [{k: v for k, v in rec.items() if k != 'shown'}] if rec and rec.get('shown') else []
