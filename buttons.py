"""The quick buttons under the chat: each developer can set their own in ``chat-buttons.json`` in the data folder.

The file is a JSON object that names a list of buttons for each kind of page: ``finding`` (a code review's findings)
and ``diff`` (the diff review). A button is ``{"label": "Is it real?", "prompt": "Is this a real problem? ..."}``; a
click sends its prompt as the reviewer's message. A kind the file leaves out keeps the built-in buttons, and an empty
list shows none. An entry that is not valid is skipped and reported by ``load`` (and by ``doctor``), never raised: a
typo in the file must not take the chat down.
"""
from __future__ import annotations

import json

import paths

KINDS = ('finding', 'diff')
MAX_BUTTONS = 12
MAX_LABEL = 40
MAX_PROMPT = 2000

DEFAULTS: dict[str, list[dict[str, str]]] = {
    'finding': [
        {'label': 'Is it real?', 'prompt': 'Is this a real problem? Check it against the code.'},
        {'label': 'Draft the comment', 'prompt': 'Draft the PR comment for this finding. Follow my note if I wrote one.'},
        {'label': 'Smallest fix?', 'prompt': 'What is the smallest correct fix?'},
        {'label': 'Softer', 'prompt': 'Make the comment shorter and softer, same substance.'},
    ],
    'diff': [
        {'label': 'Explain', 'prompt': 'Explain what this code does and why it may have been written this way.'},
        {'label': 'Bugs?', 'prompt': 'Check these lines for bugs, edge cases and anything that does not match the code around them.'},
        {'label': 'Simpler?', 'prompt': 'Is there a simpler or more idiomatic way to write this? Show it briefly.'},
        {'label': 'Word the change', 'prompt': 'Word a change request for the coding session about these lines. Follow my note if I wrote one.'},
    ],
}


def load() -> tuple[dict[str, list[dict[str, str]]], list[str]]:
    """The buttons for each page kind, and a line for every problem found in the file (empty when it is fine or absent)."""
    result = {kind: [dict(b) for b in defaults] for kind, defaults in DEFAULTS.items()}
    problems: list[str] = []
    try:
        raw = paths.CHAT_BUTTONS.read_text(encoding='utf-8')
    except FileNotFoundError:
        return result, problems
    except OSError as exc:
        return result, [f'cannot read {paths.CHAT_BUTTONS}: {exc}']
    try:
        saved = json.loads(raw)
    except ValueError as exc:
        return result, [f'{paths.CHAT_BUTTONS} is not valid JSON: {exc}']
    if not isinstance(saved, dict):
        return result, [f'{paths.CHAT_BUTTONS} must be a JSON object with a list per page kind ({", ".join(KINDS)})']
    for kind in saved:
        if kind not in KINDS:
            problems.append(f'unknown page kind "{kind}" (use {" or ".join(KINDS)})')
    for kind in KINDS:
        if kind not in saved:
            continue
        entries = saved[kind]
        if not isinstance(entries, list):
            problems.append(f'"{kind}" must be a list of buttons')
            continue
        good: list[dict[str, str]] = []
        for i, entry in enumerate(entries, 1):
            label = entry.get('label') if isinstance(entry, dict) else None
            prompt = entry.get('prompt') if isinstance(entry, dict) else None
            if not (isinstance(label, str) and label.strip() and isinstance(prompt, str) and prompt.strip()):
                problems.append(f'"{kind}" button {i} needs a "label" and a "prompt", both text')
            elif len(label.strip()) > MAX_LABEL or len(prompt.strip()) > MAX_PROMPT:
                problems.append(f'"{kind}" button {i} is too long (label up to {MAX_LABEL}, prompt up to {MAX_PROMPT} characters)')
            elif len(good) >= MAX_BUTTONS:
                problems.append(f'"{kind}" has more than {MAX_BUTTONS} buttons: the rest are ignored')
                break
            else:
                good.append({'label': label.strip(), 'prompt': prompt.strip()})
        result[kind] = good
    return result, problems
