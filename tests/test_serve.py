"""
test_serve.py - the `fha serve` localhost workbench (plan 17, Wave 3).

Runs a REAL server on 127.0.0.1:0 against a tempdir copy of example-archive
(setUpClass skips when the fixture is absent, like test_person's round-trip
class). The security invariants are the whole trust boundary, so they are tested
as hard acceptance criteria: traversal, Host-header spoofing, CSRF, upload
sanitization, and /api/open confinement. Functional coverage proves the two-step
dry-run/apply flow: a dry run leaves the record tree byte-identical, and a live
claim.review flips a fixture claim, stamps `reviewed:`, echoes the exact CLI
command, and invalidates the snapshot so the next GET rebuilds it.

serve.py imports site.py under the private name `fha_site`; this test imports
serve.py directly (its own import wiring handles the site load).
"""

import hashlib
import http.client
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

EXAMPLE = ROOT / 'example-archive'

import serve  # noqa: E402
import index as index_mod  # noqa: E402
from _lib import load_fha_yaml  # noqa: E402


def _tree_hash(root: Path) -> str:
    """SHA-256 over the record trees (not .cache/, not assets) - the bytes a
    dry run must never change."""
    h = hashlib.sha256()
    for base in ('sources', 'people', 'places', 'notes'):
        for p in sorted((root / base).rglob('*')):
            if p.is_file():
                h.update(p.relative_to(root).as_posix().encode())
                h.update(p.read_bytes())
    return h.hexdigest()


