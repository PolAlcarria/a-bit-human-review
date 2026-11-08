"""Re-review in rereview.py: the ids new findings get (next_id), the fork's answer checked (parse_answer), and the triage
carried to a new head (carry_triage, which uses default_anchor), and the PR comments a finding can be dismissed by
(format_comments)."""
import support  # noqa: F401  first: points the tool at a scratch environment before it is imported
import copy
import unittest

import freshness
from rereview import carry_triage, default_anchor, format_comments, new_findings, next_id, parse_answer

DATA = {'findings': [{'id': 'C1', 'severity': 'critical'}, {'id': 'W1', 'severity': 'warning'},
                     {'id': 'S1', 'severity': 'suggestion', 'rereview': {'status': 'fixed', 'at': 'h1'}}]}
TAKEN = {'C1', 'W1', 'S1', 'W3'}
SRC = 'src/app/io.py'


def item(fid, status, **kw):
    d = {'id': fid, 'status': status, 'severity': kw.pop('severity', 'warning'), 'title': kw.pop('title', 't'),
         'body': kw.pop('body', 'b'), 'locations': kw.pop('locations', [])}
    d.update(kw)
    return d


def good_answer():
    locations = [{'path': SRC, 'start': 5, 'end': 3}, {'path': SRC, 'start': 0}, {'path': ' ', 'start': 4},
                 {'path': SRC, 'start': 7, 'end': 900}, 'bad']
    return {'findings': [item('C1', 'still_applies', severity='critical'),
                         item('W1', 'fixed', why='  Guarded   now  '),
                         item('X', 'new', title='  Leaks   a file  handle ', severity='warning', locations=locations),
                         item('', 'new', title='Second new', severity='warning'),
                         item('', 'new', title='Q new', severity='bogus')]}


class NextIdTest(unittest.TestCase):
    def test_one_past_the_highest_number_for_the_letter(self):
        self.assertEqual(next_id('W', {'W1', 'W3', 'C7', 'W10x'}), 'W4')

    def test_first_number_when_none_is_taken(self):
        self.assertEqual(next_id('Q', set()), 'Q1')

    def test_one_past_the_highest_even_across_a_gap(self):
        self.assertEqual(next_id('C', {'C2', 'C10'}), 'C11')


