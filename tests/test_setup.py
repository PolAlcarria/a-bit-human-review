"""doctor, install and uninstall: the hooks and /findings command in a fake home, each check, doctor end to end."""
import support  # noqa: F401  first: points the tool at a scratch environment before it is imported

import contextlib
import http.server
import io
import json
import os
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import setup_cmds
import triage

OTHER = '/usr/local/bin/other-stop'
QUOTED = shlex.quote(str(setup_cmds.SCRIPT))
OURS_HOOK = f'python3 {QUOTED} hook'
OURS_INJECT = f'python3 {QUOTED} inject'
OLD = {'model': 'opus', 'hooks': {'Stop': [
    {'hooks': [{'type': 'command', 'command': OTHER}]},
    {'hooks': [{'type': 'command', 'command': 'python3 ~/old/a-bit-human-review/finding_viewer.py hook',
                'timeout': 15}]},
]}}


def make_home() -> Path:
    """A fresh folder under the scratch root, to serve as HOME."""
    return Path(tempfile.mkdtemp(dir=support.SCRATCH))


def home_env(home: Path):
    """HOME at ``home``, with no plugin variables: Path.home() reads HOME on each call, so the block sees it."""
    return support.env(HOME=str(home), CLAUDE_PLUGIN_ROOT=None, CLAUDE_PLUGIN_DATA=None)


def printed(func, *args):
    """Run ``func(*args)``; returns (its result, what it printed)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = func(*args)
    return result, out.getvalue()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def hook_commands(settings: dict) -> list[str]:
    """Every command hook's command in a settings object, in file order."""
    return [entry['command'] for groups in settings.get('hooks', {}).values()
            for group in groups for entry in group['hooks']]


