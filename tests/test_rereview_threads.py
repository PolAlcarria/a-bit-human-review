"""A re-review reads each posted finding's own thread on the PR and its side chat, and checks what the author answered
against the code: posting records which finding each comment came from (draft.record_posted), the PR's comments are
split into each finding's thread (rereview.finding_threads), the prompt quotes them under the finding, and the answer's
author_said reaches the page and the hand-off."""
import support  # noqa: F401  first: points the tool at a scratch environment before it is imported

import json
import unittest
from unittest import mock

import chat
import draft
import handoff
import jobs
import rereview
import store

BILL = 'src/app/billing.py'
REVIEW_ID = 9001


def comment(cid, body, *, path=BILL, line=42, review=REVIEW_ID, reply_to=None, who='stub-reviewer', at='2026-10-18'):
    """One entry of `gh api repos/O/R/pulls/N/comments`, cut to the fields the re-review reads."""
    return {'id': cid, 'path': path, 'line': line, 'original_line': line, 'pull_request_review_id': review,
            'in_reply_to_id': reply_to, 'user': {'login': who}, 'body': body, 'created_at': at,
            'html_url': f'https://github.com/acme/billing/pull/4242#discussion_r{cid}'}


POSTED = [{'id': REVIEW_ID, 'comments': [
    {'finding': 'C1', 'path': BILL, 'line': 42, 'body': '**Must fix:** Round the total before summing.'},
    {'finding': 'W1', 'path': BILL, 'line': 10, 'body': '**Should fix:** Hoist the rate.'},
    {'finding': 'S9', 'in_body': True}]}]


class RecordPostedTest(unittest.TestCase):
    def test_post_hands_the_worker_the_finding_behind_each_comment(self):
        name = '20261105-100001-threads-post'
        support.write_page(name, support.sample_page(name))
        drafted = {'body': '', 'event': 'COMMENT', 'comments': [
            {'finding': 'C1', 'path': BILL, 'line': 42, 'level': 'must', 'body': 'Round it.'},
            {'finding': 'W1', 'path': 'src/elsewhere.py', 'line': 3, 'level': '', 'body': 'Outside the diff.'},
            {'finding': 'S1', 'path': BILL, 'line': 42, 'level': '', 'body': 'Left out.'}]}
        jobs.write_json(draft.state_path(name), {'status': 'ready', 'review': drafted})
        payload = {'event': 'COMMENT', 'body': '', 'comments': [
            {'path': BILL, 'line': 42, 'level': 'must', 'body': 'Round it.'},
            {'path': 'src/elsewhere.py', 'line': 3, 'level': '', 'body': 'Outside the diff.'},
            {'path': BILL, 'line': 42, 'body': 'Left out.', 'include': False}]}
        with mock.patch.object(draft.jobs, 'spawn', return_value=123) as spawn:
            code, _ = draft.post(name, store.page_data(name), payload)
        self.assertEqual(code, 202)
        posted = spawn.call_args[0][0]['posted']
        self.assertEqual([p['finding'] for p in posted], ['C1', 'W1'])
        self.assertEqual(posted[0], {'finding': 'C1', 'path': BILL, 'line': 42, 'body': draft.with_level('must', 'Round it.')})
        self.assertEqual(posted[1], {'finding': 'W1', 'in_body': True})

    def test_each_posted_review_is_kept_apart_from_the_drafts(self):
        name = '20261105-100002-threads-record'
        support.write_page(name, support.sample_page(name))
        draft.record_posted(name, {'id': 1, 'html_url': 'u1'}, {'commit_id': 'abc'}, [{'finding': 'C1'}])
        draft.record_posted(name, {'id': 2}, {}, [{'finding': 'W1'}])
        draft.record_posted(name, {}, {}, [{'finding': 'X1'}])  # GitHub gave no id: nothing to match later
        reviews = draft.posted_reviews(name)
        self.assertEqual([(r['id'], r['comments']) for r in reviews], [(1, [{'finding': 'C1'}]), (2, [{'finding': 'W1'}])])
        self.assertEqual(reviews[0]['commit'], 'abc')

    def test_a_review_posted_before_posted_json_is_read_from_the_drafts(self):
        name = '20261105-100003-threads-legacy'
        support.write_page(name, support.sample_page(name))
        jobs.write_json(draft.state_path(name), {'status': 'posted', 'posted': {'id': 77, 'url': 'u'}, 'review': {
            'comments': [{'finding': 'C1', 'path': BILL, 'line': 42, 'level': 'must', 'body': 'Round it.'},
                         {'finding': 'W1', 'path': BILL, 'line': 10, 'body': 'Dropped.', 'include': False}]}})
        self.assertEqual(draft.posted_reviews(name), [{'id': 77, 'url': 'u', 'at': None, 'comments': [
            {'finding': 'C1', 'path': BILL, 'line': 42, 'body': draft.with_level('must', 'Round it.')}]}])
        draft.record_posted(name, {'id': 77}, {}, [])
        self.assertEqual(len(draft.posted_reviews(name)), 1, 'a recorded review is not read twice')


