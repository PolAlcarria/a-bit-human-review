# a-bit-human-review: architecture review and refactoring brief

**Status, 2026-10-20.** Steps 1 to 5 of section 4 have landed on `main`: the data directory (F1), the tests (F3), the
shared modules and one job state machine (F4, F6, F12), and the plugin, `doctor`, the config file and the rewritten docs
(F2, F5, F10, F11), and the storage layout, template split and version hash (F7, F8, F9). The findings below describe
the code as it was reviewed on 2026-10-19.

Reviewed on 2026-10-19 at commit `d252349` plus the uncommitted changes in the working tree (README, ARCHITECTURE,
draft.py, index_template.html, reviews.py, triage.py, viewer_template.html). Two goals set by Pol: make the
architecture clearer, and make the tool installable by other Claude Code users without hand-holding.

This document is the brief for the implementor and reviewer sessions that do the work. Each step in section 4 is
meant to be handed to one implementor and one reviewer as is. Line numbers are left out on purpose; search for the
function names. Section 5 lists the guardrails every session must follow.

## 1. Snapshot

| | |
|---|---|
| Python | 10 flat modules, about 4,700 lines, standard library only |
| Front end | `viewer_template.html` 2,188 lines (CSS 325, inline JS 1,785, 123 functions), `index_template.html` 360 lines |
| Docs | README 74 lines, ARCHITECTURE 502 lines, PRODUCT 98 lines (untracked) |
| Tests | none |
| History | 19 commits in 11 days, one author |
| Wiring | two hooks in `~/.claude/settings.json` and `~/.claude/commands/findings.md`, all with the absolute path `~/Repos/a-bit-human-review/finding_viewer.py` |
| Runtime data in the repo | `out/` 1.7 MB, `clones/` 39 MB, `worktrees/` 218 MB, `state.json`, `inject_state.json`, `settings.json`, `viewer.log` |

Processes: the two hooks (short-lived, must never block), `render` (detached, runs `claude -p` to read the findings),
`serve` (detached `ThreadingHTTPServer` on 127.0.0.1:47321, replaced when `SERVER_VERSION` changes, exits after 8 idle
hours), and `job` workers (detached: draft, post + tell, chat reply, re-review).

Import graph today (arrow = imports):

```
finding_viewer ─► extract, reviews, triage        triage ─► chat, freshness, draft, jobs, rereview, reviews
                                                  triage.cmd_job ─► finding_viewer (lazy, to break the cycle)
reviews  ─► chat, freshness, draft, jobs, rereview
rereview ─► chat, draft, extract, freshness, jobs
draft    ─► chat, jobs, send          freshness ─► chat, jobs        extract ─► chat        send ─► chat
chat     ─► jobs                      jobs ─► (nothing)
```

## 2. Keep: what is sound

- **The process model.** Hooks return at once and swallow their own errors; render, server and workers are detached
  with `start_new_session=True`; a server restart kills nothing. Keep this shape through every refactor.
- **The write discipline.** Every shared file is replaced atomically (`jobs.write_json`, `write_page`,
  `triage.do_PUT`) under an `fcntl.flock` sidecar; the re-review stages complete files and commits them in a recorded
  order under the page lock. Keep it.
- **The job invariants.** A worker writes only while the state still carries its job id; `stopping` is marked before
  the kill; the pid is recorded right after spawn; a lost worker is settled on the next read. These are right; they
  are just written three times (F6).
- **One read path for page data.** `chat.page_data` is the only way review data is read (23 call sites). Swapping the
  storage behind it is contained (F7).
- **The server's posture.** Host allow-list, custom header on writes, CSP with per-response nonce, SRI on the cdnjs
  libraries, page names validated and resolved inside `out/`. Right for a localhost tool; keep.
- **The docs describe the real behaviour.** Nothing in README or ARCHITECTURE was found to be false. The problem is
  shape, not truth (F5).

## 3. Findings, ranked

Each finding gives the evidence (where to look), why it matters for the two goals, and the fix. F1 to F5 are the
ones that block the goals; F6 to F12 are the clarity debt behind them.

### F1. Runtime data lives inside the source folder