class InstallTest(unittest.TestCase):
    def test_install_replaces_the_old_stop_hook_and_keeps_the_other_one(self):
        home = make_home()
        write(home / '.claude' / 'settings.json', json.dumps(OLD, indent=2) + '\n')
        with home_env(home):
            code, _ = printed(setup_cmds.install)
            settings = json.loads((home / '.claude' / 'settings.json').read_text(encoding='utf-8'))
        self.assertEqual(code, 0)
        stop = settings['hooks']['Stop']
        self.assertEqual([g['hooks'][0]['command'] for g in stop], [OTHER, OURS_HOOK])
        self.assertEqual(stop[1]['hooks'], [{'type': 'command', 'command': OURS_HOOK, 'timeout': 15}])
        self.assertEqual(settings['hooks']['UserPromptSubmit'],
                         [{'hooks': [{'type': 'command', 'command': OURS_INJECT, 'timeout': 10}]}])
        self.assertEqual(settings['model'], 'opus')

    def test_install_copies_the_original_settings_to_a_backup_first(self):
        home = make_home()
        text = json.dumps(OLD, indent=2) + '\n'
        write(home / '.claude' / 'settings.json', text)
        with home_env(home):
            printed(setup_cmds.install)
        self.assertEqual((home / '.claude' / 'settings.json.bak').read_text(encoding='utf-8'), text)

    def test_install_writes_the_findings_command_for_this_copy(self):
        home = make_home()
        with home_env(home):
            printed(setup_cmds.install)
        text = (home / '.claude' / 'commands' / 'findings.md').read_text(encoding='utf-8')
        self.assertIn(str(setup_cmds.SCRIPT), text)
        self.assertIn(f'allowed-tools: Bash(python3 {QUOTED}:*)', text)
        self.assertNotIn('${CLAUDE_PLUGIN_ROOT}', text)
        self.assertNotIn('CLAUDE_PLUGIN_DATA', text)
        self.assertIn(f'!`python3 {QUOTED} $ARGUMENTS`', text)
        self.assertFalse(any(line.startswith('name:') for line in text.splitlines()))

    def test_install_with_no_settings_file_creates_it_with_both_hooks_and_no_backup(self):
        home = make_home()
        with home_env(home):
            code, _ = printed(setup_cmds.install)
            settings = json.loads((home / '.claude' / 'settings.json').read_text(encoding='utf-8'))
        self.assertEqual(code, 0)
        self.assertEqual(settings['hooks']['Stop'],
                         [{'hooks': [{'type': 'command', 'command': OURS_HOOK, 'timeout': 15}]}])
        self.assertEqual(settings['hooks']['UserPromptSubmit'],
                         [{'hooks': [{'type': 'command', 'command': OURS_INJECT, 'timeout': 10}]}])
        self.assertFalse((home / '.claude' / 'settings.json.bak').exists())

    def test_install_twice_changes_nothing_the_second_time(self):
        home = make_home()
        path = home / '.claude' / 'settings.json'
        write(path, json.dumps(OLD, indent=2) + '\n')
        with home_env(home):
            printed(setup_cmds.install)
            before = path.read_bytes()
            code, out = printed(setup_cmds.install)
        self.assertEqual(code, 0)
        self.assertIn('Nothing to change', out)
        self.assertEqual(path.read_bytes(), before)

    def test_install_refuses_inside_a_plugin(self):
        home = make_home()
        with support.env(HOME=str(home), CLAUDE_PLUGIN_ROOT='/x', CLAUDE_PLUGIN_DATA=None):
            code, out = printed(setup_cmds.install)
        self.assertEqual(code, 1)
        self.assertIn('plugin', out)
        self.assertEqual(os.listdir(home), [])

    def test_install_refuses_when_only_the_plugin_data_folder_is_set_and_writes_nothing(self):
        home = make_home()
        with support.env(HOME=str(home), CLAUDE_PLUGIN_ROOT=None, CLAUDE_PLUGIN_DATA='/x'):
            code, out = printed(setup_cmds.install)
        self.assertEqual(code, 1)
        self.assertIn('plugin', out)
        self.assertEqual(os.listdir(home), [])

    def test_install_leaves_a_foreign_findings_command_alone_and_fails(self):
        home = make_home()
        cmd = home / '.claude' / 'commands' / 'findings.md'
        write(cmd, 'my own command')
        with home_env(home):
            code, out = printed(setup_cmds.install)
        self.assertEqual(code, 1)
        self.assertIn('Left alone', out)
        self.assertEqual(cmd.read_text(encoding='utf-8'), 'my own command')

    def test_install_refuses_settings_whose_hooks_are_not_an_object_and_changes_nothing(self):
        home = make_home()
        path = home / '.claude' / 'settings.json'
        write(path, '{"hooks": []}\n')
        with home_env(home):
            code, out = printed(setup_cmds.install)
        self.assertEqual(code, 1)
        self.assertIn('Nothing changed', out)
        self.assertEqual(path.read_text(encoding='utf-8'), '{"hooks": []}\n')

    def test_install_keeps_the_mode_of_settings_and_its_backup(self):
        home = make_home()
        path = home / '.claude' / 'settings.json'
        write(path, json.dumps(OLD, indent=2) + '\n')
        os.chmod(path, 0o600)
        with home_env(home):
            printed(setup_cmds.install)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.with_name('settings.json.bak').stat().st_mode), 0o600)
            printed(setup_cmds.uninstall)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_install_puts_the_other_hook_of_our_old_group_back_in_its_place(self):
        home = make_home()
        settings = {'hooks': {'Stop': [{'hooks': [
            {'type': 'command', 'command': 'python3 ~/old/finding_viewer.py hook', 'timeout': 15},
            {'type': 'command', 'command': OTHER},
        ]}]}}
        path = home / '.claude' / 'settings.json'
        write(path, json.dumps(settings, indent=2) + '\n')
        with home_env(home):
            code, _ = printed(setup_cmds.install)
            after = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(code, 0)
        self.assertEqual(after['hooks']['Stop'], [
            {'hooks': [{'type': 'command', 'command': OTHER}]},
            {'hooks': [{'type': 'command', 'command': OURS_HOOK, 'timeout': 15}]},
        ])

    def test_install_on_a_symlinked_settings_keeps_the_link_and_writes_the_target(self):
        home = make_home()
        target = home / 'dotfiles' / 'settings.json'
        write(target, json.dumps(OLD, indent=2) + '\n')
        link = home / '.claude' / 'settings.json'
        link.parent.mkdir(parents=True)
        os.symlink(target, link)
        with home_env(home):
            code, _ = printed(setup_cmds.install)
        self.assertEqual(code, 0)
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), str(target))
        commands = hook_commands(json.loads(target.read_text(encoding='utf-8')))
        self.assertIn(OURS_HOOK, commands)
        self.assertIn(OURS_INJECT, commands)
        self.assertTrue(target.with_name('settings.json.bak').exists())

    def test_a_script_path_with_a_space_is_quoted_and_the_next_run_recognises_it(self):
        spaced = make_home() / 'My Tools' / 'finding_viewer.py'
        write(spaced, '')
        quoted = shlex.quote(str(spaced))
        home = make_home()
        with home_env(home), mock.patch.object(setup_cmds, 'SCRIPT', spaced):
            code, _ = printed(setup_cmds.install)
            settings = json.loads((home / '.claude' / 'settings.json').read_text(encoding='utf-8'))
            text = (home / '.claude' / 'commands' / 'findings.md').read_text(encoding='utf-8')
            status = setup_cmds.check_hooks(setup_cmds.manual_hooks(settings), False)[0]
            again, out = printed(setup_cmds.install)
        self.assertEqual(code, 0)
        command = settings['hooks']['Stop'][0]['hooks'][0]['command']
        self.assertEqual(command, f'python3 {quoted} hook')
        self.assertEqual(shlex.split(command), ['python3', str(spaced), 'hook'])
        self.assertIn(f'allowed-tools: Bash(python3 {quoted}:*)', text)
        self.assertIn(f'!`python3 {quoted} $ARGUMENTS`', text)
        self.assertEqual(status, 'PASS')
        self.assertEqual(again, 0)
        self.assertIn('Nothing to change', out)

    def test_a_script_path_with_an_apostrophe_passes_every_check_and_the_next_run_recognises_it(self):
        spaced = make_home() / "pol's code" / 'finding_viewer.py'
        write(spaced, '')
        home = make_home()
        with home_env(home), mock.patch.object(setup_cmds, 'SCRIPT', spaced):
            code, _ = printed(setup_cmds.install)
            settings = json.loads((home / '.claude' / 'settings.json').read_text(encoding='utf-8'))
            hooks_status = setup_cmds.check_hooks(setup_cmds.manual_hooks(settings), False)[0]
            command_status = setup_cmds.check_command(False)[0]
            again, out = printed(setup_cmds.install)
        self.assertEqual(code, 0)
        self.assertEqual(shlex.split(settings['hooks']['Stop'][0]['hooks'][0]['command']),
                         ['python3', str(spaced), 'hook'])
        self.assertEqual(hooks_status, 'PASS')
        self.assertEqual(command_status, 'PASS')
        self.assertEqual(again, 0)
        self.assertIn('Nothing to change', out)

    def test_a_stale_settings_temp_file_from_an_interrupted_run_does_not_stop_install(self):
        home = make_home()
        path = home / '.claude' / 'settings.json'
        tmp = home / '.claude' / 'settings.json.tmp'
        write(path, json.dumps(OLD, indent=2) + '\n')
        write(tmp, 'half written')
        os.chmod(tmp, 0o444)
        with home_env(home):
            code, _ = printed(setup_cmds.install)
            settings = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(code, 0)
        self.assertIn(OURS_HOOK, hook_commands(settings))
        self.assertIn(OURS_INJECT, hook_commands(settings))
        self.assertFalse(tmp.exists())

    def test_a_directory_where_the_settings_temp_file_goes_fails_cleanly_and_changes_nothing(self):
        home = make_home()
        path = home / '.claude' / 'settings.json'
        write(path, json.dumps(OLD, indent=2) + '\n')
        before = path.read_bytes()
        (home / '.claude' / 'settings.json.tmp').mkdir()
        with home_env(home):
            code, out = printed(setup_cmds.install)
        self.assertEqual(code, 1)
        self.assertIn('Cannot write', out)
        self.assertIn('unchanged', out)
        self.assertEqual(path.read_bytes(), before)

    def test_a_command_folder_that_is_a_file_reports_the_failure_after_the_hooks_were_written(self):
        home = make_home()
        write(home / '.claude' / 'commands', 'not a folder')
        with home_env(home):
            code, out = printed(setup_cmds.install)
            settings = json.loads((home / '.claude' / 'settings.json').read_text(encoding='utf-8'))
        self.assertEqual(code, 1)
        self.assertIn('Cannot write', out)
        self.assertIn('hook changes above were made', out)
        self.assertIn(OURS_HOOK, hook_commands(settings))


