import support  # noqa: F401  first: points the tool at a scratch environment before it is imported

import unittest

import extract


def anchor(raw, path, start, end):
    return {'raw': raw, 'path': path, 'start': start, 'end': end, 'pos': 0}


class ToFindingsTest(unittest.TestCase):
    def test_fixture_answer_becomes_the_expected_findings(self):
        self.assertEqual(extract.to_findings(support.fixture_json('extract_answer.json')), [
            {'id': 'C1', 'severity': 'critical', 'title': 'Rounding', 'body': 'b',
             'anchors': [anchor('src/app/billing.py:42', 'src/app/billing.py', 42, 42),
                         anchor('src/app/billing.py:50', 'src/app/billing.py', 50, 50)],
             'recurring': False, 'meta': {'line': 'looked up by Claude'}},
            {'id': None, 'severity': 'critical', 'title': 'dup id', 'body': '', 'anchors': [],
             'recurring': False, 'meta': {}},
            {'id': 'W7', 'severity': 'warning', 'title': 'digits id', 'body': '',
             'anchors': [anchor('src/app/a.py:1-900', 'src/app/a.py', 1, 401)],
             'recurring': False, 'meta': {}},
            {'id': 'X1', 'severity': 'finding', 'title': 'weird', 'body': '', 'anchors': [],
             'recurring': True, 'meta': {}},
            {'id': None, 'severity': 'question', 'title': 'no id', 'body': '', 'anchors': [],
             'recurring': False, 'meta': {}},
        ])

    def test_empty_list_gives_no_findings(self):
        self.assertEqual(extract.to_findings([]), [])

    def test_duplicate_id_is_dropped_to_none(self):
        items = [{'id': 'C1', 'severity': 'critical', 'title': 'a'}, {'id': 'C1', 'severity': 'critical', 'title': 'b'}]
        self.assertEqual([f['id'] for f in extract.to_findings(items)], ['C1', None])

    def test_digits_id_gets_the_letter_of_its_severity(self):
        items = [{'id': '3', 'severity': 'critical', 'title': 'a'},
                 {'id': '4', 'severity': 'suggestion', 'title': 'b'},
                 {'id': '5', 'severity': 'question', 'title': 'c'}]
        self.assertEqual([f['id'] for f in extract.to_findings(items)], ['C3', 'S4', 'Q5'])

    def test_digits_id_with_unknown_severity_gets_f(self):
        items = [{'id': '9', 'severity': 'bogus', 'title': 'a'}]
        self.assertEqual([f['id'] for f in extract.to_findings(items)], ['F9'])

    def test_id_keeps_only_word_dot_and_dash_characters(self):
        items = [{'id': 'X<1>!', 'title': 'a'}, {'id': 'R-2.x/y', 'title': 'b'}]
        self.assertEqual([f['id'] for f in extract.to_findings(items)], ['X1', 'R-2.xy'])

    def test_id_is_cut_to_12_characters(self):
        items = [{'id': 'ABCDEFGHIJKLMNOP', 'title': 'a'}]
        self.assertEqual([f['id'] for f in extract.to_findings(items)], ['ABCDEFGHIJKL'])

    def test_unknown_severity_becomes_finding(self):
        items = [{'id': 'Z1', 'severity': 'bogus', 'title': 'a'}]
        self.assertEqual([f['severity'] for f in extract.to_findings(items)], ['finding'])

    def test_end_is_clamped_to_400_lines_after_start_but_raw_keeps_the_end(self):
        items = [{'id': 'A1', 'title': 't', 'locations': [{'path': 'src/app/a.py', 'start': 1, 'end': 900}]}]
        self.assertEqual(extract.to_findings(items)[0]['anchors'],
                         [anchor('src/app/a.py:1-900', 'src/app/a.py', 1, 401)])

    def test_end_below_start_becomes_start(self):
        items = [{'id': 'A1', 'title': 't', 'locations': [{'path': 'p.py', 'start': 50, 'end': 49}]}]
        self.assertEqual(extract.to_findings(items)[0]['anchors'], [anchor('p.py:50', 'p.py', 50, 50)])

    def test_location_without_path_is_skipped(self):
        items = [{'id': 'A1', 'title': 't', 'locations': [{'path': '', 'start': 3}]}]
        self.assertEqual(extract.to_findings(items)[0]['anchors'], [])

    def test_location_at_line_zero_is_skipped(self):
        items = [{'id': 'A1', 'title': 't', 'locations': [{'path': 'src/app/x.py', 'start': 0}]}]
        self.assertEqual(extract.to_findings(items)[0]['anchors'], [])

    def test_more_than_eight_locations_keeps_the_first_eight(self):
        locations = [{'path': 'src/app/a.py', 'start': n} for n in range(1, 11)]
        anchors = extract.to_findings([{'id': 'A1', 'title': 't', 'locations': locations}])[0]['anchors']
        self.assertEqual([a['start'] for a in anchors], [1, 2, 3, 4, 5, 6, 7, 8])

    def test_looked_up_location_sets_meta(self):
        items = [{'id': 'A1', 'title': 't', 'locations': [{'path': 'p.py', 'start': 4, 'looked_up': True}]}]
        self.assertEqual(extract.to_findings(items)[0]['meta'], {'line': 'looked up by Claude'})

    def test_location_not_looked_up_leaves_meta_empty(self):
        items = [{'id': 'A1', 'title': 't', 'locations': [{'path': 'p.py', 'start': 4}]}]
        self.assertEqual(extract.to_findings(items)[0]['meta'], {})

    def test_blank_title_is_skipped(self):
        self.assertEqual(extract.to_findings([{'title': '   '}]), [])

    def test_title_is_stripped_and_cut_to_200_characters(self):
        items = [{'id': 'A1', 'title': ' ' + 'x' * 250 + ' '}]
        self.assertEqual(extract.to_findings(items)[0]['title'], 'x' * 200)

    def test_non_object_entries_are_skipped(self):
        self.assertEqual(extract.to_findings(['not a dict', 5, None]), [])

    def test_body_is_stripped_and_defaults_to_empty(self):
        items = [{'id': 'A1', 'title': 't', 'body': ' b '}, {'id': 'A2', 'title': 't'}]
        self.assertEqual([f['body'] for f in extract.to_findings(items)], ['b', ''])

    def test_recurring_is_a_boolean(self):
        items = [{'id': 'A1', 'title': 't', 'recurring': 1}, {'id': 'A2', 'title': 't'}]
        self.assertEqual([f['recurring'] for f in extract.to_findings(items)], [True, False])


if __name__ == '__main__':
    unittest.main()
