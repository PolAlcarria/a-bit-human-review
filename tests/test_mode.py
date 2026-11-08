"""The page's mode: post (Post / Don't post) or fix (Fix / Won't fix), decided when the page is built and switched by
the reviewer on the page."""
import support  # noqa: F401  first: points the tool at a scratch environment before it is imported

import unittest
from unittest import mock

import render
import store


class BuiltModeTest(unittest.TestCase):
    """render.review_mode: what the page is built with."""

    def test_no_pr_is_fix(self):
        self.assertEqual(render.review_mode(None, None), 'fix')

    def test_a_pr_you_opened_is_fix(self):
        with mock.patch('render.freshness.viewer_login', return_value='me'):
            self.assertEqual(render.review_mode(12, 'me'), 'fix')

    def test_someone_elses_pr_is_post(self):
        with mock.patch('render.freshness.viewer_login', return_value='me'):
            self.assertEqual(render.review_mode(12, 'other'), 'post')

    def test_a_pr_when_gh_cannot_say_who_you_are_is_post(self):
        with mock.patch('render.freshness.viewer_login', return_value=None):
            self.assertEqual(render.review_mode(12, 'other'), 'post')
        self.assertEqual(render.review_mode(12, None), 'post')


class EffectiveModeTest(unittest.TestCase):
    """store.review_mode: the reviewer's choice, else the built mode, else a guess for a page built before modes."""

    def test_the_reviewers_choice_wins(self):
        self.assertEqual(store.review_mode({'pr': 12, 'mode': 'post'}, {'mode': 'fix'}), 'fix')

    def test_the_built_mode_without_a_choice(self):
        self.assertEqual(store.review_mode({'pr': 12, 'mode': 'post'}, {}), 'post')
        self.assertEqual(store.review_mode({'pr': 12, 'mode': 'fix'}, None), 'fix')

    def test_a_word_outside_the_modes_is_ignored(self):
        self.assertEqual(store.review_mode({'pr': 12, 'mode': 'post'}, {'mode': 'both'}), 'post')

    def test_an_old_page_without_a_pr_is_fix(self):
        self.assertEqual(store.review_mode({}, {}), 'fix')

    def test_an_old_pr_page_is_post_unless_a_finding_is_marked_fix(self):
        self.assertEqual(store.review_mode({'pr': 12}, {'findings': {'W1': {'status': 'post'}}}), 'post')
        self.assertEqual(store.review_mode({'pr': 12}, {'findings': {'W1': {'status': 'fix'}}}), 'fix')


if __name__ == '__main__':
    unittest.main()
