"""The regex parser for markdown reviews: findings from headings, bold labels and list items, with their severity,
their ids and their file:line anchors. Claude reads the formats it cannot (extract.py).
"""
from __future__ import annotations

import re
import textwrap

from severity import LETTER, LETTER_SEVERITY, SEVERITY_WORDS

MAX_LOCATIONS = 8

_SEV_ALT = '|'.join(sorted((re.escape(w).replace(r'\ ', r'[\s-]') for w in SEVERITY_WORDS), key=len, reverse=True))
LABEL_RE = re.compile(rf'^[^\w\[]*({_SEV_ALT})(?:\s+(?:issues?|findings?|severity|priority))?\s*(?:\([^)]*\)|\d+)?\s*:?\s*$', re.I)
PREFIX_RE = re.compile(
    r'^(critical|blocking|blocker|must[\s-]?fix|warning|should[\s-]?fix|suggestion|nice[\s-]to[\s-]have|nit|question)'
    r'\s*(?:\(\w+\))?\s*[—–:-]+\s*(.+)$', re.I)
BRACKET_ID_RE = re.compile(r'^\[([A-Z]{1,2})(\d{1,3})\]\s*[—–:.-]?\s*(.+)$')
BARE_ID_RE = re.compile(r'^([CWSQ])(\d{1,3})(?:[.:)]|\s+[—–-])\s+(.+)$')
RECURRING_RE = re.compile(r'\*\*\[Recurring\]\*\*\s*', re.I)

FENCE_RE = re.compile(r'^\s*(```|~~~)')
HEADING_RE = re.compile(r'^(#{1,6})\s+(.*?)\s*#*\s*$')
HR_RE = re.compile(r'^(?:-{3,}|\*{3,}|_{3,})$')
LIST_RE = re.compile(r'^(\s*)(?:[-*+]|\d+[.)])\s+(.*)$')
BOLD_RE = re.compile(r'^\*\*(.+?)\*\*(.*)$')

ANCHOR_RE = re.compile(
    r'(?<![\w/.:@+-])((?:[\w.@+-]+/)*[\w@+-][\w.@+-]*\.[A-Za-z][A-Za-z0-9]{0,7})(?::|#L)(\d+)(?:\s*[-–]\s*L?(\d+))?(?!\d)')
GITHUB_BLOB_RE = re.compile(r'github\.com/[^/\s]+/[^/\s]+/blob/[^/\s]+/([^\s#)]+)#L(\d+)(?:-L(\d+))?')


def plain(s: str) -> str:
    s = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', s)
    s = s.replace('**', '').replace('__', '').replace('`', '')
    return re.sub(r'\s+', ' ', s).strip()


def find_anchors(text: str) -> list[dict]:
    anchors, seen = [], set()
    for rx in (GITHUB_BLOB_RE, ANCHOR_RE):
        for m in rx.finditer(text):
            start = int(m.group(2))
            end = int(m.group(3)) if m.group(3) else start
            key = (m.group(1), start, end)
            if start > 0 and key not in seen and end >= start:
                seen.add(key)
                anchors.append({'raw': m.group(0) if rx is ANCHOR_RE else f'{m.group(1)}:{start}',
                                'path': m.group(1), 'start': start, 'end': min(end, start + 400), 'pos': m.start()})
    anchors.sort(key=lambda a: a['pos'])
    return anchors


def label_severity(text: str) -> str | None:
    m = LABEL_RE.match(plain(text))
    if not m:
        return None
    return SEVERITY_WORDS.get(re.sub(r'\s+', ' ', m.group(1).lower()).replace(' ', '-'),
                              SEVERITY_WORDS.get(m.group(1).lower()))


def headline(text: str) -> tuple[str | None, str | None, str]:
    t = plain(text)
    m = BRACKET_ID_RE.match(t) or BARE_ID_RE.match(t)
    if m:
        letter = m.group(1)
        return f'{letter}{m.group(2)}', LETTER_SEVERITY.get(letter[0]), m.group(3).strip(' :—–-')
    m = PREFIX_RE.match(t)
    if m:
        word = re.sub(r'[\s-]+', '-', m.group(1).lower())
        return None, SEVERITY_WORDS.get(word, SEVERITY_WORDS.get(word.replace('-', ' '))), m.group(2).strip(' :')
    return None, None, t.strip(' :')


def is_short_label(bold_text: str) -> bool:
    t = plain(bold_text)
    return t.endswith(':') and len(t.split()) <= 3


