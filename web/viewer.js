const D = JSON.parse(document.getElementById('data').textContent);
const ORDER = ['critical', 'warning', 'suggestion', 'question', 'finding'];
const LABEL = { critical: 'Critical', warning: 'Warning', suggestion: 'Suggestion', question: 'Question', finding: 'Finding' };
const CTX = 12, STEP = 20;
const SERVED = location.protocol === 'http:' || location.protocol === 'https:';
// A re-review rebuilds the page under the same name: a copy kept before it (old findings, old lines) is not this page's.
const LS_KEY = 'finding-viewer:' + D.page + ((D.rereviewed || []).length ? ':' + D.rereviewed.length : '');
const CHAT_HIDDEN_KEY = 'finding-viewer:chat-hidden';
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
// Rebuild a container and keep keyboard focus on the equivalent element, so Enter on a button never drops focus to <body>.
// When there is no equivalent, `fallback(old)` (asked before the rebuild) lists what to focus instead, in order:
// selectors inside the container, or elements.
function rerender(el, html, attrs, fallback) {
  const a = el.contains(document.activeElement) ? document.activeElement : null;
  const want = a ? attrs.filter(k => a.hasAttribute(k)).map(k => [k, a.getAttribute(k)]) : [];
  const instead = a && fallback ? fallback(a) : [];
  el.innerHTML = html;
  if (!a) return;
  const same = want.length ? el.querySelector(want.map(([k, v]) => `[${k}="${CSS.escape(v)}"]`).join('')) : null;
  (same || instead.map(s => typeof s === 'string' ? el.querySelector(s) : s).find(x => x?.isConnected))?.focus({ preventScroll: true });
}
// What identifies a focusable element inside the code cards: a line number, or a "more lines" button.
const CARD_KEYS = ['data-path', 'data-line', 'data-act', 'data-key'];
// A link in a card (Open in VS Code, GitHub, a finding badge) has no equivalent after a rebuild: focus its card.
const cardFallback = old => old.closest('.card[id]') ? ['#' + CSS.escape(old.closest('.card[id]').id)] : [];
// Smooth scrolling, unless the system asks for less motion.
const scrollBehavior = () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
const base = p => p.split('/').pop();
// A count with its noun: "1 comment", "2 comments".
const phrase = (k, one, many) => `${k} ${k === 1 ? one : many}`;
// Screen readers hear events that leave no status text of their own (anchor moves, Copied, draft states, chat replies).
// Clearing first and setting a moment later makes a repeated message count as new.
let announceTimer = null;
function announce(text) {
  const el = document.getElementById('announce');
  if (!el) return;
  el.textContent = '';
  clearTimeout(announceTimer);
  announceTimer = setTimeout(() => { el.textContent = text; }, 100);
}
const hiddenSev = new Set();
const windows = {};
const hlCache = {};
let statusFilter = 'all';
let current = 0;
let T = { updated: null, findings: {} };
let saveTimer = null;
let savePending = false;  // a save is scheduled and has not started yet (saveTimer is never reset after it fires)
let CHAT = { threads: {} };
let chatTimer = null;
let lastChatSig = '';
let lastListSig = '';
let pendingThreads = [];
let SETTINGS = { settings: { model: '', context: 'finding', shortcuts: true }, models: [] };
const shortcutsOn = () => SETTINGS.settings.shortcuts !== false;
let FRESH = null;
let HANDOFF = null;
let freshTimer = null;

const lineMap = {};
D.findings.forEach((f, fi) => f.locations.forEach(loc => {
  if (!loc.path) return;
  const m = lineMap[loc.path] ??= {};
  for (let l = loc.start; l <= loc.end; l++) (m[l] ??= []).some(x => x.fi === fi) || m[l].push({ fi, id: f.id, sev: f.severity });
}));

/* ---------- triage state ---------- */

const tri = id => (T.findings[id] ??= { status: null, note: '', mode: 'note', anchor: null });
// A review with no PR is your own code before a PR exists (local mode): nothing is posted, so a finding is Fix or
// Won't fix (stored as skip). A finding stored as Post there, from before local mode, counts as undecided; the stored
// value stays until you pick again.
const localMode = () => !D.pr;
const storedStatus = f => T.findings[f.id]?.status || null;
const statusOf = f => { const s = storedStatus(f); return s === 'post' && localMode() ? null : s; };
// The triage button that keeps the focus when the one that had it is rebuilt away.
const firstTri = () => localMode() ? '[data-tri="fix"]' : '[data-tri="post"]';
const noteOf = f => (T.findings[f.id]?.note || '').trim();
// What the text box holds: a note Claude follows when it writes the comment, or the comment itself, posted as written.
const modeOf = f => f && !localMode() && T.findings[f.id]?.mode === 'comment' ? 'comment' : 'note';
const ownComment = () => modeOf(D.findings[current]) === 'comment' && !fixing();
// A finding the new commits fixed (rereview.py): it stays listed with its triage, but is not open and is not posted.
const isFixed = f => f?.rereview?.status === 'fixed';
// Your own code (no PR, or a PR you opened; /api/handoff says which): a finding can be fixed in the code before anyone
// else reviews it, so Fix joins Post and Don't post. A finding already marked Fix keeps its button.
const ownCode = () => !D.pr || !!HANDOFF?.own_code;
const canFix = f => ownCode() || (!!f && statusOf(f) === 'fix');
// A Fix finding's note is always a note for Claude: there is no comment to write.
const fixing = () => { const f = D.findings[current]; return !!f && statusOf(f) === 'fix'; };

function defaultAnchor(f) {
  const loc = f.locations.find(l => l.path);
  if (!loc) return null;
  const file = D.files[loc.path];
  const inHunk = l => (file.hunks || []).some(([s, e]) => l >= s && l <= e);
  for (let l = loc.start; l <= Math.min(loc.end, file.lines.length); l++) if (inHunk(l)) return { path: loc.path, line: l };
  return { path: loc.path, line: Math.min(loc.start, file.lines.length) };
}
function anchorOf(f) {
  const a = T.findings[f.id]?.anchor;
  return a && D.files[a.path] ? a : defaultAnchor(f);
}
function commentable(a) {
  const file = a && D.files[a.path];
  return !!file && (file.hunks || []).some(([s, e]) => a.line >= s && a.line <= e);
}
function counts() {
  const c = { post: 0, skip: 0, fix: 0, total: 0, fixed: 0 };
  D.findings.forEach(f => {
    if (isFixed(f)) { c.fixed++; return; }
    c.total++;
    const s = statusOf(f);
    if (s) c[s]++;
  });
  c.decided = c.post + c.skip + c.fix;
  c.open = c.total - c.decided;
  return c;
}

function payload() {
  const findings = {};
  for (const f of D.findings) {
    const a = anchorOf(f), t = T.findings[f.id];
    // The stored status, also a Post that local mode counts as undecided: it stays until you pick again.
    findings[f.id] = {
      status: storedStatus(f), note: t?.note || '', mode: t?.mode === 'comment' ? 'comment' : 'note', title: f.title, severity: f.severity,
      anchor: a ? { path: a.path, line: a.line, in_diff: commentable(a) } : null, body: f.body,
      // Marks a re-review left until you look again: the finding changed after you chose Post; your line was reset.
      ...(t?.recheck ? { recheck: t.recheck } : {}), ...(t?.anchor_reset ? { anchor_reset: t.anchor_reset } : {}),
    };
  }
  // `rereviews` lets the server refuse a save from a tab that still shows the findings before a re-review.
  return { page: D.page, title: D.title, subtitle: D.subtitle, session_id: D.session_id, pr: D.pr, repo_root: D.repo_root,
    updated: T.updated, rereviews: (D.rereviewed || []).length, findings };
}

function setSaved(text, warn = false) {
  const el = document.getElementById('saved');
  if (el.textContent === text && el.classList.contains('warn') === warn) return;  // a live region: same text, say nothing
  el.textContent = text;
  el.classList.toggle('warn', warn);
}

function persist() {
  T.updated = new Date().toISOString();
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(T));
    // The copies kept before each earlier re-review of this page are not this page's any more.
    for (let k = 0; k < (D.rereviewed || []).length; k++) localStorage.removeItem('finding-viewer:' + D.page + (k ? ':' + k : ''));
  } catch (e) { /* private mode: the server copy still works */ }
  updateToolbar();
  if (!SERVED) return;
  clearTimeout(saveTimer);
  setSaved('Saving…');
  savePending = true;
  saveTimer = setTimeout(pushRemote, 400);
}

async function pushRemote() {
  savePending = false;
  try {
    const r = await fetch('/api/decisions/' + encodeURIComponent(D.page), {
      method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-Finding-Viewer': '1' }, body: JSON.stringify(payload()),
    });
    if (r.ok) {
      saveRefused = false;
      setSaved(savedText());
      loadHandoff();
    }
    else if (r.status === 404) setSaved('Not saved to disk: the viewer server no longer has this review. Your triage is kept in this browser; use Copy triage to keep a copy.', true);
    else if (r.status === 423 || r.status === 409) {
      // Locked by a re-review (another tab started it), or this tab predates one: the server says which. A locked
      // save goes again when the page unlocks unchanged (applyLock).
      saveRefused = r.status === 423;
      const err = (await r.json().catch(() => null))?.error;
      setSaved(`Not saved: ${typeof err === 'string' && err.trim() ? err.trim() : 'the review is being re-reviewed.'}`, true);
      loadActivity();
    }
    else {
      // A save the server turned down for a reason it gives (a bad or too large body) is not fixed by a restart.
      const err = (await r.json().catch(() => null))?.error;
      const why = typeof err === 'string' ? err.trim() : '';
      setSaved(why ? `Not saved to disk: ${why}${/[.!?]$/.test(why) ? '' : '.'} Your triage is kept in this browser.`
        : 'Not saved to disk: the viewer server refused the save. Your triage is kept in this browser; run /findings to restart the server.', true);
    }
  } catch (e) {
    setSaved('Not saved to disk: the viewer server did not answer. Your triage is kept in this browser; run /findings to restart the server.', true);
  }
}

async function loadState() {
  let local = null, remote = null;
  try { local = JSON.parse(localStorage.getItem(LS_KEY) || 'null'); } catch (e) { local = null; }
  if (SERVED) {
    try {
      const r = await fetch('/api/decisions/' + encodeURIComponent(D.page), { cache: 'no-store' });
      const body = r.ok ? await r.json() : null;
      if (body && body.findings) {
        remote = { updated: body.updated, findings: {} };
        for (const [id, v] of Object.entries(body.findings)) {
          remote.findings[id] = { status: v.status || null, note: v.note || '', mode: v.mode === 'comment' ? 'comment' : 'note', anchor: v.anchor ? { path: v.anchor.path, line: v.anchor.line } : null,
            ...(v.recheck ? { recheck: v.recheck } : {}), ...(v.anchor_reset ? { anchor_reset: v.anchor_reset } : {}) };
        }
      }
    } catch (e) { remote = null; }
  }
  const pick = [local, remote].filter(x => x && x.updated).sort((a, b) => b.updated.localeCompare(a.updated))[0];
  if (pick) T = pick;
  if (SERVED && local?.updated && (!remote?.updated || local.updated > remote.updated)) pushRemote();
  if (!SERVED) setSaved('Opened as a file: triage stays in this browser. Use Copy triage, or reopen with /findings.', true);
  else if (remote?.updated) setSaved('Saved · Claude gets it with your next message');
}

/* ---------- rendering ---------- */

function splitHighlighted(html) {
  const lines = [], stack = [];
  let cur = '';
  const re = /(<span[^>]*>)|(<\/span>)|(\n)|([^<\n]+)|(<)/g;
  let m;
  while ((m = re.exec(html))) {
    if (m[1]) { stack.push(m[1]); cur += m[1]; }
    else if (m[2]) { stack.pop(); cur += m[2]; }
    else if (m[3]) { cur += '</span>'.repeat(stack.length); lines.push(cur); cur = stack.join(''); }
    else cur += m[0];
  }
  lines.push(cur);
  return lines;
}

function highlightedLines(path) {
  if (hlCache[path]) return hlCache[path];
  const f = D.files[path];
  let out = null;
  try {
    if (window.hljs && f.lang && hljs.getLanguage(f.lang)) {
      out = splitHighlighted(hljs.highlight(f.lines.join('\n'), { language: f.lang, ignoreIllegals: true }).value);
    }
  } catch (e) { out = null; }
  if (!out || out.length !== f.lines.length) out = f.lines.map(esc);
  return (hlCache[path] = out);
}

function markdown(src) {
  if (!src) return '';
  if (window.marked && window.DOMPurify) {
    try { return DOMPurify.sanitize(marked.parse(src, { gfm: true })); } catch (e) { /* fall through to raw text */ }
  }
  return `<pre class="raw">${esc(src)}</pre>`;
}

function renderHeader() {
  document.getElementById('title').textContent = D.title;
  document.getElementById('subtitle').textContent = D.subtitle;
  const meta = [];
  if (D.pr && D.github) meta.push(`<span><a href="${esc(D.github)}/pull/${esc(D.pr)}" target="_blank" rel="noopener">PR #${esc(D.pr)} on GitHub ↗</a></span>`);
  else if (localMode()) meta.push('<span><b>Local review</b> · no PR</span>');
  if (D.pr_author) meta.push(`<span>Opened by <b>${esc(D.pr_author)}</b></span>`);
  meta.push(`<span>Code: <b>${esc(D.ref_label)}</b></span>`);
  const again = (D.rereviewed || []).at(-1);
  if (again) meta.push(`<span>Re-reviewed at <b>${esc(short(again.to))}</b> (was ${esc(short(again.from))}, ${esc(String(again.at || '').replace('T', ' ').slice(0, 16))})</span>`);
  if (D.base) meta.push(`<span>Changed lines: <b>${esc(D.base)}</b>${D.merge_base && !D.base.startsWith('PR') ? ` (merge-base ${esc(D.merge_base)})` : ''}</span>`);
  meta.push(`<span>Generated ${esc(D.generated)}</span>`);
  meta.push('<span id="fresh-meta"></span>');
  document.getElementById('meta').innerHTML = meta.join('');
  if (D.notes.length) {
    const n = document.getElementById('notes');
    n.hidden = false;
    n.innerHTML = D.notes.map(esc).join('<br>');
  }
}