class UninstallTest(unittest.TestCase):
    def test_uninstall_after_install_removes_our_hooks_and_the_command(self):
        home = make_home()
        path = home / '.claude' / 'settings.json'
        write(path, json.dumps(OLD, indent=2) + '\n')
        with home_env(home):
            printed(setup_cmds.install)
            code, _ = printed(setup_cmds.uninstall)
            settings = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(code, 0)
        self.assertEqual(hook_commands(settings), [OTHER])
        self.assertFalse(any('finding_viewer.py' in c for c in hook_commands(settings)))
        self.assertFalse((home / '.claude' / 'commands' / 'findings.md').exists())

    def test_uninstall_twice_prints_nothing_to_remove_and_leaves_settings_alone(self):
        home = make_home()
        path = home / '.claude' / 'settings.json'
        write(path, json.dumps(OLD, indent=2) + '\n')
        with home_env(home):
            printed(setup_cmds.install)
            printed(setup_cmds.uninstall)
            before = path.read_bytes()
            code, out = printed(setup_cmds.uninstall)
        self.assertEqual(code, 0)
        self.assertIn('Nothing to remove', out)
        self.assertEqual(path.read_bytes(), before)

    def test_uninstall_with_nothing_installed_creates_nothing(self):
        home = make_home()
        with home_env(home):
            code, out = printed(setup_cmds.uninstall)
        self.assertEqual(code, 0)
        self.assertEqual(os.listdir(home), [])
        self.assertIn('Nothing to remove', out)

    def test_uninstall_leaves_a_foreign_findings_command_alone(self):
        home = make_home()
        cmd = home / '.claude' / 'commands' / 'findings.md'
        write(cmd, 'my own command')
        with home_env(home):
            code, out = printed(setup_cmds.uninstall)
        self.assertEqual(code, 0)
        self.assertIn('Left alone', out)
        self.assertEqual(cmd.read_text(encoding='utf-8'), 'my own command')

    def test_uninstall_removes_two_of_our_hooks_from_one_group_and_keeps_the_rest(self):
        home = make_home()
        settings = {'hooks': {'Stop': [{'hooks': [
            {'type': 'command', 'command': OTHER},
            {'type': 'command', 'command': OURS_HOOK, 'timeout': 15},
            {'type': 'command', 'command': '/old/finding_viewer.py hook', 'timeout': 15},
        ]}]}}
        path = home / '.claude' / 'settings.json'
        write(path, json.dumps(settings, indent=2) + '\n')
        with home_env(home):
            code, _ = printed(setup_cmds.uninstall)
            after = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(code, 0)
        self.assertEqual(after['hooks'], {'Stop': [{'hooks': [{'type': 'command', 'command': OTHER}]}]})

    def test_a_directory_where_the_settings_temp_file_goes_fails_cleanly_on_uninstall(self):
        home = make_home()
        path = home / '.claude' / 'settings.json'
        write(path, json.dumps(OLD, indent=2) + '\n')
        with home_env(home):
            printed(setup_cmds.install)
            before = path.read_bytes()
            (home / '.claude' / 'settings.json.tmp').mkdir()
            code, out = printed(setup_cmds.uninstall)
        self.assertEqual(code, 1)
        self.assertIn('Cannot write', out)
        self.assertEqual(path.read_bytes(), before)


