# Changelog

## [Unreleased]

## [0.6.0] - 2026-11-08

### Added
- The `trigger` setting says when the Stop hook opens a page: `explicit` (the default) only when the prompt has
  `/findings` written in it, `mention` also when the prompt names the viewer (the old behaviour), `off` never (only
  `/findings` opens pages). A value outside these three is ignored, and `doctor` says so.
- The `extract.confirm` setting (off by default): on, the Stop hook never starts Claude to read a review (about a
  minute and $0.10–0.30). It says so instead, and `/findings` reads and opens it.
- Each review page has a mode, decided when it is built and shown in the header: **post mode** (someone else's PR:
  Post or Don't post) or **fix mode** (your own code, with no PR or a PR you opened: Fix or Won't fix). A page with a
  PR can switch between them, and the decisions move with it (Post ↔ Fix); a page with no PR is always in fix mode.
  The list of reviews shows each page's mode and counts its decisions in it.
- The `rereview.context` setting: `fork` (the default, as before) re-reviews in a fork of the review session;
  `fresh` in a new session that knows only the findings, the new commits and the PR's comments, which costs far less
  on a long review and works after the review's transcript is gone.
- A re-review stops after 80 turns (`rereview.max_turns`), and can be given a spending limit
  (`rereview.max_budget_usd`, none by default). A run that hits one fails with which limit it was, and the page is
  unchanged. `0` turns either off.
- Before you start a re-review, the banner says what it will read: about how many tokens of the review session's
  context a fork reads again, or that a fresh session reads only the findings and the new commits.
