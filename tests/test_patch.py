import support  # noqa: F401  first: points the tool at a scratch environment before it is imported

import unittest

import freshness as fr
import render

BILLING = 'src/app/billing.py'


def u0_changed():
    return fr.old_side_hunks(support.fixture_text('u0.diff'))


class ParsePatchTest(unittest.TestCase):
    """render.parse_patch on tests/fixtures/pr.diff."""

    def setUp(self):
        self.parsed = render.parse_patch(support.fixture_text('pr.diff'))

    def test_whole_result(self):
        self.assertEqual(self.parsed, {
            BILLING: ([11, 12], {10: 1, 41: 1}, [[10, 14], [41, 42]]),
            'src/app/new_module.py': ([1, 2, 3], {}, [[1, 3]]),
        })

    def test_added_lines_of_a_modified_file(self):
        self.assertEqual(self.parsed[BILLING][0], [11, 12])

    def test_removed_lines_map_to_the_new_line_before_them(self):
        # The removed 'for item' sits after new line 10, the removed 'rounded' after new line 41.
        self.assertEqual(self.parsed[BILLING][1], {10: 1, 41: 1})

    def test_hunk_ranges_include_context_lines(self):
        self.assertEqual(self.parsed[BILLING][2], [[10, 14], [41, 42]])

    def test_new_file_lines_are_all_added(self):
        self.assertEqual(self.parsed['src/app/new_module.py'][0], [1, 2, 3])

    def test_new_file_has_no_removed_lines(self):
        self.assertEqual(self.parsed['src/app/new_module.py'][1], {})

    def test_new_file_hunk_range(self):
        self.assertEqual(self.parsed['src/app/new_module.py'][2], [[1, 3]])

    def test_deleted_file_is_absent(self):
        self.assertNotIn('src/app/old.py', self.parsed)

    def test_only_the_modified_and_new_files_are_listed(self):
        self.assertEqual(sorted(self.parsed), [BILLING, 'src/app/new_module.py'])

    def test_empty_patch_has_no_files(self):
        self.assertEqual(render.parse_patch(''), {})


class OldSideHunksTest(unittest.TestCase):
    """freshness.old_side_hunks on tests/fixtures/u0.diff, a git diff -U0 -M output."""

    def setUp(self):
        self.changed = u0_changed()

    def test_modified_file_has_an_insertion_and_a_replaced_block(self):
        self.assertEqual(self.changed[BILLING], {
            'ranges': [(5, 0), (20, 3)], 'new': [(6, 2), (22, 1)], 'deleted': False})

    def test_sql_comment_lines_are_not_taken_for_file_headers(self):
        # The removed '-- old fee comment' reads '--- old fee comment' inside the hunk; it must not start a file.
        self.assertEqual(self.changed['db/migrations/0042_fees.sql'], {
            'ranges': [(3, 2)], 'new': [(3, 0)], 'deleted': False})

    def test_files_after_the_sql_hunk_are_still_read(self):
        self.assertEqual(sorted(self.changed), sorted([
            BILLING, 'db/migrations/0042_fees.sql', 'src/app/legacy.py', 'src/app/gone.py',
            'src/app/factura_número.py']))

    def test_pure_rename_has_no_hunks_and_its_new_path(self):
        self.assertEqual(self.changed['src/app/legacy.py'], {
            'ranges': [], 'new': [], 'deleted': False, 'to': 'src/app/modern.py'})

    def test_deleted_file_is_marked_deleted_with_its_old_range(self):
        self.assertEqual(self.changed['src/app/gone.py'], {'ranges': [(1, 2)], 'new': [(0, 0)], 'deleted': True})

    def test_non_ascii_path_is_kept_as_written(self):
        self.assertEqual(self.changed['src/app/factura_número.py'], {
            'ranges': [(7, 1)], 'new': [(7, 1)], 'deleted': False})

    def test_empty_patch_has_no_files(self):
        self.assertEqual(fr.old_side_hunks(''), {})


