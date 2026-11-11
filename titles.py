"""Page titles, worked out from the review data alone, so every page of the same kind reads the same.

One rule for every source (a chat review, ReportFindings, a saved report, a re-review, a diff review):

- a code review of a PR:            ``Code review · PR #42``
- a code review of a branch:        ``Code review · my-branch``  (the working tree's branch)
- a code review of a pinned commit: ``Code review · commit 1a2b3c4d``  (a saved report's commit, a cached clone)
- a diff review:                    ``Diff review · my-branch``  (``commit 1a2b3c4d`` on a detached HEAD)

The repository is not in the title: the subtitle and the list of reviews show it. ``render`` sets the title when it
builds a page, and ``store.page_data`` applies the same rule to every page it reads, so pages built by older versions
read the same way without their files being rewritten.
"""
from __future__ import annotations

import re

CODE = 'Code review'
DIFF = 'Diff review'
SEP = ' · '

# The reviewed code as render labels it: "working tree (main)", "commit 1a2b3c4d" or, on a diff review,
# "commit 1a2b3c4d (main)".
_WORKING_TREE_RE = re.compile(r'^working tree \((.+)\)$')
_COMMIT_RE = re.compile(r'^commit ([0-9a-f]{4,40})(?: \((.+)\))?$')


def _commit(sha: str | None) -> str:
    return f'commit {sha[:8]}' if sha else ''


def _usable_branch(branch: str | None) -> str:
    branch = (branch or '').strip()
    return '' if branch in ('', 'HEAD') else branch


def code_title(pr: int | None = None, branch: str | None = None, commit: str | None = None,
               fallback: str | None = None) -> str:
    """A code review's title: its PR, else the pinned commit, else the working tree's branch, else ``fallback``
    (the repository's folder name)."""
    if pr:
        return f'{CODE}{SEP}PR #{int(pr)}'
    subject = _commit(commit) or _usable_branch(branch) or (fallback or '').strip() or 'working tree'
    return f'{CODE}{SEP}{subject}'


def diff_title(branch: str | None = None, head: str | None = None) -> str:
    """A diff review's title: the branch, or the head commit on a detached HEAD."""
    return f'{DIFF}{SEP}{_usable_branch(branch) or _commit(head) or "working tree"}'


def page_title(data: dict) -> str:
    """The title of a stored page, from its review data (``review.json``): the same rule ``render`` builds pages with.
    Works on pages of older versions, which have no ``branch`` field: the branch is read from ``ref_label``."""
    label = str(data.get('ref_label') or '').strip()
    tree, commit = _WORKING_TREE_RE.match(label), _COMMIT_RE.match(label)
    branch = data.get('branch')
    if branch is None:
        branch = tree.group(1) if tree else (commit.group(2) if commit else None)
    if data.get('kind') == 'diff':
        return diff_title(branch, data.get('ref') or data.get('blob_ref'))
    if data.get('pr'):
        return code_title(pr=data['pr'])
    ref = data.get('ref') or (commit.group(1) if commit else None)
    if not ref and not _usable_branch(branch):
        # A working tree on a detached HEAD: the commit it sat at.
        ref = data.get('blob_ref')
    return code_title(branch=branch, commit=ref, fallback=data.get('repo_name'))