class CheckHooksTest(unittest.TestCase):
    def test_plugin_and_manual_hooks_together_warn(self):
        manual = {'hook': [OURS_HOOK], 'inject': [OURS_INJECT]}
        self.assertEqual(setup_cmds.check_hooks(manual, True)[:2], ('WARN', 'hooks'))

    def test_plugin_only_passes(self):
        self.assertEqual(setup_cmds.check_hooks({'hook': [], 'inject': []}, True)[0], 'PASS')

    def test_nothing_registered_fails(self):
        self.assertEqual(setup_cmds.check_hooks({'hook': [], 'inject': []}, False)[0], 'FAIL')

    def test_only_the_stop_hook_fails_and_names_the_missing_one(self):
        status, _, detail = setup_cmds.check_hooks({'hook': [OURS_HOOK], 'inject': []}, False)
        self.assertEqual(status, 'FAIL')
        self.assertIn('UserPromptSubmit', detail)

    def test_both_hooks_of_this_copy_pass(self):
        manual = {'hook': [OURS_HOOK], 'inject': [OURS_INJECT]}
        self.assertEqual(setup_cmds.check_hooks(manual, False)[:2], ('PASS', 'hooks'))

    def test_a_hook_of_a_missing_script_fails(self):
        manual = {'hook': ['python3 /nonexistent/finding_viewer.py hook'], 'inject': [OURS_INJECT]}
        self.assertEqual(setup_cmds.check_hooks(manual, False)[0], 'FAIL')

    def test_a_hook_of_another_existing_copy_warns(self):
        other = make_home() / 'old' / 'finding_viewer.py'
        write(other, '')
        manual = {'hook': [f'python3 {other} hook'], 'inject': [OURS_INJECT]}
        self.assertEqual(setup_cmds.check_hooks(manual, False)[0], 'WARN')


