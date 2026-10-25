#!/usr/bin/env python3
"""Finding viewer: open a local page showing the code each code-review finding points at.

  finding_viewer.py hook               Stop hook. Opens the page for the turn that just ended, only when its prompt
                                       names the viewer ("open it in the finding viewer", "/findings"); with the
                                       auto_open setting off it builds the page and prints its address instead.
  finding_viewer.py open [REPORT.md]   Latest review in this session, else the latest saved review report,
                                       else the list of all reviews.
  finding_viewer.py list               Open the list of all reviews.
  finding_viewer.py decisions          Print the latest Post / Don't post triage and notes.
  finding_viewer.py inject             UserPromptSubmit hook. Hands new triage to Claude.
  finding_viewer.py serve              Run the localhost server that serves pages and stores triage.
  finding_viewer.py migrate            Move the runtime data from beside the code into the data directory, and give each
                                       review page its own folder (paths.py).
  finding_viewer.py doctor             Check the machine and the wiring: one PASS / WARN / FAIL line per check.
  finding_viewer.py install            Register the hooks and /findings in ~/.claude for this copy (not for a plugin).
  finding_viewer.py uninstall          Remove them again (any copy's); the data stays.
  finding_viewer.py render SPEC.json   Internal: build the page from a spec written by the hook.
  finding_viewer.py job SPEC.json      Internal: a detached worker for drafting, posting, a side chat or a re-review
                                       of new commits (jobs.py).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import config
import handoff
import hook
import paths
import render
import sessions
import setup_cmds
import triage


log = paths.log()


def open_url(url: str) -> None:
    subprocess.run(['open' if sys.platform == 'darwin' else 'xdg-open', url],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def render_and_open(spec: dict) -> Path | None:
    """Render the page (``render.render``), start the server and, unless ``spec['open']`` is false (the Stop hook
    sets it from the auto_open setting; /findings sets it true), open the page; None when there was nothing to
    render."""
    out = render.render(spec)
    if out:
        # Served over localhost so the page can save triage; a file:// page can only keep it in the browser. The server
        # is started even when the page does not open, so the address the hook printed answers.
        served = triage.ensure_server()
        if spec.get('open', True):
            open_url(triage.page_url(out) if served else out.as_uri())
        elif not served:
            log(f'render: the server did not start; the page is at {out.as_uri()}')
    return out


def cmd_open(args: list[str]) -> int:
    cwd = os.getcwd()
    spec = None
    if args:
        p = Path(args[0]).expanduser()
        p = p if p.is_absolute() else Path(cwd) / p
        spec = hook.spec_from_report(p)
        if not spec:
            print(f'No findings with file:line anchors in {p}' if p.is_file() else f'No such file: {p}')
            return 1
    else:
        transcript = sessions.find_transcript(cwd)
        turns = hook.read_turns(transcript) if transcript else []
        for i in range(len(turns) - 1, -1, -1):
            spec = hook.spec_from_turn(turns, i, cwd)
            if spec:
                break
        if not spec:
            report = render.latest_report(cwd)
            spec = hook.spec_from_report(report) if report else None
    if not spec:
        return cmd_list(f"No code review in this session or at {config.get('report_glob')}, so opened all reviews")
    spec['session_id'] = os.environ.get('CLAUDE_CODE_SESSION_ID')
    spec['open'] = True  # asked for with /findings, so it opens whatever auto_open says
    if hook.needs_claude(spec):
        # The list of reviews opens if Claude finds no findings in it.
        spec['list_if_empty'] = True
        out = render.launch_render(spec)
        print(f"Claude is reading the review ({spec['subtitle']}); the page opens in about a minute: {triage.page_url(out)}")
        return 0
    out = render_and_open(spec)
    print(f"Opened {len(spec['findings'])} findings to triage ({spec['subtitle']}): {triage.page_url(out)}")
    return 0


def cmd_list(message: str = 'Opened all reviews') -> int:
    if not triage.ensure_server():
        print(f'The review server did not start; see {paths.LOG}')
        return 1
    url = f'{triage.ORIGIN}/'
    open_url(url)
    print(f'{message}: {url}')
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else 'open'
    if cmd == 'migrate':
        line, old = paths.migrate_if_needed(), paths.CODE / 'out'
        if not line and old.is_dir() and old.resolve() != paths.OUT.resolve():
            kind = f'links to {old.resolve()}' if old.is_symlink() else 'is a folder'
            line = f'not moved: {old} {kind}, apart from {paths.OUT}; move what you need from it by hand'
        layout = paths.migrate_layout()
        if layout:
            try:
                triage.replace_old_server()  # a server still running the old code reads the files that just moved
            except Exception as exc:  # noqa: BLE001 — report the migration whatever happens here
                log(f'replace old server: {exc!r}')
        print('\n'.join(filter(None, (line, layout))) or f'Nothing to migrate: the runtime data is in {paths.DATA}.')
        return 0 if (not line or line.startswith('moved')) and \
            (not layout or (layout.startswith('gave') and '; could not move' not in layout)) else 1
    if cmd in ('doctor', 'install', 'uninstall'):
        return {'doctor': setup_cmds.doctor, 'install': setup_cmds.install, 'uninstall': setup_cmds.uninstall}[cmd]()
    if cmd not in ('hook', 'inject') or not os.environ.get('FINDING_VIEWER_CHILD'):  # a child claude's hooks stay quiet
        paths.migrate_if_needed()
        if paths.migrate_layout() and cmd != 'serve':
            try:
                triage.replace_old_server()  # a server still running the old code reads the files that just moved
            except Exception as exc:  # noqa: BLE001 — the hooks run this first and must never fail
                log(f'replace old server: {exc!r}')
    if cmd == 'hook':
        try:
            return hook.cmd_hook()
        except Exception as exc:  # noqa: BLE001 — a crashing Stop hook must never disturb the session
            log(f'hook error: {exc!r}')
            return 0
    if cmd == 'render':
        spec = json.loads(Path(argv[2]).read_text())
        try:
            out = render_and_open(spec)
        except Exception as exc:  # noqa: BLE001 — detached child, only the log can report it
            log(f'render error: {exc!r}')
            raise
        if out is None and spec.get('list_if_empty'):
            cmd_list()
        return 0
    if cmd == 'job':
        try:
            return triage.cmd_job(argv[2])
        except Exception as exc:  # noqa: BLE001 — detached worker, only the log can report it
            log(f'job error: {exc!r}')
            raise
    if cmd == 'inject':
        try:
            return handoff.cmd_inject()
        except Exception as exc:  # noqa: BLE001 — a crashing prompt hook must never block the user's message
            log(f'inject error: {exc!r}')
            return 0
    if cmd == 'decisions':
        return handoff.cmd_decisions()
    if cmd == 'serve':
        return triage.serve()
    if cmd in ('on', 'off'):
        print('The on and off commands are gone: name the finding viewer in your prompt, or run /findings; '
              '"auto_open": false in config.json keeps the hook from opening the browser.')
        return 0
    if cmd == 'list':
        return cmd_list()
    if cmd == 'open':
        return cmd_open(argv[2:])
    return cmd_open(argv[1:])


if __name__ == '__main__':
    sys.exit(main(sys.argv))