- A re-review says what it cost: its message gives the tokens read and claude's cost ("Reviewed f48a8e88: 1 fixed,
  2 new (412k tokens read, $1.37 (the fork's total))."), and the page keeps them with the re-review (`usage`).
- **Move the lines only (no Claude)**: when the PR's new commits change none of the findings' files, the banner offers
  to bring the page to the new head for free, with the triage carried over, instead of a re-review. It also works
  after the review's transcript is gone. It does not review what the commits added.

### Changed
- **The Stop hook opens a page only when the prompt has `/findings` in it** ("review PR 12, then /findings"). Naming the
  viewer in prose ("open it in the finding viewer") no longer opens one; the hook says to run `/findings` instead. Set
  `"trigger": "mention"` in `config.json` for the old behaviour.
- The project's name alone ("a-bit-human-review") no longer counts as naming the viewer: a session working on this
  repository opened pages, and paid Claude to read them, whenever a prompt mentioned it. "finding viewer" and
  `/findings` (or `/a-bit-human-review:findings`) still count with `trigger` at `mention`.
- The hand-off to the review session follows the page's mode: in post mode it has nothing to fix, in fix mode
  nothing to post. A PR you opened used to mix both ("1 to post, 1 to fix locally, …"). Draft comments and posting
  are refused in fix mode, Send fixes to session in post mode. `/api/handoff` gives `mode` instead of `own_code`.
- A re-review's prompt is shorter: instead of the whole hand-off summary, which listed every finding a second time,
  it carries each finding's decision and note under that finding.
- A re-review no longer quotes the text of a finding the new commits do not touch (the fork has the review already),
  nor anything but the title and the reason of one fixed or dismissed in an earlier re-review.

## [0.5.1] - 2026-11-05

### Changed
- The worktree of a PR review (side chat, re-review) is on the PR's head branch, tracking `origin/<branch>`, instead
  of a detached HEAD, so Orca and `git worktree list` show the branch and you can commit and push from it. It still
  holds exactly the reviewed commit. The branch is created, or fast-forwarded, only when no commit is lost; when it is
  checked out elsewhere, has commits the review lacks, the PR is cross-repository, or git refuses it, the worktree
  stays detached and `viewer.log` says why. This creates local branches in the reviewed repo; pruning a worktree
  leaves its branch.
- Pruning old worktrees never removes one with uncommitted changes, nor one with commits on a detached HEAD that no
  ref holds. A worktree of another reviewed repo is removed through that repo, so its branch is free again there.

## [0.5.0] - 2026-11-05

### Added
- A re-review reads each posted finding's own thread on the PR, with its replies, and your side chat about it, and
  checks what the author answered against the code. A finding the author called fixed that still applies shows
  **Author says fixed · still applies** with what is still missing, one whose fix holds shows **Author's fix
  confirmed**, and one put off shows **Author: later**. The thread shows under the finding ("On the PR · N replies"),
  the job's message counts the claims that do not hold, and the hand-off to the review session names them.
- Posting a review keeps, in `out/<page>/posted.json`, which finding each comment came from. A review posted before
  this release is read from `drafts.json` while it still holds it.

## [0.4.2] - 2026-11-05

### Fixed
- A resumed side chat on a commit whose worktree was pruned (after a re-review, or three newer reviews) answered
  `Error: FileNotFoundError(2, 'No such file or directory')`. It now runs in the page's current checkout and keeps
  that folder for the rest of the thread. When Claude cannot start, the error names the missing path or program.

## [0.4.1] - 2026-11-04

### Fixed
- After a plugin update, or any other change to the code, the review server starts on the first try. It used to fail
  with "The review server did not start" when the old server was still stopping: `ensure_server()` waited a fixed
  0.5 s, and `serve` gave up on the first `Address already in use`. Now `ensure_server()` waits for the old server to
  stop answering, and `serve` retries a busy port for up to 3 s.

## [0.4.0] - 2026-11-04

### Fixed
- A saved report whose PR was pushed after the review opens as a review of that PR again (Post / Don't post), not as
  your own code (Fix / Won't fix). `pr_for_commit()` takes the only non-release PR that still holds the reviewed
  commit when no PR has it as head; with two or more, it finds no PR rather than guess.

## [0.3.2] - 2026-11-04

### Changed
- The list of reviews has two tabs, **Code reviews** and **Diff reviews**, each with its count of open cards, its own
  Done and Archived folds and its own empty state. The address keeps the tab (`/#code`, `/#diff`), "← All reviews" on a
  page goes back to that page's tab, and a diff review card shows its branch, base and size, and its open threads. A
  diff review never shares a card with a code review, and is not checked for freshness.

### Added
- README: an overview of every feature, a section on the list of reviews and `docs/demo-diff.gif`, recorded by
  `demo/record.sh diff`: search the files, ask Claude about two lines, use the answer as a change request and send it to
  the session. `demo/record.sh` takes the GIFs to record (`pr`, `local`, `diff`), falls back to ImageMagick 6's
  `convert`, and `DEMO_CDN_DIR` serves the page's libraries from a local folder where cdnjs cannot be reached.

## [0.3.1] - 2026-11-03

### Changed
- Diff review: the list of files indents each folder level less (9px instead of 14px).

### Added
- Diff review: a file or folder name the list cuts is shown whole on hover or keyboard focus, and **Slide long names**
  (under Files) makes cut names slide to show their end. It is on unless the system asks for reduced motion, and the
  choice is remembered.

## [0.3.0] - 2026-11-03

### Changed
- Diff review: the list of files shows each folder and file on one row instead of two. Folders have a chevron and a
  folder icon, files a file icon tinted by kind, and the threads, the +/− and the status (A/M/D) sit at the right. A
  long name is cut with an ellipsis while its extension stays in sight (the full path is in the tooltip), the +/− give
  way in a narrow list, and deleted files are struck through.

## [0.2.2] - 2026-11-03

### Added
- Review the new commits reads the PR's comments: a finding someone on the PR declined (the change will not be made)
  comes back **dismissed**, is marked Don't post, and is not raised again in later re-reviews, neither as still
  applying nor as a new finding. The page tags it "Dismissed on the PR" with who declined it and why.

## [0.2.1] - 2026-11-03

### Added
- Diff review: the list of changed files is a tree of folders that fold (with Fold all / Unfold all, remembered per page),
  and a search box (`/`) filters the files by name.

## [0.2.0] - 2026-11-03

### Added
- Diff review: `/findings diff [base]` opens every change of the branch since its base (committed or not) as one diff to
  read line by line. Select lines to ask Claude about them or to request a change; threads use the same side chat, and
  **Send changes to session** hands the requested changes to the session. See the README and
  [docs/behaviour.md](docs/behaviour.md#diff-review).
- Chat buttons you set yourself: `chat-buttons.json` in the data folder lists the quick buttons under the chat for code
  reviews (`finding`) and diff reviews (`diff`). `doctor` reports entries it skipped. Example in
  `examples/chat-buttons.json`.
- `examples/`: a synthetic review (`review.md`) with the two small files it points at, to try the viewer without a real review.
- `docs/demo.gif` (PR mode: post, draft, approve) and `docs/demo-local.gif` (your own code: fix), shown in the README,
  and `demo/record.sh` to re-record them: headless Chromium driving the page with smooth zooms, ImageMagick for the
  GIF, on a throwaway HOME, data folder and port, with `demo/stubs/gh` and the test stub `claude` in place of GitHub
  and Claude. Needs `chromium`, Node 22+ and `magick`.
