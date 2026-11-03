'use strict';
/* The diff review: every change of a branch since its base, read line by line. The reviewer selects lines and asks
   Claude about them or asks for a change; each selection is a thread. Threads live in the triage (decisions.json, as
   records with `user: true`), their chats in chat.json, as a review's findings do (see docs/behaviour.md). */
const D = JSON.parse(document.getElementById('data').textContent);
const SERVED = location.protocol === 'http:' || location.protocol === 'https:';
const LS_KEY = 'finding-viewer:' + D.page;
const CHAT_HIDDEN_KEY = 'finding-viewer:chat-hidden';
const STEP = 20, CTX = 3;
// The buttons under the chat when the server cannot say (a page opened as a file): the built-in ones (buttons.py).
const DEFAULT_BUTTONS = [
  { label: 'Explain', prompt: 'Explain what this code does and why it may have been written this way.' },
  { label: 'Bugs?', prompt: 'Check these lines for bugs, edge cases and anything that does not match the code around them.' },
  { label: 'Simpler?', prompt: 'Is there a simpler or more idiomatic way to write this? Show it briefly.' },
  { label: 'Word the change', prompt: 'Word a change request for the coding session about these lines. Follow my note if I wrote one.' },
];

const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const base = p => p.split('/').pop();
const dirOf = p => p.includes('/') ? p.slice(0, p.lastIndexOf('/') + 1) : '';
const phrase = (k, one, many) => `${k} ${k === 1 ? one : many}`;
const scrollBehavior = () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
// The folders of the changed files, as the list shows them: folders before files, each by name.
function buildTree(paths) {
  const root = { name: '', path: '', dirs: new Map(), files: [] };
  for (const p of paths) {
    const parts = p.split('/');
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      const key = parts.slice(0, i + 1).join('/');
      if (!node.dirs.has(parts[i])) node.dirs.set(parts[i], { name: parts[i], path: key, dirs: new Map(), files: [] });
      node = node.dirs.get(parts[i]);
    }
    node.files.push(p);
  }
  return root;
}
const byName = (a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }) || (a < b ? -1 : a > b ? 1 : 0);
const sortedDirs = node => [...node.dirs.values()].sort((a, b) => byName(a.name, b.name));
const sortedFiles = node => [...node.files].sort((a, b) => byName(base(a), base(b)));
function treeOrder(node) {
  return [...sortedDirs(node).flatMap(treeOrder), ...sortedFiles(node)];
}
const PATHS = treeOrder(buildTree(Object.keys(D.files)));
let announceTimer = null;
function announce(text) {
  const el = document.getElementById('announce');
  if (!el) return;
  el.textContent = '';
  clearTimeout(announceTimer);
  announceTimer = setTimeout(() => { el.textContent = text; }, 100);
}
// Rebuild a container and keep keyboard focus on the equivalent element.
function rerender(el, html, attrs) {
  const a = el.contains(document.activeElement) ? document.activeElement : null;
  const want = a ? attrs.filter(k => a.hasAttribute(k)).map(k => [k, a.getAttribute(k)]) : [];
  el.innerHTML = html;
  if (!a || !want.length) return;
  el.querySelector(want.map(([k, v]) => `[${k}="${CSS.escape(v)}"]`).join(''))?.focus({ preventScroll: true });
}
function markdown(src) {
  if (!src) return '';
  if (window.marked && window.DOMPurify) {
    try { return DOMPurify.sanitize(marked.parse(src, { gfm: true })); } catch (e) { /* fall through to raw text */ }
  }
  return `<pre class="raw">${esc(src)}</pre>`;
}
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
const hlCache = {};
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

/* ---------- state ---------- */

let T = { updated: null, findings: {} };   // the triage: one record per thread, by id
let CHAT = { threads: {} };
let SETTINGS = { settings: { model: '', context: 'finding', shortcuts: true }, models: [], buttons: null };
let HANDOFF = null;
let curPath = PATHS[0] || null;            // the file on show
let curId = null;                          // the thread on show
let sel = null;                            // the selected lines: { path, a, b }
let selAnchor = null;                      // where the selection started: { path, line }
let filter = 'all';
const EXTRA = {};                          // lines the reviewer opened beyond the hunks, by file: [[a, b], ...]
let saveTimer = null, savePending = false, chatTimer = null, lastChatSig = '';

const records = () => Object.entries(T.findings).filter(([, t]) => t && t.user === true && Array.isArray(t.locations) && t.locations.length);
const statusOf = t => t.status === 'fix' || t.status === 'skip' ? t.status : null;
const pathOf = t => t.locations[0].path;
const rangeText = (a, b) => `${a}${b > a ? '–' + b : ''}`;
const whereText = t => `${pathOf(t)}:${rangeText(t.locations[0].start, t.locations[0].end)}`;
const threadsIn = path => records().filter(([, t]) => t.locations.some(l => l.path === path));
function counts() {
  const all = records();
  const fix = all.filter(([, t]) => statusOf(t) === 'fix').length, skip = all.filter(([, t]) => statusOf(t) === 'skip').length;
  return { total: all.length, fix, skip, open: all.length - fix - skip };
}
function newId() {
  let n = 1;
  while (T.findings['t' + n]) n++;
  return 't' + n;
}
function titleFor(path, a, b) {
  const text = (D.files[path].lines.slice(a - 1, b).find(l => l.trim()) || '').trim().slice(0, 60);
  return `${base(path)}:${rangeText(a, b)}${text ? ' · ' + text : ''}`;
}

/* ---------- saving ---------- */

