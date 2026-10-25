"""Reading a Claude Code transcript into turns, and the turn checks in hook.py: which turns name the viewer, run
/findings, hand a triage back, or fetch a PR."""
import support  # noqa: F401  first: points the tool at a scratch environment before it is imported
import json
import unittest

from hook import asks_review, find_pr, handed_back, launched_background, names_viewer, ran_findings, read_turns


def turn(prompt, tools=(), handoff=False, texts=()):
    return {'prompt': prompt, 'texts': list(texts), 'tools': list(tools), 'handoff': handoff}


def bash(command):
    return {'name': 'Bash', 'input': {'command': command}}


def skill(name):
    return {'name': 'Skill', 'input': {'skill': name}}


def write_jsonl(name, lines):
    """A transcript in the scratch folder: each line is a dict (one JSON object) or a raw string (kept as it is)."""
    path = support.SCRATCH / name
    path.write_text('\n'.join(x if isinstance(x, str) else json.dumps(x) for x in lines) + '\n', encoding='utf-8')
    return path


def user(content, **extra):
    return {'type': 'user', 'message': {'role': 'user', 'content': content}, **extra}


def assistant(*blocks):
    return {'type': 'assistant', 'message': {'content': list(blocks)}}


def text(value):
    return {'type': 'text', 'text': value}


def peer(body):
    return {'type': 'user', 'isMeta': True, 'origin': {'kind': 'peer', 'body': body}}


def tool_use(tid, name, **inp):
    return {'type': 'tool_use', 'id': tid, 'name': name, 'input': inp}


def tool_result(tid, content):
    return user([{'type': 'tool_result', 'tool_use_id': tid, 'content': content}])


def notification(tid, status='completed'):
    body = (f'<task-notification>\n<task-id>bg-1</task-id>\n<tool-use-id>{tid}</tool-use-id>\n'
            f'<status>{status}</status>\n'
            '<summary>Review done</summary>\n<result>The review is done.</result>\n</task-notification>')
    return user(body, origin={'kind': 'task-notification', 'producer': 'session-task'})


SKILL_LAUNCH_RESULT = 'Skill "code-review" launched (forked execution, running in the background).'


class FixtureTranscriptTest(unittest.TestCase):
    turns = None

    @classmethod
    def setUpClass(cls):
        cls.turns = read_turns(support.FIXTURES / 'transcript.jsonl')

    def test_fixture_gives_three_turns(self):
        self.assertEqual(len(self.turns), 3)

    def test_first_turn_prompt_is_the_request(self):
        self.assertEqual(self.turns[0]['prompt'],
                         'Review https://github.com/acme/billing/pull/4242 and open it in the finding viewer')

    def test_first_turn_texts_are_its_assistant_texts_in_order(self):
        self.assertEqual(self.turns[0]['texts'],
                         ['Fetching the diff.', '## Critical\n- Rounding bug at src/app/billing.py:42'])

    def test_first_turn_tools_are_the_whole_tool_use_block(self):
        self.assertEqual(self.turns[0]['tools'], [{'type': 'tool_use', 'id': 't1', 'name': 'Bash',
                                                   'input': {'command': 'gh pr diff 4242 --repo acme/billing'}}])

    def test_first_turn_keeps_cwd_and_session(self):
        self.assertEqual((self.turns[0]['cwd'], self.turns[0]['session']), ('/work/billing', 'sess-0001-aaaa'))

    def test_first_turn_is_not_a_handoff(self):
        self.assertIs(self.turns[0]['handoff'], False)

    def test_triage_attachment_marks_second_turn_as_handoff(self):
        t = self.turns[1]
        self.assertEqual((t['prompt'], t['texts'], t['tools'], t['handoff']),
                         ('Thanks, what next?', ['I will post C1.'], [], True))

    def test_peer_handback_starts_third_turn_as_handoff(self):
        t = self.turns[2]
        self.assertEqual((t['prompt'], t['texts'], t['handoff']),
                         ('Finding-viewer posted the review on PR #4242', ['Noted.', 'Still the same turn.'], True))

    def test_sidechain_text_is_in_no_turn(self):
        self.assertEqual([x for t in self.turns for x in t['texts'] if 'sidechain' in x], [])

    def test_missing_file_gives_no_turns(self):
        self.assertEqual(read_turns(support.SCRATCH / 'missing.jsonl'), [])

    def test_fixture_names_the_viewer_in_the_first_turn_only(self):
        self.assertEqual([names_viewer(t) for t in self.turns], [True, False, False])

    def test_fixture_runs_no_findings_command(self):
        self.assertEqual([ran_findings(t) for t in self.turns], [False, False, False])

    def test_fixture_handed_back_from_second_turn_on(self):
        self.assertEqual([handed_back(self.turns, i) for i in range(3)], [False, True, True])

    def test_fixture_first_turn_is_about_pr_4242_fetched(self):
        self.assertEqual(find_pr(self.turns, 0), (4242, 'acme/billing', True))


