import support  # noqa: F401  first: points the tool at a scratch environment before it is imported

import unittest

import draft

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