**Where.** `HOME = Path(__file__).resolve().parent` in `finding_viewer.py`, `triage.py`, `jobs.py`, `reviews.py` and
`chat.py`; `OUT = chat.OUT` in `draft.py`, `freshness.py`, `rereview.py`. `OUT`, `CLONES`, `WORKTREES`, `STATE`,
`INJECT_STATE`, `SETTINGS`, `LOG`, `TEMPLATE`, `INDEX` all hang off `HOME`. `.gitignore` exists mostly to hide this.

**Why it matters.** Any install where the code is copied or read-only breaks: the Claude Code plugin cache is a new
directory per version, swept 14 days after an update. A `git pull` into a folder holding 260 MB of clones and
worktrees is unpleasant. The repo folder and the data folder are the same thing, which is the root of several other
findings (F2, F7, F9).

**Fix.** One module, `paths.py`, resolved once at import:

- `CODE`: the package directory (templates, the child script passed to `subprocess.Popen` in `launch_render`,
  `ensure_server` and `jobs.spawn`). This stays `Path(__file__).parent`.
- `DATA`: the runtime root. Order: `FINDING_VIEWER_DATA` env var, else `CLAUDE_PLUGIN_DATA` env var (set by Claude
  Code for plugin hooks, see section 6), else `~/.local/share/a-bit-human-review`. Created on first use.
- `OUT`, `CLONES`, `WORKTREES`, `LOG`, `STATE`, `INJECT_STATE`, `SETTINGS` (and later `CONFIG`) under `DATA`.
- `log(prefix)` factory, replacing the four `log()` copies (F4).

Migration: a `migrate` subcommand, also run automatically by `serve` and `hook` when `DATA/out` is missing and
`CODE/out` exists: move `out/`, `state.json`, `inject_state.json`, `settings.json`, `viewer.log`; re-point
`out/latest.html`. Do not move `clones/` and `worktrees/`: both are caches rebuilt on demand (`cached_clone`,
`review_checkout`), and worktrees are registered in the reviewed repo by absolute path, so moving them needs
`git worktree repair`. Run `git worktree prune` in each reviewed repo instead and delete the old folders. Known
gap already in ARCHITECTURE: `chat.json` stores each thread's `cwd` absolute; threads that ran in an old worktree
lose resume, new ones are unaffected.

### F2. Install is three hand edits with an absolute path, and nothing checks the machine

**Where.** `~/.claude/settings.json` → `hooks.Stop` and `hooks.UserPromptSubmit` run
`python3 ~/Repos/a-bit-human-review/finding_viewer.py hook|inject`; `~/.claude/commands/findings.md` has
`allowed-tools: Bash(python3 ~/Repos/a-bit-human-review/finding_viewer.py:*)` and the same path in its `!` line.
README, "Wiring into Claude Code", tells the user to edit all three.

Requirements the code has and no document states: Python 3.9 or newer (`str.removeprefix`,
`Path.is_relative_to`; no 3.10 syntax is used), macOS or Linux (`fcntl.flock` in `jobs.locked`, `ps -o command=` in
`jobs.is_worker`, `open`/`xdg-open` in `open_url`), `gh` logged in, `claude` on PATH or at `~/.local/bin/claude`.
Failure mode: `cmd_hook` catches every exception, writes it to `viewer.log` and returns 0, so a user with Python 3.8
sees nothing happen at all.

**Fix, part A: ship as a Claude Code plugin** (facts verified against the docs on 2026-10-19, section 6):

- `.claude-plugin/plugin.json` with name, description, version.
- `hooks/hooks.json` with the Stop and UserPromptSubmit hooks running
  `python3 "${CLAUDE_PLUGIN_ROOT}/finding_viewer.py" hook|inject` with the current timeouts.
- `skills/findings/SKILL.md` carrying today's `findings.md` body, with `${CLAUDE_PLUGIN_ROOT}` in the `!` line and in
  `allowed-tools`. Skills are the documented home for `${CLAUDE_PLUGIN_ROOT}` substitution in `allowed-tools`;
  `commands/*.md` is the older format and that substitution is not documented for it.