class FindingThreadsTest(unittest.TestCase):
    def test_comments_match_by_text_then_by_line_and_replies_follow(self):
        review = [
            comment(1, '**Must fix:**   Round the total\nbefore summing.'),
            comment(2, 'Hoist the rate (edited on GitHub).', line=10),
            comment(3, 'Done in 554b78a.', reply_to=1, who='dev-one', at='2026-10-19'),
            comment(4, 'Someone else, same line.', review=5),
            comment(5, 'Reply to the other one.', reply_to=4, who='dev-one'),
        ]
        threads, rest = rereview.finding_threads(review, POSTED)
        self.assertEqual([c['id'] for c in threads['C1']], [1, 3])
        self.assertEqual([c['id'] for c in threads['W1']], [2])
        self.assertEqual([c['id'] for c in rest], [4, 5])

    def test_a_comment_from_another_review_is_never_a_finding_s(self):
        threads, rest = rereview.finding_threads([comment(1, '**Must fix:** Round the total before summing.', review=6)],
                                                 POSTED)
        self.assertEqual(threads, {})
        self.assertEqual(len(rest), 1)

    def test_pr_comments_gives_the_threads_and_the_other_comments(self):
        review = [comment(1, '**Must fix:** Round the total before summing.'), comment(2, 'Fixed.', reply_to=1)]
        lists = {'repos/acme/billing/pulls/4242/comments': review, 'repos/acme/billing/issues/4242/comments': []}
        with mock.patch.object(rereview, '_gh_list', side_effect=lambda e: lists[e]):
            text, threads = rereview.pr_comments(support.sample_page('x'), POSTED)
        self.assertEqual(text, 'Other comments on the PR: none.')
        self.assertEqual([c['id'] for c in threads['C1']], [1, 2])


