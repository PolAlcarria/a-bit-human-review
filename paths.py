"""Where the finding viewer's code and its runtime data live, resolved once at import.

CODE is the folder of these modules: the page templates, and the scripts the detached children run. DATA holds all the
tool writes: the review pages and their state (out/), the cached clones and worktrees, the hooks' state files, the
settings, the optional config.json (config.py) and the log. DATA is FINDING_VIEWER_DATA when set, else
CLAUDE_PLUGIN_DATA (set by Claude Code for a plugin's hooks), else ~/.local/share/a-bit-human-review.

The data used to live beside the code; migrate_if_needed moves it into DATA once and leaves symlinks in its old place.
migrate_layout gives each review page in out/ its own folder, from the flat files older versions wrote.
"""
from __future__ import annotations

import datetime as dt
import errno
import fcntl
import json
import os
import re
import shutil
from pathlib import Path
from typing import Callable

CODE = Path(__file__).resolve().parent
DATA = Path(os.environ.get('FINDING_VIEWER_DATA') or os.environ.get('CLAUDE_PLUGIN_DATA')
            or Path.home() / '.local' / 'share' / 'a-bit-human-review').expanduser().absolute()
OUT = DATA / 'out'
CLONES = DATA / 'clones'
WORKTREES = DATA / 'worktrees'
LOG = DATA / 'viewer.log'
STATE = DATA / 'state.json'
INJECT_STATE = DATA / 'inject_state.json'
SETTINGS = DATA / 'settings.json'
CONFIG = DATA / 'config.json'
# What migrate_if_needed moves from CODE, out/ last: until out/ is in DATA, the next run finishes an interrupted move.
# clones/ and worktrees/ stay behind: they are caches, rebuilt in DATA on demand.
MOVED = ('state.json', 'inject_state.json', 'settings.json', 'viewer.log', 'out')
# A page's flat file from before each page had its own folder: <page>.<name>, the page named by its creation time.
FLAT_RE = re.compile(r'^(2\d{7}-\d{6}-[^./]*)\.(.+)$')
DATA_BLOCK_RE = re.compile(r'<script type="application/json" id="data">(.*?)</script>', re.S)

try:
    DATA.mkdir(parents=True, exist_ok=True)
except OSError:
    pass


def log(prefix: str = '') -> Callable[[str], None]:
    """A function that appends one timestamped line, ``prefix`` first, to LOG. It never raises."""
    def write(msg: str) -> None:
        try:
            with LOG.open('a', encoding='utf-8') as fh:
                fh.write(f'{dt.datetime.now().isoformat(timespec="seconds")} {prefix}{msg}\n')
        except OSError:
            pass
    return write


