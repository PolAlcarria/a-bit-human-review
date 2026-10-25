"""Review the PR's new commits in a fork of the review session, and bring the page up to date in place.

The freshness check says when the PR moved past the reviewed commit; this reviews those commits. A fork of the review
session (``claude -p --resume <review session> --fork-session``, as Draft comments does) inherits the whole review,
reads the new head in a detached worktree, reviews what the new commits changed and re-checks every finding. It answers
through ``--json-schema``: per finding, still applies, changed, fixed or new. The page is then built again at the new
head under the same name (``render.build_page``), and the triage carries over by finding id: a comment line the
reviewer picked moves through the diff, or is reset when that line itself changed.

It runs in a detached worker (``jobs.spawn``) that writes ``out/<page>/rereview.json``. That state is also the page's
lock: while it says reviewing and its worker lives, every write to the page is refused (423, ``guard`` and ``locked``),
so the triage cannot change underneath it. Only a success touches the page, in one step under that lock; a failure or
a Stop leaves it exactly as it was. The fork is deleted when the job ends, unless it is open in Claude Code.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import re
import shutil
import time
from pathlib import Path
from typing import Callable, Iterator

import chat
import claude_cli
import config
import draft
import extract
import freshness
import jobs
import jobstate
import paths
import sessions
import store
from severity import LABEL as SEVERITY_LABEL, LETTER, SEVERITIES

TIMEOUT_SECONDS = 900
STATUSES = ('still_applies', 'changed', 'fixed', 'new')
LABEL = 'Reviewing new commits'
ENDED_LABEL = 'Review of the new commits'
MAX_BODY_CHARS = 4000
# How each outcome is counted in words (one, many), and how a side chat on the finding is told it.
SAY = {'fixed': ('fixed', 'fixed'), 'still_applies': ('still applies', 'still apply'), 'changed': ('changed', 'changed'),
       'new': ('new', 'new')}
WHAT = {'still_applies': 'still applies', 'changed': 'still applies, changed', 'fixed': 'fixed', 'new': 'new'}

SCHEMA = {
    'type': 'object',
    'properties': {
        'findings': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'id': {'type': 'string', 'description': 'The finding id from the page (W1); for a new finding, the next free '
                                                    'id for its severity letter'},
            'status': {'type': 'string', 'enum': list(STATUSES)},
            'severity': {'type': 'string', 'enum': list(SEVERITIES)},
            'title': {'type': 'string', 'description': 'One line'},
            'body': {'type': 'string', 'description': 'The finding for the reviewer, markdown'},
            'recurring': {'type': 'boolean'},
            'why': {'type': 'string', 'description': 'For fixed: how the new commits fixed it, one line'},
            'locations': {'type': 'array', 'items': {'type': 'object', 'properties': {
                'path': {'type': 'string', 'description': 'Repo-relative path at the new head'},
                'start': {'type': 'integer', 'description': 'First line at the new head'},
                'end': {'type': 'integer', 'description': 'Last line at the new head (= start for one line)'},
            }, 'required': ['path', 'start']}},
        }, 'required': ['id', 'status', 'severity', 'title', 'body', 'locations']}},
    },
    'required': ['findings'],
}

PROMPT = """{moved} The reviewer wants those commits reviewed, and the findings on the finding-viewer page brought up \
to date, before anything is posted. Check what you need with the read-only tools, but do not post or change anything.

{where} What the new commits changed: `git diff {ref12} {head12}` and `git log {ref12}..{head12}`. Run one command per \
Bash call: pipes, ";", "&&" and "$(...)" are blocked.

1. Review what the new commits changed, the way you reviewed the PR. Each new problem they bring in is a new finding.
2. Re-check each finding below against the new head.

Return JSON, one entry per finding:
- Every finding below, with its id and a status: still_applies (the same problem; only its lines may have moved), \
changed (still a problem, but what it says or where it sits changed: rewrite its title and body), or fixed (the new \
commits fixed it: say how in why, one line). A finding fixed in an earlier re-review may be left out while it stays \
fixed.
- Each new problem: status new, with the next free id for its severity letter ({next_ids}). Never reuse an id the page \
already has.
- severity: critical (must-fix), warning (should-fix), suggestion, question, or finding (no severity). title: one \
line. body: the finding for the reviewer, in markdown. recurring: true when the reviewer marked it recurring. The page \
keeps the review's own title, body and severity for a finding that still applies or is fixed: write them out for \
changed and new findings.
- locations: where the finding is at the new head: path relative to the repository root, start and end lines, most \
relevant first, at most 8. A fixed finding needs none.

Reviewed commit: {ref}
New head: {head}
{commits}

The findings on the page, at the reviewed commit:
{findings}

