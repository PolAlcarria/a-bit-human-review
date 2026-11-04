#!/bin/sh
# Records docs/demo.gif (PR mode: post comments to a pull request), docs/demo-local.gif (your own code: fix) and
# docs/demo-diff.gif (the diff review: ask about lines, request a change, send it to the session).
# Usage: demo/record.sh [pr] [local] [diff]   (no argument records all three)
# Needs chromium (or CHROMIUM=<path>), node 22+ and ImageMagick (magick, or convert for ImageMagick 6). Runs against a
# throwaway HOME, data folder and port (see CONTRIBUTING.md), never the live ones, and without Claude or GitHub:
# demo/stubs/claude (over tests/stubs/claude) and demo/stubs/gh answer in their place. DEMO_CDN_DIR: see record.mjs.
set -eu
want=${*:-pr local diff}
on() { case " $want " in *" $1 "*) return 0 ;; esac; return 1; }
im=magick; command -v magick >/dev/null 2>&1 || im=convert
here=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)
export HOME="$work/home" FINDING_VIEWER_DATA="$work/data" FINDING_VIEWER_PORT=47398 FINDING_VIEWER_EXTRACT=0
export PATH="$here/demo/stubs:$here/tests/stubs:$PATH"
export STUB_CLAUDE_STRUCTURED="$here/demo/drafts.json" STUB_CLAUDE_SEND=ok STUB_CLAUDE_SLEEP=2 IM="$im"
export DEMO_REPO="$work/billing"
mkdir -p "$HOME/.claude/projects" "$HOME/.claude/sessions"
cd "$here"
stop() { curl -s -X POST -H "X-Finding-Viewer: 1" 127.0.0.1:47398/api/shutdown >/dev/null 2>&1 || true; }
trap 'stop; rm -rf "$work"' EXIT

gif() {  # gif <frames folder> <out>; DEMO_KEEP=<folder> also keeps the frames
  [ -z "${DEMO_KEEP:-}" ] || { mkdir -p "$DEMO_KEEP"; cp -r "$1" "$DEMO_KEEP/"; }
  (cd "$1" && OUT="$2" xargs -a gif-args.txt -d '\n' sh -c '"$IM" "$@" -resize 800x -dither None -fuzz 1% -colors 64 -layers OptimizeTransparency -loop 0 "$OUT"' sh)
  ls -lh "$2"
}

# PR mode: a scratch git repo that holds the PR, and the stubs for gh and claude
if on pr; then
url=$(python3 demo/pr_env.py "$work")
node demo/record.mjs pr "$url" "$work/pr"
gif "$work/pr" "$here/docs/demo.gif"
stop
fi

# Your own code: the same review, no PR
if on local; then
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
stop
fi

# The diff review: a scratch repo with a branch to read, Claude's answer from demo/chat-reply.md
if on diff; then
# The server (and with it the side chat's claude) starts here, so it gets DEMO_CHAT_REPLY.
url=$(DEMO_CHAT_REPLY="$here/demo/chat-reply.md" python3 demo/diff_env.py "$work")
node demo/record.mjs diff "$url" "$work/diff"
gif "$work/diff" "$here/docs/demo-diff.gif"
fi
