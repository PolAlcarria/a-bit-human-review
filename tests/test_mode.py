"""The page's mode: post (Post / Don't post) or fix (Fix / Won't fix), decided when the page is built and switched by
the reviewer on the page."""
import support  # noqa: F401  first: points the tool at a scratch environment before it is imported

import json
import unittest
from unittest import mock

import render
import reviews
import store
import triage


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


class RoutesFollowTheModeTest(unittest.TestCase):
    """Draft comments and posting only in post mode; the page's mode is in /api/handoff."""

    def page(self, name, mode, dec_mode=None):
        support.write_page(name, support.sample_page(name, mode=mode))
        dec = {'findings': {'C1': {'status': 'post', 'note': ''}}, **({'mode': dec_mode} if dec_mode else {})}
        store.decisions_path(name).write_text(json.dumps(dec), encoding='utf-8')
        return name

    def test_drafting_in_fix_mode_is_refused(self):
        code, body = triage.draft_review(self.page('20261108-100001-mode-draft', 'fix'))
        self.assertEqual(code, 409)
        self.assertIn('fix mode', body['error'])

    def test_drafting_after_the_reviewer_switched_to_fix_is_refused(self):
        code, _ = triage.draft_review(self.page('20261108-100002-mode-switched', 'post', 'fix'))
        self.assertEqual(code, 409)

    def test_posting_in_fix_mode_is_refused(self):
        code, body = triage.post_drafts(self.page('20261108-100003-mode-post', 'fix'), {})
        self.assertEqual(code, 409)
        self.assertIn('fix mode', body['error'])

    def test_handoff_says_the_mode_in_force(self):
        with mock.patch('triage.freshness.viewer_login', return_value='me'):
            self.assertEqual(triage.handoff(self.page('20261108-100004-mode-handoff', 'post', 'fix'))['mode'], 'fix')


class ListCountsTest(unittest.TestCase):
    """The list of reviews counts decisions in the page's mode and says the mode."""

    def row(self, name, mode, statuses, dec_mode=None):
        support.write_page(name, support.sample_page(name, mode=mode))
        dec = {'findings': dict(zip(('C1', 'W1'), ({'status': s} for s in statuses))),
               **({'mode': dec_mode} if dec_mode else {})}
        store.decisions_path(name).write_text(json.dumps(dec), encoding='utf-8')
        return next(r for r in reviews.list_reviews() if r['page'] == name)

    def test_post_mode_does_not_count_a_stored_fix(self):
        r = self.row('20261108-100011-list-post', 'post', ['post', 'fix'])
        self.assertEqual((r['mode'], r['post'], r['fix']), ('post', 1, 0))

    def test_fix_mode_does_not_count_a_stored_post(self):
        r = self.row('20261108-100012-list-fix', 'post', ['post', 'fix'], 'fix')
        self.assertEqual((r['mode'], r['post'], r['fix']), ('fix', 0, 1))


if __name__ == '__main__':
    unittest.main()
