import support  # noqa: F401  first: points the tool at a scratch environment before it is imported

import contextlib
import http.client
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import paths
import rereview
import triage

WRITE_HEADERS = {'X-Finding-Viewer': '1', 'Content-Type': 'application/json'}
BAD_FINDINGS = 'expected {"findings": {...}}'


def page_name(n: int) -> str:
    return f'20261019-1200{n:02d}-code-review-pr-4242'


def route_page_name(n: int) -> str:
    return f'20261020-1300{n:02d}-route-page-{n:02d}'


def send(method, path, body=None, headers=None):
    """One request to the server; returns (response, raw bytes). A body without headers gets the write headers."""
    if headers is None:
        headers = dict(WRITE_HEADERS) if body is not None else {}
    payload = None if body is None else json.dumps(body)
    conn = http.client.HTTPConnection('127.0.0.1', triage.PORT, timeout=5)
    try:
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        return resp, resp.read()
    finally:
        conn.close()


def request(method, path, body=None, headers=None):
    """Returns (status, parsed JSON or text); adds write headers only if a body is sent and no headers are passed."""
    resp, raw = send(method, path, body, headers)
    text = raw.decode('utf-8')
    if resp.getheader('Content-Type', '').startswith('application/json'):
        return resp.status, json.loads(text)
    return resp.status, text


def decisions_file(page):
    return support.OUT / page / 'decisions.json'