function payload() {
  return { page: D.page, title: D.title, subtitle: D.subtitle, session_id: D.session_id, pr: null, repo_root: D.repo_root,
    kind: 'diff', updated: T.updated, rereviews: 0, findings: Object.fromEntries(records()) };
}
function setSaved(text, warn = false) {
  const el = document.getElementById('saved');
  if (el.textContent === text && el.classList.contains('warn') === warn) return;
  el.textContent = text;
  el.classList.toggle('warn', warn);
}
function persist() {
  T.updated = new Date().toISOString();
  try { localStorage.setItem(LS_KEY, JSON.stringify(T)); } catch (e) { /* private mode: the server copy still works */ }
  updateToolbar();
  renderList();
  if (!SERVED) return;
  clearTimeout(saveTimer);
  setSaved('Saving…');
  savePending = true;
  saveTimer = setTimeout(pushRemote, 400);
}
async function pushRemote() {
  savePending = false;
  clearTimeout(saveTimer);
  saveTimer = null;
  try {
    const r = await fetch('/api/decisions/' + encodeURIComponent(D.page), {
      method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-Finding-Viewer': '1' }, body: JSON.stringify(payload()),
    });
    if (r.ok) { setSaved('Saved · Claude gets your change requests with your next message'); loadHandoff(); return true; }
    const err = (await r.json().catch(() => null))?.error;
    setSaved(r.status === 404 ? 'Not saved to disk: the viewer server no longer has this review. Your threads are kept in this browser.'
      : `Not saved to disk: ${typeof err === 'string' && err.trim() ? err.trim() : 'the viewer server refused the save'}. Your threads are kept in this browser.`, true);
  } catch (e) {
    setSaved('Not saved to disk: the viewer server did not answer. Your threads are kept in this browser; run /findings to restart it.', true);
  }
  return false;
}
async function loadState() {
  let local = null, remote = null;
  try { local = JSON.parse(localStorage.getItem(LS_KEY) || 'null'); } catch (e) { local = null; }
  if (SERVED) {
    try {
      const r = await fetch('/api/decisions/' + encodeURIComponent(D.page), { cache: 'no-store' });
      const body = r.ok ? await r.json() : null;
      if (body && body.findings) {
        remote = { updated: body.updated, findings: Object.fromEntries(Object.entries(body.findings).filter(([, v]) => v && v.user === true)) };
      }
    } catch (e) { remote = null; }
  }
  const pick = [local, remote].filter(x => x && x.updated).sort((a, b) => b.updated.localeCompare(a.updated))[0];
  if (pick) T = { updated: pick.updated, findings: pick.findings || {} };
  if (SERVED && local?.updated && (!remote?.updated || local.updated > remote.updated)) pushRemote();
  if (!SERVED) setSaved('Opened as a file: threads stay in this browser. Reopen with /findings diff to chat with Claude.', true);
  else if (remote?.updated) setSaved('Saved · Claude gets your change requests with your next message');
}

/* ---------- header, list ---------- */

function renderHeader() {
  document.getElementById('title').textContent = D.title;
  document.getElementById('subtitle').textContent = D.subtitle;
  const meta = [`<span>Code: <b>${esc(D.ref_label)}</b></span>`, `<span>Against: <b>${esc(D.base)}</b> (merge-base ${esc(D.merge_base)})</span>`];
  document.getElementById('meta').innerHTML = meta.join('');
  const notes = document.getElementById('notes');
  notes.hidden = !(D.notes || []).length;
  notes.innerHTML = (D.notes || []).map(esc).join('<br>');
  document.getElementById('home').hidden = !SERVED;
}
function updateToolbar() {
  const c = counts();
  document.getElementById('progress').innerHTML = c.total
    ? `<span><b>${c.total}</b> ${c.total === 1 ? 'thread' : 'threads'}</span><span><b class="fix">${c.fix}</b> to change</span><span><b class="keep">${c.skip}</b> resolved</span><span><b>${c.open}</b> open</span>`
    : '<span>Select lines to ask Claude about them, or to ask for a change.</span>';
  const seg = document.getElementById('statusFilter');
  seg.hidden = !c.total;
  seg.innerHTML = [['all', 'All', c.total], ['open', 'Open', c.open], ['fix', 'Change', c.fix], ['skip', 'Resolved', c.skip]]
    .map(([k, label, n]) => `<button type="button" data-f="${k}" class="${filter === k ? 'on' : ''}" aria-pressed="${filter === k}">${label} ${n}</button>`).join('');
  document.getElementById('copy').hidden = !c.total;
  updateSendButton();
}
const showThread = t => filter === 'all' || (filter === 'open' ? !statusOf(t) : statusOf(t) === filter);

function renderList() {
  renderThreads();
  renderFiles();
}
function renderThreads() {
  const box = document.getElementById('threads');
  const mine = records().filter(([, t]) => showThread(t)).sort(([, a], [, b]) => (a.created || '').localeCompare(b.created || ''));
  let html = '';
  if (mine.length) {
    html += `<h2 class="sev-finding"><span class="dot"></span>Threads · ${mine.length}</h2>`;
    for (const [id, t] of mine) {
      const st = statusOf(t), chat = CHAT.threads?.[id];
      const mark = st === 'fix' ? '<span class="fix" role="img" aria-label="Change requested" title="Change requested">⚒︎</span>'
        : st === 'skip' ? '<span class="skip" role="img" aria-label="Resolved" title="Resolved">✓</span>' : '';
      const note = (t.note || '').trim() ? '<span class="note" role="img" aria-label="Has your note" title="Has your note">✎</span>' : '';
      const talk = chat?.pending ? '<span class="chat" role="img" aria-label="Claude is answering" title="Claude is answering">⋯</span>'
        : chat?.messages?.length ? '<span class="chat" role="img" aria-label="Has a chat with Claude" title="Has a chat with Claude">💬</span>' : '';
      html += `<button class="item sev-finding${id === curId ? ' sel' : ''}${st ? ' is-' + st : ''}" data-th="${esc(id)}"${id === curId ? ' aria-current="true"' : ''}>
        <span class="fid">${esc(id)}</span><span class="t">${esc(t.title)}</span><span class="st">${mark}${note}${talk}</span><span class="l">${esc(whereText(t))}</span></button>`;
    }
  }
  rerender(box, html, ['data-th']);
}

/* ---------- the list of files: folders that fold, and a search by name ---------- */

