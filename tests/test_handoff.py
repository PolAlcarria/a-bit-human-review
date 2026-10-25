"""The triage summary handed back to Claude (handoff.summarize): a local review (no PR) has nothing to post."""
import support  # noqa: F401  first: points the tool at a scratch environment before it is imported

import unittest

import handoff

DEC = {'title': 'Code review · my branch', 'saved_at': '2026-10-20T12:00:00', 'findings': {
    'C1': {'status': 'fix', 'title': 'Total is not rounded', 'severity': 'critical', 'note': '',
           'anchor': {'path': 'src/app/billing.py', 'line': 42, 'in_diff': True}},
    'W1': {'status': 'skip', 'title': 'Loop re-reads the rate', 'severity': 'warning', 'note': ''},
    'S1': {'status': 'post', 'title': 'Name the constant', 'severity': 'suggestion', 'note': ''},
    'Q1': {'status': None, 'title': 'Why a float?', 'severity': 'question', 'note': ''}}}
LOCAL_FIRST = ('Finding-viewer triage for "Code review · my branch": 1 to fix, 1 won\'t fix, 2 undecided. '
               'Saved 2026-10-20T12:00:00.')
PR_FIRST = ('Finding-viewer triage for "Code review · my branch" (PR #4242): 1 to post, 1 to fix locally, '
            '1 not to post, 1 undecided. Saved 2026-10-20T12:00:00.')


def first_line(text):
    return text.split('\n', 1)[0]


def local_page(name, data=None):
    """Write a page with no PR (sample_page with pr=None) into the scratch folder; returns its name."""
    support.write_page(name, data or support.sample_page(name, pr=None, github=None))
    return name


class LocalReviewTest(unittest.TestCase):
    """A review with no PR: Fix and Won't fix only, a stored Post counts as undecided, nothing is posted."""

    def test_first_line_tallies_fix_won_t_fix_and_undecided(self):
        text = handoff.summarize(local_page('20261020-120001-local-a'), DEC, {}, for_claude=False)
        self.assertEqual(first_line(text), LOCAL_FIRST)
        self.assertTrue(text.startswith('Finding-viewer triage for "'))

    def test_no_post_wording_and_the_undecided_line_names_the_stored_post(self):
        text = handoff.summarize(local_page('20261020-120002-local-b'), DEC, {}, for_claude=False)
        for phrase in ('to post', 'not to post', 'To post:'):
            self.assertNotIn(phrase, text)
        self.assertIn("Won't fix:", text)
        self.assertIn('Undecided: S1, Q1.', text)

    def test_for_claude_asks_for_no_drafts_and_says_won_t_fix(self):
        text = handoff.summarize(local_page('20261020-120003-local-c'), DEC, {}, for_claude=True)
        self.assertNotIn('When drafting comments', text)
        self.assertIn("Change only those: leave the findings marked won't fix as they are.", text)

    def test_fixed_finding_is_left_out_without_saying_not_to_post(self):
        name = '20261020-120004-local-d'
        data = support.sample_page(name, pr=None, github=None)
        data['findings'][1]['rereview'] = {'status': 'fixed', 'at': '2026-10-19T10:00:00', 'why': 'the rate is hoisted'}
        text = handoff.summarize(local_page(name, data), DEC, {}, for_claude=False)
        self.assertIn('Fixed by the new commits, so left out: W1 (the rate is hoisted).', text)
        self.assertNotIn('not to post', text)


class PrReviewTest(unittest.TestCase):
    """A review with a PR keeps Post, Fix and Don't post as before."""

    def test_first_line_and_sections_are_unchanged(self):
        name = '20261020-120005-pr-a'
        support.write_page(name)
        text = handoff.summarize(name, dict(DEC, pr=4242), {}, for_claude=False)
        self.assertEqual(first_line(text), PR_FIRST)
        self.assertTrue(text.startswith('Finding-viewer triage for "'))
        self.assertIn('To post:', text)
        self.assertIn('Not posting:', text)

    def test_for_claude_keeps_the_pr_instructions(self):
        name = '20261020-120006-pr-b'
        support.write_page(name)
        text = handoff.summarize(name, dict(DEC, pr=4242), {}, for_claude=True)
        self.assertIn('Change only those: the findings to post stay as they are. Do not post them or mention them on the PR.',
                      text)
        self.assertIn('When drafting comments', text)


if __name__ == '__main__':
    unittest.main()
