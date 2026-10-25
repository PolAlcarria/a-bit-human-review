# Contributing

Notes for changing this tool: the rules, the tests, and how to try a server by hand. How the tool works is in
[docs/behaviour.md](docs/behaviour.md), and the map of the code is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Rules

- Python standard library only. No pip, no npm, no requirements file.
- Python 3.9 syntax: no `match`, no `X | Y` in `isinstance`, no parenthesised context managers.
- Keep to the [import rule](docs/ARCHITECTURE.md#modules): features import shared modules, never another module's
  `_private` name, and nothing imports `finding_viewer`.
- Behaviour goes in [docs/behaviour.md](docs/behaviour.md); the map goes in
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Change whichever of the two a change makes wrong.

## Tests

From the repo root:

```sh
python3 -m unittest discover -s tests -v
```

`tests/support.py` clears every `FINDING_VIEWER_*` variable for the tests, so the suite runs the same inside a Claude
Code session. A hand-run `finding_viewer.py hook` or `inject` stays quiet while `FINDING_VIEWER_CHILD` is set: unset it
for those.

`tests/support.py` is imported first by every test module. It sets up:

- a fresh scratch data folder in `FINDING_VIEWER_DATA`;
- a fake `HOME`, with an empty `~/.claude/projects` and `~/.claude/sessions`;
- a free port in `FINDING_VIEWER_PORT`;
- `tests/stubs` (a fake `claude` and `gh`) first on `PATH`;
- `GIT_CEILING_DIRECTORIES` set to the scratch folder, so git finds no real repository.

Run one file with `python3 -m unittest discover -s tests -p 'test_jobs.py' -v`. On a Mac, also run the suite under
`/usr/bin/python3` (3.9).

## SERVER_VERSION

`triage.SERVER_VERSION` is computed at import: a SHA-1 (first 12 hex digits) over every `*.py` beside `triage.py` and
every file under `web/` (dotfiles left out). `triage.ensure_server` replaces a running server whose version differs, so
any change to the code restarts the server at the next open; there is nothing to bump. Job workers are new processes and
run the new code at once. `SERVER_VERSION_OVERRIDE` (`None` in `triage.py`) pins the version: while it is a string, that
string is the version, and code changes no longer restart a running server. Setting it edits `triage.py`, so the next
open restarts once; set it back to `None` when the pin is no longer needed (the tests check that it is `None`).

## The page

The review page is `web/viewer.html`, `web/viewer.css` and `web/viewer.js`, and the list page is `web/index.html`;
`web/shared.css` is shared by both. `reviews.viewer_template()` and `reviews.index_page()` inline them at fill time, so
edit the files under `web/` and keep the placeholders (`/*__SHARED_CSS__*/`, `/*__CSS__*/`, `/*__JS__*/`,
`/*__DATA__*/`, `__NONCE__`, `__TITLE__`).

## A test server by hand

This runs the server from a copy of the working tree, with a fake home, the stubs and a test port, so nothing reaches
the live data or port 47321. From the repo root, copy the tracked and untracked files that git does not ignore:

```sh
mkdir -p /tmp/abhr-try && git ls-files -co --exclude-standard | tar -cf - -T - | tar -xf - -C /tmp/abhr-try
mkdir -p /tmp/abhr-try-home/.claude/projects /tmp/abhr-try-home/.claude/sessions
(
  cd /tmp/abhr-try
  export HOME=/tmp/abhr-try-home
  export FINDING_VIEWER_DATA=/tmp/abhr-try-data
  export FINDING_VIEWER_PORT=47399
  export PATH="$PWD/tests/stubs:$PATH"     # fake claude and gh, first on PATH
  python3 finding_viewer.py serve &
  sleep 1
  curl -s 127.0.0.1:47399/api/ping         # {"app": "finding-viewer", "version": ..., "pid": ...}
)
```

The subshell keeps the variables to itself, so your own shell keeps its real `HOME` and `PATH`. The stubs answer:

- `claude --version`: `9.9.9 (Claude Code stub)`. Any other `claude` call gets one canned stream-json reply.
- `gh api user`: the login. `gh auth status`: logged in, or exit 1 when `STUB_GH_AUTH=0`.
- `gh pr view`: PR 4242 of acme/billing.
- `gh api repos/O/R/pulls/N/reviews`: an empty list; with `--method POST`, a posted review's id and URL.

Any other `gh` call exits 1. To stop the server, send
`curl -s -X POST -H 'X-Finding-Viewer: 1' 127.0.0.1:47399/api/shutdown`. Every request needs a `Host` of
`127.0.0.1:47399` or `localhost:47399`, and every write route that header too.

## Never

- Run anything against the live data folder (`~/.local/share/a-bit-human-review`, or the plugin's data folder) or port
  47321, from a test or a try-out. The inject hook reads the recent `out/*/decisions.json` and `out/*/chat.json`
  files, so a test page there reaches real sessions.
- Put real review pages, or code from `out/`, `clones/` or `worktrees/`, into fixtures, docs or commits. Write
  synthetic fixtures instead.
- Drive a browser without asking the maintainer first. When it is agreed, use headless Chrome only, with
  `--use-mock-keychain`. Until then, check the front end by reading the code and with `curl` against a test server.
- Remove the `file://` fallback of the page (`SERVED` in `web/viewer.js`), or start a child `claude` without
  `FINDING_VIEWER_CHILD=1`.

## The plugin

After changing `.claude-plugin/`, `hooks/` or `skills/`:

- run `claude plugin validate .`;
- keep the `env CLAUDE_PLUGIN_DATA=${CLAUDE_PLUGIN_DATA}` prefix of the `!` line and the `allowed-tools` rule in
  `skills/findings/SKILL.md`: Claude Code substitutes `${CLAUDE_PLUGIN_DATA}` in a skill's text but does not export it
  to the skill's shell (the hooks do get it), so without the prefix the command would use another data folder than the
  hooks. A bare `CLAUDE_PLUGIN_DATA=… python3 …` prefix is refused by the permission check; `env …` with the same
  literal rule is allowed. `install` strips the prefix for the manual command;
- try the plugin in place with `claude --plugin-dir .`, then run `/reload-plugins` in that session;
- run `/a-bit-human-review:findings doctor` inside the `claude --plugin-dir .` session: it warns when the manual
  hooks are registered too (each hook would then run twice).
