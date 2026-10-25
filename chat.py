"""Side chat with Claude about one finding, as headless, read-only `claude -p` sessions.

Each finding gets its own thread. The first message carries the finding, its code and the reviewer's triage; later
messages resume the same Claude session. Threads live in ``out/<page>/chat.json``. Each message is answered by a
detached worker process (``jobs.spawn``), so a server restart does not lose the reply. When the review is of a commit
rather than the working tree, Claude reads a detached worktree of that commit so its tools see the reviewed code.

Settings (``settings.json``, edited from the page): the model, the context for new threads, either the finding
alone or a fork of the Claude Code session that wrote the review (``--resume <id> --fork-session``), and whether the
review pages' single-key shortcuts are on.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from pathlib import Path

import claude_cli
import config
import jobs
import jobstate
import paths
import sessions
import store
from severity import LABEL

MODEL_RE = re.compile(r'^[A-Za-z0-9._\-\[\]]{1,80}$')
CONTEXT_MODES = ('finding', 'fork')
MODEL_OPTIONS = [
    {'id': '', 'label': 'Your Claude Code default'},
    {'id': 'opus', 'label': 'Opus'},
    {'id': 'sonnet', 'label': 'Sonnet'},
    {'id': 'haiku', 'label': 'Haiku'},
    {'id': 'claude-fable-5-1', 'label': 'Fable 5.1'},
]
TIMEOUT_SECONDS = 300
MAX_MESSAGE_CHARS = 8000
EXCERPT_PAD = 8
EXCERPT_MAX_LINES = 400
READ_ONLY_TOOLS = ['Read', 'Grep', 'Glob', 'Bash(git show:*)', 'Bash(git log:*)', 'Bash(git diff:*)',
                   'Bash(git blame:*)', 'Bash(gh pr view:*)', 'Bash(gh pr diff:*)']
SYSTEM_PROMPT = """You are in a side chat on a local code-review page. The reviewer is triaging one finding from a \
code review and deciding whether to post it as a PR comment.
- Lead with the answer. Keep replies short (about 150 words) unless the reviewer asks for more.
- Check claims against the code before answering. You can read the repository (Read, Grep, Glob, git show/log/diff/\
blame, gh pr view/diff). You cannot edit files or post anything.
- {where}
- The branch's changes: `git diff {merge_base} HEAD -- <path>`.{pr_hint}
- Run one command per Bash call. Pipes, `;`, `&&` and `$(...)` are blocked, so never combine commands.
- When the reviewer asks you to draft or reword the PR comment, put only the comment text in a fenced block tagged \
`comment` (```comment ... ```), so the page can offer it as their note."""


log = paths.log('chat: ')


def _save(page: str, chat: dict) -> None:
    """Write the page's chats; hold ``jobs.locked(store.chat_path(page))`` from the load before it."""
    chat['updated'] = dt.datetime.now().isoformat(timespec='seconds')
    jobs.write_json(store.chat_path(page), chat, ensure_ascii=False, indent=1)


def load_settings() -> dict:
    settings = {'model': config.get('chat_model'), 'context': 'finding', 'shortcuts': True}
    try:
        saved = json.loads(paths.SETTINGS.read_text())
    except (OSError, ValueError):
        saved = {}
    if isinstance(saved, dict):
        model = saved.get('model')
        if isinstance(model, str) and (model == '' or MODEL_RE.match(model)):
            settings['model'] = model
        if saved.get('context') in CONTEXT_MODES:
            settings['context'] = saved['context']
        if isinstance(saved.get('shortcuts'), bool):
            settings['shortcuts'] = saved['shortcuts']
    return settings


def settings_payload() -> dict:
    return {'settings': load_settings(), 'models': MODEL_OPTIONS}