- `.claude-plugin/marketplace.json` at the repo root, so the GitHub repo is its own marketplace. Install is then
  `claude plugin marketplace add <owner>/a-bit-human-review` and `claude plugin install <name>@<marketplace>`.
  Local development: `claude --plugin-dir .` and `/reload-plugins`.

Three traps:

1. The command is namespaced: `/<plugin-name>:findings`. `MENTION_RE` in `finding_viewer.py` does not match that
   form: its `a-bit-human-review` alternative is guarded by `(?<![\w/.-])` and the slash precedes it, and there is no
   `/findings` substring. Add an alternative for `/<plugin-name>:findings`. `FINDINGS_SKILL_RE` already accepts a
   `prefix:`, so `ran_findings` is fine; `FINDINGS_BASH_RE` matches on `finding_viewer.py`, so it is fine too.
2. Plugin hooks merge with `settings.json` hooks. While both exist, every hook runs twice and two pages open. The
   manual wiring must be removed once the plugin is verified; `doctor` (part B) must warn when both are present.
3. `${CLAUDE_PLUGIN_ROOT}` changes with every plugin version. Nothing may be stored under it (F1).

Keep the manual wiring documented as the fallback for people who do not use plugins; `install` and `uninstall`
subcommands can write and remove the three entries idempotently, using the script's own resolved path.

**Fix, part B: a `doctor` subcommand** that prints one PASS/WARN/FAIL line each for: Python version, `git`,
`gh auth status`, `claude` found and `claude --version`, `DATA` writable, hooks registered (plugin or manual, warn on
both), the slash command present, server reachable on the configured port and its version, and the effective
configuration (F10). README's install section ends with "run doctor".

### F3. There are no tests

**Where.** No `tests/`, no `test_*.py`, no `unittest` or `pytest` import anywhere. A Chrome DevTools harness and a
server-side job test recipe exist only in session notes, not in the repo.

**Why it matters.** 4,700 lines with three job state machines, file locks, diff line mapping and transcript parsing.
A contributor cannot change anything safely, and a fresh install cannot prove it works. The refactors below (F4, F6,
F7) are unsafe without a net.

**Fix.** `tests/` on `unittest` (stdlib rule; `pytest` may run them but must not be required), run with
`python3 -m unittest discover -s tests -v`. Three layers:

1. **Pure functions, with fixtures.** `parse_findings`, `select_findings`, `assign_ids` (headings, bold labels,
   `[C1]` and `W2.` ids, fenced code never parsed, a table yields nothing); `parse_patch` (hunk ranges, removed map);
   `freshness.old_side_hunks`, `map_line`, `_hits` (pure insertion, deletion, rename, a removed `--` SQL comment
   line, non-ASCII path); `read_turns` on a fixture JSONL (user, assistant, sidechain skipped, `isMeta` peer
   hand-back, `hook_additional_context` attachment); `find_pr`, `handed_back`, `names_viewer`, `ran_findings`;
   `rereview.parse_answer`, `next_id`, `carry_triage`; `draft.without_level`, `with_level`, `in_diff`;
   `extract.to_findings`; `reviews.fill_template` (nonce replaced before data, `<` escaped in the payload).
2. **Server smoke.** Start `triage.serve` in a thread on a free port with `DATA` pointed at a temp dir (needs F1;
   until then, set the module constants in the test): `/api/ping`; `PUT` then `GET /api/decisions/<page>`; 403 on a
   missing `X-Finding-Viewer` header and on a foreign `Host`; 423 on a write while a fake `rereview.json` says
   `reviewing`; 409 on a stale `rereviews` count.
3. **Job workers with stubs.** A `tests/stubs/` directory with fake `claude` and `gh` executables that replay
   recorded stream-json and JSON answers, a fake `HOME` with a fake transcript and sessions file, and a helper that
   starts a test server on port 47399 from a scratch copy. This is the recipe already proven by hand on server v17;
   it becomes code.

### F4. The chat module is the shared kernel in disguise, and the kernel is duplicated

