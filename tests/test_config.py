"""The settings file and its environment overrides (among them the auto_open switch), and the hard-coded defaults the
config keys replace: the report glob, deleting forks, and the default base branch."""
import support  # noqa: F401  first: points the tool at a scratch environment before it is imported

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import config
import hook
import render
import sessions
import store
import triage


def write_config(text: str) -> Path:
    """A config.json with ``text`` in a fresh folder under the scratch root; returns its path."""
    path = Path(tempfile.mkdtemp(dir=support.SCRATCH)) / 'config.json'
    path.write_text(text, encoding='utf-8')
    return path


def missing_config() -> Path:
    """A path with no file behind it, in a fresh folder under the scratch root."""
    return Path(tempfile.mkdtemp(dir=support.SCRATCH)) / 'config.json'


class ConfigLoadTest(unittest.TestCase):
    def test_no_file_and_no_variables_gives_the_defaults(self):
        values, sources, problems = config.load(missing_config(), {})
        self.assertEqual(values, config.DEFAULTS)
        self.assertEqual(set(sources.values()), {'default'})
        self.assertEqual(problems, [])

    def test_file_value_overrides_the_default(self):
        path = write_config('{"port": 5555}')
        values, sources, _ = config.load(path, {})
        self.assertEqual(values['port'], 5555)
        self.assertEqual(sources['port'], str(path))

    def test_variable_overrides_the_file(self):
        path = write_config('{"port": 5555}')
        values, sources, _ = config.load(path, {'FINDING_VIEWER_PORT': '1234'})
        self.assertEqual(values['port'], 1234)
        self.assertEqual(sources['port'], 'FINDING_VIEWER_PORT')

    def test_empty_variable_does_not_override(self):
        path = write_config('{"port": 5555}')
        values, _, _ = config.load(path, {'FINDING_VIEWER_PORT': ''})
        self.assertEqual(values['port'], 5555)

    def test_nested_keys_are_read(self):
        path = write_config('{"extract": {"enabled": false, "model": "haiku"}}')
        values, _, problems = config.load(path, {})
        self.assertIs(values['extract.enabled'], False)
        self.assertEqual(values['extract.model'], 'haiku')
        self.assertEqual(problems, [])

    def test_extract_variable_zero_turns_extraction_off(self):
        values, _, _ = config.load(missing_config(), {'FINDING_VIEWER_EXTRACT': '0'})
        self.assertIs(values['extract.enabled'], False)

    def test_extract_variable_one_turns_extraction_on(self):
        values, _, _ = config.load(missing_config(), {'FINDING_VIEWER_EXTRACT': '1'})
        self.assertIs(values['extract.enabled'], True)

    def test_repo_dirs_variable_splits_on_the_path_separator(self):
        values, _, _ = config.load(missing_config(), {'FINDING_VIEWER_REPO_DIRS': '/a' + os.pathsep + '/b'})
        self.assertEqual(values['repo_dirs'], ['/a', '/b'])

    def test_repo_dirs_defaults_to_no_folders(self):
        values, sources, _ = config.load(missing_config(), {})
        self.assertEqual((values['repo_dirs'], sources['repo_dirs']), ([], 'default'))

    def test_repo_dirs_from_the_file(self):
        path = write_config(json.dumps({'repo_dirs': ['~/Repos']}))
        values, sources, problems = config.load(path, {})
        self.assertEqual((values['repo_dirs'], problems), (['~/Repos'], []))
        self.assertEqual(sources['repo_dirs'], str(path))

    def test_invalid_json_gives_the_defaults_and_one_problem(self):
        values, _, problems = config.load(write_config('{"port": '), {})
        self.assertEqual(values, config.DEFAULTS)
        self.assertEqual(len(problems), 1)

    def test_wrong_type_and_unknown_key_are_one_problem_each(self):
        values, _, problems = config.load(write_config('{"port": "x", "nope": 1}'), {})
        self.assertEqual(values['port'], config.DEFAULTS['port'])
        self.assertEqual(len(problems), 2)

    def test_bad_port_variable_is_one_problem_naming_the_variable(self):
        values, _, problems = config.load(missing_config(), {'FINDING_VIEWER_PORT': 'abc'})
        self.assertEqual(values['port'], config.DEFAULTS['port'])
        self.assertEqual(len(problems), 1)
        self.assertIn('FINDING_VIEWER_PORT', problems[0])

    def test_report_glob_from_the_file_must_be_relative(self):
        for glob in ('/x/*.md', '~/x'):
            with self.subTest(glob=glob):
                values, _, problems = config.load(write_config(json.dumps({'report_glob': glob})), {})
                self.assertEqual(values['report_glob'], config.DEFAULTS['report_glob'])
                self.assertEqual(len(problems), 1)

    def test_extract_variable_false_turns_extraction_off(self):
        values, _, _ = config.load(missing_config(), {'FINDING_VIEWER_EXTRACT': 'false'})
        self.assertIs(values['extract.enabled'], False)

    def test_port_zero_and_65536_are_rejected(self):
        for port in (0, 65536):
            with self.subTest(port=port):
                values, _, problems = config.load(write_config(json.dumps({'port': port})), {})
                self.assertEqual(values['port'], config.DEFAULTS['port'])
                self.assertEqual(len(problems), 1)

    def test_json_array_gives_the_defaults_and_one_problem(self):
        values, _, problems = config.load(write_config('[1, 2]'), {})
        self.assertEqual(values, config.DEFAULTS)
        self.assertEqual(len(problems), 1)

    def test_file_nested_100000_levels_deep_gives_the_defaults_and_one_problem(self):
        values, _, problems = config.load(write_config('[' * 100000), {})
        self.assertEqual(values, config.DEFAULTS)
        self.assertEqual(len(problems), 1)

    def test_delete_forks_variable_zero_and_no_turn_it_off(self):
        for text in ('0', 'no'):
            with self.subTest(text=text):
                values, _, _ = config.load(missing_config(), {'FINDING_VIEWER_DELETE_FORKS': text})
                self.assertIs(values['delete_forks'], False)

    def test_delete_forks_variable_one_turns_it_on(self):
        values, _, _ = config.load(missing_config(), {'FINDING_VIEWER_DELETE_FORKS': '1'})
        self.assertIs(values['delete_forks'], True)

    def test_auto_open_variable_zero_and_no_turn_it_off(self):
        for text in ('0', 'no'):
            with self.subTest(text=text):
                values, _, _ = config.load(missing_config(), {'FINDING_VIEWER_AUTO_OPEN': text})
                self.assertIs(values['auto_open'], False)

    def test_auto_open_variable_one_turns_it_on(self):
        values, _, _ = config.load(missing_config(), {'FINDING_VIEWER_AUTO_OPEN': '1'})
        self.assertIs(values['auto_open'], True)

    def test_auto_open_from_the_file(self):
        path = write_config('{"auto_open": false}')
        values, sources, problems = config.load(path, {})
        self.assertIs(values['auto_open'], False)
        self.assertEqual((sources['auto_open'], problems), (str(path), []))

    def test_auto_open_of_the_wrong_type_is_one_problem(self):
        values, _, problems = config.load(write_config('{"auto_open": "yes"}'), {})
        self.assertIs(values['auto_open'], True)
        self.assertEqual(len(problems), 1)
        self.assertIn('auto_open', problems[0])


