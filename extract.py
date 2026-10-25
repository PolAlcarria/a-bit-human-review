"""Read the findings out of a review with a headless, read-only `claude -p`, whatever the review's format.

The regex parser in parse.py only understands some markdown shapes (headings, bold labels, lists). This
asks Claude for the findings as JSON (`--json-schema`), so tables, prose and new review formats work too. Claude may
read the reviewed code, which lets it complete shortened paths and find the line of a finding that names a file or a
symbol but no line. It is told not to review anything itself.

It runs only on a review you asked the viewer for (``/findings``, or a prompt that names it), never on a guess.

The ``extract.enabled`` setting turns this off (the regex parser is used alone); ``extract.model`` picks the extraction
model (default ``sonnet``). See config.py.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import claude_cli
import config
from severity import LETTER, SEVERITIES

TIMEOUT_SECONDS = 300
MAX_REVIEW_CHARS = 60000
MAX_CHANGED_FILES = 300
MAX_LOCATIONS = 8
TOOLS = ['Read', 'Grep', 'Glob', 'Bash(git show:*)', 'Bash(git grep:*)', 'Bash(git ls-tree:*)', 'Bash(git log:*)',
         'Bash(git diff:*)']

SCHEMA = {
    'type': 'object',
    'properties': {
        'is_review': {'type': 'boolean'},
        'findings': {'type': 'array', 'items': {
            'type': 'object',
            'properties': {
                'id': {'type': ['string', 'null']},
                'severity': {'enum': list(SEVERITIES)},
                'title': {'type': 'string'},
                'body': {'type': 'string'},
                'recurring': {'type': 'boolean'},
                'locations': {'type': 'array', 'items': {
                    'type': 'object',
                    'properties': {'path': {'type': 'string'}, 'start': {'type': 'integer'},
                                   'end': {'type': 'integer'}, 'looked_up': {'type': 'boolean'}},
                    'required': ['path', 'start', 'looked_up'],
                }},
            },
            'required': ['id', 'severity', 'title', 'body', 'locations'],
        }},
    },
    'required': ['is_review', 'findings'],
}


SYSTEM_PROMPT = """You turn a code review, written by another Claude session, into a list of findings for a triage \
page. You do not review the code yourself: never add, merge, judge or reword findings.

What counts as a finding: each issue the reviewer raises about the code (a bug, a risk, a rule broken, an \
improvement) and each question the reviewer asks the author. Take every one, in the order written, whatever the \
format: headings, bold labels, numbered or bulleted lists, table rows, plain prose.
Not findings: summaries, verdicts, test results, "verified clean" lists, items marked already addressed, acknowledged \
or skipped, next steps, offers to post comments.
If the text is not a code review at all, set is_review to false and return no findings.

Fields:
- id: the label the reviewer gives the finding, without brackets or "#": "C1", "W2", "S3", or the number of a \
numbered item or table row ("3"). null when there is none.
- severity: critical (must-fix, blocking, critical, high), warning (should-fix, warning, major, medium), suggestion \
(nice-to-have, nit, minor, optional, low, suggestion), question (ask, question for the author), finding (no severity \
given). A heading or label sets the severity of every finding under it.
- title: the finding's headline, one line, as the reviewer wrote it, without the id, severity word or file path.
- body: the rest of the finding, copied word for word as markdown, code blocks included. For a table row, write the \
other cells as short paragraphs led by the column name in bold (for example "**Fix:** ..."). Add nothing of your own.
- recurring: true when the reviewer marks the finding as recurring or still open from an earlier review.
- locations: the code the finding points at, most relevant first, at most 8. path is relative to the repository \
root and must exist in the reviewed code; start and end are line numbers in the reviewed code (end = start for one \
line).
  - The review gives a file and line: keep the line, and complete a shortened path (lint.mjs → scripts/lint.mjs), \
preferring files the change touches. Do not open the file. looked_up = false.
  - The review names a file, function or symbol but no line: find the line in the reviewed code that the finding is \
about. looked_up = true.
  - The review names no code: no locations.