**Where.** From the other modules: `chat.page_data` 19 times, about thirty distinct `chat.*` symbols in draft,
extract, freshness, rereview, reviews, send and triage, most of them unrelated to chat (`page_data`, `log`,
`reviewed_ref`, `review_session`, `transcript`, `live_session(s)`, `delete_session`, `review_checkout`,
`PARENT_SESSION_ENV`, `READ_ONLY_TOOLS`, `SEVERITY_LABEL`, `OUT`, `LOG`, `WORKTREES`, `_now`, `describe_tool`).
Likewise `draft.run_fork` and `draft.in_diff` from rereview, `draft.delete_fork` (a one-line wrapper of
`chat.delete_session`) from reviews and rereview, `extract.SEVERITIES`, `extract.LETTER`, `extract.MAX_LOCATIONS`
from rereview, and three private functions used across modules: `freshness._viewer_login` (in `triage.own_code`),
`freshness._has_commit` and `freshness._hits` (in rereview).

Duplicates: `def log` four times (chat, finding_viewer, rereview, triage, two with a prefix); `HOME`/`OUT`/`LOG` in
five modules; `SEVERITY_LABEL` twice (chat, triage); `LETTER` and `ID_LETTER` twice (extract, finding_viewer);
`shutil.which('claude') or ~/.local/bin/claude` eight times; the stream-json reading loop three times (`chat._run`,
`draft.run_fork`, `send.deliver`: tempfile for stderr, `threading.Timer` watchdog, init/assistant/result events); the
`--settings '{"disableAllHooks": true}' --strict-mcp-config` plus env scrub plus `FINDING_VIEWER_CHILD=1` preamble
five times. `finding_viewer.py` mixes CLI, hook, markdown parser, repo lookup and renderer, which is why
`triage.cmd_job` needs a lazy `import finding_viewer`.

**Fix.** Shared modules that import nothing from the features:

| Module | Takes over |
|---|---|
| `paths.py` | `CODE`, `DATA`, every path constant, `log(prefix)` (F1) |
| `severity.py` | `SEVERITIES`, `LETTER`, `LABEL`, `SEVERITY_WORDS`, `LETTER_SEVERITY` |
| `claude_cli.py` | `binary()`, `child_env()`, `common_args()`, `run_json()` (extract), `run_stream()` yielding events with the watchdog and stderr capture (chat, draft, send, rereview), `describe_tool` |
| `sessions.py` | `transcript`, `review_session`, `live_sessions`, `live_session`, `delete_session`, `resume_command`, `PARENT_SESSION_ENV` |
| `store.py` | `page_data` and its cache, `decisions_path`, `load_decisions`, `chat_path`, `page_version`, `reviewed_ref`, `review_checkout` and worktree pruning |
| `jobstate.py` | the job state machine (F6) |

Feature modules keep their names and their public functions: `hook.py` (today's transcript → spec half of
finding_viewer: `read_turns`, `find_pr`, `spec_from_*`, `names_viewer`, `handed_back`, `fingerprint`,
`already_shown`), `parse.py` (the regex markdown parser), `render.py` (`locate_repo` through `build_page`,
`write_page`, `render`, `prune`), `finding_viewer.py` (CLI dispatch only), `triage.py` (server and routes),
`handoff.py` (`summarize`, `cmd_inject`, `cmd_decisions`, `mark_handed`, today in triage), `extract.py`, `chat.py`,
`draft.py`, `rereview.py`, `freshness.py`, `reviews.py`, `send.py`, `jobs.py`.

Import rule, to write into ARCHITECTURE: `finding_viewer` → features → shared; shared never imports a feature;
features import each other only through public names (no `_private` across modules). The lazy import in
`cmd_job` goes away because `render` no longer imports `triage`.

This is a move, not a rewrite: behaviour stays byte-identical, `SERVER_VERSION` is bumped once, the job spec format
is unchanged so workers started by the old server keep working.

### F5. The docs explain everything and map nothing

