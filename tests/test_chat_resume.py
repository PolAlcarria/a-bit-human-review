"""A resumed side chat whose saved folder is gone (a re-review, or three newer reviews, pruned its worktree) runs in
the page's current checkout, and a failure to start Claude names what was missing (chat.run)."""
import support  # noqa: F401  first: points the tool at a scratch environment before it is imported

import shutil
import unittest
from unittest import mock

import chat
import store

SESSION = 'stub-session-0001'  # the id tests/stubs/claude reports, so the resume keeps it


def pending_thread(name: str, cwd: str) -> None:
    """The page ``name`` with thread C1 resumed on ``cwd``, its new message waiting for the worker to answer it."""
    support.write_page(name, support.sample_page(name))
    chat._save(name, {'page': name, 'threads': {'C1': {
        'claude_session': SESSION, 'mode': 'finding', 'cwd': cwd,
        'messages': [{'role': 'user', 'text': 'Why is this critical?'}, {'role': 'claude', 'text': 'Because.'},
                     {'role': 'user', 'text': 'what changed?'}],
        'pending': {'job': 'job-1', 'status': 'Starting', 'steps': [], 'pid': None, 'session': SESSION}}}})


def answer(name: str, cwd: str) -> dict:
    """Runs the worker for that message and returns thread C1 as chat.json then holds it."""
    chat.run({'kind': 'chat', 'job': 'job-1', 'page': name, 'fid': 'C1', 'prompt': 'what changed?',
              'resume': SESSION, 'model': '', 'mode': 'finding', 'cwd': cwd})
    return store.load_chat(name)['threads']['C1']


class ResumeTests(unittest.TestCase):
    def test_a_thread_whose_worktree_was_pruned_answers_from_the_page_checkout(self):
        name = '20261104-180001-chat-resume-pruned'
        gone = support.SCRATCH / 'worktrees-gone' / '6b86ccd4a65c'
        gone.mkdir(parents=True)
        shutil.rmtree(gone)
        pending_thread(name, str(gone))
        th = answer(name, str(gone))
        last = th['messages'][-1]
        self.assertFalse(last.get('error'), last['text'])
        self.assertEqual(last['text'], 'Stub reply.')
        self.assertEqual(th['cwd'], store.review_checkout(store.page_data(name))[0])
        self.assertEqual(th['cwd'], str(support.WORK))
        self.assertEqual(th['claude_session'], SESSION)

    def test_a_failure_to_start_claude_names_the_missing_path(self):
        name = '20261104-180002-chat-resume-missing'
        pending_thread(name, str(support.WORK))
        missing = FileNotFoundError(2, 'No such file or directory', '/gone/x')
        with mock.patch.object(chat.claude_cli, 'Stream', side_effect=missing):
            th = answer(name, str(support.WORK))
        last = th['messages'][-1]
        self.assertTrue(last.get('error'))
        self.assertIn('/gone/x', last['text'])


if __name__ == '__main__':
    unittest.main()
