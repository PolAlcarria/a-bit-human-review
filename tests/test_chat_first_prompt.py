"""The side chat's first prompt words the reviewer's decision for the kind of review: on a review with no PR a skip is won't
fix, a post stored before is undecided, and a note is never a comment to post word for word (chat.first_prompt)."""
import support  # noqa: F401  first: points the tool at a scratch environment before it is imported

import json
import unittest

import chat
import store

FINDING = {'id': 'C1', 'severity': 'critical', 'title': 'Total is not rounded', 'body': 'Round before summing.',
           'locations': []}
COMMENT = {'status': 'post', 'mode': 'comment', 'note': 'Round the total.'}


def first_prompt(name, data_fields, decision):
    """The side chat's first prompt for finding C1 of the page ``name``, its triage set to ``decision``."""
    support.write_page(name, support.sample_page(name, **data_fields))
    (support.OUT / name / 'decisions.json').write_text(json.dumps({'findings': {'C1': decision}}), encoding='utf-8')
    return chat.first_prompt(name, store.page_data(name), FINDING, 'Why is this critical?')


class FirstPromptTests(unittest.TestCase):
    def test_a_local_review_reads_a_stored_post_as_undecided_and_a_note_as_a_note(self):
        prompt = first_prompt('20261020-150001-chat-local-post', {'pr': None, 'github': None, 'title': 'Local review'},
                              COMMENT)
        self.assertIn("Reviewer's decision so far: undecided.", prompt)
        self.assertIn("Reviewer's note:", prompt)
        self.assertNotIn('to post word for word', prompt)

    def test_a_local_review_reads_a_skip_as_won_t_fix(self):
        prompt = first_prompt('20261020-150002-chat-local-skip', {'pr': None, 'github': None, 'title': 'Local review'},
                              {'status': 'skip', 'mode': 'note', 'note': ''})
        self.assertIn("Reviewer's decision so far: Won't fix.", prompt)

    def test_a_pr_review_keeps_post_it_and_the_comment_to_post_word_for_word(self):
        prompt = first_prompt('20261020-150003-chat-pr-post', {}, COMMENT)
        self.assertIn("Reviewer's decision so far: Post it.", prompt)
        self.assertIn('Post it', prompt)
        self.assertIn('to post word for word', prompt)


if __name__ == '__main__':
    unittest.main()