class ReadTurnsTest(unittest.TestCase):
    def read(self, name, lines):
        return read_turns(write_jsonl(name, lines))

    def test_string_prompt_is_kept_as_it_is(self):
        turns = self.read('string_prompt.jsonl', [user('hello'), assistant(text('hi'))])
        self.assertEqual((turns[0]['prompt'], turns[0]['texts']), ('hello', ['hi']))

    def test_list_prompt_joins_its_text_blocks_with_newlines(self):
        turns = self.read('list_prompt.jsonl', [user([text('first'), text('second')]), assistant(text('ok'))])
        self.assertEqual(turns[0]['prompt'], 'first\nsecond')

    def test_tool_result_line_does_not_split_the_turn(self):
        turns = self.read('tool_result.jsonl', [
            user('go'),
            assistant({'type': 'tool_use', 'id': 'a', 'name': 'Bash', 'input': {'command': 'ls'}}),
            user([{'type': 'tool_result', 'tool_use_id': 'a', 'content': 'x'}]),
            assistant(text('after')),
        ])
        self.assertEqual((len(turns), turns[0]['texts'], len(turns[0]['tools'])), (1, ['after'], 1))

    def test_assistant_line_before_any_prompt_starts_an_empty_turn(self):
        turns = self.read('no_prompt.jsonl', [assistant(text('orphan'))])
        self.assertEqual((turns[0]['prompt'], turns[0]['texts']), ('', ['orphan']))

    def test_non_json_lines_are_skipped(self):
        turns = self.read('bad_line.jsonl', ['garbage {', user('p'), assistant(text('a'))])
        self.assertEqual([(t['prompt'], t['texts']) for t in turns], [('p', ['a'])])

    def test_attachment_without_triage_text_is_not_a_handoff(self):
        att = {'type': 'attachment', 'attachment': {'type': 'hook_additional_context', 'content': 'hello'}}
        turns = self.read('plain_attachment.jsonl', [user('p'), att])
        self.assertIs(turns[0]['handoff'], False)

    def test_triage_attachment_before_any_prompt_is_ignored(self):
        att = {'type': 'attachment', 'attachment': {'type': 'hook_additional_context',
                                                    'content': 'Finding-viewer triage for "X" (1 to post)'}}
        turns = self.read('early_attachment.jsonl', [att, user('p')])
        self.assertEqual((len(turns), turns[0]['handoff']), (1, False))

    def test_peer_message_with_triage_body_starts_a_handoff_turn(self):
        turns = self.read('peer_triage.jsonl', [user('p'), peer('Triage of "Code review" (1 to post)'), assistant(text('y'))])
        self.assertEqual((len(turns), turns[1]['prompt'], turns[1]['handoff'], turns[1]['texts']),
                         (2, 'Triage of "Code review" (1 to post)', True, ['y']))

    def test_peer_message_from_a_user_origin_does_not_start_a_turn(self):
        origin = {'type': 'user', 'isMeta': True, 'origin': {'kind': 'user', 'body': 'Finding-viewer posted the review'}}
        turns = self.read('user_origin.jsonl', [user('p'), origin])
        self.assertEqual([t['prompt'] for t in turns], ['p'])


