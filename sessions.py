"""Claude Code's own session files, which this tool reads and deletes: the transcripts under ~/.claude/projects, the
open sessions under ~/.claude/sessions, and the environment variables that tie a process to its parent session.

Claude Code does not document these, so every read of them lives here.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
from pathlib import Path

import config

SESSION_RE = re.compile(r'^[\w-]{8,64}$')
# Variables that tie a process to the parent Claude Code session; the side chat must be its own session.
PARENT_SESSION_ENV = ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_MESSAGING_SOCKET',
                      'CLAUDE_CODE_MESSAGING_TOKEN', 'CLAUDE_CODE_CHILD_SESSION', 'CLAUDE_CODE_SESSION_ATTENDED',
                      'CLAUDE_PID')


def transcript(sid: str | None) -> Path | None:
    """The transcript Claude Code keeps for a session, when it still exists."""
    if not sid or not SESSION_RE.match(sid):
        return None
    return next((Path.home() / '.claude' / 'projects').glob(f'*/{sid}.jsonl'), None)


def review_session(data: dict) -> tuple[str, str] | None:
    """(id, working directory) of the Claude Code session that wrote the review, when its transcript still exists."""
    sid = data.get('review_session') or data.get('session_id')
    path = transcript(sid)
    if not path:
        return None
    with path.open(encoding='utf-8', errors='replace') as fh:
        for line in fh:
            try:
                cwd = json.loads(line).get('cwd')
            except ValueError:
                continue
            if cwd:
                return sid, cwd
    return None


def live_sessions() -> dict[str, dict]:
    """The open Claude Code sessions by id (name, folder, status), from ~/.claude/sessions/<pid>.json.

    Claude Code writes one such file per open session. The format is not documented, so anything unexpected reads as
    "not running" and the page falls back to the resume command.
    """
    live: dict[str, dict] = {}
    for p in (Path.home() / '.claude' / 'sessions').glob('*.json'):
        try:
            info = json.loads(p.read_text())
            if not isinstance(info, dict) or not isinstance(info.get('sessionId'), str):
                continue
            os.kill(int(info['pid']), 0)
        except PermissionError:
            pass
        except (OSError, ValueError, KeyError, TypeError):
            continue
        live.setdefault(info['sessionId'], {'name': info.get('name'), 'cwd': info.get('cwd'), 'status': info.get('status')})
    return live


def live_session(sid: str) -> dict | None:
    """The running Claude Code process for a session id, if it is open (see ``live_sessions``)."""
    return live_sessions().get(sid)


def delete_session(sid: str | None, keep: str | None = None) -> None:
    """Remove a session's transcript and its sidecar folder. Every session this tool deletes is one it started (a fork
    of the review session, a side chat's first reply, or the relay that sent fixes to the review session); config
    delete_forks false keeps them all. Never ``keep``
    (the review session), and never a session open in Claude Code right now, which would go on writing to it."""
    if not config.get('delete_forks') or not sid or not SESSION_RE.match(sid) or sid == keep or live_session(sid):
        return
    for p in (Path.home() / '.claude' / 'projects').glob(f'*/{sid}*'):
        if p.name == f'{sid}.jsonl' and p.is_file():
            p.unlink()
        elif p.name == sid and p.is_dir():
            shutil.rmtree(p, ignore_errors=True)


def find_transcript(cwd: str) -> Path | None:
    projects = Path.home() / '.claude' / 'projects'
    sid = os.environ.get('CLAUDE_CODE_SESSION_ID')
    if sid:
        hits = list(projects.glob(f'*/{sid}.jsonl'))
        if hits:
            return hits[0]
    folder = projects / re.sub(r'[^A-Za-z0-9]', '-', cwd)
    files = sorted(folder.glob('*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def resume_command(cwd: str | None, sid: str) -> str:
    """The terminal command that reopens a Claude session where it ran."""
    return (f'cd {shlex.quote(cwd)} && ' if cwd else '') + f'claude --resume {sid}'
