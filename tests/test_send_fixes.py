"""Send fixes to session: the triage relayed to the open review session at once (handoff.send_fixes, a detached worker
run against the claude stub), and its route."""
import support  # noqa: F401  first: points the tool at a scratch environment before it is imported

import contextlib
import http.client
import io
import json
import os
import subprocess
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

import handoff
import jobs
import paths
import store
import triage

SID = 'sess-send-0001'
RELAY = 'stub-relay-0001'  # the session id the claude stub reports for a relay that is not the review session
HINT = 'Type any prompt in the review session and it gets the triage.'


def local_page(name, title):
    """A review of the reviewer's own code with no PR, owned by the review session SID."""
    support.write_page(name, support.sample_page(name, pr=None, github=None, session_id=SID, review_session=SID,
                                                 title=title))
    return name


def write_decisions(name, title, statuses):
    """The page's triage: each finding of the sample page (C1, W1) with the status given for it."""
    findings = {fid: {'status': st, 'note': '', 'mode': 'note', 'title': f'{fid} title', 'severity': 'critical',
                      'anchor': {'path': 'src/app/billing.py', 'line': 42, 'in_diff': True}}
                for fid, st in statuses.items()}
    path = support.OUT / name / 'decisions.json'
    path.write_text(json.dumps({'page': name, 'title': title, 'session_id': SID, 'pr': None,
                                'saved_at': '2026-10-20T12:00:00', 'findings': findings}), encoding='utf-8')


def open_session(test, name='review-tab'):
    """This test process as a Claude Code session named ``name`` that is open, in the fake HOME's ~/.claude/sessions."""
    path = support.HOME / '.claude' / 'sessions' / f'{os.getpid()}.json'
    path.write_text(json.dumps({'sessionId': SID, 'pid': os.getpid(), 'name': name, 'cwd': str(support.WORK),
                                'status': 'idle'}), encoding='utf-8')
    test.addCleanup(path.unlink, missing_ok=True)


def transcript(test, sid):
    """A Claude Code transcript for ``sid`` under the fake HOME, where delete_session looks; removed after the test."""
    path = support.HOME / '.claude' / 'projects' / '-work-billing' / f'{sid}.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'type': 'user', 'cwd': str(support.WORK), 'sessionId': sid}) + '\n', encoding='utf-8')
    test.addCleanup(path.unlink, missing_ok=True)
    return path


def sent(name):
    """The last Send fixes once its worker is finished, else None."""
    st = handoff.send_state(name)
    return st if st and st['status'] != 'sending' else None


def inject_output():
    """What the UserPromptSubmit hook would add to the next prompt of the review session now."""
    out = io.StringIO()
    with support.env(FINDING_VIEWER_CHILD=None), mock.patch('sys.stdin', io.StringIO(json.dumps({'session_id': SID}))), \
            contextlib.redirect_stdout(out):
        handoff.cmd_inject()
    return out.getvalue()


