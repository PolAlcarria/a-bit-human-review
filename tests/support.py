"""Test setup shared by every test module: import it first, before any module of the tool.

Importing it builds a scratch environment and points the tool at it, so nothing a test runs reaches the real data
folder, the real ~/.claude, the live port 47321, or the network:

- FINDING_VIEWER_DATA is a fresh temp folder that already has out/, so paths.migrate_if_needed never moves anything;
- HOME is a fake home with an empty ~/.claude/projects and ~/.claude/sessions;
- FINDING_VIEWER_PORT is a free port;
- tests/stubs (fake claude and gh) comes first on PATH;
- every FINDING_VIEWER_* variable and CLAUDE_PLUGIN_DATA are cleared from the outer environment first;
- GIT_CEILING_DIRECTORIES stops git at the scratch folder, so no test reaches a real repository or its remote.

The scratch folder is removed when the test process exits, after every job worker still running is killed. It is kept
when the processes cannot be listed.

Run one file with: python3 -m unittest discover -s tests -p 'test_jobs.py' -v (from the repo root).
"""
from __future__ import annotations

import atexit
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
FIXTURES = TESTS / 'fixtures'
STUBS = TESTS / 'stubs'

# Every module of the tool reads the environment when it is imported, so none may be loaded before this file.
_early = sorted(name for name, mod in list(sys.modules.items())
                if Path(getattr(mod, '__file__', None) or '/').resolve().parent == ROOT)
if _early:
    raise RuntimeError(f'import support before any module of the tool; already imported: {", ".join(_early)}')

SCRATCH = Path(tempfile.mkdtemp(prefix='abhr-tests-')).resolve()
DATA = SCRATCH / 'data'
OUT = DATA / 'out'
HOME = SCRATCH / 'home'
WORK = SCRATCH / 'work'  # stands in for the reviewed repository's folder
for _d in (OUT, HOME / '.claude' / 'projects', HOME / '.claude' / 'sessions', WORK):
    _d.mkdir(parents=True)


def _cleanup() -> None:
    """End every worker the tests started, then remove the scratch folder. A worker still running once the folder is
    gone would re-create DATA without out/, and paths.migrate_if_needed would then move the code folder's out/ into it.
    Each worker's command line names its job spec under SCRATCH, and it leads its own process group, so the stub claude
    it runs goes with it. When the processes cannot be listed, the folder stays: its out/ keeps any worker harmless."""
    try:
        listing = subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return
    for line in listing.splitlines():
        pid, _, command = line.strip().partition(' ')
        if str(SCRATCH) in command and pid.isdigit() and int(pid) != os.getpid():
            for kill in (os.killpg, os.kill):
                with contextlib.suppress(OSError):
                    kill(int(pid), signal.SIGKILL)
    shutil.rmtree(SCRATCH, True)


atexit.register(_cleanup)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


for _var in [v for v in os.environ if v.startswith('FINDING_VIEWER_')] + ['CLAUDE_PLUGIN_DATA']:
    os.environ.pop(_var, None)
os.environ['FINDING_VIEWER_DATA'] = str(DATA)
os.environ['HOME'] = str(HOME)
os.environ['FINDING_VIEWER_PORT'] = str(_free_port())
os.environ['PATH'] = str(STUBS) + os.pathsep + os.environ.get('PATH', '')
os.environ['GIT_CEILING_DIRECTORIES'] = str(SCRATCH)  # git never finds a repository above the scratch folder
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def fixture_text(name: str) -> str:
    """A file under tests/fixtures, as text."""
    return (FIXTURES / name).read_text(encoding='utf-8')


def fixture_json(name: str):
    """A JSON fixture: its first line, a '#' comment saying what shape it covers, is dropped before parsing."""
    text = fixture_text(name)
    if text.startswith('#'):
        text = text.split('\n', 1)[1] if '\n' in text else ''
    return json.loads(text)


def sample_page(name: str, **fields) -> dict:
    """Synthetic review data for a page called ``name``, as the renderer writes it; ``fields`` override keys."""
    data = {
        'page': name, 'title': 'Code review · PR #4242', 'subtitle': 'acme/billing', 'pr': 4242,
        'github': 'https://github.com/acme/billing', 'session_id': None, 'review_session': None,
        'repo_root': str(WORK), 'repo_name': 'billing', 'ref_label': 'working tree (feature/rounding)',
        'merge_base': 'abc12345', 'base': 'PR #4242 diff', 'notes': [], 'generated': '2026-10-19 12:00',
        'findings': [
            {'id': 'C1', 'severity': 'critical', 'title': 'Total is not rounded', 'body': 'Round before summing.',
             'recurring': False, 'meta': {},
             'locations': [{'raw': 'src/app/billing.py:42', 'path': 'src/app/billing.py', 'start': 42, 'end': 42,
                            'ambiguous': []}]},
            {'id': 'W1', 'severity': 'warning', 'title': 'Loop re-reads the rate', 'body': 'Hoist it.',
             'recurring': False, 'meta': {},
             'locations': [{'raw': 'src/app/billing.py:10-12', 'path': 'src/app/billing.py', 'start': 10, 'end': 12,
                            'ambiguous': []}]},
        ],
        'files': {'src/app/billing.py': {'lines': [f'line {i}' for i in range(1, 61)], 'lang': 'python',
                                         'added': [42], 'removed': {}, 'hunks': [[38, 45]], 'changed': True,
                                         'truncated': False}},
    }
    data.update(fields)
    return data


def write_page(name: str, data: dict | None = None) -> Path:
    """Write a page into the scratch out/ the way the renderer does: out/<name>/review.json (the data the server reads)
    and a minimal out/<name>/page.html beside it; returns the page.html path."""
    data = sample_page(name) if data is None else data
    folder = OUT / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'review.json').write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    path = folder / 'page.html'
    payload = json.dumps(data, ensure_ascii=False).replace('<', '\\u003c')
    path.write_text(f'<!doctype html><title>{name}</title>\n<script type="application/json" id="data">{payload}</script>\n',
                    encoding='utf-8')
    return path


# ---------------------------------------------------------------- job workers (tests/test_jobs.py)

SESSION = 'sess-0001-aaaa'  # the fake review session that drafts and forks start from


def fake_session(sid: str = SESSION, cwd: Path = WORK) -> Path:
    """A fake Claude Code transcript for ``sid`` under the fake HOME, where sessions.review_session looks for it."""
    proj = HOME / '.claude' / 'projects' / '-work-billing'
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f'{sid}.jsonl'
    line = {'type': 'user', 'cwd': str(cwd), 'sessionId': sid, 'message': {'role': 'user', 'content': 'Review PR #4242'}}
    path.write_text(json.dumps(line) + '\n', encoding='utf-8')
    return path


@contextlib.contextmanager
def env(**values):
    """Set environment variables for the block (None removes one). A worker spawned inside the block inherits them."""
    old = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def wait_for(predicate, timeout: float = 20.0, interval: float = 0.1):
    """Poll ``predicate`` until it returns something truthy, and return that; AssertionError after ``timeout`` s."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f'timed out after {timeout}s waiting for {getattr(predicate, "__name__", predicate)}')