{summary}
"""


log = paths.log('rereview: ')


def state_path(page: str) -> Path:
    return store.page_dir(page) / 'rereview.json'


def _load(page: str) -> dict | None:
    return jobstate.load(state_path(page))


def _write(page: str, st: dict) -> None:
    """Write the re-review state; hold ``jobs.page_lock(page)`` from the load before it."""
    jobs.write_json(state_path(page), st)


def _put(staged: Path, target: Path) -> None:
    """One step of an update: a staged file replaces the page's file."""
    staged.replace(target)


def _drop_staged(page: str, job: str | None) -> None:
    """Remove what a job staged but never put in place (it died or failed first)."""
    if job:
        shutil.rmtree(store.page_dir(page) / f'.staging-{job}', ignore_errors=True)


def _stalled(st: dict) -> bool:
    """Whether a state that says reviewing has no worker behind it any more (``jobs.lost``). A live worker is never
    given up on: it has its own watchdog, and the clock may have jumped while the laptop slept."""
    return st.get('status') == 'reviewing' and jobs.lost(st)


def _close(st: dict, **fields: object) -> None:
    """End the job in ``st`` as ``fields`` say. The fork goes, unless it is open in Claude Code (then it stays on
    record, so the next re-review can delete it)."""
    fork = st.get('fork')
    sessions.delete_session(fork, keep=st.get('review_session'))
    st.pop('stopping', None)
    st.update(fork=fork if fork and sessions.live_session(fork) else None, ended=time.time(), **fields)


def _mark_stopped(st: dict) -> None:
    st.pop('job', None)
    _close(st, status='failed', stopped=True, error='Stopped by you. The page is unchanged.')


def _fail_stalled(page: str, st: dict) -> None:
    """Record a lost worker: as failed (or as the stop it was) when it died before its update began, and then nothing
    of the page changed; as done when it died during the update, which is finished here, since everything it was to
    put in place was already staged. Its job id goes, so nothing writes over that. Hold the page lock."""
    pid = st.get('pid')
    job = jobstate.end(st)
    if st.get('committing'):
        _commit(page, st, late=True)
        return
    _drop_staged(page, job)
    if st.get('stopping'):
        _mark_stopped(st)  # a Stop killed it, and this read came first
    else:
        _close(st, status='failed', error=(f'The re-review process stopped (pid {pid}).' if pid else
                                           'The re-review process stopped without an answer.') + ' The page is unchanged.')


def _commit(page: str, st: dict, late: bool = False) -> None:
    """Put the staged files of an update in place, in the order recorded (the triage, the chats, the freshness check,
    the drafts, and the page last, page.html then review.json: the review data says which commit the page is at, to each
    tab and to ``stale``), then record the job as done. Each step may run twice, so a worker that died halfway is
    finished by the next read (``late``). Hold the page lock; the caller writes the state."""
    base = store.page_dir(page)
    for step in st.get('committing') or []:
        if step.get('put'):
            if (base / step['put']).exists():
                _put(base / step['put'], base / step['to'])
        elif step.get('drop'):
            (base / step['drop']).unlink(missing_ok=True)
        elif step.get('discard_drafts') and unposted(page):
            # Only drafts nobody posted: a post that landed meanwhile keeps its state and its link.
            draft.discard(page)
    for stage in base.glob('.staging-*'):
        shutil.rmtree(stage, ignore_errors=True)
    cached = freshness.load_cached(page)
    if cached and cached.get('ref') != st['to']:
        freshness.cache_path(page).unlink(missing_ok=True)  # a check of the old commit landed between the steps
    result = dict(st.get('result') or {})
    if late:
        result['message'] = result.get('message', '').removesuffix('.') + ' (completed after the worker stopped).'
    st.pop('committing', None)
    _close(st, status='done', error=None, result=result)


def state(page: str) -> dict | None:
    """The page's last re-review: reviewing, done or failed. A worker that is gone is settled at once and recorded so
    (``_fail_stalled``): the page unlocks."""
    return jobstate.settle(state_path(page), _stalled, lambda st: _fail_stalled(page, st), lambda: jobs.page_lock(page))


def locked(page: str) -> dict | None:
    """The re-review running on the page, which locks it; None when nothing does."""
    st = state(page)
    return st if st and st.get('status') == 'reviewing' else None


@contextlib.contextmanager
def guard(page: str) -> Iterator[dict | None]:
    """Hold the page's re-review lock around a write. Yields the re-review running on the page, and the caller then
    refuses the write (423); else None, and the write happens under the lock, so it cannot land while a re-review that
    just ended updates the page."""
    if not store.exists(page):
        yield None  # no such page: nothing to lock, and no lock file left behind (the caller answers 404)
        return
    with jobs.page_lock(page):
        st = _load(page)
        if st and _stalled(st):
            _fail_stalled(page, st)
            _write(page, st)
        yield st if st and st.get('status') == 'reviewing' else None


def locked_error(_st: dict) -> dict:
    return {'error': 'Claude is reviewing the new commits of this review, so nothing on it can change until that ends. '
                     'Stop the re-review (on the page, or in Sessions) to unlock it now.', 'locked': True}