**Where.** `docs/ARCHITECTURE.md`: 502 lines of narrative, single bullets of 20 to 25 lines (section 4, "Review the
new commits"; section 8, "The update"), no module graph, no data model for the page JSON, no lifecycle of a job; much
of it repeats the module docstrings word for word. `README.md`: "No install step" in the intro, then a table of three
hand edits at line 45; the best map of the code (the Files table) is in README, not ARCHITECTURE.

**Fix.**

- README, in this order: what it is in three lines; requirements (Python 3.9+, macOS or Linux, `git`, `gh` logged
  in, `claude`); install (plugin: two commands; manual: `install` subcommand or the three edits); first use; costs
  and the env vars; links. Under 100 lines.
- ARCHITECTURE: process diagram; module graph with the import rule; data model (fields of the page JSON,
  `decisions.json`, `chat.json`, `drafts.json`, `rereview.json`, `freshness.json`); one job lifecycle diagram
  (begin → running → done | failed | stopped, with lost-worker settlement); the route table and the state files
  table as they are; known gaps as they are. Target 200 lines.
- `docs/behaviour.md`: the narrative sections of today's ARCHITECTURE that are worth keeping (detection rules, the
  re-review update order, the draft flow), one heading per feature, so the module docstrings can shrink to a
  summary and a pointer.
- `CONTRIBUTING.md`: run the tests, bump `SERVER_VERSION` (until F9), the test server recipe (scratch copy, fake
  HOME, stubs, port 47399), never touch the live `out/` or port 47321, browser checks only with
  `--use-mock-keychain` and after asking.

### F6. Three copies of one job state machine

**Where.** `draft.py`: `_load`, `_write`, `_stalled`, `_fail_stalled`, `state`, `_update`, `_mark_stopped`, `stop`,
`activity`. `rereview.py`: the same names, plus staging. `chat.py`: `_stalled`, `_end_pending`, `settled`,
`_update_pending`, `_finish`, `_stopped_message`, `stop`, `activity`, per thread. The invariants (job id gate,
`stopping` before kill, pid right after spawn, settle lost workers on read, `end_group` and `drop_spec`) are repeated
with small differences.

**Fix.** `jobstate.py`: `begin(path, kind, **fields)`, `update_if(path, job, **fields)`, `settle(path, on_lost)`,
`stop(path, job, kill)`, `running_row(...)` and `ended_row(...)` for the Running panel. Draft, re-review and chat
keep their own fields, messages and the staging. About 200 lines fewer and one place for the invariants. Only after
F3's layer 3 exists.

### F7. The HTML page is the database, and each review is six to nine flat files

**Where.** `chat.page_data` regex-reads `<script type="application/json" id="data">` out of `out/<page>.html`;
`reviews.page_html` re-fills the current template on every request, so the stored HTML is a data file with a stale
template around it. `reviews.delete_review` unlinks a ten-item list; `prune`, `recent_pages`, `group_pages`,
`list_reviews` glob `2*.html`, `*.decisions.json`, `*.chat.json`; `rereview._stage` writes `.staged-<job>` siblings
and `_commit` replays them.

**Fix.** `out/<page>/` with `review.json`, `decisions.json`, `chat.json`, `freshness.json`, `drafts.json`,
`rereview.json`, a `locks/` folder and, for the `file://` fallback only, `page.html` written at render. Delete is
`rmtree`, list is `iterdir`, staging is `out/<page>/.staging-<job>/` plus renames. `page_data` reads `review.json`.
A migration for the current pages is about 30 lines today and a support burden once anyone else has data, so this
goes in before the first external user, right after F1 or with it.

### F8. One 2,188-line template

**Where.** `viewer_template.html`: 325 lines of CSS, 1,785 lines of JS in one inline script, 123 functions, module
globals `D` (page data) and `T` (triage); the `SERVED` flag keeps the `file://` fallback working. 17 CSS lines are
duplicated with `index_template.html`.

**Fix.** `web/viewer.html`, `web/viewer.css`, `web/viewer.js`, `web/index.html`, `web/shared.css`;
`reviews.fill_template` inlines CSS and JS at fill time, so the served page stays a single file with the CSP nonce and
the `file://` fallback intact. A later step splits `viewer.js` by panel (triage, chat, drafts, re-review, activity,
freshness) and concatenates. Verification is by code reading and curl unless Pol approves a headless Chrome run.

### F9. The server version is a hand-bumped integer

