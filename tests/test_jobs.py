"""Background job workers, run for real against the claude and gh stubs (tests/stubs): side chats, drafts, stops."""
import support  # noqa: F401  first: points the tool at a scratch environment before it is imported

import json
import os
import shutil
import signal
import subprocess
import unittest

import chat
import draft
import freshness
import jobs
import store

A, B, C = 'a1' * 20, 'b2' * 20, 'c3' * 20  # the gh stub's PR: A is the reviewed commit, B and C come after it
READY_ANSWER = {
    'body': 'Overall fine.', 'event': 'COMMENT',
    'comments': [{'finding': 'C1', 'path': 'src/app/billing.py', 'line': 42, 'level': 'must-fix',
                  'body': '**Must-fix:** Round the total.'}],
}
EXPECTED_REVIEW = {
    'body': 'Overall fine.', 'event': 'COMMENT',
    'comments': [{'finding': 'C1', 'title': 'Total is not rounded', 'path': 'src/app/billing.py', 'line': 42,
                  'start_line': None, 'level': 'must-fix', 'body': 'Round the total.', 'own': False, 'in_diff': True}],
}


def settled_thread(page, fid):
    """The thread once its reply is in (its pending is cleared), else None."""
    th = (store.load_chat(page).get('threads') or {}).get(fid)
    return th if th and th.get('pending') is None else None


def finished_draft(page):
    """The page's draft state once it is no longer drafting, else None."""
    st = draft.state(page)
    return st if st and st['status'] != 'drafting' else None


def stepped_draft(page):
    """The draft state's steps once the worker has recorded its tool call, else None."""
    return (draft.state(page) or {}).get('steps') or None


