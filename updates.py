"""Is a newer version of the plugin out? The SessionStart hook (``finding_viewer.py session``) asks, at most once a day.

The running version is ``version`` in this copy's .claude-plugin/plugin.json; the latest is the same file on GitHub's
main branch. When the latest is newer the hook prints one line (a ``systemMessage``, which Claude Code shows to the
user) saying how to update. It only notifies: Claude Code owns installing and updating plugins. The answer, or a
failed lookup, is cached in ``update_check.json`` in the data folder, so a session start costs one file read; the
lookup has a short timeout and any failure is silent. The ``update_check`` setting (config.py) turns it all off.
"""
from __future__ import annotations

import json
import time
import urllib.request
from typing import Callable

import config
import paths

LATEST_URL = 'https://raw.githubusercontent.com/PolAlcarria/a-bit-human-review/main/.claude-plugin/plugin.json'
CACHE = paths.DATA / 'update_check.json'
CACHE_SECONDS = 24 * 3600
RETRY_SECONDS = 3600  # after a failed lookup (offline, GitHub down): the next session start does not wait again
TIMEOUT = 3


def parse(version: object) -> tuple[int, ...] | None:
    """'0.6.0' as (0, 6, 0); None for anything that is not dotted whole numbers."""
    try:
        return tuple(int(part) for part in str(version).strip().removeprefix('v').split('.'))
    except ValueError:
        return None


def local_version() -> str | None:
    try:
        data = json.loads((paths.CODE / '.claude-plugin' / 'plugin.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data.get('version') if isinstance(data, dict) else None


def fetch_latest() -> str | None:
    """The version on GitHub, None when it cannot be read."""
    try:
        with urllib.request.urlopen(LATEST_URL, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except (OSError, ValueError):  # URLError and timeouts are OSErrors; HTTPException is not raised by urlopen here
        return None
    version = data.get('version') if isinstance(data, dict) else None
    return version if isinstance(version, str) and parse(version) else None


def latest_version(fetch: Callable[[], str | None] = fetch_latest, now: float | None = None) -> str | None:
    """The latest version, from the cache while it is fresh (a day, an hour after a failed lookup), else from
    ``fetch``. Never raises."""
    now = time.time() if now is None else now
    try:
        cached = json.loads(CACHE.read_text(encoding='utf-8'))
        if not isinstance(cached, dict):
            cached = {}
    except (OSError, ValueError):
        cached = {}
    age = now - cached.get('checked_at', 0) if isinstance(cached.get('checked_at'), (int, float)) else CACHE_SECONDS
    if age < (CACHE_SECONDS if cached.get('latest') else RETRY_SECONDS) and age >= 0:
        return cached.get('latest')
    latest = fetch()
    try:
        CACHE.write_text(json.dumps({'checked_at': now, 'latest': latest}), encoding='utf-8')
    except OSError:
        pass
    return latest or cached.get('latest')


def notice(fetch: Callable[[], str | None] = fetch_latest) -> str | None:
    """The line to show when a newer version is out, None when this one is current, the check is off or it failed."""
    if not config.get('update_check'):
        return None
    local = local_version()
    if not local or not parse(local):
        return None
    latest = latest_version(fetch)
    if not latest or parse(latest) <= parse(local):
        return None
    return (f'a-bit-human-review {latest} is available (you have {local}). Update it with /plugin, or turn on '
            f'auto-update for its marketplace there. Silence this with "update_check": false in {paths.CONFIG}.')


def cmd_session_start() -> int:
    """The SessionStart hook: prints the notice as a systemMessage when there is one."""
    text = notice()
    if text:
        print(json.dumps({'systemMessage': text}))
    return 0