class ParseAnswerTest(unittest.TestCase):
    def parse(self, out, taken=TAKEN):
        return parse_answer(out, DATA, set(taken))

    def error(self, out):
        with self.assertRaises(ValueError) as cm:
            parse_answer(out, DATA, set(TAKEN))
        return str(cm.exception)

    def test_answer_has_one_entry_per_item_in_order(self):
        self.assertEqual([a['id'] for a in self.parse(good_answer())], ['C1', 'W1', 'W4', 'W5', 'F1'])

    def test_still_applies_entry_is_kept_as_the_review_has_it(self):
        self.assertEqual(self.parse(good_answer())[0], {
            'id': 'C1', 'status': 'still_applies', 'severity': 'critical', 'title': 't', 'body': 'b',
            'recurring': None, 'why': '', 'author_said': 'none', 'anchors': []})

    def test_fixed_entry_has_its_why_with_whitespace_collapsed(self):
        self.assertEqual(self.parse(good_answer())[1], {
            'id': 'W1', 'status': 'fixed', 'severity': 'warning', 'title': 't', 'body': 'b',
            'recurring': None, 'why': 'Guarded now', 'author_said': 'none', 'anchors': []})

    def test_new_entry_takes_the_first_id_after_the_highest_taken(self):
        self.assertEqual(self.parse(good_answer())[2], {
            'id': 'W4', 'status': 'new', 'severity': 'warning', 'title': 'Leaks a file handle', 'body': 'b',
            'recurring': None, 'why': '', 'author_said': 'none', 'anchors': [
                {'raw': 'src/app/io.py:5', 'path': SRC, 'start': 5, 'end': 5, 'pos': 0},
                {'raw': 'src/app/io.py:7-407', 'path': SRC, 'start': 7, 'end': 407, 'pos': 0}]})

    def test_new_entry_title_has_whitespace_collapsed(self):
        self.assertEqual(self.parse(good_answer())[2]['title'], 'Leaks a file handle')

    def test_new_entry_anchors_drop_unusable_locations_and_clamp_the_end(self):
        self.assertEqual([(a['raw'], a['start'], a['end']) for a in self.parse(good_answer())[2]['anchors']],
                         [('src/app/io.py:5', 5, 5), ('src/app/io.py:7-407', 7, 407)])

    def test_new_entry_without_id_gets_the_id_after_the_previous_new_one(self):
        self.assertEqual(self.parse(good_answer())[3], {
            'id': 'W5', 'status': 'new', 'severity': 'warning', 'title': 'Second new', 'body': 'b',
            'recurring': None, 'why': '', 'author_said': 'none', 'anchors': []})

    def test_unknown_severity_becomes_finding(self):
        self.assertEqual(self.parse(good_answer())[4], {
            'id': 'F1', 'status': 'new', 'severity': 'finding', 'title': 'Q new', 'body': 'b',
            'recurring': None, 'why': '', 'author_said': 'none', 'anchors': []})

    def test_new_ids_skip_taken_ids_and_each_other(self):
        self.assertEqual([a['id'] for a in self.parse(good_answer()) if a['status'] == 'new'], ['W4', 'W5', 'F1'])

    def test_taken_ids_set_is_not_mutated(self):
        taken = set(TAKEN)
        parse_answer(good_answer(), DATA, taken)
        self.assertEqual(taken, TAKEN)

    def test_finding_fixed_before_may_be_left_out(self):
        out = {'findings': [item('C1', 'still_applies', severity='critical'), item('W1', 'fixed')]}
        self.assertEqual([a['id'] for a in self.parse(out)], ['C1', 'W1'])

    def test_recurring_that_is_not_a_bool_becomes_none(self):
        out = {'findings': [item('C1', 'fixed', recurring='yes'), item('W1', 'changed')]}
        self.assertEqual(self.parse(out)[0]['recurring'], None)

    def test_recurring_bool_is_kept(self):
        out = {'findings': [item('C1', 'fixed'), item('W1', 'changed', recurring=True)]}
        self.assertEqual(self.parse(out)[1]['recurring'], True)

    def test_id_keeps_only_word_characters_dots_and_dashes(self):
        out = {'findings': [item('C1<b>', 'fixed'), item('W1', 'changed')]}
        data = {'findings': [{'id': 'C1b', 'severity': 'critical'}, {'id': 'W1', 'severity': 'warning'}]}
        self.assertEqual(parse_answer(out, data, set())[0]['id'], 'C1b')

    def test_no_answer_object_is_refused(self):
        self.assertEqual(self.error(None), 'it has no list of findings')

    def test_findings_that_is_not_a_list_is_refused(self):
        self.assertEqual(self.error({'findings': 'x'}), 'it has no list of findings')

    def test_entry_that_is_not_an_object_is_refused(self):
        self.assertEqual(self.error({'findings': ['x']}), 'entry 1 is not an object')

    def test_unknown_status_is_refused(self):
        self.assertEqual(self.error({'findings': [item('C1', 'gone')]}),
                         "C1 has the status 'gone', which is not one of still_applies, changed, fixed, dismissed, new")

    def test_unknown_status_on_an_entry_without_id_names_the_entry(self):
        self.assertEqual(self.error({'findings': [item('', 'gone')]}),
                         "entry 1 has the status 'gone', which is not one of still_applies, changed, fixed, dismissed, new")

    def test_id_not_on_the_page_cannot_be_fixed(self):
        self.assertEqual(self.error({'findings': [item('Z9', 'fixed')]}),
                         'Z9 is not a finding on the page, so it cannot be fixed')

    def test_id_given_twice_is_refused(self):
        out = {'findings': [item('C1', 'fixed'), item('C1', 'changed'), item('W1', 'fixed')]}
        self.assertEqual(self.error(out), 'C1 comes back twice')

    def test_new_finding_without_title_is_refused(self):
        out = {'findings': [item('C1', 'fixed'), item('W1', 'fixed'), item('', 'new', title='  ')]}
        self.assertEqual(self.error(out), 'new finding 3 has no title')

    def test_finding_left_out_is_refused(self):
        self.assertEqual(self.error({'findings': [item('C1', 'fixed')]}),
                         'it leaves out W1: every finding on the page needs a status')


LINES = [f'line {i}' for i in range(1, 61)]
U0 = ('diff --git a/src/app/billing.py b/src/app/billing.py\n--- a/src/app/billing.py\n+++ b/src/app/billing.py\n'
      '@@ -5,0 +6,2 @@\n+    # one\n+    # two\n@@ -41 +43 @@\n-    old = 1\n+    new = 1\n')
