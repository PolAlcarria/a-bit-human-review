"""The Stop hook end to end on synthetic transcripts: a review that runs in the background opens one page when it
reports back (unless auto_open is off), and /findings with no argument finds it too."""
import support  # noqa: F401  first: points the tool at a scratch environment before it is imported
import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

import finding_viewer
import hook


def user(content, **extra):
    return {'type': 'user', 'message': {'role': 'user', 'content': content}, **extra}


def assistant(*blocks):
    return {'type': 'assistant', 'message': {'content': list(blocks)}}


def text(value):
    return {'type': 'text', 'text': value}


def tool_use(tid, name, **inp):
    return {'type': 'tool_use', 'id': tid, 'name': name, 'input': inp}


def tool_result(tid, content):
    return user([{'type': 'tool_result', 'tool_use_id': tid, 'content': content}])


def notification(tid):
    body = (f'<task-notification>\n<task-id>bg-1</task-id>\n<tool-use-id>{tid}</tool-use-id>\n'
            '<status>completed</status>\n'
            '<summary>Review done</summary>\n<result>The review is done.</result>\n</task-notification>')
    return user(body, origin={'kind': 'task-notification', 'producer': 'session-task'})


def write_jsonl(name: str, lines: list) -> Path:
    """A transcript in the scratch folder: one JSON object per line."""
    path = support.SCRATCH / name
    path.write_text('\n'.join(json.dumps(x) for x in lines) + '\n', encoding='utf-8')
    return path


# A ReportFindings call, which the hook turns into findings with anchors (hook.spec_from_tool).
REPORT = tool_use('toolu_rf1', 'ReportFindings', findings=[
    {'file': 'src/app/billing.py', 'line': 42, 'summary': 'Total is not rounded',
     'failure_scenario': 'A 0.1 + 0.2 total'},
    {'file': 'src/app/billing.py', 'line': 10, 'summary': 'Loop re-reads the rate',
     'failure_scenario': 'Slow on 1e6 rows'}])

# The turn that launches /code-review in the background, and the turn its task's notification starts.
LAUNCH = [
    user('Review my branch with /code-review and open it in the finding viewer'),
    assistant(tool_use('toolu_bg1', 'Skill', skill='code-review')),
    tool_result('toolu_bg1', 'Skill "code-review" launched (forked execution, running in the background).'),
    assistant(text('The review runs in the background.')),
]
NOTIFIED = [
    notification('toolu_bg1'),
    assistant(REPORT),
    tool_result('toolu_rf1', 'ok'),
    assistant(text('Two findings, reported above.')),
]


def run_hook(path, session):
    """Runs the Stop hook on the transcript at ``path`` for ``session``; returns (the systemMessage it printed or None,
    the mock standing in for render.launch_render)."""
    event = {'transcript_path': str(path), 'cwd': str(support.WORK), 'session_id': session,
             'last_assistant_message': 'Done.'}
    out = io.StringIO()
    page = support.OUT / f'{session}-page' / 'page.html'
    with support.env(FINDING_VIEWER_CHILD=None), mock.patch('hook.render.launch_render', return_value=page) as launch, \
            mock.patch('sys.stdin', io.StringIO(json.dumps(event))), contextlib.redirect_stdout(out):
        hook.cmd_hook()
    text_out = out.getvalue().strip()
    return (json.loads(text_out)['systemMessage'] if text_out else None), launch