class BackgroundTurnsTest(unittest.TestCase):
    PROMPT = 'Review my branch with /code-review and open it in the finding viewer'

    def read(self, name, lines):
        return read_turns(write_jsonl(name, lines))

    def launch_and_notify(self, name, first=None):
        """A turn that launches /code-review in the background (it starts with ``first``, the prompt by default), then the
        turn its task's notification starts."""
        return self.read(name, [
            user(self.PROMPT) if first is None else first,
            assistant(text('Running.'), tool_use('toolu_bg1', 'Skill', skill='code-review')),
            tool_result('toolu_bg1', SKILL_LAUNCH_RESULT),
            notification('toolu_bg1'),
            assistant(text('Two findings.'), tool_use('toolu_rf1', 'ReportFindings', findings=[])),
        ])

    def test_background_skill_launch_is_kept_on_its_turn(self):
        turns = self.read('bg_skill.jsonl', [
            user('go'),
            assistant(tool_use('toolu_bg1', 'Skill', skill='code-review')),
            tool_result('toolu_bg1', SKILL_LAUNCH_RESULT),
        ])
        self.assertEqual((len(turns), turns[0]['background']), (1, ['toolu_bg1']))

    def test_background_agent_is_kept_once(self):
        turns = self.read('bg_agent.jsonl', [
            user('go'),
            assistant(tool_use('toolu_a1', 'Agent', prompt='review', run_in_background=True)),
            tool_result('toolu_a1', 'Async agent launched successfully. You will be notified when it is done.'),
        ])
        self.assertEqual(turns[0]['background'], ['toolu_a1'])

    def test_agent_without_run_in_background_is_not_background(self):
        turns = self.read('fg_agent.jsonl', [user('go'), assistant(tool_use('toolu_a2', 'Agent', prompt='review'))])
        self.assertEqual(turns[0]['background'], [])

    def test_bash_saying_running_in_the_background_is_not_background(self):
        turns = self.read('bg_bash.jsonl', [
            user('go'),
            assistant(tool_use('toolu_b1', 'Bash', command='sleep 100')),
            tool_result('toolu_b1', 'Command running in the background'),
        ])
        self.assertEqual(turns[0]['background'], [])

    def test_foreground_agent_that_mentions_the_background_is_not_background(self):
        turns = self.read('fg_result.jsonl', [
            user('go'),
            assistant(tool_use('toolu_a3', 'Agent', prompt='index it')),
            tool_result('toolu_a3', 'Done. The indexer keeps running in the background.'),
        ])
        self.assertEqual(turns[0]['background'], [])

    def test_null_text_in_a_result_does_not_raise(self):
        turns = self.read('null_text.jsonl', [
            user('go'),
            assistant(tool_use('toolu_a4', 'Agent', prompt='x', run_in_background=True)),
            user([{'type': 'tool_result', 'tool_use_id': 'toolu_a4', 'content': [{'type': 'text', 'text': None}]}]),
        ])
        self.assertEqual((len(turns), turns[0]['background']), (1, ['toolu_a4']))

    def test_string_origin_does_not_raise(self):
        turns = self.read('str_origin.jsonl', [user('p', origin='cli'), assistant(text('a'))])
        self.assertEqual((len(turns), turns[0]['prompt'], turns[0]['texts']), (1, 'p', ['a']))

    def test_meta_row_with_a_notification_origin_does_not_start_a_turn(self):
        turns = self.read('meta_notification.jsonl', [
            user('p'),
            {**notification('toolu_x'), 'isMeta': True},
            assistant(text('a')),
        ])
        self.assertEqual((len(turns), turns[0]['texts']), (1, ['a']))

    def test_asks_review_follows_a_notification_of_a_notification(self):
        turns = self.read('bg_chain.jsonl', [
            user('check my branch, open it in the finding viewer'),
            assistant(tool_use('toolu_bg1', 'Skill', skill='code-review')),
            tool_result('toolu_bg1', SKILL_LAUNCH_RESULT),
            notification('toolu_bg1'),
            assistant(tool_use('toolu_ag2', 'Agent', prompt='look at the rounding', run_in_background=True)),
            tool_result('toolu_ag2', 'Async agent launched successfully. You will be notified when it is done.'),
            notification('toolu_ag2'),
            assistant(text('Done.')),
        ])
        self.assertEqual((turns[2]['continues'], asks_review(turns[2], turns), asks_review(turns[2])), (1, True, False))

    def test_notification_starts_a_turn_of_its_own(self):
        self.assertEqual(len(self.launch_and_notify('notify_count.jsonl')), 2)

    def test_notification_turn_keeps_the_prompt_and_handoff_of_the_launch(self):
        cases = ((user(self.PROMPT), False), (peer('Triage of "Code review" (1 to post)'), True))
        for first, handed in cases:
            with self.subTest(launch_is_handoff=handed):
                turns = self.launch_and_notify('notify_prompt.jsonl', first)
                self.assertEqual(turns[0]['handoff'], handed)
                self.assertEqual((turns[1]['prompt'], turns[1]['handoff']), (turns[0]['prompt'], handed))

    def test_notification_turn_has_only_the_rows_after_the_notification(self):
        turns = self.launch_and_notify('notify_rows.jsonl')
        self.assertEqual((turns[1]['texts'], turns[1]['tools']),
                         (['Two findings.'], [tool_use('toolu_rf1', 'ReportFindings', findings=[])]))

    def test_notification_turn_continues_the_launching_turn(self):
        self.assertEqual(self.launch_and_notify('notify_continues.jsonl')[1]['continues'], 0)

    def test_launching_turn_is_found_by_tool_use_id_past_another_prompt(self):
        turns = self.read('bg_between.jsonl', [
            user(self.PROMPT),
            assistant(tool_use('toolu_bg1', 'Skill', skill='code-review')),
            tool_result('toolu_bg1', SKILL_LAUNCH_RESULT),
            user('What is the weather?'),
            assistant(text('Sunny.')),
            notification('toolu_bg1'),
            assistant(text('Done.')),
        ])
        self.assertEqual((turns[2]['continues'], turns[2]['prompt']), (0, self.PROMPT))

    def test_notification_matching_no_call_carries_on_the_turn_before(self):
        turns = self.read('bg_unmatched.jsonl', [
            user('first'), assistant(text('a')),
            user('second'), assistant(text('b')),
            notification('toolu_nobody'),
            assistant(text('c')),
        ])
        self.assertEqual((len(turns), turns[2]['continues'], turns[2]['prompt']), (3, 1, 'second'))

    def test_notification_with_no_turn_before_it_starts_an_empty_turn(self):
        turns = self.read('bg_first.jsonl', [notification('toolu_bg1'), assistant(text('Late.'))])
        self.assertEqual((turns[0]['prompt'], 'continues' in turns[0], turns[0]['texts']), ('', False, ['Late.']))

    def test_launched_background_is_true_for_the_launching_turn_only(self):
        turns = self.launch_and_notify('bg_flag.jsonl')
        self.assertEqual((launched_background(turns[0]), launched_background(turns[1])), (True, False))

    def test_notification_turn_asks_for_review_through_its_launching_turn(self):
        turns = self.read('bg_ask.jsonl', [
            user('check my branch, open it in the finding viewer'),
            assistant(tool_use('toolu_bg1', 'Skill', skill='code-review')),
            tool_result('toolu_bg1', SKILL_LAUNCH_RESULT),
            notification('toolu_bg1'),
            assistant(text('Two findings.')),
        ])
        self.assertEqual((asks_review(turns[1], turns), asks_review(turns[1])), (True, False))