BILL = 'src/app/billing.py'


def loc(s, e):
    return {'path': BILL, 'start': s, 'end': e}


def page(findings, hunks):
    return {'page': 'p1', 'title': 'Code review · PR #4242', 'subtitle': 's', 'session_id': 'sess-0001-aaaa', 'pr': 4242,
            'repo_root': '/work/billing', 'files': {BILL: {'lines': list(LINES), 'hunks': hunks}}, 'findings': findings}


class CarryTriageTest(unittest.TestCase):
    def setUp(self):
        old = page([{'id': 'C1', 'severity': 'critical', 'title': 'Rounding', 'body': 'b1', 'locations': [loc(20, 22)]},
                    {'id': 'W1', 'severity': 'warning', 'title': 'Loop', 'body': 'b2', 'locations': [loc(40, 41)]},
                    {'id': 'S1', 'severity': 'suggestion', 'title': 'Name', 'body': 'b3', 'locations': [loc(50, 50)]}],
                   [[18, 25], [38, 45]])
        changed = freshness.old_side_hunks(U0)
        new = page([{'id': 'C1', 'severity': 'critical', 'title': 'Rounding', 'body': 'b1', 'locations': [loc(22, 24)],
                     'rereview': {'status': 'still_applies', 'at': 'h2'}},
                    {'id': 'W1', 'severity': 'warning', 'title': 'Loop v2', 'body': 'b2 changed',
                     'locations': [loc(42, 43)], 'rereview': {'status': 'changed', 'at': 'h2'}},
                    {'id': 'S1', 'severity': 'suggestion', 'title': 'Name', 'body': 'b3', 'locations': [loc(52, 52)],
                     'rereview': {'status': 'fixed', 'at': 'h2'}},
                    {'id': 'W2', 'severity': 'warning', 'title': 'New one', 'body': 'b4', 'locations': [loc(10, 10)],
                     'rereview': {'status': 'new', 'at': 'h2'}}],
                   [[20, 27], [40, 47]])
        self.dec = {'page': 'p1', 'rereviews': 0, 'findings': {
            'C1': {'status': 'post', 'note': 'keep', 'mode': 'note', 'anchor': {'path': BILL, 'line': 21}},
            'W1': {'status': 'post', 'note': 'n2', 'mode': 'comment', 'anchor': {'path': BILL, 'line': 41}},
            'S1': {'status': 'skip', 'note': '', 'mode': 'note', 'recheck': 'h1'},
            'Q9': {'status': 'post', 'note': 'gone id', 'mode': 'note'}}}
        self.dec_before = copy.deepcopy(self.dec)
        self.old, self.new, self.changed = old, new, changed
        self.out = carry_triage(self.dec, old, new, changed, 'h2')
        self.f = self.out['findings']

    def test_the_reviewers_page_mode_is_kept(self):
        dec = {**copy.deepcopy(self.dec), 'mode': 'fix'}
        self.assertEqual(carry_triage(dec, self.old, self.new, self.changed, 'h2')['mode'], 'fix')

    def test_c1_keeps_status_note_and_mode(self):
        self.assertEqual((self.f['C1']['status'], self.f['C1']['note'], self.f['C1']['mode']), ('post', 'keep', 'note'))

    def test_c1_picked_line_moves_through_the_diff(self):
        self.assertEqual(self.f['C1']['anchor'], {'path': BILL, 'line': 23, 'in_diff': True})

    def test_c1_moved_line_has_no_reset_and_no_recheck(self):
        self.assertNotIn('anchor_reset', self.f['C1'])
        self.assertNotIn('recheck', self.f['C1'])

    def test_c1_takes_title_severity_and_body_from_the_new_page(self):
        self.assertEqual((self.f['C1']['title'], self.f['C1']['severity'], self.f['C1']['body']),
                         ('Rounding', 'critical', 'b1'))

    def test_w1_line_that_changed_is_reset_to_the_first_flagged_line(self):
        self.assertEqual(self.f['W1']['anchor_reset'], {'path': BILL, 'line': 41, 'at': 'h2'})

    def test_w1_reset_anchor_goes_to_the_new_default_line(self):
        self.assertEqual(self.f['W1']['anchor'], {'path': BILL, 'line': 42, 'in_diff': True})

    def test_w1_changed_post_is_rechecked_at_this_head(self):
        self.assertEqual(self.f['W1']['recheck'], 'h2')

    def test_w1_changed_skip_is_not_rechecked(self):
        dec = copy.deepcopy(self.dec_before)
        dec['findings']['W1'] = {'status': 'skip', 'note': 'n2', 'mode': 'comment', 'anchor': {'path': BILL, 'line': 41}}
        w1 = carry_triage(dec, self.old, self.new, self.changed, 'h2')['findings']['W1']
        self.assertEqual(w1['status'], 'skip')
        self.assertNotIn('recheck', w1)

    def test_w1_takes_the_reworded_title_and_body(self):
        self.assertEqual((self.f['W1']['title'], self.f['W1']['body']), ('Loop v2', 'b2 changed'))

    def test_w1_keeps_its_note_and_mode(self):
        self.assertEqual((self.f['W1']['note'], self.f['W1']['mode'], self.f['W1']['status']), ('n2', 'comment', 'post'))

    def test_s1_fixed_finding_loses_its_recheck(self):
        self.assertNotIn('recheck', self.f['S1'])

    def test_s1_keeps_skip_status_and_note(self):
        self.assertEqual((self.f['S1']['status'], self.f['S1']['note']), ('skip', ''))

    def test_s1_anchor_is_the_default_line_outside_the_diff(self):
        self.assertEqual(self.f['S1']['anchor'], {'path': BILL, 'line': 52, 'in_diff': False})

    def test_new_finding_starts_undecided(self):
        self.assertEqual(self.f['W2'], {'status': None, 'note': '', 'mode': 'note', 'title': 'New one',
                                        'severity': 'warning', 'body': 'b4',
                                        'anchor': {'path': BILL, 'line': 10, 'in_diff': False}})

    def test_finding_no_longer_on_the_page_keeps_its_triage(self):
        self.assertEqual(self.f['Q9'], {'status': 'post', 'note': 'gone id', 'mode': 'note'})

    def test_top_level_keeps_the_rereview_count(self):
        self.assertEqual(self.out['rereviews'], 0)

    def test_top_level_takes_the_page_fields_of_the_new_page(self):
        o = self.out
        self.assertEqual((o['page'], o['title'], o['subtitle'], o['pr'], o['session_id'], o['repo_root']),
                         ('p1', 'Code review · PR #4242', 's', 4242, 'sess-0001-aaaa', '/work/billing'))

    def test_updated_is_a_utc_timestamp_ending_in_z(self):
        self.assertRegex(self.out['updated'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$')

    def test_input_decisions_are_not_modified(self):
        self.assertEqual(self.dec, self.dec_before)

    def test_anchor_the_reviewer_never_moved_takes_the_new_default(self):
        dec = copy.deepcopy(self.dec_before)
        dec['findings']['C1']['anchor'] = {'path': BILL, 'line': 20}
        f = carry_triage(dec, self.old, self.new, self.changed, 'h2')['findings']
        self.assertEqual(f['C1']['anchor'], {'path': BILL, 'line': 22, 'in_diff': True})
        self.assertNotIn('anchor_reset', f['C1'])

    def test_default_anchor_is_first_flagged_line_inside_a_hunk(self):
        self.assertEqual(default_anchor(self.old, self.old['findings'][0]), {'path': BILL, 'line': 20})

    def test_default_anchor_prefers_a_flagged_line_inside_a_hunk(self):
        data = {'files': {BILL: {'lines': list(LINES), 'hunks': [[18, 25]]}}}
        f = {'locations': [{'path': BILL, 'start': 15, 'end': 20}]}
        self.assertEqual(default_anchor(data, f), {'path': BILL, 'line': 18})

    def test_default_anchor_without_hunks_is_the_first_flagged_line(self):
        data = {'files': {BILL: {'lines': list(LINES), 'hunks': []}}}
        f = {'locations': [{'path': BILL, 'start': 15, 'end': 20}]}
        self.assertEqual(default_anchor(data, f), {'path': BILL, 'line': 15})


if __name__ == '__main__':
    unittest.main()


class DismissedTest(unittest.TestCase):
    """A finding a comment on the PR declined: dismissed, Don't post, and not asked about again."""

    def test_dismissed_is_a_status_the_answer_may_give_with_its_why(self):
        out = {'findings': [item('C1', 'dismissed', why='  @dev-one: rounding is   intended '), item('W1', 'fixed')]}
        a = parse_answer(out, DATA, set(TAKEN))[0]
        self.assertEqual((a['status'], a['why']), ('dismissed', '@dev-one: rounding is intended'))

    def test_finding_dismissed_before_may_be_left_out(self):
        data = {'findings': [{'id': 'C1', 'severity': 'critical', 'rereview': {'status': 'dismissed', 'at': 'h1'}},
                             {'id': 'W1', 'severity': 'warning'}]}
        self.assertEqual([a['id'] for a in parse_answer({'findings': [item('W1', 'still_applies')]}, data, set())],
                         ['W1'])

    def test_dismissed_finding_keeps_the_review_words_and_records_why(self):
        data = {'findings': [{'id': 'C1', 'severity': 'critical', 'title': 'Rounding', 'body': 'b1', 'locations': []}]}
        answer = [{'id': 'C1', 'status': 'dismissed', 'severity': 'warning', 'title': 'other', 'body': 'other',
                   'recurring': None, 'why': 'author: intended', 'anchors': []}]
        out, counts = new_findings(data, answer, {}, 'h2')
        self.assertEqual((out[0]['title'], out[0]['severity'], out[0]['rereview']),
                         ('Rounding', 'critical', {'status': 'dismissed', 'at': 'h2', 'why': 'author: intended'}))
        self.assertEqual(counts['dismissed'], 1)

    def carry(self, status, old_rr=None, head='h2'):
        f = {'id': 'C1', 'severity': 'critical', 'title': 'Rounding', 'body': 'b1', 'locations': [loc(20, 22)]}
        old = page([{**f, **({'rereview': old_rr} if old_rr else {})}], [[18, 25]])
        new = page([{**f, 'rereview': {'status': 'dismissed', 'at': 'h2'}}], [[18, 25]])
        dec = {'findings': {'C1': {'status': status, 'note': 'n', 'mode': 'note', 'recheck': 'h1'}}}
        return carry_triage(dec, old, new, {}, head)['findings']['C1']

    def test_newly_dismissed_finding_becomes_dont_post_and_keeps_its_note(self):
        c1 = self.carry('post')
        self.assertEqual((c1['status'], c1['note']), ('skip', 'n'))
        self.assertNotIn('recheck', c1)

    def test_undecided_dismissed_finding_becomes_dont_post(self):
        self.assertEqual(self.carry(None)['status'], 'skip')

    def test_finding_dismissed_before_keeps_what_the_reviewer_chose_since(self):
        self.assertEqual(self.carry('post', old_rr={'status': 'dismissed', 'at': 'h1'})['status'], 'post')


class FormatCommentsTest(unittest.TestCase):
    def test_threads_with_replies_then_the_conversation(self):
        review = [{'id': 2, 'in_reply_to_id': 1, 'user': {'login': 'dev-one'}, 'body': "We won't change\nthis.",
                   'created_at': '2026-10-19T10:00:00Z'},
                  {'id': 1, 'path': BILL, 'line': 41, 'user': {'login': 'rev'}, 'body': 'Loop is O(n^2)',
                   'created_at': '2026-10-18T10:00:00Z'},
                  {'id': 3, 'path': BILL, 'line': None, 'original_line': 7, 'user': {'login': 'rev'}, 'body': 'Name',
                   'created_at': '2026-10-18T11:00:00Z'}]
        talk = [{'user': {'login': 'dev-one'}, 'body': 'Leaving the rounding as is.', 'created_at': '2026-10-19T12:00:00Z'},
                {'user': {'login': 'bot'}, 'body': '  ', 'created_at': '2026-10-19T13:00:00Z'}]
        lines = format_comments(review, talk).splitlines()
        self.assertTrue(lines[0].startswith('Comments on the PR, oldest first'))
        self.assertEqual(lines[1:], [f'- {BILL}:41, @rev: Loop is O(n^2)', "  - reply by @dev-one: We won't change this.",
                                     f'- {BILL}:7, @rev: Name', 'On the conversation:',
                                     '- @dev-one: Leaving the rounding as is.'])

    def test_no_comments_is_empty(self):
        self.assertEqual(format_comments([], []), '')

    def test_long_comments_are_cut(self):
        talk = [{'user': {'login': 'a'}, 'body': 'x' * 5000, 'created_at': str(i)} for i in range(30)]
        text = format_comments([], talk)
        self.assertLess(len(text), 23000)
        self.assertTrue(text.endswith('- … (more comments, left out)'))
