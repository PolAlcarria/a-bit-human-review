// Drives headless Chromium over CDP (no npm packages, Node 22+): opens a demo page, triages the findings with the
// keyboard and saves one PNG per step, with smooth zooms between regions of the page. record.sh turns the frames
// into a GIF with ImageMagick. Usage: node record.mjs <pr|local> <page url> <frames folder>
import { spawn } from 'node:child_process';
import { mkdirSync, writeFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const [mode, url, framesDir] = process.argv.slice(2);
const port = 9333, W = 1280, H = 800;
mkdirSync(framesDir, { recursive: true });
const chrome = spawn(process.env.CHROMIUM || 'chromium', [
  '--headless=new', '--use-mock-keychain', '--password-store=basic', '--hide-scrollbars', '--no-first-run',
  `--remote-debugging-port=${port}`, `--user-data-dir=${mkdtempSync(join(tmpdir(), 'abhr-chrome-'))}`,
  `--window-size=${W},${H}`, 'about:blank',
], { stdio: 'ignore' });

const sleep = ms => new Promise(r => setTimeout(r, ms));
let target;
for (let i = 0; i < 50 && !target; i++) {
  try { target = (await (await fetch(`http://127.0.0.1:${port}/json`)).json()).find(t => t.type === 'page'); } catch { await sleep(200); }
}
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener('open', r));
let id = 0;
const pending = new Map();
ws.addEventListener('message', e => { const m = JSON.parse(e.data); pending.get(m.id)?.(m.result); });
const send = (method, params = {}) => new Promise(r => { pending.set(++id, r); ws.send(JSON.stringify({ id, method, params })); });
const js = async expr => (await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true }))?.result?.value;

let n = 0, cur = { x: 0, y: 0, w: W };
const durations = [];
async function shot(seconds) {
  const { data } = await send('Page.captureScreenshot', { format: 'png', clip: { x: cur.x, y: cur.y, width: cur.w, height: cur.w * H / W, scale: W / cur.w } });
  writeFileSync(join(framesDir, `f${String(n++).padStart(3, '0')}.png`), Buffer.from(data, 'base64'));
  durations.push(seconds > 0.5 ? seconds * 0.75 : seconds);  // holds a bit shorter: a README GIF should be quick
}
// Zoom to a region (x, y, width; the height follows the page's aspect ratio) over `steps` eased frames, then hold.
async function view(x, y, w, hold = 1.5, steps = 6) {
  const from = cur;
  for (let i = 1; i <= steps; i++) {
    const t = i / steps, e = t < .5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2;
    cur = { x: from.x + (x - from.x) * e, y: from.y + (y - from.y) * e, w: from.w + (w - from.w) * e };
    await shot(i < steps ? 0.07 : hold);
  }
}
async function key(k, hold = 1) {
  const code = k.length === 1 ? { text: k, key: k, code: `Key${k.toUpperCase()}` } : { key: k, code: k };
  await send('Input.dispatchKeyEvent', { type: 'keyDown', ...code });
  await send('Input.dispatchKeyEvent', { type: 'keyUp', ...code });
  await sleep(450);
  await shot(hold);
}
async function type(text, per = 4) {
  for (let i = 0; i < text.length; i += per) {
    await send('Input.insertText', { text: text.slice(i, i + per) });
    await sleep(60);
    await shot(0.09);
  }
  await shot(0.8);
}
async function click(selector, hold = 1) {
  const r = await js(`(() => { const b = document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect(); return [b.x + b.width / 2, b.y + b.height / 2]; })()`);
  for (const t of ['mousePressed', 'mouseReleased']) await send('Input.dispatchMouseEvent', { type: t, x: r[0], y: r[1], button: 'left', clickCount: 1 });
  await sleep(450);
  await shot(hold);
}
async function waitFor(expr, ms = 20000) {
  for (let t = 0; t < ms; t += 250) { if (await js(expr)) return; await sleep(250); }
  throw new Error(`timed out waiting for ${expr}`);
}

await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: 1, mobile: false });
await send('Page.navigate', { url });
await sleep(2500);
await shot(2);
await key('h', 0.4);                       // hide the side chat so the code shows

if (mode === 'local') {
  await view(0, 0, 760, 2.2);              // the findings list and the decision card
  await key('f', 1.2);                     // C1: fix
  await view(225, 330, 700, 2.2);          // the code the finding points at
  await view(0, 0, W, 0.4);
  await key('j', 0.8); await key('f', 1);  // C2: fix
  await key('j', 0.8); await key('r', 1);  // W1: won't fix
  await key('j', 0.8); await key('f', 1);  // W2: fix
  await view(560, 0, 720, 3);              // the progress and "Send fixes to session"
} else {
  await view(0, 0, 760, 2.4);              // PR #4242, the findings list, severity
  await key('a', 1.2);                     // C1: post
  await view(225, 330, 700, 2.4);          // the code, with the finding's line marked
  await view(0, 0, W, 0.4);
  await key('j', 0.8); await key('a', 1);  // C2: post
  await key('j', 0.8);                     // W1: post, with a note
  await key('e', 0.4);
  await view(225, 90, 760, 0.3);
  await type('Re-raise the last error instead of returning None.');
  await key('Escape', 0.4);
  await key('a', 1.4);
  await view(0, 0, W, 0.4);
  await key('j', 0.8); await key('r', 1.2); // W2: don't post
  await view(560, 0, 720, 1.6);            // progress and the Draft comments button
  await click('#draft', 0.5);
  await view(0, 0, W, 0.5);
  await waitFor(`document.getElementById('drafts').open && !!document.getElementById('d-post') && !document.getElementById('d-post').disabled`);
  await shot(2.5);                         // the drafted comments, to edit
  await view(240, 80, 800, 3);
  await click('#d-post', 0.8);
  await view(0, 0, W, 0.5);
  await sleep(2500);
  await shot(3.5);                         // posted
}

// ImageMagick's arguments, one per line: each frame with its delay in hundredths of a second.
writeFileSync(join(framesDir, 'gif-args.txt'), durations.map((d, i) =>
  `-delay\n${Math.max(2, Math.round(d * 100))}\nf${String(i).padStart(3, '0')}.png`).join('\n') + '\n');
ws.close();
chrome.kill();
process.exit(0);