const TREE_KEY = 'finding-viewer:tree:' + D.page;
let fileQuery = '';
// Folders the reviewer folded, by path. A folder that holds only another folder shows as one row ("src/app"); its key is
// the path of the outer one.
const FOLDED = new Set();
try { JSON.parse(localStorage.getItem(TREE_KEY) || '[]').forEach(k => typeof k === 'string' && FOLDED.add(k)); } catch (e) { /* private mode */ }
function saveFolded() {
  try { localStorage.setItem(TREE_KEY, JSON.stringify([...FOLDED])); } catch (e) { /* private mode: until reload */ }
}
const searchTerms = () => fileQuery.toLowerCase().split(/\s+/).filter(Boolean);
const pathMatches = (p, terms) => terms.every(t => p.toLowerCase().includes(t));
const matchingPaths = () => { const terms = searchTerms(); return terms.length ? PATHS.filter(p => pathMatches(p, terms)) : PATHS; };
const dirPrefixes = p => p.split('/').slice(0, -1).map((_, i, parts) => parts.slice(0, i + 1).join('/'));
// Open the folders above a file, so the list shows it.
function reveal(path) {
  const before = FOLDED.size;
  dirPrefixes(path).forEach(k => FOLDED.delete(k));
  if (FOLDED.size !== before) saveFolded();
}
const filesIn = node => node.files.length + [...node.dirs.values()].reduce((n, d) => n + filesIn(d), 0);
const allFilesIn = node => [...node.files, ...[...node.dirs.values()].flatMap(allFilesIn)];

function fileRow(p, depth) {
  const f = D.files[p];
  const del = Object.values(f.removed_text || {}).reduce((n, l) => n + l.length, 0);
  const n = threadsIn(p).length;
  const kind = { added: 'A', deleted: 'D' }[f.status] || 'M';
  const pad = 16 + depth * 14;
  return `<button class="item file${p === curPath ? ' sel' : ''}" data-file="${esc(p)}" style="padding-left:${pad}px" title="${esc(p)}"${p === curPath ? ' aria-current="true"' : ''}>
    <span class="fid st-${kind}" title="${esc(f.status || 'modified')}">${kind}</span><span class="t">${esc(base(p))}</span>
    <span class="st">${n ? `<span class="chat" role="img" aria-label="${phrase(n, 'thread', 'threads')}" title="${phrase(n, 'thread', 'threads')}">💬${n}</span>` : ''}</span>
    <span class="l">+${f.added.length} −${del}</span></button>`;
}
function dirRows(node, depth, searching) {
  let html = '';
  for (let dir of sortedDirs(node)) {
    // One row for a chain of folders that each hold only the next one.
    let label = dir.name;
    const top = dir;
    while (!dir.files.length && dir.dirs.size === 1) { dir = [...dir.dirs.values()][0]; label += '/' + dir.name; }
    const open = searching || !FOLDED.has(top.path);
    const inside = allFilesIn(dir);
    const talk = inside.reduce((n, p) => n + threadsIn(p).length, 0);
    const holdsCur = !open && inside.includes(curPath);
    html += `<button class="item dir${holdsCur ? ' sel' : ''}" data-dir="${esc(top.path)}" aria-expanded="${open}" style="padding-left:${16 + depth * 14}px" title="${esc(dir.path)}">
      <span class="fid caret" aria-hidden="true">${open ? '▾' : '▸'}</span><span class="t">${esc(label)}/</span>
      <span class="st">${!open && talk ? `<span class="chat" role="img" aria-label="${phrase(talk, 'thread', 'threads')} inside" title="${phrase(talk, 'thread', 'threads')} inside">💬${talk}</span>` : ''}</span>
      <span class="l">${phrase(inside.length, 'file', 'files')}</span></button>`;
    if (open) html += dirRows(dir, depth + 1, searching) + sortedFiles(dir).map(p => fileRow(p, depth + 1)).join('');
  }
  return html;
}
function renderFiles() {
  const box = document.getElementById('files');
  const terms = searchTerms(), shown = matchingPaths();
  const root = buildTree(shown);
  let html = dirRows(root, 0, !!terms.length) + sortedFiles(root).map(p => fileRow(p, 0)).join('');
  if (!shown.length) html = `<div class="empty small">No changed file matches “${esc(fileQuery.trim())}”.</div>`;
  document.getElementById('files-h').textContent = terms.length ? `Files · ${shown.length} of ${PATHS.length}` : `Files · ${PATHS.length}`;
  const fold = document.getElementById('files-fold');
  const hasDirs = PATHS.some(p => p.includes('/'));
  fold.hidden = !hasDirs || !!terms.length;
  fold.textContent = FOLDED.size ? 'Unfold all' : 'Fold all';
  rerender(box, html, ['data-file', 'data-dir']);
}
function toggleDir(key) {
  if (FOLDED.has(key)) FOLDED.delete(key); else FOLDED.add(key);
  saveFolded();
  renderFiles();
}
document.getElementById('files-fold').addEventListener('click', () => {
  if (FOLDED.size) FOLDED.clear(); else PATHS.forEach(p => dirPrefixes(p).forEach(k => FOLDED.add(k)));
  saveFolded();
  renderFiles();
});
const search = document.getElementById('filesearch');
search.addEventListener('input', () => { fileQuery = search.value; renderFiles(); });
search.addEventListener('keydown', e => {
  if (e.key === 'Enter') {
    const first = matchingPaths()[0];
    if (first) { e.preventDefault(); openFile(first); announce(`Opened ${first}.`); }
  } else if (e.key === 'Escape' && search.value) {
    e.preventDefault(); e.stopPropagation();
    search.value = ''; fileQuery = ''; renderFiles();
  }
});
// Bring the file on show into view in the list.
const showCurrentFile = () => document.querySelector('#files .item.file.sel')?.scrollIntoView({ block: 'nearest' });

/* ---------- the diff ---------- */