def save_settings(body: dict) -> tuple[int, dict]:
    model, context = body.get('model', ''), body.get('context', 'finding')
    # A page from before the shortcuts setting sends no `shortcuts`: keep what is saved rather than turning them back on.
    shortcuts = body.get('shortcuts', load_settings()['shortcuts'])
    if not isinstance(model, str) or (model and not MODEL_RE.match(model)):
        return 400, {'error': 'Model id may only use letters, digits, ".", "-", "_" and "[]".'}
    if context not in CONTEXT_MODES:
        return 400, {'error': f'context must be one of {CONTEXT_MODES}'}
    if not isinstance(shortcuts, bool):
        return 400, {'error': 'shortcuts must be true or false'}
    paths.SETTINGS.write_text(json.dumps({'model': model, 'context': context, 'shortcuts': shortcuts}))
    return 200, settings_payload()


def _stalled(th: dict) -> str | None:
    """Why a thread waits for a reply that will never come: no worker is behind it any more (``jobs.lost``)."""
    pending = th.get('pending')
    if not pending or not jobs.lost(pending):
        return None
    if pending.get('pid'):
        return f"No answer: the chat process stopped (pid {pending['pid']}). Send the message again."
    return 'No answer: the chat process stopped. Send the message again.'


def _end_pending(th: dict, message: dict) -> None:
    """End a pending reply without its answer; hold the chat file's lock. A first message's worker may have started a
    session already (its id comes with the init event); it holds no reply, so its transcript goes too."""
    pending, th['pending'] = th['pending'], None
    th['messages'].append(message)
    jobs.drop_spec(pending.get('job'))
    if pending.get('session') and pending['session'] != th.get('claude_session'):
        sessions.delete_session(pending['session'])


def settled(page: str) -> dict:
    """The page's chats, with threads whose worker died marked as failed."""
    chat = store.load_chat(page)
    if not any(_stalled(th) for th in (chat.get('threads') or {}).values()):
        return chat
    with jobs.locked(store.chat_path(page)):
        chat = store.load_chat(page)
        changed = False
        for th in (chat.get('threads') or {}).values():
            why = _stalled(th)
            if why:
                jobstate.end(th['pending'])
                # A Stop killed it and this read came first: it is recorded as the stop it was.
                _end_pending(th, _stopped_message() if th['pending'].get('stopping') else
                             {'role': 'claude', 'text': why, 'at': jobstate.now(), 'error': True})
                changed = True
        if changed:
            _save(page, chat)
    return chat


def snapshot(page: str) -> dict:
    """Chat state for the page, with threads whose worker died marked as failed."""
    chat = settled(page)
    rs = sessions.review_session(store.page_data(page) or {})
    chat['review_session'] = {'id': rs[0][:8], 'available': True} if rs else {'available': False}
    return chat


def start(page: str, fid: str, message: str, note: str | None = None) -> tuple[int, dict]:
    message = (message or '').strip()
    if not message or len(message) > MAX_MESSAGE_CHARS:
        return 400, {'error': f'message must be 1–{MAX_MESSAGE_CHARS} characters'}
    data = store.page_data(page)
    finding = next((f for f in (data or {}).get('findings', []) if f['id'] == fid), None)
    if not finding:
        return 404, {'error': 'unknown finding'}
    claude = claude_cli.binary()
    if not Path(claude).exists():
        return 500, {'error': 'claude CLI not found on PATH'}
    settled(page)
    settings = load_settings()
    job = jobs.new_id()
    with jobs.locked(store.chat_path(page)):
        chat = store.load_chat(page)
        chat.setdefault('page', page)
        chat.setdefault('title', data.get('title'))
        chat.setdefault('session_id', data.get('session_id'))
        th = chat.setdefault('threads', {}).setdefault(fid, {'claude_session': None, 'messages': [], 'pending': None})
        if th.get('pending'):
            return 409, {'error': 'Claude is still answering the previous message'}
        resume = th.get('claude_session')
        mode = th.get('mode') or settings['context']
        # A re-review moved the page to a newer commit since the last reply (rereview.py): Claude hears it first.
        last = (th['messages'] or [{}])[-1]
        moved = last.get('claude') if last.get('role') == 'system' else None
        th['messages'].append({'role': 'user', 'text': message, 'at': jobstate.now()})
        th['pending'] = {'started': time.time(), 'status': 'Starting', 'steps': [], 'job': job, 'pid': None,
                         'session': resume}
        _save(page, chat)
    prompt = (f'{moved}\n\n{message}' if moved else message) if resume else first_prompt(page, data, finding, message, note)
    spec = {'kind': 'chat', 'job': job, 'page': page, 'fid': fid, 'prompt': prompt, 'resume': resume,
            'model': settings['model'], 'mode': mode, 'cwd': th.get('cwd')}
    try:
        pid = jobs.spawn(spec)
    except OSError as exc:
        log(f'{page} {fid}: the chat process did not start: {exc!r}')
        _finish(page, fid, job, {'role': 'claude', 'text': f'Error: the chat process did not start: {exc!r}',
                                 'at': jobstate.now(), 'error': True})
        return 500, {'error': f'The chat process did not start: {exc!r}'}
    # Recorded at once: a worker that dies from here on reads as failed on the next poll, not after the timeout.
    th = _update_pending(page, fid, job, pid=pid) or th
    return 202, {'ok': True, 'thread': th}


