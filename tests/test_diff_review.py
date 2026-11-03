import support  # noqa: F401  first: points the tool at a scratch environment before it is imported

import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import chat
import finding_viewer
import handoff
import render
import reviews
import rereview
import store

GIT = ['git', '-c', 'user.name=t', '-c', 'user.email=t@t']


def git(repo, *args):
    return subprocess.run([*GIT, '-C', str(repo), *args], capture_output=True, text=True, check=True).stdout


def make_repo(name):
    """A repository with a main branch and a feature branch that modifies, deletes and adds a file (all committed)."""
    repo = support.SCRATCH / name
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'main')
    (repo / 'app.py').write_text('def a():\n    return 1\n\n\ndef b():\n    return 2\n')
    (repo / 'gone.txt').write_text('old\nline\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'base')
    git(repo, 'checkout', '-qb', 'feature')
    (repo / 'app.py').write_text('def a():\n    return 1\n\n\ndef b():\n    x = 3\n    return x\n\n\ndef c():\n    return 4\n')
    git(repo, 'rm', '-q', 'gone.txt')
    (repo / 'new.md').write_text('new file\nwith two lines\n')
    git(repo, 'add', '-A')
    git(repo, 'commit', '-qm', 'work')
    return repo


def build(repo, **spec):
    return render.build_page({'kind': 'diff', 'cwd': str(repo), **spec})


class BuildDiffPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = make_repo('diff-committed')
        cls.out, cls.data = build(cls.repo, session_id='sess-1')

    def test_the_page_is_a_diff_review_without_findings(self):
        d = self.data
        self.assertEqual((d['kind'], d['findings'], d['pr'], d['session_id']), ('diff', [], None, 'sess-1'))
        self.assertEqual(d['base'], 'main')
        self.assertIn('feature against main', d['subtitle'])
        self.assertEqual(self.out.name, 'page.html')

    def test_a_clean_branch_is_pinned_to_head(self):
        head = git(self.repo, 'rev-parse', 'HEAD').strip()
        self.assertEqual((self.data['ref'], self.data['blob_ref']), (head, head))
        self.assertTrue(self.data['ref_label'].startswith('commit '))

    def test_each_file_has_its_status_added_lines_and_removed_text(self):
        files = self.data['files']
        self.assertEqual(sorted(files), ['app.py', 'gone.txt', 'new.md'])
        self.assertEqual((files['app.py']['status'], files['app.py']['added']), ('modified', [6, 7, 8, 9, 10, 11]))
        self.assertEqual(files['app.py']['removed_text'], {'5': ['    return 2']})
        self.assertEqual(files['app.py']['removed'], {5: 1})
        self.assertEqual((files['gone.txt']['status'], files['gone.txt']['lines']), ('deleted', []))
        self.assertEqual(files['gone.txt']['removed_text'], {'0': ['old', 'line']})
        self.assertEqual((files['new.md']['status'], files['new.md']['added'], files['new.md']['hunks']), ('added', [1, 2], [[1, 2]]))

    def test_the_lines_are_the_ones_of_the_reviewed_code(self):
        self.assertEqual(self.data['files']['app.py']['lines'][5:7], ['    x = 3', '    return x'])

    def test_an_explicit_base_is_used(self):
        git(self.repo, 'branch', '-f', 'other', 'HEAD~1')
        _out, data = build(self.repo, base='other')
        self.assertEqual(data['base'], 'other')

    def test_uncommitted_and_untracked_changes_show_the_working_tree(self):
        repo = make_repo('diff-dirty')
        (repo / 'app.py').write_text('changed\n')
        (repo / 'fresh.txt').write_text('untracked\n')
        _out, data = build(repo)
        self.assertIsNone(data['ref'])
        self.assertTrue(data['ref_label'].startswith('working tree'))
        self.assertEqual(data['files']['app.py']['lines'], ['changed'])
        self.assertEqual((data['files']['fresh.txt']['status'], data['files']['fresh.txt']['added']), ('added', [1]))

    def test_nothing_to_show_is_an_error_that_says_so(self):
        repo = make_repo('diff-same')
        git(repo, 'checkout', '-q', 'main')
        with self.assertRaisesRegex(render.DiffError, 'nothing new'):
            build(repo)

    def test_a_base_that_does_not_exist_or_looks_like_an_option_is_refused(self):
        for base in ('nope', '--output=/tmp/x'):
            with self.subTest(base=base), self.assertRaises(render.DiffError):
                build(self.repo, base=base)

    def test_outside_a_repository_is_an_error(self):
        folder = support.SCRATCH / 'not-a-repo'
        folder.mkdir(exist_ok=True)
        with self.assertRaisesRegex(render.DiffError, 'not inside a git repository'):
            build(folder)

    def test_binary_files_are_named_in_the_notes(self):
        repo = make_repo('diff-binary')
        (repo / 'blob.bin').write_bytes(b'\x00\x01\x02' * 10)
        git(repo, 'add', '-A')
        git(repo, 'commit', '-qm', 'bin')
        _out, data = build(repo)
        self.assertNotIn('blob.bin', data['files'])
        self.assertTrue(any('blob.bin' in n for n in data['notes']))

    def test_parse_patch_still_leaves_a_deleted_file_out(self):
        patch = git(self.repo, 'diff', 'main', 'HEAD')
        self.assertNotIn('gone.txt', render.parse_patch(patch))
        self.assertIn('gone.txt', render.parse_patch_detail(patch))


class DiffPageTest(unittest.TestCase):
    """The stored page: its template, its threads, the hand-off to the session."""

    @classmethod
    def setUpClass(cls):
        repo = make_repo('diff-page')
        out, data = build(repo, session_id=None)
        render.write_page(out, data)
        cls.page = out.parent.name
        cls.data = data

    def thread(self, **fields):
        rec = {'user': True, 'status': None, 'note': '', 'title': 'app.py:6-7 · x = 3', 'severity': 'finding', 'body': '',
               'locations': [{'path': 'app.py', 'start': 6, 'end': 7}], 'created': '2026-11-03T10:00:00'}
        rec.update(fields)
        return rec

    def save(self, findings):
        store.decisions_path(self.page).write_text(json.dumps({'findings': findings, 'updated': '2026-11-03T10:01:00',
                                                               'saved_at': '2026-11-03T10:01:00'}), encoding='utf-8')

    def tearDown(self):
        store.decisions_path(self.page).unlink(missing_ok=True)
        store.chat_path(self.page).unlink(missing_ok=True)

    def test_the_server_serves_the_diff_template(self):
        html = reviews.page_html(self.page, 'N0NCE').decode()
        self.assertIn('id="selbar"', html)
        self.assertIn('N0NCE', html)
        self.assertNotIn('/*__', html)
        self.assertNotIn('__NONCE__', html)

    def test_the_diff_page_has_the_file_search_and_the_folder_fold(self):
        html = reviews.page_html(self.page).decode()
        for needle in ('id="filesearch"', 'id="files-fold"', 'id="files"', 'id="threads"'):
            self.assertIn(needle, html)

    def test_a_review_page_still_gets_the_review_template(self):
        html = reviews.fill_template(support.sample_page('20261019-120000-x'))
        self.assertIn('id="send-fixes"', html)
        self.assertNotIn('id="selbar"', html)

    def test_find_finding_builds_a_thread_from_a_user_record(self):
        self.save({'t1': self.thread(note='use a constant')})
        f = store.find_finding(self.page, store.page_data(self.page), 't1')
        self.assertEqual((f['id'], f['severity'], f['title']), ('t1', 'finding', 'app.py:6-7 · x = 3'))
        self.assertEqual(f['locations'], [{'raw': 'app.py:6-7', 'path': 'app.py', 'start': 6, 'end': 7, 'ambiguous': []}])

    def test_find_finding_refuses_what_is_not_a_valid_thread(self):
        self.save({'a': self.thread(user=False), 'b': self.thread(locations=[]), 'c': self.thread(locations=[{'path': 'app.py', 'start': '6', 'end': 7}]),
                   'd': self.thread(locations=[{'path': 'app.py', 'start': 0, 'end': 3}]), 'e': 'text'})
        data = store.page_data(self.page)
        for fid in 'abcde':
            with self.subTest(fid=fid):
                self.assertIsNone(store.find_finding(self.page, data, fid))
        self.assertIsNone(store.find_finding(self.page, data, 'missing'))

    def test_a_review_page_does_not_take_threads_from_the_triage(self):
        page = '20261019-120001-review-with-user-record'
        support.write_page(page)
        store.decisions_path(page).write_text(json.dumps({'findings': {'t1': self.thread()}}), encoding='utf-8')
        self.assertIsNone(store.find_finding(page, store.page_data(page), 't1'))
        self.assertEqual(store.find_finding(page, store.page_data(page), 'C1')['id'], 'C1')

    def test_the_chat_prompt_shows_the_selected_lines_and_the_thread_state(self):
        self.save({'t1': self.thread(status='fix', note='use a constant')})
        data = store.page_data(self.page)
        finding = store.find_finding(self.page, data, 't1')
        prompt = chat.first_prompt(self.page, data, finding, 'why x?')
        self.assertIn('Change requested', prompt)
        self.assertIn('use a constant', prompt)
        self.assertIn('>+    6     x = 3', prompt)
        self.assertIn('>+    7     return x', prompt)
        self.assertTrue(prompt.rstrip().endswith('why x?'))

    def test_the_diff_chat_has_its_own_system_prompt(self):
        self.assertIn('diff-review', chat.DIFF_SYSTEM_PROMPT)
        self.assertIn('```change', chat.DIFF_SYSTEM_PROMPT)

    def test_the_summary_for_the_session_lists_the_requested_changes(self):
        self.save({'t1': self.thread(status='fix', note='use a constant'), 't2': self.thread(status='skip', title='gone'),
                   't3': self.thread(status=None, title='question')})
        dec = store.load_decisions(store.decisions_path(self.page))
        text = handoff.summarize(self.page, dec, {}, for_claude=True)
        self.assertIn('Diff review for', text)
        self.assertIn('1 changes requested, 1 resolved, 1 open', text)
        self.assertIn('Changes the reviewer asks for', text)
        self.assertIn('- t1 app.py:6-7 · x = 3. At app.py:6-7.', text)
        self.assertIn('use a constant', text)
        self.assertIn('Resolved, no change wanted:', text)
        self.assertIn('Make the changes under "Changes the reviewer asks for"', text)
        self.assertNotIn('post', text.lower().replace('postscript', ''))

    def test_send_fixes_needs_a_requested_change(self):
        self.save({'t1': self.thread()})
        code, body = handoff.send_fixes(self.page, store.page_data(self.page))
        self.assertEqual(code, 400)
        self.assertEqual(body['error'], 'Request at least one change first.')

    def test_the_list_counts_the_threads_of_a_diff_review(self):
        self.save({'t1': self.thread(status='fix'), 't2': self.thread(status='skip'), 't3': self.thread()})
        row = next(r for r in reviews.list_reviews() if r['page'] == self.page)
        self.assertEqual((row['kind'], row['findings'], row['fix'], row['skip'], row['post']), ('diff', 3, 1, 1, 0))

    def test_a_diff_review_cannot_be_re_reviewed(self):
        self.assertIn('no findings', rereview.unavailable(self.data))


class CmdDiffTest(unittest.TestCase):
    def test_it_builds_the_page_and_prints_where(self):
        repo = make_repo('diff-cmd')
        with mock.patch('os.getcwd', return_value=str(repo)), mock.patch.object(finding_viewer.triage, 'ensure_server', return_value=True), \
                mock.patch.object(finding_viewer, 'open_url') as opened, mock.patch('builtins.print') as printed:
            self.assertEqual(finding_viewer.cmd_diff([]), 0)
        opened.assert_called_once()
        self.assertIn('Opened the diff review', printed.call_args[0][0])

    def test_it_says_why_there_is_nothing_to_show(self):
        repo = make_repo('diff-cmd-empty')
        git(repo, 'checkout', '-q', 'main')
        with mock.patch('os.getcwd', return_value=str(repo)), mock.patch('builtins.print') as printed:
            self.assertEqual(finding_viewer.cmd_diff([]), 1)
        self.assertIn('nothing new', printed.call_args[0][0])

    def test_more_than_one_argument_prints_the_usage(self):
        with mock.patch('builtins.print') as printed:
            self.assertEqual(finding_viewer.cmd_diff(['a', 'b']), 1)
        self.assertIn('Usage', printed.call_args[0][0])

    def test_main_routes_diff(self):
        with mock.patch.object(finding_viewer, 'cmd_diff', return_value=0) as cmd, \
                mock.patch.object(finding_viewer.paths, 'migrate_if_needed'), mock.patch.object(finding_viewer.paths, 'migrate_layout', return_value=None):
            self.assertEqual(finding_viewer.main(['finding_viewer.py', 'diff', 'origin/main']), 0)
        cmd.assert_called_once_with(['origin/main'])


if __name__ == '__main__':
    unittest.main()
