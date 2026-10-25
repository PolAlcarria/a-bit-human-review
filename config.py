"""The tool's settings: DATA/config.json, each key overridable by its FINDING_VIEWER_* environment variable.

Every key defaults to the tool's behaviour without a config file, so the file is optional and holds only the keys a
user changes. Nested keys are written nested: {"extract": {"enabled": false}}. An unset or empty variable does not
override. A value of the wrong type, in the file or a variable, is ignored with a problem line that
`finding_viewer.py doctor` prints. The file is read on every get(), so a change applies to the next hook, job or page
request; port is read once, when the server and the hooks start.

Not settings: FINDING_VIEWER_DATA says where DATA is (paths.py), FINDING_VIEWER_CHILD marks a child claude process.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Mapping

import paths

DEFAULTS: dict[str, Any] = {
    'port': 47321,
    'repo_dirs': [],
    'extract.enabled': True,
    'extract.model': 'sonnet',
    'draft_model': '',
    'rereview_model': '',
    'chat_model': '',
    'report_glob': '.claude/.history/code-reviews/*/*.md',
    'default_base': '',
    'delete_forks': True,
    'auto_open': True,
}
ENV = {
    'port': 'FINDING_VIEWER_PORT',
    'repo_dirs': 'FINDING_VIEWER_REPO_DIRS',
    'extract.enabled': 'FINDING_VIEWER_EXTRACT',
    'extract.model': 'FINDING_VIEWER_EXTRACT_MODEL',
    'draft_model': 'FINDING_VIEWER_DRAFT_MODEL',
    'rereview_model': 'FINDING_VIEWER_REREVIEW_MODEL',
    'chat_model': 'FINDING_VIEWER_CHAT_MODEL',
    'report_glob': 'FINDING_VIEWER_REPORT_GLOB',
    'default_base': 'FINDING_VIEWER_DEFAULT_BASE',
    'delete_forks': 'FINDING_VIEWER_DELETE_FORKS',
    'auto_open': 'FINDING_VIEWER_AUTO_OPEN',
}
FALSE_WORDS = ('0', 'false', 'no', 'off')


def load(path: Path | None = None, environ: Mapping[str, str] | None = None) -> tuple[dict, dict, list[str]]:
    """(values, sources, problems): every key's effective value; where it came from ('default', the file's path or the
    variable's name); and one line for each value that was ignored. Never raises: the hooks read it."""
    path = paths.CONFIG if path is None else path
    environ = os.environ if environ is None else environ
    values, sources, problems = copy.deepcopy(DEFAULTS), dict.fromkeys(DEFAULTS, 'default'), []
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        raw = {}
    except (OSError, ValueError, RecursionError) as exc:
        problems.append(f'{path} not read, so every key has its default: {exc}')
        raw = {}
    if not isinstance(raw, dict):
        problems.append(f'{path} is not a JSON object, so every key has its default')
        raw = {}
    for key, value in _flatten(raw).items():
        if key not in DEFAULTS:
            problems.append(f'{path}: unknown key {key}')
        elif not _valid(key, value):
            problems.append(f'{path}: {key} = {value!r} ignored, {_expected(key)}')
        else:
            values[key], sources[key] = value, str(path)
    for key, var in ENV.items():
        text = environ.get(var)
        if not text:
            continue
        value = _parse(key, text)
        if value is None or not _valid(key, value):
            problems.append(f'{var}={text!r} ignored, {_expected(key)}')
        else:
            values[key], sources[key] = value, var
    return values, sources, problems


def get(key: str) -> Any:
    """One key's effective value (see load)."""
    return load()[0][key]


def _flatten(raw: dict) -> dict:
    """The file's keys as DEFAULTS names them: {"extract": {"enabled": false}} gives 'extract.enabled'."""
    flat = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            flat.update({f'{key}.{sub}': v for sub, v in value.items()})
        else:
            flat[key] = value
    return flat


def _parse(key: str, text: str) -> Any:
    """A variable's text as the key's type, None when it is not one. A list is os.pathsep-separated."""
    default = DEFAULTS[key]
    if isinstance(default, bool):
        return text.strip().lower() not in FALSE_WORDS
    if isinstance(default, int):
        try:
            return int(text)
        except ValueError:
            return None
    if isinstance(default, list):
        return [part for part in text.split(os.pathsep) if part]
    return text


def _valid(key: str, value: Any) -> bool:
    default = DEFAULTS[key]
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool) and 0 < value < 65536
    if isinstance(default, list):
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    if key == 'report_glob':
        return isinstance(value, str) and bool(value) and not value.startswith(('/', '~'))
    return isinstance(value, str)


def _expected(key: str) -> str:
    """What a key takes, for a problem line."""
    default = DEFAULTS[key]
    if isinstance(default, bool):
        return 'expected true or false'
    if isinstance(default, int):
        return 'expected a port number'
    if isinstance(default, list):
        return 'expected a list of folders'
    if key == 'report_glob':
        return 'expected a glob relative to the repository root'
    return 'expected a string'