function visibleRanges(path) {
  const f = D.files[path], n = f.lines.length;
  const raw = [...(f.hunks || []), ...(EXTRA[path] || [])];
  for (const k of Object.keys(f.removed_text || {})) raw.push([Math.max(1, +k - CTX + 1), Math.min(n, +k + CTX)]);
  for (const [, t] of threadsIn(path)) for (const l of t.locations) if (l.path === path) raw.push([Math.max(1, l.start - CTX), Math.min(n, l.end + CTX)]);
  const ranges = [];
  for (const [a0, b0] of raw.map(([a, b]) => [Math.max(1, a), Math.min(n, b)]).filter(([a, b]) => a <= b).sort((x, y) => x[0] - y[0] || x[1] - y[1])) {
    const last = ranges.at(-1);
    if (last && a0 <= last[1] + 1) last[1] = Math.max(last[1], b0);
    else ranges.push([a0, b0]);
  }
  return ranges;
}
function lineClasses(path) {
  // line -> thread ids that cover it (a thread of the file on show)
  const map = {};
  for (const [id, t] of threadsIn(path)) for (const l of t.locations) if (l.path === path) for (let i = l.start; i <= l.end; i++) (map[i] ??= []).push(id);
  return map;
}
const gapRow = (path, a, b) => {
  const size = b - a + 1;
  const btn = (act, label) => `<button type="button" class="more" data-gap="${act}" data-a="${a}" data-b="${b}">${label}</button>`;
  return `<tr class="gap"><td colspan="4"><div class="gapbar"><span>${phrase(size, 'unchanged line', 'unchanged lines')} hidden</span>
    ${a > 1 ? btn('up', `↓ ${Math.min(STEP, size)} below the change above`) : ''}${b < D.files[path].lines.length ? btn('down', `↑ ${Math.min(STEP, size)} above the change below`) : ''}${size > STEP ? btn('all', 'Show all') : ''}</div></td></tr>`;
};
const delRows = lines => lines.map(t => `<tr class="del"><td class="g"></td><td class="n"></td><td class="m">−</td><td class="c">${esc(t) || ' '}</td></tr>`).join('');

function fileTable(path) {
  const f = D.files[path], n = f.lines.length, added = new Set(f.added), hl = highlightedLines(path);
  const rt = f.removed_text || {}, byLine = lineClasses(path);
  const rows = [];
  if (rt['0']) rows.push(delRows(rt['0']));
  let prev = 0;
  for (const [a, b] of visibleRanges(path)) {
    if (a > prev + 1) rows.push(gapRow(path, prev + 1, a - 1));
    for (let i = a; i <= b; i++) {
      const ids = byLine[i] || [];
      const shown = ids.includes(curId) ? curId : ids[0];
      const st = shown ? statusOf(T.findings[shown]) : null;
      const cls = [added.has(i) ? 'add' : '', shown ? 'flag t-' + (st || 'open') : '', shown && shown === curId ? 'cur' : '',
        sel && sel.path === path && i >= sel.a && i <= sel.b ? 'selrow' : ''].filter(Boolean).join(' ');
      const badges = records().filter(([, t]) => t.locations.some(l => l.path === path && l.start === i)).map(([id, t]) =>
        `<button type="button" class="fb s-${statusOf(t) || 'open'}${id === curId ? ' cur' : ''}" data-th="${esc(id)}" title="${esc(t.title)}">${esc(id)}</button>`).join('');
      rows.push(`<tr class="${cls}" data-l="${i}"><td class="g">${badges}</td><td class="n"><button type="button" class="ln" data-line="${i}" aria-label="Select line ${i}" aria-pressed="${!!(sel && sel.path === path && i >= sel.a && i <= sel.b)}">${i}</button></td><td class="m">${added.has(i) ? '+' : ''}</td><td class="c">${hl[i - 1] || ' '}</td></tr>`);
      if (rt[i]) rows.push(delRows(rt[i]));
    }
    prev = b;
  }
  if (n && prev < n) rows.push(gapRow(path, prev + 1, n));
  return rows.join('');
}

function renderDetail() {
  const main = document.getElementById('detail');
  if (!curPath) { main.innerHTML = '<div class="empty">Nothing to show.</div>'; return; }
  const f = D.files[curPath];
  const del = Object.values(f.removed_text || {}).reduce((n, l) => n + l.length, 0);
  const abs = `${D.repo_root}/${curPath}`;
  const gh = D.github && D.blob_ref && f.status !== 'deleted' ? `<a href="${esc(D.github)}/blob/${esc(D.blob_ref)}/${esc(curPath)}" target="_blank" rel="noopener">GitHub</a>` : '';
  const i = PATHS.indexOf(curPath);
  const top = main.scrollTop;
  main.innerHTML = `<p class="hint pick">Click a line number (shift-click for a range), or drag over the code. Then ask Claude about it or ask for a change.
      <kbd>[</kbd> <kbd>]</kbd> change file · <kbd>/</kbd> search files · <kbd>c</kbd> chat · <kbd>Esc</kbd> clear</p>
    <section class="card" id="file-card">
      <div class="card-h"><code class="path">${esc(curPath)}</code><span class="badge">${esc(f.status || 'modified')}</span>
        <span class="badge in">+${f.added.length}</span><span class="badge warn">−${del}</span>${f.truncated ? '<span class="badge warn">truncated: the page keeps the first 30000 lines</span>' : ''}
        <span class="spacer"></span><span class="rng">${i + 1} / ${PATHS.length}</span>
        ${f.status === 'deleted' ? '' : `<a href="vscode://file/${encodeURI(abs)}">Open in VS Code</a>`}${gh}</div>
      <div class="scroll"><table class="code diff" id="difftable">${fileTable(curPath)}</table></div>
    </section>`;
  main.scrollTop = top;
}
function repaintFile() {
  const t = document.getElementById('difftable');
  if (t) t.innerHTML = fileTable(curPath);
}
function paintSelection() {
  document.querySelectorAll('#difftable tr[data-l]').forEach(tr => {
    const l = +tr.dataset.l, on = !!sel && sel.path === curPath && l >= sel.a && l <= sel.b;
    tr.classList.toggle('selrow', on);
    tr.querySelector('.ln')?.setAttribute('aria-pressed', String(on));
  });
  renderSelbar();
}