class _ServeCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not EXAMPLE.is_dir():
            raise unittest.SkipTest('example-archive not present')

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / 'arc'
        shutil.copytree(EXAMPLE, self.root)
        self.config = load_fha_yaml(self.root, strict=True)
        index_mod.build_index(self.root, self.config)
        self.state = serve.ServeState(self.root, self.config, 0)
        serve.ensure_snapshot(self.state)
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), serve._Handler)
        self.httpd.state = self.state
        self.port = self.httpd.server_address[1]
        self.state.port = self.port
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self._tmp.cleanup()

    # - request helpers -

    def req(self, method, path, *, body=None, headers=None, host=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        h = {'Host': host or f'127.0.0.1:{self.port}'}
        if headers:
            h.update(headers)
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        status = r.status
        data = r.read()
        rheaders = dict(r.getheaders())
        conn.close()
        return status, data, rheaders

    def csrf(self):
        _s, d, _h = self.req('GET', '/')
        import re
        m = re.search(rb'name="fha-csrf" content="([0-9a-f]+)"', d)
        return m.group(1).decode() if m else None

    def post_run(self, verb, args, dry_run, csrf=True):
        headers = {'Content-Type': 'application/json'}
        if csrf:
            headers['X-FHA-CSRF'] = self.state.csrf_token
        body = json.dumps({'verb': verb, 'args': args, 'dry_run': dry_run})
        return self.req('POST', '/api/run', body=body, headers=headers)

    def a_suggested_claim(self):
        import sqlite3
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        try:
            row = conn.execute(
                "SELECT id, source_id FROM claims WHERE status='suggested' LIMIT 1").fetchone()
        finally:
            conn.close()
        return row


class HomeAndChromeTests(_ServeCase):
    def test_home_ok_with_servebar_and_csrf(self):
        s, d, h = self.req('GET', '/')
        self.assertEqual(s, 200)
        txt = d.decode('utf-8')
        self.assertIn('fha serve', txt)               # serve bar chrome
        self.assertIn('name="fha-csrf"', txt)          # CSRF meta tag
        self.assertEqual(h.get('Cache-Control'), 'no-store')
        self.assertEqual(h.get('X-Content-Type-Options'), 'nosniff')

    def test_person_page_uses_root_asset_urls_not_relative_escape(self):
        # Thomas Hartley has a portrait in the fixture.
        s, d, _h = self.req('GET', '/persons/p-de957bcda1.html')
        self.assertEqual(s, 200)
        txt = d.decode('utf-8')
        self.assertIn('record:', txt)                  # workbench record strip
        # Asset hrefs must be /root/ URLs, never ../../ escapes over HTTP.
        self.assertIn('/root/', txt)
        self.assertNotIn('src="../../', txt)
        self.assertNotIn('src="..\\', txt)

    def test_review_and_inbox_render(self):
        s, d, _h = self.req('GET', '/review')
        self.assertEqual(s, 200)
        self.assertIn('Review', d.decode('utf-8'))
        s, d, _h = self.req('GET', '/inbox')
        self.assertEqual(s, 200)
        self.assertIn('Inbox', d.decode('utf-8'))


class SecurityTests(_ServeCase):
    def test_traversal_variants_are_rejected(self):
        for path in ('/persons/../../fha.yaml', '/%2e%2e/fha.yaml',
                     '/persons/..%2f..%2ffha.yaml', '/root/photos/../../fha.yaml',
                     '/root/nope/x', '/root/documents/..%2f..%2ffha.yaml'):
            s, d, _h = self.req('GET', path)
            self.assertGreaterEqual(s, 400, f'{path} should be 4xx, got {s}')
            self.assertNotIn(b'root_person', d)   # fha.yaml never leaks

    def test_host_header_spoof_403(self):
        s, _d, _h = self.req('GET', '/', host='evil.example.com')
        self.assertEqual(s, 403)
        s, _d, _h = self.req('GET', '/', host='evil.example.com:1234')
        self.assertEqual(s, 403)

    def test_localhost_host_allowed(self):
        s, _d, _h = self.req('GET', '/', host=f'localhost:{self.port}')
        self.assertEqual(s, 200)

    def test_post_without_csrf_403(self):
        s, _d, _h = self.post_run('index.rebuild', {}, False, csrf=False)
        self.assertEqual(s, 403)

    def test_post_with_non_ascii_csrf_is_plain_403(self):
        # compare_digest on str raises TypeError for non-ASCII; the gate must
        # answer a garbage header with a plain 403, never an exception.
        s, _d, _h = self.req('POST', '/api/run', body=b'{}',
                             headers={'Content-Type': 'application/json',
                                      'X-FHA-CSRF': 'café'})
        self.assertEqual(s, 403)

    def test_snapshot_from_previous_session_is_stale(self):
        # The CSRF token is baked into snapshot pages, so a snapshot built by a
        # DIFFERENT serve process must read as stale even when no record
        # changed - otherwise every Apply after a restart 403s.
        self.assertFalse(serve.snapshot_is_stale(self.state))
        restarted = serve.ServeState(self.root, self.config, self.port)
        self.assertTrue(serve.snapshot_is_stale(restarted))

    def test_post_with_wrong_csrf_403(self):
        s, _d, _h = self.req('POST', '/api/run',
                             body=json.dumps({'verb': 'index.rebuild', 'dry_run': False}),
                             headers={'Content-Type': 'application/json',
                                      'X-FHA-CSRF': 'deadbeef' * 4})
        self.assertEqual(s, 403)


class FindTests(_ServeCase):
    def test_find_returns_results(self):
        s, d, h = self.req('GET', '/api/find?q=Hartley')
        self.assertEqual(s, 200)
        self.assertIn('application/json', h.get('Content-Type', ''))
        results = json.loads(d)['results']
        self.assertTrue(results)
        self.assertIn('id', results[0])
        self.assertIn('type', results[0])


class ApiRunTests(_ServeCase):
    def test_unknown_verb_400(self):
        s, d, _h = self.post_run('nope.explode', {}, True)
        self.assertEqual(s, 400)
        self.assertFalse(json.loads(d)['ok'])

    def test_extra_args_400(self):
        row = self.a_suggested_claim()
        self.assertIsNotNone(row)
        s, d, _h = self.post_run('claim.review',
                                 {'claim_id': row[0], 'status': 'accepted', 'bogus': 'x'}, True)
        self.assertEqual(s, 400)

    def test_dry_run_leaves_tree_byte_identical(self):
        row = self.a_suggested_claim()
        before = _tree_hash(self.root)
        s, d, _h = self.post_run('claim.review', {'claim_id': row[0], 'status': 'accepted'}, True)
        self.assertEqual(s, 200)
        payload = json.loads(d)
        self.assertIn('cli_echo', payload)
        self.assertEqual(_tree_hash(self.root), before, 'dry-run must write nothing')

    def test_default_dry_run_when_flag_absent(self):
        # A POST with no dry_run key is a preview (defense in depth).
        row = self.a_suggested_claim()
        before = _tree_hash(self.root)
        body = json.dumps({'verb': 'claim.review', 'args': {'claim_id': row[0], 'status': 'accepted'}})
        s, _d, _h = self.req('POST', '/api/run', body=body,
                             headers={'Content-Type': 'application/json',
                                      'X-FHA-CSRF': self.state.csrf_token})
        self.assertEqual(s, 200)
        self.assertEqual(_tree_hash(self.root), before, 'no explicit dry_run:false must not write')

    def test_live_claim_review_flips_and_invalidates(self):
        row = self.a_suggested_claim()
        cid = row[0]
        before = _tree_hash(self.root)
        s, d, _h = self.post_run('claim.review', {'claim_id': cid, 'status': 'accepted'}, False)
        self.assertEqual(s, 200)
        payload = json.loads(d)
        self.assertTrue(payload['ok'])
        self.assertIn('--status accepted', payload['cli_echo'])
        # The tree changed and the marker was invalidated (next GET rebuilds).
        self.assertNotEqual(_tree_hash(self.root), before)
        self.assertFalse(self.state.marker.exists())
        # The change landed: status accepted + reviewed stamped.
        src = None
        for f in (self.root / 'sources').rglob('*.md'):
            if cid.lower() in f.read_text(encoding='utf-8').lower():
                src = f
                break
        self.assertIsNotNone(src)
        text = src.read_text(encoding='utf-8')
        self.assertIn('status: accepted', text)
        self.assertIn('reviewed:', text)
        # Next GET rebuilds the marker.
        self.req('GET', '/')
        self.assertTrue(self.state.marker.exists())


class UploadTests(_ServeCase):
    def _multipart(self, filename, content, fields=None):
        boundary = '----testboundary1234'
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                 f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n']
        body = parts[0].encode() + content + b'\r\n'
        for k, v in (fields or {}).items():
            body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'
                     f'{v}\r\n').encode()
        body += f'--{boundary}--\r\n'.encode()
        return boundary, body

    def test_upload_sanitizes_and_writes_only_into_inbox(self):
        boundary, body = self._multipart('../evil.txt', b'payload',
                                         fields={'what': 'a smoke note'})
        s, d, _h = self.req('POST', '/api/upload', body=body,
                            headers={'Content-Type': f'multipart/form-data; boundary={boundary}',
                                     'X-FHA-CSRF': self.state.csrf_token})
        self.assertEqual(s, 200)
        self.assertTrue(json.loads(d)['ok'])
        inbox = self.root / 'inbox'
        self.assertTrue((inbox / 'evil.txt').is_file())         # basename only
        self.assertFalse((self.root / 'evil.txt').exists())     # no escape
        self.assertTrue((inbox / 'evil.txt.notes.md').is_file())  # sidecar written

    def test_upload_without_csrf_403(self):
        boundary, body = self._multipart('x.txt', b'x')
        s, _d, _h = self.req('POST', '/api/upload', body=body,
                             headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
        self.assertEqual(s, 403)

    def test_upload_rejects_windows_reserved_device_names(self):
        # CON.part (any extension) is the console device under classic Win32
        # semantics - a plain 400, never a write attempt.
        for bad in ('CON', 'con.txt', 'NUL.jpg', 'com1.pdf'):
            boundary, body = self._multipart(bad, b'x')
            s, _d, _h = self.req('POST', '/api/upload', body=body,
                                 headers={'Content-Type':
                                          f'multipart/form-data; boundary={boundary}',
                                          'X-FHA-CSRF': self.state.csrf_token})
            self.assertEqual(s, 400, bad)
        # Trailing dots are stripped (Win32 does it silently on write); the
        # remaining plain name is accepted.
        boundary, body = self._multipart('evil...', b'x')
        s, _d, _h = self.req('POST', '/api/upload', body=body,
                             headers={'Content-Type':
                                      f'multipart/form-data; boundary={boundary}',
                                      'X-FHA-CSRF': self.state.csrf_token})
        self.assertEqual(s, 200)
        self.assertTrue((self.root / 'inbox' / 'evil').is_file())

    def test_upload_note_newlines_cannot_inject_frontmatter_keys(self):
        boundary, body = self._multipart('note-inject.txt', b'x',
                                         fields={'what': 'a scan\nliving: hacked'})
        s, _d, _h = self.req('POST', '/api/upload', body=body,
                             headers={'Content-Type':
                                      f'multipart/form-data; boundary={boundary}',
                                      'X-FHA-CSRF': self.state.csrf_token})
        self.assertEqual(s, 200)
        sidecar = (self.root / 'inbox' / 'note-inject.txt.notes.md').read_text(encoding='utf-8')
        self.assertNotIn('\nliving:', sidecar)


class OpenTests(_ServeCase):
    def test_open_confinement(self):
        opened = {}
        orig = serve._os_open
        serve._os_open = lambda p: opened.setdefault('p', p)
        try:
            # In-archive file: allowed, _os_open invoked.
            s, _d, _h = self.req('POST', '/api/open', body=json.dumps({'path': 'fha.yaml'}),
                                 headers={'Content-Type': 'application/json',
                                          'X-FHA-CSRF': self.state.csrf_token})
            self.assertEqual(s, 200)
            self.assertIn('p', opened)
            # Outside the archive: refused, _os_open never called.
            opened.clear()
            s, _d, _h = self.req('POST', '/api/open',
                                 body=json.dumps({'path': '../../etc/passwd'}),
                                 headers={'Content-Type': 'application/json',
                                          'X-FHA-CSRF': self.state.csrf_token})
            self.assertIn(s, (403, 404))
            self.assertNotIn('p', opened)
        finally:
            serve._os_open = orig


class DisposabilityTests(_ServeCase):
    def test_delete_serve_snapshot_leaves_site_unchanged(self):
        site = serve._load_site_module()
        out_a = Path(self._tmp.name) / 'site_a'
        r = site.run_site(self.root, out_a, linked=False)
        self.assertTrue(r.ok)
        files_a = sorted(p.relative_to(out_a).as_posix()
                         for p in out_a.rglob('*') if p.is_file())
        # A serve session (snapshot already built in setUp); now delete it.
        shutil.rmtree(self.root / '.cache' / 'serve')
        out_b = Path(self._tmp.name) / 'site_b'
        r = site.run_site(self.root, out_b, linked=False)
        self.assertTrue(r.ok)
        files_b = sorted(p.relative_to(out_b).as_posix()
                         for p in out_b.rglob('*') if p.is_file())
        self.assertEqual(files_a, files_b,
                         'deleting .cache/serve must not change the standalone site')


class PreflightTests(_ServeCase):
    def test_preflight_ok(self):
        r = serve.run_serve_preflight(self.root, port=0)
        self.assertTrue(r.ok)
        self.assertEqual(r.data['status'], 'ok')

    def test_preflight_port_busy(self):
        # The class already holds self.port; a second bind must be refused.
        r = serve.run_serve_preflight(self.root, port=self.port)
        self.assertFalse(r.ok)
        self.assertEqual(r.data['status'], 'port-busy')
        self.assertTrue(any('busy' in m.text for m in r.messages))


if __name__ == '__main__':
    unittest.main()