class BackgroundReviewHookTest(unittest.TestCase):
    def test_launch_then_notification_opens_one_page_with_the_findings(self):
        message, launch = run_hook(write_jsonl('bg_a.jsonl', LAUNCH + NOTIFIED), 'sess-bg-a')
        spec = launch.call_args[0][0]
        self.assertIn('2 findings to triage', message or '')
        self.assertEqual((launch.call_count, spec['source'], len(spec['findings'])), (1, 'tool', 2))

    def test_background_review_whose_launch_prompt_does_not_name_the_viewer_opens_nothing(self):
        earlier = [user('open the last review in the finding viewer'), assistant(text('Nothing to open yet.'))]
        launch = [user('Review my branch with /code-review')] + LAUNCH[1:]
        message, render = run_hook(write_jsonl('bg_b.jsonl', earlier + launch + NOTIFIED), 'sess-bg-b')
        self.assertEqual((message, render.call_count), (None, 0))

    def test_the_same_notification_twice_opens_one_page(self):
        path = write_jsonl('bg_c.jsonl', LAUNCH + NOTIFIED)
        _, first = run_hook(path, 'sess-bg-c')
        write_jsonl('bg_c.jsonl', LAUNCH + NOTIFIED + NOTIFIED)  # the same notification once more, in the same file
        second_message, second = run_hook(path, 'sess-bg-c')
        self.assertEqual((first.call_count, second.call_count, second_message), (1, 0, None))

    def test_launch_alone_says_the_page_opens_when_the_review_reports_back(self):
        message, render = run_hook(write_jsonl('bg_d.jsonl', LAUNCH), 'sess-bg-d')
        self.assertEqual((message, render.call_count),
                         ('Finding viewer: the review runs in the background; its page opens when it reports back.', 0))

    def test_plain_turn_naming_the_viewer_without_a_review_still_says_so(self):
        path = write_jsonl('plain_e.jsonl', [user('open it in the finding viewer'),
                                             assistant(text('There is no review here.'))])
        message, render = run_hook(path, 'sess-plain-e')
        self.assertIsNotNone(message)
        self.assertTrue(message.startswith('Finding viewer: no code review in this turn to open.'))
        render.assert_not_called()

    def test_background_launch_then_a_reply_without_a_review_prints_nothing(self):
        path = write_jsonl('bg_i.jsonl', LAUNCH + [notification('toolu_bg1'), assistant(text('Nothing to report.'))])
        message, render = run_hook(path, 'sess-bg-i')
        self.assertEqual((message, render.call_count), (None, 0))

    def test_background_agent_in_a_handed_back_turn_opens_nothing(self):
        handoff = user('Finding-viewer triage for "x" (PR #7): 1 to post. open it in the finding viewer')
        path = write_jsonl('bg_ii.jsonl', [
            handoff,
            assistant(tool_use('toolu_ag1', 'Agent', prompt='check the diff', run_in_background=True)),
            tool_result('toolu_ag1', 'Async agent launched successfully. You will be notified when it is done.'),
            notification('toolu_ag1'),
            assistant(REPORT),
            tool_result('toolu_rf1', 'ok'),
            assistant(text('Two findings, reported above.')),
        ])
        message, render = run_hook(path, 'sess-bg-ii')
        self.assertEqual((message, render.call_count), (None, 0))

    def test_background_agent_for_a_question_says_no_review_is_in_the_turn(self):
        path = write_jsonl('bg_vi.jsonl', [
            user('explore how state is stored and open the finding viewer'),
            assistant(tool_use('toolu_ag1', 'Agent', prompt='read the store', run_in_background=True)),
            tool_result('toolu_ag1', 'Async agent launched successfully. You will be notified when it is done.'),
            assistant(text('The agent reads the store in the background.')),
        ])
        message, render = run_hook(path, 'sess-bg-vi')
        self.assertEqual(
            (message, render.call_count),
            ('Finding viewer: no code review in this turn to open. /findings opens the latest one in this session.', 0))


class FindingsCommandTest(unittest.TestCase):
    def test_findings_with_no_argument_finds_the_background_review(self):
        transcript = write_jsonl('bg_f.jsonl', LAUNCH + NOTIFIED)
        out = io.StringIO()
        with mock.patch('finding_viewer.sessions.find_transcript', return_value=transcript), \
                mock.patch('finding_viewer.render_and_open', return_value=support.OUT / 'x' / 'page.html') as opened, \
                mock.patch('finding_viewer.os.getcwd', return_value=str(support.WORK)), \
                contextlib.redirect_stdout(out):
            finding_viewer.cmd_open([])
        self.assertEqual(opened.call_count, 1)
        spec = opened.call_args[0][0]
        self.assertEqual((spec['source'], len(spec['findings'])), ('tool', 2))
        self.assertTrue(out.getvalue().startswith('Opened 2 findings to triage'))