class MapLineTest(unittest.TestCase):
    """freshness.map_line with the u0.diff hunks: where a reviewed line sits at the new head."""

    def setUp(self):
        self.changed = u0_changed()

    def test_line_above_every_change_is_unchanged(self):
        self.assertEqual(fr.map_line(self.changed, BILLING, 3), (BILLING, 3))

    def test_insertion_after_a_line_does_not_move_that_line(self):
        # The insertion sits after old line 5, so line 5 itself stays where it is.
        self.assertEqual(fr.map_line(self.changed, BILLING, 5), (BILLING, 5))

    def test_line_below_an_insertion_moves_down_by_the_inserted_count(self):
        self.assertEqual(fr.map_line(self.changed, BILLING, 6), (BILLING, 8))

    def test_line_inside_a_replaced_block_maps_to_none(self):
        self.assertIsNone(fr.map_line(self.changed, BILLING, 21))

    def test_first_line_of_a_replaced_block_maps_to_none(self):
        self.assertIsNone(fr.map_line(self.changed, BILLING, 20))

    def test_last_line_of_a_replaced_block_maps_to_none(self):
        self.assertIsNone(fr.map_line(self.changed, BILLING, 22))

    def test_insertion_and_replacement_above_net_out(self):
        # +2 from the insertion, then the block of 3 became 1 line: -2 in total.
        self.assertEqual(fr.map_line(self.changed, BILLING, 30), (BILLING, 30))

    def test_renamed_file_follows_its_new_path(self):
        self.assertEqual(fr.map_line(self.changed, 'src/app/legacy.py', 9), ('src/app/modern.py', 9))

    def test_deleted_file_maps_to_none(self):
        self.assertIsNone(fr.map_line(self.changed, 'src/app/gone.py', 1))

    def test_file_not_in_the_diff_is_unchanged(self):
        self.assertEqual(fr.map_line(self.changed, 'src/app/untouched.py', 4), ('src/app/untouched.py', 4))

    def test_line_after_removed_sql_comments_moves_up_by_two(self):
        self.assertEqual(fr.map_line(self.changed, 'db/migrations/0042_fees.sql', 10),
                         ('db/migrations/0042_fees.sql', 8))

    def test_removed_sql_comment_line_maps_to_none(self):
        self.assertIsNone(fr.map_line(self.changed, 'db/migrations/0042_fees.sql', 3))

    def test_removed_line_in_non_ascii_path_maps_to_none(self):
        self.assertIsNone(fr.map_line(self.changed, 'src/app/factura_número.py', 7))

    def test_line_after_replaced_line_in_non_ascii_path_is_unchanged(self):
        self.assertEqual(fr.map_line(self.changed, 'src/app/factura_número.py', 8),
                         ('src/app/factura_número.py', 8))


class HitsTest(unittest.TestCase):
    """freshness.hits: whether old lines start..end overlap a changed range of billing.py."""

    def setUp(self):
        self.ranges = u0_changed()[BILLING]['ranges']

    def test_span_before_the_insertion_does_not_hit(self):
        self.assertIs(fr.hits(self.ranges, 1, 4), False)

    def test_insertion_between_start_and_end_hits(self):
        self.assertIs(fr.hits(self.ranges, 5, 6), True)

    def test_insertion_below_the_span_end_does_not_hit(self):
        self.assertIs(fr.hits(self.ranges, 4, 5), False)

    def test_span_after_the_insertion_does_not_hit(self):
        self.assertIs(fr.hits(self.ranges, 6, 7), False)

    def test_span_on_the_middle_line_of_the_replaced_block_hits(self):
        self.assertIs(fr.hits(self.ranges, 21, 21), True)

    def test_span_on_the_last_line_of_the_replaced_block_hits(self):
        self.assertIs(fr.hits(self.ranges, 22, 22), True)

    def test_span_on_the_first_line_of_the_replaced_block_hits(self):
        self.assertIs(fr.hits(self.ranges, 20, 20), True)

    def test_span_after_the_replaced_block_does_not_hit(self):
        self.assertIs(fr.hits(self.ranges, 23, 30), False)


if __name__ == '__main__':
    unittest.main()
