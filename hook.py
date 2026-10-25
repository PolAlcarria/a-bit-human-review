"""The Stop hook (``finding_viewer.py hook``): when the turn that just ended named the viewer, find the review in it
and start the renderer.

The transcript (Claude Code's JSONL) is read into turns. A turn that starts with a background task's notification
carries on the turn that launched the task (its prompt and hand-off), so a review run in the background opens its page
when it reports back. The findings come from a ReportFindings call, a saved report, or the answer's text (parse.py, or
Claude through the renderer), and the PR from the turn and the prompts before it.
The page is rendered in a detached process (``render.launch_render``), so the hook returns at once.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import config
import extract
import parse
import paths
import render
import store
import triage
from severity import SEVERITY_WORDS

ASKED_REVIEW_RE = re.compile(r'\breview', re.I)
# A prompt that names the viewer, which is the only way the Stop hook opens a page: /findings, the plugin's
# /a-bit-human-review:findings, "finding viewer", or the project's name. Not inside a path
# (~/.claude/commands/findings.md, ~/Repos/a-bit-human-review/…) and not finding_viewer.py.
MENTION_RE = re.compile(r'(?<![\w/.-])/(?:a-bit-human-review:)?findings\b|\bfindings?[\s-]viewer\b'
                        r'|(?<![\w/.-])a-bit-human-review\b', re.I)
# What Claude Code stores around a slash command and its shell output: the command's name and output never count as
# naming the viewer (the /findings command opens its own page); its arguments do.
COMMAND_TAG_RE = re.compile(r'<(command-name|command-message|local-command-[\w-]+)>.*?</\1>', re.S)
# Claude running /findings itself during the turn: the Skill tool, or the command's own script in a Bash call.
FINDINGS_SKILL_RE = re.compile(r'^(?:[\w.-]+:)?findings$')
FINDINGS_BASH_RE = re.compile(
    r'\bpython3?\s+(?:\'[^\']*|"[^"]*|\S*)finding_viewer\.py'
    r'(?![\'"]?\s+(?:hook|inject|serve|render|job|decisions|on|off|doctor|install|uninstall|migrate)\b)')
# A triage handed back from the review page: the hook's summary, a pasted "Copy triage", or the page telling the review
# session what it posted.
HANDOFF_RE = re.compile(r'Finding-viewer triage for "|Finding-viewer posted the review|^Triage of "', re.M)
SEVERITY_HINT_RE = re.compile(
    r'\b(?:critical|blocking|blockers?|must[\s-]fix|should[\s-]fix|nice[\s-]to[\s-]have|nits?|suggestions?|warnings?)\b', re.I)
# A tool call that runs in the background (a forked skill such as /code-review, an Agent run in the background): the
# result text a background launch starts with. Its task's notification comes in a later turn and names the call in
# <tool-use-id>.
BACKGROUND_RESULT_RE = re.compile(
    r'\s*(?:Skill "[^"]*" launched \([^)]*running in the background|Async agent launched)')
BACKGROUND_TOOLS = ('Skill', 'Agent', 'Task')
TOOL_USE_ID_RE = re.compile(r'<tool-use-id>\s*([\w-]+)\s*</tool-use-id>')


def report_path_re(glob: str) -> re.Pattern:
    """Finds a saved report's path in a review's text: ``glob`` (config report_glob, relative to the repository root),
    with an optional absolute folder (or ~ folder) before it. As in a glob, * and ? stay inside one folder name, **
    spans folders and **/ matches zero or more folders. Every run is bounded (a path of 400 characters, a name of
    255) and the absolute folder can only start where a path starts, so the default glob searches long text fast (it
    runs in the Stop hook); a glob with two `**` can still take seconds on a long unbroken run of path characters.
    The file must exist to be opened."""
    glob = re.sub(r'(?:\*\*/)+', '**/', glob)  # repeated **/ folders match the same as one
    char = r'[^\s`\'"()\[\]]'      # a character of a path in the text
    name = r'[^\s`\'"()\[\]/]'     # a character of one folder or file name
    run, seg = f'{char}{{0,400}}?', f'{name}{{0,255}}?'
    parts = {'**/': f'(?:{run}/)?', '**': run, '*': seg, '?': name}
    body = ''.join(parts.get(part, re.escape(part)) for part in re.split(r'(\*\*/|\*\*|\*|\?)', glob) if part)
    return re.compile(rf'((?:(?<!{char})~?/{run})?{body})')


REPORT_PATH_RE = report_path_re(config.get('report_glob'))
SHA_RE = re.compile(r'<!--\s*reviewed-sha:\s*([0-9a-f]{7,40})\s*-->')
PR_URL_RE = re.compile(r'github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)')
PR_SHORT_RE = re.compile(r'(?<![\w./-])([\w.-]+/[\w.-]+)#(\d+)\b')
PR_REF_RE = re.compile(r'\b(?:PR|pull request)\s*#(\d+)', re.I)
# owner/repo in the review's own commands: `gh ... --repo owner/repo`, `gh api repos/owner/repo/...`.
REPO_HINT_RE = re.compile(r'(?:--repo[= ]|-R |\brepos/)([\w.-]+/[\w.-]+?)(?:\.git)?(?=[/\s"\'`]|$)')


def block_text(content) -> str:
    """The text of a message's content: a string as it is, or the text blocks of a list joined by newlines."""
    if isinstance(content, str):
        return content
    return '\n'.join(b.get('text') or '' for b in (content if isinstance(content, list) else [])
                     if isinstance(b, dict) and b.get('type') == 'text')