/* ---------- selecting lines ---------- */

function setSel(path, a, b) {
  sel = a == null ? null : { path, a: Math.min(a, b), b: Math.max(a, b) };
  if (!sel) selAnchor = null;
  paintSelection();
  if (sel) announce(`Selected ${sel.b > sel.a ? 'lines ' + rangeText(sel.a, sel.b) : 'line ' + sel.a} of ${base(path)}.`);
}
function renderSelbar() {
  const bar = document.getElementById('selbar');
  bar.hidden = !sel;
  if (!sel) { bar.innerHTML = ''; return; }
  bar.innerHTML = `<span class="where"><b>${esc(base(sel.path))}</b> · ${sel.b > sel.a ? 'lines ' + rangeText(sel.a, sel.b) : 'line ' + sel.a}</span>
    <button type="button" class="tb ask" data-act="ask">${SERVED ? 'Ask Claude' : 'Add thread'}</button>
    <button type="button" class="tb fix" data-act="change">Request a change</button>
    <button type="button" class="linkbtn" data-act="clear">Clear</button>`;
}
function rowOf(node) {
  const el = node && (node.nodeType === 1 ? node : node.parentElement);
  return el?.closest?.('#difftable tr[data-l]') || null;
}
document.getElementById('detail').addEventListener('click', e => {
  const th = e.target.closest('[data-th]');
  if (th) return openThread(th.dataset.th);
  const gap = e.target.closest('button[data-gap]');
  if (gap) {
    const a = +gap.dataset.a, b = +gap.dataset.b, how = gap.dataset.gap;
    (EXTRA[curPath] ??= []).push(how === 'up' ? [a, Math.min(b, a + STEP - 1)] : how === 'down' ? [Math.max(a, b - STEP + 1), b] : [a, b]);
    return repaintFile();
  }
  const ln = e.target.closest('button.ln');
  if (!ln) return;
  const line = +ln.dataset.line;
  if (e.shiftKey && selAnchor && selAnchor.path === curPath) return setSel(curPath, selAnchor.line, line);
  if (sel && sel.a === line && sel.b === line) return setSel(null);
  selAnchor = { path: curPath, line };
  setSel(curPath, line, line);
});
// Dragging over the code selects its lines.
document.getElementById('detail').addEventListener('mouseup', () => {
  const s = window.getSelection?.();
  if (!s || s.isCollapsed || !s.rangeCount) return;
  const a = rowOf(s.anchorNode), b = rowOf(s.focusNode);
  if (!a || !b) return;
  selAnchor = { path: curPath, line: +a.dataset.l };
  setSel(curPath, +a.dataset.l, +b.dataset.l);
});
document.getElementById('selbar').addEventListener('click', e => {
  const act = e.target.closest('button')?.dataset.act;
  if (act === 'clear') { window.getSelection?.()?.removeAllRanges(); setSel(null); }
  else if (act === 'ask' || act === 'change') startThread(act === 'change');
});

/* ---------- threads ---------- */

function startThread(change) {
  if (!sel) return;
  const { path, a, b } = sel;
  const same = records().find(([, t]) => t.locations.length === 1 && t.locations[0].path === path && t.locations[0].start === a && t.locations[0].end === b);
  let id = same?.[0];
  if (!id) {
    id = newId();
    T.findings[id] = { user: true, status: null, note: '', mode: 'note', title: titleFor(path, a, b), severity: 'finding', body: '',
      anchor: null, locations: [{ path, start: a, end: b }], created: new Date().toISOString() };
  }
  if (change && !statusOf(T.findings[id])) T.findings[id].status = 'fix';
  window.getSelection?.()?.removeAllRanges();
  sel = null; selAnchor = null;
  persist();
  openThread(id, change ? 'note' : 'chat');
}
function openThread(id, focus) {
  const t = T.findings[id];
  if (!t || !t.locations) return;
  curId = id;
  curPath = pathOf(t);
  reveal(curPath);
  history.replaceState(null, '', '#' + encodeURIComponent(id));
  renderList();
  showCurrentFile();
  renderDetail();
  renderSelbar();
  renderThread();
  renderChat();
  const row = document.querySelector(`#difftable tr[data-l="${t.locations[0].start}"]`);
  row?.scrollIntoView({ block: 'center', behavior: scrollBehavior() });
  if (focus === 'chat') { if (chatHidden()) setChatHidden(false); document.getElementById('chatin').focus(); }
  else if (focus === 'note') document.getElementById('note')?.focus();
}
function closeThread() {
  curId = null;
  history.replaceState(null, '', location.pathname + location.search);
  renderList(); repaintFile(); renderThread(); renderChat();
}
function removeThread(id) {
  if (CHAT.threads?.[id]?.messages?.length) return announce('This thread has a chat, so it stays. Mark it resolved instead.');
  delete T.findings[id];
  if (curId === id) curId = null;
  persist();
  repaintFile(); renderThread(); renderChat();
  announce('Thread removed.');
}
function setThreadStatus(id, status) {
  const t = T.findings[id];
  t.status = t.status === status ? null : status;
  persist();
  renderThread();
  repaintFile();
  announce({ fix: 'Change requested.', skip: 'Marked resolved.' }[t.status] || 'Marked open.');
}
function renderThread() {
  const box = document.getElementById('thread');
  const t = curId && T.findings[curId];
  box.hidden = !t;
  if (!t) { box.innerHTML = ''; return; }
  const st = statusOf(t);
  box.innerHTML = `<div class="th-h"><b>${esc(curId)}</b><code>${esc(whereText(t))}</code><span class="spacer"></span>
      <button type="button" class="linkbtn" data-act="goto" title="Scroll to the lines">Show lines</button>
      <button type="button" class="linkbtn" data-act="remove" title="Remove this thread (only while it has no chat)">Remove</button>
      <button type="button" class="linkbtn" data-act="close" aria-label="Close the thread" title="Close the thread">✕</button></div>
    <div class="tri-row"><button type="button" class="tb fix${st === 'fix' ? ' on' : ''}" data-act="fix" aria-pressed="${st === 'fix'}">⚒︎ Request a change</button>
      <button type="button" class="tb skip${st === 'skip' ? ' on' : ''}" data-act="skip" aria-pressed="${st === 'skip'}">✓ Resolved</button>
      <span class="tri-state">${st === 'fix' ? 'Claude changes this code when you send.' : st === 'skip' ? 'No change wanted.' : 'Open: a question so far.'}</span></div>
    <textarea id="note" rows="2" aria-label="Your note on these lines" placeholder="${st === 'fix' ? 'What the code should do instead. Claude follows it when it changes the code.' : 'Optional note. Becomes the change request once you pick “Request a change”.'}">${esc(t.note)}</textarea>`;
  autosize(document.getElementById('note'));
}
function autosize(el) {
  if (!el) return;
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight + 2, 200) + 'px';
}
document.getElementById('thread').addEventListener('click', e => {
  const act = e.target.closest('button')?.dataset.act;
  if (!act || !curId) return;
  if (act === 'fix' || act === 'skip') setThreadStatus(curId, act);
  else if (act === 'remove') removeThread(curId);
  else if (act === 'close') closeThread();
  else if (act === 'goto') document.querySelector(`#difftable tr[data-l="${T.findings[curId].locations[0].start}"]`)?.scrollIntoView({ block: 'center', behavior: scrollBehavior() });
});
document.getElementById('thread').addEventListener('input', e => {
  if (e.target.id !== 'note' || !curId) return;
  T.findings[curId].note = e.target.value;
  autosize(e.target);
  persist();
});
document.getElementById('list').addEventListener('click', e => {
  const th = e.target.closest('[data-th]'), file = e.target.closest('[data-file]'), dir = e.target.closest('[data-dir]');
  if (th) openThread(th.dataset.th, 'chat');
  else if (file) openFile(file.dataset.file);
  else if (dir) toggleDir(dir.dataset.dir);
});
function openFile(path) {
  if (!D.files[path]) return;
  curPath = path;
  reveal(path);
  if (curId && !T.findings[curId]?.locations.some(l => l.path === path)) curId = null;
  renderList(); renderDetail(); renderSelbar(); renderThread(); renderChat();
  showCurrentFile();
  document.getElementById('detail').scrollTop = 0;
}
document.getElementById('statusFilter').addEventListener('click', e => {
  const f = e.target.closest('button[data-f]')?.dataset.f;
  if (f) { filter = f; updateToolbar(); renderList(); }
});

