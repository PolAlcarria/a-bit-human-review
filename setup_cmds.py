"""Setting the tool up in Claude Code, and checking the machine it runs on: doctor, install and uninstall.

install and uninstall wire the manual path, for a copy of the code that is not a plugin: the Stop and UserPromptSubmit
hooks in ~/.claude/settings.json and the /findings command in ~/.claude/commands/findings.md, each running this copy's
finding_viewer.py by its absolute path. Both are idempotent and print what they changed; settings.json is copied to
settings.json.bak before they change it. A plugin needs neither: hooks/hooks.json and skills/findings/SKILL.md come
with the code.

doctor prints one PASS, WARN or FAIL line per check, then the effective configuration, and exits 1 on any FAIL.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path

import claude_cli
import config
import paths
import triage

PLUGIN = 'a-bit-human-review'
SCRIPT = paths.CODE / 'finding_viewer.py'
SKILL = paths.CODE / 'skills' / 'findings' / 'SKILL.md'
# The subcommand each hook event runs, and its timeout in seconds, as in hooks/hooks.json.
HOOKS = {'Stop': ('hook', 15), 'UserPromptSubmit': ('inject', 10)}
# A hook command that runs some copy of this tool: python3 <any path>/finding_viewer.py hook|inject, quoted or not.
OURS_RE = re.compile(r'finding_viewer\.py["\']?\s+(hook|inject)\b')
# The script in a hook command or the command file: single-quoted, double-quoted (no newline in either) or a bare word.
SCRIPT_RE = re.compile(r'''(?:'([^'\n]*finding_viewer\.py)'|"([^"\n]*finding_viewer\.py)"|'''
                       r'''([^\s"'`()]*finding_viewer\.py))''')
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def settings_file() -> Path:
    return Path.home() / '.claude' / 'settings.json'


def command_file() -> Path:
    return Path.home() / '.claude' / 'commands' / 'findings.md'


def in_plugin() -> bool:
    """This code runs as a plugin: Claude Code set CLAUDE_PLUGIN_ROOT or CLAUDE_PLUGIN_DATA (the plugin's hooks get
    both, its command passes the second), or the code sits in its plugin folder."""
    return (bool(os.environ.get('CLAUDE_PLUGIN_ROOT')) or bool(os.environ.get('CLAUDE_PLUGIN_DATA'))
            or '/.claude/plugins/' in str(paths.CODE))


def plugin_enabled(settings: dict) -> bool:
    """settings.json enables this plugin, from any marketplace ("a-bit-human-review@<marketplace>": true)."""
    enabled = settings.get('enabledPlugins')
    return isinstance(enabled, dict) and any(k.split('@')[0] == PLUGIN and v for k, v in enabled.items())


def read_settings() -> tuple[dict, str | None]:
    """(settings, the file's text): ({}, None) when there is no file. Raises OSError, or ValueError when the file is
    not a JSON object."""
    path = settings_file()
    try:
        text = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return {}, None
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f'{path} is not a JSON object')
    return data, text


def manual_hooks(settings: dict) -> dict[str, list[str]]:
    """The commands of this tool's hooks in settings.json, by subcommand: {'hook': [...], 'inject': [...]}."""
    found: dict[str, list[str]] = {'hook': [], 'inject': []}
    for _event, _group, entry in _entries(settings):
        m = OURS_RE.search(str(entry.get('command') or ''))
        if m:
            found[m.group(1)].append(str(entry['command']))
    return found


def _entries(settings: dict):
    """(event, group, entry) for every command hook in settings.json, skipping anything of an unexpected shape."""
    hooks = settings.get('hooks')
    for event, groups in (hooks.items() if isinstance(hooks, dict) else []):
        for group in (groups if isinstance(groups, list) else []):
            if isinstance(group, dict) and isinstance(group.get('hooks'), list):
                for entry in group['hooks']:
                    if isinstance(entry, dict):
                        yield event, group, entry


def _without(items: list, item) -> None:
    """Remove ``item`` from ``items`` in place, by identity: list.remove would match an equal dict, not this one."""
    items[:] = [x for x in items if x is not item]


def script_of(text: str) -> Path | None:
    """The finding_viewer.py a hook command or the command file runs, unquoted, with ~ expanded."""
    found = _script_word(text)
    if found is None:
        m = SCRIPT_RE.search(text)
        found = next((g for g in m.groups() if g), None) if m else None
    if found is None:
        return None
    try:
        return Path(found).expanduser()
    except RuntimeError:  # ~user with no such user: the path as written
        return Path(found)


def _script_word(text: str) -> str | None:
    """The first word that names a finding_viewer.py, split the way the shell splits it, so a quoted path with a space
    or an apostrophe comes out whole. The command file's `!` line and allowed-tools rule add ! ` :* and ) around it."""
    for line in text.splitlines():
        if 'finding_viewer.py' not in line:
            continue
        try:
            words = shlex.split(line)
        except ValueError:
            continue
        for word in words:
            word = word.lstrip('!`').rstrip(':*)`')
            if word.endswith('finding_viewer.py'):
                return word
    return None


def quoted_script() -> str:
    """This copy's script as one shell word: quoted when its path has a space or another special character."""
    return shlex.quote(str(SCRIPT))


def hook_command(sub: str) -> str:
    return f'python3 {quoted_script()} {sub}'


def command_text() -> str:
    """The /findings command file for this copy: the plugin's skill with this script's path, without its name line and
    without the plugin's data folder prefix."""
    text = SKILL.read_text(encoding='utf-8').replace('env CLAUDE_PLUGIN_DATA=${CLAUDE_PLUGIN_DATA} ', '')
    text = text.replace('${CLAUDE_PLUGIN_ROOT}/finding_viewer.py', quoted_script())
    return re.sub(r'(?m)^name: .*\n', '', text, count=1)


def _write_fresh(path: Path, text: str, mode: int | None) -> None:
    """Write ``path`` as a new file: created with ``mode`` when given (else the default for a new file), then set to
    ``mode`` exactly, so the file is never more readable than the original while it is being written."""
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666 if mode is None else mode)
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        fh.write(text)
    if mode is not None:
        os.chmod(path, mode)