class JobWorkerTests(unittest.TestCase):
    def setUp(self):
        support.fake_session()

    def kill_later(self, pid, job):
        """Stop a worker's process group when the test ends, if the pid is still that job's worker."""
        self.addCleanup(jobs.kill_group, pid, job)

    def page(self, name):
        support.write_page(name, support.sample_page(name, session_id=support.SESSION, review_session=support.SESSION))
        return name

    def reply(self, page, message='Why is this critical?'):
        """Ask the side chat on finding C1 and wait for the reply; returns the thread."""
        code, body = chat.start(page, 'C1', message)
        self.assertEqual(code, 202)
        pending = body['thread']['pending']
        self.kill_later(pending['pid'], pending['job'])
        return support.wait_for(lambda: settled_thread(page, 'C1'))

    def start_draft(self, page, **env_values):
        """Start a draft with these environment variables set; the worker inherits them when it is spawned."""
        with support.env(**env_values):
            code, st = draft.start(page, store.page_data(page), 'Draft it.', 'v1', {})
        if st.get('pid'):
            self.kill_later(st['pid'], st['job'])
        return code, st

    def ready_draft(self, page):
        """A draft the stub answers with READY_ANSWER; returns the finished state."""
        answer = support.SCRATCH / f'{page}-answer.json'
        answer.write_text(json.dumps(READY_ANSWER), encoding='utf-8')
        code, _ = self.start_draft(page, STUB_CLAUDE_STRUCTURED=answer,
                                   STUB_CLAUDE_ARGS=support.SCRATCH / f'{page}-args.jsonl')
        self.assertEqual(code, 202)
        return support.wait_for(lambda: finished_draft(page))

    def kill_draft(self, page):
        """Start a draft that sleeps, wait for its first step, then SIGKILL its process group; returns its pid."""
        code, st = self.start_draft(page, STUB_CLAUDE_SLEEP='30')
        self.assertEqual(code, 202)
        support.wait_for(lambda: stepped_draft(page))
        os.killpg(st['pid'], signal.SIGKILL)
        support.wait_for(lambda: not jobs.alive(st['pid']))
        return st['pid']

    def check_pr(self):
        return freshness._check_pr(str(support.WORK), 4242, A, 'https://github.com/acme/billing')

    def test_stubs_come_first_on_path(self):
        self.assertEqual(shutil.which('claude'), str(support.STUBS / 'claude'))
        self.assertEqual(shutil.which('gh'), str(support.STUBS / 'gh'))

    def test_chat_reply_records_the_claude_session(self):
        th = self.reply(self.page('jobs-chat-session'))
        self.assertEqual(th['claude_session'], 'stub-session-0001')

    def test_chat_thread_keeps_the_user_message_first(self):
        th = self.reply(self.page('jobs-chat-user'))
        self.assertEqual(th['messages'][0]['role'], 'user')
        self.assertEqual(th['messages'][0]['text'], 'Why is this critical?')

    def test_chat_reply_text_steps_and_model_come_from_the_stub(self):
        last = self.reply(self.page('jobs-chat-text'))['messages'][-1]
        self.assertEqual(last['role'], 'claude')
        self.assertEqual(last['text'], 'Stub reply.')
        self.assertEqual(last['steps'], ['Reading billing.py'])
        self.assertIs(last['error'], False)
        self.assertEqual(last['model'], 'stub-model')

    def test_chat_reply_carries_cost_and_context_tokens(self):
        last = self.reply(self.page('jobs-chat-cost'))['messages'][-1]
        self.assertEqual(last['cost'], 0.0123)
        self.assertEqual(last['context_tokens'], 120)

    def test_chat_reply_is_saved_in_the_page_chat_file(self):
        page = self.page('jobs-chat-file')
        self.reply(page)
        saved = json.loads((support.OUT / page / 'chat.json').read_text(encoding='utf-8'))['threads']['C1']
        self.assertEqual(saved['messages'][-1]['text'], 'Stub reply.')
        self.assertEqual(saved['claude_session'], 'stub-session-0001')

    def test_draft_start_answers_drafting_with_a_pid(self):
        code, st = self.start_draft(self.page('jobs-draft-start'), STUB_CLAUDE_SLEEP='30')
        self.assertEqual(code, 202)
        self.assertEqual(st['status'], 'drafting')
        self.assertIsInstance(st['pid'], int)

    def test_draft_ends_ready_with_the_stub_fork(self):
        st = self.ready_draft(self.page('jobs-draft-ready'))
        self.assertEqual(st['status'], 'ready')
        self.assertEqual(st['fork'], 'stub-session-0001')
        self.assertEqual(st['steps'], ['Reading billing.py'])

    def test_draft_review_is_built_from_the_structured_output(self):
        self.assertEqual(self.ready_draft(self.page('jobs-draft-review'))['review'], EXPECTED_REVIEW)

    def test_draft_state_is_saved_in_the_page_drafts_file(self):
        page = self.page('jobs-draft-file')
        self.ready_draft(page)
        saved = json.loads((support.OUT / page / 'drafts.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['status'], 'ready')
        self.assertEqual(saved['review'], EXPECTED_REVIEW)

    def test_draft_forks_the_review_session_with_resume(self):
        page = self.page('jobs-draft-fork')
        self.ready_draft(page)
        lines = (support.SCRATCH / f'{page}-args.jsonl').read_text(encoding='utf-8').splitlines()
        args = json.loads(lines[0])
        self.assertIn('-p', args)
        self.assertEqual(args[args.index('--resume') + 1], 'sess-0001-aaaa')
        self.assertIn('--fork-session', args)

    def test_stop_answers_stopped_true(self):
        page = self.page('jobs-draft-stop-answer')
        self.start_draft(page, STUB_CLAUDE_SLEEP='30')
        support.wait_for(lambda: stepped_draft(page))
        self.assertEqual(draft.stop(page), (200, {'ok': True, 'stopped': True}))

    def test_stopped_draft_is_recorded_as_failed_and_stopped(self):
        page = self.page('jobs-draft-stop-state')
        self.start_draft(page, STUB_CLAUDE_SLEEP='30')
        support.wait_for(lambda: stepped_draft(page))
        draft.stop(page)
        st = draft.state(page)
        self.assertEqual(st['status'], 'failed')
        self.assertIs(st['stopped'], True)
        self.assertEqual(st['error'], 'Stopped by you.')

    def test_stopped_draft_shows_as_stopped_in_the_panel(self):
        page = self.page('jobs-draft-stop-panel')
        self.start_draft(page, STUB_CLAUDE_SLEEP='30')
        support.wait_for(lambda: stepped_draft(page))
        draft.stop(page)
        self.assertEqual(draft.activity(draft.state(page))[0]['state'], 'stopped')

    def test_stop_ends_the_worker_process(self):
        page = self.page('jobs-draft-stop-process')
        _, st = self.start_draft(page, STUB_CLAUDE_SLEEP='30')
        pid = st['pid']
        support.wait_for(lambda: stepped_draft(page))
        draft.stop(page)
        support.wait_for(lambda: not jobs.alive(pid))
        self.assertIs(jobs.alive(pid), False)

    def test_dead_worker_still_says_drafting_until_the_next_read(self):
        page = self.page('jobs-draft-dead-disk')
        self.kill_draft(page)
        on_disk = json.loads((support.OUT / page / 'drafts.json').read_text(encoding='utf-8'))
        self.assertEqual(on_disk['status'], 'drafting')

    def test_dead_worker_reads_as_failed_on_the_next_state_read(self):
        page = self.page('jobs-draft-dead-read')
        self.kill_draft(page)
        self.assertEqual(draft.state(page)['status'], 'failed')
        on_disk = json.loads((support.OUT / page / 'drafts.json').read_text(encoding='utf-8'))
        self.assertEqual(on_disk['status'], 'failed')

    def test_dead_worker_error_names_its_pid(self):
        page = self.page('jobs-draft-dead-error')
        pid = self.kill_draft(page)
        self.assertEqual(draft.state(page)['error'], f'The drafting process stopped (pid {pid}).')

    def test_dead_worker_shows_as_failed_in_the_panel(self):
        page = self.page('jobs-draft-dead-panel')
        self.kill_draft(page)
        self.assertEqual(draft.activity(draft.state(page))[0]['state'], 'failed')

    def test_viewer_login_comes_from_gh_api_user(self):
        self.assertEqual(freshness.viewer_login(), 'stub-reviewer')

    def test_behind_pr_lists_the_commits_after_the_reviewed_one(self):
        r = self.check_pr()
        self.assertEqual(r['status'], 'behind')
        self.assertEqual(r['state'], 'OPEN')
        self.assertEqual(r['head'], C)
        self.assertEqual([c['sha'] for c in r['new_commits']], [B, C])

    def test_new_commit_names_its_author_and_url(self):
        r = self.check_pr()
        self.assertEqual(r['new_commits'][1]['author'], 'Dev One')
        self.assertEqual(r['new_commits'][0]['url'], f'https://github.com/acme/billing/pull/4242/commits/{B}')

    def test_my_standing_review_is_read_from_the_pr(self):
        self.assertEqual(self.check_pr()['mine'], {
            'state': 'CHANGES_REQUESTED', 'at': '2026-10-18T12:00:00Z', 'commit': A, 'new_since': 2,
            'covers_head': False})

    def test_open_pr_with_changes_is_not_done_and_has_a_compare_url(self):
        r = self.check_pr()
        self.assertIs(r['done'], False)
        self.assertEqual(r['compare_url'], f'https://github.com/acme/billing/compare/{A}...{C}')

    def test_gh_stub_refuses_unknown_calls(self):
        r = subprocess.run(['gh', 'repo', 'clone', 'acme/billing'], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)


if __name__ == '__main__':
    unittest.main()
