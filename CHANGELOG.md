# Changelog

## [Unreleased]

### Added
- `examples/`: a synthetic review (`review.md`) with the two small files it points at, to try the viewer without a real review.
- `docs/demo.gif` (PR mode: post, draft, approve) and `docs/demo-local.gif` (your own code: fix), shown in the README,
  and `demo/record.sh` to re-record them: headless Chromium driving the page with smooth zooms, ImageMagick for the
  GIF, on a throwaway HOME, data folder and port, with `demo/stubs/gh` and the test stub `claude` in place of GitHub
  and Claude. Needs `chromium`, Node 22+ and `magick`.