{where}
Run one command per Bash call: pipes, ";", "&&" and "$(...)" are blocked. Read code only to place a line the review \
does not give, and never to check whether the review is right: prefer `git grep -n` over reading whole files."""


def enabled() -> bool:
    return config.get('extract.enabled')


def where_text(root: Path, ref: str | None) -> str:
    if ref:
        return (f'The reviewed code is commit {ref[:12]} of the repository at {root} (your working directory). Your '
                f'working tree may be empty or on another branch, so read only through git: list files with '
                f'`git ls-tree -r --name-only {ref[:12]}`, read one with `git show {ref[:12]}:<path>`, search with '
                f'`git grep -n <pattern> {ref[:12]}`.')
    return (f'The reviewed code is the working tree of the repository at {root} (your working directory): use Read, '
            f'Grep and Glob.')


def build_prompt(text: str, request: str, repo: str, ref: str | None, changed: list[str]) -> str:
    parts = [f'Repository: {repo}', f"Reviewed code: {f'commit {ref}' if ref else 'the working tree'}"]
    if changed:
        shown = changed[:MAX_CHANGED_FILES]
        more = f'\n(and {len(changed) - len(shown)} more)' if len(changed) > len(shown) else ''
        parts.append('Files the change under review touches:\n' + '\n'.join(f'- {p}' for p in shown) + more)
    if request.strip():
        parts.append(f'The request that started the review:\n<request>\n{request.strip()[:4000]}\n</request>')
    parts.append(f'The review:\n<review>\n{text[:MAX_REVIEW_CHARS]}\n</review>')
    return '\n\n'.join(parts)


def ask(prompt: str, schema: dict, model: str, cwd: Path, timeout: int, extra: list[str], log) -> dict | None:
    """One headless `claude -p` call with a JSON schema: the result's `structured_output` (plus `_cost`), or None."""
    if not Path(claude_cli.binary()).exists():
        log('extract: claude CLI not found')
        return None
    args = ['--json-schema', json.dumps(schema), *extra, *(['--model', model] if model else [])]
    try:
        r = claude_cli.run_json(args, prompt, cwd, timeout)
    except subprocess.TimeoutExpired:
        log(f'extract: no answer after {timeout} s')
        return None
    except OSError as exc:
        log(f'extract: {exc!r}')
        return None
    try:
        out = json.loads(r.stdout)
    except ValueError:
        log(f'extract: exit {r.returncode}, no JSON; stderr: {r.stderr.strip()[-300:]}')
        return None
    data = out.get('structured_output')
    if out.get('is_error') or not isinstance(data, dict):
        log(f"extract: failed ({out.get('subtype')}): {str(out.get('result'))[:300]}")
        return None
    return {**data, '_cost': out.get('total_cost_usd')}


def run(text: str, root: Path, ref: str | None, changed: list[str], request: str = '', repo: str = '',
        log=print) -> dict | None:
    """{'is_review', 'findings', 'model', 'cost'} with findings in the viewer's format, or None when Claude failed."""
    model = config.get('extract.model')
    extra = ['--allowedTools', *TOOLS,
             '--append-system-prompt', SYSTEM_PROMPT.format(where=where_text(root, ref))]
    data = ask(build_prompt(text, request, repo or str(root), ref, changed), SCHEMA, model, root, TIMEOUT_SECONDS,
               extra, log)
    if data is None:
        return None
    return {'is_review': bool(data.get('is_review')), 'findings': to_findings(data.get('findings') or []),
            'model': model or 'default', 'cost': data['_cost']}


def to_findings(items: list[dict]) -> list[dict]:
    """Claude's JSON → the viewer's finding dicts, with unique ids that the server accepts."""
    findings, taken = [], set()
    for it in items:
        if not isinstance(it, dict) or not str(it.get('title') or '').strip():
            continue
        severity = it.get('severity') if it.get('severity') in SEVERITIES else 'finding'
        fid = re.sub(r'[^\w.-]', '', str(it.get('id') or ''))[:12]
        if fid.isdigit():
            fid = LETTER[severity] + fid
        if fid in taken:
            fid = ''
        anchors, looked_up = [], False
        for loc in (it.get('locations') or [])[:MAX_LOCATIONS]:
            path, start = str(loc.get('path') or '').strip(), int(loc.get('start') or 0)
            if not path or start < 1:
                continue
            end = max(int(loc.get('end') or start), start)
            looked_up = looked_up or bool(loc.get('looked_up'))
            anchors.append({'raw': f'{path}:{start}' + (f'-{end}' if end > start else ''), 'path': path,
                            'start': start, 'end': min(end, start + 400), 'pos': 0})
        findings.append({'id': fid or None, 'severity': severity, 'title': str(it['title']).strip()[:200],
                         'body': str(it.get('body') or '').strip(), 'anchors': anchors,
                         'recurring': bool(it.get('recurring')),
                         'meta': {'line': 'looked up by Claude'} if looked_up else {}})
        if fid:
            taken.add(fid)
    return findings