**Where.** `SERVER_VERSION = 19` in `triage.py`, compared by `ensure_server`; bumped in 10 of 19 commits. A user who
updates to a build where it was not bumped keeps the old server for up to 8 hours.

**Fix.** Derive it at import from a hash of every `*.py` and template under `CODE` (or the newest mtime), keep an
integer only as a manual override. `/api/ping` and `ensure_server` compare the string as before.

### F10. Configuration is eight env vars plus a settings file

**Where.** `FINDING_VIEWER_PORT`, `_REPO_DIRS`, `_EXTRACT`, `_EXTRACT_MODEL`, `_DRAFT_MODEL`, `_REREVIEW_MODEL`,
`_CHAT_MODEL`, `_CHILD`; `settings.json` holds model, context and shortcuts, edited from the page. Hooks inherit
Claude Code's environment, so a user cannot set these per tool, and the port must agree between the hook process
and the server.

**Fix.** `DATA/config.json` with `port`, `repo_dirs`, `extract.enabled`, `extract.model`, `draft_model`,
`rereview_model`, `chat_model`, `report_glob`, `delete_forks`; the env vars stay as overrides of the same keys;
`doctor` prints the effective values. `FINDING_VIEWER_CHILD` stays an env var: it is a process marker, not a setting.

### F11. Author-specific defaults

**Where.** `REPORT_PATH_RE` and `latest_report` look for `.claude/.history/code-reviews/*/*.md` and skip
`_learnings.md`; `spec_from_report` writes a subtitle naming one review skill; `chat._run` falls back to
`origin/develop` for the merge base in the system prompt; `default_base` tries `develop` before `main`, which is
fine as a generic order.

**Fix.** `report_glob` in config with today's value as the default and documented as optional; a neutral subtitle
("Saved review report"); use `render.default_base(root)` instead of the literal in the chat prompt.

### F12. The seams with Claude Code internals are spread out

**Where.** Transcript JSONL fields (`isSidechain`, `isMeta`, `origin.kind == 'peer'`,
`attachment.type == 'hook_additional_context'`, content blocks) in `read_turns`; `~/.claude/sessions/<pid>.json`
in `chat.live_sessions`; `~/.claude/projects/*/<sid>.jsonl` in `chat.transcript` and `chat.delete_session`;
`CLAUDE_CODE_SESSION_ID` in `find_transcript` and `cmd_decisions`; CLI flags `--fork-session`, `--json-schema`,
`--output-format stream-json`, `--settings`, `--strict-mcp-config`, `--permission-mode dontAsk`, `--add-dir` and the
result fields `structured_output`, `total_cost_usd`, `usage` in five modules.

**Why it matters.** A Claude Code release can change any of these and the tool fails silently (the hook swallows
errors). For other users the blast radius must be one module and one test file.

**Fix.** All of it lives in `sessions.py` and `claude_cli.py` (F4) with fixtures in `tests/` (F3). Two behaviours a
new user must be told about in README, each behind a config switch defaulting to today's behaviour: the tool deletes
fork transcripts under `~/.claude/projects` (`delete_forks`), and it relays a message into a live session through
SendMessage after a post.

### Smaller items

- `viewer.log` has no size cap. Rotate at a few MB in `paths.log`.
- `triage.py`'s `__main__` block only serves; the `on`/`off` legacy message in `main` can go at the next version.
- `PRODUCT.md` and `.impeccable/` are untracked: commit `PRODUCT.md`, add `.impeccable/` to `.gitignore` or commit it.
- `README.md` claims "no install step"; true for Python packages, false for the wiring. Reword with F5.

## 4. The plan

Steps are ordered by dependency. Estimates are for one implementor session with a reviewer pass. Steps that touch
`triage.py` or `viewer_template.html` run one at a time.

### Step 1. Data directory (F1)

- **Scope.** `paths.py`; every module switches to it; `migrate` subcommand and its automatic trigger; `.gitignore`
  shrinks to `__pycache__/` and `*.tmp`; README gets one line about `FINDING_VIEWER_DATA`.
