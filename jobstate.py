"""The state machine every detached job on a page shares: drafting, posting and telling (draft.py), a re-review of
the new commits (rereview.py), a side chat reply (chat.py, one per thread, which loads and updates its thread
itself), and Send fixes (handoff.py). Each keeps its own fields and messages.

A job's state is a JSON record that carries its job id and, once its worker is spawned, the worker's pid:
- a worker writes only while the record still carries its job id (``update_if``), so a stopped or replaced job's
  worker stays quiet;
- the pid is recorded right after the spawn, so a worker that dies from then on reads as lost at once (``jobs.lost``);
- a lost worker is settled on the next read, under the lock (``settle``): what is left of its process group ends, and
  its spec goes (``end``);
- a stop marks the record ``stopping`` before the kill (``stop``), so a read that finds the killed worker first records
  a stop, not a failure.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Callable, ContextManager

import jobs


def now() -> str:
    """The local time to the second, as job states and chat messages record it."""
    return dt.datetime.now().isoformat(timespec='seconds')


def load(path: Path) -> dict | None:
    """The job state in ``path``: None when there is none, or it does not parse."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _lock(path: Path, lock: Callable[[], ContextManager[None]] | None) -> ContextManager[None]:
    """The state's lock: ``lock()`` when given, else ``jobs.locked(path)``."""
    return lock() if lock else jobs.locked(path)


def settle(path: Path, stalled: Callable[[dict], object], on_lost: Callable[[dict], None],
           lock: Callable[[], ContextManager[None]] | None = None) -> dict | None:
    """The job state in ``path``. When ``stalled`` finds no worker behind it, ``on_lost`` records the verdict at once,
    under the lock and checked again there, and the state is written."""
    st = load(path)
    if st and stalled(st):
        with _lock(path, lock):
            st = load(path)
            if st and stalled(st):
                on_lost(st)
                jobs.write_json(path, st)
    return st


def update_if(path: Path, job: str, fields: dict, lock: Callable[[], ContextManager[None]] | None = None,
              while_status: str | None = None) -> dict | None:
    """Update the job state with ``fields`` while it still carries ``job`` (and, with ``while_status``, still has that
    status): a stopped or replaced job's worker stays quiet. The new state, or None when the job is not this one any
    more."""
    with _lock(path, lock):
        st = load(path)
        if not st or st.get('job') != job or (while_status and st.get('status') != while_status):
            return None
        st.update(fields)
        jobs.write_json(path, st)
        return st


def end(rec: dict) -> str | None:
    """Settle a worker that is gone: end what is left of its process group (``jobs.end_group``), drop its spec, and take
    its job id off ``rec``, so nothing writes over the verdict. Returns that job id."""
    jobs.end_group(rec.get('pid'))
    job = rec.pop('job', None)
    jobs.drop_spec(job)
    return job


def stop(rec: dict, starting: str, mark: Callable[[], object], after: Callable[[], bool]) -> tuple[int, dict]:
    """Stop the running job ``rec`` with its claude process, refused while its worker has no pid yet (``starting`` says
    so). ``mark()`` marks it stopping first, through the job's own update: a poll that finds the killed worker before
    the stop is written then records it as stopped, not failed; when the job is not this one any more, nothing is
    stopped. Then the worker's process group ends, and ``after()`` records the stop and says whether it did."""
    if not rec.get('pid'):
        return 409, {'error': starting}
    if not mark():
        return 200, {'ok': True, 'stopped': False}
    jobs.kill_group(rec['pid'], rec['job'])
    return 200, {'ok': True, 'stopped': after()}


def record_stop(path: Path, job: str, running: str, on_stop: Callable[[dict], None],
                lock: Callable[[], ContextManager[None]] | None = None) -> bool:
    """After a stop's kill: ``on_stop`` records the stop while the state still says ``running`` for this job; when the
    job finished just before the stop, only its ``stopping`` mark goes. The job's spec goes too. Whether the state now
    says stopped."""
    with _lock(path, lock):
        cur = load(path) or {}
        if cur.get('status') == running and cur.get('job') == job:
            on_stop(cur)
            jobs.write_json(path, cur)
        elif cur.pop('stopping', None):  # it finished just before the stop
            jobs.write_json(path, cur)
    jobs.drop_spec(job)
    return bool(cur.get('stopped'))


def running_row(rec: dict, **row: object) -> dict:
    """A job that runs now, for the Running panel: ``row`` (its id, kind, label, session…) with when it started, its
    worker's pid and whether that is alive. A key in ``row`` wins."""
    pid = rec.get('pid')
    return {'state': 'running', 'started': rec.get('started'), 'pid': pid, 'alive': bool(pid) and jobs.alive(pid),
            **row}


def stop_fields(rec: dict, step: str | None) -> dict:
    """The rest of a running row that can be stopped: the step it is at ('Stopping' once a stop was asked for), and
    whether the panel offers Stop."""
    return {'step': 'Stopping' if rec.get('stopping') else step, 'stop': not rec.get('stopping')}


def ended_row(state: str, ended: float, error: str | None, **row: object) -> dict:
    """A job that ended, for the Running panel: ``row`` with how it ended (done, failed or stopped), when, and its error
    when it failed."""
    return {**row, 'state': state, 'ended': ended, 'error': error if state == 'failed' else None}
