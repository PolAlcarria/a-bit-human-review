# a-bit-human-review: architecture

This file is the map: processes, modules, data model, jobs, routes, state files. As of 2026-10-20.
Line numbers are left out on purpose; search for the function names.
How each feature behaves: [behaviour.md](behaviour.md).

## Processes

```
Claude Code session
  Stop hook ............ finding_viewer.py hook      acts only on a turn that names the viewer
  UserPromptSubmit ..... finding_viewer.py inject    hands the triage back to Claude
  /findings ............ finding_viewer.py open | list | decisions
  setup ................ finding_viewer.py doctor | install | uninstall   (by hand, or through /findings)
        | the hook and open start, detached:
        v
  render ............... finding_viewer.py render <spec>   claude -p reads the findings
        | triage.ensure_server()   (list calls it too)
        v
  server ............... triage.py serve (finding_viewer.py serve runs the same), 127.0.0.1:<port>
        |   exits after 8 idle hours; replaced when SERVER_VERSION (a hash of the code) differs
        | starts, detached, one per job:
        v
  workers .............. finding_viewer.py job <spec>   draft, post + tell, side chat reply, re-review, send fixes
  render and the workers run claude -p children with FINDING_VIEWER_CHILD=1 (their hooks stay quiet)
```

### Data flow

```
review turn ends ─► /findings (or Stop hook, if named) ─► detect review ─► render (detached): find repo, Claude reads findings ─► browser
                                                                        │
    your next prompt ◄── UserPromptSubmit hook ◄── decisions.json ◄─────┤ Post / Don't post / note / anchor
                                                   chat.json  ◄─────────┤ side chat (claude -p per finding, detached worker)
                                                   freshness.json ◄─────┤ has the PR moved on? (one check at a time)
                       page rebuilt at the head ◄── rereview.json ◄─────┤ Review the new commits (fork of the review session,
                       (triage carried over)        (the page's lock)   │ detached worker; every write waits: 423)
    GitHub review ◄── gh api ◄── you approve ◄──── drafts.json ◄────────┘ Draft comments (fork of the review session,
                    (detached worker)                                     detached worker)
```