def first_prompt(page: str, data: dict, finding: dict, message: str, note: str | None = None) -> str:
    dec = {}
    try:
        dec = (json.loads(store.decisions_path(page).read_text()).get('findings') or {}).get(finding['id'], {})
    except (OSError, ValueError):
        pass
    # A review with no PR posts nothing (as handoff.summarize says): a skip is won't fix, and a post stored before is
    # undecided; a note there is never a comment to post word for word.
    local = not data.get('pr')
    status = {'skip': "Won't fix" if local else "Don't post", 'fix': 'Fix it in the code (the reviewer\'s own code; not posted)'}
    if not local:
        status['post'] = 'Post it'
    status = status.get(dec.get('status'), 'undecided')
    mine = ("Reviewer's own comment for the PR, to post word for word"
            if not local and dec.get('mode') == 'comment' and dec.get('status') != 'fix' else "Reviewer's note")
    parts = [
        'The reviewer opened a side chat about one finding of this review.',
        f"Review: {data.get('title')}" + (f" (PR #{data['pr']})" if data.get('pr') else ''),
        f"Finding {finding['id']} [{LABEL.get(finding['severity'], '')}]: {finding['title']}",
        '', finding.get('body') or '(no further text)', '',
        f'Reviewer\'s decision so far: {status}.' + (f" {mine}: {dec['note'].strip()}" if (dec.get('note') or '').strip() else ''),
        '', 'Code at the finding (">" marks flagged lines, "+" lines added in the branch):',
    ]
    budget = EXCERPT_MAX_LINES
    for loc in finding.get('locations', []):
        excerpt, used = code_excerpt(data, loc, budget)
        parts.append(excerpt)
        budget -= used
    if note:
        parts += ['', note]
    parts += ['', "Reviewer's message:", message]
    return '\n'.join(parts)


def code_excerpt(data: dict, loc: dict, budget: int) -> tuple[str, int]:
    file = data['files'].get(loc.get('path') or '')
    if not file:
        return f"- {loc['raw']}: file not found in the reviewed code", 1
    lines, added = file['lines'], set(file['added'])
    a = max(1, loc['start'] - EXCERPT_PAD)
    b = min(len(lines), loc['end'] + EXCERPT_PAD, a + max(budget, 10) - 1)
    label = f"- {loc['path']}:{loc['start']}" + (f"-{loc['end']}" if loc['end'] > loc['start'] else '')
    rows = [f"{'>' if loc['start'] <= i <= loc['end'] else ' '}{'+' if i in added else ' '}{i:5} {lines[i - 1]}"
            for i in range(a, b + 1)]
    return label + '\n```\n' + '\n'.join(rows) + '\n```', len(rows)


def _update_pending(page: str, fid: str, job: str, status: str | None = None, step: str | None = None,
                    **fields: object) -> dict | None:
    """Update the thread's pending reply while it is still this job's (a stopped job's worker stays quiet)."""
    with jobs.locked(store.chat_path(page)):
        chat = store.load_chat(page)
        th = (chat.get('threads') or {}).get(fid)
        if not th or (th.get('pending') or {}).get('job') != job:
            return None
        if status:
            th['pending']['status'] = status
        if step:
            th['pending']['steps'] = (th['pending'].get('steps') or [])[-11:] + [step]
        th['pending'].update(fields)
        _save(page, chat)
        return th


