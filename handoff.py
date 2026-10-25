"""Hand the triage back to Claude. The UserPromptSubmit hook (``cmd_inject``) adds the triage and side chats that
changed on a page to the review session's next prompt; ``cmd_decisions`` prints the latest triage; ``summarize`` writes
the text for both, and the triage part of the prompts of Draft comments and Review the new commits. ``send_fixes`` hands
the triage to the open review session at once (Send fixes to session on the page).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import draft
import freshness
import jobs
import jobstate
import paths
import rereview
import send
import sessions
import store
from severity import LABEL

INJECT_WINDOW_SECONDS = 48 * 3600


def summarize(page: str, dec: dict, chat_state: dict, for_claude: bool) -> str:
    data = store.page_data(page) or {}
    # A review with no PR is the reviewer's own code before a PR exists: nothing is posted, so a finding is to fix, won't
    # fix (stored as skip) or undecided; one stored as post, from before that, counts as undecided.
    local = not (data.get('pr') or dec.get('pr'))
    # Findings the new commits fixed (rereview.py) stay on the page, but there is nothing to post or decide for them.
    fixed = rereview.fixed(data)
    items = [(k, v) for k, v in (dec.get('findings') or {}).items() if k not in fixed]
    threads = {k: t for k, t in (chat_state.get('threads') or {}).items() if t.get('messages')}
    for fid in threads:
        if fid not in dict(items) and fid not in fixed:
            items.append((fid, {'title': '', 'status': None}))
    post = [] if local else [(k, v) for k, v in items if v.get('status') == 'post']
    fix = [(k, v) for k, v in items if v.get('status') == 'fix']
    skip = [(k, v) for k, v in items if v.get('status') == 'skip']

    def undecided_status(v: dict) -> bool:
        return not v.get('status') or (local and v.get('status') == 'post')

    undecided = [k for k, v in items if undecided_status(v)]
    pr = f" (PR #{dec['pr']})" if dec.get('pr') else ''
    title = dec.get('title') or chat_state.get('title') or page
    tally = (f"{len(fix)} to fix, {len(skip)} won't fix, {len(undecided)} undecided" if local else
             f"{len(post)} to post, {f'{len(fix)} to fix locally, ' if fix else ''}{len(skip)} not to post, "
             f"{len(undecided)} undecided")
    lines = [f"Finding-viewer triage for \"{title}\"{pr}: {tally}{f', {len(fixed)} fixed' if fixed else ''}. "
             f"Saved {dec.get('saved_at') or chat_state.get('updated', '?')}."]
    if again := rereview.heads_up(data):
        lines.append(again)
    if warning := freshness.heads_up(page):
        lines.append(warning)
    if settled := freshness.pr_note(page):
        lines.append(settled)

    def note_lines(v: dict, fid: str) -> list[str]:
        note = (v.get('note') or '').strip()
        label = ("Reviewer's own comment, to post word for word"
                 if v.get('mode') == 'comment' and v.get('status') != 'fix' and not local else "Reviewer's note")
        out = [f'  {label}: ' + note.replace('\n', '\n    ')] if note else []
        # Not the page's own lines (a re-review noting that the code moved on).
        msgs = [m for m in threads.get(fid, {}).get('messages') or [] if m.get('role') != 'system']
        last = next((m['text'] for m in reversed(msgs) if m.get('role') == 'claude' and not m.get('error')), None)
        if msgs:
            gist = re.sub(r'\s+', ' ', last or '(no reply yet)')
            out.append(f'  Page chat with Claude ({len(msgs)} messages). Last reply: {gist[:400]}{"…" if len(gist) > 400 else ""}')
        return out

    def anchor(v: dict) -> str:
        a = v.get('anchor') or {}
        if not a.get('path'):
            return 'no code anchor, so it belongs in the review body'
        where = f"{a['path']}:{a['line']}"
        return where if a.get('in_diff') else f'{where}, outside the diff, so GitHub cannot take it inline: put it in the review body'

    # Posted from the page already (draft.py): said per finding, so the hand-off after a post does not post it twice.
    st = jobstate.load(draft.state_path(page)) or {}
    posted = {c.get('finding') for c in (st.get('review') or {}).get('comments') or []
              if c.get('include', True) and (c.get('body') or '').strip()} if st.get('status') == 'posted' else set()
    if post:
        lines.append('To post:')
        for k, v in post:
            sev = LABEL.get(v.get('severity'), '')
            lines.append(f"- {k} [{sev}] {v.get('title', '')}. Comment on {anchor(v)}.")
            if k in posted:
                lines.append(f"  Already posted from the page ({(st.get('posted') or {}).get('url') or 'GitHub'}): "
                             'do not post it again.')
            if v.get('recheck'):
                lines.append(f"  It changed in the re-review at {v['recheck'][:10]}, after the reviewer chose Post: the "
                             'reviewer has not looked at it again yet.')
            lines += note_lines(v, k)
    if fix:
        lines.append("To fix locally (the reviewer's own code: change the code before anyone else reviews it; never post these):")
        for k, v in fix:
            sev = LABEL.get(v.get('severity'), '')
            a = v.get('anchor') or {}
            where = f"At {a['path']}:{a['line']}." if a.get('path') else 'No code anchor.'
            lines.append(f"- {k} [{sev}] {v.get('title', '')}. {where}")
            lines += note_lines(v, k)
    if skip:
        lines.append("Won't fix:" if local else 'Not posting:')
        for k, v in skip:
            lines.append(f"- {k} {v.get('title', '')}")
            lines += note_lines(v, k)
    if undecided:
        lines.append('Undecided: ' + ', '.join(undecided) + '.')
        for k, v in items:
            if undecided_status(v):
                lines += [f'  {k}:'] + note_lines(v, k) if (v.get('note') or '').strip() or k in threads else []
    if fixed:
        lines.append(('Fixed by the new commits, so left out: ' if local else 'Fixed by the new commits, so not to post: ')
                     + '; '.join(f"{k} ({rr['why']})" if rr.get('why') else k for k, rr in fixed.items()) + '.')
    lines.append(f'Full finding text and notes: {store.decisions_path(page)}')
    if threads:
        lines.append(f'Full page chats: {store.chat_path(page)}')
    if for_claude:
        if fix:
            change = ("Change only those: leave the findings marked won't fix as they are." if local else
                      'Change only those: the findings to post stay as they are. Do not post them or mention them on the PR.')
            lines.append('Fix the findings under "To fix locally" in the code, following the reviewer\'s notes, and show the '
                         f'reviewer what you changed. {change}')
        if not local and (post or not fix):
            lines.append("When drafting comments, follow the reviewer's notes and post the reviewer's own comments word for "
                         'word. Show the reviewer the final comments and get a yes before posting anything.')
    return '\n'.join(lines)


def _has_input(dec: dict) -> bool:
    return any(v.get('status') or (v.get('note') or '').strip() for v in (dec.get('findings') or {}).values())


def _load_state() -> dict:
    try:
        state = json.loads(paths.INJECT_STATE.read_text())
    except (OSError, ValueError):
        state = {}
    return state if isinstance(state, dict) else {}


def _save_state(state: dict) -> None:
    if len(state) > 50:
        state = dict(list(state.items())[-50:])
    paths.INJECT_STATE.write_text(json.dumps(state))


def recent_pages(window: float | None = None) -> list[str]:
    """Pages with triage or chat, newest activity first."""
    latest: dict[str, float] = {}
    for page in store.pages():
        for name in ('decisions.json', 'chat.json'):
            try:
                mtime = (store.page_dir(page) / name).stat().st_mtime
            except OSError:
                continue
            latest[page] = max(latest.get(page, 0), mtime)
    cutoff = time.time() - window if window else 0
    return [page for page, m in sorted(latest.items(), key=lambda kv: kv[1], reverse=True) if m > cutoff]


def page_state(page: str) -> tuple[dict, dict, str | None, str]:
    dec = store.load_decisions(store.decisions_path(page)) or {}
    chat_state = store.load_chat(page)
    owner = dec.get('session_id') or chat_state.get('session_id')
    return dec, chat_state, owner, store.page_version(dec, chat_state)


def mark_seen(sid: str, page: str, version: str) -> None:
    """Record that session ``sid`` has the page's triage at ``version``, so the inject hook does not add it again."""
    state = _load_state()
    state.setdefault(sid, {})[page] = version
    _save_state(state)


