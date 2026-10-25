"""Draft the PR comments in a fork of the review session, post the ones the reviewer approves, tell the review session.

1. Draft: ``claude -p --resume <review session> --fork-session`` inherits the whole review conversation, gets the
   triage, and returns the review as JSON (``--json-schema``). Read-only tools only, so it cannot post. The drafts land
   in ``out/<page>/drafts.json`` and the page shows them for editing.
2. Post: exactly the approved text is posted as one review with ``gh api`` (never by the fork, which could reword
   it), each comment led by the level chosen on the page ("**Must-fix:** …"). Comments outside the diff go in the
   review body, since GitHub accepts inline comments only inside hunks. A comment the reviewer wrote on the page
   (a note in "my comment" mode) replaces whatever the fork drafted for that finding.
3. Tell: the fork is resumed once with only SendMessage and tells the review session what was posted, so it does not
   post again. Then the fork's transcript is deleted.

Drafting, and posting + telling, each run in a detached worker process (``jobs.spawn``) that writes
``drafts.json`` itself, so a server restart in the middle loses nothing. The server starts the worker, reads the
state, and stops a draft on request.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

import chat
import claude_cli
import config
import jobs
import jobstate
import paths
import send
import sessions
import store

DRAFT_TIMEOUT_SECONDS = 600
POST_TIMEOUT_SECONDS = 60
EVENTS = ('COMMENT', 'REQUEST_CHANGES', 'APPROVE')
# The level a posted comment leads with, and the one each finding severity starts at ('' posts no level).
LEVELS = {'must-fix': 'Must-fix', 'should-fix': 'Should-fix', 'suggestion': 'Suggestion', 'nit': 'Nit', 'question': 'Question'}
SEVERITY_LEVEL = {'critical': 'must-fix', 'warning': 'should-fix', 'suggestion': 'suggestion', 'question': 'question'}
# A level the drafter wrote into the body anyway ("**Must-fix:** …", "[Nit] …"); the page adds the chosen one in front.
LEVEL_PREFIX_RE = re.compile(r'^\s*(?:\*\*|__)?\[?(?:must[\s-]?fix|should[\s-]?fix|nice[\s-]to[\s-]have|suggestion|nit|question)'
                             r'\]?(?:\*\*|__)?\s*:?\s*(?:\*\*|__)?\s*', re.I)
# A comment that opens with a block (code fence, list, heading, quote, table) gets its level on a line of its own.
BLOCK_START_RE = re.compile(r'(?:```|~~~|[-*+] |\d+[.)] |#|>|\|)')
JOB_LABEL = {'draft': 'Draft comments', 'post': 'Posting to GitHub'}

SCHEMA = {
    'type': 'object',
    'properties': {
        'body': {'type': 'string', 'description': "The review's top-level text (markdown): short; holds what has no "
                                                  'anchor or sits outside the diff. May be empty.'},
        'event': {'type': 'string', 'enum': list(EVENTS)},
        'comments': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'finding': {'type': 'string', 'description': 'Finding id from the triage, e.g. C1'},
            'path': {'type': 'string', 'description': 'Repo-relative path, as in the triage anchor'},
            'line': {'type': 'integer', 'description': 'New-side line the comment goes on (last line of a range)'},
            'start_line': {'type': 'integer', 'description': 'First line of a multi-line range; omit for one line'},
            'level': {'type': 'string', 'enum': [*LEVELS, ''],
                      'description': "The comment's level; the page puts it in front of the body"},
            'body': {'type': 'string', 'description': 'The comment, ready to post as is, without its level'},
        }, 'required': ['finding', 'path', 'line', 'level', 'body']}},
    },
    'required': ['body', 'event', 'comments'],
}

PROMPT = """The reviewer triaged the findings of this review on the finding-viewer page. Draft the GitHub review for
the findings marked to post, following the reviewer's notes (a note can change the wording, the scope or the ask).
Check anything you are unsure of with the read-only tools, but do not post or change anything: the reviewer approves
the drafts on the page, and the page posts them. Findings under "To fix locally" are for the author to change
in the code: no comment for them, and keep them out of the body.