def stale(page: str, payload: dict) -> str | None:
    """Why a triage save from a tab that loaded the page before its last re-review is refused (it would put the old
    findings and lines back), or None when the tab is up to date."""
    done = (store.page_data(page) or {}).get('rereviewed') or []
    if not done or payload.get('rereviews') == len(done):
        return None
    return (f"This review was re-reviewed at {done[-1]['to'][:8]} after this tab loaded it, so this change was not "
            'saved. Reload the page.')


def unavailable(data: dict) -> str | None:
    """Why the page cannot review new commits, or None when it can."""
    if not store.reviewed_ref(data):
        return 'This page shows the working tree, so there is no reviewed commit to compare new commits with.'
    if not sessions.review_session(data):
        return "The review session's transcript is gone, so there is no review to fork."
    return None


def fixed(data: dict) -> dict[str, dict]:
    """The page's findings a re-review found fixed, by id: they stay listed, but there is nothing to post for them."""
    return {f['id']: f['rereview'] for f in data.get('findings') or [] if (f.get('rereview') or {}).get('status') == 'fixed'}


def said(counts: dict) -> str:
    """'1 fixed, 1 still applies, 2 new'."""
    parts = [f'{counts[k]} {SAY[k][counts[k] != 1]}' for k in ('fixed', 'still_applies', 'changed', 'new') if counts.get(k)]
    return ', '.join(parts) or 'no findings'


def heads_up(data: dict) -> str | None:
    """One line for Claude when the page was re-reviewed at a newer commit: what it showed before, and what changed."""
    done = data.get('rereviewed') or []
    if not done:
        return None
    last = done[-1]
    return (f"The page was re-reviewed at {last['to'][:10]} after the PR got new commits (it showed {last['from'][:10]} "
            f"before): {said(last.get('counts') or {})}. The ids and lines below are at {last['to'][:10]}.")


def view(data: dict, st: dict | None) -> dict:
    """The re-review as the page needs it: whether it can run (and why not), and the last one's state."""
    why = unavailable(data)
    out = {'can': not why, 'why': why}
    if st:
        out.update({k: st.get(k) for k in ('status', 'started', 'ended', 'from', 'to', 'commits', 'rewritten', 'error',
                                           'stopped', 'stopping', 'result', 'discard_drafts')})
    return out


def activity(st: dict | None) -> list[dict]:
    """The re-review for the Running panel: running now, or ended and when."""
    if not st:
        return []
    if st.get('status') == 'reviewing':
        return [jobstate.running_row(st, id='rereview', kind='rereview', label=LABEL, session=st.get('fork'),
                                     **jobstate.stop_fields(st, (st.get('steps') or ['Starting'])[-1]))]
    if not st.get('ended'):
        return []
    state_ = 'done' if st.get('status') == 'done' else 'stopped' if st.get('stopped') else 'failed'
    return [jobstate.ended_row(state_, st['ended'], st.get('error'), id='rereview', kind='rereview', label=ENDED_LABEL)]


def _update(page: str, job: str, **fields: object) -> dict | None:
    """Change the state while this job is still reviewing: a stopped or replaced job's worker stays quiet."""
    return jobstate.update_if(state_path(page), job, fields, lambda: jobs.page_lock(page), while_status='reviewing')


def _others(page: str) -> list[dict]:
    """What else runs on the page and writes to it (drafting, posting, telling, side chats): a re-review waits for it."""
    return [j for j in draft.activity(draft.state(page)) + chat.activity(chat.settled(page)) if j['state'] == 'running']


def unposted(page: str) -> bool:
    """Whether the page has drafts nobody posted yet: they point at the old lines, so a re-review discards them."""
    st = draft.state(page)
    return bool(st and st.get('review') and st.get('status') in ('ready', 'failed'))