def read_turns(path: str | Path) -> list[dict]:
    turns: list[dict] = []
    cur: dict | None = None
    try:
        fh = open(path, encoding='utf-8', errors='replace')
    except OSError:
        return turns
    with fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            kind = d.get('type')
            if kind == 'attachment' and cur is not None:
                # The inject hook's triage is stored right after the prompt it was added to.
                att = d.get('attachment') or {}
                if att.get('type') == 'hook_additional_context' and HANDOFF_RE.search(str(att.get('content') or '')):
                    cur['handoff'] = True
                continue
            if kind not in ('user', 'assistant') or d.get('isSidechain'):
                continue
            content = (d.get('message') or {}).get('content')
            if kind == 'user':
                origin = d.get('origin')
                origin = origin if isinstance(origin, dict) else {}
                if origin.get('kind') == 'task-notification' and not d.get('isMeta'):
                    # A background task reporting back: a turn of its own, which carries on the turn that launched the
                    # task (found by the tool call's id, else the turn before), so it keeps that turn's prompt and
                    # hand-off. Its texts and tools are its own.
                    m = TOOL_USE_ID_RE.search(block_text(content))
                    launch = len(turns) - 1
                    if m:
                        launch = next((i for i in range(len(turns) - 1, -1, -1)
                                       if any(t.get('id') == m.group(1) for t in turns[i]['tools'])), launch)
                    src = turns[launch] if launch >= 0 else {}
                    cur = {'prompt': src.get('prompt') or '', 'texts': [], 'tools': [], 'cwd': d.get('cwd'),
                           'session': d.get('sessionId'), 'handoff': bool(src.get('handoff')), 'background': []}
                    if launch >= 0:
                        cur['continues'] = launch
                    turns.append(cur)
                    continue
                if d.get('isMeta'):
                    # A message from another session starts a turn only when it is the page's Send button handing
                    # back a triage; other peer messages can arrive mid-turn and must not split it.
                    if origin.get('kind') == 'peer' and HANDOFF_RE.search(origin.get('body') or ''):
                        cur = {'prompt': origin['body'], 'texts': [], 'tools': [], 'cwd': d.get('cwd'),
                               'session': d.get('sessionId'), 'handoff': True, 'background': []}
                        turns.append(cur)
                    continue
                if isinstance(content, str):
                    prompt = content
                elif isinstance(content, list):
                    blocks = [b for b in content if isinstance(b, dict)]
                    if any(b.get('type') == 'tool_result' for b in blocks):
                        # A background launch's result starts with the launch line (BACKGROUND_RESULT_RE); the task's
                        # notification comes in a later turn (above), which finds the call on the turn that launched it.
                        names = {t.get('id'): t.get('name') for t in cur['tools']} if cur is not None else {}
                        for b in blocks:
                            tid = b.get('tool_use_id')
                            if b.get('type') != 'tool_result' or names.get(tid) not in BACKGROUND_TOOLS:
                                continue
                            if BACKGROUND_RESULT_RE.match(block_text(b.get('content'))):
                                bg = cur.setdefault('background', [])
                                if tid not in bg:
                                    bg.append(tid)
                        continue
                    prompt = '\n'.join(b.get('text', '') for b in blocks if b.get('type') == 'text')
                else:
                    continue
                cur = {'prompt': prompt, 'texts': [], 'tools': [], 'cwd': d.get('cwd'), 'session': d.get('sessionId'),
                       'handoff': bool(HANDOFF_RE.search(prompt)), 'background': []}
                turns.append(cur)
                continue
            if cur is None:
                cur = {'prompt': '', 'texts': [], 'tools': [], 'cwd': d.get('cwd'), 'session': d.get('sessionId'),
                       'background': []}
                turns.append(cur)
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get('type') == 'text' and b.get('text'):
                    cur['texts'].append(b['text'])
                elif b.get('type') == 'tool_use':
                    cur['tools'].append(b)
                    if b.get('name') in ('Agent', 'Task') and (b.get('input') or {}).get('run_in_background') is True:
                        bg = cur.setdefault('background', [])
                        if b.get('id') not in bg:
                            bg.append(b.get('id'))
    return turns


