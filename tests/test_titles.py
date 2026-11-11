import support  # noqa: F401  first: points the tool at a scratch environment before it is imported

import shutil
import unittest

import hook
import store
import titles


class CodeTitleTest(unittest.TestCase):
    def test_a_pr_wins_over_branch_and_commit(self):
        self.assertEqual(titles.code_title(pr=42, branch='feat', commit='1a2b3c4d5e'), 'Code review · PR #42')

    def test_a_pinned_commit_wins_over_the_branch(self):
        self.assertEqual(titles.code_title(branch='feat', commit='1a2b3c4d5e6f'), 'Code review · commit 1a2b3c4d')

    def test_the_branch(self):
        self.assertEqual(titles.code_title(branch='feature/rounding'), 'Code review · feature/rounding')

    def test_detached_head_or_no_repo_falls_back_to_the_folder(self):
        self.assertEqual(titles.code_title(branch='HEAD', fallback='billing'), 'Code review · billing')
        self.assertEqual(titles.code_title(branch='', fallback=''), 'Code review · working tree')


class DiffTitleTest(unittest.TestCase):
    def test_the_branch(self):
        self.assertEqual(titles.diff_title('feat', '1a2b3c4d5e'), 'Diff review · feat')

    def test_detached_head_names_the_commit(self):
        self.assertEqual(titles.diff_title('HEAD', '1a2b3c4d5e'), 'Diff review · commit 1a2b3c4d')


class PageTitleTest(unittest.TestCase):
    """Every source, and pages of older versions (no ``branch``), end up under the same rule."""

    def test_old_titles_of_a_pr_review_read_the_same(self):
        for old in ('Code review · billing PR #4242', 'Code review · PR #4242', 'Code review findings',
                    'Review of the billing changes'):
            data = support.sample_page('p', title=old)
            self.assertEqual(titles.page_title(data), 'Code review · PR #4242', old)

    def test_a_branch_review_without_a_pr(self):
        data = support.sample_page('p', title='Code review findings', pr=None)
        self.assertEqual(titles.page_title(data), 'Code review · feature/rounding')

    def test_a_pinned_commit_without_a_pr(self):
        data = support.sample_page('p', title='# My report', pr=None, ref='1a2b3c4d5e6f', ref_label='commit 1a2b3c4d')
        self.assertEqual(titles.page_title(data), 'Code review · commit 1a2b3c4d')

    def test_the_branch_field_wins_over_the_label(self):
        data = support.sample_page('p', pr=None, branch='main', ref_label='working tree (other)')
        self.assertEqual(titles.page_title(data), 'Code review · main')

    def test_a_diff_review(self):
        for label, ref in (('working tree (feat)', None), ('commit 1a2b3c4d (feat)', '1a2b3c4d5e')):
            data = {'kind': 'diff', 'title': 'whatever', 'pr': None, 'ref': ref, 'ref_label': label}
            self.assertEqual(titles.page_title(data), 'Diff review · feat', label)

    def test_it_is_stable(self):
        data = support.sample_page('p', pr=None)
        once = titles.page_title(data)
        self.assertEqual(titles.page_title({**data, 'title': once}), once)


class StoredPageTest(unittest.TestCase):
    def test_page_data_reads_an_old_title_under_the_rule(self):
        name = '20261001-120000-old-title'
        support.write_page(name, support.sample_page(name, title='Code review · billing PR #4242'))
        self.addCleanup(shutil.rmtree, support.OUT / name, True)
        self.assertEqual(store.page_data(name)['title'], 'Code review · PR #4242')


class HookTitleTest(unittest.TestCase):
    def test_report_findings_gets_a_branch_title_not_a_fixed_one(self):
        tool = {'input': {'findings': [{'file': 'a.py', 'line': 3, 'summary': 'Bug'}]}}
        spec = hook.spec_from_tool(tool, str(support.WORK))
        self.assertTrue(spec['title'].startswith('Code review · '), spec['title'])
        self.assertNotEqual(spec['title'], 'Code review findings')


if __name__ == '__main__':
    unittest.main()