def _finish(page: str, fid: str, job: str, message: dict) -> bool:
    """End the thread's pending reply with ``message`` while it is still this job's; False when it ended already."""
    with jobs.locked(store.chat_path(page)):
        chat = store.load_chat(page)
        th = (chat.get('threads') or {}).get(fid)
        if not th or (th.get('pending') or {}).get('job') != job:
            return False
        _end_pending(th, message)
        _save(page, chat)
    return True


def _stopped_message() -> dict:
    return {'role': 'claude', 'text': 'Stopped by you.', 'at': jobstate.now(), 'error': True, 'stopped': True}


def stop(page: str, fid: str) -> tuple[int, dict]:
    """Stop the worker answering in a thread, with its claude process, and say so in the thread."""
    pending = ((settled(page).get('threads') or {}).get(fid) or {}).get('pending')
    if not pending:
        return 409, {'error': 'Nothing to stop: the reply is already in.'}
    return jobstate.stop(pending, 'The chat process is still starting. Try again in a moment.',
                         lambda: _update_pending(page, fid, pending['job'], stopping=True),
                         lambda: _stopped(page, fid, pending['job']))


def _stopped(page: str, fid: str, job: str) -> bool:
    """After a stop's kill: say so in the thread. When the reply came in just before, whether that reply was a stop."""
    if not _finish(page, fid, job, _stopped_message()):
        last = ((store.load_chat(page).get('threads') or {}).get(fid) or {}).get('messages', [{}])[-1]
        return bool(last.get('stopped'))
    return True


def activity(chat: dict) -> list[dict]:
    """The page's side chats for the Running panel: each reply being written, and each thread's last reply."""
    out = []
    for fid, th in (chat.get('threads') or {}).items():
        pending, label = th.get('pending'), f'Chat on {fid}'
        if pending:
            out.append(jobstate.running_row(pending, id=f'chat:{fid}', kind='chat', fid=fid, label=label,
                                            session=pending.get('session') or th.get('claude_session'),
                                            **jobstate.stop_fields(pending, pending.get('status'))))
            continue
        last = (th.get('messages') or [None])[-1]
        if not last or last.get('role') != 'claude':
            continue
        try:
            ended = dt.datetime.fromisoformat(last['at']).timestamp()
        except (KeyError, TypeError, ValueError):
            continue
        state = 'stopped' if last.get('stopped') else 'failed' if last.get('error') else 'done'
        out.append(jobstate.ended_row(state, ended, last.get('text'), id=f'chat:{fid}', kind='chat', fid=fid,
                                      label=label))
    return out


def repo_flag(data: dict) -> str:
    github = data.get('github') or ''
    return f" --repo {github.removeprefix('https://github.com/')}" if github else ''


def where_text(cwd: str, checkout: str, is_snapshot: bool, ref: str | None) -> str:
    if is_snapshot and Path(cwd) == Path(checkout):
        return (f'Your working directory is a checkout of exactly the reviewed code (commit {ref[:12]}), so Read, '
                f'Grep and Glob see what the reviewer sees.')
    if is_snapshot:
        return (f'The reviewed code (commit {ref[:12]}) is checked out at {checkout}. Read and search files there, '
                f'by absolute path: your working directory may be on another branch.')
    if ref:
        return (f'Your working directory may differ from the reviewed commit {ref[:12]}: read reviewed files with '
                f'`git show {ref[:12]}:<path>`.')
    return 'Your working directory is the repository working tree, which is the reviewed code.'


def run(spec: dict) -> None:
    """Worker (``finding_viewer.py job``): answer one message in a thread and record the reply in ``chat.json``."""
    claude = claude_cli.binary()
    _run(claude, spec['page'], spec['fid'], spec['job'], spec['prompt'], spec.get('resume'),
         store.page_data(spec['page']) or {}, spec.get('model') or '', spec.get('mode') or 'finding', spec.get('cwd'))