def _unmark(sid: str | None, page: str, version: str | None, before: str | None = None) -> None:
    """Undo ``mark_seen`` for ``version`` when a send did not reach the session: the version seen before it goes back
    (None takes the mark away), so the next prompt carries the triage. Only while the mark still holds ``version``."""
    state = _load_state()
    seen = state.get(sid) or {}
    if not sid or seen.get(page) != version:
        return
    if before is None:
        seen.pop(page, None)
    else:
        seen[page] = before
    _save_state(state)


def mark_handed(page: str) -> None:
    """Keep the inject hook from handing this triage over again: the review session hears of the post from the fork. Not
    while findings wait to be fixed locally: the next prompt still carries those, and the summary says what was posted."""
    dec, _chat, owner, version = page_state(page)
    fixed = rereview.fixed(store.page_data(page) or {})
    if any(v.get('status') == 'fix' and k not in fixed for k, v in (dec.get('findings') or {}).items()):
        return
    if owner:
        mark_seen(owner, page, version)


SEND_HINT = 'Type any prompt in the review session and it gets the triage.'


def send_path(page: str) -> Path:
    """The page's Send fixes state: out/<page>/send.json."""
    return store.page_dir(page) / 'send.json'


def _send_stalled(st: dict) -> bool:
    return st.get('status') == 'sending' and jobs.lost(st)