class NamesViewerTest(unittest.TestCase):
    def test_slash_findings_names_the_viewer(self):
        self.assertIs(names_viewer(turn('/findings')), True)

    def test_plugin_command_names_the_viewer(self):
        self.assertIs(names_viewer(turn('review PR 12, then /a-bit-human-review:findings')), True)

    def test_plugin_command_name_alone_never_counts(self):
        self.assertIs(names_viewer(turn('<command-name>/a-bit-human-review:findings</command-name>')), False)

    def test_plugin_skill_inside_a_path_does_not_count(self):
        self.assertIs(names_viewer(turn('see ~/.claude/plugins/cache/x/a-bit-human-review:findings')), False)

    def test_command_name_alone_never_counts(self):
        self.assertIs(names_viewer(turn('<command-name>/findings</command-name>')), False)

    def test_command_name_with_its_args_does_not_count_the_name(self):
        self.assertIs(names_viewer(turn('<command-name>/findings</command-name>\n<command-args>list</command-args>')), False)

    def test_command_args_count_when_they_name_the_viewer(self):
        prompt = '<command-message>findings</command-message>\n<command-args>open it in the finding viewer</command-args>'
        self.assertIs(names_viewer(turn(prompt)), True)

    def test_findings_command_inside_a_path_does_not_count(self):
        self.assertIs(names_viewer(turn('see ~/.claude/commands/findings.md')), False)

    def test_handed_back_triage_is_not_a_request(self):
        self.assertIs(names_viewer(turn('open it in the finding viewer', handoff=True)), False)

    def test_project_name_counts(self):
        self.assertIs(names_viewer(turn('run a-bit-human-review on it')), True)

    def test_findings_dash_viewer_counts(self):
        self.assertIs(names_viewer(turn('open the findings-viewer')), True)

    def test_script_name_does_not_count(self):
        self.assertIs(names_viewer(turn('python3 finding_viewer.py')), False)