/* ---------- hand-off to the session ---------- */

const SS = () => HANDOFF?.send;
const sendBusy = () => SS()?.status === 'sending';
const SEND_HINT = 'Type any prompt in the session and it gets your change requests.';
let sendError = '', sendPosting = false, handoffTimer = null;
function updateSendButton() {
  const btn = document.getElementById('send-fixes'), why = document.getElementById('send-why');
  const X = SS(), to = HANDOFF?.session?.name || 'the session';
  const busy = sendBusy() || sendPosting;
  const current = !!X && X.version === HANDOFF?.version;
  const sent = current && X.status === 'sent';
  const error = sendError || (current && X.status === 'failed' ? X.error || `The changes were not sent. ${SEND_HINT}` : '');
  btn.hidden = !SERVED || !(counts().fix || busy);
  btn.textContent = busy ? 'Sending…' : sent ? '✓ Sent' : 'Send changes to session';
  btn.title = busy ? `Sending your change requests to ${X?.to || to}.`
    : sent ? `Sent to ${X.to || to}. Change a request to send again.`
    : `Sends your change requests to ${to} now, so Claude starts on them without waiting for your next message there.`;
  if (busy) btn.setAttribute('aria-disabled', 'true'); else btn.removeAttribute('aria-disabled');
  why.hidden = btn.hidden || busy || !error;
  why.textContent = error;
}
async function loadHandoff() {
  if (!SERVED) return;
  try {
    const r = await fetch('/api/handoff/' + encodeURIComponent(D.page), { cache: 'no-store' });
    if (r.ok) HANDOFF = await r.json();
  } catch (e) { /* server off: keep the last answer */ }
  updateSendButton();
  clearTimeout(handoffTimer);
  if (sendBusy()) handoffTimer = setTimeout(loadHandoff, 1500);
}
async function sendFixes() {
  if (sendBusy() || sendPosting) return;
  if (SS()?.status === 'sent' && SS().version === HANDOFF?.version) return announce('Already sent. Change a request to send again.');
  sendPosting = true;
  updateSendButton();
  try {
    if (savePending) await pushRemote();
    sendError = '';
    try {
      const r = await fetch('/api/send-fixes/' + encodeURIComponent(D.page), { method: 'POST', headers: { 'X-Finding-Viewer': '1' } });
      const body = await r.json().catch(() => null);
      if (!r.ok && body?.status) { if (HANDOFF) HANDOFF.send = body; }
      else if (!r.ok) sendError = (typeof body?.error === 'string' && body.error.trim()) || `The viewer server refused to send the changes. ${SEND_HINT}`;
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
}
document.getElementById('send-fixes').addEventListener('click', sendFixes);

function requestsText() {
  const lines = [`Diff review "${D.title}" (${D.subtitle})`];
  const all = records().sort(([, a], [, b]) => (a.created || '').localeCompare(b.created || ''));
  const group = (title, pick) => {
    const items = all.filter(([, t]) => pick(statusOf(t)));
    if (!items.length) return;
    lines.push('', title);
    for (const [id, t] of items) lines.push(`- ${id} ${whereText(t)}${(t.note || '').trim() ? '\n    ' + t.note.trim().replace(/\n/g, '\n    ') : ''}`);
  };
  group('Changes requested:', s => s === 'fix');
  group('Open (questions):', s => !s);
  group('Resolved:', s => s === 'skip');
  return lines.join('\n');
}
document.getElementById('copy').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(requestsText()); announce('Copied.'); setSaved('Copied to the clipboard.'); }
  catch (e) { setSaved('Could not copy: the browser refused. Select the text in the notes instead.', true); }
});