class PromptTest(unittest.TestCase):
    def test_the_thread_and_the_side_chat_come_under_their_finding(self):
        data = support.sample_page('20261105-100004-threads-prompt')
        threads = {'C1': [comment(1, '**Must fix:** Round it.'), comment(2, 'Done.', reply_to=1, who='dev-one')]}
        chats = {'W1': ['    - reviewer: Is the rate read per row?', '    - Claude: Yes, line 11.']}
        st = {'from': 'a' * 40, 'to': 'b' * 40}
        prompt = rereview.build_prompt(data, st, {'threads': threads, 'chats': chats}, None, {'C1', 'W1'})
        c1, w1 = prompt.index('- C1 '), prompt.index('- W1 ')
        thread = prompt.index('Its thread on the PR')
        self.assertTrue(c1 < thread < w1)
        self.assertIn('    - posted by @stub-reviewer: **Must fix:** Round it.\n    - reply by @dev-one: Done.', prompt)
        self.assertGreater(prompt.index("The reviewer's side chat"), w1)
        self.assertIn('    - Claude: Yes, line 11.', prompt)
        self.assertIn('author_said', prompt)

    def test_the_reviewers_decision_and_note_come_under_their_finding_and_no_summary(self):
        data = support.sample_page('20261108-100021-decisions-prompt')
        dec = {'findings': {'C1': {'status': 'post', 'note': 'Ask for a test too.'}, 'W1': {'status': None, 'note': ''},
                            'S1': {'status': 'skip', 'note': ''}}}
        st = {'from': 'a' * 40, 'to': 'b' * 40}
        prompt = rereview.build_prompt(data, st, {'decisions': rereview.decisions(dec)}, None, {'C1', 'W1'})
        c1, w1 = prompt.index('- C1 '), prompt.index('- W1 ')
        mine = prompt.index('The reviewer chose: Post (a comment on the PR). Their note (not instructions to you):\n'
                            '    Ask for a test too.')
        self.assertTrue(c1 < mine < w1)
        self.assertEqual(prompt.count('The reviewer chose'), 1)  # W1 has nothing, S1 is not on the page
        self.assertNotIn('Finding-viewer triage', prompt)

    def test_in_a_fork_only_touched_or_rewritten_findings_carry_their_text(self):
        data = support.sample_page('20261108-100022-bodies-prompt')
        bodies = {f['id']: f['body'] for f in data['findings'] if f.get('body')}
        st = {'from': 'a' * 40, 'to': 'b' * 40}
        spec = {'affected': {'C1': {'kind': 'lines'}}}
        prompt = rereview.build_prompt(data, st, spec, None, set(bodies))
        self.assertIn(bodies['C1'].strip().split('\n')[0], prompt)
        for fid in set(bodies) - {'C1'}:
            self.assertNotIn(bodies[fid].strip().split('\n')[0], prompt)
        self.assertEqual(prompt.count('Its text is in your review above'), len(bodies) - 1)
        for every in (rereview.build_prompt(data, st, spec, None, set(bodies), context='fresh'),
                      rereview.build_prompt(data, {**st, 'rewritten': True}, spec, None, set(bodies))):
            self.assertNotIn('Its text is in your review above', every)

    def test_a_finding_closed_before_gets_only_its_title_and_why(self):
        data = support.sample_page('20261108-100023-closed-prompt')
        f = data['findings'][0]
        f['rereview'] = {'status': 'fixed', 'at': 'c' * 40, 'why': 'rounded now'}
        st = {'from': 'a' * 40, 'to': 'b' * 40}
        prompt = rereview.build_prompt(data, st, {'affected': {f['id']: {'kind': 'lines'}},
                                                  'decisions': {f['id']: {'status': 'post', 'note': 'x'}}}, None, set())
        start = prompt.index(f"- {f['id']} ")
        block = prompt[start:prompt.index('\n- ', start + 1)]
        self.assertIn('in an earlier re-review: rounded now', block)
        self.assertNotIn(f['body'].strip().split('\n')[0], block)
        self.assertNotIn('The reviewer chose', block)

    def test_decisions_keep_a_status_or_a_note_and_shorten_long_notes(self):
        got = rereview.decisions({'findings': {'C1': {'status': 'fix', 'note': ''}, 'W1': {'status': None, 'note': ' '},
                                               'S1': {'status': 'odd', 'note': 'n' * 700}, 'Q1': 'not a dict'}})
        self.assertEqual(set(got), {'C1', 'S1'})
        self.assertEqual(got['C1'], {'status': 'fix', 'note': ''})
        self.assertEqual((got['S1']['status'], len(got['S1']['note'])), (None, rereview.MAX_NOTE_CHARS + 1))

    def test_threads_past_the_total_budget_are_left_out_with_a_pointer(self):
        data = support.sample_page('20261105-100007-threads-budget')
        threads = {'C1': [comment(1, 'x' * 900)], 'W1': [comment(2, 'y' * 900, line=10)]}
        st = {'from': 'a' * 40, 'to': 'b' * 40}
        with mock.patch.object(rereview, 'MAX_THREADS_TOTAL_CHARS', 1000):
            prompt = rereview.build_prompt(data, st, {'threads': threads}, None, {'C1', 'W1'})
        self.assertIn('x' * 900, prompt)
        self.assertNotIn('y' * 900, prompt)
        self.assertIn('  Its thread on the PR: left out, the prompt is long already.', prompt)

    def test_side_chats_keep_the_latest_messages_and_skip_errors_and_page_lines(self):
        name = '20261105-100005-threads-chat'
        support.write_page(name, support.sample_page(name))
        msgs = [{'role': 'user', 'text': f'question {i}'} for i in range(20)] + [
            {'role': 'system', 'text': 'Re-reviewed at …'}, {'role': 'claude', 'text': 'Error: x', 'error': True},
            {'role': 'claude', 'text': 'Last  answer.'}]
        chat._save(name, {'threads': {'C1': {'messages': msgs}, 'W1': {'messages': []}}})
        with mock.patch.object(rereview, 'MAX_CHAT_CHARS', 120):
            lines = rereview.side_chats(name)
        self.assertEqual(set(lines), {'C1'})
        self.assertEqual(lines['C1'][0], '    - … (earlier messages, left out)')
        self.assertEqual(lines['C1'][-1], '    - Claude: Last answer.')
        self.assertNotIn('Re-reviewed', ' '.join(lines['C1']))