def find_pr(turns: list[dict], index: int) -> tuple[int | None, str | None, bool]:
    """The PR the turn is about, its owner/repo (from the turn or the 12 prompts before it), and whether a tool call in
    those turns fetched it. A PR nobody fetched may only be mentioned, such as the PR a stacked branch builds on."""
    turn = turns[index]
    window = range(index, max(-1, index - 12), -1)
    texts = [turn['prompt'], '\n'.join(turn['texts'])] + [turns[i]['prompt'] for i in window[1:]]
    number, repo = None, None
    for t in texts:
        m = next((m for m in [*PR_URL_RE.finditer(t or ''), *PR_SHORT_RE.finditer(t or '')]
                  if number is None or int(m.group(2)) == number), None)
        if m:
            number, repo = int(m.group(2)), m.group(1)
            break
        m = PR_REF_RE.search(t or '')
        if m and number is None:
            number = int(m.group(1))
    if number is None:
        return None, None, False
    if repo is None:
        hint = REPO_HINT_RE.search('\n'.join(json.dumps(t.get('input') or {}) for t in turn['tools']))
        repo = hint.group(1) if hint else None
    # `gh pr view|diff|checkout … N`, `repos/o/r/pulls/N`, `pull/N/head`, a PR URL, or an MCP tool's pull_number.
    fetched = re.compile(rf'\bgh\s+pr\s+\w+\b[^"|;&]{{0,80}}?(?<![\w#/.-]){number}\b|\bpulls?/{number}\b'
                         rf'|pull_?number\\?"?\s*:\s*\\?"?{number}\b', re.I)
    calls = '\n'.join(json.dumps(t.get('input') or {}) for i in window for t in turns[i]['tools'])
    return number, repo, bool(fetched.search(calls))


def review_intent(turn: dict, text: str, named: bool, turns: list[dict] | None = None) -> bool:
    """Is the answer finding-shaped (in any format), and was a review asked for? Naming the viewer counts as asking."""
    if len(text) < 400 or not (parse.find_anchors(text) or len(SEVERITY_HINT_RE.findall(text)) >= 2):
        return False
    return named or asks_review(turn, turns)


def names_viewer(turn: dict) -> bool:
    """The turn's prompt asks for the viewer by name. A handed-back triage mentions it too, but is not a request."""
    return not turn.get('handoff') and bool(MENTION_RE.search(COMMAND_TAG_RE.sub(' ', turn['prompt'] or '')))


def ran_findings(turn: dict) -> bool:
    """Claude ran /findings during the turn ("review it, then open with /findings"), so its page is already open."""
    for t in turn['tools']:
        inp = t.get('input') or {}
        if t.get('name') == 'Skill' and FINDINGS_SKILL_RE.match(str(inp.get('skill') or '').strip().lstrip('/')):
            return True
        if t.get('name') == 'Bash' and FINDINGS_BASH_RE.search(str(inp.get('command') or '')):
            return True
    return False


def launched_background(turn: dict) -> bool:
    """The turn started a task that runs in the background (a forked skill such as /code-review, or an Agent run in the
    background): its result comes in a later turn, with the task's notification. It does not check that the task is a
    review; the caller does (asks_review)."""
    return bool(turn.get('background'))


def asks_review(turn: dict, turns: list[dict] | None = None) -> bool:
    """A review was asked for: the prompt says "review", or a ReportFindings call or a skill with "review" in its name
    ran in the turn or in the turns it carries on from (``continues``, back to the launch of a background task)."""
    tools = list(turn['tools'])
    i = turn.get('continues') if turns is not None else None
    while i is not None:  # each link points to an earlier turn, so this ends
        tools += turns[i]['tools']
        i = turns[i].get('continues')
    return bool(ASKED_REVIEW_RE.search(turn['prompt'] or '')) or any(
        t.get('name') == 'ReportFindings' or (t.get('name') == 'Skill' and 'review' in str((t.get('input') or {}).get('skill', '')).lower())
        for t in tools)


