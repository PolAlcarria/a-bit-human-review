# Changelog

## [Unreleased]

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