def migrate_if_needed() -> str | None:
    """Move the data of a checkout from before DATA (out/ and the state files beside the code) into DATA, once.

    Runs only when DATA has no out/ and CODE has a real out/ folder: a link there is left alone (an earlier run made it,
    or the user did), so pointing FINDING_VIEWER_DATA elsewhere later gives a fresh folder. Under a lock each item in
    MOVED moves when DATA does not have it yet and leaves a symlink to its new place behind, so a server or a job still
    running the old code keeps reading and writing the same files; out/latest.html is re-pointed into DATA. Returns the
    line it logged, None when there was nothing to move. Never raises: the hooks call it first.
    """
    moved: list[str] = []
    try:
        old_out = CODE / 'out'
        if OUT.exists() or old_out.is_symlink() or not old_out.is_dir():
            return None
        with open(DATA / '.migrate.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            # Another process moved it while this one waited.
            if OUT.exists() or old_out.is_symlink() or not old_out.is_dir():
                return None
            for name in MOVED:
                src, dst = CODE / name, DATA / name
                if src.exists() and not dst.exists():
                    _move(src, dst)
                    moved.append(name)
            latest = OUT / 'latest.html'
            if latest.is_symlink():
                target = Path(os.readlink(latest))
                if target.is_absolute() and target.parent != OUT:
                    latest.unlink()
                    latest.symlink_to(OUT / target.name)
    except Exception as exc:  # noqa: BLE001 — the hooks run this first and must never fail
        line = f'stopped after moving {", ".join(moved) or "nothing"} from {CODE} to {DATA}: {exc!r}'
    else:
        line = f'moved {", ".join(moved)} from {CODE} to {DATA}, leaving links behind'
    log('migrate: ')(line)
    return line


def migrate_layout() -> str | None:
    """Give each review page in out/ its own folder (out/<page>/ with review.json, page.html, the triage, chats and job
    states, and locks/), from the flat files older versions wrote (out/<page>.html, out/<page>.decisions.json, ...).

    Runs whenever out/ holds a page's flat file, so a file that a process still running the old code writes later is
    folded in by the next run; a flat file replaces the folder's copy, being the newer. Under .migrate.lock, like
    migrate_if_needed. A re-review that died while putting its staged files in place is finished first (the renames it
    recorded), so nothing it staged is lost. out/latest.html is re-pointed at the page's page.html. A file that cannot be
    moved, or a page whose re-review commit cannot finish (its files stay flat, so its staged files are not deleted), is
    named in the line and left for the next run; the others still move. Returns the line it logged, None when there was
    nothing to do. Never raises: the hooks call it first, so the check is one listing of out/.
    """
    pages: list[str] = []
    real: list[str] = []
    failed: list[str] = []
    first_error: Exception | None = None
    try:
        if not _flat_files():
            return None
        with open(DATA / '.migrate.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            flat = _flat_files()  # another process may have done it while this one waited
            pages = sorted({FLAT_RE.match(p.name).group(1) for p in flat})
            held: set[str] = set()  # pages whose re-review commit could not finish: their files stay flat
            for page in pages:
                try:
                    _finish_commit(page)
                except (OSError, ValueError, TypeError, AttributeError) as exc:
                    held.add(page)
                    failed.append(f'{page}.rereview.json')
                    first_error = first_error or exc
            # Before the folds: a run cut short after its last fold must not leave latest.html dangling. A held page
            # keeps its flat html, so latest.html keeps pointing at that until the next run moves it.
            latest = OUT / 'latest.html'
            if latest.is_symlink():
                m = FLAT_RE.match(Path(os.readlink(latest)).name)
                if m and m.group(2) == 'html' and m.group(1) not in held:
                    latest.unlink()
                    latest.symlink_to(OUT / m.group(1) / 'page.html')
            real = [p for p in pages if (OUT / f'{p}.html').is_file() or (OUT / p / 'page.html').is_file()]
            for path in _flat_files():
                if FLAT_RE.match(path.name).group(1) in held:  # folding would delete its staged files: next run
                    continue
                try:
                    _fold(path)
                except (OSError, ValueError, TypeError, AttributeError) as exc:
                    failed.append(path.name)
                    first_error = first_error or exc
    except Exception as exc:  # noqa: BLE001 — the hooks run this first and must never fail
        line = f'stopped giving the review pages in {OUT} their own folder ({", ".join(pages) or "none yet"}): {exc!r}'
    else:
        if not pages:
            return None
        gone = len(pages) - len(real)  # pages whose flat files are all that is left: a page deleted meanwhile
        line = f'gave {len(real)} review page{"s" * (len(real) != 1)} in {OUT} their own folder'
        if gone:
            line += f'; moved the leftover files of {gone} deleted page{"s" * (gone != 1)} into folders too'
        if failed:
            line += f'; could not move {", ".join(failed)}: {first_error!r}'
    log('migrate: ')(line)
    return line


def _flat_files() -> list[Path]:
    """The pages' flat files in out/: regular files named <page>.<name>."""
    try:
        with os.scandir(OUT) as entries:
            return [Path(e.path) for e in entries if FLAT_RE.match(e.name) and e.is_file(follow_symlinks=False)]
    except OSError:
        return []


def _finish_commit(page: str) -> None:
    """Take the renames a re-review recorded but did not finish (its ``committing`` steps, flat names in out/). A
    ``discard_drafts`` step is left for the re-review code, which finishes the job on its next read."""
    try:
        st = json.loads((OUT / f'{page}.rereview.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return
    for step in (st.get('committing') or []) if isinstance(st, dict) else []:
        if not isinstance(step, dict):
            continue
        if step.get('put'):
            if isinstance(step.get('to'), str) and (OUT / step['put']).is_file():
                os.replace(OUT / step['put'], OUT / step['to'])
        elif step.get('drop'):
            (OUT / step['drop']).unlink(missing_ok=True)


def _fold(path: Path) -> None:
    """Move one flat file into its page's folder: <page>.html as page.html, with its data block as review.json;
    <page>.<name>.lock into locks/; <page>.<name> as <name>. A staged or temporary file of a write that never finished
    is deleted."""
    page, name = FLAT_RE.match(path.name).groups()
    if '.staged-' in name or name == 'tmp' or name.endswith('.tmp'):
        path.unlink(missing_ok=True)
        return
    folder = OUT / page
    folder.mkdir(exist_ok=True)
    if name == 'html':
        m = DATA_BLOCK_RE.search(path.read_text(encoding='utf-8'))
        if m:
            try:
                data = json.loads(m.group(1))
                tmp = folder / 'review.tmp'
                tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
                os.replace(tmp, folder / 'review.json')
            except ValueError:  # a data block that does not parse: the page still moves, without review.json
                (folder / 'review.tmp').unlink(missing_ok=True)
        os.replace(path, folder / 'page.html')
    elif name.endswith('.lock'):
        (folder / 'locks').mkdir(exist_ok=True)
        os.replace(path, folder / 'locks' / name)
    else:
        os.replace(path, folder / name)


def _move(src: Path, dst: Path) -> None:
    """Move ``src`` to ``dst`` and leave a symlink to ``dst`` in its place, for processes still running the old code."""
    if src.is_symlink():  # a state file linked elsewhere: link to where it points, not to the link
        dst.symlink_to(os.path.realpath(src))
        src.unlink()
    else:
        try:
            os.rename(src, dst)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            # Another filesystem: copy under a temporary name first, so a half copy is never taken for the data.
            tmp = dst.with_name(f'.{dst.name}.migrating')
            if tmp.is_dir():
                shutil.rmtree(tmp)
            if src.is_dir():
                shutil.copytree(src, tmp, symlinks=True)
            else:
                shutil.copy2(src, tmp)
            os.rename(tmp, dst)
            if src.is_dir():
                shutil.rmtree(src)
            else:
                src.unlink()
    src.symlink_to(dst)
