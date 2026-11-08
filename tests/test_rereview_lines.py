"""Moving the findings to the new head without Claude, when the new commits change none of their files
(rereview.start and run with lines_only)."""
import support  # noqa: F401  first: points the tool at a scratch environment before it is imported

import json
import subprocess
import unittest
from unittest import mock

import jobs
import render
import rereview
import store

GIT = ['git', '-c', 'user.name=t', '-c', 'user.email=t@t', '-c', 'commit.gpgsign=false']


def git(repo, *args):
    return subprocess.run([*GIT, '-C', str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def make_repo(name):
    """A repository whose second commit changes only a file no finding is in."""
    repo = support.SCRATCH / name
    (repo / 'src' / 'app').mkdir(parents=True)
    git(repo, 'init', '-q', '-b', 'main')
    (repo / 'src' / 'app' / 'billing.py').write_text(''.join(f'line {i}\n' for i in range(1, 61)))
    (repo / 'README.md').write_text('readme\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'base')
    first = git(repo, 'rev-parse', 'HEAD')
    (repo / 'README.md').write_text('readme\nmore\n')
    git(repo, 'commit', '-qam', 'docs')
    return repo, first, git(repo, 'rev-parse', 'HEAD')


class LinesOnlyTest(unittest.TestCase):
    def setUp(self):
        self.repo, self.first, self.head = make_repo(f'lines-{self._testMethodName}')
        self.name = f'20261108-1000{len(self._testMethodName) % 90 + 10:02d}-lines-{self._testMethodName[-12:]}'
        data = support.sample_page(self.name, pr=None, github=None, mode='fix', ref=self.first,
                                   repo_root=str(self.repo), session_id='sess-gone-lines')
        support.write_page(self.name, data)
        store.decisions_path(self.name).write_text(json.dumps(
            {'rereviews': 0, 'findings': {'C1': {'status': 'fix', 'note': 'keep me', 'mode': 'note'}}}), encoding='utf-8')
        self.fresh = {'status': 'behind', 'head': self.head, 'new_commits': [{'sha': self.head, 'title': 'docs'}],
                      'affected': {}}

    def start(self, fresh=None):
        with mock.patch('rereview.freshness.check', return_value=fresh or self.fresh), \
                mock.patch('rereview.jobs.spawn', return_value=4242) as spawn:
            code, body = rereview.start(self.name, store.page_data(self.name), {}, False, lines_only=True)
        return code, body, spawn

    def test_start_needs_no_review_transcript_and_records_lines_only(self):
        code, body, spawn = self.start()
        self.assertEqual(code, 202)
        self.assertIs(rereview.state(self.name)['lines_only'], True)
        self.assertEqual(rereview.state(self.name)['context'], 'none')
        self.assertEqual(spawn.call_args[0][0]['kind'], 'rereview')

    def test_start_is_refused_when_a_findings_file_changed(self):
        code, body, spawn = self.start({**self.fresh, 'affected': {'C1': {'kind': 'file'}}})
        self.assertEqual(code, 409)
        self.assertIn('review the new commits with Claude', body['error'])
        spawn.assert_not_called()

    def test_run_moves_the_page_to_the_new_head_with_its_triage_and_no_claude(self):
        _code, _body, spawn = self.start()
        with mock.patch('rereview.draft.run_fork') as fork, mock.patch.object(jobs, 'lost', return_value=False):
            rereview.run(spawn.call_args[0][0], render.build_page, render.write_page)
        fork.assert_not_called()
        st = rereview.state(self.name)
        self.assertEqual(st['status'], 'done', st.get('error'))
        self.assertIn('without Claude', st['result']['message'])
        data = store.page_data(self.name)
        self.assertEqual(store.reviewed_ref(data), self.head)
        self.assertIs(data['rereviewed'][-1]['lines_only'], True)
        self.assertEqual([(f['id'], f['locations'][0]['start']) for f in data['findings']], [('C1', 42), ('W1', 10)])
        dec = json.loads(store.decisions_path(self.name).read_text(encoding='utf-8'))
        self.assertEqual((dec['findings']['C1']['status'], dec['findings']['C1']['note']), ('fix', 'keep me'))
        self.assertIn('without a re-review', rereview.heads_up(data))


if __name__ == '__main__':
    unittest.main()