class NonHttpPortTest(unittest.TestCase):
    """Something that is not the viewer holds the port: the ping reads as no server and does not raise."""

    def test_a_listener_that_speaks_ssh_reads_as_no_server(self):
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)

        def serve() -> None:
            conn, _ = listener.accept()
            with conn:
                conn.sendall(b'SSH-2.0-OpenSSH_9.6\r\n')

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        origin = f'http://127.0.0.1:{listener.getsockname()[1]}'
        with mock.patch.object(triage, 'ORIGIN', origin):
            self.assertIsNone(triage._call('GET', '/api/ping', timeout=2))


class PortOverrideTest(unittest.TestCase):
    """The port is read when triage is imported, so both cases run in a child process that imports it."""

    def port_in_child(self, with_variable: bool) -> str:
        folder = Path(tempfile.mkdtemp(dir=support.SCRATCH))
        (folder / 'out').mkdir()
        (folder / 'config.json').write_text('{"port": 5555}', encoding='utf-8')
        env = {**os.environ, 'FINDING_VIEWER_DATA': str(folder)}
        if with_variable:
            env['FINDING_VIEWER_PORT'] = '1234'
        else:
            env.pop('FINDING_VIEWER_PORT', None)
        r = subprocess.run([sys.executable, '-c', 'import triage; print(triage.PORT)'], cwd=support.ROOT, env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_variable_port_wins_over_the_file(self):
        self.assertEqual(self.port_in_child(with_variable=True), '1234')

    def test_file_port_is_used_without_the_variable(self):
        self.assertEqual(self.port_in_child(with_variable=False), '5555')


class ReportGlobTest(unittest.TestCase):
    def test_default_glob_finds_an_absolute_report_path_in_backticks(self):
        regex = hook.report_path_re('.claude/.history/code-reviews/*/*.md')
        text = 'Saved to `/Users/x/repo/.claude/.history/code-reviews/2026-10-01/pr-12.md`.'
        self.assertEqual(regex.search(text).group(1), '/Users/x/repo/.claude/.history/code-reviews/2026-10-01/pr-12.md')

    def test_default_glob_finds_a_relative_report_path(self):
        regex = hook.report_path_re('.claude/.history/code-reviews/*/*.md')
        found = regex.search('see .claude/.history/code-reviews/a/b.md now')
        self.assertEqual(found.group(1), '.claude/.history/code-reviews/a/b.md')

    def test_default_glob_needs_a_folder_level_so_a_flat_report_is_not_found(self):
        regex = hook.report_path_re('.claude/.history/code-reviews/*/*.md')
        self.assertIsNone(regex.search('see .claude/.history/code-reviews/pr-12.md'))

    def test_default_glob_finds_a_path_inside_a_markdown_link(self):
        regex = hook.report_path_re('.claude/.history/code-reviews/*/*.md')
        text = 'see [report](/Users/x/r/.claude/.history/code-reviews/d/pr.md)'
        self.assertEqual(regex.search(text).group(1), '/Users/x/r/.claude/.history/code-reviews/d/pr.md')

    def test_default_glob_stops_at_a_colon_or_comma_after_the_path(self):
        regex = hook.report_path_re('.claude/.history/code-reviews/*/*.md')
        self.assertEqual(regex.search('saved .claude/.history/code-reviews/d/pr.md: done').group(1),
                         '.claude/.history/code-reviews/d/pr.md')
        self.assertEqual(regex.search('saved .claude/.history/code-reviews/d/pr.md, done').group(1),
                         '.claude/.history/code-reviews/d/pr.md')

    def test_double_star_glob_matches_zero_or_more_folders(self):
        regex = hook.report_path_re('reviews/**/*.md')
        self.assertEqual(regex.search('see reviews/a.md').group(1), 'reviews/a.md')
        self.assertEqual(regex.search('see reviews/x/y/a.md').group(1), 'reviews/x/y/a.md')

    def test_repeated_double_star_folders_match_like_one(self):
        regex = hook.report_path_re('reviews/**/**/*.md')
        self.assertEqual(regex.search('see reviews/x/y/a.md').group(1), 'reviews/x/y/a.md')

    def test_default_glob_keeps_a_home_folder_prefix(self):
        regex = hook.report_path_re('.claude/.history/code-reviews/*/*.md')
        text = 'see ~/Repos/x/.claude/.history/code-reviews/d/p.md'
        self.assertEqual(regex.search(text).group(1), '~/Repos/x/.claude/.history/code-reviews/d/p.md')

    def test_custom_glob_finds_its_own_paths_only(self):
        regex = hook.report_path_re('reviews/*.md')
        self.assertEqual(regex.search('wrote reviews/pr-3.md.').group(1), 'reviews/pr-3.md')
        self.assertIsNone(regex.search('wrote notes/pr-3.md'))

    def test_latest_report_is_the_newest_file_matching_a_custom_glob(self):
        folder = Path(tempfile.mkdtemp(dir=support.SCRATCH))
        (folder / 'reviews').mkdir()
        old, new = folder / 'reviews' / 'old.md', folder / 'reviews' / 'new.md'
        old.write_text('old', encoding='utf-8')
        new.write_text('new', encoding='utf-8')
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))
        with support.env(FINDING_VIEWER_REPORT_GLOB='reviews/*.md'):
            self.assertEqual(render.latest_report(str(folder)), new)

    def test_default_glob_finds_no_report_outside_the_history_folder(self):
        folder = Path(tempfile.mkdtemp(dir=support.SCRATCH))
        (folder / 'reviews').mkdir()
        (folder / 'reviews' / 'new.md').write_text('new', encoding='utf-8')
        with support.env(FINDING_VIEWER_REPORT_GLOB=None):
            self.assertIsNone(render.latest_report(str(folder)))


