"""The SessionStart update notice: version comparison, the day's cache, the update_check switch, and silence on failure."""
import support  # noqa: F401  first: points the tool at a scratch environment before it is imported

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

import paths
import updates


def fetcher(value):
    calls = []

    def fetch():
        calls.append(1)
        return value
    fetch.calls = calls
    return fetch


class UpdatesTest(unittest.TestCase):
    def setUp(self):
        updates.CACHE.unlink(missing_ok=True)
        patch = mock.patch.object(updates, 'local_version', return_value='0.6.0')
        patch.start()
        self.addCleanup(patch.stop)

    def test_parse(self):
        self.assertEqual(updates.parse('0.10.2'), (0, 10, 2))
        self.assertEqual(updates.parse('v1.0'), (1, 0))
        self.assertIsNone(updates.parse('1.0-beta'))
        self.assertIsNone(updates.parse(None))

    def test_newer_version_gives_a_notice_naming_both_and_the_switch(self):
        text = updates.notice(fetcher('0.7.0'))
        self.assertIn('0.7.0', text)
        self.assertIn('0.6.0', text)
        self.assertIn('update_check', text)

    def test_same_older_or_unreadable_latest_gives_none(self):
        for latest in ('0.6.0', '0.5.9', None):
            updates.CACHE.unlink(missing_ok=True)
            self.assertIsNone(updates.notice(fetcher(latest)), latest)

    def test_the_answer_is_cached_for_a_day(self):
        fetch = fetcher('0.7.0')
        updates.notice(fetch)
        updates.notice(fetch)
        self.assertEqual(len(fetch.calls), 1)
        updates.latest_version(fetch, now=updates.time.time() + updates.CACHE_SECONDS + 5)
        self.assertEqual(len(fetch.calls), 2)

    def test_a_failed_lookup_is_not_retried_within_the_hour_and_keeps_the_last_answer(self):
        updates.latest_version(fetcher('0.7.0'), now=1000.0)
        failing = fetcher(None)
        self.assertEqual(updates.latest_version(failing, now=1000.0 + updates.CACHE_SECONDS + 1), '0.7.0')
        self.assertEqual(len(failing.calls), 1)
        updates.CACHE.unlink()
        updates.latest_version(failing, now=5000.0)
        updates.latest_version(failing, now=5000.0 + 60)
        self.assertEqual(len(failing.calls), 2)

    def test_the_setting_turns_it_off_without_a_lookup(self):
        fetch = fetcher('0.7.0')
        with mock.patch.dict(os.environ, {'FINDING_VIEWER_UPDATE_CHECK': '0'}):
            self.assertIsNone(updates.notice(fetch))
        self.assertEqual(fetch.calls, [])

    def test_the_hook_prints_a_system_message_only_when_there_is_a_notice(self):
        for text in ('update available', None):
            out = io.StringIO()
            with mock.patch.object(updates, 'notice', return_value=text), redirect_stdout(out):
                self.assertEqual(updates.cmd_session_start(), 0)
            self.assertEqual(out.getvalue(), json.dumps({'systemMessage': text}) + '\n' if text else '')

    def test_fetch_latest_is_none_on_a_network_error(self):
        with mock.patch.object(updates.urllib.request, 'urlopen', side_effect=OSError('offline')):
            self.assertIsNone(updates.fetch_latest())

    def test_plugin_json_has_a_version_and_the_hook_is_registered(self):
        data = json.loads((paths.CODE / '.claude-plugin' / 'plugin.json').read_text())
        self.assertTrue(updates.parse(data['version']))
        hooks = json.loads((paths.CODE / 'hooks' / 'hooks.json').read_text())['hooks']
        self.assertIn('finding_viewer.py\\" session', json.dumps(hooks['SessionStart']))


if __name__ == '__main__':
    unittest.main()
