import support  # noqa: F401  first: points the tool at a scratch environment before it is imported

import json
import unittest

import buttons
import chat
import paths


class ButtonsTest(unittest.TestCase):
    """buttons.load: the quick buttons under the chat, from chat-buttons.json in the data folder."""

    def tearDown(self):
        paths.CHAT_BUTTONS.unlink(missing_ok=True)

    def write(self, content):
        paths.CHAT_BUTTONS.write_text(content if isinstance(content, str) else json.dumps(content), encoding='utf-8')

    def test_without_a_file_the_built_in_buttons_are_used(self):
        got, problems = buttons.load()
        self.assertEqual(problems, [])
        self.assertEqual(got, buttons.DEFAULTS)
        self.assertEqual([b['label'] for b in got['finding']], ['Is it real?', 'Draft the comment', 'Smallest fix?', 'Softer'])

    def test_the_built_in_lists_are_copies(self):
        buttons.load()[0]['diff'].clear()
        self.assertTrue(buttons.load()[0]['diff'])

    def test_a_kind_in_the_file_replaces_the_built_in_list_of_that_kind_only(self):
        self.write({'diff': [{'label': ' Mine ', 'prompt': ' Look at this. '}]})
        got, problems = buttons.load()
        self.assertEqual(problems, [])
        self.assertEqual(got['diff'], [{'label': 'Mine', 'prompt': 'Look at this.'}])
        self.assertEqual(got['finding'], buttons.DEFAULTS['finding'])

    def test_an_empty_list_shows_no_buttons(self):
        self.write({'finding': []})
        got, problems = buttons.load()
        self.assertEqual((got['finding'], problems), ([], []))

    def test_an_invalid_entry_is_skipped_and_reported(self):
        self.write({'diff': [{'label': 'ok', 'prompt': 'fine'}, {'label': 'no prompt'}, 'text', {'label': '', 'prompt': 'x'},
                             {'label': 5, 'prompt': 'x'}]})
        got, problems = buttons.load()
        self.assertEqual(got['diff'], [{'label': 'ok', 'prompt': 'fine'}])
        self.assertEqual(len(problems), 4)
        self.assertIn('"diff" button 2', problems[0])

    def test_too_long_entries_are_skipped_and_reported(self):
        self.write({'finding': [{'label': 'x' * (buttons.MAX_LABEL + 1), 'prompt': 'p'}, {'label': 'l', 'prompt': 'p' * (buttons.MAX_PROMPT + 1)},
                                {'label': 'fits', 'prompt': 'p' * buttons.MAX_PROMPT}]})
        got, problems = buttons.load()
        self.assertEqual([b['label'] for b in got['finding']], ['fits'])
        self.assertEqual(len(problems), 2)

    def test_more_than_the_maximum_are_cut_and_reported(self):
        self.write({'finding': [{'label': f'b{i}', 'prompt': 'p'} for i in range(buttons.MAX_BUTTONS + 3)]})
        got, problems = buttons.load()
        self.assertEqual(len(got['finding']), buttons.MAX_BUTTONS)
        self.assertEqual(len(problems), 1)

    def test_an_unknown_kind_and_a_kind_that_is_not_a_list_are_reported(self):
        self.write({'review': [], 'diff': 'nope'})
        got, problems = buttons.load()
        self.assertEqual(got, buttons.DEFAULTS)
        self.assertEqual(len(problems), 2)

    def test_a_file_that_is_not_json_or_not_an_object_leaves_the_built_in_buttons(self):
        for text in ('{oops', '[]', '"x"'):
            with self.subTest(text=text):
                self.write(text)
                got, problems = buttons.load()
                self.assertEqual(got, buttons.DEFAULTS)
                self.assertEqual(len(problems), 1)

    def test_the_example_file_is_valid(self):
        paths.CHAT_BUTTONS.write_text((support.ROOT / 'examples' / 'chat-buttons.json').read_text(encoding='utf-8'), encoding='utf-8')
        got, problems = buttons.load()
        self.assertEqual(problems, [])
        self.assertEqual([len(got['finding']), len(got['diff'])], [3, 4])

    def test_the_settings_answer_carries_the_buttons(self):
        self.write({'diff': [{'label': 'Mine', 'prompt': 'Mine?'}]})
        self.assertEqual(chat.settings_payload()['buttons']['diff'], [{'label': 'Mine', 'prompt': 'Mine?'}])


if __name__ == '__main__':
    unittest.main()