def parse_findings(text: str) -> list[dict]:
    """Split a markdown review into findings, tracking severity from headings and bold labels."""
    found: list[dict] = []
    cur: dict | None = None
    ctx: str | None = None
    fence = False
    prev_blank = False

    def close() -> None:
        nonlocal cur
        if cur:
            found.append(cur)
        cur = None

    def start(title, severity, fid, indent, kind, strong, head, recurring=False) -> None:
        nonlocal cur
        close()
        cur = {'title': title, 'severity': severity, 'id': fid, 'indent': indent, 'kind': kind,
               'strong': strong, 'head': head, 'lines': [], 'recurring': recurring}

    for raw in text.split('\n'):
        if FENCE_RE.match(raw):
            fence = not fence
            if cur:
                cur['lines'].append(raw)
            continue
        if fence:
            if cur:
                cur['lines'].append(raw)
            continue
        stripped = raw.strip()
        blank = not stripped
        if blank:
            if cur:
                cur['lines'].append(raw)
            prev_blank = True
            continue
        if HR_RE.match(stripped):
            close()
            ctx = None
            prev_blank = False
            continue
        h = HEADING_RE.match(raw)
        if h:
            close()
            sev = label_severity(h.group(2))
            if sev:
                ctx = sev
            else:
                fid, sev2, title = headline(h.group(2))
                if fid or sev2 or find_anchors(h.group(2)):
                    start(title, sev2 or ctx, fid, -1, 'heading', True, h.group(2))
                else:
                    ctx = None
            prev_blank = False
            continue

        m = LIST_RE.match(raw)
        indent = len(raw) - len(raw.lstrip(' \t'))
        content = m.group(2) if m else stripped
        is_item = bool(m)
        kind = 'item' if is_item else 'para'

        if cur and (cur['kind'] == 'heading' or indent > cur['indent']):
            cur['lines'].append(raw)
            prev_blank = False
            continue

        recurring = bool(RECURRING_RE.match(content))
        if recurring:
            content = RECURRING_RE.sub('', content, count=1)
        bold = BOLD_RE.match(content)
        if bold:
            btext, rest = bold.group(1), bold.group(2)
            sev = label_severity(btext)
            if sev:
                close()
                ctx = sev
                if find_anchors(rest):
                    start(plain(rest).strip(' :—–-'), sev, None, indent, kind, True, rest)
            elif is_short_label(btext):
                if cur:
                    cur['lines'].append(raw)
            else:
                fid, sev2, title = headline(btext)
                start(title, sev2 or ctx, fid, indent, kind, True, content, recurring)
                cur['rest'] = rest.strip(' —–-')
        elif is_item:
            if cur and cur['kind'] == 'para':
                cur['lines'].append(raw)
            elif ctx:
                start(plain(content), ctx, None, indent, 'item', False, content)
            else:
                close()
        elif cur and cur['kind'] == 'item' and indent == 0 and prev_blank:
            close()
        elif cur:
            cur['lines'].append(raw)
        prev_blank = False
    close()

    for f in found:
        f['anchors'] = find_anchors(f['head'] + '\n' + '\n'.join(f['lines']))[:MAX_LOCATIONS * 2]
        rest = f.pop('rest', '')
        if not re.sub(r'[\W_]+', '', ANCHOR_RE.sub('', rest)):
            rest = ''
        f['body'] = (rest + '\n\n' + textwrap.dedent('\n'.join(f['lines'])).strip()).strip()
        f['title'] = (f['title'] or 'Untitled finding')[:200]
        for k in ('lines', 'head', 'indent', 'kind'):
            f.pop(k)
    return found


def select_findings(findings: list[dict]) -> list[dict]:
    """The findings that carry a severity, else the bold anchored ones; none unless at least one has an anchor."""
    has_severity = any(f['severity'] for f in findings)
    keep = [f for f in findings if f['severity']] if has_severity else [f for f in findings if f['strong'] and f['anchors']]
    return assign_ids(keep) if any(f['anchors'] for f in keep) else []


def assign_ids(findings: list[dict]) -> list[dict]:
    taken = {f['id'] for f in findings if f.get('id')}
    counters: dict[str, int] = {}
    for f in findings:
        f['severity'] = f.get('severity') or 'finding'
        if not f.get('id'):
            letter = LETTER[f['severity']]
            while True:
                counters[letter] = counters.get(letter, 0) + 1
                fid = f'{letter}{counters[letter]}'
                if fid not in taken:
                    break
            f['id'] = fid
            taken.add(fid)
        f.pop('strong', None)
    return findings