/* ---------- chat ---------- */

const prettyModel = id => {
  const m = /claude-([a-z]+)-(\d+)-(\d+)/.exec(id || '');
  return m ? `${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2]}.${m[3]}` : (id || '');
};
const modelLabel = id => SETTINGS.models.find(o => o.id === id)?.label || id || 'Your Claude Code default';
const chatHidden = () => document.body.classList.contains('chat-off');
function setChatHidden(hidden) {
  document.body.classList.toggle('chat-off', hidden);
  try { localStorage.setItem(CHAT_HIDDEN_KEY, hidden ? '1' : ''); } catch (e) { /* private mode */ }
  renderChatToggle();
}
function renderChatToggle() {
  const btn = document.getElementById('chat-show');
  btn.hidden = !chatHidden();
  const busy = Object.values(CHAT.threads || {}).some(t => t.pending);
  btn.innerHTML = `${busy ? '<span class="pulse" role="img" aria-label="Claude is answering" title="Claude is answering"></span>' : '💬'} Show chat`;
}
function renderChatSub() {
  const th = CHAT.threads?.[curId];
  const s = SETTINGS.settings;
  const ctx = th?.mode ? (th.mode === 'fork' ? 'forked from the session that wrote the code' : 'the selected lines only')
    : (s.context === 'fork' && CHAT.review_session?.available ? 'will fork the session that wrote the code' : 'the selected lines only');
  document.getElementById('chat-sub').textContent = `${modelLabel(s.model)} · ${ctx} · read-only.`;
}
function renderQuick() {
  const list = Array.isArray(SETTINGS.buttons?.diff) ? SETTINGS.buttons.diff : DEFAULT_BUTTONS;
  document.getElementById('quick').innerHTML = list.map(b => `<button type="button" data-q="${esc(b.prompt)}">${esc(b.label)}</button>`).join('');
}
function renderChat() {
  const box = document.getElementById('msgs');
  const send = document.getElementById('chatsend');
  const t = curId && T.findings[curId];
  document.getElementById('chat-title').textContent = t ? `Discuss ${curId} with Claude` : 'Discuss with Claude';
  document.getElementById('thread').hidden = !t;
  if (!t) {
    box.innerHTML = '<p class="hint">Select lines in the diff, then Ask Claude or Request a change. Claude gets those lines with the code around them, and can read the rest of the repo.</p>';
    send.disabled = true;
    lastChatSig = '';
    renderChatSub();
    return;
  }
  if (!SERVED) {
    box.innerHTML = '<p class="hint">Chat needs the viewer server. Reopen this diff with /findings diff.</p>';
    send.disabled = true;
    return;
  }
  const th = CHAT.threads?.[curId];
  const msgs = th?.messages || [];
  const sig = `${curId}:${msgs.length}:${!!th?.pending}`;
  const secs = th?.pending ? Math.max(0, Math.round(Date.now() / 1000 - th.pending.started)) : 0;
  const pendingWord = th?.pending ? `Claude: ${th.pending.status || 'Thinking'}…` : '';
  send.disabled = !!th?.pending;
  if (sig === lastChatSig && th?.pending) {
    const word = box.querySelector('.pending [role="status"]'), clock = box.querySelector('.pending .secs');
    if (word && clock) {
      if (word.textContent !== pendingWord) word.textContent = pendingWord;
      clock.firstChild.nodeValue = `${secs}s`;
      return;
    }
  }
  let html = msgs.length ? '' : '<p class="hint">Ask about these lines: why they are written this way, what could break, or how else they could look. Or ask Claude to word the change request.</p>';
  for (const m of msgs) {
    if (m.role === 'user') { html += `<div class="msg user">${esc(m.text)}</div>`; continue; }
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
    const kind = c.classList.contains('language-change') ? 'change' : c.classList.contains('language-comment') ? 'comment' : '';
    if (kind) {
      const btn = Object.assign(document.createElement('button'), { type: 'button', className: 'linkbtn usenote', textContent: kind === 'change' ? 'Use as change request' : 'Use as my note' });
      btn.dataset.text = c.textContent;
      btn.dataset.kind = kind;
      c.closest('pre').after(btn);
    } else if (window.hljs) {
      try { hljs.highlightElement(c); } catch (e) { /* leave unhighlighted */ }
    }
  });
  if (sig !== lastChatSig || atBottom) box.scrollTop = box.scrollHeight;
  lastChatSig = sig;
  renderChatSub();
}
document.getElementById('msgs').addEventListener('click', e => {
  const b = e.target.closest('.usenote');
  if (!b || !curId) return;
  const t = T.findings[curId], text = b.dataset.text.trim();
  t.note = (t.note || '').trim() && (t.note || '').trim() !== text ? `${t.note.trim()}\n\n${text}` : text;
  if (b.dataset.kind === 'change') t.status = 'fix';
  persist();
  renderThread();
  repaintFile();
  document.getElementById('note')?.focus();
});
async function loadChat() {
  if (SERVED) {
    try {
      const r = await fetch('/api/chat/' + encodeURIComponent(D.page), { cache: 'no-store' });
      if (r.ok) { CHAT = await r.json(); CHAT.threads ??= {}; }
    } catch (e) { /* server off: keep the last known chat */ }
  }
  renderChat();
  renderChatToggle();
  renderList();
  clearTimeout(chatTimer);
  if (Object.values(CHAT.threads || {}).some(t => t.pending)) chatTimer = setTimeout(loadChat, 1000);
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
  if (!curId) return chatError('Select lines in the diff first: the chat is about those lines.');
  // The server finds the thread's lines in the saved triage, so the thread must be saved before its first message.
  await pushRemote();
  const id = curId;
  try {
    const r = await fetch(`/api/chat/${encodeURIComponent(D.page)}/${encodeURIComponent(id)}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Finding-Viewer': '1' }, body: JSON.stringify({ message: text }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) return chatError(body.error || 'The viewer server refused the message and gave no reason. Run /findings to restart it, then send again.');
    const input = document.getElementById('chatin');
    input.value = '';
    input.style.height = 'auto';
    CHAT.threads ??= {};
    CHAT.threads[id] = body.thread;
    loadChat();
  } catch (e) {
    chatError('The viewer server is off. Run /findings to restart it.');
  }
}
document.getElementById('chatform').addEventListener('submit', e => { e.preventDefault(); sendChat(document.getElementById('chatin').value); });
document.getElementById('chatin').addEventListener('input', e => { e.target.style.height = 'auto'; e.target.style.height = e.target.scrollHeight + 2 + 'px'; });
document.getElementById('chatin').addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); sendChat(e.target.value); }
});
document.getElementById('quick').addEventListener('click', e => {
  const b = e.target.closest('button[data-q]');
  if (b) sendChat(b.dataset.q);
});
document.getElementById('chat-hide').addEventListener('click', () => setChatHidden(true));
document.getElementById('chat-show').addEventListener('click', () => setChatHidden(false));

/* ---------- settings (the model; the buttons come from chat-buttons.json) ---------- */

function renderSettings() {
  const s = SETTINGS.settings;
  const known = SETTINGS.models.some(o => o.id === s.model);
  const sel = document.getElementById('set-model');
  sel.innerHTML = SETTINGS.models.map(o => `<option value="${esc(o.id)}">${esc(o.label)}</option>`).join('') + '<option value="__custom">Other model id…</option>';
  sel.value = known ? s.model : '__custom';
  const custom = document.getElementById('set-custom');
  custom.hidden = known;
  if (!known) custom.value = s.model;
  document.querySelectorAll('input[name="set-ctx"]').forEach(r => { r.checked = r.value === s.context; });
  const fork = document.getElementById('fork-avail');
  const rs = CHAT.review_session;
  fork.className = 'hint' + (rs?.available ? '' : ' warn');
  fork.textContent = rs?.available ? `Session that wrote this code: ${rs.id}.`
    : 'No session on record for this page (the diff was opened outside Claude Code), so threads use the selected lines only.';
  renderChatSub();
}
async function loadSettings() {
  if (SERVED) {
    try {
      const r = await fetch('/api/settings', { cache: 'no-store' });
      if (r.ok) SETTINGS = await r.json();
    } catch (e) { /* keep defaults */ }
  }
  renderSettings();
  renderQuick();
}
async function saveSettings() {
  const sel = document.getElementById('set-model'), custom = document.getElementById('set-custom');
  const model = sel.value === '__custom' ? custom.value.trim() : sel.value;
  const context = document.querySelector('input[name="set-ctx"]:checked')?.value || 'finding';
  const out = document.getElementById('set-saved');
  if (sel.value === '__custom' && !model) { out.textContent = 'Type a model id.'; return; }
  try {
    const r = await fetch('/api/settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-Finding-Viewer': '1' },
      body: JSON.stringify({ model, context, shortcuts: SETTINGS.settings.shortcuts !== false }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) { out.textContent = body.error || 'Settings not saved: the viewer server refused them.'; out.className = 'hint warn'; return; }
    SETTINGS = { ...SETTINGS, ...body, buttons: SETTINGS.buttons };
    out.textContent = 'Saved for all review pages.';
    out.className = 'hint';
    renderChatSub();
  } catch (e) {
    out.textContent = 'Viewer server is off. Run /findings to restart it.';
    out.className = 'hint warn';
  }
}
function toggleSettings(open) {
  document.getElementById('settings').hidden = !open;
  document.getElementById('gear').classList.toggle('on', open);
  if (open) renderSettings();
}
document.getElementById('gear').addEventListener('click', () => toggleSettings(document.getElementById('settings').hidden));
document.getElementById('set-done').addEventListener('click', () => toggleSettings(false));
document.getElementById('settings').addEventListener('change', e => {
  if (e.target.id === 'set-model') {
    const custom = document.getElementById('set-custom');
    custom.hidden = e.target.value !== '__custom';
    if (!custom.hidden) { custom.focus(); return; }
  }
  saveSettings();
});

/* ---------- keys ---------- */

document.addEventListener('keydown', e => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const tag = (e.target.tagName || '').toLowerCase();
  const typing = tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable;
  if (e.key === 'Escape' && sel && !typing) { window.getSelection?.()?.removeAllRanges(); return setSel(null); }
  if (typing || e.target.closest?.('dialog')) return;
  const keys = {
    c: () => { if (chatHidden()) setChatHidden(false); document.getElementById('chatin').focus(); },
    '/': () => search.focus(),
    h: () => setChatHidden(!chatHidden()),
    '[': () => openFile(PATHS[Math.max(0, PATHS.indexOf(curPath) - 1)]),
    ']': () => openFile(PATHS[Math.min(PATHS.length - 1, PATHS.indexOf(curPath) + 1)]),
  };
  if (keys[e.key]) { e.preventDefault(); keys[e.key](); }
});
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') { loadHandoff(); loadChat(); } });

/* ---------- start ---------- */

let startHidden = false;
try { startHidden = localStorage.getItem(CHAT_HIDDEN_KEY) === '1'; } catch (e) { /* private mode */ }
setChatHidden(startHidden);
renderHeader();
renderQuick();
updateToolbar();
renderList();
renderDetail();
renderChat();
loadState().then(() => {
  const fromHash = decodeURIComponent(location.hash.slice(1));
  updateToolbar();
  if (fromHash && T.findings[fromHash]?.locations) openThread(fromHash); else { renderList(); renderDetail(); }
});
loadChat().then(loadSettings);
loadHandoff();
