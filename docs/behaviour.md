# a-bit-human-review: behaviour

How each feature behaves, in detail. The map of the code (processes, modules, data model, routes, state files) is in
[ARCHITECTURE.md](ARCHITECTURE.md).

## Detecting a review: the Stop hook

Runs as a Stop hook after every turn, but acts only on a turn whose prompt **names the viewer** (`MENTION_RE`: "finding
viewer", "findings viewer", "finding-viewer", "a-bit-human-review", or `/findings` or `/a-bit-human-review:findings`
inside the text; not inside a path, and not the `/findings` command or its shell output, which `COMMAND_TAG_RE` strips
first; a slash command's arguments do count). A transcript that never names the viewer is not even parsed. `/findings`
is the other way a page opens (`/a-bit-human-review:findings` when installed as a plugin); it runs the same detection on
the latest review turn of the session. `/findings` with no argument opens the latest review in this session, else the
newest saved report in the session's repository (`report_glob`), else the list of all reviews
(`finding_viewer.cmd_open`). With a file path, it opens that report. Nothing else opens a page, so the viewer never runs
on a review you did not ask it for. With the `auto_open` setting off, the hook still builds the page and its
`systemMessage` gives the address, but the browser is left alone: the spec carries `open: false`, which
`finding_viewer.render_and_open` honours; `/findings` sets `open: true`, so it always opens the page it was asked for.
Child `claude -p` processes set `FINDING_VIEWER_CHILD=1`, which turns the hook off for them. The hook reads the session
transcript (JSONL), groups it into turns (user prompt + assistant text + tool calls; sidechains and tool results are
skipped) and looks at the last one.

Findings come from the first source that yields any:

1. **`ReportFindings` tool call**: structured `file`, `line`, `summary`, `failure_scenario`. Each becomes a finding with
   id `F1`, `F2`, …
2. **Saved review report**: a path matching the `report_glob` setting (default `.claude/.history/code-reviews/*/*.md`)
   in the text. The report's `<!-- reviewed-sha: … -->` header pins the commit to show.
3. **Markdown in the answer**, parsed by `parse_findings()`: - Severity comes from headings and bold labels (`##
   Must-fix`, `**Should-fix:**`, `Nit`, `Question`, …) and carries over to the items below them until the next heading
   or rule. - Explicit ids are kept: `[C1]`, `W2.`, `S3 —`. Missing ids are assigned per severity letter (C, W, S, Q,
   F). - Anchors: `path.py:42`, `path.py:42-50`, `path#L42-L50`, and GitHub blob URLs. Up to 8 locations per finding. -
   Fenced code blocks are never parsed as findings.

Whether the turn is a review at all:

- **Never a turn that works on a handed-back triage** (`handed_back()`): one that got the triage from the inject hook (a
  `hook_additional_context` attachment after its prompt) or as a pasted "Copy triage", and one where the page tells the
  review session what it posted (a peer message, stored as an `isMeta` user row with `origin.kind == "peer"`, which then
  starts its own turn). The turns after it ("yes, post them") are skipped too, until a turn asks for a review again. The
  comments Claude drafts and posts there come from your triage, so they must not open as a new review. This also applies
  to `/findings`.
- `select_findings()` on the parsed markdown: at least one anchored finding.
- Otherwise `review_intent()`, for reviews the parser cannot read (tables, prose, findings without lines). The answer
  must be finding-shaped (400+ characters with a `file:line` anchor or two severity words) and a review must have been
  asked for: the prompt names the viewer, says "review", or a `ReportFindings` call or a skill with "review" in its name
  ran in the turn. Claude then reads the findings ([Building a page: render](#building-a-page-render)).
- A named turn with no review in it gets a one-line `systemMessage` saying so.

**A review run in the background** (the `code-review` skill forked, or an `Agent` call with `run_in_background`) splits
in two turns: the one whose prompt names the viewer launches it (the tool's result says it runs in the background), and
its findings come in a later turn that starts with the task's notification (a user row with `origin.kind ==
"task-notification"`, naming the call in `<tool-use-id>`). `read_turns()` gives that turn the prompt and hand-off of the
turn that launched the task (found by the call's id, else the turn before) and records it as `continues`; its texts and
tools stay its own, and `asks_review()` also counts the launching turn's tools. So the page is built (and opened, unless
`auto_open` is off) when the review reports back. The launching turn, with no review in it yet, says "the review runs in
the background; its page opens when it reports back" (with `auto_open` off, "its page is ready when it reports back")
when it asked for a review (its prompt says "review", or a review skill ran); otherwise it gets the usual "no code
review in this turn". A notification turn with no review says nothing. One task can notify more than once: the
fingerprint check below keeps it to one page.

The spec also records the PR and its **owner/repo** (`find_pr()`): from a PR URL or `owner/repo#N` in the turn or the 12
prompts before it, else `PR #N` plus `--repo owner/repo` or `repos/owner/repo/` in the turn's own tool calls. A PR that
no tool call in those turns fetched (`gh pr view|diff|checkout N`, `pulls/N`, `pull/N/head`, an MCP `pull_number`) is
only **mentioned** when the session sits on a branch of its own (`own_branch()`: not the default branch, not detached)
in that PR's repo: a kickoff prompt that names the PR a stacked branch builds on would otherwise label the review with
it. The spec then carries it as `mentioned_pr` and no `pr`, and render looks up the branch's own PR.

Before rendering, a fingerprint is checked against `state.json` (last 20 per session, 50 sessions), so the same review
does not open twice: the review text when Claude reads the findings, else titles + anchors. A turn in which Claude ran
`/findings` itself (`ran_findings`: the Skill tool with `findings`, or `finding_viewer.py` in a Bash call) is skipped
too: that page is open already, and the two copies of the review text differ, so the fingerprint would not catch it. The
hook then writes a spec to `out/specs/`, starts `finding_viewer.py render` as a detached process and returns at once
with a `systemMessage` carrying the page URL. Any exception is logged to `viewer.log` and swallowed: a Stop hook must
never disturb the session.

## Building a page: render

**Which repository** (`locate_repo()`): the session's repo when it is the PR's repo (any GitHub remote matches); else a
local clone of the PR's repo, looked for as `<folder>/<repo name>` in the session's folder, the folder beside the
session's repository (its parent folder), and each folder of the `repo_dirs` setting (empty by default; set it in
`config.json` in the data folder, for example `{"repo_dirs": ["~/Repos"]}`, or with `FINDING_VIEWER_REPO_DIRS`, folders
separated by `:`); else a clone under `clones/<owner>/<repo>` made with `gh repo clone` (no checkout, files over 1 MB
fetched only when read). Every `gh` call passes `--repo` when the owner/repo is known. If no repository is found, the
page says so instead of showing findings with no code.

**Which PR, for a saved report** (`pr_for_commit()`): a report pins a commit but names no PR, so `render()` asks `gh api
repos/<owner>/<repo>/commits/<sha>/pulls` for the PR whose head is that commit, skipping PRs from the default branch (a
develop → main release PR). A match makes the page a PR review: PR diff markers, a link to the PR in the header, a
freshness check against the PR head, and "(PR #N)" in the hand-off.

**Which PR, for a PR only mentioned** (`branch_pr()`): `gh pr view` in the session's folder gives the branch's own PR,
which becomes the page's PR and title ("Code review · PR #N"). Without one, the page is a branch review. Either way the
page notes which PR came up and what it shows instead.

**Which code to show**, in order:

1. the report's `reviewed_sha`, if that commit exists locally;
2. for a PR (found from a PR URL or `PR #N` in the turn or the 12 prompts before it): the PR head from `gh pr view`,
   fetching `pull/N/head` if the clone lacks it;
3. otherwise the working tree.

**Diff markers** come from `gh pr diff` for PRs, or `git diff <merge-base>` against the default branch otherwise.
`parse_patch()` records, per file, the added lines, where lines were removed, and the **hunk ranges** (new-side, context
included). Hunk ranges matter because GitHub accepts inline comments only inside them.

**Reading the findings with Claude** (`extract.py`): for a saved report or a review in the chat, a headless `claude -p
--json-schema` (the `extract.model` setting, Sonnet by default, read-only tools, hooks off, 300 s limit) turns the
review text into findings: id, severity, title, body, locations. It handles any format, completes shortened paths, and
finds the line for findings that name a file or symbol but no line (tagged "line: looked up by Claude" on the page). It
is told not to review, judge or reword. Numbered items keep their number with the severity letter (should-fix 3 → `W3`).
If Claude fails, the regex findings are used and the page says so; if both find nothing, no page opens. `ReportFindings`
input is already structured and skips this step. The `extract.enabled` setting turns it off
(`FINDING_VIEWER_EXTRACT=0`). About 45–60 s and $0.10–0.30 per review.

**Path resolution** (`resolve_path()`): anchors are matched against the repo's file list, also trying a `src/` prefix
and suffix matches. When a suffix is ambiguous, files in the diff win, then the shortest path; the alternatives are
shown on the page.

The output is the folder `out/<YYYYmmdd-HHMMSS>-<slug>/`: `review.json` (the data) and `page.html` (the template with
the data JSON inlined, `<` escaped). `build_page()` builds the data and `write_page()` writes both files, each in one
step (a temporary file renamed over it), review.json last. A re-review ([Review the new
commits](#review-the-new-commits)) builds the same page again through `build_page()` at the PR's new head, under the
same name. `out/latest.html` is re-pointed at its `page.html`, the server is started, and the browser opens the page
through it (or as `file://` if the server cannot start, in which case triage stays in the browser only) unless the spec
says `open: false` (the Stop hook with `auto_open` off). `prune()` keeps the newest 40 pages and deletes older ones with
everything stored for them (`reviews.delete_review()`).

## The server

A `ThreadingHTTPServer` on `127.0.0.1:47321` (the `port` setting changes it).

- **Lifecycle**: `ensure_server()` pings `/api/ping` and compares `SERVER_VERSION`, a hash of the code (see
  [CONTRIBUTING.md](../CONTRIBUTING.md#server_version)). A missing server, or one running other code, is replaced by a
  detached `triage.py serve`. The server exits after 8 hours without requests.
- **Security**: requests with a foreign `Host` header are refused (blocks DNS rebinding). Writes also need the
  `X-Finding-Viewer: 1` header and a local or absent `Origin`; a custom header forces a CORS preflight, which the server
  never approves. Page names must match `[\w.-]{1,200}`, not start with a dot, and name a folder inside `out/`. The
  index and the pages are sent with a `Content-Security-Policy` under which a script runs only if it carries that
  response's nonce (the inline script, and on review pages the three cdnjs libraries, which also carry SRI hashes).
- **Pages are served with the current template** (`reviews.page_html()`): the server reads the page's `review.json` and
  fills today's template (`web/viewer.html` with `shared.css`, `viewer.css` and `viewer.js` put in), so page changes
  reach old reviews. `page.html` is only for `file://`. The stored files are not rewritten. `store.page_data()` fills
  the fields older pages lack: `page`, `pr` (from a `PR #N` title) and `session_id`.

The routes and the 423 / 409 rules: [ARCHITECTURE.md, Routes](ARCHITECTURE.md#routes).

## The review page

Loads highlight.js, marked and DOMPurify from cdnjs; everything else is inline.

- **Triage state** is saved to `localStorage` on every change and `PUT` to the server after a 400 ms debounce. On load,
  whichever copy is newer wins, and a newer local copy is pushed back. The `localStorage` key counts the page's
  re-reviews, so a copy kept before one (old findings, old lines) is never taken for the rebuilt page; the page's next
  save removes those older keys.
- **Anchor**: by default the first flagged line that falls inside a diff hunk, else the first flagged line. Clicking a
  line number moves it. Each saved anchor carries `in_diff`, which tells Claude whether it can be an inline comment.
- **Fix** (own code only): on a review with no PR, or of a PR the `gh` user opened (`own_code` from `/api/handoff`),
  each finding gets a third button, Fix (`f`), between Post and Don't post (on a PR review; a local review has only Fix
  and Won't fix), stored as `status: "fix"`. Its note is always a note for Claude, never a comment posted word for word.
  Fix findings are never drafted or posted; Copy triage and the hand-off list them under "To fix locally".
- **Local review** (no PR): a review with no PR is your own code before a PR exists, so nothing is posted. Each finding
  gets Fix (`f`) and Won't fix (`r`, stored as `skip`), with no Post button, key or filter; the header says "Local
  review · no PR". A finding stored as Post before this (an older page) counts as undecided and keeps its stored value
  until you pick again. Copy triage says "N to fix, N won't fix, N undecided".
- **Send fixes to session**: on your own code (no PR, or a PR you opened) with at least one Fix finding, the toolbar
  button hands the triage to the review session now, without waiting for your next message there: `POST
  /api/send-fixes/<page>` starts a detached worker that relays `summarize(…, for_claude=True)` with `send.deliver` to
  the session open in Claude Code (`~/.claude/sessions`), then deletes the relay's own transcript. The button reads
  "Sending…", then "✓ Sent" until the triage changes; when the review session is not open or the relay fails, it
  says why, and that typing any prompt in the review session gets it the triage.
- **Keys**: `j`/`k` next/previous, `c` chat, `h` hide/show the chat, `o` open in VS Code.
- **Chat panel** folds away with ⇥ or `h`; "💬 Show chat" in the toolbar brings it back (it pulses while Claude is
  answering). The choice is remembered per browser (`localStorage`) for all pages.
- **Freshness banner**: fetched on load, every 3 minutes while the tab is visible, and on returning to the tab when the
  last check is over a minute old. It also says when you already approved the PR (or requested changes), when, and
  whether commits landed after it; it turns green when there is nothing to warn about. While a Check again runs, its
  button reads "Checking…" and ignores clicks (`aria-disabled`, so focus stays on it). The label flips inside an
  `aria-hidden` span under a fixed `aria-label`, so the banner's status region does not read it out; "Checking again…"
  is said once.
- **Running** (toolbar, next to "Saved"): `● N running` while a job runs on the review, else a quiet "Sessions". It
  opens a non-modal panel (a disclosure: `aria-expanded`, Esc or a click elsewhere closes it, Esc from inside returns
  focus to the button) from `GET /api/activity/<page>`, polled every 1.5 s while something runs and every 10 s
  otherwise; the panel is rebuilt only when the answer changes, and its clocks tick in place. It lists the jobs running
  (elapsed time, step, worker pid, Claude session, **Stop** for drafting and side chats), the ones that ended in the
  last 10 minutes (done, failed with the error, stopped), and the Claude sessions behind the page (the review session,
  the draft fork while its transcript exists, noted "deleted when you post, re-draft or discard", each chat thread) with
  a copyable `cd <cwd> && claude --resume <id>` for each one not running now: not open in Claude Code
  (`~/.claude/sessions`) and not used by a job here. A Stop is said once, by the panel; a refused Stop's message stays
  until the answer changes. A job that starts or ends elsewhere (another tab) also refreshes the chat, the drafts and
  the banner.
- "← All reviews" links to the list; it is hidden when the page is opened as a file.

## Draft comments and posting

Draft comments (toolbar button on PR reviews whose review transcript still exists; `n` with nothing left to decide
flashes it; `page.html#drafts` opens the dialog). `draft.py`, in three steps:

1. **Draft**: `claude -p --resume <review session> --fork-session` inherits the whole review conversation, gets `PROMPT`
   plus the same `summarize()` text the hook injects (and the freshness / approval notes), and returns the review
   through `--json-schema`: body, event, one comment per finding to post (path, line, optional start_line, level). A
   finding whose note is in "my comment" mode gets the reviewer's text word for word, whatever the fork returned for it;
   a level the fork wrote into a body anyway is stripped. Read-only tools plus `--add-dir out/`, so it can read the full
   triage but cannot post. Model: the session default, or the `draft_model` setting. It runs in a detached worker
   ([Background jobs](#background-jobs)). The fork's id is recorded from its first event, the steps show in the dialog,
   and the drafts land in `out/<page>/drafts.json` with `in_diff` per comment (checked against the page's hunk ranges).
   Stop (Running panel) ends the worker, deletes the fork and leaves "Stopped by you." as the dialog's error.
2. **Post**: you edit the review type, the body, each comment and its level (Must-fix, Should-fix, Suggestion, Nit,
   Question or none; it starts at the finding's severity), or leave a comment out, and click "Approve & post". A
   detached worker, never the fork, posts exactly that text as one review, each comment led by its level in bold: `gh
   api repos/<o>/<r>/pulls/<n>/reviews` on the reviewed commit. Comments outside the diff go in the review body, since
   GitHub accepts inline comments only inside hunks. A refused post keeps your edits and shows GitHub's error. The
   inject hook's state is marked, so your next message in the review session does not carry the same triage again. A
   second click while posting is refused, and posting cannot be stopped (GitHub may already have the review).
3. **Tell**: the fork is resumed once with only `SendMessage` (`send.deliver()`) and tells the review session (found in
   `~/.claude/sessions/<pid>.json`, written by Claude Code; undocumented, so anything unexpected reads as closed) what
   was posted and not to post it again. Then the fork's transcript and folder are deleted, unless the `delete_forks`
   setting is false. A closed review session is not told. Discard and re-draft delete the fork as well. A fork open in
   Claude Code (resumed from the Running panel) is never deleted, and it does not tell: `told` says why.

The page polls `GET /api/handoff/<page>` while drafting, posting or telling.

## The list of reviews

Served at `/`, filled from `GET /api/reviews`.

- **Grouping**: pages of the same PR in the same repo (or, without a PR, the same title) form one card. The newest
  render leads; earlier renders fold under "N earlier renders".
- **Badges**: finding count, how many are decided and marked to post or to fix (`N to fix`, own code only; findings the
  new commits fixed are counted apart, "N fixed since"), `● N running` while a job runs on the review ("● Reviewing new
  commits" during a re-review; the list re-reads itself every 5 s until it ends, without re-checking the PRs), side
  chats, where the triage went (from `drafts.json`: drafting, "drafts ready to approve" linking to the dialog, "✓ posted
  to GitHub" linking to the review, "drafts failed"; else from the inject hook's `inject_state.json`: "📨 sent to
  Claude", or "triage changed since" when Claude has an older version), and freshness for the newest render: up to date,
  N new commits (with how many findings sit on changed code), force-pushed, merged or closed, plus your own review: "✓
  you approved" (with "N new commits since" when the PR moved past your approval) or "you requested changes". The cached
  result shows at once; each newest render is re-checked (3 at a time) if the last check is over a minute old.
- **Done**: groups whose newest render's PR is merged, closed or approved by you at its current head fold under "Done"
  at the bottom (the freshness result's `done`). A push after your approval brings the group back.
- **Archive**: one click on a card's newest render (`PUT /api/archive/<page>`) archives every render in the group
  (`reviews.set_archived()`, stored in `out/archived.json`). A group is archived when its newest render is, so a new
  review of the same PR brings the card back. Archived groups fold under "Archived" below Done, are not re-checked for
  freshness, and offer Unarchive. Focus moves to the next card's button (or follows the card when that fold is open),
  and a status line announces the move. Archiving keeps the page, its triage and chats; the newest 40 pages are still
  all that is kept.
- **Delete**: two clicks (the button arms for 4 s), then `DELETE /api/reviews/<page>`.
- While a re-review runs, Delete on that render and Archive on its card carry `aria-disabled` with the reason, and say
  it when clicked; the server refuses them with 423 anyway.
- The list reloads whenever the tab becomes visible again.

## Hand-back to Claude: the inject hook

Runs as a UserPromptSubmit hook. For pages with triage or chat activity in the last 48 hours it injects a summary as
`additionalContext`, with these rules:

- only pages **owned by the current session** (the `session_id` stored when the page was rendered);
- only pages whose version (`saved_at|chat updated`) this session has not seen yet (`inject_state.json`);
- at most two pages per prompt; nothing when the page has neither a decision, a note, a chat nor a re-review.

A re-review rewrites `decisions.json`, so its version changes and the review session gets the new state with the next
prompt. The summary then says "The page was re-reviewed at <head> … (it showed <old> before): 1 fixed, 2 still apply, 1
new", gives ids and anchors at the new head, leaves the fixed findings out of "To post" and "Undecided" (they get a
"Fixed by the new commits, so not to post" line), and notes under a finding marked Post that it changed since the
reviewer chose Post (`recheck`).

The summary (`summarize()`) lists what to post with its anchor (anchors outside the diff are flagged "put it in the
review body"), what to fix locally (an instruction to change the code and not post it), what not to post, what is
undecided, each note, the last chat reply per finding, the freshness warning, a note when the PR is already merged,
closed or reviewed by you (`pr_note()`: approved means post as a COMMENT review and do not approve again), the paths to
the full JSON, and an instruction to show the final comments and get a yes before posting. `/findings decisions` prints
the same text on demand and marks it seen. Side-chat processes set `FINDING_VIEWER_CHILD=1`, which turns this hook off
for them. On a local review (no PR) it says "N to fix, N won't fix, N undecided" and has no "to post" or drafting
instruction.

After a post from the page ([Draft comments and posting](#draft-comments-and-posting)), the hook keeps handing over a
triage that still has Fix findings (`mark_handed()` does not mark it seen while they wait), and the summary marks each
finding the post already covered: "do not post it again".

A Send fixes ([The review page](#the-review-page)) records the version it sends for the review session in
`inject_state.json` as the relay starts, so the next prompt does not carry the same triage again (a newer triage still
does). A relay that fails, or a worker that is lost, takes that mark back (or puts back the version seen before), so the
next prompt carries the triage. Its message starts with `Finding-viewer triage for "`, so the Stop hook treats the turn
it starts as a hand-off and opens no page.

## Side chat

One thread per finding, stored in `out/<page>/chat.json`; each thread keeps its own `claude` session id and resumes it.

- **First message** carries the review title, the finding text, the reviewer's decision and note, a code excerpt per
  location (8 lines of padding, 400 lines total; `>` marks flagged lines, `+` added lines) and, if the PR has moved, the
  freshness warning. Later messages are sent as-is.
- **Process**: a detached worker per message ([Background jobs](#background-jobs)) runs `claude -p --output-format
  stream-json --permission-mode dontAsk`, a read-only tool allowlist (Read, Grep, Glob, `git show/log/diff/blame`, `gh
  pr view/diff`), `--strict-mcp-config`, hooks disabled, the parent session's environment variables removed, a 300 s
  watchdog. The thread's Claude session id is recorded at the `init` event, so the Running panel shows it while the
  reply is written. The system prompt tells it to lead with the answer and to put drafted PR comments in a fenced
  `comment` block, which the page offers as "use as note".
- **Reviewed code**: when the page shows a commit, the chat runs inside a detached git worktree of that commit under
  `worktrees/<sha12>` (the 3 most recent are kept), so its tools read exactly the reviewed code. Working-tree reviews
  run in the repo itself.
- **Context mode** (⚙ panel, `settings.json`): `finding` starts a fresh session that knows only the finding; `fork` runs
  `--resume <review-session> --fork-session`, inheriting the whole review conversation (about 157k tokens for a real
  review). If the review transcript is gone, it falls back to `finding` and says so. The same panel's `shortcuts`
  switch, also in `settings.json`, turns the review pages' single-key shortcuts off for speech input.
- **Progress**: the page polls `/api/chat/<page>` every second while a reply is pending; tool calls show as steps
  ("Reading foo.py"). Stop (Running panel) ends the worker and adds "Stopped by you." to the thread; the thread keeps
  the session it had before.
- **Cost**: `total_cost_usd` in `claude -p` results is cumulative over the session's history, so the cost of one message
  is the difference from the previous total. A fork's first message shows no cost, because it inherits the review's
  spend.
- **After a re-review**: each thread on a finding gets one line from the page (`role: system`): "Re-reviewed at
  f48a8e88: still applies. This chat read the code at a6ee6e66…". The thread's next message tells Claude first that the
  code moved, what the re-review found, and where the new head is checked out (the thread's own session and folder stay;
  the new head's worktree is added with `--add-dir`).

## Freshness

Answers "is the reviewed code still what the PR holds?" and caches the result in `out/<page>/freshness.json` for 60 s,
so the hand-off hook and the chat can mention it without calling GitHub.

- **One check per page at a time** (`jobs.check_once()`): a request that arrives while a check of that page runs (Check
  again clicked again, another tab, the periodic poll, the list) waits for it and gets its result, so `gh pr view` and
  `git fetch` run once. The same in-memory registry gives the Running panel its "Check again" row; it dies with the
  server, as the request does.

- **PR reviews**: compares the reviewed commit with `headRefOid` from `gh pr view`. Status is `current`, `behind` (new
  commits listed with author, date and links) or `rewritten` (force-pushed; the reviewed commit is gone).
- **Your review** (`mine`, PR reviews only): your standing review as GitHub counts it, the latest approval or change
  request by the `gh` user (`gh api user`, looked up once per process; a dismissal clears it). It records the commit it
  was on, how many commits landed after it and whether it covers the head. `done` is true when the PR is merged or
  closed, or your approval covers the head.
- **Branch reviews**: compares with local `HEAD`; `unknown` if `HEAD` does not contain the reviewed commit.
- **Working-tree reviews**: always `unknown`, there is no commit to compare.
- **Affected findings** (`_affected()`, only for `behind` and `rewritten`): runs `git diff -U0 reviewed..head` on the
  findings' files and rates each finding by its worst location: `deleted` (file removed), `lines` (a changed hunk
  overlaps the flagged lines), `file` (same file, other lines).
- `heads_up()` turns the cached result into one paragraph ("the PR has 2 newer commits… Findings on changed code: W1
  (flagged lines changed). Re-check them against the new head before posting.") used by the hand-off and by new chat
  threads.
- **Where a line went** (`map_line()`): `old_side_hunks()` also records each hunk's new side and renames, so a line of
  the reviewed commit maps to its line at the new head, or to nothing when it changed. The re-review moves picked
  comment lines with it. It counts each hunk's lines, so a removed line that starts with `--` (an SQL comment) is not
  taken for a file header; `u0_diff()` runs the `-U0` diff with `core.quotePath=false`, so non-ASCII paths match.
- A check that ends after a re-review moved the page to a newer commit is not cached (it compared the old one): it
  writes under the page's lock (`jobs.page_lock()`), which the re-review holds while it moves the page. The re-review
  itself marks the cached check `current` when it reviewed the head that check found, else drops it.

## Background jobs

Drafting, posting + telling, each side chat reply, each re-review and Send fixes run as **their own detached
processes**: `finding_viewer.py job out/specs/jobs/<job>.json`, started like `render` (`start_new_session=True`, output
to `viewer.log`). A server restart or shutdown never kills them. The server writes the job's state, starts the worker
(`jobs.spawn()`), records its pid at once, reads the state on every poll and stops the worker on request; the worker
writes its own progress and result (`draft.run_draft()`, `draft.run_post()`, `chat.run()`, `rereview.run()`,
`handoff.run_send()`) and deletes its spec. Drafting and re-reviewing run their fork through the same
`draft.run_fork()`. Every Claude transcript this tool deletes is one it started: a draft or re-review fork, the session
of a first chat message that was stopped or whose worker died, or the relay session of Send fixes. With the
`delete_forks` setting false, all of them are kept (`sessions.delete_session`).

- **Shared files**: the server and several workers write `drafts.json` and `chat.json`, so every read-modify-write holds
  an `fcntl.flock` on a sidecar `out/<page>/locks/drafts.lock` / `out/<page>/locks/chat.lock`, and every write replaces
  the file atomically. A worker writes only while the state still carries its job id, so a stopped job stays quiet.
- **A dead worker** (`jobs.lost()`): a state that says running whose pid is gone reads as failed on the next read, and
  is recorded so ("The drafting process stopped (pid N).", "No answer: the chat process stopped (pid N)…"); the
  `claude` child it may have left in its process group is ended. A posting worker that dies says to check GitHub before
  posting again; a telling worker that dies leaves `told` failed and deletes the fork, unless the `delete_forks` setting
  is false. A state with no pid fails at once when it has no job id (a server before version 17 ran it in a thread, gone
  with that server), and after 30 s otherwise (its server died before recording the pid). A live pid is never given up
  on, whatever the clock says (a laptop that slept mid-draft): each worker has its own watchdog (draft 600 s, re-review
  900 s, chat 300 s, `gh` 60 s, tell 180 s, send 180 s). The server reaps the workers it started, so a finished one is
  not mistaken for alive.
- **Ending without the answer**: a first chat message that is stopped or whose worker died deletes the session it had
  started (its id comes with the `init` event), so the next message starts the thread afresh.
- **Stop**: the job is marked `stopping` first, so a poll that finds the killed worker before the stop is written
  records it as stopped, not failed. Then SIGTERM to the worker's process group (it leads its own session, so its
  `claude` child goes too; nothing is signalled unless the pid's command line still names the job's spec), SIGKILL after
  3 s, and the stopped state is written unless the job ended first. Drafting, re-reviews and side chats only: not
  posting (GitHub may already have the review) and not Check again (it has its own timeouts).
- **Running panel** (`triage.activity()`, `GET /api/activity/<page>`): `draft.activity()`, `chat.activity()`,
  `rereview.activity()` and `jobs.checks()` each describe their jobs, running or ended, from the state files and the
  check registry (which forgets a check 10 minutes after it ends); the Claude sessions come from
  `sessions.review_session()`, `sessions.live_sessions()` and each transcript that still exists.

## Review the new commits

When the freshness check says `behind` (or `rewritten`), the banner offers "Review the N new commits" ("Review the new
head"), unless the page cannot fork its review session (the transcript is gone) or shows the working tree: then there is
no button and the reason is the banner text's title. Starting saves any pending triage first. With unposted drafts
(ready, or a failed post), the banner first asks "This discards the unposted drafts…" with Discard drafts and review /
Cancel (Esc cancels); they go only if the re-review succeeds. While it runs, its own banner says "Reviewing the N new
commits… 1:12" (the clock is `aria-hidden`; the start is said once) with Stop, and the page is **locked**: Post / Fix /
Don't post (`a`, `f`, `r`, `u`), the note (read-only) and its mode, the line numbers and `[` `]`, "use as note", Draft
comments (start, re-draft, post, discard; open drafts can still be read) and the side chat's Send and quick questions
carry `aria-disabled` and the reason, keep focus, and say the reason when used. Reading, navigating, filters, Check
again, Copy triage and Sessions stay. The lock comes from `GET /api/activity` (polled every 1.5 s while it runs), so a
tab that has not noticed it yet has its write refused (423) and shows the server's reason, then locks too. The chat's
text box stays editable, so a question can be typed meanwhile. Starting moves focus to the new banner's Stop (the Review
button leaves). When the job succeeds, every tab of the page reloads itself (a tab in the background, once it is shown
again) and says once what changed: "Reviewed f48a8e88: 1 fixed, 1 still applies, 2 new."; session storage carries across
the reload the server's words, an unsent chat message, the open finding (also in the hash), where focus was (the chat
box, or the banner, whose note then takes it) and whether a save was refused meanwhile, which the rebuilt page cannot
take (the note says to make that change again). A failure or a Stop unlocks the page as it was, says why, and sends a
save the lock refused again. On the rebuilt page each finding carries what the re-review found: "Fixed in f48a8e88" with
the reason (listed under "Fixed in the new commits" at the bottom and in Copy triage, not counted as open, not drafted,
its decision still shown), "Changed", or "New"; the header says "Re-reviewed at …". A finding that changed after you
chose Post says "Changed since you decided Post" with a "Still post it" button, and a line you picked that changed says
"Your chosen line changed; anchor reset". Both marks live in `decisions.json` (`recheck`, `anchor_reset`) and go when
you decide again or pick a line (the reset note also with the next re-review).

The freshness check says the PR moved past the reviewed commit; this reviews those commits and brings the page to the
new head (decisions E1–E4 of 2026-10-19).

- **Start** (`POST /api/rereview/<page>`, `rereview.start()`): refused when the page shows the working tree or the
  review transcript is gone (409, and the page shows no button), when the check (`freshness.check()`, cached for a
  minute) finds nothing newer or the new head is not in the clone (409), while another job runs on the page (409 "Wait
  until Draft comments ends, or stop it in Sessions."; posting and telling cannot be stopped), and while unposted drafts
  are not confirmed for discarding (409 with `drafts: true`). The server hands the worker the triage as the hand-off
  hook writes it (`summarize()`), the new commits and the freshness `affected` map.
- **The fork**: `claude -p --resume <review session> --fork-session`, read-only tools (`chat.READ_ONLY_TOOLS`), hooks
  off, `FINDING_VIEWER_CHILD=1`, `--add-dir out/` and `--add-dir` a detached worktree of the new head
  (`store.review_checkout()`), in the review session's folder; model: the session default or the `rereview_model`
  setting. `PROMPT` gives the PR, the reviewed commit, the new head, the commits in between, each finding with its id,
  anchors, body and what the new commits touched, and the triage; it asks the fork to review what the commits changed
  and to re-check each finding. `--json-schema` returns per finding an id, a status (`still_applies`, `changed`,
  `fixed`, `new`), title, severity, body, recurring, anchors at the new head and, for `fixed`, a one-line why.
  `parse_answer()` checks it like `extract.py`: a status outside the four, an id the page does not have, a finding
  twice, or an open finding left out fails the job with that reason. New findings get the next id for their severity
  letter after every id the page ever showed (findings never leave the page; triage and chat ids count too).
- **The update**: `git diff -U0 -M <old> <new>` gives `freshness.map_line()`. The findings, at the new head, go through
  `render.build_page()` with `reviewed_sha` = the new head and the page's own title, PR, repo, folder, session ids and
  name, so files, hunks, diff markers, PR author and blob links are all read again; a finding without new anchors keeps
  the old ones that map cleanly. The page data gains `rereviewed` (`from`, `to`, `at`, `commits`, `rewritten`, `counts`,
  `discarded_drafts`, appended) and each finding its `rereview` (`status`, `at`, `why`). `carry_triage()` keeps each
  decision, note and mode by id; a saved anchor that differs from the old default line was picked by the reviewer and
  moves through the diff when its line is unchanged and its file is still one of the finding's, else it goes back to the
  default and `anchor_reset` says so; a `changed` finding marked Post gets `recheck`; new findings start undecided;
  `updated` is set to now, so no copy a browser kept wins over it; an `anchor_reset` from an earlier re-review goes. A
  finding that still applies, or is fixed, keeps the review's own title, body and severity; only a `changed` or `new`
  one takes the fork's words. Each side chat on a finding gets a line ([Side chat](#side-chat)), the cached check
  becomes `current` ([Freshness](#freshness)), confirmed unposted drafts are discarded with their fork (only if still
  unposted: a posted state and its link stay).
- **One step** (`_stage()`, `_commit()`), under the page lock, after checking that the job was not stopped meanwhile:
  every new file is written complete into `out/<page>/.staging-<job>/` (the triage, the chats, the freshness check,
  `page.html` and `review.json`); then the steps that put them in place are recorded in the state (`committing`); then
  they are taken in that order: the triage, the chats, the freshness check, the drafts, and the page last (`page.html`,
  then `review.json`, since the review data says which commit the page is at (to each tab, to the reload check and to
  `stale()`); then the state says
  done, with the counts. A worker that dies before `committing` is recorded leaves the page as it was (the next read
  says "The page is unchanged" and deletes what it staged); one that dies after it is finished by the next read
  (`_fail_stalled()` runs the remaining steps, each of which can run twice), which reports success "(completed after the
  worker stopped)". Anything that fails before (the fork, its answer, the build, the staging) leaves the page exactly as
  it was. `start()` reads the page's commit again under the lock, and the worker checks it before it starts: a page that
  moved meanwhile fails the start ("Start again"). The fork's transcript is deleted when the job ends, however it ends,
  unless the `delete_forks` setting is false or it is open in Claude Code (the Sessions list then says "kept: open in
  <name>"). `viewer.log` gets "rereview: … Reviewed f48a8e88: 1 fixed, … ($x.xx, the fork's total; Nk tokens read)".
- **The lock**: `out/<page>/rereview.json` saying `reviewing` is the lock, so it survives server restarts. Every write
  route checks it under `jobs.page_lock()` (`rereview.guard()` holds that `flock` around the write: triage, chats,
  drafts, Archive for every render of the card, Delete), so a write cannot land while the worker updates the page, and
  answers 423 meanwhile. `jobs.page_lock()` is re-entrant within a thread, so code holding it can read the state. A dead
  worker unlocks at once, by the same rules as above (`jobs.lost()`); a live one is never timed out by the server. Stop
  (page banner or Running panel) is always allowed.

## The data folder

Runtime data lives outside the code folder, in the data folder: `out/` (the review pages with their triage, chats and
drafts), `clones/`, `worktrees/`, `settings.json`, `state.json`, `inject_state.json`, `viewer.log`. The folder is
`FINDING_VIEWER_DATA` when set; else `CLAUDE_PLUGIN_DATA`, which Claude Code sets for a plugin (its folder is under
`~/.claude/plugins/data/`, and `claude plugin uninstall` deletes it unless `--keep-data` is given); else
`~/.local/share/a-bit-human-review`. `config.json` (optional settings, see README) lives there too.

An older checkout kept this data beside the code: the first run of this version moves `out/` and the four files there by
itself (`finding_viewer.py migrate` does it on demand) and leaves symlinks in their old place, so a server or job still
running the old version keeps working. The old `clones/` and `worktrees/` stay where they are, as caches: new ones are
made in the data folder, while pages built from an old clone and side chats started before keep reading the old ones.

Older versions also kept each page as flat files in `out/` (`out/<page>.html`, `out/<page>.decisions.json`, and so on).
The first run of this version gives each page its own folder (`paths.migrate_layout()`, also
`finding_viewer.py migrate`), under `.migrate.lock`, and folds in any flat file that a process still running the old
version writes later. Nothing is lost: the files are moved, and `review.json` is read out of each page's data block.
After a run that folds pages into their folders, the hook or command that ran it first replaces a server that runs other
code (`triage.replace_old_server()`, only when a finding-viewer server of another version answers), so that one run can
take up to about 4 s. `finding_viewer.py migrate` does the same.