Five stages: **detect** a review, **render** a page (finding the reviewed repo and reading the findings with Claude),
**serve** it, **hand** the triage back to Claude, and three helpers on the page (side **chat**, **freshness**, and a
**re-review** of the PR's new commits). Drafting, posting, side chats, re-reviews and sending fixes run as detached
**jobs** that outlive the server ([Background jobs](behaviour.md#background-jobs)).

- Hooks return at once. They log their own errors to `viewer.log` and swallow them.
- After a run that folds flat pages into their folders (`paths.migrate_layout()`), the hook or command that ran it first
  calls `triage.replace_old_server()`: it pings the server, and calls `ensure_server()` only when a finding-viewer
  server of another version answers, so a server still running the old code is replaced. It never starts a server when
  none runs. That one run can take up to about 4 s, the only exception to "return at once".
- Render, server and workers are detached (`start_new_session=True`), so a server restart kills none of them.
- Files that the server and its workers both write (job states, chats, drafts, decisions) are replaced atomically, and
  each read-modify-write holds an `fcntl.flock` on a sidecar in the page folder's `locks/` (`jobs.locked`,
  `jobs.page_lock`).
- Runtime files go to the data folder (`paths.DATA`), never under the code folder (a read-only plugin cache).

## Modules

Three layers: the entry point, the feature modules that do the work, and the shared modules several features need.

| Layer | Module | Holds |
|---|---|---|
| entry point | `finding_viewer.py` | The CLI (`hook`, `inject`, `open`, `list`, `decisions`, `serve`, `migrate`, `render`, `job`, `doctor`, `install`, `uninstall`); opening a page in the browser |
| feature | `hook.py` | The Stop hook: the transcript read into turns, the review and its PR found in the turn ([Detecting a review](behaviour.md#detecting-a-review-the-stop-hook)); owns `state.json`, the fingerprints already shown |
| feature | `parse.py` | The regex parser for markdown reviews |
| feature | `extract.py` | Claude reading the findings out of any review ([Building a page](behaviour.md#building-a-page-render)) |
| feature | `render.py` | Building a page: the reviewed repository and commit, the PR's diff, the code at each finding ([Building a page](behaviour.md#building-a-page-render)); owns `out/<page>/review.json` and `out/<page>/page.html`, `out/latest.html`, `out/specs/` and `clones/` |
| feature | `triage.py` | The localhost server and its routes ([The server](behaviour.md#the-server)); `cmd_job`, where a detached worker starts; holds `SERVER_VERSION`, a hash of the code |
| feature | `reviews.py` | The stored pages: the list, the page template assembled from `web/`, archive and delete; owns `out/archived.json` |
| feature | `handoff.py` | The triage handed back to Claude: the inject hook, `decisions`, the summary ([Hand-back](behaviour.md#hand-back-to-claude-the-inject-hook)); Send fixes to session (`send_fixes`, a detached worker that relays the triage, `run_send`); owns `inject_state.json` and `out/<page>/send.json` |
| feature | `chat.py` | Side chat: one thread per finding, each reply a detached worker that resumes the thread's Claude session ([Side chat](behaviour.md#side-chat)); owns `chat.json` and `settings.json` |
| feature | `draft.py` | Draft comments and posting: the fork that drafts the review, the post, the tell ([Draft comments](behaviour.md#draft-comments-and-posting)); owns `drafts.json` |
| feature | `send.py` | The relay to a live session: `deliver()` relays a message with only SendMessage: resuming the review's fork to tell the review session what was posted, or as a session of its own for Send fixes (deleted afterwards) ([Draft comments](behaviour.md#draft-comments-and-posting)) |
| feature | `rereview.py` | Re-review of the PR's new commits: the fork, its answer, the update of the page under its lock ([Review the new commits](behaviour.md#review-the-new-commits)); owns `rereview.json` |
| feature | `freshness.py` | Whether the reviewed commit is still what the PR holds: the cached check and where a line went ([Freshness](behaviour.md#freshness)); owns `freshness.json` |
| feature | `setup_cmds.py` | `doctor`, `install`, `uninstall`: the checks, and the hook entries and slash command for a copy that is not a plugin |
| page | `web/viewer.html`, `web/viewer.css`, `web/viewer.js` | The review page: markup, CSS and script, with highlight.js, marked and DOMPurify from cdnjs; `reviews.viewer_template()` inlines the CSS and script at fill time, so the page stays one file; the review data is embedded as JSON; `SERVED` keeps the `file://` fallback ([The review page](behaviour.md#the-review-page)) |
| page | `web/index.html` | The list of all reviews, served at `/`, its CSS and script inline ([The list of reviews](behaviour.md#the-list-of-reviews)) |
| page | `web/shared.css` | The CSS both pages share: colour variables and resets, inlined into each |
| plugin | `.claude-plugin/plugin.json`, `marketplace.json` | The plugin's name (`a-bit-human-review`), version and author; the repo is its own marketplace (source `./`) |
| plugin | `hooks/hooks.json` | The Stop hook (`finding_viewer.py hook`, 15 s) and the UserPromptSubmit hook (`inject`, 10 s) |
| plugin | `skills/findings/SKILL.md` | The `/a-bit-human-review:findings` command; `install` derives `~/.claude/commands/findings.md` from it |
| shared | `config.py` | Settings, read from `config.json` in the data folder; each key is overridable by its `FINDING_VIEWER_*` variable |
| shared | `paths.py` | Where the code and the data live; `log` appends to `viewer.log`; the one-time move of an old checkout's data, and `migrate_layout`, the one-time move of flat pages into a folder each, both under `.migrate.lock` |
| shared | `severity.py` | The severities, their id letters and labels, the words reviews use for them |
| shared | `store.py` | Review data on disk: each page's folder `out/<page>/` (`page_dir`, `pages`, `exists`), `page_data` (reads `review.json`, the one way review data is read), the triage and chat files (`decisions.json`, `chat.json`), the checkout of the reviewed commit (`worktrees/`), `default_base` and `git` |
| shared | `sessions.py` | Claude Code's session files: transcripts, open sessions, deleting a fork |
| shared | `claude_cli.py` | Running `claude` as a child: the binary, its environment and flags, a JSON call, a stream-json reader |
| shared | `jobs.py` | Detached workers: spawn, file locks, kill, one Check again at a time; owns `out/specs/jobs/` and the lock sidecars (`out/<page>/locks/chat.lock`, `drafts.lock`, `rereview.lock`, `send.lock`) |
| shared | `jobstate.py` | The job state machine that drafting, re-review, side chat and Send fixes share ([Background jobs](behaviour.md#background-jobs)) |

Who imports whom (an arrow means "imports"):

```
entry     finding_viewer ─► hook, render, handoff, triage, setup_cmds   (and config, paths, sessions)

features  hook ─► extract, parse, render, triage        setup_cmds ─► triage
          triage ─► chat, draft, freshness, handoff, render, rereview, reviews
          handoff ─► draft, freshness, rereview, send    render ─► extract, parse, reviews
          reviews ─► chat, draft, freshness, rereview    rereview ─► chat, draft, extract, freshness
          draft ─► chat, send                            send ─► chat
          chat, extract, freshness, parse ─► no feature
```

Shared modules, and the modules that import each one:

- `store`: chat, draft, freshness, handoff, hook, render, rereview, reviews, triage
- `jobstate`: chat, draft, handoff, rereview
- `severity`: chat, extract, handoff, hook, parse, rereview
- `claude_cli`: chat, draft, extract, rereview, send, setup_cmds
- `jobs`: chat, draft, freshness, handoff, jobstate, rereview, reviews, triage
- `sessions`: chat, claude_cli, draft, finding_viewer, handoff, rereview, reviews, triage
- `config`: chat, draft, extract, finding_viewer, hook, render, rereview, sessions, setup_cmds, store, triage
- `paths`: chat, config, draft, finding_viewer, handoff, hook, jobs, render, rereview, reviews, setup_cmds,
  store, triage

The import rule:

- `finding_viewer` → features → shared. Nothing imports `finding_viewer`.
- A shared module imports only shared modules.
- Features import each other only through public names: no `_private` name is used outside its module.
- Every import is at the top of its module, and the imports form no cycle. `render` does not import `triage`: opening
  the page is the entry point's job (`finding_viewer.render_and_open`), so `triage.cmd_job` can import `render`.

## Data model

Every file is JSON. The files of a page are in its folder, `out/<page>/`. The names below are the ones the code writes;
an older file can lack some of them.

- **Page data**: `out/<page>/review.json`, from `render.build_page()`. `page.html` beside it carries the same JSON in
  its `<script type="application/json" id="data">` block. Top level: `page`, `title`, `subtitle`, `generated`,
  `session_id`, `review_session`, `pr`, `pr_author`, `base`, `ref`, `blob_ref`, `ref_label`, `merge_base`, `github`,
  `repo_root`, `repo_name`, `notes`, `findings`, `files` (by path: `lines`, `lang`, `added`, `removed`, `hunks`,
  `changed`, `truncated`), and `rereviewed` after a re-review. A finding: `id`, `severity`, `title`, `body`,
  `recurring`, `meta`, `locations` (`raw`, `path`, `start`, `end`, `ambiguous`), and `rereview` (`status`, `at`, `why`)
  after a re-review. A `rereviewed` entry: `from`, `to`, `at`, `commits`, `rewritten`, `counts`, `discarded_drafts`.
- **`decisions.json`** (`PUT /api/decisions/<page>`, `triage.py`): `page`, `title`, `subtitle`, `session_id`,
  `pr`, `repo_root`, `updated` (the tab's clock), `rereviews` (how many re-reviews the tab saw), `saved_at` (set by the
  server), `findings`. Each finding id maps to `status` (`post`, `fix`, `skip` or null), `note`, `mode` (`note` or
  `comment`), `title`, `severity`, `anchor` (`path`, `line`, `in_diff`), `body`, and after a re-review `recheck` (the
  head it changed at) and `anchor_reset` (`path`, `line`, `at`).
- **`chat.json`** (`store.chat_path`): `threads`, keyed by finding id, and `updated`. A thread has
  `claude_session`, `cwd`, `mode` (`finding` or `fork`), `messages` (`role` is `user`, `claude` or `system`; `text`,
  `at`; a Claude reply carries `steps`, `error`, `model`, `cost`, `total` and `context_tokens`, a stopped one `stopped`;
  the system line after a re-review carries `rereview` (the new head) and `claude`, the note Claude reads next), and
  `pending` (null, or `started`, `status`, `steps`, `job`, `pid`, `session`, `stopping`).
- **`drafts.json`** (`draft.py`): `status` (`drafting`, `ready`, `posting`, `posted`, `failed`), `started`,
  `version`, `phase`, `steps`, `error`, `stopped`, `stopping`, `review_session`, `cwd`, `fork`, `job`, `pid`,
  `drafted_at`, `ended`, `review` (`body`, `event`, `comments`: `finding`, `title`, `path`, `line`, `start_line`,
  `level`, `body`, `own`, `in_diff`), `posted` (`url`, `id`, `at`, `inline`, `event`), `telling`, and `told` (`ok`,
  `to`, `error`, `ended`).
- **`send.json`** (`handoff.py`): `status` (`sending`, `sent`, `failed`), `job`, `pid`, `started`, `ended`, `error`,
  `session` (the review session's id), `to` (its name), `cwd`, `version` (the triage version sent).
- **`rereview.json`** (`rereview.py`): `status` (`reviewing`, `done`, `failed`), `started`, `ended`, `job`,
  `pid`, `fork`, `review_session`, `cwd`, `from`, `to`, `commits`, `rewritten`, `discard_drafts`, `steps`, `error`,
  `stopped`, `stopping`, `committing` (the steps still to take while the update runs), and `result` (`to`, `counts`,
  `message`).
- **`freshness.json`** (`freshness.py`): `status` (`current`, `behind`, `rewritten`, `unknown`), `ref` (the
  reviewed commit), `head`, `checked_at` (seconds since the epoch), `reason` (when `unknown`), `new_commits` (`sha`,
  `title`, `author`, `date`, `url`), `affected` (by finding id: `kind` (`deleted`, `lines` or `file`) and `locations`).
  For PR reviews also `pr`, `state` (GitHub's PR state), `mine` (`state`, `at`, `commit`, `new_since`, `covers_head`),
  `done` and `compare_url`.

## Job lifecycle

A draft, a post, a re-review, a side chat reply and a Send fixes all follow one path (`jobstate.py`, `jobs.py`):

```
start     the server writes the job's state (job id, a status such as drafting, pid null), then jobs.spawn()
          starts the worker and records its pid at once; a side chat keeps this record as its thread's `pending`.
  |
  v
running   the worker writes progress only while the state still carries its job id (jobstate.update_if)
  |
  +--> done, or failed (with `stopped` set when a Stop ended it)
  |
  +--> Stop: `stopping` is written first, then SIGTERM to the worker's process group, SIGKILL after 3 s
  |     (jobs.kill_group), then the stop is recorded (jobstate.record_stop)
  |
  +--> lost: the pid is gone, or there is no pid and the state has no job id or is 30 s old (jobs.lost). The next
        read settles it as failed: jobstate.settle records it, jobstate.end ends what is left of the group and drops
        the spec
```

- A live pid is never given up on, whatever the clock says (a laptop that slept mid-draft).
- Watchdogs, one per worker: drafting 600 s (`draft.DRAFT_TIMEOUT_SECONDS`), re-review 900 s
  (`rereview.TIMEOUT_SECONDS`), side chat 300 s (`chat.TIMEOUT_SECONDS`), the post's `gh api` call 60 s
  (`draft.POST_TIMEOUT_SECONDS`), telling 180 s (`send.TIMEOUT_SECONDS`), sending fixes 180 s
  (`send.TIMEOUT_SECONDS`).
- Stop works for drafting, re-review and side chat. Posting and telling cannot be stopped (GitHub may already have
  the review), and neither can Send fixes or Check again (it has its own timeouts).
- The server reaps the workers it started (`jobs.spawn`), so a finished one is not mistaken for alive.
- A stop signals a process group only when the pid's command line still names the job's spec (`jobs.is_worker`).
- A re-review locks the page: every write to it answers 423 until the worker ends (see [Routes](#routes)).

## Routes

| Route | Purpose |
|---|---|
| `GET /` | The list of all reviews (`web/index.html`) |
| `GET /api/reviews` | Every stored page with triage counts, chats and the last freshness check |
| `PUT /api/archive/<page>` | `{"archived": true\|false}`: archive the page's card (every render in its group), or bring it back |
| `DELETE /api/reviews/<page>` | Delete a page with its triage, chats, freshness cache and spec (409 while a job runs on it) |
| `GET /<page>.html` | A review page |
| `GET` / `PUT /api/decisions/<page>` | Load / save triage |
| `GET /api/chat/<page>` | Chat threads (marks dead workers as failed) |
| `POST /api/chat/<page>/<finding-id>` | Send a chat message (202, answered by a detached worker) |
| `GET /api/freshness/<page>[?force=1]` | Has the PR moved past the reviewed commit (one check per page at a time; others wait for it) |
| `GET /api/activity/<page>` | The Running panel: jobs running and ended in the last 10 minutes, Claude sessions to resume; and the re-review: whether the page can run one (and why not), and the last one's state, which locks the page |
| `POST /api/stop/<page>/draft`, `POST /api/stop/<page>/rereview`, `POST /api/stop/<page>/chat/<finding-id>` | Stop drafting, a re-review or a side chat reply |
| `GET /api/handoff/<page>` | The review session (name when open), whether the page can draft, whether the code is yours (`own_code`: no PR, or a PR the `gh` user opened), the drafts so far, and the last Send fixes (`send`) |
| `POST /api/draft/<page>` | Draft comments: a fork of the review session drafts the review (202; see `draft.py`) |
| `POST /api/draft/<page>/post` | Post the edited drafts as one GitHub review (202), then tell the review session |
| `DELETE /api/draft/<page>` | Discard the drafts and delete the fork (409 while drafting, posting or telling) |
| `POST /api/rereview/<page>` | `{"discard_drafts": true\|false}`: review the PR's new commits in a fork of the review session (202; see [Review the new commits](behaviour.md#review-the-new-commits)). 409 while another job runs on the page, and when unposted drafts are not confirmed for discarding (`drafts: true` in the answer) |
| `POST /api/send-fixes/<page>` | Send fixes to session: relay the triage to the open review session now (202, see `handoff.send_fixes`); 409 when the code is not yours, the review session is not open or a send is still running, 400 with no Fix finding, 500 when the worker does not start (the answer is the failed state), 404 for an unknown page |
| `GET` / `PUT /api/settings` | Chat model and context mode |
| `GET /api/ping`, `POST /api/shutdown` | Version check, restart |

While a re-review runs on a page, every route that writes to it answers **423** with the reason: `PUT /api/decisions`,
`POST /api/chat`, `POST` / `DELETE /api/draft` and `/api/draft/<page>/post`, `POST /api/send-fixes`, `PUT /api/archive` (for any render of the
card), `DELETE /api/reviews` and another `POST /api/rereview`. Stop on the re-review stays allowed. `PUT
/api/decisions` also answers 409 to a tab that loaded the page before its last re-review (the payload's `rereviews`
count is behind), since it would put the old findings and lines back.

## State files

Every path is in the data folder: `FINDING_VIEWER_DATA`, else `CLAUDE_PLUGIN_DATA`, else `~/.local/share/a-bit-human-review`.

| File | Contents | Kept |
|---|---|---|
| `out/<page>/` | One folder per page, named by its creation time and title | newest 40 |
| `out/<page>/review.json` | The review data (and, once re-reviewed, `rereviewed` and each finding's `rereview`) | with its page |
| `out/<page>/page.html` | The page as one file, with the data inlined, for opening as `file://` | with its page |
| `out/<page>/decisions.json` | Status (`post`, `fix` or `skip`), note and its mode (`note` for Claude, or `comment` posted as written), anchor, title, body per finding; after a re-review, `recheck` until you decide again, `anchor_reset` until you pick a line or the next re-review | with its page |
| `out/<page>/chat.json` | Threads (with a line from the page after a re-review), `claude` session ids, pending state (worker pid, job id, session) | with its page |
| `out/<page>/freshness.json` | Last freshness check | with its page |
| `out/<page>/drafts.json` | Drafts, their status, the fork's id, the worker's pid and job id, when it ended, the posted review's URL, whether the session was told | with its page |
| `out/<page>/rereview.json` | The last re-review: reviewing (the page's lock), done with its counts, or failed with the reason; the fork, worker pid and job id, from and to which commit, whether drafts go | with its page |
| `out/<page>/send.json` | The last Send fixes: sending, sent or failed with the reason, the review session's name, the version sent | with its page |
| `out/<page>/locks/` (`chat.lock`, `drafts.lock`, `send.lock`, `rereview.lock`) | Empty sidecars for the file locks (the last one is the page lock, taken by every write to the page) | with its page |
| `out/<page>/.staging-<job>/` | A re-review's new files, complete, before they are put in place | until its update ends; a dead worker's are put in place or deleted by the next read |
| `out/latest.html` | A symlink to the newest page's `page.html`; each render re-points it | one |
| `out/archived.json` | Archived pages and when they were archived; entries for deleted pages drop out on the next write | |
| `out/specs/<page>.json` | Spec handed from the hook to the renderer | newest 40 |
| `out/specs/jobs/<job>.json` | Spec handed from the server to a job worker (prompt, finding, review to post) | until the job ends, is stopped or found dead, or its page is deleted |
| `state.json` | Review fingerprints already shown, per session | 20 × 50 sessions |
| `inject_state.json` | Page versions each session has seen | 50 sessions |
| `settings.json` | Chat model, context mode and the single-key shortcuts switch | |
| `config.json` | Settings; optional, written by you (see README) | yours |
| `worktrees/<sha12>` | Detached checkouts of reviewed commits, registered in the reviewed repo | newest 3 |
| `clones/<owner>/<repo>` | Clones of reviewed repos not found on this machine | kept; delete freely |
| `viewer.log` | Server starts, hook / render / chat / job errors, workers' output | |
| `.migrate.lock` | Empty lock held while an old checkout's data moves into the data folder, or while flat pages get their folders | |

## Known gaps

- On the list, a PR's newest render is not always its newest reviewed commit (a review of an older commit can be
  rendered later), so the lead card can show "N new commits" while an earlier render is up to date.
- The Stop hook reads the whole transcript after every turn, in every project, to look for the viewer's name; once a
  session has named it, every later turn parses the transcript too. Fine for normal sessions; it is the first place
  to look if very long sessions feel slow to end a turn.
- A review of another PR that a subagent ran from a feature-branch session counts as "only mentioned": sidechains
  are not parsed, so the subagent's `gh pr diff N` is not seen, and the page shows the session branch's PR. A PR URL
  in a prompt does not count; any `gh pr view N` in the parent's own turns does.
- `chat.json` stores each thread's working directory as an absolute path. Moving the data folder breaks resuming threads
  that ran in a worktree; new threads are unaffected.
- A worker's pid could be reused by another process after the worker died (unlikely within minutes); the state then
  reads as running until that process exits. Stop then only records the stop: it signals a process group only when
  the pid's command line still names that job's spec.
- A Stop that lands before the job's `init` event (its first second or so) cannot know the new session's id, so a
  draft fork's, a re-review fork's or a first chat message's transcript is left behind.
- The re-review logs the fork's `total_cost_usd`, which for a fork can include the review session's own spend (see
  [Side chat](behaviour.md#side-chat)); one result cannot tell the two apart, so the log also gives the tokens the run read.
- A re-review whose answer leaves out a finding that is still open fails (the page keeps every finding, so it cannot
  guess one); running it again usually helps.
- `prune()` can delete a page while a re-review runs on it, if 40 newer pages appear meanwhile; the worker then finds
  its state gone and writes nothing. Taking its page lock re-creates `out/<page>/locks/` with an empty `rereview.lock`,
  and with it the folder `out/<page>/`, which has no `page.html`. `prune` and the list ignore it, since they count only
  folders that hold a `page.html`, and nothing removes it.
- A render still running the old version across an update writes a flat `out/<page>.html`, which the next run folds into
  its folder. A job worker (draft, side chat or re-review) still running the old version finds its flat state file gone,
  so it writes nothing more; the server then reads the last state that was folded in, shows the job running until its
  process ends, and then reads it as failed, so its result is lost. An empty `<page>.<name>.lock` that it leaves is
  folded into `locks/` too.