Return JSON:
- comments: one per finding to post, on its anchor below (path and line on the new side of the diff). Use
  start_line..line for a comment about a block.
- level of each comment: must-fix for a Critical finding, should-fix for a Warning, suggestion for a Suggestion,
  question for a Question, empty for a plain Finding. Change it only when the reviewer's note asks for another level
  (nit, say). The page puts the level in front of the comment, so do not write it in the body.
- A finding with a "Reviewer's own comment" is already written: return it as a comment with exactly that text, even
  when it has no anchor (then path "" and line 0), and keep it out of the review body.
- body: the review's top-level text. Keep it short. It also holds findings with no anchor or outside the diff.
- event: COMMENT unless the reviewer's notes ask for something else.
Write each comment ready to post as is: no finding ids, no "Draft:" labels.

"""


def state_path(page: str) -> Path:
    return store.page_dir(page) / 'drafts.json'


def _load(page: str) -> dict | None:
    return jobstate.load(state_path(page))


def _write(page: str, st: dict) -> None:
    """Write the drafts state; hold ``jobs.locked(state_path(page))`` from the load before it."""
    jobs.write_json(state_path(page), st)


def _stalled(st: dict) -> bool:
    """Whether a state that says drafting, posting or telling has no worker behind it any more (``jobs.lost``). A live
    worker is never given up on: it has its own watchdog, and the clock may have jumped while the laptop slept."""
    status = st.get('status')
    if status not in ('drafting', 'posting') and not (status == 'posted' and not st.get('told')):
        return False
    return jobs.lost(st)


def _fail_stalled(st: dict) -> None:
    """Record a lost worker as failed, in the terms of what it was doing: drafting, posting or telling. Its job id goes
    too, so nothing writes over the verdict."""
    pid = st.get('pid')
    gone = f' (pid {pid})' if pid else ''
    jobstate.end(st)
    if st['status'] == 'drafting' and st.get('stopping'):
        _mark_stopped(st)  # a Stop killed it, and this read came first
    elif st['status'] == 'drafting':
        st.update(status='failed', phase='draft', ended=time.time(),
                  error=f'The drafting process stopped{gone}.' if gone else 'The process stopped without an answer.')
    elif st['status'] == 'posting':
        st.update(status='failed', phase='post', ended=time.time(),
                  error=f'The posting process stopped{gone or " without an answer"}. Check the PR on GitHub before '
                        'posting again: the review may already be there.')
    else:
        sessions.delete_session(st.get('fork'), keep=st.get('review_session'))
        st.update(fork=None, told={'ok': False, 'to': st.get('telling'), 'ended': time.time(),
                                   'error': f'The process telling the review session stopped{gone}, so it may not '
                                            'have been told.'})


def state(page: str) -> dict | None:
    """The page's drafts and where they are: drafting, ready, posting, posted or failed.

    A worker that is gone reads as failed at once, and is recorded so."""
    return jobstate.settle(state_path(page), _stalled, _fail_stalled)


def _update(page: str, job: str, **fields: object) -> dict | None:
    """Change the drafts state while it is still this job's: a stopped or replaced job's worker stays quiet."""
    return jobstate.update_if(state_path(page), job, fields)


def default_level(severity: str | None) -> str:
    return SEVERITY_LEVEL.get(severity or '', '')


def without_level(body: str) -> str:
    """The body without a level label the drafter put in front anyway; a plain opening word ("Nit picking…") stays."""
    m = LEVEL_PREFIX_RE.match(body)
    return body[m.end():] if m and re.search(r'[:*_\[]', m.group(0)) and body[m.end():].strip() else body


def with_level(level: str, text: str) -> str:
    """The comment as posted: its level in bold, then the text."""
    if level not in LEVELS:
        return text
    return f'**{LEVELS[level]}:**' + ('\n\n' if BLOCK_START_RE.match(text) else ' ') + text


def can_draft(data: dict) -> bool:
    return bool(data.get('pr') and data.get('github') and sessions.review_session(data))


def in_diff(data: dict, path: str, line: int, start: int | None = None) -> bool:
    """GitHub takes an inline comment only inside one hunk of the diff (new side, context included)."""
    hunks = ((data.get('files') or {}).get(path) or {}).get('hunks') or []
    return any(a <= (start or line) and line <= b for a, b in hunks)


def _busy(st: dict | None) -> str | None:
    """What still runs on the drafts and needs the fork: drafting, posting, or telling the review session."""
    status = (st or {}).get('status')
    if status == 'posted' and not st.get('told'):
        return f"Still telling {st.get('telling') or 'the review session'} what was posted."
    return {'drafting': 'Already drafting.', 'posting': 'Still posting the review to GitHub.'}.get(status)


def start(page: str, data: dict, prompt: str, version: str, own: dict[str, dict]) -> tuple[int, dict]:
    """Start drafting. ``own`` maps a finding id to the comment the reviewer wrote for it and its anchor."""
    review = sessions.review_session(data)
    if not data.get('pr') or not data.get('github'):
        return 409, {'error': 'Only PR reviews can be posted from the page.'}
    if not review:
        return 409, {'error': "The review session's transcript is gone, so there is nothing to fork."}
    claude = claude_cli.binary()
    if not Path(claude).exists():
        return 500, {'error': 'claude CLI not found on PATH'}
    state(page)  # a worker that is gone counts as ended
    job = jobs.new_id()
    with jobs.locked(state_path(page)):
        old = _load(page)
        if busy := _busy(old):
            return 409, {'error': busy}
        sessions.delete_session((old or {}).get('fork'), keep=review[0])
        st = {'status': 'drafting', 'started': time.time(), 'version': version, 'review_session': review[0],
              'cwd': review[1], 'fork': None, 'steps': [], 'job': job, 'pid': None}
        _write(page, st)
    try:
        pid = jobs.spawn({'kind': 'draft', 'job': job, 'page': page, 'prompt': prompt, 'own': own})
    except OSError as exc:
        error = f'The drafting process did not start: {exc!r}'
        return 500, _update(page, job, status='failed', phase='draft', error=error, ended=time.time()) or {'error': error}
    # Recorded at once: a worker that dies from here on reads as failed on the next poll, not after the timeout.
    return 202, _update(page, job, pid=pid) or st


def run_draft(spec: dict) -> None:
    """Worker (``finding_viewer.py job``): draft in a fork of the review session and record the drafts."""
    page, job, own = spec['page'], spec['job'], spec.get('own') or {}
    st = _load(page) or {}
    if st.get('job') != job:
        return
    data = store.page_data(page) or {}
    cmd = [claude_cli.binary(), '-p', *claude_cli.common_args(), '--resume', st['review_session'], '--fork-session',
           '--output-format', 'stream-json', '--verbose', '--allowedTools', *chat.READ_ONLY_TOOLS,
           '--add-dir', str(paths.OUT), '--json-schema', json.dumps(SCHEMA)]
    model = config.get('draft_model')
    if model:
        cmd += ['--model', model]
    result, error, fork = run_fork(cmd, st['cwd'], spec['prompt'], DRAFT_TIMEOUT_SECONDS,
                                   lambda **fields: _update(page, job, **fields), 'no drafts returned')
    if error:
        chat.log(f'draft {page}: {error}')
        _update(page, job, status='failed', error=error, phase='draft', fork=fork, ended=time.time())
        return
    out = result['structured_output']
    findings = {f['id']: f for f in data.get('findings', [])}
    comments = []

    def add(fid: str, path: str, line: int, start: int | None, level: str, body: str) -> None:
        comments.append({'finding': fid, 'title': (findings.get(fid) or {}).get('title', ''), 'path': path, 'line': line,
                         'start_line': start, 'level': level, 'body': body, 'own': fid in own,
                         'in_diff': bool(path) and in_diff(data, path, line, start)})

    for c in out.get('comments') or []:
        fid, path, line = str(c.get('finding') or ''), str(c.get('path') or ''), int(c.get('line') or 0)
        start = int(c['start_line']) if c.get('start_line') and int(c['start_line']) < line else None
        severity = (findings.get(fid) or {}).get('severity')
        level = c['level'] if c.get('level') in LEVELS or c.get('level') == '' else default_level(severity)
        # The reviewer's own comment goes out word for word, whatever the drafter returned for it.
        add(fid, path, line, start, level, own[fid]['body'] if fid in own else without_level(str(c.get('body') or '')))
    drafted = {c['finding'] for c in comments}
    for fid, o in own.items():
        if fid not in drafted:
            a = o.get('anchor') or {}
            add(fid, str(a.get('path') or ''), int(a.get('line') or 0), None,
                default_level((findings.get(fid) or {}).get('severity')), o['body'])
    event = out.get('event') if out.get('event') in EVENTS else 'COMMENT'
    _update(page, job, status='ready', error=None, drafted_at=jobstate.now(), ended=time.time(), fork=fork,
            review={'body': str(out.get('body') or ''), 'event': event, 'comments': comments})


def run_fork(cmd: list[str], cwd: str, prompt: str, timeout: int, record: Callable[..., object],
             empty: str) -> tuple[dict | None, str | None, str | None]:
    """Run a fork of the review session (``claude -p --resume … --fork-session``, stream-json, ``--json-schema``) to its
    result: (the result event, an error, the fork's id). ``record(fork=…)`` gets the fork's id from its first event and
    ``record(steps=…)`` each tool call, so the state can show them; ``empty`` is the error for a result without JSON."""
    result, error, fork, steps = None, None, None, []
    try:
        stream = claude_cli.Stream(cmd, cwd, prompt, timeout)
        for ev in stream:
            if ev.get('type') == 'system' and ev.get('subtype') == 'init' and ev.get('session_id'):
                # Recorded at once, so a discard or a stop can delete the fork even if this run dies.
                fork = ev['session_id']
                record(fork=fork)
            elif ev.get('type') == 'assistant':
                for block in (ev.get('message') or {}).get('content') or []:
                    if isinstance(block, dict) and block.get('type') == 'tool_use':
                        steps = (steps + [claude_cli.describe_tool(block)])[-12:]
                        record(steps=steps)
            elif ev.get('type') == 'result':
                result = ev
                fork = ev.get('session_id') or fork
        if result is None:
            error = stream.failure
        elif result.get('is_error') or not isinstance(result.get('structured_output'), dict):
            error = str(result.get('result') or result.get('subtype') or empty)[:400]
    except Exception as exc:  # noqa: BLE001 — any failure must end as a visible state, never a stuck job
        error = repr(exc)
    return result, error, fork


def _mark_stopped(st: dict) -> None:
    """Drafting stopped by the reviewer: the fork goes, and the dialog says so."""
    sessions.delete_session(st.get('fork'), keep=st.get('review_session'))
    st.pop('job', None)
    st.pop('stopping', None)
    st.update(status='failed', phase='draft', error='Stopped by you.', stopped=True, fork=None, ended=time.time())


def stop(page: str) -> tuple[int, dict]:
    """Stop drafting: end the worker with its claude process, delete the fork, and say so on the page."""
    st = state(page) or {}
    if st.get('status') == 'posting' or (st.get('status') == 'posted' and not st.get('told')):
        return 409, {'error': "Posting can't be stopped: GitHub may already have the review."}
    if st.get('status') != 'drafting':
        return 409, {'error': 'Nothing to stop: drafting already ended.'}
    return jobstate.stop(st, 'The drafting process is still starting. Try again in a moment.',
                         lambda: _update(page, st['job'], stopping=True),
                         lambda: jobstate.record_stop(state_path(page), st['job'], 'drafting', _mark_stopped))


def activity(st: dict | None) -> list[dict]:
    """Drafting, posting and telling for the Running panel: what runs now, and what ended and when."""
    if not st:
        return []
    status, ended = st.get('status'), st.get('ended')
    if status == 'drafting':
        return [jobstate.running_row(st, id='draft', kind='draft', label=JOB_LABEL['draft'], session=st.get('fork'),
                                     **jobstate.stop_fields(st, (st.get('steps') or ['Starting'])[-1]))]
    if status == 'posting':
        return [jobstate.running_row(st, id='post', kind='post', label=JOB_LABEL['post'], stop=False)]
    if not ended:
        return []
    if status in ('ready', 'failed'):
        kind = 'post' if st.get('phase') == 'post' else 'draft'
        state_ = 'done' if status == 'ready' else 'stopped' if st.get('stopped') else 'failed'
        return [jobstate.ended_row(state_, ended, st.get('error'), id=kind, kind=kind, label=JOB_LABEL[kind])]
    out = [{'id': 'post', 'kind': 'post', 'label': JOB_LABEL['post'], 'state': 'done', 'ended': ended}]
    told = st.get('told')
    if not told:
        out.append(jobstate.running_row(st, id='tell', kind='tell',
                                        label=f"Telling {st.get('telling') or 'the review session'}", started=ended,
                                        session=st.get('fork'), stop=False))
    elif told.get('ended'):
        out.append(jobstate.ended_row('done' if told.get('ok') else 'failed', told['ended'], told.get('error'),
                                      id='tell', kind='tell', label=f"Telling {told.get('to') or 'the review session'}"))
    return out


def discard(page: str) -> tuple[int, dict]:
    state(page)  # a worker that is gone counts as ended
    with jobs.locked(state_path(page)):
        st = _load(page)
        if busy := _busy(st):
            return 409, {'error': f'{busy} Discard once that ends.'}
        sessions.delete_session((st or {}).get('fork'), keep=(st or {}).get('review_session'))
        state_path(page).unlink(missing_ok=True)
    return 200, {'ok': True}


def post(page: str, data: dict, payload: dict) -> tuple[int, dict]:
    """Post the approved drafts as one review; payload is the page's edited review."""
    st = state(page)
    if not st or st.get('status') not in ('ready', 'failed') or not st.get('review'):
        return 409, {'error': 'There are no drafts to post.'}
    event = payload.get('event')
    if event not in EVENTS:
        return 400, {'error': f'event must be one of {EVENTS}'}
    body, inline, outside = str(payload.get('body') or '').strip(), [], []
    for c in payload.get('comments') or []:
        text = str(c.get('body') or '').strip()
        if not c.get('include', True) or not text:
            continue
        text = with_level(str(c.get('level') or ''), text)
        path, line = str(c.get('path') or ''), int(c.get('line') or 0)
        start = int(c['start_line']) if c.get('start_line') and int(c['start_line']) < line else None
        if path and line and in_diff(data, path, line, start):
            item = {'path': path, 'line': line, 'side': 'RIGHT', 'body': text}
            if start:
                item.update(start_line=start, start_side='RIGHT')
            inline.append(item)
        else:
            outside.append(f'**`{path}:{line}`**\n\n{text}' if path else text)
    if outside:
        body = '\n\n---\n\n'.join(([body] if body else []) + outside)
    if not inline and not body:
        return 400, {'error': 'Nothing to post: every comment is empty or left out.'}
    review = {'commit_id': store.reviewed_ref(data), 'body': body, 'event': event, 'comments': inline}
    job = jobs.new_id()
    with jobs.locked(state_path(page)):
        # Checked again under the lock: a second click must not post the review twice.
        st = _load(page)
        if not st or st.get('status') not in ('ready', 'failed') or not st.get('review'):
            return 409, {'error': 'There are no drafts to post.'}
        st.update(status='posting', started=time.time(), ended=None, error=None, phase='post', job=job, pid=None,
                  review=_kept(st['review'], payload))
        _write(page, st)
    try:
        pid = jobs.spawn({'kind': 'post', 'job': job, 'page': page, 'review': review})
    except OSError as exc:
        error = f'The posting process did not start: {exc!r}'
        return 500, _update(page, job, status='failed', error=error, ended=time.time()) or {'error': error}
    return 202, _update(page, job, pid=pid) or st


def _kept(drafted: dict, payload: dict) -> dict:
    """The drafts as edited on the page, so a failed post keeps the reviewer's edits."""
    comments = []
    for old, new in zip(drafted.get('comments') or [], payload.get('comments') or []):
        level = new.get('level') if new.get('level') in LEVELS else ''
        comments.append({**old, 'body': str(new.get('body') or ''), 'include': bool(new.get('include', True)), 'level': level})
    return {'body': str(payload.get('body') or ''), 'event': payload.get('event'), 'comments': comments}


def run_post(spec: dict, on_posted: Callable[[str], None]) -> None:
    """Worker (``finding_viewer.py job``): post the approved review, then have the fork tell the review session."""
    page, job, review = spec['page'], spec['job'], spec['review']
    data = store.page_data(page) or {}
    slug = data['github'].removeprefix('https://github.com/')
    try:
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as fh:
            json.dump(review, fh)
        r = subprocess.run(['gh', 'api', f"repos/{slug}/pulls/{data['pr']}/reviews", '--method', 'POST', '--input',
                            fh.name], cwd=data.get('repo_root') or None, capture_output=True, text=True,
                           timeout=POST_TIMEOUT_SECONDS)
        os.unlink(fh.name)
        answer = json.loads(r.stdout) if r.stdout.strip().startswith('{') else {}
        error = None if r.returncode == 0 else (answer.get('message') or r.stderr.strip() or 'gh api failed')[:600]
        if error and answer.get('errors'):
            error += ': ' + '; '.join(str(e) for e in answer['errors'])[:400]
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        answer, error = {}, repr(exc)
    if error:
        chat.log(f'draft {page}: posting failed: {error}')
        _update(page, job, status='failed', error=f'GitHub refused the review: {error}', ended=time.time())
        return
    st = _update(page, job, status='posted', ended=time.time(),
                 posted={'url': answer.get('html_url'), 'id': answer.get('id'), 'at': jobstate.now(),
                         'inline': len(review['comments']), 'event': review['event']})
    if not st:
        return
    on_posted(page)
    _tell(page, job, data, review, st)


def _tell(page: str, job: str, data: dict, review: dict, st: dict) -> None:
    """The fork tells the review session what was posted, then the fork is deleted."""
    live, open_fork = sessions.live_session(st['review_session']), sessions.live_session(st.get('fork') or '')
    if open_fork:
        # Resuming it here as well would write to a transcript someone has open; it stays, and so does its transcript.
        told = {'ok': False, 'error': f"The fork is open in {open_fork.get('name') or 'Claude Code'}, so it did not tell "
                                      'the review session, and it was not deleted.'}
    elif not live or not live.get('name'):
        told = {'ok': False, 'error': 'The review session is closed, so it was not told.'}
    elif not st.get('fork'):
        told = {'ok': False, 'error': 'The fork is gone, so the review session was not told.'}
    else:
        _update(page, job, telling=live['name'])
        posted = [c.get('finding') for c in st['review']['comments'] if c.get('include', True) and c.get('body', '').strip()]
        message = (f"Finding-viewer posted the review of PR #{data['pr']} from the review page: "
                   f"{review['event']} review, {len(review['comments'])} inline comments, on {review['commit_id'][:10]}. "
                   f"{st['posted']['url']}\n"
                   'The reviewer approved this exact text on the page, so it is done: do not post these comments again.'
                   + (f"\nFindings posted: {', '.join(posted)}." if posted else ''))
        result = send.deliver(live['name'], message, ['--resume', st['fork']], st['cwd'])
        told = {'ok': result['ok'], 'to': live['name'], 'error': result['error']}
    sessions.delete_session(st.get('fork'), keep=st['review_session'])
    _update(page, job, told={**told, 'ended': time.time()}, fork=st.get('fork') if open_fork else None)