def _run(claude: str, page: str, fid: str, job: str, prompt: str, resume: str | None, data: dict, model: str,
         mode: str, saved_cwd: str | None) -> None:
    _update_pending(page, fid, job, 'Checking out the reviewed code')
    checkout, is_snapshot = store.review_checkout(data)
    ref = store.reviewed_ref(data)
    fork = sessions.review_session(data) if mode == 'fork' and not resume else None
    notice = ''
    if mode == 'fork' and not resume and not fork:
        mode = 'finding'
        notice = "_The review session's transcript wasn't found, so this thread has the finding only._\n\n"
    cwd = saved_cwd if resume and saved_cwd else fork[1] if fork else checkout
    system = SYSTEM_PROMPT.format(where=where_text(cwd, checkout, is_snapshot, ref),
                                  merge_base=data.get('merge_base') or store.default_base(checkout) or 'origin/HEAD',
                                  pr_hint=f" For the whole PR: `gh pr diff {data['pr']}{repo_flag(data)}`." if data.get('pr') else '')
    cmd = [claude, '-p', *claude_cli.common_args(), '--output-format', 'stream-json', '--verbose',
           '--allowedTools', *READ_ONLY_TOOLS, '--append-system-prompt', system]
    if is_snapshot and Path(cwd) != Path(checkout):
        cmd += ['--add-dir', checkout]
    if resume:
        cmd += ['--resume', resume]
    elif fork:
        cmd += ['--resume', fork[0], '--fork-session']
        _update_pending(page, fid, job, 'Loading the review conversation')
    if model:
        cmd += ['--model', model]
    # total_cost_usd and modelUsage are cumulative over the session's whole history (a fork inherits the review's
    # spend), so the model comes from the replies and the cost of this message is derived below.
    session, answer, error, steps, total, used, usage = resume, None, None, [], None, None, {}
    try:
        stream = claude_cli.Stream(cmd, cwd, prompt, TIMEOUT_SECONDS)
        for ev in stream:
            if ev.get('type') == 'system' and ev.get('subtype') == 'init':
                # Recorded at once, so the Running panel shows the session while the reply is written.
                session = ev.get('session_id') or session
                _update_pending(page, fid, job, 'Thinking', session=session)
            elif ev.get('type') == 'assistant':
                used = (ev.get('message') or {}).get('model') or used
                for block in (ev.get('message') or {}).get('content') or []:
                    if block.get('type') == 'tool_use':
                        step = claude_cli.describe_tool(block)
                        steps.append(step)
                        _update_pending(page, fid, job, step, step)
                    elif block.get('type') == 'text':
                        _update_pending(page, fid, job, 'Writing')
            elif ev.get('type') == 'result':
                session = ev.get('session_id') or session
                answer = ev.get('result')
                total = ev.get('total_cost_usd')
                usage = ev.get('usage') or {}
                if ev.get('is_error'):
                    error = answer or ev.get('subtype') or 'error'
        if answer is None and not error:
            error = stream.failure
    except Exception as exc:  # noqa: BLE001 — any failure must end as a visible message, never a stuck "pending"
        error = repr(exc)
        log(f'{page} {fid}: {error}')
    with jobs.locked(store.chat_path(page)):
        chat = store.load_chat(page)
        th = (chat.get('threads') or {}).get(fid)
        if not th or (th.get('pending') or {}).get('job') != job:
            return  # stopped, or marked failed meanwhile: that message stands
        th['pending'] = None
        th['claude_session'] = session
        if not resume:
            th['mode'], th['cwd'] = mode, cwd
        prev_total = next((m['total'] for m in reversed(th['messages']) if m.get('total') is not None), None)
        if total is None:
            cost = None
        elif resume:
            cost = total - prev_total if prev_total is not None and total >= prev_total else None
        else:
            cost = None if fork else total
        context_tokens = sum(usage.get(k) or 0 for k in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'))
        th['messages'].append({'role': 'claude', 'text': error and f'Error: {error}' or notice + (answer or '(empty reply)'),
                               'at': jobstate.now(), 'steps': steps[-12:], 'error': bool(error), 'model': used,
                               'cost': cost, 'total': total, 'context_tokens': context_tokens or None})
        _save(page, chat)