class SendFixesTests(unittest.TestCase):
    def start(self, name, send='ok', session=None, sleep=None):
        """Start Send fixes on the page, with the relay answering ``send`` (ok or error) as the session ``session`` (the
        review session's id when None) and waiting ``sleep`` seconds; the worker inherits the claude stub's variables.
        Returns the start answer."""
        prompts = support.SCRATCH / f'{name}-prompts.jsonl'
        with support.env(STUB_CLAUDE_SEND=send, STUB_CLAUDE_PROMPT=prompts, STUB_CLAUDE_SESSION=session,
                         STUB_CLAUDE_SLEEP=sleep):
            code, body = handoff.send_fixes(name, store.page_data(name))
        if body.get('pid'):
            self.addCleanup(jobs.kill_group, body['pid'], body['job'])
        return code, body

    def test_fixes_are_sent_to_the_open_session_and_its_triage_is_marked_seen(self):
        name = local_page('20261020-140001-send-ok', 'Send ok triage')
        write_decisions(name, 'Send ok triage', {'C1': 'fix'})
        open_session(self)
        code, body = self.start(name)
        self.assertEqual(code, 202)
        self.assertEqual(body['status'], 'sending')
        st = support.wait_for(lambda: sent(name))
        self.assertEqual(st['status'], 'sent', st.get('error'))
        prompt = json.loads((support.SCRATCH / f'{name}-prompts.jsonl').read_text(encoding='utf-8').splitlines()[0])
        self.assertIn('Finding-viewer triage for "', prompt)
        self.assertIn('to="review-tab"', prompt)
        inject = json.loads(paths.INJECT_STATE.read_text(encoding='utf-8'))
        self.assertEqual(inject[SID][name], handoff.page_state(name)[3])

    def test_the_inject_hook_does_not_add_the_sent_triage_to_the_next_prompt(self):
        name = local_page('20261020-140002-send-inject', 'Send inject triage')
        write_decisions(name, 'Send inject triage', {'C1': 'fix'})
        open_session(self)
        self.start(name)
        self.assertEqual(support.wait_for(lambda: sent(name))['status'], 'sent')
        # A second page the session has not seen yet: the hook does add its triage, so the hook is live here.
        control = local_page('20261020-140003-send-control', 'Send control triage')
        write_decisions(control, 'Send control triage', {'C1': 'fix'})
        output = inject_output()
        self.assertIn('Send control triage', output)
        self.assertNotIn('Send inject triage', output)

    def test_a_relay_that_fails_is_recorded_with_its_reason_and_the_hint(self):
        name = local_page('20261020-140004-send-error', 'Send error triage')
        write_decisions(name, 'Send error triage', {'C1': 'fix'})
        open_session(self)
        code, _body = self.start(name, send='error')
        self.assertEqual(code, 202)
        st = support.wait_for(lambda: sent(name))
        self.assertEqual(st['status'], 'failed')
        self.assertIn(HINT, st['error'])
        self.assertTrue(st['error'].startswith('Not sent to review-tab: '), st['error'])
        self.assertNotIn('\n', st['error'])
        inject = json.loads(paths.INJECT_STATE.read_text(encoding='utf-8')) if paths.INJECT_STATE.exists() else {}
        self.assertNotIn(name, inject.get(SID, {}))

    def test_a_failed_relay_puts_back_the_version_seen_before(self):
        name = local_page('20261020-140016-send-earlier', 'Send earlier triage')
        write_decisions(name, 'Send earlier triage', {'C1': 'fix'})
        open_session(self)
        paths.INJECT_STATE.write_text(json.dumps({SID: {name: 'earlier-version'}}), encoding='utf-8')
        self.addCleanup(paths.INJECT_STATE.unlink, missing_ok=True)
        self.start(name, send='error')
        self.assertEqual(support.wait_for(lambda: sent(name))['status'], 'failed')
        inject = json.loads(paths.INJECT_STATE.read_text(encoding='utf-8'))
        self.assertEqual(inject[SID][name], 'earlier-version')

    def test_the_relay_session_is_deleted_and_the_review_session_is_kept(self):
        name = local_page('20261020-140008-send-transcripts', 'Send transcripts triage')
        write_decisions(name, 'Send transcripts triage', {'C1': 'fix'})
        open_session(self)
        relay = transcript(self, RELAY)
        review = transcript(self, SID)
        self.start(name, session=RELAY)
        self.assertEqual(support.wait_for(lambda: sent(name))['status'], 'sent')
        self.assertFalse(relay.exists())
        self.assertTrue(review.exists())

    def test_with_the_review_session_as_the_relay_its_transcript_is_kept(self):
        name = local_page('20261020-140009-send-own-session', 'Send own session triage')
        write_decisions(name, 'Send own session triage', {'C1': 'fix'})
        open_session(self)
        review = transcript(self, SID)
        self.start(name, session=SID)
        self.assertEqual(support.wait_for(lambda: sent(name))['status'], 'sent')
        self.assertTrue(review.exists())

    def test_a_second_send_while_one_runs_is_refused(self):
        name = local_page('20261020-140014-send-twice', 'Send twice triage')
        write_decisions(name, 'Send twice triage', {'C1': 'fix'})
        open_session(self)
        code, _body = self.start(name, sleep='5')
        self.assertEqual(code, 202)
        code, body = handoff.send_fixes(name, store.page_data(name))
        self.assertEqual(code, 409)
        self.assertIn('Still sending the fixes', body['error'])

    def test_a_lost_worker_reads_as_failed_with_the_hint_and_unmarks_the_triage(self):
        name = local_page('20261020-140015-send-lost', 'Send lost triage')
        write_decisions(name, 'Send lost triage', {'C1': 'fix'})
        open_session(self)
        version = handoff.page_state(name)[3]
        gone = subprocess.Popen(['true'])
        gone.wait()
        (support.OUT / name / 'send.json').write_text(json.dumps({
            'status': 'sending', 'job': jobs.new_id(), 'pid': gone.pid, 'started': time.time(), 'ended': None,
            'error': None, 'session': SID, 'to': 'review-tab', 'version': version, 'cwd': str(support.WORK)}),
            encoding='utf-8')
        paths.INJECT_STATE.write_text(json.dumps({SID: {name: version}}), encoding='utf-8')
        self.addCleanup(paths.INJECT_STATE.unlink, missing_ok=True)
        st = handoff.send_state(name)
        self.assertEqual(st['status'], 'failed')
        self.assertIn(HINT, st['error'])
        inject = json.loads(paths.INJECT_STATE.read_text(encoding='utf-8'))
        self.assertNotIn(name, inject.get(SID, {}))

    def test_with_no_open_session_nothing_is_sent(self):
        name = local_page('20261020-140005-send-closed', 'Send closed triage')
        write_decisions(name, 'Send closed triage', {'C1': 'fix'})
        code, body = handoff.send_fixes(name, store.page_data(name))
        self.assertEqual(code, 409)
        self.assertIn(HINT, body['error'])
        self.assertIsNone(handoff.send_state(name))

    def test_a_triage_with_no_fix_finding_is_refused(self):
        name = local_page('20261020-140006-send-nofix', 'Send nofix triage')
        write_decisions(name, 'Send nofix triage', {'C1': 'skip'})
        open_session(self)
        code, _body = handoff.send_fixes(name, store.page_data(name))
        self.assertEqual(code, 400)

    def test_the_finished_send_shows_as_done_in_the_running_panel(self):
        name = local_page('20261020-140007-send-panel', 'Send panel triage')
        write_decisions(name, 'Send panel triage', {'C1': 'fix'})
        open_session(self)
        self.start(name)
        support.wait_for(lambda: sent(name))
        rows = [row for row in triage.activity(name)['ended'] if row.get('id') == 'send']
        self.assertEqual([row['state'] for row in rows], ['done'])