class ServerTestCase(unittest.TestCase):
    """Starts the real request handler on the scratch port, once per test class."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', triage.PORT), triage.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()


class AccessTests(ServerTestCase):
    def test_ping_reports_app_version_and_pid(self):
        status, body = request('GET', '/api/ping')
        self.assertEqual(status, 200)
        self.assertEqual(body['app'], 'finding-viewer')
        self.assertEqual(body['version'], triage.SERVER_VERSION)
        self.assertEqual(body['pid'], os.getpid())

    def test_scratch_port_and_output_folder_are_isolated(self):
        self.assertNotEqual(triage.PORT, 47321)
        self.assertTrue(str(paths.OUT).startswith(str(support.SCRATCH)))

    def test_get_with_foreign_host_is_forbidden(self):
        status, body = request('GET', '/api/ping', headers={'Host': f'evil.example:{triage.PORT}'})
        self.assertEqual((status, body), (403, {'error': 'forbidden host'}))

    def test_put_without_viewer_header_is_forbidden_and_writes_nothing(self):
        page = page_name(5)
        support.write_page(page)
        status, body = request('PUT', f'/api/decisions/{page}', {'findings': {}, 'rereviews': 0},
                               headers={'Content-Type': 'application/json'})
        self.assertEqual((status, body), (403, {'error': 'forbidden'}))
        self.assertFalse(decisions_file(page).exists())

    def test_put_with_foreign_host_is_forbidden_and_writes_nothing(self):
        page = page_name(7)
        support.write_page(page)
        headers = {'Host': 'evil.example', **WRITE_HEADERS}
        status, body = request('PUT', f'/api/decisions/{page}', {'findings': {}, 'rereviews': 0}, headers=headers)
        self.assertEqual((status, body), (403, {'error': 'forbidden'}))
        self.assertFalse(decisions_file(page).exists())

    def test_put_with_foreign_origin_is_forbidden_and_writes_nothing(self):
        page = page_name(8)
        support.write_page(page)
        headers = {'Origin': 'http://evil.example', **WRITE_HEADERS}
        status, body = request('PUT', f'/api/decisions/{page}', {'findings': {}, 'rereviews': 0}, headers=headers)
        self.assertEqual((status, body), (403, {'error': 'forbidden'}))
        self.assertFalse(decisions_file(page).exists())


class SaveTests(ServerTestCase):
    def test_get_of_never_saved_decisions_is_empty(self):
        page = page_name(3)
        support.write_page(page)
        self.assertEqual(request('GET', f'/api/decisions/{page}'), (200, {}))

    def test_put_saves_decisions_and_get_returns_them(self):
        page = page_name(4)
        support.write_page(page)
        path = f'/api/decisions/{page}'
        status, saved = request('PUT', path, {'findings': {'C1': {'status': 'post', 'note': 'n'}}, 'rereviews': 0})
        self.assertEqual(status, 200)
        self.assertIs(saved['ok'], True)
        self.assertIn('saved_at', saved)
        status, body = request('GET', path)
        self.assertEqual(status, 200)
        self.assertEqual(body['findings'], {'C1': {'status': 'post', 'note': 'n'}})
        self.assertEqual(body['rereviews'], 0)
        self.assertEqual(body['saved_at'], saved['saved_at'])
        self.assertTrue(decisions_file(page).exists())

    def test_put_to_unknown_page_is_404(self):
        self.assertEqual(request('PUT', '/api/decisions/nope', {'findings': {}, 'rereviews': 0}),
                         (404, {'error': 'unknown page'}))

    def test_get_with_bad_page_name_is_400(self):
        self.assertEqual(request('GET', '/api/decisions/bad%20name'), (400, {'error': 'bad page name'}))

    def test_put_whose_findings_is_not_an_object_is_400(self):
        page = page_name(12)
        support.write_page(page)
        self.assertEqual(request('PUT', f'/api/decisions/{page}', {'findings': []}), (400, {'error': BAD_FINDINGS}))

    def test_put_with_stale_rereviews_count_is_409(self):
        page = page_name(11)
        support.write_page(page, support.sample_page(page, rereviewed=[
            {'from': '1111111122222222', 'to': 'abcdef1234567890', 'at': '2026-10-19T12:30:00'}]))
        status, body = request('PUT', f'/api/decisions/{page}', {'findings': {}, 'rereviews': 0})
        self.assertEqual(status, 409)
        self.assertEqual(body['error'], 'This review was re-reviewed at abcdef12 after this tab loaded it, '
                                        'so this change was not saved. Reload the page.')
        self.assertFalse(decisions_file(page).exists())

    def test_put_without_rereviews_count_is_409(self):
        page = page_name(21)
        support.write_page(page, support.sample_page(page, rereviewed=[
            {'from': '1111111122222222', 'to': 'abcdef1234567890', 'at': '2026-10-19T12:30:00'}]))
        status, body = request('PUT', f'/api/decisions/{page}', {'findings': {}})
        self.assertEqual(status, 409)
        self.assertIn('re-reviewed at abcdef12', body['error'])

    def test_put_with_current_rereviews_count_is_saved(self):
        page = page_name(31)
        support.write_page(page, support.sample_page(page, rereviewed=[
            {'from': '1111111122222222', 'to': 'abcdef1234567890', 'at': '2026-10-19T12:30:00'}]))
        self.assertEqual(request('PUT', f'/api/decisions/{page}', {'findings': {}, 'rereviews': 1})[0], 200)

    def test_put_is_refused_with_423_while_a_rereview_runs(self):
        page = page_name(10)
        support.write_page(page)
        path = f'/api/decisions/{page}'
        status, first = request('PUT', path, {'findings': {'C1': {'status': 'post', 'note': 'first'}}, 'rereviews': 0})
        self.assertEqual(status, 200)
        state = support.OUT / page / 'rereview.json'
        # The pid is this test process, which is alive, so the re-review reads as running and locks the page.
        state.write_text(json.dumps({'status': 'reviewing', 'pid': os.getpid(), 'job': 'abcdef123456',
                                     'started': time.time()}), encoding='utf-8')
        self.addCleanup(state.unlink, missing_ok=True)
        status, body = request('PUT', path, {'findings': {'C1': {'status': 'post', 'note': 'second'}}, 'rereviews': 0})
        self.assertEqual(status, 423)
        self.assertEqual(body, rereview.locked_error({}))
        status, saved = request('GET', path)
        self.assertEqual(saved['findings'], {'C1': {'status': 'post', 'note': 'first'}})
        self.assertEqual(saved['saved_at'], first['saved_at'])
        state.unlink()
        status, body = request('PUT', path, {'findings': {'C1': {'status': 'post', 'note': 'second'}}, 'rereviews': 0})
        self.assertEqual(status, 200)


class PageTests(ServerTestCase):
    def test_written_page_is_served_with_a_nonce_csp(self):
        page = page_name(13)
        support.write_page(page)
        resp, _ = send('GET', f'/{page}.html')
        self.assertEqual(resp.status, 200)
        self.assertIn("script-src 'nonce-", resp.getheader('Content-Security-Policy'))

    def test_missing_html_page_is_404(self):
        self.assertEqual(request('GET', '/missing.html'), (404, {'error': 'not found'}))

    def make_page(self, name):
        """A page written the way the renderer does; its folder is removed again after the test."""
        self.addCleanup(shutil.rmtree, support.OUT / name, ignore_errors=True)
        return support.write_page(name)

    def point_latest_at(self, target):
        """Point out/latest.html at ``target`` for this test; the link that was there, if any, comes back afterwards."""
        latest = support.OUT / 'latest.html'
        before = os.readlink(latest) if latest.is_symlink() else None
        self.addCleanup(self.restore_latest, latest, before)
        latest.unlink(missing_ok=True)
        latest.symlink_to(target)

    @staticmethod
    def restore_latest(latest, before):
        latest.unlink(missing_ok=True)
        if before is not None:
            latest.symlink_to(before)

    def test_latest_html_serves_the_page_it_links_to(self):
        name = route_page_name(1)
        self.make_page(name)
        self.point_latest_at(support.OUT / name / 'page.html')
        resp, raw = send('GET', '/latest.html')
        self.assertEqual(resp.status, 200)
        self.assertIn("script-src 'nonce-", resp.getheader('Content-Security-Policy'))
        body = raw.decode('utf-8')
        self.assertIn(f'"page": "{name}"', body)
        self.assertIn('Code review · PR #4242', body)

    def test_a_symlinked_page_folder_is_not_served(self):
        name = route_page_name(2)
        outside = support.SCRATCH / f'outside-{name}'
        outside.mkdir()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (outside / 'page.html').write_text('<!doctype html><title>outside</title>', encoding='utf-8')
        (outside / 'review.json').write_text(json.dumps(support.sample_page(name)), encoding='utf-8')
        link = support.OUT / name
        link.symlink_to(outside, target_is_directory=True)
        self.addCleanup(link.unlink, missing_ok=True)
        self.assertEqual(request('GET', f'/{name}.html'), (404, {'error': 'not found'}))

    def test_a_symlinked_page_html_is_not_served(self):
        name = route_page_name(3)
        page = self.make_page(name)
        (page.parent / 'review.json').unlink()
        secret = support.SCRATCH / f'secret-{name}.txt'
        secret.write_text('SECRET', encoding='utf-8')
        self.addCleanup(secret.unlink, missing_ok=True)
        page.unlink()
        page.symlink_to(secret)
        resp, raw = send('GET', f'/{name}.html')
        self.assertEqual(resp.status, 404)
        self.assertNotIn(b'SECRET', raw)

    def test_names_with_a_leading_dot_are_refused(self):
        self.assertEqual(request('GET', '/..html'), (404, {'error': 'not found'}))
        self.assertEqual(request('GET', '/.staging-ab12.html'), (404, {'error': 'not found'}))
        self.assertEqual(request('GET', '/api/decisions/..'), (400, {'error': 'bad page name'}))


class ServerVersionTest(unittest.TestCase):
    def test_version_is_a_string_derived_from_the_code(self):
        self.assertIsNone(triage.SERVER_VERSION_OVERRIDE)
        self.assertIsInstance(triage.SERVER_VERSION, str)
        self.assertEqual(triage.SERVER_VERSION, triage.source_version())
        self.assertEqual(len(triage.SERVER_VERSION), 12)

    def test_version_changes_with_a_module_or_a_web_file_and_not_with_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'web').mkdir()
            (root / 'tests').mkdir()
            (root / 'a.py').write_text('x = 1\n')
            (root / 'web' / 'page.css').write_text('body {}\n')
            (root / 'tests' / 'test_a.py').write_text('y = 1\n')
            v1 = triage.source_version(root)
            self.assertEqual(triage.source_version(root), v1)
            with (root / 'a.py').open('a') as fh:
                fh.write('# c\n')
            self.assertNotEqual(triage.source_version(root), v1)
            (root / 'a.py').write_text('x = 1\n')
            self.assertEqual(triage.source_version(root), v1)
            (root / 'web' / 'page.css').write_text('body {color: red}\n')
            v2 = triage.source_version(root)
            self.assertNotEqual(v2, v1)
            (root / 'tests' / 'test_a.py').write_text('y = 2\n')
            self.assertEqual(triage.source_version(root), v2)
            (root / 'web' / 'new.js').write_text('let n = 1;\n')
            self.assertNotEqual(triage.source_version(root), v2)

    @contextlib.contextmanager
    def ping_stub(self, body):
        """A server on the test port that answers every GET with ``body``, standing in for the review server's ping."""

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 — the name http.server calls
                data = json.dumps(body).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):  # keep the test output quiet
                pass

        server = ThreadingHTTPServer(('127.0.0.1', triage.PORT), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield
        finally:
            server.shutdown()
            thread.join(5)
            server.server_close()

    def test_replace_old_server_replaces_only_a_finding_viewer_server_of_other_code(self):
        cases = [
            ('nothing listens', None, 0),
            ('a server of this code', {'app': 'finding-viewer', 'version': triage.SERVER_VERSION}, 0),
            ('a server of old code', {'app': 'finding-viewer', 'version': 22}, 1),
            ('another app on the port', {'app': 'something-else', 'version': 1}, 0),
        ]
        for name, body, expected_calls in cases:
            with self.subTest(name), mock.patch.object(triage, 'ensure_server') as ensure:
                with (self.ping_stub(body) if body is not None else contextlib.nullcontext()):
                    triage.replace_old_server()
                self.assertEqual(ensure.call_count, expected_calls)


if __name__ == '__main__':
    unittest.main()