function updateToolbar() {
  const c = counts(), done = doneState();
  // Both outcomes count. On a done PR the Post count says what you marked, not where it goes; with no PR there is
  // no PR to go to.
  const posts = done ? `${c.post} marked to post` : D.pr ? phrase(c.post, 'goes to the PR', 'go to the PR') : `${c.post} to post`;
  const fixes = ownCode() || c.fix ? `<b class="fix">${c.fix} to fix</b> · ` : '';
  const tally = localMode() ? `<b class="fix">${c.fix} to fix</b> · <b class="keep">${c.skip} won't fix</b>`
    : `<b>${posts}</b> · ${fixes}<b class="keep">${phrase(c.skip, 'stays here', 'stay here')}</b>`;
  document.getElementById('progress').innerHTML =
    `<div class="bar"><span style="width:${c.total ? (100 * c.decided / c.total) : 0}%"></span></div>
     <span>${c.decided}/${c.total} decided · ${tally}${c.fixed ? ` · ${c.fixed} fixed in the new commits` : ''}</span>`;
  const segs = localMode()
    ? [['all', 'All', D.findings.length], ['open', 'To decide', c.open], ['fix', 'Fix', c.fix], ['skip', "Won't fix", c.skip]]
    : [['all', 'All', D.findings.length], ['open', 'To decide', c.open], ['post', 'Post', c.post], ...(ownCode() || c.fix || statusFilter === 'fix' ? [['fix', 'Fix', c.fix]] : []), ['skip', "Don't post", c.skip]];
  rerender(document.getElementById('statusFilter'), segs.map(([k, label, n]) =>
    `<button data-status="${k}" class="${statusFilter === k ? 'on' : ''}">${label} ${n}</button>`).join(''), ['data-status']);
  const sev = {};
  D.findings.forEach(f => sev[f.severity] = (sev[f.severity] || 0) + 1);
  rerender(document.getElementById('filters'), ORDER.filter(s => sev[s]).map(s =>
    `<button class="chip sev-${s}${hiddenSev.has(s) ? ' off' : ''}" data-sev="${s}"><span class="dot"></span>${LABEL[s]} ${sev[s]}</button>`).join(''), ['data-sev']);
  const btn = document.getElementById('draft');
  btn.hidden = !HANDOFF?.can_draft;
  if (HANDOFF?.can_draft) {
    const X = DS();
    btn.textContent = { drafting: 'Drafting…', ready: 'Review drafts', posting: 'Posting…', posted: '✓ Posted' }[X?.status]
      || (X?.review ? 'Review drafts' : 'Draft comments');
    const why = !X ? `A fork of ${HANDOFF.session?.name || 'the review session'} drafts the PR comments from your triage. You edit and approve them here; nothing is posted before that.`
      : X.status === 'failed' ? X.error || '' : '';
    // A done PR needs nothing more: the button stays usable but is no longer shown as the next step.
    btn.classList.toggle('send', !done);
    btn.title = [done?.title, why].filter(Boolean).join(' ');
    // While a re-review locks the page it cannot start drafting; it still opens drafts there are, to read them.
    const starts = !X || (X.status === 'failed' && !X.review);
    if (locked() && starts) { btn.setAttribute('aria-disabled', 'true'); btn.title = LOCK_WHY; }
    else btn.removeAttribute('aria-disabled');
  }
  // The toolbar label changes silently, so say each draft state once. A failure already shows as an alert in the
  // open dialog; it is said here only when the dialog is closed.
  const st = HANDOFF?.can_draft ? DS()?.status || '' : '';
  if (lastDraftStatus !== undefined && st !== lastDraftStatus) {
    // A stop was already said by the Running panel, where it was made.
    const say = DRAFT_SAY[st] || (st === 'failed' && !DS().stopped && !document.getElementById('drafts').open ? DS().error || 'The draft failed.' : '');
    if (say) announce(say);
  }
  if (HANDOFF) lastDraftStatus = st;
  updateSendButton();
}

/* ---------- Drafts: a fork of the review session drafts the comments, you approve, the server posts (draft.py) ---------- */

const DS = () => HANDOFF?.draft;
// Keep polling while something runs, including the fork telling the review session after a post.
const draftBusy = () => ['drafting', 'posting'].includes(DS()?.status) || (DS()?.status === 'posted' && !DS().told);
const EVENT_LABEL = { COMMENT: 'Comment', REQUEST_CHANGES: 'Request changes', APPROVE: 'Approve' };
// The level a posted comment leads with, in bold (draft.py LEVELS), and the one each severity starts at.
const LEVEL_LABEL = { 'must-fix': 'Must-fix', 'should-fix': 'Should-fix', suggestion: 'Suggestion', nit: 'Nit', question: 'Question', '': 'No level' };
const SEVERITY_LEVEL = { critical: 'must-fix', warning: 'should-fix', suggestion: 'suggestion', question: 'question' };
const defaultLevel = fid => SEVERITY_LEVEL[D.findings.find(f => f.id === fid)?.severity] || '';
let draftPoll = null, draftEdit = null, draftShownFor = '', draftError = '';
const DRAFT_SAY = { drafting: 'Drafting the comments.', ready: 'The drafts are ready to review.', posting: 'Posting the review to GitHub.', posted: 'Posted to GitHub.' };
let lastDraftStatus;  // undefined until the first hand-off answer, so the state on page load is not announced

function savedText() {
  const S = HANDOFF?.session;
  return S?.name ? `Saved · ${S.name} gets it with your next message there` : 'Saved · Claude gets it with your next message';
}

async function draftApi(method, suffix = '', body = null) {
  draftError = '';
  try {
    const r = await fetch(`/api/draft/${encodeURIComponent(D.page)}${suffix}`, {
      method, body: body && JSON.stringify(body),
      headers: { 'X-Finding-Viewer': '1', ...(body ? { 'Content-Type': 'application/json' } : {}) },
    });
    if (!r.ok) draftError = (await r.json().catch(() => ({}))).error || 'The viewer server refused the request and gave no reason. Run /findings to restart it, then try again.';
  } catch (e) {
    draftError = 'Viewer server is off. Run /findings to restart it.';
  }
}

async function startDraft() {
  if (locked()) return lockedSay();
  // The server drafts from the saved triage, so save any pending change first.
  if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; await pushRemote(); }
  draftEdit = null;
  await draftApi('POST');
  openDrafts();
  loadHandoff();
  loadActivity();
}

async function postDrafts() {
  const R = draftEdit;
  if (!R) return;
  if (locked()) return lockedSay();
  await draftApi('POST', '/post', { event: R.event, body: R.body, comments: R.comments.map(c =>
    ({ path: c.path, line: c.line, start_line: c.start_line, level: c.level || '', body: c.body, include: c.include !== false })) });
  await loadHandoff();
  renderDrafts(true);
  loadActivity();
}

async function discardDrafts() {
  if (locked()) return lockedSay();
  await draftApi('DELETE');
  draftEdit = null;
  if (!draftError) closeDrafts();
  loadHandoff();
}

// A modal <dialog>: focus moves in, Tab stays inside, and close() returns focus to the button that opened it.
function openDrafts() {
  const d = document.getElementById('drafts');
  if (!d.open) d.showModal();
  renderDrafts(true);
}

function closeDrafts() { document.getElementById('drafts').close(); }

/* ---------- Send fixes to session: the triage goes to the open review session now (handoff.send_fixes) ---------- */

const SS = () => HANDOFF?.send;
const sendBusy = () => SS()?.status === 'sending';
const SEND_HINT = 'Type any prompt in the review session and it gets the triage.';
let sendError = '';  // a send the server refused (no open session, nothing to fix): shown until the next try
let lastSendStatus;  // undefined until the first hand-off answer, so the state on page load is not announced
let sendPosting = false;  // a POST is in flight: set before its first await, so a second click cannot send again

function updateSendButton() {
  const btn = document.getElementById('send-fixes'), why = document.getElementById('send-why');
  const X = SS(), to = HANDOFF?.session?.name || 'the review session';
  const busy = sendBusy() || sendPosting;
  const st = X?.status || '';
  // A refusal said about an earlier state is not shown for this one.
  if (lastSendStatus !== undefined && st !== lastSendStatus) sendError = '';
  // A send of the triage as it is now: once it changes, the button offers to send again.
  const current = !!X && X.version === HANDOFF?.version;
  const sent = current && X.status === 'sent';
  const error = sendError || (current && X.status === 'failed' ? X.error || `The fixes were not sent. ${SEND_HINT}` : '');
  // Your own code only, with something to fix; a send that runs stays on show.
  btn.hidden = !SERVED || !ownCode() || !(counts().fix || busy);
  btn.textContent = busy ? 'Sending…' : sent ? '✓ Sent' : 'Send fixes to session';
  btn.title = busy ? `Sending your triage to ${X?.to || to}.`
    : sent ? `Sent to ${X.to || to}. Change the triage to send it again.`
    : `Sends your triage to ${to} now, so Claude starts on the fixes without waiting for your next message there.`;
  if (busy) btn.setAttribute('aria-disabled', 'true');
  else if (locked()) { btn.setAttribute('aria-disabled', 'true'); btn.title = LOCK_WHY; }  // as the Draft button does
  else btn.removeAttribute('aria-disabled');
  why.hidden = btn.hidden || busy || !error;
  why.textContent = error;
  // Each outcome is said once.
  if (lastSendStatus !== undefined && st !== lastSendStatus) {
    if (st === 'sending') announce(`Sending the fixes to ${X?.to || to}.`);
    else if (st === 'sent') announce(`Sent to ${X.to || to}.`);
    else if (st === 'failed') announce(X.error || `The fixes were not sent. ${SEND_HINT}`);
  }
  if (HANDOFF) lastSendStatus = st;
}

async function sendFixes() {
  if (sendBusy() || sendPosting) return;
  // The triage is already with the session: a click does not send it again until the triage changes.
  if (SS()?.status === 'sent' && SS().version === HANDOFF?.version) {
    announce('Already sent. Change the triage to send it again.');
    return;
  }
  if (locked()) return lockedSay();
  sendPosting = true;  // before the first await, so a second click returns above
  updateSendButton();
  try {
    // The server sends the saved triage, so save a change still pending first.
    if (savePending) { clearTimeout(saveTimer); saveTimer = null; await pushRemote(); }
    sendError = '';
    try {
      const r = await fetch('/api/send-fixes/' + encodeURIComponent(D.page), { method: 'POST', headers: { 'X-Finding-Viewer': '1' } });
      const body = await r.json().catch(() => null);
      // A refusal that answers with the send state (the worker did not start) shows that state, and nothing else.
      if (!r.ok && body?.status) { if (HANDOFF) HANDOFF.send = body; }
      else if (!r.ok) sendError = (typeof body?.error === 'string' && body.error.trim()) || `The viewer server refused to send the fixes. ${SEND_HINT}`;
      else if (HANDOFF && body) HANDOFF.send = body;
    } catch (e) {
      sendError = `The viewer server did not answer, so nothing was sent. ${SEND_HINT}`;
    }
  } finally {
    sendPosting = false;
  }
  if (sendError) announce(sendError);
  updateSendButton();
  loadHandoff();
  loadActivity();
}

const postCount = () => (draftEdit?.comments || []).filter(c => c.include !== false && c.body.trim()).length;