class AutoOpenTest(unittest.TestCase):
    """auto_open off: the hook still builds the page but marks it not to open and says so; /findings always opens."""

    def test_hook_marks_the_page_to_open_by_default(self):
        _, launch = run_hook(write_jsonl('ao_a.jsonl', LAUNCH + NOTIFIED), 'sess-ao-a')
        self.assertIs(launch.call_args[0][0]['open'], True)

    def test_auto_open_off_builds_the_page_but_marks_it_not_to_open(self):
        with support.env(FINDING_VIEWER_AUTO_OPEN='0'):
            message, launch = run_hook(write_jsonl('ao_b.jsonl', LAUNCH + NOTIFIED), 'sess-ao-b')
        self.assertEqual((launch.call_count, launch.call_args[0][0]['open']), (1, False))
        self.assertIn('2 findings to triage', message or '')

    def test_auto_open_off_launch_alone_says_the_page_is_ready_when_the_review_reports_back(self):
        with support.env(FINDING_VIEWER_AUTO_OPEN='0'):
            message, render = run_hook(write_jsonl('ao_c.jsonl', LAUNCH), 'sess-ao-c')
        self.assertEqual(
            (message, render.call_count),
            ('Finding viewer: the review runs in the background; its page is ready when it reports back.', 0))

    def test_render_and_open_starts_the_server_always_and_the_browser_only_for_a_page_marked_to_open(self):
        page = support.OUT / 'ao' / 'page.html'
        for spec, opens in (({'open': False}, 0), ({'open': True}, 1), ({}, 1)):
            with self.subTest(spec=spec), mock.patch('finding_viewer.render.render', return_value=page), \
                    mock.patch('finding_viewer.triage.ensure_server', return_value=True) as server, \
                    mock.patch('finding_viewer.open_url') as opened:
                finding_viewer.render_and_open(spec)
                self.assertEqual((server.call_count, opened.call_count), (1, opens))
                if opens:
                    self.assertEqual(opened.call_args[0][0], finding_viewer.triage.page_url(page))

    def test_render_and_open_opens_nothing_when_there_was_nothing_to_render(self):
        with mock.patch('finding_viewer.render.render', return_value=None), \
                mock.patch('finding_viewer.triage.ensure_server') as server, \
                mock.patch('finding_viewer.open_url') as opened:
            self.assertIsNone(finding_viewer.render_and_open({'open': True}))
        self.assertEqual((server.call_count, opened.call_count), (0, 0))

    def test_findings_marks_its_page_to_open_whatever_auto_open_says(self):
        transcript = write_jsonl('ao_d.jsonl', LAUNCH + NOTIFIED)
        with support.env(FINDING_VIEWER_AUTO_OPEN='0'), \
                mock.patch('finding_viewer.sessions.find_transcript', return_value=transcript), \
                mock.patch('finding_viewer.render_and_open', return_value=support.OUT / 'x' / 'page.html') as opened, \
                mock.patch('finding_viewer.os.getcwd', return_value=str(support.WORK)), \
                contextlib.redirect_stdout(io.StringIO()):
            finding_viewer.cmd_open([])
        self.assertIs(opened.call_args[0][0]['open'], True)

    def test_claude_reading_message_says_opens_or_is_ready(self):
        chat = [user('Review my branch and open it in the finding viewer'),
                assistant(text('## Critical\n- Rounding bug at src/app/billing.py:42'))]
        for value, word, tag in ((None, 'opens', 'on'), ('0', 'is ready', 'off')):
            with self.subTest(auto_open=value), support.env(FINDING_VIEWER_AUTO_OPEN=value):
                message, launch = run_hook(write_jsonl(f'ao_e_{tag}.jsonl', chat), f'sess-ao-e-{tag}')
                self.assertEqual(message, f'Finding viewer: Claude is reading the review; the page {word} in about '
                                          f'a minute → {hook.triage.page_url(launch.return_value)}')

    def test_the_old_off_command_points_at_auto_open(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(finding_viewer.main(['finding_viewer.py', 'off']), 0)
        self.assertEqual(out.getvalue().strip(), 'The on and off commands are gone: name the finding viewer in your '
                         'prompt, or run /findings; "auto_open": false in config.json keeps the hook from opening the '
                         'browser.')


if __name__ == '__main__':
    unittest.main()