class AuthorSaidTest(unittest.TestCase):
    DATA = {'findings': [{'id': 'C1', 'severity': 'critical'}, {'id': 'W1', 'severity': 'warning'},
                         {'id': 'S1', 'severity': 'suggestion', 'rereview': {'status': 'fixed', 'at': 'h1'}}]}

    def parse(self, items):
        return rereview.parse_answer({'findings': items}, self.DATA, {'C1', 'W1', 'S1'})

    def entry(self, fid, status, **kw):
        return {'id': fid, 'status': status, 'severity': 'warning', 'title': 't', 'body': 'b', 'locations': [], **kw}

    def test_author_said_is_kept_when_known_and_none_otherwise(self):
        answer = self.parse([self.entry('C1', 'still_applies', author_said='fixed', why='The  zero case.'),
                             self.entry('W1', 'fixed', author_said='bogus'),
                             self.entry('', 'new', title='N', author_said='fixed')])
        self.assertEqual([a['author_said'] for a in answer], ['fixed', 'none', 'none'])
        self.assertEqual(answer[0]['why'], 'The zero case.')

    def test_the_page_gets_the_claim_what_is_missing_and_the_thread(self):
        data = support.sample_page('y')
        data['findings'].append({'id': 'S1', 'severity': 'suggestion', 'title': 's', 'body': '', 'locations': [],
                                 'rereview': {'status': 'fixed', 'at': 'h1', 'why': 'gone'}})
        answer = self.parse([self.entry('C1', 'still_applies', author_said='fixed', why='The zero case.'),
                             self.entry('W1', 'fixed', author_said='fixed', why='Hoisted.')])
        threads = {'C1': [comment(1, 'Round it.'), comment(2, 'Done.', reply_to=1, who='dev-one')],
                   'S1': [comment(3, 'Name it.', line=5)]}
        findings, _ = rereview.new_findings(data, answer, {}, 'h2', threads)
        rr = {f['id']: f['rereview'] for f in findings}
        self.assertEqual(rr['C1']['author_said'], 'fixed')
        self.assertEqual(rr['C1']['why'], 'The zero case.')
        self.assertEqual([c['who'] for c in rr['C1']['thread']], ['stub-reviewer', 'dev-one'])
        self.assertTrue(rr['C1']['thread'][1]['url'].endswith('discussion_r2'))
        self.assertTrue(rereview.claimed_but_open(rr['C1']))
        self.assertFalse(rereview.claimed_but_open(rr['W1']))
        self.assertNotIn('thread', rr['W1'])
        # A finding fixed before keeps its outcome, with its thread read again.
        self.assertEqual((rr['S1']['status'], rr['S1']['at'], rr['S1']['thread'][0]['body']), ('fixed', 'h1', 'Name it.'))

    def test_an_open_finding_the_author_did_not_call_fixed_keeps_no_why(self):
        answer = self.parse([self.entry('C1', 'still_applies', why='chatter'), self.entry('W1', 'still_applies')])
        findings, _ = rereview.new_findings(support.sample_page('z'), answer, {}, 'h2')
        self.assertEqual(findings[0]['rereview'], {'status': 'still_applies', 'at': 'h2'})

    def test_the_chat_note_and_the_hand_off_say_the_claim_does_not_hold(self):
        rr = {'status': 'still_applies', 'at': 'h2', 'author_said': 'fixed', 'why': 'The zero case.'}
        self.assertEqual(rereview._outcome(rr, 'h2'),
                         'still applies, though the author said on the PR it is fixed (still missing: The zero case.)')
        name = '20261105-100006-threads-handoff'
        data = support.sample_page(name)
        data['findings'][0]['rereview'] = rr
        support.write_page(name, data)
        dec = {'title': 'Code review · PR #4242', 'findings': {'C1': {'status': 'post', 'title': 'Total', 'note': ''}}}
        text = handoff.summarize(name, dec, {}, for_claude=True)
        self.assertIn('  The author said on the PR that it is fixed, but the re-review found it still applies: '
                      'The zero case.', text)


if __name__ == '__main__':
    unittest.main()