function renderDrafts(force = false) {
  if (!document.getElementById('drafts').open) return;
  const X = DS(), who = HANDOFF?.session?.name || 'the review session';
  // Only re-render when the drafts' state changes: a re-render while typing would move the cursor.
  const key = [X?.status, X?.drafted_at, (X?.steps || []).length, !!X?.told, draftError].join('|');
  if (!force && key === draftShownFor) return;
  draftShownFor = key;
  const body = document.getElementById('drafts-body'), foot = document.getElementById('drafts-foot');
  const sub = document.getElementById('drafts-sub');
  document.getElementById('drafts-title').textContent = `Draft review · PR #${D.pr}`;
  const err = draftError || (X?.status === 'failed' ? X.error : '');
  const errHtml = err ? `<span class="err" role="alert">${esc(err)}</span>` : '';
  sub.textContent = X?.drafted_at ? `drafted by a fork of ${who} · ${new Date(X.drafted_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}` : '';
  if (!X || (X.status === 'failed' && !X.review)) {
    body.innerHTML = `<p>A fork of <b>${esc(who)}</b> drafts the PR comments from your triage, with the whole review conversation as context. Comments you wrote yourself go in word for word. You edit and approve them here; nothing reaches GitHub before that.</p>`;
    foot.innerHTML = `${errHtml}<span class="grow"></span><button class="tb primary" data-act="draft">Draft comments</button>`;
  } else if (X.status === 'drafting') {
    const steps = (X.steps || []).map(s => `<li>${esc(s)}</li>`).join('');
    body.innerHTML = `<p>A fork of <b>${esc(who)}</b> is drafting the comments from your triage. Usually one to three minutes.</p>${steps ? `<ul class="dsteps">${steps}</ul>` : ''}`;
    foot.innerHTML = '<span class="grow">You can close this; the toolbar button says when the drafts are ready.</span>';
  } else if (X.status === 'posting') {
    body.innerHTML = '<p>Posting the review to GitHub…</p>';
    foot.innerHTML = '';
  } else if (X.status === 'posted') {
    const P = X.posted || {}, T = X.told;
    const told = !T ? `Telling <b>${esc(who)}</b> what was posted…`
      : T.ok ? `✓ <b>${esc(T.to)}</b> was told, so it will not post these again. The fork is deleted.` : `${esc(T.error || 'The review session was not told.')} The fork is deleted.`;
    body.innerHTML = `<p>✓ <b>Posted</b> as a ${esc(EVENT_LABEL[P.event] || P.event)} review with ${P.inline} inline comment${P.inline === 1 ? '' : 's'}.
      ${P.url ? `<a href="${esc(P.url)}" target="_blank" rel="noopener">Open on GitHub ↗</a>` : ''}</p><p>${told}</p>`;
    foot.innerHTML = '<span class="grow"></span><button class="tb" data-act="draft">Draft again</button><button class="tb" data-act="close">Close</button>';
  } else {
    // Ready, or a post that failed: the editable drafts. Edits live in draftEdit until posted.
    if (!draftEdit || draftEdit.at !== X.drafted_at) {
      draftEdit = { at: X.drafted_at, ...structuredClone(X.review) };
      draftEdit.comments.forEach(c => { c.level ??= defaultLevel(c.finding); });  // drafts saved before levels existed
    }
    const R = draftEdit;
    const spot = c => c.path ? `${c.path}:${c.line}` : 'the review body';
    body.innerHTML = `<label class="f">Review type <select id="d-event">${Object.entries(EVENT_LABEL).map(([k, v]) =>
        `<option value="${k}"${R.event === k ? ' selected' : ''}>${v}</option>`).join('')}</select></label>
      <label class="f">Review body <textarea id="d-body" rows="3">${esc(R.body)}</textarea></label>
      ${R.comments.map((c, i) => `<div class="dcard${c.include === false ? ' off' : ''}">
        <div class="where"><input type="checkbox" data-include="${i}"${c.include === false ? '' : ' checked'} aria-label="Post the comment on ${esc(spot(c))}" title="Post this comment">
          <select data-level="${i}" aria-label="Level of the comment on ${esc(spot(c))}" title="Posted in bold in front of the comment">${Object.entries(LEVEL_LABEL).map(([k, v]) =>
            `<option value="${k}"${c.level === k ? ' selected' : ''}>${v}</option>`).join('')}</select>
          <b>${esc(c.finding)}</b>${c.path ? `<code>${esc(c.path)}:${c.start_line ? `${c.start_line}-` : ''}${c.line}</code>` : ''}<span class="t">${esc(c.title)}</span>${c.own ? '<span class="tag">your words</span>' : ''}</div>
        ${c.in_diff ? '' : `<span class="outside">${c.path ? 'Outside the diff: GitHub cannot take it inline, so it goes in the review body.' : 'No code anchor, so it goes in the review body.'}</span>`}
        <textarea data-body="${i}" aria-label="Comment on ${esc(spot(c))}" rows="${Math.min(12, Math.max(3, c.body.split('\n').length + 1))}">${esc(c.body)}</textarea></div>`).join('')}`;
    foot.innerHTML = `${errHtml}<span class="done-note" id="d-done" hidden></span><span class="grow">Posted with your gh account exactly as written here, each comment led by its level in bold.</span>
      <button class="tb" data-act="discard">Discard</button><button class="tb" data-act="draft">Re-draft</button>
      <button class="tb primary" data-act="post" id="d-post"></button>`;
    updatePostButton();
  }
  syncLock();
  // A rebuilt footer can take the focused button with it; keep focus inside the open dialog.
  if (!document.getElementById('drafts').contains(document.activeElement)) document.getElementById('drafts-close').focus({ preventScroll: true });
}

function updatePostButton() {
  const b = document.getElementById('d-post');
  if (!b || !draftEdit) return;  // a footer left from drafts that were discarded or are being redrafted
  const n = postCount(), done = doneState();
  // On a done PR the line above the button says so, and the button says it posts anyway: never blocked, never silent.
  const note = document.getElementById('d-done');
  if (note) { note.hidden = !done; note.textContent = done?.line || ''; }
  b.textContent = done ? done.button(n) : `Approve & post ${n} comment${n === 1 ? '' : 's'}`;
  b.disabled = !n && !draftEdit.body.trim();
}

async function loadHandoff() {
  if (!SERVED) return;
  const own = ownCode();
  try {
    const r = await fetch('/api/handoff/' + encodeURIComponent(D.page), { cache: 'no-store' });
    if (r.ok) HANDOFF = await r.json();
  } catch (e) { /* server off: keep the last answer */ }
  updateToolbar();
  if (ownCode() !== own) renderKeys();
  renderDrafts();
  const saved = document.getElementById('saved');
  if (saved && /^Saved · /.test(saved.textContent)) setSaved(savedText());
  clearTimeout(draftPoll);
  if (draftBusy() || sendBusy()) draftPoll = setTimeout(loadHandoff, 1500);
}

/* ---------- Running: what runs on this review and the Claude sessions behind it (jobs.py) ---------- */