def handed_back(turns: list[dict], index: int) -> bool:
    """The turn works on a triage handed back from the review page, or follows one ("yes, post them") with no new
    review asked for since. The comments it drafts or posts come from that triage, so they are not a new review."""
    for i in range(index, -1, -1):
        if turns[i].get('handoff'):
            return True
        if asks_review(turns[i], turns):
            return False
    return False


def spec_from_report(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None
    findings = parse.select_findings(parse.parse_findings(text))
    if not findings and not extract.enabled():
        return None
    sha = SHA_RE.search(text[:400])
    title = next((parse.plain(ln[2:]) for ln in text.splitlines() if ln.startswith('# ')), path.stem)
    return {'source': 'report', 'title': title, 'subtitle': f'Saved review report · {path.name}',
            'cwd': str(path.parent), 'report_path': str(path), 'reviewed_sha': sha.group(1) if sha else None,
            'findings': findings, 'text': text}


def spec_from_tool(tool: dict, cwd: str) -> dict | None:
    items = (tool.get('input') or {}).get('findings') or []
    findings = []
    for i, it in enumerate(items, 1):
        line = int(it.get('line') or 0)
        anchors = [{'raw': f"{it.get('file')}:{line}", 'path': it.get('file', ''), 'start': max(line, 1),
                    'end': max(line, 1), 'pos': 0}] if it.get('file') else []
        body = it.get('summary', '')
        if it.get('failure_scenario'):
            body += f"\n\n**Failure scenario:** {it['failure_scenario']}"
        meta = {k: it[k] for k in ('category', 'verdict', 'outcome') if it.get(k)}
        title = it.get('short_summary') or it.get('summary', '')
        # A review that classifies its findings says so in the category ("must-fix") or as a headline prefix
        # ("Must-fix — …"); without either the finding stays unclassified.
        category = re.sub(r'[\s_-]+', ' ', str(it.get('category') or '').lower()).strip()
        severity = SEVERITY_WORDS.get(category) or SEVERITY_WORDS.get(category.replace(' ', '-'))
        if severity:
            meta.pop('category', None)
        _, prefixed, rest = parse.headline(title)
        if prefixed:
            severity, title = severity or prefixed, rest
        findings.append({'id': None, 'severity': severity, 'title': title,
                         'body': body, 'anchors': anchors, 'recurring': False, 'meta': meta})
    findings = parse.assign_ids(findings)
    if not findings:
        return None
    return {'source': 'tool', 'title': 'Code review findings', 'subtitle': 'Reported with ReportFindings',
            'cwd': cwd, 'findings': findings}


def spec_from_turn(turns: list[dict], index: int, cwd: str, extra_text: str = '', named: bool = False) -> dict | None:
    if handed_back(turns, index):
        return None
    turn = turns[index]
    text = '\n\n'.join(turn['texts'])
    if extra_text and extra_text.strip() and extra_text.strip()[:200] not in text:
        text = f'{text}\n\n{extra_text}'
    cwd = turn.get('cwd') or cwd
    root = render.repo_root(cwd) or Path(cwd)

    tools = [t for t in turn['tools'] if t.get('name') == 'ReportFindings']
    if tools:
        spec = spec_from_tool(tools[-1], cwd)
        if spec:
            return spec
    for m in reversed(list(REPORT_PATH_RE.finditer(text))):
        p = Path(m.group(1)).expanduser()
        for cand in ([p] if p.is_absolute() else [root / p, Path(cwd) / p]):
            if cand.is_file():
                spec = spec_from_report(cand)
                if spec:
                    return spec
    if not text.strip():
        return None
    findings = parse.select_findings(parse.parse_findings(text))
    intent = review_intent(turn, text, named, turns) if extract.enabled() and not findings else False
    if not findings and not intent:
        return None
    pr, repo, fetched = find_pr(turns, index)
    branch = (store.git(root, 'rev-parse', '--abbrev-ref', 'HEAD') or '').strip()
    mentioned = None
    if pr and not fetched and own_branch(root, branch) and (not repo or repo.lower() in render.remote_slugs(root)):
        # Named but never fetched, while the session sits on a branch of its own: the review is most likely of that
        # branch. render.build_page asks gh for the branch's PR instead.
        mentioned, pr = pr, None
    when = dt.datetime.now().strftime('%Y-%m-%d %H:%M')
    where = f"{repo.split('/')[-1]} PR #{pr}" if pr and repo else f'PR #{pr}' if pr else branch or root.name
    return {'source': 'chat', 'title': f'Code review · {where}',
            'subtitle': f'Review from the chat · session {str(turn.get("session") or "")[:8]} · {when}',
            'cwd': cwd, 'pr': pr, 'mentioned_pr': mentioned, 'repo': repo, 'findings': findings, 'text': text,
            'request': turn['prompt'] or ''}


def own_branch(root: Path, branch: str) -> bool:
    """A branch other than the default one, so likely the work under review (not a checkout to read other PRs from)."""
    default = (store.default_base(root) or '').removeprefix('refs/remotes/').split('/', 1)[-1]
    return branch not in ('', 'HEAD', default, 'develop', 'main', 'master')


def fingerprint(spec: dict) -> str:
    if needs_claude(spec):
        return hashlib.sha1(spec['text'].encode()).hexdigest()
    key = [(f['title'], [(a['path'], a['start']) for a in f.get('anchors', [])]) for f in spec['findings']]
    return hashlib.sha1(json.dumps(key).encode()).hexdigest()


def needs_claude(spec: dict) -> bool:
    return bool(spec.get('text')) and extract.enabled()


def already_shown(session: str, fp: str) -> bool:
    try:
        state = json.loads(paths.STATE.read_text())
    except (OSError, ValueError):
        state = {}
    seen = state.setdefault(session, [])
    if fp in seen:
        return True
    seen.append(fp)
    state[session] = seen[-20:]
    if len(state) > 50:
        state = dict(list(state.items())[-50:])
    paths.STATE.write_text(json.dumps(state))
    return False


def cmd_hook() -> int:
    """Stop hook: build the page for the turn that just ended, only when its prompt named the viewer, and open it unless
    auto_open is off."""
    if os.environ.get('FINDING_VIEWER_CHILD'):
        return 0
    try:
        event = json.loads(sys.stdin.read() or '{}')
    except ValueError:
        return 0
    transcript, cwd = event.get('transcript_path'), event.get('cwd') or os.getcwd()
    if not transcript:
        return 0
    try:
        # Runs after every turn of every session: a transcript that never names the viewer is not parsed at all.
        if not MENTION_RE.search(Path(transcript).read_text(encoding='utf-8', errors='replace')):
            return 0
    except OSError:
        return 0
    turns = read_turns(transcript)
    # A turn that ran /findings has its page already; a second one would read the review again, at a second cost.
    if not turns or not names_viewer(turns[-1]) or ran_findings(turns[-1]):
        return 0
    for _ in range(3):
        if turns[-1]['texts'] or event.get('last_assistant_message'):
            break
        time.sleep(0.4)
        turns = read_turns(transcript)
    spec = spec_from_turn(turns, len(turns) - 1, cwd, extra_text=event.get('last_assistant_message') or '', named=True)
    # auto_open off: the page is still built and its address given, but the browser is left alone
    # (/findings builds and opens a fresh copy).
    opens = config.get('auto_open')
    if not spec:
        if launched_background(turns[-1]) and asks_review(turns[-1], turns):
            msg = (f'Finding viewer: the review runs in the background; its page {"opens" if opens else "is ready"} '
                   'when it reports back.')
        elif turns[-1].get('continues') is not None:
            # A background task reported back without a review (another task, or the same one again): its launch
            # already said where the page opens.
            return 0
        else:
            msg = ('Finding viewer: no code review in this turn to open. '
                   '/findings opens the latest one in this session.')
        print(json.dumps({'systemMessage': msg}))
        return 0
    if already_shown(event.get('session_id', ''), fingerprint(spec)):
        return 0
    spec['session_id'] = event.get('session_id')
    spec['open'] = opens
    out = render.launch_render(spec)
    if needs_claude(spec):
        msg = (f'Finding viewer: Claude is reading the review; the page {"opens" if opens else "is ready"} in about a '
               f'minute → {triage.page_url(out)}')
    else:
        n = len(spec['findings'])
        msg = f'Finding viewer: {n} finding{"s" * (n != 1)} to triage → {triage.page_url(out)}'
    print(json.dumps({'systemMessage': msg}))
    return 0