def _write_settings(settings: dict, original: str | None) -> Path | None:
    """Replace settings.json in one step, after copying the original text to settings.json.bak (returned). Both keep the
    original file's permission bits: the file can hold secrets in its env."""
    path = settings_file().resolve()  # a symlinked settings.json is written where it points
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if original is not None else None
    backup = None
    if original is not None:
        backup = path.with_name(path.name + '.bak')
        _write_fresh(backup, original, mode)
    tmp = path.with_name(path.name + '.tmp')
    _write_fresh(tmp, json.dumps(settings, indent=2, ensure_ascii=False) + '\n', mode)
    os.replace(tmp, path)
    return backup


def install() -> int:
    """Wire this copy into ~/.claude: its two hooks (replacing any other copy's) and the /findings command."""
    if in_plugin():
        print(f'This copy runs as a plugin ({paths.CODE}): the plugin registers its own hooks and '
              f'/{PLUGIN}:findings, and its folder changes with each update. Nothing installed.')
        return 1
    try:
        settings, original = read_settings()
    except (OSError, ValueError) as exc:
        print(f'Cannot read {settings_file()}: {exc}. Nothing changed.')
        return 1
    hooks = settings.setdefault('hooks', {})
    if not isinstance(hooks, dict) or not all(isinstance(hooks.get(ev, []), list) for ev in HOOKS):
        print(f'Cannot change {settings_file()}: its hooks are not in the usual shape. Nothing changed.')
        return 1
    changes, status = [], 0
    for event, (sub, timeout) in HOOKS.items():
        want = {'type': 'command', 'command': hook_command(sub), 'timeout': timeout}
        groups = hooks.setdefault(event, [])
        kept = False
        for ev, group, entry in list(_entries(settings)):
            m = OURS_RE.search(str(entry.get('command') or ''))
            if ev != event or not m or m.group(1) != sub:
                continue
            if entry == want and not kept:
                kept = True
                continue
            _without(group['hooks'], entry)
            changes.append(f'removed {event} hook: {entry.get("command")}')
            if not group['hooks']:
                _without(groups, group)
        if not kept:
            groups.append({'hooks': [want]})
            changes.append(f'added {event} hook: {want["command"]} (timeout {timeout} s)')
    backup = None
    if changes:
        try:
            backup = _write_settings(settings, original)
        except OSError as exc:
            print(f'Cannot write {settings_file()}: {exc}. settings.json is unchanged: no hook was added or removed.')
            return 1
    cmd, text = command_file(), command_text()
    error = None
    try:
        current = cmd.read_text(encoding='utf-8', errors='replace') if cmd.is_file() else None
        if current is not None and 'finding_viewer.py' not in current:
            print(f"Left alone: {cmd} is another command named /findings, so this tool's /findings is not installed.")
            status = 1
        elif current != text:
            cmd.parent.mkdir(parents=True, exist_ok=True)
            cmd.write_text(text, encoding='utf-8')
            changes.append(f'{"updated" if current is not None else "added"} {cmd}')
    except OSError as exc:
        error = (f'Cannot write {cmd}: {exc}. /findings is not installed; '
                 + ('the hook changes above were made.' if changes else 'nothing else changed.'))
    for line in changes:
        print(line)
    if backup:
        print(f'The previous settings are in {backup}.')
    if error:
        print(error)
        return 1
    if not changes:
        print(f'Nothing to change: the hooks and /findings already run {SCRIPT}.')
    if plugin_enabled(settings):
        print(f'Warning: the {PLUGIN} plugin is enabled too, so each hook runs twice. '
              'Disable the plugin or run uninstall.')
    print(f'Check the machine with: python3 {quoted_script()} doctor')
    return status