class ReportPathSpeedTest(unittest.TestCase):
    """The search runs in the Stop hook, which gives up after 15 s, so a long token must not make it backtrack."""
    GLOBS = ('.claude/.history/code-reviews/*/*.md', '**/*.md', '*.md', 'reviews/**/*.md', '**/reviews/**/*.md')
    TEXTS = {
        'repeated folder': '/.claude/.history/code-reviews/a/' * 125,
        'repeated name': '/a' * 2000,
        'base64-like blob': 'Ab0+/' * 6400,
        'one long name': 'a' * 32000,
        'slashes': '/' * 32000,
    }

    def test_each_glob_searches_each_long_text_in_under_a_second(self):
        for glob in self.GLOBS:
            regex = hook.report_path_re(glob)
            for label, text in self.TEXTS.items():
                with self.subTest(glob=glob, text=label):
                    start = time.monotonic()
                    regex.search(text)
                    self.assertLess(time.monotonic() - start, 1.0)


class DeleteForksTest(unittest.TestCase):
    SID = 'fork-0001-aaaa'  # no other test uses this session id

    def transcript(self) -> Path:
        path = support.HOME / '.claude' / 'projects' / '-t' / f'{self.SID}.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}\n', encoding='utf-8')
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return path

    def test_delete_forks_off_keeps_the_transcript(self):
        path = self.transcript()
        with support.env(FINDING_VIEWER_DELETE_FORKS='0'):
            sessions.delete_session(self.SID)
        self.assertTrue(path.exists())

    def test_delete_forks_on_removes_the_transcript(self):
        path = self.transcript()
        with support.env(FINDING_VIEWER_DELETE_FORKS=None):
            sessions.delete_session(self.SID)
        self.assertFalse(path.exists())


class DefaultBaseTest(unittest.TestCase):
    def repo(self) -> Path:
        """A repository whose branches are master and trunk, so without a setting the default base is master."""
        root = Path(tempfile.mkdtemp(dir=support.SCRATCH)) / 'repo'
        root.mkdir()
        for args in (['init', '-q'],
                     ['-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'init'],
                     ['branch', 'trunk'],
                     ['branch', '-M', 'master']):
            subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)
        return root

    def test_without_a_setting_the_first_known_branch_is_the_base(self):
        with support.env(FINDING_VIEWER_DEFAULT_BASE=None):
            self.assertEqual(store.default_base(self.repo()), 'master')

    def test_a_configured_branch_that_exists_is_the_base(self):
        with support.env(FINDING_VIEWER_DEFAULT_BASE='trunk'):
            self.assertEqual(store.default_base(self.repo()), 'trunk')

    def test_a_configured_branch_that_is_missing_falls_back(self):
        with support.env(FINDING_VIEWER_DEFAULT_BASE='nope'):
            self.assertEqual(store.default_base(self.repo()), 'master')


if __name__ == '__main__':
    unittest.main()
