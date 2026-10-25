import support  # noqa: F401  first: points the tool at a scratch environment before it is imported

import json
import re
import unittest

import reviews

NONCE = 'n0nce123'
DATA = {'title': 'Fix <b>bold</b> & co',
        'findings': [{'id': 'C1', 'body': '</script><script>alert(1)</script> __NONCE__ ünïcode'}]}
DATA_BLOCK = re.compile(r'<script type="application/json" id="data">(.*?)</script>', re.S)


class FillTemplateTest(unittest.TestCase):
    def setUp(self):
        self.page = reviews.fill_template(DATA, NONCE)

    def test_nonce_goes_on_all_four_script_tags(self):
        self.assertEqual(self.page.count('nonce="n0nce123"'), 4)

    def test_placeholder_inside_review_text_is_left_alone(self):
        self.assertEqual(self.page.count('__NONCE__'), 1)

    def test_title_is_escaped_into_the_title_tag(self):
        self.assertIn('<title>Fix &lt;b&gt;bold&lt;/b&gt; &amp; co</title>', self.page)

    def test_script_end_tag_in_review_text_is_escaped(self):
        self.assertNotIn('</script><script>alert', self.page)
        self.assertIn('\\u003c/script>', self.page)

    def test_data_and_title_placeholders_are_replaced(self):
        self.assertNotIn('/*__DATA__*/', self.page)
        self.assertNotIn('__TITLE__', self.page)

    def test_data_block_round_trips_to_the_same_data(self):
        self.assertEqual(json.loads(DATA_BLOCK.search(self.page).group(1)), DATA)

    def test_without_nonce_script_tags_get_an_empty_nonce(self):
        self.assertEqual(reviews.fill_template(DATA).count('nonce=""'), 4)

    def test_without_nonce_placeholder_in_review_text_is_still_left(self):
        self.assertEqual(reviews.fill_template(DATA).count('__NONCE__'), 1)

    def test_non_ascii_text_is_written_unescaped(self):
        page = reviews.fill_template(DATA)
        self.assertIn('ünïcode', page)
        self.assertNotIn('\\u00fc', page)


if __name__ == '__main__':
    unittest.main()
