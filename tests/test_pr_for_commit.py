from __future__ import annotations

import support  # noqa: F401  first: points the tool at a scratch environment before it is imported

import json
import subprocess
import unittest
from unittest import mock

import render

REVIEWED, LATER, OTHER = '6e' * 20, '55' * 20, 'c3' * 20


def pull(number: int, head_sha: str, head_ref: str, default_branch: str = 'develop') -> dict:
    """One entry of `gh api repos/O/R/commits/SHA/pulls`, cut to the fields pr_for_commit reads."""
    return {'number': number, 'head': {'sha': head_sha, 'ref': head_ref, 'repo': {'default_branch': default_branch}}}


def lookup(pulls: list[dict], sha: str = REVIEWED) -> int | None:
    answer = subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(pulls), stderr='')
    with mock.patch('render.subprocess.run', return_value=answer):
        return render.pr_for_commit(support.WORK, sha, 'acme/billing')


class PrForCommitTest(unittest.TestCase):
    """render.pr_for_commit: which PR a saved report's reviewed commit belongs to."""

    def test_the_pr_whose_head_is_the_reviewed_commit(self):
        self.assertEqual(lookup([pull(4242, REVIEWED, 'feature/rounding')]), 4242)

    def test_the_pr_whose_head_moved_on_after_the_review(self):
        # The author pushed after the review: GitHub still lists the PR for the reviewed commit, at a newer head.
        self.assertEqual(lookup([pull(4242, LATER, 'feature/rounding')]), 4242)

    def test_a_release_pr_from_the_default_branch_is_skipped(self):
        self.assertIsNone(lookup([pull(4300, REVIEWED, 'develop')]))

    def test_the_feature_pr_wins_over_a_release_pr_that_also_holds_the_commit(self):
        self.assertEqual(lookup([pull(4300, OTHER, 'develop'), pull(4242, LATER, 'feature/rounding')]), 4242)

    def test_two_prs_holding_the_commit_at_other_heads_are_ambiguous(self):
        self.assertIsNone(lookup([pull(4242, LATER, 'feature/rounding'), pull(4243, OTHER, 'feature/rounding-v2')]))

    def test_a_failed_lookup_finds_no_pr(self):
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout='', stderr='HTTP 404')
        with mock.patch('render.subprocess.run', return_value=failed):
            self.assertIsNone(render.pr_for_commit(support.WORK, REVIEWED, 'acme/billing'))


if __name__ == '__main__':
    unittest.main()