- **Not in scope.** No other refactor, no renames beyond the path constants.
- **Done when.** `grep -n "Path(__file__)" *.py` shows only `paths.py` and the three `Popen` child-script lines;
  a fresh clone with `FINDING_VIEWER_DATA=/tmp/x` runs `serve`, `hook` (with a fixture event on stdin) and
  `decisions` without writing anything into the clone; Pol's existing `out/` is migrated and the live list shows
  the same reviews with their triage.
- **Verify.** `python3 finding_viewer.py doctor` does not exist yet, so: `ls` the clone after the run, `curl
  127.0.0.1:<port>/api/reviews` on a test server (port 47399, scratch copy, fake HOME) before and after migration
  and diff the JSON.
- **Estimate.** Half a day.

### Step 2. Tests (F3)

- **2a, in parallel with step 1.** Layer 1 (pure functions) with fixtures under `tests/fixtures/`. No dependency on
  paths.
- **2b, after step 1.** Layer 2 (server smoke on a free port with `FINDING_VIEWER_DATA` set to a temp dir) and layer
  3 (job workers with `tests/stubs/claude` and `tests/stubs/gh`).
- **Done when.** `python3 -m unittest discover -s tests -v` passes on a clean checkout with no real `out/`, no real
  `~/.claude`, and no network; each fixture file states what shape it covers in a first-line comment.
- **Estimate.** One day.

### Step 3. Shared modules, render split, one job state machine (F4, F6, F12)

- **Scope.** The table in F4; `jobstate.py`; `SERVER_VERSION` bumped once; ARCHITECTURE's import rule written.
- **Rule.** Behaviour-preserving. The reviewer's first check is that the diff is moves and renames: every public
  function keeps its signature or has every caller updated in the same change; `tests/` passes before and after
  with no test edited except import paths.
- **Order inside the step.** `paths` is done; then `severity`, `sessions`, `store`, `claude_cli` (each a
  self-contained move with tests passing), then `render`/`hook`/`parse`/`handoff` out of `finding_viewer` and
  `triage`, then `jobstate` last.
- **Done when.** No module imports a `_private` name from another; `python3 -c "import triage"` has no lazy
  imports; `grep -c "shutil.which('claude')" *.py` is 1; the stream-json loop exists once.
- **Estimate.** One and a half to two days.

### Step 4. Plugin, doctor, config, docs (F2, F5, F10, F11)

- **Scope.** `.claude-plugin/plugin.json`, `hooks/hooks.json`, `skills/findings/SKILL.md`,
  `.claude-plugin/marketplace.json`; `MENTION_RE` extended for the namespaced command; `doctor`, `install`,
  `uninstall`, `migrate` subcommands; `config.json` with env overrides; README, ARCHITECTURE, `docs/behaviour.md`,
  `CONTRIBUTING.md` as in F5.
- **Done when.** `claude plugin validate .` passes; `claude --plugin-dir .` in a fresh session runs `/<name>:findings
  list` and opens the list; a review turn that names the viewer opens one page, not two, with the manual hooks
  removed; `doctor` on Pol's machine prints all PASS and warns when the manual hooks are re-added; a reader of the
  new README can install in under five minutes without opening another file.
- **Estimate.** One day.

### Step 5. Storage layout, template split, version hash (F7, F8, F9)

- **Scope.** `out/<page>/` with `review.json` and a migration; `web/` split inlined by `fill_template`; version from
  a source hash.
- **Done when.** `delete_review` is an `rmtree`; `page_data` reads JSON; the served page is still one file and still
  works opened as `file://`; CSP log is clean on a test server; the server restarts after a code change with no
  manual bump.
- **Estimate.** One to two days.

Total: about six working days.

## 5. Guardrails for implementor and reviewer sessions

- **Standard library only.** No pip, no npm. Python 3.9 syntax: no `match`, no `X | Y` in `isinstance`, no
  parenthesised context managers.
- **No commits unless Pol asks.** Leave the work in the working tree with a summary of files changed.
- **Behaviour-preserving steps stay pure.** Steps 1, 3 and 5 change no behaviour. A bug found on the way is reported
  to Pol, not fixed in the same diff.
