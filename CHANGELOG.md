# Changelog

## [Unreleased]

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
