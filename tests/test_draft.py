import support  # noqa: F401  first: points the tool at a scratch environment before it is imported

import unittest

import claude_cli
import draft
import triage

PATH = 'src/app/billing.py'
DIFF = {'files': {PATH: {'hunks': [[10, 14], [41, 42]]}}}


class WithoutLevelTest(unittest.TestCase):
    def test_strips_bold_level_label_with_colon(self):
        self.assertEqual(draft.without_level('**Must-fix:** Round the total.'), 'Round the total.')

    def test_strips_bracketed_label(self):
        self.assertEqual(draft.without_level('[Nit] spacing'), 'spacing')

    def test_keeps_plain_opening_word(self):
        self.assertEqual(draft.without_level('Nit picking aside, fine.'), 'Nit picking aside, fine.')

    def test_strips_bold_label_without_colon(self):
        self.assertEqual(draft.without_level('**Should-fix** handle None'), 'handle None')

    def test_strips_plain_label_with_colon(self):
        self.assertEqual(draft.without_level('Suggestion: use a set'), 'use a set')

    def test_strips_underscore_bold_label(self):
        self.assertEqual(draft.without_level('__Question:__ why?'), 'why?')

    def test_keeps_body_when_nothing_would_be_left(self):
        self.assertEqual(draft.without_level('**Nit:**'), '**Nit:**')

    def test_keeps_lowercase_word_without_label_marks(self):
        self.assertEqual(draft.without_level('question why this?'), 'question why this?')

    def test_strips_lowercase_spaced_label_with_surrounding_spaces(self):
        self.assertEqual(draft.without_level('  **must fix**: x'), 'x')

    def test_body_without_any_label_is_unchanged(self):
        self.assertEqual(draft.without_level('Round the total.'), 'Round the total.')


class WithLevelTest(unittest.TestCase):
    def test_level_in_bold_then_text_on_the_same_line(self):
        self.assertEqual(draft.with_level('must-fix', 'Round it.'), '**Must-fix:** Round it.')

    def test_code_fence_goes_on_its_own_line(self):
        self.assertEqual(draft.with_level('nit', '```py\nx\n```'), '**Nit:**\n\n```py\nx\n```')

    def test_tilde_fence_goes_on_its_own_line(self):
        self.assertEqual(draft.with_level('nit', '~~~\nx\n~~~'), '**Nit:**\n\n~~~\nx\n~~~')

    def test_bullet_list_goes_on_its_own_line(self):
        self.assertEqual(draft.with_level('question', '- a list'), '**Question:**\n\n- a list')

    def test_table_goes_on_its_own_line(self):
        self.assertEqual(draft.with_level('suggestion', '| a | b |'), '**Suggestion:**\n\n| a | b |')

    def test_numbered_list_goes_on_its_own_line(self):
        self.assertEqual(draft.with_level('should-fix', '1. first'), '**Should-fix:**\n\n1. first')

    def test_heading_goes_on_its_own_line(self):
        self.assertEqual(draft.with_level('nit', '# Title'), '**Nit:**\n\n# Title')

    def test_quote_goes_on_its_own_line(self):
        self.assertEqual(draft.with_level('nit', '> quoted'), '**Nit:**\n\n> quoted')

    def test_dash_inside_a_line_does_not_start_a_block(self):
        self.assertEqual(draft.with_level('nit', 'x - not a list'), '**Nit:** x - not a list')

    def test_empty_level_gives_text_unchanged(self):
        self.assertEqual(draft.with_level('', 'plain'), 'plain')

    def test_unknown_level_gives_text_unchanged(self):
        self.assertEqual(draft.with_level('bogus', 'plain'), 'plain')

    def test_severity_without_level_gives_text_unchanged(self):
        self.assertEqual(draft.with_level('finding', 'plain'), 'plain')

    def test_without_level_undoes_with_level(self):
        self.assertEqual(draft.without_level(draft.with_level('must-fix', 'Round it.')), 'Round it.')


class InDiffTest(unittest.TestCase):
    def test_line_before_first_hunk_is_outside(self):
        self.assertIs(draft.in_diff(DIFF, PATH, 9), False)

    def test_first_line_of_hunk_is_inside(self):
        self.assertIs(draft.in_diff(DIFF, PATH, 10), True)

    def test_last_line_of_hunk_is_inside(self):
        self.assertIs(draft.in_diff(DIFF, PATH, 14), True)

    def test_line_after_hunk_is_outside(self):
        self.assertIs(draft.in_diff(DIFF, PATH, 15), False)

    def test_line_in_second_hunk_is_inside(self):
        self.assertIs(draft.in_diff(DIFF, PATH, 41), True)

    def test_range_starting_before_the_hunk_is_outside(self):
        self.assertIs(draft.in_diff(DIFF, PATH, 12, 9), False)

    def test_range_inside_one_hunk_is_inside(self):
        self.assertIs(draft.in_diff(DIFF, PATH, 13, 11), True)

    def test_range_across_two_hunks_is_outside(self):
        self.assertIs(draft.in_diff(DIFF, PATH, 41, 14), False)

    def test_file_not_in_the_diff_is_outside(self):
        self.assertIs(draft.in_diff(DIFF, 'src/app/other.py', 10), False)

    def test_data_without_files_is_outside(self):
        self.assertIs(draft.in_diff({}, 'x', 1), False)