def _send_lost(page: str, st: dict) -> None:
    """Record a lost sending worker as failed; its job id goes too, so nothing writes over the verdict. The triage it was
    sending was marked seen before the relay ran (``run_send``), and that mark goes too: the next prompt carries it."""
    pid = st.get('pid')
    jobstate.end(st)
    _unmark(st.get('session'), page, st.get('version'))
    st.update(status='failed', ended=time.time(),
              error=f"The process sending the fixes stopped{f' (pid {pid})' if pid else ''}, so "
                    f"{st.get('to') or 'the review session'} may not have them. {SEND_HINT}")


def send_state(page: str) -> dict | None:
    """The last Send fixes: sending, sent or failed. A worker that is gone reads as failed at once, and is recorded so."""
    return jobstate.settle(send_path(page), _send_stalled, lambda st: _send_lost(page, st))


def send_fixes(page: str, data: dict) -> tuple[int, dict]:
    """Send fixes to session: relay the page's triage to the open review session now, rather than with its next prompt.
    The relay can take up to send.TIMEOUT_SECONDS, so a detached worker runs it (``run_send``). The caller checks that
    the code is the reviewer's own."""
    dec, _chat, owner, version = page_state(page)
    fixed = rereview.fixed(data)
    if not any(v.get('status') == 'fix' and k not in fixed for k, v in (dec.get('findings') or {}).items()):
        return 400, {'error': 'Mark at least one finding Fix first.'}
    sid = owner or data.get('session_id')
    live = sessions.live_session(sid) if sid and sessions.SESSION_RE.match(sid) else None
    if not live or not live.get('name'):
        return 409, {'error': f'The review session is not open in Claude Code, so nothing was sent. {SEND_HINT}'}
    send_state(page)  # a worker that is gone counts as ended
    job = jobs.new_id()
    with jobs.locked(send_path(page)):
        old = jobstate.load(send_path(page)) or {}
        if old.get('status') == 'sending':
            return 409, {'error': f"Still sending the fixes to {old.get('to') or 'the review session'}."}
        st = {'status': 'sending', 'job': job, 'pid': None, 'started': time.time(), 'ended': None, 'error': None,
              'session': sid, 'to': live['name'], 'version': version,
              'cwd': live.get('cwd') or data.get('repo_root') or str(Path.home())}
        jobs.write_json(send_path(page), st)
    try:
        pid = jobs.spawn({'kind': 'send', 'job': job, 'page': page})
    except OSError as exc:
        error = f'The sending process did not start: {exc!r}. {SEND_HINT}'
        return 500, jobstate.update_if(send_path(page), job, {'status': 'failed', 'error': error, 'ended': time.time()}) \
            or {'error': error}
    # Recorded at once: a worker that dies from here on reads as failed on the next poll.
    return 202, jobstate.update_if(send_path(page), job, {'pid': pid}) or st