class ScriptOfTest(unittest.TestCase):
    def test_a_quoted_path_with_an_apostrophe_comes_back_whole(self):
        path = make_home() / "pol's code" / 'finding_viewer.py'
        self.assertEqual(setup_cmds.script_of(f'python3 {shlex.quote(str(path))} hook'), path)

    def test_an_unknown_user_home_returns_the_path_as_written_and_does_not_raise(self):
        self.assertEqual(setup_cmds.script_of('python3 ~nosuchuser/x/finding_viewer.py hook'),
                         Path('~nosuchuser/x/finding_viewer.py'))


class CheckCommandTest(unittest.TestCase):
    def test_no_findings_command_fails(self):
        with home_env(make_home()):
            self.assertEqual(setup_cmds.check_command(False)[0], 'FAIL')

    def test_installed_findings_command_passes(self):
        with home_env(make_home()):
            printed(setup_cmds.install)
            self.assertEqual(setup_cmds.check_command(False)[:2], ('PASS', 'command'))

    def test_another_findings_command_fails(self):
        home = make_home()
        write(home / '.claude' / 'commands' / 'findings.md', 'my own command')
        with home_env(home):
            status, _, detail = setup_cmds.check_command(False)
        self.assertEqual(status, 'FAIL')
        self.assertIn('another command', detail)


class PingHandler(http.server.BaseHTTPRequestHandler):
    """Answers /api/ping the way the review server does, with the version the test set on the server."""

    def do_GET(self):  # noqa: N802 — the name http.server calls
        body = json.dumps({'app': 'finding-viewer', 'version': self.server.version, 'pid': 4242}).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the test output quiet
        pass


class CheckServerTest(unittest.TestCase):
    def test_a_free_port_passes(self):
        status, check, detail = setup_cmds.check_server(free_port(), timeout=0.3)
        self.assertEqual((status, check), ('PASS', 'server'))
        self.assertIn('free', detail)

    def test_a_port_held_by_a_silent_listener_fails(self):
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            s.listen()
            status, _, detail = setup_cmds.check_server(s.getsockname()[1], timeout=0.3)
        self.assertEqual(status, 'FAIL')
        self.assertIn('taken', detail)

    def test_a_port_set_by_a_variable_is_fixed_by_changing_the_variable(self):
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            s.listen()
            status, _, detail = setup_cmds.check_server(s.getsockname()[1], timeout=0.3, where='FINDING_VIEWER_PORT')
        self.assertEqual(status, 'FAIL')
        self.assertIn('change FINDING_VIEWER_PORT', detail)

    def test_a_port_held_by_a_program_that_is_not_http_fails_without_raising(self):
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            s.listen()

            def greet():
                conn, _ = s.accept()
                with conn:
                    conn.recv(4096)  # the request, so the close is a clean end and not a reset
                    conn.sendall(b'SSH-2.0-OpenSSH_9.6\r\n')

            worker = threading.Thread(target=greet, daemon=True)
            worker.start()
            status, _, detail = setup_cmds.check_server(s.getsockname()[1], timeout=1)
            worker.join(5)
        self.assertEqual(status, 'FAIL')
        self.assertIn('taken', detail)

    def serve_ping(self, version: int) -> int:
        """A ping server answering with ``version``; returns its port. It stops when the test ends."""
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), PingHandler)
        server.version = version
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_a_review_server_of_this_version_passes(self):
        status, _, detail = setup_cmds.check_server(self.serve_ping(triage.SERVER_VERSION))
        self.assertEqual(status, 'PASS')
        self.assertIn(f'version {triage.SERVER_VERSION}', detail)

    def test_a_review_server_of_another_version_warns_that_the_next_open_restarts_it(self):
        status, _, detail = setup_cmds.check_server(self.serve_ping(22))
        self.assertEqual(status, 'WARN')
        self.assertIn('restarts', detail)