if __name__ == '__main__':
    unittest.main()


class ContextTest(unittest.TestCase):
    """draft.context: fresh (the default) needs no review transcript and quotes every finding to post in full; fork
    reads the review session again and is quoted only what a re-review changed or added since."""

    def page(self, **fields):
        return support.sample_page('20261109-100000-draft-context', **{'session_id': 'sess-gone-0000', **fields})

    def test_fresh_is_the_default_and_a_page_with_a_pr_can_draft_without_its_transcript(self):
        self.assertEqual(draft.context(), 'fresh')
        self.assertTrue(draft.can_draft(self.page()))
        self.assertFalse(draft.can_draft(self.page(pr=None)))

    def test_a_fresh_prompt_says_it_has_not_seen_the_review_and_quotes_the_findings_to_post(self):
        prompt = draft.build_prompt(self.page(), 'To post:\n- C1', [], ['C1'], 'fresh')
        self.assertTrue(prompt.startswith('You are drafting the GitHub review comments of PR #4242'))
        self.assertIn('- C1 [Critical] Total is not rounded\n  at src/app/billing.py:42\n  Round before summing.', prompt)
        self.assertNotIn('Hoist it.', prompt)  # W1 is not to post
        self.assertTrue(prompt.endswith('To post:\n- C1'))

    def test_a_fork_prompt_quotes_only_what_a_rereview_changed_or_added(self):
        data = self.page()
        data['findings'][1]['rereview'] = {'status': 'new'}
        prompt = draft.build_prompt(data, 'To post:', ['heads up'], ['C1', 'W1'], 'fork')
        self.assertTrue(prompt.startswith(draft.PROMPT))
        self.assertNotIn('Round before summing.', prompt)
        self.assertIn('a re-review changed or added after your review', prompt)
        self.assertIn('Hoist it.', prompt)
        self.assertTrue(prompt.endswith('To post:\nheads up'))

    def test_the_view_says_a_fork_cannot_run_without_the_transcript(self):
        v = draft.view(self.page())
        self.assertEqual((v['context'], v['fork_ok']), ('fresh', False))
        self.assertIn('Draft in a new session instead', v['fork_why'])
        self.assertNotIn('context_tokens', v)

    def test_the_view_gives_what_a_fork_reads_again(self):
        support.fake_session('sess-draft-ctx-01')
        v = draft.view(self.page(session_id='sess-draft-ctx-01'))
        self.assertIs(v['fork_ok'], True)
        self.assertIn('context_tokens', v)  # None: the fake transcript holds no answer yet


class LimitsTest(unittest.TestCase):
    """draft.max_turns and draft.max_budget_usd: the flags a draft gets, and the error when it stops at one."""

    def test_the_default_caps_turns_and_not_spending(self):
        self.assertEqual(claude_cli.limit_args('draft'), ['--max-turns', '40'])

    def test_a_budget_is_passed_and_zero_turns_is_no_limit(self):
        with support.env(FINDING_VIEWER_DRAFT_MAX_TURNS='0', FINDING_VIEWER_DRAFT_MAX_BUDGET_USD='0.5'):
            self.assertEqual(claude_cli.limit_args('draft'), ['--max-budget-usd', '0.5'])

    def test_a_draft_that_stopped_at_a_limit_says_which_setting(self):
        error = claude_cli.limit_error('draft', 'Drafting', {'subtype': 'error_max_turns'}, 'error_max_turns')
        self.assertEqual(error, 'Drafting stopped at its turn limit (draft.max_turns) before it answered. Raise the '
                                'limit, or set it to 0 for none, and start again.')


class ChosenContextTest(unittest.TestCase):
    """The page's buttons name the context; anything else is refused before a run starts."""

    def test_fork_fresh_or_nothing(self):
        self.assertEqual(triage.chosen_context({'context': 'fork'}), ('fork', None))
        self.assertEqual(triage.chosen_context({}), (None, None))
        self.assertEqual(triage.chosen_context({'context': 'both'}), (None, 'context must be fork or fresh'))
