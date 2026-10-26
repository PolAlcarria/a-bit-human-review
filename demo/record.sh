#!/bin/sh
# Records docs/demo.gif (PR mode: post comments to a pull request) and docs/demo-local.gif (your own code: fix).
# Needs chromium, node 22+ and ImageMagick (magick). Runs against a throwaway HOME, data folder and port (see CONTRIBUTING.md),
# never the live ones, and without Claude or GitHub: tests/stubs/claude and demo/stubs/gh answer in their place.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)
export HOME="$work/home" FINDING_VIEWER_DATA="$work/data" FINDING_VIEWER_PORT=47398 FINDING_VIEWER_EXTRACT=0
export PATH="$here/demo/stubs:$here/tests/stubs:$PATH"
export STUB_CLAUDE_STRUCTURED="$here/demo/drafts.json" STUB_CLAUDE_SEND=ok STUB_CLAUDE_SLEEP=2
export DEMO_REPO="$work/billing"
mkdir -p "$HOME/.claude/projects" "$HOME/.claude/sessions"
cd "$here"
stop() { curl -s -X POST -H "X-Finding-Viewer: 1" 127.0.0.1:47398/api/shutdown >/dev/null 2>&1 || true; }
trap 'stop; rm -rf "$work"' EXIT

gif() {  # gif <frames folder> <out>; DEMO_KEEP=<folder> also keeps the frames
  [ -z "${DEMO_KEEP:-}" ] || { mkdir -p "$DEMO_KEEP"; cp -r "$1" "$DEMO_KEEP/"; }
  (cd "$1" && OUT="$2" xargs -a gif-args.txt -d '\n' sh -c 'magick "$@" -resize 800x -dither None -fuzz 1% -colors 64 -layers OptimizeTransparency -loop 0 "$OUT"' sh)
  ls -lh "$2"
}

# PR mode: a scratch git repo that holds the PR, and the stubs for gh and claude
url=$(python3 demo/pr_env.py "$work")
node demo/record.mjs pr "$url" "$work/pr"
gif "$work/pr" "$here/docs/demo.gif"
stop

# Your own code: the same review, no PR
url=$(python3 - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, '.')
import hook, render, triage
spec = hook.spec_from_report(Path('examples/review.md'))
spec['out'] = str(render.store.html_path('demo-local'))
out = render.render(spec)
assert triage.ensure_server()
print(triage.page_url(out))
PY
)
node demo/record.mjs local "$url" "$work/local"
gif "$work/local" "$here/docs/demo-local.gif"