def run_send(spec: dict) -> None:
    """Worker (``finding_viewer.py job``): relay the triage as it is now to the review session and record how it went.
    The version is marked seen before the relay, so a prompt typed there meanwhile does not get the same triage from the
    inject hook too; a relay that fails puts the mark back (``_unmark``)."""
    page, job = spec['page'], spec['job']
    st = jobstate.load(send_path(page)) or {}
    if st.get('job') != job:
        return
    dec, chat_state, _owner, version = page_state(page)
    text = summarize(page, dec, chat_state, for_claude=True)
    before = (_load_state().get(st['session']) or {}).get(page)
    mark_seen(st['session'], page, version)
    result = send.deliver(st['to'], text, [], st['cwd'])
    # The relay is a session of its own that this tool started; the review session is never deleted.
    sessions.delete_session(result.get('session'), keep=st.get('session'))
    if result['ok']:
        jobstate.update_if(send_path(page), job, {'status': 'sent', 'ended': time.time(), 'version': version, 'error': None})
        return
    _unmark(st['session'], page, version, before)
    why = re.sub(r'\s+', ' ', str(result['error'] or '')).strip()[:300] or 'no answer'
    if why[-1] not in '.!?':
        why += '.'
    jobstate.update_if(send_path(page), job, {'status': 'failed', 'ended': time.time(),
                                              'error': f"Not sent to {st['to']}: {why} {SEND_HINT}"})


def send_activity(st: dict | None) -> list[dict]:
    """Send fixes for the Running panel: running now, or ended (done or failed) and when."""
    if not st:
        return []
    label = f"Sending fixes to {st.get('to') or 'the review session'}"
    if st.get('status') == 'sending':
        return [jobstate.running_row(st, id='send', kind='send', label=label, stop=False)]
    if st.get('ended'):
        state = 'done' if st.get('status') == 'sent' else 'failed'
        return [jobstate.ended_row(state, st['ended'], st.get('error'), id='send', kind='send', label=label)]
    return []


def cmd_inject() -> int:
    """UserPromptSubmit hook: add triage and page chats that changed since this session last saw them."""
    if os.environ.get('FINDING_VIEWER_CHILD'):
        return 0
    try:
        event = json.loads(sys.stdin.read() or '{}')
    except ValueError:
        return 0
    sid = event.get('session_id') or ''
    state = _load_state()
    seen = state.setdefault(sid, {})
    blocks = []
    for page in recent_pages(INJECT_WINDOW_SECONDS):
        dec, chat_state, owner, version = page_state(page)
        if (owner and owner != sid) or seen.get(page) == version:
            continue
        has_chat = any(t.get('messages') for t in (chat_state.get('threads') or {}).values())
        # A re-review is news for the review session even without triage: the page now shows a newer commit.
        if not _has_input(dec) and not has_chat and not (store.page_data(page) or {}).get('rereviewed'):
            continue
        blocks.append(summarize(page, dec, chat_state, for_claude=True))
        seen[page] = version
        if len(blocks) == 2:
            break
    if blocks:
        _save_state(state)
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit',
                                                 'additionalContext': '\n\n'.join(blocks)}}))
    return 0


def cmd_decisions() -> int:
    """Print the latest triage of this session (else the latest overall) and mark it as seen."""
    sid = os.environ.get('CLAUDE_CODE_SESSION_ID', '')
    pages = [(page, *page_state(page)) for page in recent_pages()]
    mine = [row for row in pages if row[3] == sid] or pages
    if not mine:
        print('No triage saved yet. Open a review with /findings, then mark findings Post or Don\'t post.')
        return 1
    page, dec, chat_state, _owner, version = mine[0]
    print(summarize(page, dec, chat_state, for_claude=True))
    mark_seen(sid, page, version)
    return 0