def uninstall() -> int:
    """Remove every manual hook of this tool from ~/.claude/settings.json, whichever copy it runs, and its /findings
    command. The data folder stays."""
    try:
        settings, original = read_settings()
    except (OSError, ValueError) as exc:
        print(f'Cannot read {settings_file()}: {exc}. Nothing changed.')
        return 1
    changes = []
    hooks = settings.get('hooks')
    for event, group, entry in list(_entries(settings)):
        if OURS_RE.search(str(entry.get('command') or '')):
            _without(group['hooks'], entry)
            changes.append(f'removed {event} hook: {entry.get("command")}')
            if not group['hooks']:
                groups = hooks.get(event, [])
                _without(groups, group)
                if not groups:
                    hooks.pop(event, None)
    if isinstance(hooks, dict) and not hooks and changes:
        del settings['hooks']
    backup = None
    if changes:
        try:
            backup = _write_settings(settings, original)
        except OSError as exc:
            print(f'Cannot write {settings_file()}: {exc}. settings.json is unchanged: no hook was removed.')
            return 1
    cmd = command_file()
    error = None
    try:
        if cmd.is_file():
            if 'finding_viewer.py' in cmd.read_text(encoding='utf-8', errors='replace'):
                cmd.unlink()
                changes.append(f'removed {cmd}')
            else:
                print(f'Left alone: {cmd} is another command named /findings.')
    except OSError as exc:
        error = (f'Cannot remove {cmd}: {exc}. '
                 + ('the hook removals above were made.' if changes else 'nothing else changed.'))
    for line in changes:
        print(line)
    if backup:
        print(f'The previous settings are in {backup}.')
    if error:
        print(error)
        return 1
    if not changes:
        print('Nothing to remove: no hook or /findings command of this tool in ~/.claude.')
    print(f'The review data stays in {paths.DATA}; delete that folder to remove it too.')
    return 0


def _run(cmd: list[str], timeout: float = 20) -> str | None:
    """The first line of stdout (stripped) when the command exits 0, else None. Never raises."""
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    lines = r.stdout.strip().splitlines()
    return lines[0].strip() if lines else ''


