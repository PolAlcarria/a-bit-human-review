import support  # noqa: F401  first: points the tool at a scratch environment before it is imported

import unittest

import parse

NO_SEVERITY = ("Notes on the change.\n\n- **Cache never expires** in src/app/cache.py:15, so stale rates stay.\n"
               "- **Docs** could be nicer.\n- plain item at src/app/cache.py:30\n")


def anchors(f):
    return [(a['path'], a['start'], a['end']) for a in f['anchors']]


def record(f):
    """The fields a test compares, as one tuple: title, severity, id, strong, recurring, anchors, body."""
    return (f['title'], f['severity'], f['id'], f['strong'], f['recurring'], anchors(f), f['body'])


def parsed(name):
    return parse.parse_findings(support.fixture_text(name))


class HeadingSeverityTest(unittest.TestCase):
    """review_headings.md: '## Critical' and '## Suggestions' headings over plain list items."""

    def test_one_finding_per_list_item_in_document_order(self):
        self.assertEqual([f['title'] for f in parsed('review_headings.md')], [
            'Missing rounding in src/app/billing.py:42 when the invoice total is negative.',
            'Race in the retry loop at src/app/queue.py:10-14.',
            'Rename calc in src/app/billing.py:88 for clarity.',
        ])

    def test_first_item_record(self):
        self.assertEqual(record(parsed('review_headings.md')[0]), (
            'Missing rounding in src/app/billing.py:42 when the invoice total is negative.',
            'critical', None, False, False, [('src/app/billing.py', 42, 42)], ''))

    def test_range_anchor_keeps_start_and_end(self):
        self.assertEqual(anchors(parsed('review_headings.md')[1]), [('src/app/queue.py', 10, 14)])

    def test_items_under_critical_heading_are_critical(self):
        self.assertEqual([f['severity'] for f in parsed('review_headings.md')[:2]], ['critical', 'critical'])

    def test_items_under_suggestions_heading_are_suggestions(self):
        self.assertEqual(parsed('review_headings.md')[2]['severity'], 'suggestion')

    def test_plain_items_are_not_strong_and_have_no_reviewer_id(self):
        self.assertEqual([(f['strong'], f['id']) for f in parsed('review_headings.md')], [(False, None)] * 3)

    def test_select_generates_one_id_per_letter(self):
        self.assertEqual([f['id'] for f in parse.select_findings(parsed('review_headings.md'))], ['C1', 'C2', 'S1'])


class BoldLabelTest(unittest.TestCase):
    """review_bold_labels.md: '**Must-fix:**' and '**Nit:**' label lines over bold finding titles."""

    def test_bold_titles_become_findings(self):
        self.assertEqual([f['title'] for f in parsed('review_bold_labels.md')],
                         ['Unbounded query', 'Typo in docstring'])

    def test_must_fix_label_gives_critical(self):
        self.assertEqual(parsed('review_bold_labels.md')[0]['severity'], 'critical')

    def test_nit_label_gives_suggestion(self):
        self.assertEqual(parsed('review_bold_labels.md')[1]['severity'], 'suggestion')

    def test_bold_titles_are_strong(self):
        self.assertEqual([f['strong'] for f in parsed('review_bold_labels.md')], [True, True])

    def test_anchor_comes_from_the_title_line(self):
        self.assertEqual([anchors(f) for f in parsed('review_bold_labels.md')],
                         [[('src/app/reports.py', 120, 120)], [('src/app/reports.py', 7, 7)]])

    def test_continuation_line_joins_the_body(self):
        self.assertEqual(parsed('review_bold_labels.md')[0]['body'],
                         'in src/app/reports.py:120 loads every row.\n\nIt also ignores the tenant filter.')

    def test_title_with_nothing_after_it_has_empty_body(self):
        self.assertEqual(parsed('review_bold_labels.md')[1]['body'], '')

    def test_select_keeps_both_with_ids_from_their_labels(self):
        self.assertEqual([f['id'] for f in parse.select_findings(parsed('review_bold_labels.md'))], ['C1', 'S1'])


class ReviewerIdTest(unittest.TestCase):
    """review_ids.md: ids the reviewer wrote, '[C1]' in a heading and 'W2.' in a heading."""

    def test_bracket_id_heading_keeps_its_id(self):
        self.assertEqual(parsed('review_ids.md')[0]['id'], 'C1')

    def test_bare_id_heading_keeps_its_id(self):
        self.assertEqual(parsed('review_ids.md')[1]['id'], 'W2')

    def test_bracket_id_heading_record(self):
        self.assertEqual(record(parsed('review_ids.md')[0]), (
            'Missing auth check — src/app/api/views.py:55', 'critical', 'C1', True, False,
            [('src/app/api/views.py', 55, 55)], 'The view skips the permission check.'))

    def test_bare_id_heading_record(self):
        self.assertEqual(record(parsed('review_ids.md')[1]), (
            'Slow loop at src/app/tasks.py:30', 'warning', 'W2', True, False,
            [('src/app/tasks.py', 30, 30)], 'Body here.'))

    def test_reviewer_ids_are_kept_by_select(self):
        self.assertEqual([f['id'] for f in parse.select_findings(parsed('review_ids.md'))], ['C1', 'W2'])