let ACT = { running: [], ended: [], sessions: [] };
let actSig = '', actRunSig = null, actTimer = null, actTick = null, jobsShown = '', jobsError = '';
const jobsBtn = () => document.getElementById('jobs');
const jobsPanel = () => document.getElementById('jobs-panel');
// Elapsed time: m:ss, or h:mm:ss past an hour.
function clock(secs) {
  secs = Math.max(0, Math.floor(secs));
  const h = Math.floor(secs / 3600), m = Math.floor(secs / 60) % 60, s = String(secs % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
}
const endedAgo = t => { const s = Date.now() / 1000 - t; return s < 60 ? 'just now' : `${Math.round(s / 60)} min ago`; };
const OUTCOME = { done: ['✓', 'ok', 'done'], failed: ['✗', 'bad', 'failed'], stopped: ['■', 'off', 'stopped'] };

function renderJobsButton() {
  const btn = jobsBtn(), n = ACT.running.length;
  btn.hidden = !SERVED;
  btn.classList.toggle('busy', n > 0);
  const html = n ? `<span class="pulse" aria-hidden="true"></span>${n} running` : 'Sessions';
  if (btn.innerHTML !== html) btn.innerHTML = html;
}

function jobsHtml() {
  const now = Date.now() / 1000;
  const running = ACT.running.map(j => {
    const meta = [j.step && esc(j.step), j.pid && `pid ${j.pid}${j.alive ? '' : ' (gone)'}`,
      j.session && `session <code>${esc(j.session.slice(0, 8))}</code>`].filter(Boolean).join(' · ');
    const at = new Date(j.started * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    return `<li class="job"><div class="jrow"><span class="pulse" aria-hidden="true"></span><b>${esc(j.label)}</b>
      <span class="jt" data-started="${esc(j.started)}" aria-hidden="true">${clock(now - j.started)}</span><span class="sr-only">started at ${at}</span>
      ${j.stop ? `<button class="tb small" data-stop="${esc(j.id)}">Stop<span class="sr-only"> ${esc(j.label)}</span></button>` : ''}</div>
      ${meta ? `<div class="jmeta">${meta}</div>` : ''}</li>`;
  }).join('');
  const ended = ACT.ended.map(j => {
    const [mark, cls, word] = OUTCOME[j.state] || OUTCOME.done;
    return `<li class="job"><div class="jrow"><span class="${cls}" aria-hidden="true">${mark}</span><b>${esc(j.label)}</b>
      <span class="${cls}">${word}</span><span class="jt" data-ended="${esc(j.ended)}">${endedAgo(j.ended)}</span></div>
      ${j.error ? `<div class="jmeta bad">${esc(j.error)}</div>` : ''}</li>`;
  }).join('');
  const sessions = ACT.sessions.map(S => `<li class="job"><div class="jrow"><b>${esc(S.label)}</b>${S.note ? `<span class="jmeta">${esc(S.note)}</span>` : ''}</div>
      <div class="jmeta">session <code>${esc(S.id.slice(0, 8))}</code>${S.cwd && !S.resume ? ` in <code>${esc(S.cwd)}</code>` : ''}</div>
      ${S.resume ? `<div class="resume"><code>${esc(S.resume)}</code><button class="tb small" data-copy="${esc(S.id)}" aria-label="Copy the command that resumes the ${esc(S.label.toLowerCase())}">Copy</button></div>` : ''}</li>`).join('');
  return `<h3>Running</h3>${running ? `<ul>${running}</ul>` : '<p class="none">Nothing runs on this review now.</p>'}
    ${ended ? `<h3>Ended in the last 10 minutes</h3><ul>${ended}</ul>` : ''}
    <h3>Claude sessions</h3>${sessions ? `<ul>${sessions}</ul>` : '<p class="none">No Claude session on record for this review.</p>'}
    ${jobsError ? `<p class="err" role="alert">${esc(jobsError)}</p>` : ''}`;
}

// Rebuilt only when the answer changes, so focus and a text selection survive the polls; the clocks tick in place.
function renderJobs(force = false) {
  const panel = jobsPanel();
  if (panel.hidden) return;
  const sig = actSig + '|' + jobsError;
  if (!force && sig === jobsShown) return;
  jobsShown = sig;
  // A Stop button that left (its job ended): the next one, else a Copy button, else the panel.
  rerender(panel, jobsHtml(), ['data-stop', 'data-copy'], () => ['[data-stop]', '[data-copy]', panel]);
}

function tickJobs() {
  const now = Date.now() / 1000;
  jobsPanel().querySelectorAll('.jt[data-started]').forEach(el => { el.textContent = clock(now - el.dataset.started); });
  jobsPanel().querySelectorAll('.jt[data-ended]').forEach(el => {
    const t = endedAgo(+el.dataset.ended);
    if (el.textContent !== t) el.textContent = t;
  });
}

// A disclosure, not a dialog: focus stays on the button, the panel follows it in tab order, Esc or a click elsewhere
// closes it.
function toggleJobs(open, refocus = false) {
  jobsPanel().hidden = !open;
  jobsBtn().setAttribute('aria-expanded', String(open));
  clearInterval(actTick);
  if (open) {
    renderJobs(true);
    actTick = setInterval(tickJobs, 1000);
    loadActivity();
  } else if (refocus) jobsBtn().focus();
}

// Polled every 1.5 s while something runs and every 10 s otherwise, so a job started in another tab shows up.
async function loadActivity() {
  if (!SERVED) return;
  clearTimeout(actTimer);
  try {
    const r = await fetch('/api/activity/' + encodeURIComponent(D.page), { cache: 'no-store' });
    if (r.ok) ACT = await r.json();
  } catch (e) { /* server off: keep the last answer */ }
  const sig = JSON.stringify(ACT);
  if (sig !== actSig) {
    actSig = sig;
    jobsError = '';  // a refused Stop's message lasts until something changes
    renderJobsButton();
    renderJobs();
    // A job that starts or ends elsewhere (another tab, a Stop) also changes the chat, the drafts and the banner here.
    const runSig = ACT.running.map(j => j.id).sort().join();
    if (actRunSig !== null && runSig !== actRunSig) {
      loadChat();
      loadHandoff();
      if (actRunSig.includes('check') && !runSig.includes('check') && !freshChecking) loadFreshness();
    }
    actRunSig = runSig;
    onRereview();
  }
  actTimer = setTimeout(() => { if (document.visibilityState === 'visible') loadActivity(); }, ACT.running.length ? 1500 : 10000);
}

/* ---------- Review the new commits: a fork of the review session re-checks the findings at the new head (rereview.py) ---------- */

// ACT.rereview says whether the page can review new commits (and why not) and how the last re-review went. While one
// runs the page is locked: the server refuses every write (423), and each control that writes says why and does
// nothing, keeping focus (aria-disabled, not disabled). Reading, filters, Check again and Sessions stay.
const RR = () => ACT.rereview || {};
const locked = () => RR().status === 'reviewing';
const LOCK_WHY = 'Locked while Claude reviews the new commits. Stop it in the banner above to unlock.';
// Set before the page reloads itself for a re-review that ended, so the new page says once what changed.
const RR_KEY = 'finding-viewer:rereviewed:' + D.page;
let rrWas = null, rrNote = null, rrShown = '', rrTick = null, rrConfirm = false, rrStarting = false, lockShown = false;
let saveRefused = false;  // the last save met the lock: sent again if the page unlocks unchanged

// "1 fixed, 1 still applies, 2 new" (rereview.said).
function rrCounts(c = {}) {
  const say = { fixed: ['fixed', 'fixed'], still_applies: ['still applies', 'still apply'], changed: ['changed', 'changed'], new: ['new', 'new'] };
  return Object.keys(say).filter(k => c[k]).map(k => `${c[k]} ${say[k][c[k] === 1 ? 0 : 1]}`).join(', ') || 'no findings';
}

function lockedSay() {
  announce(LOCK_WHY);
  const el = document.getElementById('rereview');
  el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash');
}

// Controls outside the rebuilt parts (the chat's send and quick buttons, "use as note", the drafts dialog's actions, the
// note) follow the lock here; the triage buttons, the line numbers and the Draft button carry it when they are built.
function syncLock() {
  const on = locked();
  const mark = el => {
    if (on) { el.setAttribute('aria-disabled', 'true'); el.dataset.locked = '1'; el.title = LOCK_WHY; }
    else if (el.dataset.locked) { el.removeAttribute('aria-disabled'); delete el.dataset.locked; el.title = ''; }
  };
  document.querySelectorAll('#chatsend, #quick button, .usenote, #drafts [data-act="draft"], #drafts [data-act="post"], #drafts [data-act="discard"]').forEach(mark);
  const note = document.getElementById('note');
  if (note) { note.readOnly = on; note.title = on ? LOCK_WHY : ''; }
}

const lockAttr = () => locked() ? ` aria-disabled="true" title="${esc(LOCK_WHY)}"` : '';

// The re-review's own banner: running (with its clock and Stop), or what the last one in this tab came to.
function renderRereview() {
  const el = document.getElementById('rereview'), R = RR();
  let html = '', cls = '';
  if (locked()) {
    const what = R.rewritten ? 'the new head' : `the ${phrase(R.commits || 0, 'new commit', 'new commits')}`;
    html = `<span class="pulse" aria-hidden="true"></span><b>Reviewing ${esc(what)}…</b>
      <span class="jt" data-started="${esc(R.started)}" aria-hidden="true">${clock(Date.now() / 1000 - R.started)}</span>
      <span class="why">Triage, notes, drafts and the chat wait until it ends.</span>
      <button data-rr="stop"${R.stopping ? ' aria-disabled="true"' : ''}>${R.stopping ? 'Stopping…' : 'Stop'}<span class="sr-only"> the review of the new commits</span></button>`;
  } else if (rrNote) {
    cls = rrNote.kind;
    html = `<span>${esc(rrNote.text)}</span><button data-rr="dismiss" aria-label="Dismiss this message">✕</button>`;
  }
  el.hidden = !html;
  el.className = `banner ${cls}`.trim();
  if (html === rrShown) return;
  rrShown = html;
  // Stop leaves with the run: focus goes to the message's Dismiss, else the list.
  rerender(el, html, ['data-rr'], () => ['[data-rr="dismiss"]', document.getElementById('list')]);
}

function tickRereview() {
  const t = document.querySelector('#rereview .jt[data-started]');
  if (t) t.textContent = clock(Date.now() / 1000 - t.dataset.started);
}

// Called with every new activity answer: lock or unlock, and notice a re-review ending (here or in another tab).
function onRereview() {
  const R = RR(), was = rrWas;
  rrWas = R.status || null;
  const last = (D.rereviewed || []).at(-1);
  if (R.status === 'done' && R.result?.to && R.result.to !== last?.to) {
    // The server's page is newer than this one: reload it, once. What the reload would lose goes along in session
    // storage: the server's word on what changed, an unsent chat message, the finding (also in the hash), where focus
    // was, and whether a change made meanwhile was refused (the rebuilt page cannot take it).
    let seen = null;
    try { seen = sessionStorage.getItem(RR_KEY + ':reload'); } catch (e) { /* no session storage: reload anyway */ }
    if (seen !== String(R.ended)) {
      const chatin = document.getElementById('chatin'), a = document.activeElement;
      const keep = { message: R.result.message, chat: chatin.value, finding: D.findings[current]?.id, dropped: saveRefused,
        focus: a === chatin ? 'chat' : a?.closest?.('#rereview') ? 'note' : '' };
      try { sessionStorage.setItem(RR_KEY + ':reload', String(R.ended)); sessionStorage.setItem(RR_KEY, JSON.stringify(keep)); } catch (e) { /* all of it is lost */ }
      location.reload();
      return;
    }
  }
  if (R.status === 'reviewing' && was !== 'reviewing') {
    rrNote = null;
    announce(`Reviewing ${R.rewritten ? 'the new head' : phrase(R.commits || 0, 'new commit', 'new commits')}. The review is locked until it ends.`);
  } else if (was === 'reviewing' && R.status !== 'reviewing' && R.status !== 'done') {
    // A stop is said where it was made (this banner or the Running panel); a failure is said here.
    rrNote = R.stopped ? { kind: '', text: 'Stopped: the review of the new commits. The page is unchanged.' }
      : { kind: 'bad', text: `The review of the new commits failed: ${R.error || 'no reason given.'}` };
    if (!R.stopped) announce(rrNote.text);
  }
  applyLock();
}

function applyLock() {
  const on = locked();
  if (on !== lockShown) rrConfirm = false;
  renderRereview();
  syncLock();
  // The banner's Review button follows the answer too (whether the page can fork, whether one runs).
  renderFreshness();
  syncCheckButton();
  if (on === lockShown) return;
  lockShown = on;
  clearInterval(rrTick);
  if (on) rrTick = setInterval(tickRereview, 1000);
  // Unlocked and unchanged (it failed or was stopped): the save the lock refused goes now.
  else if (saveRefused) pushRemote();
  // Rebuild what carries the lock in its markup; rerender keeps focus on the same control.
  updateToolbar();
  const f = D.findings[current];
  if (f && document.getElementById('triage')) {
    refreshTriage();
    rerender(document.getElementById('cards'), cardsHtml(f), CARD_KEYS, cardFallback);
  }
}

// Unposted drafts point at the old lines: starting asks first, in the banner, and they go only if the review succeeds.
const unpostedDrafts = () => { const X = DS(); return !!X?.review && ['ready', 'failed'].includes(X.status); };

async function startRereview(discard) {
  if (rrStarting || locked()) return;
  if (!discard && unpostedDrafts()) { rrConfirm = true; renderFreshness(); document.getElementById('rr-go')?.focus(); return; }
  rrStarting = true;
  // The fork gets the saved triage, so save any pending change first (as startDraft does).
  if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; await pushRemote(); }
  let error = '', ask = false;
  try {
    const r = await fetch('/api/rereview/' + encodeURIComponent(D.page), {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Finding-Viewer': '1' }, body: JSON.stringify({ discard_drafts: !!discard }),
    });
    const body = await r.json().catch(() => ({}));
    ask = r.status === 409 && body.drafts;  // drafts appeared meanwhile (another tab): ask here too
    if (!r.ok && !ask) error = body.error || 'The viewer server refused and gave no reason. Run /findings to restart it, then try again.';
  } catch (e) {
    error = 'The viewer server is off. Run /findings to restart it.';
  }
  rrStarting = false;
  rrConfirm = ask;
  rrNote = error ? { kind: 'bad', text: `Not started: ${error}` } : null;
  if (error) announce(rrNote.text);
  await loadHandoff();
  await loadActivity();
  renderFreshness();
  renderRereview();
  if (ask) document.getElementById('rr-go')?.focus();
}

function cancelRereview() {
  rrConfirm = false;
  renderFreshness();
  document.getElementById('rr-start')?.focus();
}

async function stopRereview(btn) {
  if (btn.getAttribute('aria-disabled')) return;
  btn.setAttribute('aria-disabled', 'true');
  btn.firstChild.nodeValue = 'Stopping…';
  try {
    const r = await fetch(`/api/stop/${encodeURIComponent(D.page)}/rereview`, { method: 'POST', headers: { 'X-Finding-Viewer': '1' } });
    const body = await r.json().catch(() => ({}));
    if (r.ok) announce(body.stopped === false ? 'The review of the new commits had already ended.' : 'Stopped: the review of the new commits. The page is unchanged.');
    else rrNote = { kind: 'bad', text: `Not stopped: ${body.error || 'the viewer server refused and gave no reason.'}` };
  } catch (e) {
    rrNote = { kind: 'bad', text: 'Not stopped: the viewer server is off. Run /findings to restart it.' };
  }
  await loadActivity();
  renderRereview();
}

// After the reload a re-review asked for: say once what it found, and put back what the reload took (onRereview).
function rereviewedNote() {
  let keep = null;
  try { keep = JSON.parse(sessionStorage.getItem(RR_KEY) || 'null'); sessionStorage.removeItem(RR_KEY); } catch (e) { /* nothing to say */ }
  const last = (D.rereviewed || []).at(-1);
  if (!keep || !last) return;
  const at = D.findings.findIndex(f => f.id === keep.finding);
  if (!location.hash && at >= 0) select(at);
  const chatin = document.getElementById('chatin');
  if (keep.chat && !chatin.value) {
    chatin.value = keep.chat;
    chatin.style.height = 'auto';
    chatin.style.height = chatin.scrollHeight + 2 + 'px';
  }
  rrNote = { kind: 'ok', text: (keep.message || `Reviewed ${short(last.to)}: ${rrCounts(last.counts)}.`)
    + (last.discarded_drafts ? ' The unposted drafts were discarded.' : '')
    + (keep.dropped ? ' A change you made while it ran was not saved, since the findings moved: make it again here.' : '') };
  renderRereview();
  announce(rrNote.text);
  if (keep.focus === 'chat') chatin.focus();
  else if (keep.focus === 'note') document.querySelector('#rereview [data-rr="dismiss"]')?.focus();
}

async function stopJob(btn) {
  const J = ACT.running.find(j => j.id === btn.dataset.stop);
  if (!J || btn.getAttribute('aria-disabled')) return;
  // aria-disabled, not disabled: the button keeps focus while the job stops (up to 3 s).
  btn.setAttribute('aria-disabled', 'true');
  btn.firstChild.nodeValue = 'Stopping…';
  let error = '';
  try {
    const r = await fetch(`/api/stop/${encodeURIComponent(D.page)}/${J.kind === 'chat' ? 'chat/' + encodeURIComponent(J.fid) : J.kind}`,
      { method: 'POST', headers: { 'X-Finding-Viewer': '1' } });
    const body = await r.json().catch(() => ({}));
    error = r.ok ? '' : body.error || 'Not stopped: the viewer server refused and gave no reason. Run /findings to restart it, then try again.';
    // Said here only: the chat and the toolbar stay quiet about a stop (see loadChat, updateToolbar).
    if (r.ok) announce(body.stopped === false ? `${J.label} had already ended.` : `Stopped: ${J.label}.`);
  } catch (e) {
    error = 'Not stopped: the viewer server is off. Run /findings to restart it.';
  }
  await loadActivity();
  jobsError = error;  // after the answer: a changed answer clears the line, this one must stay
  renderJobs(true);  // the error line, or the button back when the stop was refused
}

async function copyResume(btn) {
  const text = ACT.sessions.find(S => S.id === btn.dataset.copy)?.resume;
  if (!text) return;
  try { await navigator.clipboard.writeText(text); }
  catch (e) {
    const ta = Object.assign(document.createElement('textarea'), { value: text });
    document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove();
    btn.focus();
  }
  btn.textContent = 'Copied';
  announce('Copied');
  setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
}

// Fixed findings show under "All" only: the other filters count what is still open.
const visible = f => !hiddenSev.has(f.severity) &&
  (statusFilter === 'all' || (!isFixed(f) && (statusFilter === 'open' ? !statusOf(f) : statusOf(f) === statusFilter)));
// The list's groups in order: each severity, then the findings the new commits fixed.
const groupsOf = () => [...ORDER.map(s => [`sev-${s}`, LABEL[s], f => f.severity === s && !isFixed(f)]),
  ['fixed-h', 'Fixed in the new commits', isFixed]];

function renderList() {
  const nav = document.getElementById('list');
  const top = nav.scrollTop, done = doneState();
  const skipSays = localMode() ? "Won't fix" : 'Stays here';
  let html = '';
  for (const [cls, title, has] of groupsOf()) {
    const items = D.findings.map((f, i) => [f, i]).filter(([f]) => has(f) && visible(f));
    if (!items.length) continue;
    html += `<h2 class="${cls}"><span class="dot"></span>${title} · ${items.length}</h2>`;
    for (const [f, i] of items) {
      const s = f.severity;
      const loc = f.locations.find(l => l.path) || f.locations[0];
      const where = (loc ? `${loc.path ? base(loc.path) : loc.raw}:${loc.start}${loc.end > loc.start ? '–' + loc.end : ''}` : 'no code anchor')
        + ({ new: ' · new', changed: ' · changed' }[f.rereview?.status] || '');
      const st = statusOf(f);
      const postSays = isFixed(f) ? 'Marked Post, but fixed, so not posted' : done ? done.word : 'Will be posted';
      const mark = st === 'post' ? `<span class="post" role="img" aria-label="${esc(postSays)}" title="${esc(postSays)}">✓</span>`
        : st === 'fix' ? '<span class="fix" role="img" aria-label="To fix in your code" title="To fix in your code">⚒︎</span>'
        : st === 'skip' ? `<span class="skip" role="img" aria-label="${esc(skipSays)}" title="${esc(skipSays)}">✗</span>` : '';
      const note = !noteOf(f) ? '' : modeOf(f) === 'comment' && st !== 'fix'
        ? '<span class="note own" role="img" aria-label="Has your own comment" title="Has your own comment">✎</span>'
        : '<span class="note" role="img" aria-label="Has your note" title="Has your note">✎</span>';
      const th = CHAT.threads?.[f.id];
      const talk = th?.pending ? '<span class="chat" role="img" aria-label="Claude is answering" title="Claude is answering">⋯</span>'
        : th?.messages?.length ? '<span class="chat" role="img" aria-label="Has a chat with Claude" title="Has a chat with Claude">💬</span>' : '';
      const mv = FRESH?.affected?.[f.id];
      const moved = mv ? `<span class="moved" role="img" aria-label="${esc(movedText(mv.kind))}" title="${esc(movedText(mv.kind))}">↻</span>` : '';
      html += `<button class="item sev-${s}${i === current ? ' sel' : ''}${loc ? '' : ' noanchor'}${st ? ' is-' + st : ''}${isFixed(f) ? ' is-fixed' : ''}" data-i="${i}"${i === current ? ' aria-current="true"' : ''}>
        <span class="fid">${esc(f.id)}</span><span class="t">${esc(f.title)}</span><span class="st">${mark}${note}${talk}${moved}</span><span class="l">${esc(where)}</span></button>`;
    }
  }
  // An item that left the list (decided under "To decide", say): focus the next one, else the previous, else the list.
  const listFallback = old => {
    const items = [...nav.querySelectorAll('.item')], k = items.indexOf(old);
    const sel = it => `.item[data-i="${it.dataset.i}"]`;
    return k < 0 ? [nav] : [...items.slice(k + 1).map(sel), ...items.slice(0, k).reverse().map(sel), nav];
  };
  rerender(nav, html || '<div class="empty">Nothing matches these filters.</div>', ['data-i'], listFallback);
  nav.scrollTop = top;
}

// The key hint inside a triage button; none while single-key shortcuts are off.
const hint = k => shortcutsOn() ? ` <kbd aria-hidden="true">${k}</kbd>` : '';
const notePlaceholder = () => (fixing() || localMode() ? 'How to fix it, or what to leave alone. Claude follows it when it changes the code.  '
  : ownComment()
  ? 'The comment exactly as it should appear on the PR (markdown). Claude does not reword it.  '
  : 'Your thoughts on this finding: agree, disagree, what to change or add in the comment. Claude reads this when it writes the comment.  ')
  + (shortcutsOn() ? '(e to type, Esc to leave)' : '(Esc to leave)');
const noteLabel = () => fixing() ? 'Your note on the fix' : ownComment() ? 'Your comment for the PR, posted as written' : 'Your note on this finding';
const useLabel = () => ownComment() ? 'Use as my comment' : 'Use as my note';

function noteModeHtml(f) {
  if (statusOf(f) === 'fix' || localMode()) return '<span>Claude follows it when it fixes the code. Nothing is posted.</span>';
  const m = modeOf(f);
  const b = (k, label) => `<button type="button" data-mode="${k}"${m === k ? ' class="on"' : ''} aria-pressed="${m === k}"${lockAttr()}>${label}</button>`;
  return `<span class="seg" role="group" aria-label="What the text below is">${b('note', 'Note for Claude')}${b('comment', 'My comment, as written')}</span>
    <span>${m === 'comment' ? 'Goes on the PR word for word, after its level.' : 'Claude follows it when it writes the comment.'}</span>`;
}

function setNoteMode(mode) {
  const f = D.findings[current];
  if (locked()) return lockedSay();
  if (!f || modeOf(f) === mode) return;
  tri(f.id).mode = mode;
  persist();
  rerender(document.getElementById('note-mode'), noteModeHtml(f), ['data-mode']);
  const note = document.getElementById('note');
  note.placeholder = notePlaceholder();
  note.setAttribute('aria-label', noteLabel());
  document.querySelectorAll('.usenote').forEach(b => { b.textContent = useLabel(); });
  if (noteOf(f)) renderList();
}

function triControls(f) {
  const s = statusOf(f), done = doneState(), local = localMode();
  // A fixed finding keeps its decision on show, but nothing is posted for it.
  const state = isFixed(f) ? `Fixed in ${short(f.rereview.at)}: left out of the drafts`
    : local ? ({ fix: 'To fix in your code', skip: "Won't fix" }[s] || 'Not decided yet')
    : s === 'post' ? (done ? done.word : 'Will be posted') : s === 'fix' ? 'To fix in your code, not posted' : s === 'skip' ? 'Stays here' : 'Not decided yet';
  const fix = canFix(f) ? `<button class="tb fix${s === 'fix' ? ' on' : ''}" data-tri="fix" aria-pressed="${s === 'fix'}"${lockAttr()}>⚒︎ Fix${hint('f')}</button>` : '';
  // A local review has nothing to post: Fix or Won't fix only.
  const post = local ? '' : `<button class="tb post${s === 'post' ? ' on' : ''}" data-tri="post" aria-pressed="${s === 'post'}"${lockAttr()}>✓ Post${hint('a')}</button>`;
  return `<div class="tri-row">
      <span class="tri-pair">${post}
      ${fix}
      <button class="tb skip${s === 'skip' ? ' on' : ''}" data-tri="skip" aria-pressed="${s === 'skip'}"${lockAttr()}>✗ ${local ? "Won't fix" : "Don't post"}${hint('r')}</button></span>
      <span class="tri-state">${esc(state)}</span>
      <span class="spacer"></span>
      <button class="tb next${s ? ' primary' : ''}" data-tri="next">Next undecided →${hint('n')}</button>
    </div>`;
}

// What a re-review left for you on this finding: it changed after you chose Post, or your comment line was reset.
function rrMarks(f) {
  const t = T.findings[f.id] || {}, out = [];
  if (t.recheck && statusOf(f) === 'post' && !isFixed(f)) {
    out.push(`<div class="rr-note"><span>Changed since you decided Post (re-reviewed at ${esc(short(t.recheck))}): read it again.</span>
      <button class="tb" data-tri="confirm"${lockAttr()}>Still post it</button></div>`);
  }
  if (t.anchor_reset) {
    out.push(`<div class="rr-note"><span>Your chosen line changed; anchor reset (it was <code>${esc(t.anchor_reset.path)}:${esc(t.anchor_reset.line)}</code>).</span></div>`);
  }
  return out.join('');
}

function triAnchor(f) {
  const a = anchorOf(f);
  if (statusOf(f) === 'fix') {
    if (!a) return '<span>No code anchor.</span>';
    return `<span>⚒︎ Fix at <code>${esc(a.path)}:${a.line}</code></span>`;
  }
  if (localMode()) {
    if (!a) return '<span>No code anchor.</span>';
    return `<span>At <code>${esc(a.path)}:${a.line}</code></span>
    <span>${shortcutsOn() ? 'Click a line number, or press <kbd>[</kbd> / <kbd>]</kbd>, to move it.' : 'Click a line number to move it.'}</span>`;
  }
  if (!a) return '<span>No code anchor, so this would go in the review body.</span>';
  const ok = commentable(a);
  return `<span>💬 Comment goes on <code>${esc(a.path)}:${a.line}</code></span>
    ${ok ? '<span class="badge in">inside the diff, can be an inline comment</span>'
         : '<span class="badge warn">outside the diff: GitHub won’t take it inline, so it would go in the review body</span>'}
    <span>${shortcutsOn() ? 'Click a line number, or press <kbd>[</kbd> / <kbd>]</kbd>, to move it.' : 'Click a line number to move it.'}</span>`;
}

function codeCard(f, fi, loc, li) {
  const key = `${fi}:${li}`;
  const rng = `:${loc.start}${loc.end > loc.start ? '–' + loc.end : ''}`;
  if (!loc.path) {
    return `<section class="card sev-${f.severity}" id="loc-${li}"><div class="card-h"><code class="path">${esc(loc.raw)}</code></div>
      <div class="missing">File not found in ${esc(D.ref_label)}. The anchor may point at a file this review didn't check out.</div></section>`;
  }
  const file = D.files[loc.path];
  const n = file.lines.length;
  const w = windows[key] ??= { a: Math.max(1, loc.start - CTX), b: Math.min(n, loc.end + CTX) };
  const added = new Set(file.added);
  const hl = highlightedLines(loc.path);
  const marks = lineMap[loc.path] || {};
  const anchor = fi === current ? anchorOf(f) : null;
  const pickLock = locked() ? ` aria-disabled="true" title="${esc(LOCK_WHY)}"` : ' title="Put the comment on this line"';
  const rows = [];
  const delRow = k => `<tr class="del"><td class="g"></td><td class="n"></td><td class="m">−</td><td class="c">${file.removed[k]} line${file.removed[k] > 1 ? 's' : ''} removed here</td></tr>`;
  for (let i = w.a; i <= w.b; i++) {
    if (file.removed[i - 1]) rows.push(delRow(i - 1));
    const isAnchor = !!anchor && anchor.path === loc.path && anchor.line === i;
    const cls = [added.has(i) ? 'add' : '', i >= loc.start && i <= loc.end ? 'flag' : '', isAnchor ? 'anchor' : ''].join(' ');
    const badges = (marks[i] || []).map(x =>
      `<a class="fb fsev-${x.sev}${x.fi === fi ? ' cur' : ''}" href="#${encodeURIComponent(x.id)}" title="${esc(D.findings[x.fi].title)}">${esc(x.id)}</a>`).join('');
    rows.push(`<tr class="${cls}"><td class="g">${badges}</td><td class="n"><button type="button" class="ln" data-path="${esc(loc.path)}" data-line="${i}" data-key="${key}" aria-label="Put the comment on line ${i}" aria-pressed="${isAnchor ? 'true' : 'false'}"${pickLock}>${i}</button></td><td class="m">${added.has(i) ? '+' : ''}</td><td class="c">${hl[i - 1] || ' '}</td></tr>`);
  }
  if (file.removed[w.b] && w.b === n) rows.push(delRow(w.b));

  const flagged = [];
  for (let i = loc.start; i <= Math.min(loc.end, n); i++) flagged.push(i);
  let badge;
  if (loc.start > n) badge = `<span class="badge warn">line ${loc.start} is past the end of the file (${n} lines)</span>`;
  else if (!file.changed) badge = '<span class="badge">file not changed in this branch</span>';
  else if (flagged.some(i => added.has(i))) badge = '<span class="badge in">changed in this branch</span>';
  else badge = '<span class="badge warn">outside the diff</span>';
  const mvLoc = FRESH?.affected?.[f.id]?.locations?.find(x => x.path === loc.path);
  if (mvLoc) badge += `<span class="badge warn">${mvLoc.kind === 'lines' ? 'these lines changed in a newer commit' : mvLoc.kind === 'deleted' ? 'file deleted in a newer commit' : 'file changed in a newer commit'}</span>`;
  const amb = loc.ambiguous.length ? `<span class="badge warn" title="${esc(loc.ambiguous.join('\n'))}">${loc.ambiguous.length} files match “${esc(base(loc.raw.split(':')[0]))}”</span>` : '';
  const abs = `${D.repo_root}/${loc.path}`;
  const gh = D.github && D.blob_ref ? `<a href="${esc(D.github)}/blob/${esc(D.blob_ref)}/${esc(loc.path)}#L${loc.start}${loc.end > loc.start ? '-L' + loc.end : ''}" target="_blank">GitHub</a>` : '';
  return `<section class="card sev-${f.severity}" id="loc-${li}" tabindex="-1">
    <div class="card-h"><code class="path">${esc(loc.path)}</code><span class="rng">${esc(rng)}</span>${badge}${amb}
      <span class="spacer"></span><a href="vscode://file/${encodeURI(abs)}:${loc.start}">Open in VS Code</a>${gh}</div>
    ${w.a > 1 ? `<button class="more" data-act="up" data-key="${key}">↑ ${Math.min(STEP, w.a - 1)} more lines</button>` : ''}
    <div class="scroll"><table class="code">${rows.join('')}</table></div>
    <div class="foot">${w.b < n ? `<button class="more" data-act="down" data-key="${key}">↓ ${Math.min(STEP, n - w.b)} more lines</button>` : ''}
      ${w.a > 1 || w.b < n ? `<button class="more" data-act="all" data-key="${key}">Whole file (${n} lines)</button>` : ''}</div>
  </section>`;
}

const cardsHtml = f => f.locations.map((loc, li) => codeCard(f, current, loc, li)).join('') ||
  '<div class="missing">This finding has no file:line anchor.</div>';

function renderDetail() {
  const main = document.getElementById('detail');
  const f = D.findings[current];
  if (!f) { main.innerHTML = '<div class="empty">No findings in this review.</div>'; return; }
  const tags = [];
  if (f.recurring) tags.push('<span class="tag">Recurring</span>');
  // What the last re-review found (rereview.py); "still applies" needs no tag.
  const R = f.rereview;
  if (R?.status === 'fixed') tags.push(`<span class="tag fixed">Fixed in ${esc(short(R.at))}</span>`);
  else if (R?.status === 'changed') tags.push(`<span class="tag changed" title="Still applies, but what it says or where it sits changed in ${esc(short(R.at))}">Changed</span>`);
  else if (R?.status === 'new') tags.push(`<span class="tag new" title="Found in the new commits, at ${esc(short(R.at))}">New</span>`);
  tags.push(`<span id="fresh-tag">${freshTag(f)}</span>`);
  for (const [k, v] of Object.entries(f.meta || {})) tags.push(`<span class="tag">${esc(k)}: ${esc(v)}</span>`);
  const s = statusOf(f);
  rerender(main, `
    <div class="dhead sev-${f.severity}"><span class="sev">${LABEL[f.severity]}</span><span class="fidbig">${esc(f.id)}</span>
      <h2>${esc(f.title)}</h2>${tags.join(' ')}</div>
    ${R?.status === 'fixed' && R.why ? `<p class="rr-why">Fixed in ${esc(short(R.at))}: ${esc(R.why)}</p>` : ''}
    <div class="triage ${s || ''}" id="triage">
      <div id="tri-controls">${triControls(f)}</div>
      <div id="rr-marks">${rrMarks(f)}</div>
      <div class="note-mode" id="note-mode">${noteModeHtml(f)}</div>
      <textarea id="note" rows="2" aria-label="${esc(noteLabel())}" placeholder="${esc(notePlaceholder())}"></textarea>
      <div class="tri-anchor" id="tri-anchor">${triAnchor(f)}</div>
    </div>
    <div class="body">${markdown(f.body)}</div>
    <div class="sect">Affected code${f.locations.length > 1 ? ` · ${f.locations.length} locations` : ''}</div>
    <div id="cards">${cardsHtml(f)}</div>
    <div class="keys" id="keys"></div>`, ['data-tri', 'data-mode'], () => [firstTri()]);
  renderKeys();
  const note = document.getElementById('note');
  note.value = T.findings[f.id]?.note || '';
  autosize(note);
  syncLock();
  if (window.hljs) main.querySelectorAll('.body pre code').forEach(c => { try { hljs.highlightElement(c); } catch (e) { /* leave unhighlighted */ } });
  main.querySelectorAll('.body :not(pre) > code').forEach(c => {
    const li = f.locations.findIndex(l => c.textContent.includes(l.raw) || (l.raw.includes(c.textContent.trim()) && /:\d/.test(c.textContent)));
    if (li >= 0) { c.classList.add('jump'); c.dataset.li = li; }
  });
  main.scrollTop = 0;
}

// Everything that names a single-key shortcut follows the setting: the legend under the finding, the hint under the
// anchor, the key hints in the triage buttons, the note's placeholder and the (h) in the chat buttons' titles.
function renderKeys() {
  const on = shortcutsOn();
  document.getElementById('chat-show').title = on ? 'Show the chat with Claude (h)' : 'Show the chat with Claude';
  document.getElementById('chat-hide').title = on ? 'Hide the chat (h)' : 'Hide the chat';
  const el = document.getElementById('keys');
  const local = localMode();
  if (el) el.innerHTML = shortcutsOn()
    ? `${local ? '' : '<kbd>a</kbd> post · '}${canFix(D.findings[current]) ? '<kbd>f</kbd> fix in your code · ' : ''}<kbd>r</kbd> ${local ? "won't fix" : "don't post"} · <kbd>u</kbd> clear · <kbd>n</kbd> next undecided · <kbd>e</kbd> write a note${local ? '' : ' or your comment'} ·
      <kbd>c</kbd> chat with Claude · <kbd>h</kbd> hide/show the chat · <kbd>j</kbd>/<kbd>k</kbd> next/previous · <kbd>o</kbd> open in VS Code · ${local ? 'click a line number to choose the line · <kbd>[</kbd>/<kbd>]</kbd> move the line' : 'click a line number to choose where the comment goes · <kbd>[</kbd>/<kbd>]</kbd> move the comment line'}`
    : 'Single-key shortcuts are off; turn them on under the chat’s ⚙ settings. <kbd>↑</kbd>/<kbd>↓</kbd> next/previous · click a line number to choose ' + (local ? 'the line' : 'where the comment goes');
  const f = D.findings[current], anchorHint = document.getElementById('tri-anchor'), note = document.getElementById('note');
  if (!f || !anchorHint) return;
  anchorHint.innerHTML = triAnchor(f);
  rerender(document.getElementById('tri-controls'), triControls(f), ['data-tri'], () => [firstTri()]);
  if (note) note.placeholder = notePlaceholder();
}

function refreshTriage() {
  const f = D.findings[current];
  document.getElementById('triage').className = `triage ${statusOf(f) || ''}`;
  rerender(document.getElementById('tri-controls'), triControls(f), ['data-tri'], () => [firstTri()]);
  // "Still post it" leaves with its mark: focus goes to Post.
  rerender(document.getElementById('rr-marks'), rrMarks(f), ['data-tri'], () => [document.querySelector('#tri-controls ' + firstTri())]);
  document.getElementById('tri-anchor').innerHTML = triAnchor(f);
  rerender(document.getElementById('note-mode'), noteModeHtml(f), ['data-mode'], () => [document.querySelector('#tri-controls [data-tri="fix"]')]);
  const note = document.getElementById('note');
  note.placeholder = notePlaceholder();
  note.setAttribute('aria-label', noteLabel());
  document.querySelectorAll('.usenote').forEach(b => { b.textContent = useLabel(); });
}

function autosize(el) {
  el.style.height = 'auto';
  el.style.height = Math.max(60, el.scrollHeight + 2) + 'px';
}

/* ---------- is the reviewed code still current? ---------- */

const short = sha => (sha || '').slice(0, 8);
const movedText = kind => kind === 'lines' ? 'Flagged lines changed since the review'
  : kind === 'deleted' ? 'File deleted since the review' : 'File changed since the review';
const freshTag = f => FRESH?.affected?.[f.id] ? `<span class="tag moved">${esc(movedText(FRESH.affected[f.id].kind))}</span>` : '';

function ago(iso) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (!isFinite(s)) return '';
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

// Your own standing review on the PR, from the freshness check. `plain`: without bold, as a detail under the done line.
function mineText(F, plain = false) {
  const M = F?.mine;
  if (!M) return '';
  const b = s => plain ? s : `<b>${s}</b>`;
  const at = M.at ? ` ${esc(ago(M.at))}` : '';
  const where = M.covers_head ? 'the current head'
    : M.new_since != null ? `${M.new_since} commit${M.new_since === 1 ? '' : 's'} landed after it` : 'the PR was force-pushed since';
  const detail = M.commit ? `at ${short(M.commit)}, ${where}` : where;
  return M.state === 'APPROVED'
    ? `${b(`✓ You approved PR #${esc(F.pr)}`)}${at} (${detail}).${F.state === 'OPEN' && M.covers_head ? ' Comments posted now go on an approved PR.' : ''}`
    : `${b(`You requested changes on PR #${esc(F.pr)}`)}${at} (${detail}).`;
}

// The PR needs nothing more from this page: merged or closed, or you approved what it holds now. Taken from the
// freshness check only (an older check has no `done`); null while there is none, so nothing changes until it arrives.
// Posting stays possible: a finding marked Post stays marked, and every place that offers posting says where it goes.
// `button(n)` is the drafts dialog's post button for n included comments.
function doneState(F = FRESH) {
  if (!F?.pr || !(F.done ?? ['MERGED', 'CLOSED'].includes(F.state))) return null;
  const pr = `PR #${F.pr}`;
  if (['MERGED', 'CLOSED'].includes(F.state)) {
    const is = `is ${F.state.toLowerCase()}`;
    return { line: `${pr} ${is}. Your triage is kept here; comments posted now go on a ${F.state.toLowerCase()} PR.`,
      word: `Marked to post (PR ${is})`, button: n => `Post ${phrase(n, 'comment', 'comments')} anyway (PR ${is})`,
      title: `Not the next step: ${pr} ${is}. You can still draft comments and post them anyway.` };
  }
  if (F.mine?.state !== 'APPROVED' || !F.mine.covers_head) return null;
  const at = F.mine.at ? ` ${ago(F.mine.at)}` : '';
  return { line: `You approved ${pr}${at} at the current head.`, word: 'Marked to post (you approved this PR)',
    button: n => `Post ${phrase(n, 'comment', 'comments')} anyway (you approved this PR)`,
    title: `Not the next step: you approved ${pr} at its current head. You can still draft comments and post them anyway.` };
}

function renderFreshness() {
  const el = document.getElementById('fresh');
  const F = FRESH;
  const meta = document.getElementById('fresh-meta');
  const checked = F?.checked_at ? new Date(F.checked_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
  if (meta) meta.innerHTML = !F ? '' : F.status === 'current' ? `Head: <b>up to date</b> (checked ${checked})`
    : F.status === 'unknown' ? (F.reason && !/working tree/.test(F.reason) ? esc(F.reason) : '') : `Head: <b>${short(F.head)}</b> (checked ${checked})`;
  // Three looks: current (green), moved (orange: new commits or a force-push), done (neutral: nothing more to post).
  // Done wins over moved: its state line comes first and the new commits become a detail under it.
  const done = doneState(F);
  const first = done ? `<span class="first">${esc(done.line)}</span>` : '';
  // Under the done line, your review is a plain detail; "you approved" is the done line itself when that is the reason.
  const mine = !done ? mineText(F) : ['MERGED', 'CLOSED'].includes(F.state) ? mineText(F, true) : 'Comments posted now go on an approved PR.';
  el.classList.toggle('done', !!done);
  if (!F || !['behind', 'rewritten'].includes(F.status)) {
    const settled = !done && F && ['MERGED', 'CLOSED'].includes(F.state) ? `PR #${esc(F.pr)} is ${F.state === 'MERGED' ? 'merged' : 'closed'}.` : '';
    const text = [mine, settled].filter(Boolean).join(' ');
    el.hidden = !text && !done;
    el.classList.toggle('calm', !done && F?.mine?.state !== 'CHANGES_REQUESTED');
    if (text || done) showFresh(el, `${first}${text ? `<span>${text}</span>` : ''}`);
    return;
  }
  el.classList.remove('calm');
  const where = F.pr ? `PR #${esc(F.pr)}` : 'This branch';
  const n = (F.new_commits || []).length;
  const flag = s => done ? s : `⚠ <b>${s}</b>`;
  const lead = F.status === 'rewritten'
    ? `${flag(`${where} was force-pushed`)}: the reviewed commit ${short(F.ref)} is no longer part of it (head is now ${short(F.head)}).`
    : `${flag(`${where} has ${n} new commit${n === 1 ? '' : 's'} since the reviewed code`)} (${short(F.ref)} → ${short(F.head)}).`;
  const groups = { lines: [], deleted: [], file: [] };
  for (const [fid, a] of Object.entries(F.affected || {})) groups[a.kind]?.push(fid);
  const impact = [
    groups.lines.length ? `Flagged lines changed: <b>${groups.lines.map(esc).join(', ')}</b>.` : '',
    groups.deleted.length ? `File deleted: <b>${groups.deleted.map(esc).join(', ')}</b>.` : '',
    groups.file.length ? `Same file changed: ${groups.file.map(esc).join(', ')}.` : '',
  ].filter(Boolean).join(' ') || "None of the findings' files changed.";
  const state = done ? (mine ? ` ${mine}` : '') : (F.state && F.state !== 'OPEN' ? ` PR is ${F.state.toLowerCase()}.` : '') + (mine ? ` ${mine}` : '');
  const list = (F.new_commits || []).map(c =>
    `<li>${c.url ? `<a href="${esc(c.url)}" target="_blank">${short(c.sha)}</a>` : short(c.sha)} ${esc(c.title)} <span class="who">· ${esc(c.author)}, ${esc(ago(c.date))}</span></li>`).join('');
  // Review them (rereview.py): offered while the page can fork its review session; else the reason is the lead's title.
  // Hidden while one runs (its own banner says so). With unposted drafts it asks here first.
  const R = RR(), label = F.status === 'rewritten' ? 'Review the new head' : `Review the ${phrase(n, 'new commit', 'new commits')}`;
  const review = !SERVED || !R.can || locked() ? ''
    : rrConfirm ? `<span class="confirm"><span>This discards the unposted drafts: they refer to the old lines. They go only once the review succeeds.</span>
        <button class="go" id="rr-go">Discard drafts and review</button><button id="rr-cancel">Cancel</button></span>`
    : `<button class="go" id="rr-start" title="A fork of the review session reviews the new commits and re-checks each finding at the new head; this page then shows the new head with your triage carried over. It costs about as much as drafting comments.">${esc(label)}</button>`;
  showFresh(el, `${first}<span${R.can === false && R.why ? ` title="${esc(R.why)}"` : ''}>${lead}${state}</span><span>${impact}</span>
    ${F.compare_url ? `<a href="${esc(F.compare_url)}" target="_blank">See what changed ↗</a>` : ''}
    <button id="fresh-again" aria-label="Check again"><span aria-hidden="true">Check again</span></button>${review}
    ${list ? `<details><summary>${n} new commit${n === 1 ? '' : 's'}</summary><ul>${list}</ul></details>` : ''}`);
  el.hidden = false;
}

// The banner is a status region: rebuild it only when its content changes, so an unchanged check is not read out
// again, and keep focus on "Check again" across the rebuild.
let freshShown = '', doneSaid = '';
// Check again runs one at a time (the server also shares a running check). Meanwhile the button reads "Checking…" and
// ignores clicks: aria-disabled rather than disabled, so focus stays on it across the banner rebuild. The label sits
// in an aria-hidden span under a fixed aria-label, so the banner (a status region) does not read it out as it flips;
// "Checking" is said once, by announce().
let freshChecking = false;
function syncCheckButton() {
  const label = document.querySelector('#fresh-again > span');
  if (!label) return;
  const text = freshChecking ? 'Checking…' : 'Check again';
  if (label.textContent !== text) label.textContent = text;
  if (freshChecking) label.parentElement.setAttribute('aria-disabled', 'true'); else label.parentElement.removeAttribute('aria-disabled');
}
function showFresh(el, html) {
  if (html === freshShown) return;
  freshShown = html;
  // A button that leaves (Review, once the re-review it starts locks the page): focus goes to that re-review's Stop,
  // else to the list.
  rerender(el, html, ['id'], () => [document.querySelector('#rereview [data-rr="stop"]'), document.getElementById('list')]);
}

async function loadFreshness(force = false) {
  if (!SERVED) return;
  if (force) {
    if (freshChecking) return;
    freshChecking = true;
    syncCheckButton();
    announce('Checking again…');
    setTimeout(loadActivity, 300);  // the Running panel's Check again row
  }
  clearTimeout(freshTimer);
  try {
    const r = await fetch(`/api/freshness/${encodeURIComponent(D.page)}${force ? '?force=1' : ''}`, { cache: 'no-store' });
    if (r.ok) FRESH = await r.json();
  } catch (e) { /* server off: keep the last result */ }
  if (force) {
    freshChecking = false;
    loadActivity();
  }
  const wasShown = !document.getElementById('fresh').hidden;
  renderFreshness();
  syncCheckButton();
  // The banner is a status region, but one that appears together with its text is not reliably read out. Say the
  // done line once, when a result first makes the PR done; a banner already on screen announces its own change.
  const done = doneState();
  if ((done?.word || '') !== doneSaid) {
    doneSaid = done?.word || '';
    if (done && !wasShown) announce(done.line);
  }
  updateToolbar();
  renderList();
  const f = D.findings[current];
  if (f && document.getElementById('triage')) refreshTriage();
  updatePostButton();
  const tag = document.getElementById('fresh-tag');
  if (f && tag) tag.innerHTML = freshTag(f);
  const cards = document.getElementById('cards');
  if (f && cards) rerender(cards, cardsHtml(f), CARD_KEYS, cardFallback);
  freshTimer = setTimeout(() => { if (document.visibilityState === 'visible') loadFreshness(); }, 3 * 60 * 1000);
}

/* ---------- chat with Claude ---------- */

const prettyModel = id => {
  const m = /claude-([a-z]+)-(\d+)-(\d+)/.exec(id || '');
  return m ? `${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2]}.${m[3]}` : (id || '');
};
const modelLabel = id => SETTINGS.models.find(o => o.id === id)?.label || id || 'Your Claude Code default';

function renderChatSub() {
  const f = D.findings[current];
  const th = CHAT.threads?.[f?.id];
  const s = SETTINGS.settings;
  const ctx = th?.mode ? (th.mode === 'fork' ? 'forked from the review session' : 'finding only')
    : (s.context === 'fork' && CHAT.review_session?.available ? 'will fork the review session' : 'finding only');
  document.getElementById('chat-sub').innerHTML =
    `${esc(modelLabel(s.model))} · ${esc(ctx)} · read-only. ${shortcutsOn() ? 'Press <kbd>c</kbd> to type.' : 'Click below to type.'}`;
}

function renderSettings() {
  const s = SETTINGS.settings;
  const known = SETTINGS.models.some(o => o.id === s.model);
  const sel = document.getElementById('set-model');
  sel.innerHTML = SETTINGS.models.map(o => `<option value="${esc(o.id)}">${esc(o.label)}</option>`).join('') +
    '<option value="__custom">Other model id…</option>';
  sel.value = known ? s.model : '__custom';
  const custom = document.getElementById('set-custom');
  custom.hidden = known;
  if (!known) custom.value = s.model;
  document.querySelectorAll('input[name="set-ctx"]').forEach(r => { r.checked = r.value === s.context; });
  document.getElementById('set-keys').checked = s.shortcuts !== false;
  const rs = CHAT.review_session;
  const avail = document.getElementById('fork-avail');
  avail.className = 'hint' + (rs?.available ? '' : ' warn');
  avail.textContent = rs?.available
    ? `Review session for this page: ${rs.id}. Threads that already started keep their context.`
    : "This page has no review session on record (for example, a review opened by hand), so its new threads use the finding only.";
  renderChatSub();
}

async function loadSettings() {
  if (!SERVED) return;
  try {
    const r = await fetch('/api/settings', { cache: 'no-store' });
    if (r.ok) SETTINGS = await r.json();
  } catch (e) { /* keep defaults */ }
  renderSettings();
  renderKeys();
}

async function saveSettings() {
  const sel = document.getElementById('set-model');
  const custom = document.getElementById('set-custom');
  const model = sel.value === '__custom' ? custom.value.trim() : sel.value;
  const context = document.querySelector('input[name="set-ctx"]:checked')?.value || 'finding';
  const out = document.getElementById('set-saved');
  if (sel.value === '__custom' && !model) { out.textContent = 'Type a model id.'; return; }
  try {
    const r = await fetch('/api/settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-Finding-Viewer': '1' }, body: JSON.stringify({ model, context, shortcuts: document.getElementById('set-keys').checked }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) { out.textContent = body.error || 'Settings not saved: the viewer server refused them. Run /findings to restart it, then try again.'; out.className = 'hint warn'; return; }
    SETTINGS = body;
    out.textContent = 'Saved for all review pages.';
    out.className = 'hint';
    renderChatSub();
    renderKeys();
  } catch (e) {
    out.textContent = 'Viewer server is off. Run /findings to restart it.';
    out.className = 'hint warn';
  }
}

function renderChat() {
  const f = D.findings[current];
  const box = document.getElementById('msgs');
  const send = document.getElementById('chatsend');
  document.getElementById('chat-title').textContent = f ? `Discuss ${f.id} with Claude` : 'Discuss with Claude';
  if (!f) return;
  if (!SERVED) {
    box.innerHTML = '<p class="hint">Chat needs the viewer server. Reopen this review with /findings.</p>';
    send.disabled = true;
    return;
  }
  const th = CHAT.threads?.[f.id];
  const msgs = th?.messages || [];
  const sig = `${f.id}:${msgs.length}:${!!th?.pending}`;
  const secs = th?.pending ? Math.max(0, Math.round(Date.now() / 1000 - th.pending.started)) : 0;
  const pendingWord = th?.pending ? `Claude: ${th.pending.status || 'Thinking'}…` : '';
  send.disabled = !!th?.pending;
  // While waiting, only the seconds tick. They stay out of the status region so a screen reader does not re-read it.
  if (sig === lastChatSig && th?.pending) {
    const word = box.querySelector('.pending [role="status"]'), clock = box.querySelector('.pending .secs');
    if (word && clock) {
      if (word.textContent !== pendingWord) word.textContent = pendingWord;
      clock.firstChild.nodeValue = `${secs}s`;
      return;
    }
  }
  let html = msgs.length ? '' : `<p class="hint">Ask anything about ${esc(f.id)}: is it real, what's the fix, or draft the PR comment. Claude gets the finding, its code and your note, and can read the rest of the repo.</p>`;
  for (const m of msgs) {
    if (m.role === 'user') { html += `<div class="msg user">${esc(m.text)}</div>`; continue; }
    // A line the page added: a re-review moved the code on since this chat started (rereview.py).
    if (m.role === 'system') { html += `<p class="msg system">${esc(m.text)}</p>`; continue; }
    const spend = typeof m.cost === 'number' ? ` · $${m.cost.toFixed(2)}`
      : m.context_tokens ? ` · read ${Math.round(m.context_tokens / 1000)}k tokens of context` : '';
    const cost = (m.model ? ` · ${prettyModel(m.model)}` : '') + spend;
    const steps = m.steps?.length ? `<details class="steps"><summary>${m.steps.length} step${m.steps.length > 1 ? 's' : ''}${cost}</summary>${m.steps.map(esc).join('<br>')}</details>`
      : cost ? `<div class="steps">${cost.slice(3)}</div>` : '';
    html += `<div class="msg claude${m.stopped ? ' stopped' : m.error ? ' error' : ''}"><div class="who">Claude</div>${markdown(m.text)}${steps}</div>`;
  }
  if (th?.pending) html += `<div class="pending"><span class="pulse"></span><span><span role="status">${esc(pendingWord)}</span> <span class="secs" aria-hidden="true">${secs}s</span></span></div>`;
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.innerHTML = html;
  box.querySelectorAll('.msg.claude pre code').forEach(c => {
    if (c.classList.contains('language-comment')) {
      const btn = Object.assign(document.createElement('button'), { className: 'linkbtn usenote', textContent: useLabel() });
      btn.dataset.text = c.textContent;
      c.closest('pre').after(btn);
    } else if (window.hljs) {
      try { hljs.highlightElement(c); } catch (e) { /* leave unhighlighted */ }
    }
  });
  if (sig !== lastChatSig || atBottom) box.scrollTop = box.scrollHeight;
  lastChatSig = sig;
  renderChatSub();
  syncLock();
}

async function loadChat() {
  if (SERVED) {
    try {
      const r = await fetch('/api/chat/' + encodeURIComponent(D.page), { cache: 'no-store' });
      if (r.ok) { CHAT = await r.json(); CHAT.threads ??= {}; }
    } catch (e) { /* server off: keep the last known chat */ }
  }
  renderChat();
  renderChatToggle();
  // The poll runs every second while Claude answers; rebuild the list only when a thread starts, ends or grows.
  const listSig = JSON.stringify(Object.entries(CHAT.threads || {}).map(([id, t]) => [id, !!t.pending, (t.messages || []).length]));
  if (listSig !== lastListSig) { lastListSig = listSig; renderList(); }
  // A finished reply only removes the pending row, which says nothing; say it.
  const waiting = Object.keys(CHAT.threads || {}).filter(id => CHAT.threads[id].pending);
  for (const id of pendingThreads) if (!waiting.includes(id)) {
    const last = (CHAT.threads[id]?.messages || []).at(-1);
    // A stop was already said by the Running panel, where it was made.
    if (!last?.stopped) announce(last?.error ? `Claude could not answer about ${id}.` : `Claude answered about ${id}.`);
  }
  pendingThreads = waiting;
  clearTimeout(chatTimer);
  if (Object.values(CHAT.threads || {}).some(t => t.pending)) chatTimer = setTimeout(loadChat, 1000);
}

const chatHidden = () => document.body.classList.contains('chat-off');

function setChatHidden(hidden) {
  document.body.classList.toggle('chat-off', hidden);
  try { localStorage.setItem(CHAT_HIDDEN_KEY, hidden ? '1' : ''); } catch (e) { /* private mode: the choice lasts until reload */ }
  renderChatToggle();
}

function renderChatToggle() {
  const btn = document.getElementById('chat-show');
  btn.hidden = !chatHidden();
  const busy = Object.values(CHAT.threads || {}).some(t => t.pending);
  btn.innerHTML = `${busy ? '<span class="pulse" role="img" aria-label="Claude is answering" title="Claude is answering"></span>' : '💬'} Show chat`;
}

function chatError(text) {
  const box = document.getElementById('msgs');
  box.insertAdjacentHTML('beforeend', `<div class="msg claude error"><div class="who">Not sent</div>${esc(text)}</div>`);
  box.scrollTop = box.scrollHeight;
  announce(`Not sent: ${text}`);
}

async function sendChat(text) {
  text = (text || '').trim();
  if (!text || !SERVED) return;
  if (locked()) return chatError(LOCK_WHY);
  const f = D.findings[current];
  if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; await pushRemote(); }
  try {
    const r = await fetch(`/api/chat/${encodeURIComponent(D.page)}/${encodeURIComponent(f.id)}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Finding-Viewer': '1' }, body: JSON.stringify({ message: text }),
    });
    const body = await r.json().catch(() => ({}));
    if (r.status === 423) loadActivity();  // a re-review another tab started: lock this one too
    if (!r.ok) return chatError(body.error || 'The viewer server refused the message and gave no reason. Run /findings to restart it, then send again.');
    const input = document.getElementById('chatin');
    input.value = '';
    input.style.height = 'auto';
    CHAT.threads ??= {};
    CHAT.threads[f.id] = body.thread;
    loadChat();
    loadActivity();
  } catch (e) {
    chatError('The viewer server is off. Run /findings to restart it.');
  }
}

function useAsNote(text) {
  if (locked()) return lockedSay();
  const f = D.findings[current];
  const t = tri(f.id);
  const draft = text.trim();
  // As your own comment the draft replaces the text; as a note it goes under what you already wrote.
  t.note = !ownComment() && (t.note || '').trim() ? `${t.note.trim()}\n\nComment draft:\n${draft}` : draft;
  persist();
  const note = document.getElementById('note');
  note.value = t.note;
  autosize(note);
  renderList();
  note.scrollIntoView({ behavior: scrollBehavior(), block: 'center' });
  note.focus();
}

/* ---------- actions ---------- */

function select(i, push = true) {
  if (i < 0 || i >= D.findings.length) return;
  current = i;
  renderList();
  renderDetail();
  renderChat();
  // j, k or Enter while a list item has focus: focus follows the selection instead of staying on the old item.
  if (document.activeElement?.classList.contains('item')) document.querySelector('.item.sel')?.focus({ preventScroll: true });
  document.querySelector('.item.sel')?.scrollIntoView({ block: 'nearest' });
  if (push) history.replaceState(null, '', '#' + encodeURIComponent(D.findings[i].id));
}

function setStatus(status) {
  if (locked()) return lockedSay();
  const f = D.findings[current];
  const t = tri(f.id);
  t.status = t.status === status ? null : status;
  delete t.recheck;  // you looked at it again
  persist();
  refreshTriage();
  renderList();
}

function clearStatus() {
  if (locked()) return lockedSay();
  const t = tri(D.findings[current].id);
  t.status = null;
  delete t.recheck;
  persist();
  refreshTriage();
  renderList();
}

// "Still post it": a finding that changed after you chose Post stays Post, and the mark goes.
function confirmRecheck() {
  if (locked()) return lockedSay();
  delete tri(D.findings[current].id).recheck;
  persist();
  refreshTriage();
  announce('Still marked Post.');
}

const allOrder = () => groupsOf().flatMap(([, , has]) => D.findings.map((f, i) => [f, i]).filter(([f]) => has(f)).map(([, i]) => i));
const visibleOrder = () => allOrder().filter(i => visible(D.findings[i]));

function nextUndecided() {
  const order = allOrder();
  const pos = order.indexOf(current);
  for (let k = 1; k <= order.length; k++) {
    const i = order[(pos + k) % order.length];
    if (!statusOf(D.findings[i]) && !isFixed(D.findings[i])) return select(i);
  }
  // Nothing left to decide: point at the next step.
  const target = ['draft', 'send-fixes', 'copy'].map(id => document.getElementById(id)).find(b => !b.hidden);
  target.classList.remove('flash'); void target.offsetWidth; target.classList.add('flash');
}

function setAnchor(path, line) {
  if (locked()) return lockedSay();
  const f = D.findings[current];
  tri(f.id).anchor = { path, line };
  delete tri(f.id).anchor_reset;  // you picked a line again
  persist();
  rerender(document.getElementById('rr-marks'), rrMarks(f), ['data-tri']);
  document.getElementById('tri-anchor').innerHTML = triAnchor(f);
  rerender(document.getElementById('cards'), cardsHtml(f), CARD_KEYS, cardFallback);
  announce(localMode() ? `Line set to ${path}:${line}`
    : `Comment goes on ${path}:${line}, ${commentable({ path, line }) ? 'inside the diff' : 'outside the diff'}`);
}

// [ and ]: move the anchor one line up or down in the same file, widening the code window when that line is not shown.
function moveAnchor(delta) {
  if (locked()) return lockedSay();
  const f = D.findings[current];
  const a = f && anchorOf(f);
  if (!a) return;
  const n = D.files[a.path].lines.length;
  const line = Math.min(n, Math.max(1, a.line + delta));
  if (!n || line === a.line) return;
  const shown = f.locations.map((l, li) => l.path === a.path && windows[`${current}:${li}`]).filter(Boolean);
  if (shown.length && !shown.some(w => w.a <= line && line <= w.b)) {
    const w = shown.find(w => w.a <= a.line && a.line <= w.b) || shown[0];
    w.a = Math.min(w.a, line);
    w.b = Math.max(w.b, line);
  }
  setAnchor(a.path, line);
  document.querySelector('tr.anchor')?.scrollIntoView({ block: 'nearest' });
}

function triageText() {
  const p = payload();
  const items = Object.entries(p.findings);
  const fmt = ([id, v]) => {
    const a = v.anchor ? `${v.anchor.path}:${v.anchor.line}${v.anchor.in_diff ? '' : ' (outside the diff)'}` : 'no anchor';
    const label = v.mode === 'comment' && v.status !== 'fix' && !localMode() ? 'My comment (post word for word)' : 'Note';
    return `- ${id} ${v.title} @ ${a}` + (v.note.trim() ? `\n  ${label}: ${v.note.trim().replace(/\n/g, '\n  ')}` : '');
  };
  // As in the hand-off (summarize): a finding the new commits fixed is in none of the three, but in its own group.
  const fixed = new Set(D.findings.filter(isFixed).map(f => f.id));
  // On a local review an old Post is undecided, as on the page.
  const stOf = v => (localMode() && v.status === 'post') ? null : (v.status || null);
  const group = (label, st) => {
    const rows = items.filter(([id, v]) => !fixed.has(id) && stOf(v) === st).map(fmt);
    return rows.length ? `${label}:\n${rows.join('\n')}` : '';
  };
  const fixedRows = D.findings.filter(isFixed).map(f => `- ${f.id} ${f.title}${f.rereview.why ? ` (${f.rereview.why})` : ''}`);
  // A local review has nothing to post, so the fixed line says "left out" there, not "not to post".
  const fixedPart = fixedRows.length ? `Fixed by the new commits, so ${localMode() ? 'left out' : 'not to post'}:\n${fixedRows.join('\n')}` : '';
  if (localMode()) {
    const n = st => items.filter(([id, v]) => !fixed.has(id) && stOf(v) === st).length;
    return [`Triage of "${p.title}": ${n('fix')} to fix, ${n('skip')} won't fix, ${n(null)} undecided`, group('To fix locally', 'fix'),
      group("Won't fix", 'skip'), group('Undecided', null), fixedPart].filter(Boolean).join('\n\n');
  }
  return [`Triage of "${p.title}"${p.pr ? ` (PR #${p.pr})` : ''}`, group('To post', 'post'), group('To fix locally', 'fix'), group("Don't post", 'skip'), group('Undecided', null),
    fixedPart].filter(Boolean).join('\n\n');
}

async function copyTriage() {
  const text = triageText();
  const btn = document.getElementById('copy');
  try { await navigator.clipboard.writeText(text); }
  catch (e) {
    const ta = Object.assign(document.createElement('textarea'), { value: text });
    document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove();
  }
  btn.textContent = 'Copied';
  announce('Copied');
  setTimeout(() => { btn.textContent = 'Copy triage'; }, 1500);
}

/* ---------- events ---------- */

document.getElementById('list').addEventListener('click', e => {
  const b = e.target.closest('.item');
  if (b) select(+b.dataset.i);
});
document.getElementById('filters').addEventListener('click', e => {
  const b = e.target.closest('.chip');
  if (!b) return;
  hiddenSev.has(b.dataset.sev) ? hiddenSev.delete(b.dataset.sev) : hiddenSev.add(b.dataset.sev);
  updateToolbar();
  renderList();
});
document.getElementById('statusFilter').addEventListener('click', e => {
  const b = e.target.closest('button');
  if (!b) return;
  statusFilter = b.dataset.status;
  updateToolbar();
  renderList();
});
document.getElementById('copy').addEventListener('click', copyTriage);
document.getElementById('send-fixes').addEventListener('click', sendFixes);
document.getElementById('draft').addEventListener('click', () => {
  const X = DS();
  if (!X || (X.status === 'failed' && !X.review)) startDraft(); else openDrafts();
});
const draftsEl = document.getElementById('drafts');
draftsEl.addEventListener('click', e => {
  if (e.target === draftsEl || e.target.id === 'drafts-close') return closeDrafts();
  const act = e.target.closest('button[data-act]')?.dataset.act;
  if (act === 'draft') startDraft();
  else if (act === 'post') postDrafts();
  else if (act === 'discard') discardDrafts();
  else if (act === 'close') closeDrafts();
});
draftsEl.addEventListener('input', e => {
  const R = draftEdit, t = e.target;
  if (!R) return;
  if (t.id === 'd-body') R.body = t.value;
  else if (t.id === 'd-event') R.event = t.value;
  else if (t.dataset.body) R.comments[+t.dataset.body].body = t.value;
  else if (t.dataset.level) R.comments[+t.dataset.level].level = t.value;
  else if (t.dataset.include) {
    R.comments[+t.dataset.include].include = t.checked;
    t.closest('.dcard').classList.toggle('off', !t.checked);
  }
  updatePostButton();
});
document.getElementById('fresh').addEventListener('click', e => {
  if (e.target.closest('#fresh-again')) loadFreshness(true);
  else if (e.target.closest('#rr-start')) startRereview(false);
  else if (e.target.closest('#rr-go')) startRereview(true);
  else if (e.target.closest('#rr-cancel')) cancelRereview();
});
document.getElementById('fresh').addEventListener('keydown', e => { if (e.key === 'Escape' && rrConfirm) { e.stopPropagation(); cancelRereview(); } });
document.getElementById('rereview').addEventListener('click', e => {
  const b = e.target.closest('[data-rr]');
  if (b?.dataset.rr === 'stop') stopRereview(b);
  else if (b?.dataset.rr === 'dismiss') { rrNote = null; renderRereview(); }
});
jobsBtn().addEventListener('click', () => toggleJobs(jobsPanel().hidden));
jobsPanel().addEventListener('click', e => {
  const stop = e.target.closest('[data-stop]');
  if (stop) return stopJob(stop);
  const copy = e.target.closest('[data-copy]');
  if (copy) copyResume(copy);
});
// A click anywhere else closes the panel. The path is taken at dispatch, so a button the panel rebuilt still counts as inside.
document.addEventListener('click', e => {
  if (!jobsPanel().hidden && !e.composedPath().some(n => n === jobsPanel() || n === jobsBtn())) toggleJobs(false);
});
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') { loadHandoff(); loadActivity(); }
  if (document.visibilityState === 'visible' && (!FRESH || Date.now() / 1000 - FRESH.checked_at > 60)) loadFreshness();
});
document.getElementById('chatform').addEventListener('submit', e => {
  e.preventDefault();
  sendChat(document.getElementById('chatin').value);
});
document.getElementById('chatin').addEventListener('input', e => {
  e.target.style.height = 'auto';
  e.target.style.height = e.target.scrollHeight + 2 + 'px';
});
document.getElementById('quick').addEventListener('click', e => {
  const b = e.target.closest('button[data-q]');
  if (b) sendChat(b.dataset.q);
});
function toggleSettings(open) {
  const panel = document.getElementById('settings');
  panel.hidden = !open;
  document.getElementById('gear').classList.toggle('on', open);
  if (open) renderSettings();
}
document.getElementById('gear').addEventListener('click', () => toggleSettings(document.getElementById('settings').hidden));
document.getElementById('chat-hide').addEventListener('click', () => setChatHidden(true));
document.getElementById('chat-show').addEventListener('click', () => setChatHidden(false));
document.getElementById('set-done').addEventListener('click', () => toggleSettings(false));
document.getElementById('settings').addEventListener('change', e => {
  if (e.target.id === 'set-model') {
    const custom = document.getElementById('set-custom');
    custom.hidden = e.target.value !== '__custom';
    if (!custom.hidden) { custom.focus(); return; }
  }
  saveSettings();
});
document.getElementById('msgs').addEventListener('click', e => {
  const b = e.target.closest('.usenote');
  if (b) useAsNote(b.dataset.text);
});
document.getElementById('detail').addEventListener('input', e => {
  if (e.target.id !== 'note') return;
  const hadNote = !!noteOf(D.findings[current]);
  tri(D.findings[current].id).note = e.target.value;
  autosize(e.target);
  persist();
  if (hadNote !== !!noteOf(D.findings[current])) renderList();
});
document.getElementById('detail').addEventListener('click', e => {
  const m = e.target.closest('[data-mode]');
  if (m) { setNoteMode(m.dataset.mode); return; }
  const t = e.target.closest('[data-tri]');
  if (t) {
    if (t.dataset.tri === 'next') nextUndecided();
    else if (t.dataset.tri === 'confirm') confirmRecheck();
    else setStatus(t.dataset.tri);
    return;
  }
  const num = e.target.closest('.ln[data-line]');
  if (num) { setAnchor(num.dataset.path, +num.dataset.line); return; }
  const b = e.target.closest('.more');
  if (b) {
    const w = windows[b.dataset.key];
    const [fi, li] = b.dataset.key.split(':').map(Number);
    const n = D.files[D.findings[fi].locations[li].path].lines.length;
    if (b.dataset.act === 'up') w.a = Math.max(1, w.a - STEP);
    if (b.dataset.act === 'down') w.b = Math.min(n, w.b + STEP);
    if (b.dataset.act === 'all') { w.a = 1; w.b = n; }
    const tmp = document.createElement('div');
    tmp.innerHTML = codeCard(D.findings[fi], fi, D.findings[fi].locations[li], li);
    const card = tmp.firstElementChild, hadFocus = document.activeElement === b;
    b.closest('.card').replaceWith(card);
    // The same button again, or the card itself once that button is gone (top or bottom reached).
    if (hadFocus) (card.querySelector(`.more[data-act="${b.dataset.act}"]`) || card).focus({ preventScroll: true });
    return;
  }
  const j = e.target.closest('code.jump');
  if (j) {
    const card = document.getElementById('loc-' + j.dataset.li);
    card.scrollIntoView({ behavior: scrollBehavior(), block: 'center' });
    card.classList.remove('flash'); void card.offsetWidth; card.classList.add('flash');
  }
});
window.addEventListener('hashchange', () => {
  const i = D.findings.findIndex(f => '#' + encodeURIComponent(f.id) === location.hash);
  if (i >= 0 && i !== current) select(i, false);
});
document.addEventListener('keydown', e => {
  const el = document.activeElement;
  if (/INPUT|TEXTAREA|SELECT/.test(el?.tagName || '')) {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      if (el.id === 'chatin') sendChat(el.value); else el.blur();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      el.blur();
      if (el.closest('#settings')) toggleSettings(false);
    }
    return;
  }
  if (document.getElementById('drafts').open) {
    // The drafts dialog is open: Esc closes it, and the page's single-key shortcuts stay off.
    if (e.key === 'Escape') closeDrafts();
    return;
  }
  if (e.key === 'Escape' && !jobsPanel().hidden) {
    // Focus goes back to the button when it was in the panel (or on the button), and stays put otherwise.
    toggleJobs(false, el === jobsBtn() || jobsPanel().contains(el));
    return;
  }
  if (e.key === 'Escape' && !document.getElementById('settings').hidden) { toggleSettings(false); return; }
  // Option stays allowed for typed characters: on Spanish, German or French Mac layouts [ and ] need it. Cmd and Ctrl
  // stay with the browser (Cmd+[ is Back), and so do Option+arrows.
  if (e.metaKey || e.ctrlKey || (e.altKey && e.key.length !== 1)) return;
  // Single-key shortcuts off (for speech input): only the arrows and Escape act.
  if (!shortcutsOn() && e.key.length === 1) return;
  const order = visibleOrder();
  const pos = order.indexOf(current);
  const keys = {
    j: () => select(order[Math.min(order.length - 1, pos + 1)] ?? order[0]),
    ArrowDown: () => select(order[Math.min(order.length - 1, pos + 1)] ?? order[0]),
    k: () => select(order[Math.max(0, pos - 1)] ?? order[0]),
    ArrowUp: () => select(order[Math.max(0, pos - 1)] ?? order[0]),
    a: () => { if (!localMode()) setStatus('post'); },
    f: () => { if (canFix(D.findings[current])) setStatus('fix'); },
    r: () => setStatus('skip'),
    u: clearStatus,
    n: nextUndecided,
    e: () => document.getElementById('note')?.focus(),
    c: () => { if (chatHidden()) setChatHidden(false); document.getElementById('chatin').focus(); },
    h: () => setChatHidden(!chatHidden()),
    o: () => document.querySelector('.card-h a[href^="vscode:"]')?.click(),
    '[': () => moveAnchor(-1),
    ']': () => moveAnchor(1),
  };
  if (keys[e.key]) { e.preventDefault(); keys[e.key](); }
});

document.getElementById('home').hidden = !SERVED;
let startHidden = false;
try { startHidden = localStorage.getItem(CHAT_HIDDEN_KEY) === '1'; } catch (e) { /* private mode: start with the chat shown */ }
setChatHidden(startHidden);
renderHeader();
updateToolbar();
const startAt = D.findings.findIndex(f => '#' + encodeURIComponent(f.id) === location.hash);
select(startAt >= 0 ? startAt : (visibleOrder()[0] ?? 0), false);
rereviewedNote();
loadState().then(() => { updateToolbar(); renderList(); renderDetail(); });
loadChat().then(loadSettings);
loadFreshness();
renderJobsButton();
loadActivity();
// The list links "drafts ready" to page.html#drafts, which opens the drafts dialog.
loadHandoff().then(() => { if (location.hash === '#drafts' && HANDOFF?.can_draft) openDrafts(); });