def start(page: str, data: dict, summary: str, discard_drafts: bool) -> tuple[int, dict]:
    """Start a re-review of the commits the PR got since the reviewed one. ``summary`` is the triage as the hand-off
    hook gives it; ``discard_drafts`` confirms that unposted drafts go when the re-review succeeds."""
    review = sessions.review_session(data)
    if (why := unavailable(data)) or not review:
        return 409, {'error': why or "The review session's transcript is gone, so there is no review to fork."}
    claude = claude_cli.binary()
    if not Path(claude).exists():
        return 500, {'error': 'claude CLI not found on PATH'}
    if running := locked(page):  # said before the check below, which may call GitHub (and again under the lock)
        return 423, locked_error(running)
    fresh = freshness.check(page)  # the cached check while it is under a minute old
    head, ref = fresh.get('head'), store.reviewed_ref(data)
    if fresh.get('status') not in ('behind', 'rewritten') or not head or head == ref:
        return 409, {'error': 'There is nothing new to review: the last check finds no commit after the reviewed one. '
                              'Check again if you expected some.'}
    if not freshness.has_commit(data.get('repo_root') or '', head):
        return 409, {'error': f'The new head {head[:8]} is not in the local clone, so it cannot be read. Check again '
                              'to fetch it.'}
    commits = [{'sha': c.get('sha'), 'title': c.get('title')} for c in fresh.get('new_commits') or []]
    job = jobs.new_id()
    with jobs.page_lock(page):
        old = _load(page)
        if old and _stalled(old):
            _fail_stalled(page, old)
            _write(page, old)
        if old and old.get('status') == 'reviewing':
            return 423, locked_error(old)
        if store.reviewed_ref(store.page_data(page) or {}) != ref:
            return 409, {'error': 'The page moved to another commit meanwhile. Start again.'}
        if running := _others(page):
            # Posting and telling cannot be stopped (GitHub may already have the review).
            stop = ', or stop it in Sessions' if all(j.get('stop') for j in running) else ''
            return 409, {'error': f"Wait until {', '.join(j['label'] for j in running)} ends{stop}."}
        if unposted(page) and not discard_drafts:
            return 409, {'error': 'This discards the unposted drafts, which refer to the old lines. Confirm to go on.',
                         'drafts': True}
        sessions.delete_session((old or {}).get('fork'), keep=review[0])
        st = {'status': 'reviewing', 'started': time.time(), 'job': job, 'pid': None, 'fork': None, 'steps': [],
              'review_session': review[0], 'cwd': review[1], 'from': ref, 'to': head, 'commits': len(commits),
              'rewritten': fresh.get('status') == 'rewritten', 'discard_drafts': bool(discard_drafts)}
        _write(page, st)
    try:
        pid = jobs.spawn({'kind': 'rereview', 'job': job, 'page': page, 'summary': summary, 'commits': commits,
                          'affected': fresh.get('affected') or {}})
    except OSError as exc:
        error = f'The re-review process did not start: {exc!r}'
        with jobs.page_lock(page):
            cur = _load(page) or {}
            if cur.get('job') == job:
                _close(cur, status='failed', error=error)
                _write(page, cur)
        return 500, {'error': error}
    # Recorded at once: a worker that dies from here on reads as failed (and the page unlocks) on the next poll.
    return 202, _update(page, job, pid=pid) or st


def stop(page: str) -> tuple[int, dict]:
    """Stop the re-review: end the worker with its claude process, delete the fork, and unlock the page unchanged."""
    st = state(page) or {}
    if st.get('status') != 'reviewing':
        return 409, {'error': 'Nothing to stop: the re-review already ended.'}
    # After the kill it is settled like a lost worker: stopped with the page unchanged, or, when the kill landed during
    # the update, the update finished from what was staged.
    return jobstate.stop(st, 'The re-review process is still starting. Try again in a moment.',
                         lambda: _update(page, st['job'], stopping=True),
                         lambda: jobstate.record_stop(state_path(page), st['job'], 'reviewing',
                                                      lambda cur: _fail_stalled(page, cur), lambda: jobs.page_lock(page)))


# ---------------------------------------------------------------- the worker