def check_gh() -> tuple[str, str, str]:
    if shutil.which('gh') is None:
        return 'FAIL', 'gh', 'gh not found on PATH: PR reviews, freshness checks and posting need it'
    try:
        r = subprocess.run(['gh', 'auth', 'status', '--hostname', 'github.com'], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, errors='replace', timeout=20)
    except subprocess.TimeoutExpired:
        return 'WARN', 'gh', 'gh auth status did not answer in 20 s'
    except OSError as exc:
        return 'FAIL', 'gh', f'gh cannot run: {exc}'
    if r.returncode != 0:
        return 'FAIL', 'gh', 'not logged in: run gh auth login'
    account = next((ln.strip() for ln in (r.stdout + r.stderr).splitlines() if 'account' in ln), '')
    return 'PASS', 'gh', 'logged in' + (f': {account}' if account else '')


def check_claude() -> tuple[str, str, str]:
    binary = claude_cli.binary()
    if not Path(binary).is_file():
        return 'FAIL', 'claude', 'claude not found on PATH or at ~/.local/bin/claude'
    try:
        r = subprocess.run([binary, '--version'], env=claude_cli.child_env(), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, errors='replace', timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 'FAIL', 'claude', f'{binary} --version failed: {exc}'
    if r.returncode != 0:
        err = r.stderr.strip().splitlines()
        reason = err[-1] if err else f'exit code {r.returncode}'
        return 'FAIL', 'claude', f'{binary} --version failed: {reason}'
    first = (r.stdout.strip().splitlines() or ['?'])[0]
    return 'PASS', 'claude', f'{first} ({binary})'


def check_data() -> tuple[str, str, str]:
    try:
        paths.DATA.mkdir(parents=True, exist_ok=True)
        probe = paths.DATA / '.doctor-probe'
        probe.write_text('ok', encoding='utf-8')
        probe.unlink()
    except OSError as exc:
        return 'FAIL', 'data', f'{paths.DATA} is not writable: {exc}'
    return 'PASS', 'data', f'{paths.DATA} is writable'


def check_hooks(manual: dict[str, list[str]], plugin: bool) -> tuple[str, str, str]:
    """The Stop and UserPromptSubmit hooks, from the plugin or from settings.json: one set, each running this copy."""
    have = [sub for sub in ('hook', 'inject') if manual[sub]]
    where = settings_file()
    if plugin and have:
        return ('WARN', 'hooks', f'the plugin and {where} both register them, so each hook runs twice and a review '
                f'opens twice: remove the manual ones with python3 {quoted_script()} uninstall')
    if plugin:
        return 'PASS', 'hooks', 'the plugin registers the Stop and UserPromptSubmit hooks'
    if not have:
        return 'FAIL', 'hooks', f'none registered: install the plugin, or run python3 {quoted_script()} install'
    if len(have) == 1:
        missing = 'UserPromptSubmit (inject)' if 'hook' in have else 'Stop (hook)'
        return 'FAIL', 'hooks', f'the {missing} hook is missing from {where}: run python3 {quoted_script()} install'
    found = [(command, script_of(command)) for sub in have for command in manual[sub]]
    for command, script in found:
        if script is None or not script.is_file():
            return ('FAIL', 'hooks', f'a hook runs {script or command}, which does not exist: '
                    f'run python3 {quoted_script()} install')
    for _command, script in found:
        if script.resolve() != SCRIPT.resolve():
            return 'WARN', 'hooks', f'the hooks run {script}, not this copy ({SCRIPT})'
    if any(len(manual[sub]) > 1 for sub in have):
        return 'WARN', 'hooks', f'a hook is registered twice in {where}, so it runs twice'
    return 'PASS', 'hooks', f'Stop and UserPromptSubmit hooks in {where}'


def check_command(plugin: bool) -> tuple[str, str, str]:
    """The /findings command file: this copy's, another command of that name, or none. The plugin's own command is
    not a file here."""
    cmd = command_file()
    try:
        text = cmd.read_text(encoding='utf-8', errors='replace') if cmd.is_file() else ''
    except OSError as exc:
        return 'FAIL', 'command', f'cannot read {cmd}: {exc}'
    if 'finding_viewer.py' in text:
        script = script_of(text)
        if script is None or not script.is_file():
            return ('FAIL', 'command', f'{cmd} runs {script}, which does not exist: '
                    f'run python3 {quoted_script()} install')
        if script.resolve() != SCRIPT.resolve():
            return 'WARN', 'command', f'{cmd} runs {script}, not this copy ({SCRIPT})'
        extra = f', and /{PLUGIN}:findings from the plugin' if plugin else ''
        return 'PASS', 'command', f'/findings in {cmd}{extra}'
    if plugin:
        return 'PASS', 'command', f'/{PLUGIN}:findings from the plugin'
    if text:
        return 'FAIL', 'command', f'{cmd} is another command named /findings: install the plugin for /{PLUGIN}:findings'
    return 'FAIL', 'command', f'no /findings command: install the plugin, or run python3 {quoted_script()} install'


def check_server(port: int, timeout: float = 2.0, where: str | None = None) -> tuple[str, str, str]:
    """The review server on ``port``: this code's version answering, an older one (the next open restarts it), a free
    port, or another program on it. ``where`` says where the port was set (a variable, or a file), for the advice."""
    where = str(paths.CONFIG) if where is None else where
    info = None
    try:
        with NO_PROXY.open(f'http://127.0.0.1:{port}/api/ping', timeout=timeout) as resp:
            info = json.loads(resp.read().decode('utf-8'))
    except (OSError, ValueError, http.client.HTTPException):  # a non-HTTP program raises BadStatusLine and the like
        info = None
    if isinstance(info, dict) and info.get('app') == 'finding-viewer':
        v = info.get('version')
        if v == triage.SERVER_VERSION:
            return 'PASS', 'server', f'version {v} answers on port {port} (pid {info.get("pid")})'
        return ('WARN', 'server', f'the server on port {port} runs version {v}, this code is version '
                f'{triage.SERVER_VERSION}: the next open restarts it')
    with socket.socket() as s:
        s.settimeout(1)
        taken = s.connect_ex(('127.0.0.1', port)) == 0
    if taken:
        fix = f'change {where}' if where.startswith('FINDING_VIEWER_') else f'set "port" in {where}'
        return 'FAIL', 'server', f'port {port} is taken by another program: {fix}'
    return 'PASS', 'server', f'port {port} is free; the server starts on the next open'


def doctor() -> int:
    """Check the machine and the wiring; one line per check. Exit 1 on any FAIL."""
    failed = False

    def report(status: str, check: str, detail: str) -> None:
        nonlocal failed
        failed = failed or status == 'FAIL'
        print(f'{status:<4}  {check:<9} {detail}', flush=True)

    v = sys.version_info
    report('PASS' if v >= (3, 9) else 'FAIL', 'python',
           f'Python {v.major}.{v.minor}.{v.micro} ({sys.executable})' + ('' if v >= (3, 9) else ': needs 3.9 or newer'))
    ok = sys.platform == 'darwin' or sys.platform.startswith('linux')
    report('PASS' if ok else 'FAIL', 'platform', sys.platform + ('' if ok else ': needs macOS or Linux'))
    out = _run(['git', '--version'])
    report('PASS' if out else 'FAIL', 'git', out or 'git not found on PATH')
    report(*check_gh())
    report(*check_claude())
    report(*check_data())
    try:
        settings, _ = read_settings()
    except (OSError, ValueError) as exc:
        report('FAIL', 'settings', f'cannot read {settings_file()}: {exc}')
        settings = None
    plugin = in_plugin() or (settings is not None and plugin_enabled(settings))
    if settings is not None:
        report(*check_hooks(manual_hooks(settings), plugin))
    report(*check_command(plugin))
    values, sources, problems = config.load()
    where = sources['port'] if sources['port'].startswith('FINDING_VIEWER_') else str(paths.CONFIG)
    report(*check_server(values['port'], where=where))
    for line in problems:
        report('WARN', 'config', line)
    if not problems:
        report('PASS', 'config', str(paths.CONFIG) if paths.CONFIG.exists()
               else f'no {paths.CONFIG}: every key has its default')
    for key in config.DEFAULTS:
        print(f'{"":16}{key} = {json.dumps(values[key])}  ({sources[key]})')
    return 1 if failed else 0
