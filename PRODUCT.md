# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Developers who run code reviews with Claude Code and want a human decision before any AI finding becomes a PR
comment. Today the only user is the author, triaging Claude's reviews of teammates' PRs. The tool is
meant to go public eventually for any Claude Code user, so future work must not assume the author's setup.

The scene: a review turn has just ended in the terminal, the page opens in the browser beside it, the user decides
every finding, then goes back to the same Claude session to post what they approved.

## Product Purpose

A human gate between an AI code review and the pull request. Each finding sits next to the code it points at; the
reviewer marks it **Post** or **Don't post**, writes a note, picks the line the comment goes on, and can ask a side
Claude about it. The triage flows back into the review session, so Claude drafts and posts only what was approved.

Success is both of these at once:

- **Fewer, better comments.** Only findings worth a teammate's time get posted, in the reviewer's own words, on the
  right line.
- **Fast throughput.** A 15–20 finding review gets triaged in minutes.

## Positioning

It reviews the reviewer instead of being another reviewer. It accepts whatever shape the review came in
(`ReportFindings` calls, saved reports, markdown, tables, prose), shows code from the PR's own repo at the reviewed
commit, and closes the loop back into the originating Claude Code session through hooks, with no copy-paste. The page
itself never posts anything to GitHub.

## Operating Context

- **Trigger:** only on request. `/findings` opens the latest review in the session, the list, a saved report, or
  prints the current triage; or the prompt that asks for the review names the viewer ("…and open it in the finding
  viewer"), and a Stop hook opens the page when that turn ends. A review nobody asked the viewer for is never read.
- **Hand-back:** a UserPromptSubmit hook injects the triage into the next prompt of the session that ran the review.
  A Send action that relays the triage to that session (`send.py`) is in progress.
- **Surfaces:** a list of all reviews at `http://127.0.0.1:47321/` and one review page per render.
- **Around each finding:** PR freshness (new commits, force-push, merged/closed, "you already approved"), diff
  markers and hunk ranges, a per-finding side chat via headless `claude -p` (read-only tools, optionally forking the
  review session).
- **Input:** keyboard-driven (`j`/`k`, `c`, `h`, `o`) alongside the mouse.
- **Where it's used:** a desktop browser next to the terminal; light or dark following the OS.
- **What gets reviewed:** mostly GitHub PRs via `gh`; local branches and working trees too.

## Capabilities and Constraints

- **Durable rule:** Python standard library only. No pip installs. The only external tools are `git`, `gh` and the
  `claude` CLI.
- **Current, not committed** (the user chose not to make these binding; they may change): page source split under `web/`
  (markup, CSS, script, shared CSS) and inlined by the server, so each served page is still one file; plus cdnjs
  libraries (highlight.js, marked, DOMPurify) and no build step; a server bound to `127.0.0.1`; desktop-only use.
- **Real costs:** reading findings with Claude takes about 45–60 s and $0.10–0.30 per review; side-chat messages
  cost money too. Waits and costs are shown, not hidden.
- **GitHub limit:** inline comments are accepted only inside diff hunks. Each anchor records `in_diff`; anchors
  outside go into the review body.
- **Terminology:** finding, severity (critical / warning / suggestion / question / finding, from must-fix /
  should-fix / nit / question), Post / Don't post, note, anchor, triage, hand-off, render, freshness, side chat,
  review session.
- **Author-specific today:** saved review reports (`.claude/.history/code-reviews/*.md`). These must stay
  an optional source once the tool is public.
- **Open decisions:** how a public user installs it (today the hooks and the `/findings` command are wired by hand
  in three places); whether the localhost-only and no-build-step choices survive going public.

## Brand Commitments

- **Name:** a-bit-human-review (the repo name; not yet confirmed as the public name).
- **Voice, observed but not confirmed as binding:** plain and literal, second person, says exactly what happens and
  what it costs ("takes about a minute and $0.10–0.30"), no hype.
- No logo, mark or other identity assets exist.

## Evidence on Hand

- `README.md` and `docs/ARCHITECTURE.md` describe the real behaviour.
- `out/` holds real rendered reviews, triage, chats and freshness checks from the author's own reviews.
  It is gitignored runtime data containing private code and teammate PRs: never publish it as a demo or screenshot
  without replacing it with synthetic content.
- There are no other users, testimonials, usage metrics, time-saved numbers, screenshots or press. Do not invent
  any.

## Product Principles

1. **The human decides; the page never posts.** Every comment that reaches a PR went through an explicit Post
   decision by a person.
2. **Saying no is a first-class outcome.** Rejecting a finding should be as quick and as respectable as posting one;
   the goal is fewer comments, not more.
3. **Speed comes from decisiveness, not skimming.** The code in context and the decision belong together, reachable
   from the keyboard, so a whole review moves in minutes.
4. **Tell the truth about state.** Say when the PR moved past the review, when Claude guessed a line, when an anchor
   cannot be inline, and what a call costs and how long it takes.
5. **Any review, any repo.** Works whatever format the review came in and wherever the code lives; anything
   organisation-specific stays an optional adapter.