class SendRouteTests(unittest.TestCase):
    """POST /api/send-fixes/<page> through the real request handler on the scratch port."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', triage.PORT), triage.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def post(self, name, headers=None):
        """POST /api/send-fixes/<name>; returns (status, parsed JSON body)."""
        conn = http.client.HTTPConnection('127.0.0.1', triage.PORT, timeout=5)
        try:
            conn.request('POST', f'/api/send-fixes/{name}', headers=headers or {})
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read().decode('utf-8'))
        finally:
            conn.close()

    def test_a_post_without_the_viewer_header_is_forbidden(self):
        name = local_page('20261020-140010-send-route-forbidden', 'Route forbidden triage')
        write_decisions(name, 'Route forbidden triage', {'C1': 'fix'})
        self.assertEqual(self.post(name), (403, {'error': 'forbidden'}))

    def test_a_local_page_with_no_open_session_answers_409(self):
        name = local_page('20261020-140011-send-route-closed', 'Route closed triage')
        write_decisions(name, 'Route closed triage', {'C1': 'fix'})
        status, body = self.post(name, {'X-Finding-Viewer': '1'})
        self.assertEqual(status, 409)
        self.assertIn(HINT, body['error'])

    def test_an_unknown_page_is_404(self):
        self.assertEqual(self.post('20261020-140012-no-such-page', {'X-Finding-Viewer': '1'}),
                         (404, {'error': 'not found'}))

    def test_a_pr_page_is_refused_as_not_the_reviewers_own_code(self):
        name = '20261020-140013-send-route-pr'
        support.write_page(name)
        write_decisions(name, 'Route PR triage', {'C1': 'fix'})
        status, body = self.post(name, {'X-Finding-Viewer': '1'})
        self.assertEqual(status, 409)
        self.assertIn('your own code', body['error'])


if __name__ == '__main__':
    unittest.main()