class FencedBlockTest(unittest.TestCase):
    """review_fenced.md: a code fence under a heading; lines inside the fence are never findings."""

    def test_only_the_finding_after_the_fence_is_parsed(self):
        self.assertEqual([f['title'] for f in parsed('review_fenced.md')], ['Real finding at src/app/billing.py:42'])

    def test_finding_after_the_fence_record(self):
        self.assertEqual(record(parsed('review_fenced.md')[0]), (
            'Real finding at src/app/billing.py:42', 'critical', None, False, False,
            [('src/app/billing.py', 42, 42)], ''))

    def test_no_anchor_comes_from_inside_the_fence(self):
        self.assertEqual([a for f in parsed('review_fenced.md') for a in anchors(f)],
                         [('src/app/billing.py', 42, 42)])


class TableTest(unittest.TestCase):
    """review_table.md: a markdown table is not read as findings."""

    def test_table_rows_are_not_findings(self):
        self.assertEqual(parsed('review_table.md'), [])

    def test_select_of_a_table_review_is_empty(self):
        self.assertEqual(parse.select_findings(parsed('review_table.md')), [])


class InlineTextTest(unittest.TestCase):
    """Short review texts written inline in this file."""

    def test_recurring_marker_sets_recurring_and_leaves_the_title(self):
        found = parse.parse_findings("## Warnings\n\n- **[Recurring]** **Retry without backoff** at src/app/queue.py:22\n")
        self.assertEqual(len(found), 1)
        self.assertEqual(record(found[0]), ('Retry without backoff', 'warning', None, True, True,
                                            [('src/app/queue.py', 22, 22)], 'at src/app/queue.py:22'))

    def test_text_without_any_severity_gives_severity_none(self):
        self.assertEqual([f['severity'] for f in parse.parse_findings(NO_SEVERITY)], [None, None])

    def test_bold_items_without_severity_record(self):
        self.assertEqual([record(f) for f in parse.parse_findings(NO_SEVERITY)], [
            ('Cache never expires', None, None, True, False, [('src/app/cache.py', 15, 15)],
             'in src/app/cache.py:15, so stale rates stay.'),
            ('Docs', None, None, True, False, [], 'could be nicer.'),
        ])

    def test_plain_item_outside_any_title_is_dropped(self):
        self.assertEqual(len(parse.parse_findings(NO_SEVERITY)), 2)

    def test_select_without_severity_keeps_only_the_anchored_title(self):
        self.assertEqual([(f['id'], f['severity'], f['title']) for f in parse.select_findings(parse.parse_findings(NO_SEVERITY))],
                         [('F1', 'finding', 'Cache never expires')])

    def test_item_without_anchor_selects_nothing(self):
        text = "## Warnings\n\n- Something vague with no location\n"
        self.assertEqual(len(parse.parse_findings(text)), 1)
        self.assertEqual(parse.select_findings(parse.parse_findings(text)), [])

    def test_horizontal_rule_ends_the_severity_section(self):
        self.assertEqual(parse.parse_findings("## Critical\n\n---\n\n- item at src/app/a.py:1\n"), [])

    def test_empty_text_has_no_findings(self):
        self.assertEqual(parse.parse_findings(''), [])

    def test_select_ignores_strong_anchored_items_when_some_item_has_a_severity(self):
        found = [
            {'id': None, 'title': 'Loose', 'severity': None, 'strong': True, 'anchors': [{'path': 'a.py', 'start': 1}]},
            {'id': None, 'title': 'Vague', 'severity': 'warning', 'strong': False, 'anchors': []},
        ]
        self.assertEqual(parse.select_findings(found), [])


class AssignIdsTest(unittest.TestCase):
    """assign_ids: free ids per letter, missing severities, the internal 'strong' key."""

    def sample(self):
        return [{'id': None, 'severity': 'critical', 'strong': True},
                {'id': 'C1', 'severity': 'critical', 'strong': True},
                {'id': None, 'severity': None, 'strong': False},
                {'id': None, 'severity': 'critical'}]

    def test_free_ids_skip_the_ids_already_taken(self):
        self.assertEqual([f['id'] for f in parse.assign_ids(self.sample())], ['C2', 'C1', 'F1', 'C3'])

    def test_missing_severity_becomes_finding(self):
        self.assertEqual([f['severity'] for f in parse.assign_ids(self.sample())],
                         ['critical', 'critical', 'finding', 'critical'])

    def test_strong_key_is_removed_from_every_item(self):
        self.assertEqual([('strong' in f) for f in parse.assign_ids(self.sample())], [False] * 4)

    def test_returns_the_same_list_object(self):
        items = self.sample()
        self.assertIs(parse.assign_ids(items), items)

    def test_each_letter_counts_on_its_own(self):
        items = [{'id': None, 'severity': 'warning'}, {'id': None, 'severity': 'suggestion'},
                 {'id': None, 'severity': 'question'}, {'id': None, 'severity': 'warning'}]
        self.assertEqual([f['id'] for f in parse.assign_ids(items)], ['W1', 'S1', 'Q1', 'W2'])


if __name__ == '__main__':
    unittest.main()