class CheckGhTest(unittest.TestCase):
    def test_a_logged_in_gh_passes_and_names_the_account(self):
        status, check, detail = setup_cmds.check_gh()
        self.assertEqual((status, check), ('PASS', 'gh'))
        self.assertIn('stub-reviewer', detail)

    def test_a_gh_that_is_not_logged_in_fails(self):
        with support.env(STUB_GH_AUTH='0'):
            status, check, detail = setup_cmds.check_gh()
        self.assertEqual((status, check), ('FAIL', 'gh'))
        self.assertIn('gh auth login', detail)


class PluginTest(unittest.TestCase):
    def test_the_plugin_data_variable_alone_makes_this_a_plugin(self):
        with support.env(CLAUDE_PLUGIN_ROOT=None, CLAUDE_PLUGIN_DATA='/x'):
            self.assertTrue(setup_cmds.in_plugin())

    def test_without_either_plugin_variable_this_is_not_a_plugin(self):
        with support.env(CLAUDE_PLUGIN_ROOT=None, CLAUDE_PLUGIN_DATA=None):
            self.assertFalse(setup_cmds.in_plugin())

    def test_an_enabled_plugin_from_any_marketplace_counts(self):
        self.assertTrue(setup_cmds.plugin_enabled({'enabledPlugins': {'a-bit-human-review@x': True}}))

    def test_a_disabled_plugin_or_another_plugin_does_not_count(self):
        self.assertFalse(setup_cmds.plugin_enabled({'enabledPlugins': {'a-bit-human-review@x': False}}))
        self.assertFalse(setup_cmds.plugin_enabled({'enabledPlugins': {'other-plugin@x': True}}))


class DoctorUnreadableSettingsTest(unittest.TestCase):
    def test_unreadable_settings_gets_one_settings_line_and_no_hooks_line(self):
        home = make_home()
        write(home / '.claude' / 'settings.json', '{"hooks": ')
        with home_env(home):
            code, out = printed(setup_cmds.doctor)
        self.assertEqual(code, 1)
        checks = [line.split()[1] for line in out.splitlines() if line and not line.startswith(' ')
                  and len(line.split()) > 1]
        self.assertIn('settings', checks)
        self.assertNotIn('hooks', checks)
        self.assertIn('command', checks)


class DoctorEndToEndTest(unittest.TestCase):
    """doctor runs in a child process, as the user runs it: the stubs answer gh and claude, git is the real one."""

    def run_tool(self, sub: str, env: dict) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(support.ROOT / 'finding_viewer.py'), sub], env=env,
                              cwd=str(support.ROOT), capture_output=True, text=True, timeout=120)

    def test_doctor_fails_without_the_wiring_then_passes_after_install(self):
        home = make_home()
        (home / '.claude' / 'projects').mkdir(parents=True)
        (home / '.claude' / 'sessions').mkdir()
        data = make_home()
        (data / 'out').mkdir()
        env = {**os.environ, 'HOME': str(home), 'FINDING_VIEWER_DATA': str(data),
               'FINDING_VIEWER_PORT': str(free_port())}
        env.pop('CLAUDE_PLUGIN_ROOT', None)
        env.pop('CLAUDE_PLUGIN_DATA', None)
        env.pop('FINDING_VIEWER_CHILD', None)

        first = self.run_tool('doctor', env)
        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
        self.assertTrue(any(line.startswith('FAIL') and 'hooks' in line for line in first.stdout.splitlines()),
                        first.stdout)
        self.assertIn('port = ', first.stdout)

        installed = self.run_tool('install', env)
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)

        second = self.run_tool('doctor', env)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        for line in second.stdout.splitlines():
            if line and not line.startswith(' '):
                self.assertTrue(line.startswith(('PASS', 'WARN', 'FAIL')), line)
        self.assertFalse([line for line in second.stdout.splitlines() if line.startswith('FAIL')], second.stdout)


if __name__ == '__main__':
    unittest.main()
