"""The worktree of a PR review (store.review_checkout) checks out the PR's head branch at the reviewed commit when git
allows it safely, else a detached worktree with one viewer.log line saying why; pruning keeps a worktree that has
uncommitted changes."""
import support  # noqa: F401  first: points the tool at a scratch environment before it is imported

import itertools
import os
import shutil
import subprocess
import unittest
from pathlib import Path

import paths
import render
import store

B = 'feat/rounding'
_n = itertools.count()
GIT_ENV = {**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't',
           'GIT_COMMITTER_EMAIL': 't@t'}


def git(cwd, *args: str) -> str:
    r = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, text=True, env=GIT_ENV)
    if r.returncode:
        raise AssertionError(f'git {" ".join(args)} failed: {r.stderr}')
    return r.stdout.strip()


def commit(cwd, msg: str) -> str:
    (Path(cwd) / f'f{next(_n)}.txt').write_text(msg)
    git(cwd, 'add', '-A')
    git(cwd, 'commit', '-q', '-m', msg)
    return git(cwd, 'rev-parse', 'HEAD')


def rev(cwd, name: str):
    r = subprocess.run(['git', '-C', str(cwd), 'rev-parse', '--verify', '--quiet', name], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def head_ref(cwd) -> str:
    """The branch a folder has checked out, or 'detached'."""
    r = subprocess.run(['git', '-C', str(cwd), 'symbolic-ref', '--quiet', '--short', 'HEAD'], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else 'detached'


def log_text() -> str:
    try:
        return paths.LOG.read_text(encoding='utf-8')
    except OSError:
        return ''


class Repo:
    """A scratch bare origin with `main` and the PR branch B (two commits past main), and a clone of it on main."""

    def __init__(self):
        base = support.SCRATCH / f'repo-{next(_n)}'
        seed, self.origin, self.clone = base / 'seed', base / 'origin.git', base / 'clone'
        seed.mkdir(parents=True)
        git(seed, 'init', '-q', '-b', 'main')
        self.main = commit(seed, 'main')
        git(seed, 'switch', '-q', '-c', B)
        self.c1 = commit(seed, 'pr 1')
        self.c2 = commit(seed, 'pr 2')
        git(seed, 'switch', '-q', 'main')
        subprocess.run(['git', 'clone', '-q', '--bare', str(seed), str(self.origin)], check=True, capture_output=True)
        subprocess.run(['git', 'clone', '-q', str(self.origin), str(self.clone)], check=True, capture_output=True)
        git(self.clone, 'branch', '-q', '-D', B) if rev(self.clone, f'refs/heads/{B}') else None

    def page(self, ref: str, **fields) -> dict:
        return support.sample_page('p', **{'repo_root': str(self.clone), 'ref': ref, 'pr': 4242, 'head_branch': B,
                                           'cross_repo': False, 'github': None, **fields})


class ReviewCheckoutTests(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(paths.WORKTREES, ignore_errors=True)
        self.repo = Repo()
        self.log_start = len(log_text())

    def new_log(self) -> str:
        return log_text()[self.log_start:]

    def checkout(self, data: dict) -> Path:
        path, snapshot = store.review_checkout(data)
        self.assertTrue(snapshot)
        self.assertEqual(Path(path), paths.WORKTREES / data['ref'][:12])
        self.assertEqual(rev(path, 'HEAD'), data['ref'])
        return Path(path)

    def test_a_branch_only_on_origin_is_created_tracking_it(self):  # (a)
        path = self.checkout(self.repo.page(self.repo.c2))
        self.assertEqual(head_ref(path), B)
        self.assertEqual(git(path, 'rev-parse', '--abbrev-ref', f'{B}@{{upstream}}'), f'origin/{B}')
        self.assertEqual(rev(self.repo.clone, f'refs/heads/{B}'), self.repo.c2)

    def test_an_older_page_looks_the_branch_up_with_gh(self):  # (a), page rendered before 0.5.1
        data = self.repo.page(self.repo.c2)
        del data['head_branch'], data['cross_repo']
        with support.env(STUB_GH_HEAD_REF=B):
            path = self.checkout(data)
        self.assertEqual(head_ref(path), B)

    def test_a_failed_lookup_stays_detached(self):
        data = self.repo.page(self.repo.c2)
        del data['head_branch'], data['cross_repo']
        with support.env(STUB_GH_FAIL='1'):
            path = self.checkout(data)
        self.assertEqual(head_ref(path), 'detached')
        self.assertIn('could not look up', self.new_log())

    def test_the_branch_held_by_the_users_checkout_stays_there(self):  # (b)
        git(self.repo.clone, 'switch', '-q', B)
        path = self.checkout(self.repo.page(self.repo.c2))
        self.assertEqual(head_ref(path), 'detached')
        self.assertEqual(head_ref(self.repo.clone), B)
        self.assertEqual(rev(self.repo.clone, B), self.repo.c2)
        self.assertIn(f'{B} is checked out in {self.repo.clone}', self.new_log())

    def test_the_branch_held_by_the_users_own_worktree_stays_there(self):  # (b), a worktree outside the tool's folder
        mine = support.SCRATCH / f'mine-{next(_n)}'
        git(self.repo.clone, 'worktree', 'add', '-q', '-b', B, str(mine), self.repo.c2)
        path = self.checkout(self.repo.page(self.repo.c2))
        self.assertEqual(head_ref(path), 'detached')
        self.assertEqual(head_ref(mine), B)
        self.assertIn(f'{B} is checked out in {mine}', self.new_log())

    def test_a_branch_git_cannot_create_falls_back_to_detached(self):
        git(self.repo.clone, 'branch', 'feat', self.repo.main)  # refs/heads/feat blocks refs/heads/feat/rounding
        path = self.checkout(self.repo.page(self.repo.c2))
        self.assertEqual(head_ref(path), 'detached')
        self.assertIn(f'checking out {B} failed', self.new_log())

    def test_a_branch_name_git_refuses_stays_detached(self):
        path = self.checkout(self.repo.page(self.repo.c2, head_branch='bad..name'))
        self.assertEqual(head_ref(path), 'detached')
        self.assertIn('no usable head branch name', self.new_log())

    def test_a_local_branch_with_commits_the_review_lacks_is_left_alone(self):  # (c)
        git(self.repo.clone, 'switch', '-q', '-c', B, self.repo.c2)
        mine = commit(self.repo.clone, 'my own work')
        git(self.repo.clone, 'switch', '-q', 'main')
        path = self.checkout(self.repo.page(self.repo.c2))
        self.assertEqual(head_ref(path), 'detached')
        self.assertEqual(rev(self.repo.clone, f'refs/heads/{B}'), mine)
        self.assertIn(f'{B} has commits that {self.repo.c2[:12]} lacks', self.new_log())

    def test_a_re_review_takes_the_branch_from_the_older_worktree(self):  # (d)
        old = self.checkout(self.repo.page(self.repo.c1))
        self.assertEqual(head_ref(old), B)
        new = self.checkout(self.repo.page(self.repo.c2))
        self.assertEqual(head_ref(new), B)
        self.assertEqual(head_ref(old), 'detached')
        self.assertEqual(rev(old, 'HEAD'), self.repo.c1)

    def test_an_older_worktree_with_uncommitted_work_keeps_the_branch(self):  # (d), dirty
        old = self.checkout(self.repo.page(self.repo.c1))
        tracked = next(p for p in old.iterdir() if p.suffix == '.txt')
        tracked.write_text('edited, not committed')
        new = self.checkout(self.repo.page(self.repo.c2))
        self.assertEqual(head_ref(new), 'detached')
        self.assertEqual(head_ref(old), B)
        self.assertEqual(rev(self.repo.clone, f'refs/heads/{B}'), self.repo.c1)
        self.assertIn('uncommitted changes', self.new_log())

    def test_a_branch_behind_the_review_is_fast_forwarded(self):  # (e)
        git(self.repo.clone, 'branch', B, self.repo.c1)
        path = self.checkout(self.repo.page(self.repo.c2))
        self.assertEqual(head_ref(path), B)
        self.assertEqual(rev(self.repo.clone, f'refs/heads/{B}'), self.repo.c2)
        self.assertEqual(git(path, 'rev-parse', '--abbrev-ref', f'{B}@{{upstream}}'), f'origin/{B}')

    def test_a_branch_at_the_reviewed_commit_is_checked_out_as_it_is(self):
        git(self.repo.clone, 'branch', B, self.repo.c2)
        path = self.checkout(self.repo.page(self.repo.c2))
        self.assertEqual(head_ref(path), B)

    def test_a_branch_gone_from_origin_is_still_created(self):  # a merged PR whose branch was deleted
        git(self.repo.origin, 'branch', '-q', '-D', B)
        git(self.repo.clone, 'update-ref', '-d', f'refs/remotes/origin/{B}')
        path = self.checkout(self.repo.page(self.repo.c2))
        self.assertEqual(head_ref(path), B)
        r = subprocess.run(['git', '-C', str(path), 'rev-parse', '--abbrev-ref', f'{B}@{{upstream}}'], capture_output=True)
        self.assertNotEqual(r.returncode, 0)

    def test_a_cross_repo_pr_stays_detached(self):  # (f)
        path = self.checkout(self.repo.page(self.repo.c2, cross_repo=True))
        self.assertEqual(head_ref(path), 'detached')
        self.assertIsNone(rev(self.repo.clone, f'refs/heads/{B}'))
        self.assertIn('cross-repository', self.new_log())

    def test_a_commit_page_without_a_pr_stays_detached(self):  # (f)
        data = self.repo.page(self.repo.c2, pr=None, title='Code review · commit')
        del data['head_branch'], data['cross_repo']
        path = self.checkout(data)
        self.assertEqual(head_ref(path), 'detached')
        self.assertIsNone(rev(self.repo.clone, f'refs/heads/{B}'))
        self.assertEqual(self.new_log(), '')

    def test_prune_keeps_a_worktree_with_uncommitted_changes(self):  # (g)
        newer = commit(self.repo.clone, 'one more')
        for sha in (self.repo.main, self.repo.c1, self.repo.c2, newer):
            git(self.repo.clone, 'worktree', 'add', '-q', '--detach', str(paths.WORKTREES / sha[:12]), sha)
        dirty = paths.WORKTREES / self.repo.main[:12]
        (dirty / 'wip.txt').write_text('unsaved')
        clean = paths.WORKTREES / self.repo.c1[:12]
        os.utime(dirty, (1, 1))
        os.utime(clean, (2, 2))
        self.checkout(self.repo.page(commit(self.repo.clone, 'newest'), pr=None))
        self.assertTrue((dirty / 'wip.txt').exists())
        self.assertFalse(clean.exists())
        self.assertIn(f'kept worktree {dirty}: it has uncommitted changes', self.new_log())

    def fill(self, *shas: str) -> list:
        """Detached tool worktrees of ``shas`` in the clone, oldest first by mtime."""
        trees = []
        for i, sha in enumerate(shas):
            tree = paths.WORKTREES / sha[:12]
            git(self.repo.clone, 'worktree', 'add', '-q', '--detach', str(tree), sha)
            os.utime(tree, (i + 1, i + 1))
            trees.append(tree)
        return trees

    def test_prune_keeps_a_worktree_with_commits_on_its_detached_head(self):
        a, b, _, _ = self.fill(self.repo.main, self.repo.c1, self.repo.c2, commit(self.repo.clone, 'four'))
        orphan = commit(a, 'committed in the fallback worktree')
        os.utime(a, (1, 1))
        self.checkout(self.repo.page(commit(self.repo.clone, 'newest'), pr=None))
        self.assertEqual(rev(a, 'HEAD'), orphan)
        self.assertFalse(b.exists())
        self.assertIn(f'kept worktree {a}: its detached HEAD {orphan[:12]} has commits no branch holds', self.new_log())

    def test_prune_removes_another_repos_worktree_through_that_repo(self):
        other = Repo()
        stray = paths.WORKTREES / other.c1[:12]
        git(other.clone, 'worktree', 'add', '-q', '-b', 'held', str(stray), other.c1)
        os.utime(stray, (0, 0))
        self.fill(self.repo.c1, self.repo.c2)
        self.checkout(self.repo.page(commit(self.repo.clone, 'newest'), pr=None))
        self.assertFalse(stray.exists())
        self.assertNotIn(str(stray), git(other.clone, 'worktree', 'list'))
        git(other.clone, 'switch', '-q', 'held')  # the branch is free again


class ResolvePrTests(unittest.TestCase):
    """render.resolve_pr hands the page the PR's head branch, and nothing when gh fails (so the checkout asks again)."""

    def test_the_head_branch_and_cross_repo_flag_are_returned(self):
        head = {}
        with support.env(STUB_GH_HEAD_REF=B):
            render.resolve_pr(support.WORK, 4242, [], 'acme/billing', head)
        self.assertEqual(head, {'head_branch': B, 'cross_repo': False})

    def test_a_failed_lookup_returns_no_branch(self):
        head = {}
        with support.env(STUB_GH_FAIL='1'):
            render.resolve_pr(support.WORK, 4242, [], 'acme/billing', head)
        self.assertEqual(head, {})


if __name__ == '__main__':
    unittest.main()
