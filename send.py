"""Deliver one message to another Claude Code session.

Only a Claude can message another session (the SendMessage tool), so a headless ``claude -p`` whose only allowed tool
is SendMessage forwards a message the server wrote. ``draft.py`` runs it as a resume of the review's fork, so the
review session hears about the posted review from the fork that drafted it. ``handoff.send_fixes`` runs it as a session
of its own, which it deletes afterwards, to hand the triage to the review session at once. The stream-json output shows
the tool call, so the server checks that the message went out verbatim and records what the tool answered.
"""
from __future__ import annotations

from pathlib import Path

import chat
import claude_cli

TIMEOUT_SECONDS = 180

RELAY_PROMPT = """Relay one message to another Claude Code session on the reviewer's behalf.
Call SendMessage exactly once, with to="{to}" and message set to the text between the markers below, copied
verbatim: same characters and line breaks, nothing added or removed. Call no other tool. Then reply with the single
word SENT, or with the error the tool returned.

<<<MESSAGE
{message}
MESSAGE>>>"""


def deliver(to: str, message: str, extra_args: list[str], cwd: str | Path) -> dict:
    """Send `message` to the session named `to`; {'ok', 'error', 'reply', 'verbatim', 'cost', 'session'} (the relay's own session id)."""
    cmd = [claude_cli.binary(), '-p', *claude_cli.common_args(), '--output-format', 'stream-json', '--verbose',
           '--allowedTools', 'SendMessage', *extra_args]
    sent, reply, error, cost, answer = None, None, None, None, None
    session = None
    try:
        stream = claude_cli.Stream(cmd, cwd, RELAY_PROMPT.format(to=to, message=message), TIMEOUT_SECONDS)
        call_id = None
        for ev in stream:
            if ev.get('type') == 'system' and ev.get('subtype') == 'init' and ev.get('session_id'):
                session = ev['session_id']
            for block in (ev.get('message') or {}).get('content') or []:
                if not isinstance(block, dict):
                    continue
                if block.get('type') == 'tool_use' and block.get('name') == 'SendMessage' and call_id is None:
                    call_id, sent = block.get('id'), block.get('input') or {}
                elif block.get('type') == 'tool_result' and block.get('tool_use_id') == call_id and call_id:
                    content = block.get('content')
                    text = content if isinstance(content, str) else ' '.join(
                        c.get('text', '') for c in content or [] if isinstance(c, dict))
                    reply = {'text': text.strip()[:600], 'is_error': bool(block.get('is_error'))}
            if ev.get('type') == 'result':
                answer, cost = ev.get('result'), ev.get('total_cost_usd')
                if ev.get('is_error'):
                    error = answer or ev.get('subtype') or 'error'
        if sent is None and not error:
            error = 'The relay did not call SendMessage: ' + (answer or stream.failure)
    except Exception as exc:  # noqa: BLE001 — the caller records any failure as a visible state
        error = repr(exc)
    if reply and reply['is_error']:
        error = reply['text'] or 'SendMessage failed'
    verbatim = bool(sent) and (sent.get('message') or '').strip() == message.strip() and sent.get('to') == to
    if sent and not verbatim:
        chat.log(f'send to {to}: the relay changed the message or recipient (to={sent.get("to")!r})')
    if error:
        chat.log(f'send to {to}: {error}')
    return {'ok': not error, 'error': error, 'reply': (reply or {}).get('text'), 'verbatim': verbatim, 'cost': cost,
            'session': session}