def taken_ids(page: str, data: dict) -> set[str]:
    """Every finding id the page ever showed: its findings (a fixed one stays listed), its triage and its chats."""
    try:
        dec = json.loads(store.decisions_path(page).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        dec = {}
    return ({f['id'] for f in data.get('findings') or []} | set((dec if isinstance(dec, dict) else {}).get('findings') or {})
            | set(store.load_chat(page).get('threads') or {}))


def next_id(letter: str, taken: set[str]) -> str:
    """The next id for a severity letter: one past the highest the page ever used, so no id is shown twice."""
    n = max((int(m.group(1)) for t in taken if (m := re.fullmatch(rf'{letter}(\d+)', t))), default=0)
    return f'{letter}{n + 1}'


def _anchor(path: str, start: int, end: int) -> dict:
    return {'raw': f'{path}:{start}' + (f'-{end}' if end > start else ''), 'path': path, 'start': start, 'end': end,
            'pos': 0}


def _anchors(locs: object) -> list[dict]:
    """The answer's locations as the renderer takes them, like ``extract.to_findings`` reads them."""
    out = []
    for loc in (locs if isinstance(locs, list) else [])[:extract.MAX_LOCATIONS]:
        if not isinstance(loc, dict):
            continue
        path = str(loc.get('path') or '').strip()
        try:
            start = int(loc.get('start') or 0)
            end = int(loc.get('end') or start)
        except (TypeError, ValueError):
            continue
        if path and start >= 1:
            out.append(_anchor(path, start, min(max(end, start), start + 400)))
    return out


def parse_answer(out: object, data: dict, taken: set[str]) -> list[dict]:
    """The fork's answer, checked: one entry per finding on the page, each with a known status; new findings get ids
    the page never showed. Raises ValueError saying what is wrong, so the job fails with a clear error."""
    items = out.get('findings') if isinstance(out, dict) else None
    if not isinstance(items, list):
        raise ValueError('it has no list of findings')
    known = {f['id']: f for f in data.get('findings') or []}
    seen: set[str] = set()
    taken = set(taken)
    answer = []
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            raise ValueError(f'entry {i} is not an object')
        status = it.get('status')
        fid = re.sub(r'[^\w.-]', '', str(it.get('id') or ''))[:12]
        if status not in STATUSES:
            raise ValueError(f"{fid or f'entry {i}'} has the status {status!r}, which is not one of {', '.join(STATUSES)}")
        severity = it.get('severity') if it.get('severity') in SEVERITIES else None
        title = ' '.join(str(it.get('title') or '').split())[:200]
        if status == 'new':
            if not title:
                raise ValueError(f'new finding {i} has no title')
            severity = severity or 'finding'
            fid = next_id(LETTER[severity], taken)
            taken.add(fid)
        elif fid not in known:
            raise ValueError(f"{fid or f'entry {i}'} is not a finding on the page, so it cannot be {status}")
        elif fid in seen:
            raise ValueError(f'{fid} comes back twice')
        seen.add(fid)
        recurring = it.get('recurring') if isinstance(it.get('recurring'), bool) else None
        answer.append({'id': fid, 'status': status, 'severity': severity, 'title': title,
                       'body': str(it.get('body') or '').strip(), 'recurring': recurring,
                       'why': ' '.join(str(it.get('why') or '').split())[:300], 'anchors': _anchors(it.get('locations'))})
    left = [fid for fid, f in known.items() if fid not in seen and (f.get('rereview') or {}).get('status') != 'fixed']
    if left:
        raise ValueError(f"it leaves out {', '.join(left)}: every finding on the page needs a status")
    return answer


def _moved(changed: dict, f: dict) -> list[dict]:
    """A finding's locations carried to the new head through the diff, without those whose lines changed."""
    out = []
    for loc in f.get('locations') or []:
        info = changed.get(loc.get('path') or '')
        if not loc.get('path') or (info and freshness.hits(info['ranges'], loc['start'], loc['end'])):
            continue
        a, b = freshness.map_line(changed, loc['path'], loc['start']), freshness.map_line(changed, loc['path'], loc['end'])
        if a and b:
            out.append(_anchor(a[0], a[1], b[1]))
    return out


def new_findings(data: dict, answer: list[dict], changed: dict, head: str) -> tuple[list[dict], dict]:
    """The page's findings at the new head as ``build_page`` takes them, each with what the re-review found
    (``rereview``), and how many of each outcome. A finding the answer left out was fixed before and stays so."""
    by_id = {a['id']: a for a in answer if a['status'] != 'new'}
    counts = dict.fromkeys(STATUSES, 0)
    out = []

    def spec(fid: str, severity: str, title: str, body: str, recurring: bool, anchors: list[dict], meta: dict,
             rr: dict | None) -> dict:
        return {'id': fid, 'severity': severity, 'title': title, 'body': body, 'recurring': recurring,
                'anchors': anchors, 'meta': meta, 'rereview': rr}

    for f in data.get('findings') or []:
        a = by_id.get(f['id'])
        # "line: looked up by Claude" was about the old anchors.
        meta = {k: v for k, v in (f.get('meta') or {}).items() if k != 'line'}
        if a is None:
            out.append(spec(f['id'], f['severity'], f['title'], f.get('body', ''), f.get('recurring', False),
                            _moved(changed, f), meta, f.get('rereview')))
            continue
        counts[a['status']] += 1
        rr = {'status': a['status'], 'at': head, **({'why': a['why']} if a['status'] == 'fixed' and a['why'] else {})}
        # Only a changed finding takes the fork's words; one that still applies, or is fixed, keeps the review's own.
        reworded = a['status'] == 'changed'
        out.append(spec(f['id'], (reworded and a['severity']) or f['severity'], (reworded and a['title']) or f['title'],
                        (reworded and a['body']) or f.get('body', ''),
                        f.get('recurring', False) if a['recurring'] is None else a['recurring'],
                        a['anchors'] or _moved(changed, f), meta, rr))
    for a in answer:
        if a['status'] == 'new':
            counts['new'] += 1
            out.append(spec(a['id'], a['severity'], a['title'], a['body'], bool(a['recurring']), a['anchors'], {},
                            {'status': 'new', 'at': head}))
    return out, counts


def default_anchor(data: dict, f: dict) -> dict | None:
    """The line a finding's comment goes on unless the reviewer picks one: the first flagged line inside a diff hunk,
    else the first flagged line (``defaultAnchor`` on the page)."""
    loc = next((loc for loc in f.get('locations') or [] if loc.get('path')), None)
    file = (data.get('files') or {}).get(loc['path']) if loc else None
    if not file:
        return None
    n = len(file['lines'])
    for line in range(loc['start'], min(loc['end'], n) + 1):
        if any(a <= line <= b for a, b in file.get('hunks') or []):
            return {'path': loc['path'], 'line': line}
    return {'path': loc['path'], 'line': min(loc['start'], n)}


def carry_triage(dec: dict, old: dict, new: dict, changed: dict, head: str) -> dict:
    """The triage at the new head. Decisions and notes stay with their finding id; a comment line the reviewer picked
    moves through the diff, or goes back to the first flagged line when that line itself changed (``anchor_reset``
    says so on the page, until the next re-review); a finding marked Post that changed gets ``recheck``; new findings
    start undecided."""
    old_by_id = {f['id']: f for f in old.get('findings') or []}
    findings = {}
    for f in new.get('findings') or []:
        v = dict((dec.get('findings') or {}).get(f['id']) or {'status': None, 'note': '', 'mode': 'note'})
        v.update(title=f['title'], severity=f['severity'], body=f['body'])
        v.pop('anchor_reset', None)  # said once, by the re-review that reset it
        saved, before = v.get('anchor') or {}, default_anchor(old, old_by_id.get(f['id']) or {}) or {}
        anchor = None
        if saved.get('path') and saved.get('line') and (saved['path'], saved['line']) != (before.get('path'), before.get('line')):
            moved = freshness.map_line(changed, saved['path'], int(saved['line']))
            if moved and moved[0] in {loc['path'] for loc in f['locations'] if loc.get('path')}:
                anchor = {'path': moved[0], 'line': moved[1]}
            else:
                v['anchor_reset'] = {'path': saved['path'], 'line': saved.get('line'), 'at': head}
        anchor = anchor or default_anchor(new, f)
        v['anchor'] = {**anchor, 'in_diff': draft.in_diff(new, anchor['path'], anchor['line'])} if anchor else None
        rr = f.get('rereview') or {}
        if rr.get('status') == 'changed' and rr.get('at') == head and v.get('status') == 'post':
            v['recheck'] = head
        elif rr.get('status') == 'fixed':
            v.pop('recheck', None)
        findings[f['id']] = v
    # Triage of ids the page no longer shows stays as it was.
    findings.update({k: v for k, v in (dec.get('findings') or {}).items() if k not in findings})
    now = dt.datetime.now(dt.timezone.utc)
    return {**dec, 'page': new['page'], 'title': new['title'], 'subtitle': new.get('subtitle', ''),
            'session_id': new.get('session_id'), 'pr': new.get('pr'), 'repo_root': new.get('repo_root'),
            # Newer than any copy a browser kept, so the page takes this one.
            'updated': now.isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
            'saved_at': dt.datetime.now().isoformat(timespec='seconds'), 'findings': findings}


def _outcome(rr: dict, head: str) -> str:
    """What a re-review at ``head`` found for a finding, in words: 'still applies', 'fixed (why)'…"""
    what = WHAT.get(rr.get('status'), 'not re-checked')
    if rr.get('status') == 'fixed' and rr.get('at') != head:
        what = 'fixed in an earlier re-review'
    return what + (f" ({rr['why']})" if rr.get('status') == 'fixed' and rr.get('why') else '')


def _noted_chats(page: str, new: dict, ref: str, head: str) -> dict | None:
    """The page's chats with one line in each thread on a finding: the code moved on (the next message there tells
    Claude too). None when there is no thread. Nothing else writes the chats while the page is locked."""
    outcome = {f['id']: f.get('rereview') or {} for f in new.get('findings') or []}
    state_ = store.load_chat(page)
    threads = [(fid, th) for fid, th in (state_.get('threads') or {}).items() if th.get('messages')]
    for fid, th in threads:
        what = _outcome(outcome.get(fid) or {}, head)
        th['messages'].append({
            'role': 'system', 'at': jobstate.now(), 'rereview': head,
            'text': f'Re-reviewed at {head[:8]}: {what}. This chat read the code at {ref[:8]}; your next message '
                    'tells Claude the code moved.',
            'claude': f'Note from the review page: the PR got new commits, and the review was re-checked at commit '
                      f'{head[:12]} (this chat started on {ref[:12]}). This finding: {what}. From now on read the '
                      f'code at the new head: {paths.WORKTREES / head[:12]}, or `git show {head[:12]}:<path>`.'})
    if not threads:
        return None
    state_['updated'] = jobstate.now()
    return state_


def _settled_freshness(page: str, head: str) -> dict | None:
    """The page now shows the head the last check found: that check says current. None when a check found a newer
    head meanwhile: it compared it with the old commit, so it goes, and the next one starts afresh."""
    cached = freshness.load_cached(page)
    if not cached or cached.get('head') != head:
        return None
    kept = {k: v for k, v in cached.items() if k not in ('affected', 'reason')}
    return {**kept, 'status': 'current', 'ref': head, 'new_commits': [], 'compare_url': None}


def _stage(page: str, job: str, st: dict, old: dict, new: dict, changed: dict,
           write_page: Callable[[Path, dict], None]) -> list[dict]:
    """Write every file the update changes, complete, into the page's staging folder (``.staging-<job>/``), and return
    the steps that put them in place, in order (``_commit``): the triage, the chats, the freshness check, the drafts,
    the page html and its review data."""
    stage = store.page_dir(page) / f'.staging-{job}'
    stage.mkdir(exist_ok=True)

    def staged(target: Path, text: str) -> dict:
        (stage / target.name).write_text(text, encoding='utf-8')
        return {'put': f'{stage.name}/{target.name}', 'to': target.name}

    ref, head = st['from'], st['to']
    dec_path = store.decisions_path(page)
    try:
        dec = json.loads(dec_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        dec = {}
    steps = [staged(dec_path, json.dumps(carry_triage(dec if isinstance(dec, dict) else {}, old, new, changed, head),
                                         ensure_ascii=False, indent=1))]
    if (chats := _noted_chats(page, new, ref, head)) is not None:
        steps.append(staged(store.chat_path(page), json.dumps(chats, ensure_ascii=False, indent=1)))
    fresh = _settled_freshness(page, head)
    steps.append(staged(freshness.cache_path(page), json.dumps(fresh)) if fresh else
                 {'drop': freshness.cache_path(page).name})
    if st.get('discard_drafts'):
        steps.append({'discard_drafts': True})
    # render.write_page writes page.html and review.json side by side.
    write_page(stage / 'page.html', new)
    steps += [{'put': f'{stage.name}/page.html', 'to': 'page.html'},
              {'put': f'{stage.name}/review.json', 'to': 'review.json'}]
    return steps


def build_prompt(data: dict, st: dict, spec: dict, checkout: str | None, taken: set[str]) -> str:
    ref, head, pr = st['from'], st['to'], data.get('pr')
    commits = spec.get('commits') or []
    n = len(commits)
    if st.get('rewritten'):
        moved = (f"PR #{pr} was force-pushed since your review" if pr else 'The branch was rewritten since your review') + \
                f': the reviewed commit {ref[:12]} is no longer part of it, and its head is now {head[:12]}.'
        listed = 'The reviewed commit is no longer in the history: compare the two commits as a whole.'
    else:
        moved = (f'PR #{pr} has {n} new commit{"s" * (n != 1)} since your review.' if pr else
                 f'The branch has {n} new commit{"s" * (n != 1)} since your review.')
        listed = 'New commits:\n' + '\n'.join(f"- {str(c.get('sha'))[:12]} {c.get('title') or ''}" for c in commits)
    where = (f'The new head is checked out at {checkout}: read and search the code there, by absolute path. Your working '
             'directory may be on another commit.' if checkout else
             f'Read the code at the new head with `git show {head[:12]}:<path>`: your working directory may be on '
             'another commit.')
    affected, done, rows = spec.get('affected') or {}, fixed(data), []
    for f in data.get('findings') or []:
        at = ', '.join(f"{loc['path']}:{loc['start']}" + (f"-{loc['end']}" if loc['end'] > loc['start'] else '')
                       for loc in f.get('locations') or [] if loc.get('path')) or 'no code anchor'
        hit = {'lines': 'flagged lines changed in the new commits', 'deleted': 'file deleted in the new commits',
               'file': 'same file changed in the new commits'}.get((affected.get(f['id']) or {}).get('kind'))
        rows.append(f"- {f['id']} [{SEVERITY_LABEL.get(f['severity'], '')}] {f['title']}\n  at {at}"
                    + (f' ({hit})' if hit else '')
                    + (f"\n  Fixed at {done[f['id']]['at'][:12]} in an earlier re-review"
                       + (f": {done[f['id']]['why']}" if done[f['id']].get('why') else '') if f['id'] in done else ''))
        body = (f.get('body') or '').strip()
        if body:
            body = body[:MAX_BODY_CHARS] + ('…' if len(body) > MAX_BODY_CHARS else '')
            rows.append('  ' + body.replace('\n', '\n  '))
    next_ids = ', '.join(f'{next_id(letter, taken)} ({SEVERITY_LABEL[sev]})' for sev, letter in LETTER.items())
    return PROMPT.format(moved=moved, where=where, ref12=ref[:12], head12=head[:12], next_ids=next_ids, ref=ref,
                         head=head, commits=listed, findings='\n'.join(rows) or '(none)', summary=spec.get('summary') or '')


def run(spec: dict, build_page: Callable[[dict], tuple[Path, dict] | None],
        write_page: Callable[[Path, dict], None]) -> None:
    """Worker (``finding_viewer.py job``): review the new commits in a fork of the review session, then rebuild the page
    at the new head with the triage carried over. ``build_page`` and ``write_page`` are the renderer's."""
    page, job = spec['page'], spec['job']
    st = _load(page) or {}
    if st.get('job') != job:
        return
    data = store.page_data(page) or {}
    ref, head = st['from'], st['to']
    if store.reviewed_ref(data) != ref:
        with jobs.page_lock(page):
            cur = _load(page) or {}
            if cur.get('job') == job and cur.get('status') == 'reviewing':
                _close(cur, status='failed', error='The page moved to another commit since this re-review started. '
                                                   'Start again. The page is unchanged.')
                _write(page, cur)
        return
    _update(page, job, steps=['Checking out the new head'])
    checkout, is_snapshot = store.review_checkout({**data, 'ref': head})
    taken = taken_ids(page, data)
    cmd = [claude_cli.binary(), '-p', *claude_cli.common_args(), '--resume', st['review_session'], '--fork-session',
           '--output-format', 'stream-json', '--verbose', '--allowedTools', *chat.READ_ONLY_TOOLS,
           '--add-dir', str(paths.OUT), *(['--add-dir', checkout] if is_snapshot else []),
           '--json-schema', json.dumps(SCHEMA)]
    model = config.get('rereview_model')
    if model:
        cmd += ['--model', model]
    result, error, fork = draft.run_fork(cmd, st['cwd'], build_prompt(data, st, spec, checkout if is_snapshot else None, taken),
                                         TIMEOUT_SECONDS, lambda **fields: _update(page, job, **fields),
                                         'no findings returned')
    answer, built = None, None
    if not error:
        try:
            answer = parse_answer(result['structured_output'], data, taken)
        except ValueError as exc:
            error = f'The answer did not parse: {exc}.'
    if not error:
        _update(page, job, steps=((_load(page) or {}).get('steps') or [])[-11:] + ['Updating the page'])
        try:
            patch = freshness.u0_diff(data.get('repo_root') or '', ref, head)
            if patch is None:
                raise ValueError(f'git diff {ref[:8]} {head[:8]} failed')
            changed = freshness.old_side_hunks(patch)
            findings, counts = new_findings(data, answer, changed, head)
            # Drafts cannot change while the page is locked, so whether they go is known now.
            entry = {'from': ref, 'to': head, 'at': jobstate.now(), 'commits': st.get('commits'),
                     'rewritten': bool(st.get('rewritten')), 'counts': counts,
                     'discarded_drafts': bool(st.get('discard_drafts')) and unposted(page)}
            # The same page, name and session; the code, files, diff markers and links are read again at the new head.
            built = build_page({
                'title': data['title'], 'subtitle': data.get('subtitle', ''), 'cwd': data.get('repo_root') or str(Path.home()),
                'repo': (data.get('github') or '').removeprefix('https://github.com/') or None, 'pr': data.get('pr'),
                'reviewed_sha': head, 'session_id': data.get('session_id'), 'review_session': data.get('review_session'),
                'findings': findings, 'out': str(store.html_path(page)), 'rereviewed': [*(data.get('rereviewed') or []), entry]})
            if not built or store.reviewed_ref(built[1]) != head:
                raise ValueError(f'the page could not be built at {head[:8]}')
        except Exception as exc:  # noqa: BLE001 — any failure must leave the page as it was and say why
            error = f'The page was not updated: {exc}.'
    if error:
        log(f'{page}: {error}')
        with jobs.page_lock(page):
            cur = _load(page) or {}
            if cur.get('job') == job and cur.get('status') == 'reviewing':
                cur['fork'] = fork or cur.get('fork')
                _close(cur, status='failed', error=f'{error} The page is unchanged.')
                _write(page, cur)
            else:  # stopped meanwhile: the stop said so already
                sessions.delete_session(fork, keep=cur.get('review_session'))
        return
    _out, new = built
    message = f'Reviewed {head[:8]}: {said(counts)}.'
    # Under the lock every write to the page takes: stage every new file, record the steps that put them in place
    # (``committing``), take the steps, mark the job done. A worker that dies before the record leaves the page as it
    # was; one that dies after it is finished by the next read (``_fail_stalled``), as the staged files are complete.
    with jobs.page_lock(page):
        cur = _load(page) or {}
        if cur.get('job') != job or cur.get('status') != 'reviewing' or cur.get('stopping'):
            sessions.delete_session(fork, keep=cur.get('review_session'))
            return
        cur['fork'] = fork or cur.get('fork')
        try:
            steps = _stage(page, job, cur, data, new, changed, write_page)
        except Exception as exc:  # noqa: BLE001 — a failed staging must leave the page as it was and say why
            _drop_staged(page, job)
            error = f'The page was not updated: {exc!r}.'
            log(f'{page}: {error}')
            _close(cur, status='failed', error=f'{error} The page is unchanged.')
            _write(page, cur)
            return
        cur.update(committing=steps, result={'to': head, 'counts': counts, 'message': message})
        _write(page, cur)
        _commit(page, cur)
        _write(page, cur)
    usage = result.get('usage') or {}
    read = sum(usage.get(k) or 0 for k in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'))
    # A fork's total_cost_usd can include the review session's own spend (chat.py says so for a forked chat), which
    # one result cannot tell apart; the tokens this run read are its own.
    log(f"{page}: {message} (${result.get('total_cost_usd') or 0:.2f}, the fork's total; {read // 1000}k tokens read)")
