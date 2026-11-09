"""Running the claude CLI as a child: where it is, the environment and flags every child gets, a one-shot JSON call,
and a stream-json call read event by event.

Every child is a session of its own (the parent session's variables are dropped), carries FINDING_VIEWER_CHILD=1 so
this tool's hooks stay quiet in it, and runs without hooks, MCP servers or permission prompts.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Iterator

import config
import sessions


def binary() -> str:
    """The claude CLI: on PATH, else where its installer puts it (it may not exist)."""
    return shutil.which('claude') or str(Path.home() / '.local/bin/claude')


def child_env() -> dict[str, str]:
    """This process's environment without the parent session's variables, marked as this tool's child."""
    env = {k: v for k, v in os.environ.items() if k not in sessions.PARENT_SESSION_ENV}
    env['FINDING_VIEWER_CHILD'] = '1'
    return env


def common_args() -> list[str]:
    """The flags every child gets: no MCP servers, no hooks, no permission prompts (only the allowed tools run)."""
    return ['--strict-mcp-config', '--settings', json.dumps({'disableAllHooks': True}), '--permission-mode', 'dontAsk']


def run_json(args: list[str], prompt: str, cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    """``claude -p --output-format json`` with the common flags, then ``args``, and ``prompt`` on stdin, run to its end
    in ``cwd`` (the home folder when ``cwd`` is not a folder). Raises subprocess.TimeoutExpired and OSError."""
    return subprocess.run([binary(), '-p', '--output-format', 'json', *common_args(), *args],
                          cwd=cwd if cwd.is_dir() else Path.home(), input=prompt, capture_output=True, text=True,
                          env=child_env(), timeout=timeout)


class Stream:
    """A ``claude -p --output-format stream-json`` child. Iterating it starts the process in ``cwd``, writes ``prompt``
    to its stdin and yields each event it prints, parsed; lines that are not JSON are skipped. A watchdog kills it after
    ``timeout`` seconds. Once the events end, ``failure`` holds what to report when no result came: the end of its
    stderr, else its exit code. Starting it can raise OSError."""

    def __init__(self, cmd: list[str], cwd: str | Path, prompt: str, timeout: int) -> None:
        self.cmd, self.cwd, self.prompt, self.timeout = cmd, cwd, prompt, timeout
        self.failure = ''

    def __iter__(self) -> Iterator[dict]:
        # stderr goes to a file: an unread pipe could fill up and stall the stdout loop.
        with tempfile.TemporaryFile(mode='w+') as stderr:
            proc = subprocess.Popen(self.cmd, cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=stderr, text=True, env=child_env())
            watchdog = threading.Timer(self.timeout, proc.kill)
            watchdog.start()
            proc.stdin.write(self.prompt)
            proc.stdin.close()
            for line in proc.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                yield ev
            proc.wait(timeout=10)
            watchdog.cancel()
            stderr.seek(0)
            try:
                tail = stderr.read().strip()[-400:]
            except ValueError as exc:  # undecodable stderr: the error the callers recorded before, when no result came
                tail = repr(exc)
            self.failure = tail or f'claude exited with code {proc.returncode}'


def describe_tool(block: dict) -> str:
    name, inp = block.get('name'), block.get('input') or {}
    if name == 'Read':
        return f"Reading {Path(inp.get('file_path', '')).name}"
    if name == 'Grep':
        return f"Searching for “{str(inp.get('pattern', ''))[:40]}”"
    if name == 'Glob':
        return f"Looking for {str(inp.get('pattern', ''))[:40]}"
    if name == 'Bash':
        return f"Running {str(inp.get('command', ''))[:70]}"
    return f'Using {name}'


def limit_args(prefix: str) -> list[str]:
    """The claude flags for a run's limits (config ``<prefix>.max_turns``, ``<prefix>.max_budget_usd``); 0 is none."""
    args = []
    if turns := config.get(f'{prefix}.max_turns'):
        args += ['--max-turns', str(turns)]
    if budget := config.get(f'{prefix}.max_budget_usd'):
        args += ['--max-budget-usd', f'{budget:g}']
    return args


def limit_error(prefix: str, what: str, result: dict | None, error: str | None) -> str | None:
    """A run's error, said in the reviewer's words when it ended at one of its limits (claude's result subtypes):
    ``what`` names the run ("The re-review"), ``prefix`` its settings."""
    subtype = (result or {}).get('subtype')
    if not error or subtype not in ('error_max_turns', 'error_max_budget_usd'):
        return error
    limit, key = ('turn', 'max_turns') if subtype == 'error_max_turns' else ('spending', 'max_budget_usd')
    return (f'{what} stopped at its {limit} limit ({prefix}.{key}) before it answered. Raise the limit, or set it to 0 '
            'for none, and start again.')


def usage_of(result: dict | None, where: str) -> dict:
    """What a run read and wrote, in tokens, and what claude says it cost. A fork's total_cost_usd can include the
    review session's own spend (chat.py says so for a forked chat), which one result cannot tell apart; the tokens this
    run read are its own."""
    usage = (result or {}).get('usage') or {}
    read = sum(usage.get(k) or 0 for k in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'))
    return {'context': where, 'tokens_read': read, 'cached': usage.get('cache_read_input_tokens') or 0,
            'output_tokens': usage.get('output_tokens') or 0, 'cost_usd': (result or {}).get('total_cost_usd')}


def spent(usage: dict | None) -> str:
    """'412k tokens read, $1.37 (the fork's total)', or '' when nothing was counted."""
    read, cost = (usage or {}).get('tokens_read') or 0, (usage or {}).get('cost_usd')
    parts = [f'{read / 1000:.0f}k tokens read' if read >= 1000 else f'{read} tokens read'] if read else []
    if isinstance(cost, (int, float)):
        parts.append(f'${cost:.2f}' + (" (the fork's total)" if usage.get('context') == 'fork' else ''))
    return ', '.join(parts)