- **Bump `SERVER_VERSION`** on any server-side change until step 5 lands, or the live server keeps running old code.
- **Keep the hooks fast.** The Stop hook pre-scans the transcript for `MENTION_RE` before parsing; the inject hook
  reads only recent pages. Nothing added to them may call `git`, `gh` or `claude`.
- **Never run against the live data or port.** Tests and manual checks use a scratch copy of the tree, a fake
  `HOME`, stub `claude` and `gh` first on PATH, and port 47399. The live UserPromptSubmit hook reads every recent
  `out/*.decisions.json`, so a test page in the real `out/` ends up in Pol's real sessions.
- **`out/` holds private code** from reviewed PRs. No file from it goes into fixtures, docs or screenshots; write
  synthetic fixtures.
- **Headless Chrome only after asking Pol**, always with `--use-mock-keychain` and the real `HOME`, one Chrome reused
  across checks. Until then, browser-side items are listed as not verified, with code reading and curl as the
  evidence.
- **Keep the `file://` fallback** (`SERVED` in the page) and the `FINDING_VIEWER_CHILD=1` marker on every child
  `claude`.
- **Split implementors by file; run topics on shared files sequentially.** The reviewer reads `git diff` against this
  document's "done when" lines and sends must-fix findings back to the same implementor.

## 6. Reference: Claude Code plugin facts (verified 2026-10-19)

From `code.claude.com/docs/en/plugins/*`, `/hooks` and `/skills`, read on 2026-10-19.

- **Layout.** Only `plugin.json` sits in `.claude-plugin/`; `hooks/hooks.json`, `commands/`, `skills/`, `bin/`,
  `scripts/` sit at the plugin root. The manifest is optional but sets the name used for namespacing; `name` is the
  only required key, kebab-case, not starting with `claude-` or `anthropic-`.
- **Hooks.** `hooks/hooks.json` has the same shape as the `hooks` block of `settings.json`. `${CLAUDE_PLUGIN_ROOT}` is
  substituted in `command` and `args` and exported to the hook's environment. Timeouts are seconds; the default is
  600 s, 30 s for UserPromptSubmit. Stop and UserPromptSubmit take no matcher. Plugin hooks merge with the user's
  `settings.json` hooks.
- **Commands and skills.** Both support the `!` inline shell and `allowed-tools`; the docs call `commands/` the older
  format and recommend `skills/<name>/SKILL.md`. `${CLAUDE_PLUGIN_ROOT}` is substituted in the Markdown body; for
  `allowed-tools` the substitution is documented for skills. The command is `/<plugin>:<name>`; the user's own
  `/findings` keeps working beside it. A policy `disableSkillShellExecution` turns the `!` lines off for plugins.
- **Install paths.** `claude --plugin-dir <path>` is per session and loads in place; a plugin under
  `~/.claude/skills/<name>/` loads every session in place; a marketplace is a folder or GitHub repo with
  `.claude-plugin/marketplace.json`, added with `claude plugin marketplace add <path|owner/repo>` and installed with
  `claude plugin install <plugin>@<marketplace>`. Marketplace installs are copied to
  `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`; the old version is swept 14 days after an update.
- **Data directory.** `${CLAUDE_PLUGIN_DATA}` resolves to `~/.claude/plugins/data/<id>/`, survives updates, is
  exported to hook processes, and is deleted on uninstall unless `--keep-data` is passed.
- **No install or update hook exists.** `claude plugin validate <path>` is the author-side check. No minimum
  Claude Code version field exists.

## 7. Decisions for Pol

1. **Plugin name and command.** The plugin name becomes the command prefix. `a-bit-human-review` gives
   `/a-bit-human-review:findings`; a plugin named `finding-viewer` gives `/finding-viewer:findings`. Either way
   `MENTION_RE` learns the new form.
2. **Data directory outside a plugin.** `~/.local/share/a-bit-human-review` (proposed) or `~/.claude/a-bit-human-review`.
3. **Manual install.** Keep it documented and scripted (`install`/`uninstall`), or plugin only.
4. **Existing data.** Migrate today's `out/` (proposed) or start fresh.
5. **Step 5 timing.** Before the first external user (proposed) or later.
6. **Fork transcripts.** Keep deleting them by default, behind a `delete_forks` switch (proposed), or default off.
