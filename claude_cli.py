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