class RanFindingsTest(unittest.TestCase):
    def test_findings_skill_counts(self):
        self.assertIs(ran_findings(turn('x', [skill('findings')])), True)

    def test_plugin_qualified_findings_skill_counts(self):
        self.assertIs(ran_findings(turn('x', [skill('a-bit-human-review:findings')])), True)

    def test_slash_prefixed_findings_skill_counts(self):
        self.assertIs(ran_findings(turn('x', [skill('/findings')])), True)

    def test_other_skill_does_not_count(self):
        self.assertIs(ran_findings(turn('x', [skill('code-review')])), False)

    def test_viewer_open_subcommand_counts(self):
        self.assertIs(ran_findings(turn('x', [bash('python3 ~/tools/abhr/finding_viewer.py open')])), True)

    def test_viewer_hook_subcommand_does_not_count(self):
        self.assertIs(ran_findings(turn('x', [bash('python3 finding_viewer.py hook')])), False)

    def test_viewer_maintenance_subcommands_do_not_count(self):
        for sub in ('doctor', 'install', 'uninstall', 'migrate'):
            with self.subTest(sub=sub):
                self.assertIs(ran_findings(turn('x', [bash(f'python3 ~/x/finding_viewer.py {sub}')])), False)

    def test_viewer_without_subcommand_counts(self):
        self.assertIs(ran_findings(turn('x', [bash('python3 finding_viewer.py')])), True)

    def test_viewer_run_with_plain_python_counts(self):
        self.assertIs(ran_findings(turn('x', [bash('python finding_viewer.py list')])), True)

    def test_quoted_viewer_path_with_a_space_counts(self):
        self.assertIs(ran_findings(turn('x', [bash("python3 '/a b/finding_viewer.py' list")])), True)

    def test_quoted_viewer_path_with_a_maintenance_subcommand_does_not_count(self):
        self.assertIs(ran_findings(turn('x', [bash("python3 '/ab/finding_viewer.py' doctor")])), False)

    def test_reading_the_script_does_not_count(self):
        self.assertIs(ran_findings(turn('x', [bash('cat finding_viewer.py')])), False)


class HandedBackTest(unittest.TestCase):
    def test_yes_after_a_handoff_is_handed_back(self):
        turns = [turn('triage', handoff=True), turn('yes, post them')]
        self.assertIs(handed_back(turns, 1), True)

    def test_new_review_request_after_a_handoff_ends_it(self):
        turns = [turn('triage', handoff=True), turn('please review PR #7 again'), turn('ok')]
        self.assertIs(handed_back(turns, 2), False)

    def test_plain_turns_are_not_handed_back(self):
        self.assertIs(handed_back([turn('hello'), turn('ok')], 1), False)


class FindPrTest(unittest.TestCase):
    def test_pr_number_from_prompt_without_a_fetch(self):
        self.assertEqual(find_pr([turn('Look at PR #77 please')], 0), (77, None, False))

    def test_repo_and_fetch_from_a_gh_call(self):
        self.assertEqual(find_pr([turn('Look at PR #77', [bash('gh pr view 77 --repo acme/billing')])], 0),
                         (77, 'acme/billing', True))

    def test_short_reference_gives_the_repo_and_number(self):
        self.assertEqual(find_pr([turn('acme/billing#4242 builds on acme/billing#4100')], 0),
                         (4242, 'acme/billing', False))

    def test_no_pr_mentioned_gives_nothing(self):
        self.assertEqual(find_pr([turn('nothing here')], 0), (None, None, False))

    def test_pr_from_an_earlier_prompt_without_a_fetch(self):
        self.assertEqual(find_pr([turn('Review PR #55'), turn('continue')], 1), (55, None, False))

    def test_pr_from_an_earlier_prompt_fetched_by_an_api_call(self):
        turns = [turn('Review PR #55', [bash('gh api repos/acme/billing/pulls/55')]), turn('continue')]
        self.assertEqual(find_pr(turns, 1), (55, None, True))

    def test_pr_url_in_the_answer_text_gives_the_repo(self):
        self.assertEqual(find_pr([turn('x', texts=['See https://github.com/acme/billing/pull/9001'])], 0),
                         (9001, 'acme/billing', False))


if __name__ == '__main__':
    unittest.main()
