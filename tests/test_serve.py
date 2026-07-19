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

import argparse
import contextlib
import hashlib
import http.client
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

EXAMPLE = ROOT / 'example-archive'

import serve  # noqa: E402
import index as index_mod  # noqa: E402
import process  # noqa: E402
from _lib import load_fha_yaml, read_record  # noqa: E402


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
        # 'Review'/'Inbox' appear in the servebar nav on every page, so those
        # words alone cannot tell a working queue render from a broken one -
        # assert the actual queue-item markup and a real fixture claim's
        # value/inbox item name instead.
        review_data = serve.gather_review(self.state)
        self.assertTrue(review_data['items'], 'fixture must have a suggested claim for this to prove anything')
        s, d, _h = self.req('GET', '/review')
        self.assertEqual(s, 200)
        txt = d.decode('utf-8')
        self.assertIn('class="queue-item"', txt)
        self.assertIn(review_data['items'][0]['headline'], txt)

        inbox_data = serve.gather_inbox(self.state)
        self.assertTrue(inbox_data['items'], 'fixture must have inbox contents for this to prove anything')
        s, d, _h = self.req('GET', '/inbox')
        self.assertEqual(s, 200)
        txt = d.decode('utf-8')
        self.assertIn('class="wb-inbox-list"', txt)
        self.assertIn(inbox_data['items'][0]['name'], txt)


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

    def test_find_unknown_kind_is_a_plain_400(self):
        # Same rule as an unknown verb: a typo'd kind must be a named 400,
        # never a silently empty result set.
        s, d, _h = self.req('GET', '/api/find?q=Hartley&kind=persno')
        self.assertEqual(s, 400)
        self.assertIn('persno', d.decode('utf-8'))

    def test_find_valid_kind_filter_still_works(self):
        s, d, _h = self.req('GET', '/api/find?q=Hartley&kind=person')
        self.assertEqual(s, 200)
        results = json.loads(d)['results']
        self.assertTrue(all(r['type'] == 'person' for r in results))


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

    def test_reindex_failure_after_a_successful_write_is_a_warning_not_a_false_failure(self):
        # P2 codex finding (round 5, PR #30): the engine write and its
        # follow-up reindex/snapshot-invalidation used to share one
        # try/except - a failure in the FOLLOW-UP step escaped run_api_run
        # entirely, hit do_POST's generic handler, and answered 500/"internal
        # error", which the workbench renders as "Nothing was written" even
        # though the record was already saved. The write must be reported as
        # what it was: a success, with a warning that the refresh needs a
        # manual `fha index`.
        row = self.a_suggested_claim()
        cid = row[0]
        real_reindex_after = serve._reindex_after

        def flaky_reindex_after(state, verb, result):
            raise RuntimeError('simulated index corruption')

        serve._reindex_after = flaky_reindex_after
        try:
            s, d, _h = self.post_run('claim.review', {'claim_id': cid, 'status': 'accepted'}, False)
        finally:
            serve._reindex_after = real_reindex_after
        self.assertEqual(s, 200)
        payload = json.loads(d)
        self.assertTrue(payload['ok'], payload)   # the write itself succeeded
        self.assertTrue(payload['changed'])       # and is reported as such
        msg = payload['messages'][-1]
        self.assertEqual(msg['level'], 'warning')
        self.assertIn('could not refresh automatically', msg['text'])
        self.assertEqual(msg['next_step'], 'fha index')
        # The record really was written to disk, despite the follow-up failure.
        src = None
        for f in (self.root / 'sources').rglob('*.md'):
            if cid.lower() in f.read_text(encoding='utf-8').lower():
                src = f
                break
        self.assertIsNotNone(src)
        self.assertIn('status: accepted', src.read_text(encoding='utf-8'))

    def test_person_new_apply_reuses_the_previewed_minted_id(self):
        # P2 codex finding (round 5, PR #30): the workbench's dry-run preview
        # calls person.new and shows a real minted P-id, but Apply used to
        # call the SAME engine again with dry_run:false - drawing a second,
        # DIFFERENT random id, so the record actually created never matched
        # what the preview showed. `/api/run`'s person.new verb now threads
        # a client-supplied person_id straight through to person.run_new, so
        # a client that reuses the preview's id gets that exact record.
        s, d, _h = self.post_run('person.new', {'name': 'Preview Reuse Test'}, True)
        self.assertEqual(s, 200)
        previewed = json.loads(d)['data']['person_id']
        s, d, _h = self.post_run(
            'person.new', {'name': 'Preview Reuse Test', 'person_id': previewed}, False)
        self.assertEqual(s, 200)
        payload = json.loads(d)
        self.assertTrue(payload['ok'], payload)
        self.assertEqual(payload['data']['person_id'], previewed)

    def test_claim_new_apply_reuses_the_previewed_minted_id(self):
        row = self.a_suggested_claim()
        sid = row[1]
        args = {'source_id': sid, 'claim_type': 'occupation', 'value': 'Reuse test claim'}
        s, d, _h = self.post_run('claim.new', args, True)
        self.assertEqual(s, 200)
        previewed = json.loads(d)['data']['claim_id']
        s, d, _h = self.post_run('claim.new', dict(args, claim_id=previewed), False)
        self.assertEqual(s, 200)
        payload = json.loads(d)
        self.assertTrue(payload['ok'], payload)
        self.assertEqual(payload['data']['claim_id'], previewed)

    def test_process_file_apply_reuses_the_previewed_minted_source_id(self):
        # P2 codex finding (round 7, PR #30): same preview/apply mismatch as
        # person.new/claim.new above, for `process.file` - the dry-run
        # preview mints and shows a real S-id, but Apply used to call
        # run_process again with no way to reuse it, drawing a second,
        # different id. `process.file`'s schema now carries a `source_id`
        # round-trip key the same way, threaded through to
        # `_mint_one_source_id`'s override.
        target = self.root / 'documents' / 'transcripts' / 'new-interview.txt'
        target.write_text('a fresh transcript', encoding='utf-8')
        s, d, _h = self.post_run(
            'process.file', {'file': 'documents/transcripts/new-interview.txt'}, True)
        self.assertEqual(s, 200)
        preview_payload = json.loads(d)
        self.assertTrue(preview_payload['ok'], preview_payload)
        previewed = preview_payload['data']['source_id']
        self.assertTrue(previewed and previewed.upper().startswith('S-'))
        self.assertTrue(target.exists())  # dry run wrote nothing

        s, d, _h = self.post_run(
            'process.file',
            {'file': 'documents/transcripts/new-interview.txt', 'source_id': previewed},
            False)
        self.assertEqual(s, 200)
        payload = json.loads(d)
        self.assertTrue(payload['ok'], payload)
        self.assertEqual(payload['data']['source_id'], previewed)
        records = [
            f for f in (self.root / 'sources').rglob('*.md')
            if previewed.lower() in f.read_text(encoding='utf-8').lower()
        ]
        self.assertEqual(len(records), 1)

    def test_live_claim_review_place_id_writes_structured_place(self):
        # P2 codex finding (PR #30): the workbench claim-edit modal's place
        # lookup had no id target, so a place picked from the lookup could
        # only ever be submitted as `place_text` (a wikilink), never the
        # structured `place` L-id the picker promised to resolve. The
        # frontend fix lives in workbench.js/_modals.html (no JS test
        # harness in this repo); this locks in the server-side contract
        # those templates now drive - `place` alone reaches `run_claim` and
        # writes a real `place:` L-id, exactly like `fha claim --place`.
        row = self.a_suggested_claim()
        cid = row[0]
        s, d, _h = self.post_run(
            'claim.review', {'claim_id': cid, 'place': 'L-7c1a9f4e22'}, False)
        self.assertEqual(s, 200)
        payload = json.loads(d)
        self.assertTrue(payload['ok'])
        self.assertIn('--place L-7c1a9f4e22', payload['cli_echo'])
        src = None
        for f in (self.root / 'sources').rglob('*.md'):
            if cid.lower() in f.read_text(encoding='utf-8').lower():
                src = f
                break
        self.assertIsNotNone(src)
        text = src.read_text(encoding='utf-8')
        self.assertIn('place: L-7c1a9f4e22', text)
        # The source-as-written wording survives the id edit (SPEC §15:
        # backfilling place: never alters place_text; P2 codex finding,
        # round 4, PR #31 - the old switch-removes-the-other behavior made
        # any workbench claim edit erase it).
        self.assertIn('place_text: "Fairview, Kansas"', text)


class SnapshotIndexFreshnessTests(_ServeCase):
    """P2 codex finding (round 3, PR #30): `ensure_snapshot` used to check
    `snapshot_is_stale` (which DOES watch record-file mtimes) but then call
    `run_site` without first rebuilding a stale `.cache/index.sqlite` -
    `open_index_db(..., strict=False)` only warns and still hands back rows
    from before the edit. The rebuilt snapshot then got stamped `.built`
    over stale content, hiding a record edit made outside serve until some
    unrelated file happened to bump the marker's staleness check again."""

    def test_ensure_snapshot_rebuilds_a_stale_index_before_rendering(self):
        import sqlite3
        row = self.a_suggested_claim()
        self.assertIsNotNone(row)
        cid, _sid = row
        src_path = None
        for f in (self.root / 'sources').rglob('*.md'):
            if cid.lower() in f.read_text(encoding='utf-8').lower():
                src_path = f
                break
        self.assertIsNotNone(src_path)

        # Add a brand-new claim item to the ## Claims block by hand - exactly
        # what a human editing the file directly (outside serve) would do.
        # This bumps the file's mtime past the already-built snapshot AND
        # leaves the index (built in setUp) with no row for the new claim.
        lines = src_path.read_text(encoding='utf-8').splitlines()
        heading_idx = next(i for i, ln in enumerate(lines) if ln.strip() == '## Claims')
        close_idx = next(i for i in range(heading_idx, len(lines)) if lines[i].strip() == '```')
        new_claim_id = 'C-zzzzzzzzzz'
        lines[close_idx:close_idx] = [
            '- value: "A brand-new fact added outside serve"',
            f'  id: {new_claim_id}',
            '  type: note',
            '  status: suggested',
        ]
        src_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')

        serve.invalidate_snapshot(self.state)
        self.assertTrue(serve.snapshot_is_stale(self.state))
        serve.ensure_snapshot(self.state)

        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        try:
            found = conn.execute(
                'SELECT 1 FROM claims WHERE id = ?', (new_claim_id.lower(),)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(
            found, 'ensure_snapshot must rebuild the index before rendering the snapshot')


class PhotoIndexStalenessTests(_ServeCase):
    """P2 codex finding (round 3, PR #30): photo strips/portraits/captions
    render straight from `.cache/photos.sqlite`, but the staleness probe
    didn't watch it - a `fha photoindex scan`/`tag-person`/`set-summary`
    run while serve stayed open left the snapshot looking fresh over a
    stale catalog until an unrelated watched file happened to change."""

    def test_touching_photos_sqlite_marks_the_snapshot_stale(self):
        serve.ensure_snapshot(self.state)   # fresh baseline (setUp already built one)
        self.assertFalse(serve.snapshot_is_stale(self.state))
        photos_db = self.root / '.cache' / 'photos.sqlite'
        photos_db.write_text('', encoding='utf-8')
        future = time.time() + 5
        os.utime(photos_db, (future, future))
        # Bypass the short-TTL mtime memo (_MEMO_TTL, unrelated to this fix)
        # so the freshly-touched file is seen immediately rather than
        # possibly within the same 1s cache window as the baseline check.
        self.state._mtime_memo = None
        self.assertTrue(serve.snapshot_is_stale(self.state))


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
        # The sidecar is named after the ASSET's stem (`evil`), never the full
        # `evil.txt` name - that is the form process.py's _find_sidecar looks
        # for (SPEC §12.1); the old `evil.txt.notes.md` form was never found.
        self.assertTrue((inbox / 'evil.notes.md').is_file())
        self.assertFalse((inbox / 'evil.txt.notes.md').exists())

    def test_upload_with_note_sidecar_is_found_by_fha_process(self):
        # End-to-end proof of fix 1: an uploaded note is not just SHAPED like
        # a sidecar, `fha process` must actually consume it.
        boundary, body = self._multipart(
            'grandpa-letter.txt', b'payload',
            fields={'what': 'A letter grandpa wrote in 1945.', 'who': 'Grandpa Joe'})
        s, d, _h = self.req('POST', '/api/upload', body=body,
                            headers={'Content-Type': f'multipart/form-data; boundary={boundary}',
                                     'X-FHA-CSRF': self.state.csrf_token})
        self.assertEqual(s, 200)
        self.assertTrue(json.loads(d)['ok'])
        inbox = self.root / 'inbox'
        asset = inbox / 'grandpa-letter.txt'
        sidecar = inbox / 'grandpa-letter.notes.md'
        self.assertTrue(asset.is_file())
        self.assertTrue(sidecar.is_file())
        found = process._find_sidecar(asset)
        self.assertEqual(found, sidecar)
        meta, note_body = process._read_sidecar(sidecar)
        # The "what" text is prose body (-> ## Notes); the "who" hint is under
        # `people:`, the field _read_sidecar actually reads for names, so it
        # is folded into that same body rather than silently dropped.
        self.assertIn('A letter grandpa wrote in 1945.', note_body)
        self.assertIn('Grandpa Joe', note_body)

    def test_upload_note_sidecar_write_failure_is_reported_not_swallowed(self):
        # P2 codex finding (PR #30): a sidecar write failure AFTER the asset
        # bytes are already saved used to be swallowed (`except OSError:
        # pass`) - the API still answered 200 with no hint the note was
        # lost. It must now surface as a warning, and `changed` must not
        # falsely claim the sidecar was written.
        boundary, body = self._multipart(
            'sidecar-fail.txt', b'payload',
            fields={'what': 'a note that will not save', 'who': 'Someone'})

        real_write_text = Path.write_text

        def _flaky_write_text(self_path, *a, **kw):
            if self_path.name.endswith('.notes.md'):
                raise OSError('simulated disk failure')
            return real_write_text(self_path, *a, **kw)

        with mock.patch.object(Path, 'write_text', _flaky_write_text):
            s, d, _h = self.req('POST', '/api/upload', body=body,
                                headers={'Content-Type': f'multipart/form-data; boundary={boundary}',
                                         'X-FHA-CSRF': self.state.csrf_token})
        self.assertEqual(s, 200)
        payload = json.loads(d)
        self.assertTrue(payload['ok'])   # the asset itself is safe
        inbox = self.root / 'inbox'
        asset = inbox / 'sidecar-fail.txt'
        sidecar = inbox / 'sidecar-fail.notes.md'
        self.assertTrue(asset.is_file())
        self.assertFalse(sidecar.exists())                 # genuinely never landed
        self.assertEqual(payload['changed'], [str(asset)])  # not falsely reported as written
        msg = payload['messages'][0]
        self.assertEqual(msg['level'], 'warning')
        self.assertIn('note could not be saved', msg['text'])

    def test_upload_snapshot_invalidation_failure_is_a_warning_not_a_false_refusal(self):
        # P2 codex finding (round 6, PR #30): the round-5 refresh-failure fix
        # covered /api/run's invalidate_snapshot call but not /api/upload's -
        # a failure there (e.g. the .built marker locked/read-only) escaped
        # uncaught to do_POST's generic 500 handler, which the workbench
        # renders as "Upload refused" for a file that was already safely
        # written - inviting a duplicate re-upload. It must report success
        # with a warning instead.
        boundary, body = self._multipart('snapshot-fail.txt', b'payload')
        real_invalidate = serve.invalidate_snapshot

        def _flaky_invalidate(state):
            raise OSError('simulated marker unlink failure')

        serve.invalidate_snapshot = _flaky_invalidate
        try:
            s, d, _h = self.req('POST', '/api/upload', body=body,
                                headers={'Content-Type': f'multipart/form-data; boundary={boundary}',
                                         'X-FHA-CSRF': self.state.csrf_token})
        finally:
            serve.invalidate_snapshot = real_invalidate
        self.assertEqual(s, 200)
        payload = json.loads(d)
        self.assertTrue(payload['ok'], payload)   # the file itself is safe
        asset = self.root / 'inbox' / 'snapshot-fail.txt'
        self.assertTrue(asset.is_file())
        self.assertEqual(payload['changed'], [str(asset)])
        msg = payload['messages'][-1]
        self.assertEqual(msg['level'], 'warning')
        self.assertIn('could not refresh automatically', msg['text'])

    def test_upload_note_bumps_stem_when_only_the_sidecar_collides(self):
        # The asset name `photo.jpg` has no collision, but an unrelated OLDER
        # stub already holds its sidecar's stem (`photo.notes.md`) - both the
        # asset and its new sidecar must bump together, not just the sidecar,
        # or the new note would end up split from the file it describes.
        inbox = self.root / 'inbox'
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / 'photo.notes.md').write_text(
            '---\nnoted: 2020-01-01\n---\n\nan older stub\n', encoding='utf-8')
        boundary, body = self._multipart('photo.jpg', b'bytes', fields={'what': 'a new note'})
        s, d, _h = self.req('POST', '/api/upload', body=body,
                            headers={'Content-Type': f'multipart/form-data; boundary={boundary}',
                                     'X-FHA-CSRF': self.state.csrf_token})
        self.assertEqual(s, 200)
        self.assertTrue(json.loads(d)['ok'])
        self.assertTrue((inbox / 'photo -2.jpg').is_file())
        self.assertTrue((inbox / 'photo -2.notes.md').is_file())
        self.assertFalse((inbox / 'photo.jpg').exists())
        # The pre-existing, unrelated stub is untouched.
        self.assertIn('an older stub', (inbox / 'photo.notes.md').read_text(encoding='utf-8'))

    def test_upload_without_a_note_still_bumps_on_a_stranger_sidecar(self):
        # P2 codex finding (round 4, PR #30): the collision check used to
        # only look at an existing same-stem sidecar when THIS upload also
        # carried a note (`has_note`). Uploading a bare `photo.jpg` with no
        # note next to an unrelated pre-existing `photo.notes.md` used to
        # sail straight through - `fha process`/`gather_inbox` pair an asset
        # with ANY same-stem sidecar by stem alone, so that stranger's note
        # would silently get filed onto this new, unrelated source.
        inbox = self.root / 'inbox'
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / 'photo.notes.md').write_text(
            '---\nnoted: 2020-01-01\n---\n\nan older, unrelated stub\n', encoding='utf-8')
        boundary, body = self._multipart('photo.jpg', b'bytes')  # no what/who fields
        s, d, _h = self.req('POST', '/api/upload', body=body,
                            headers={'Content-Type': f'multipart/form-data; boundary={boundary}',
                                     'X-FHA-CSRF': self.state.csrf_token})
        self.assertEqual(s, 200)
        self.assertTrue(json.loads(d)['ok'])
        self.assertTrue((inbox / 'photo -2.jpg').is_file())
        self.assertFalse((inbox / 'photo.jpg').exists())
        self.assertFalse((inbox / 'photo -2.notes.md').exists())  # no note was given
        # The pre-existing, unrelated stub is untouched and still paired only
        # with the ORIGINAL `photo.jpg` stem, not silently claimed by the new upload.
        self.assertIn('an older, unrelated stub', (inbox / 'photo.notes.md').read_text(encoding='utf-8'))

    def test_upload_bumps_destination_when_a_part_file_already_exists(self):
        # P2 codex finding (round 3, PR #30): the write lands at
        # `<dest>.part` first (write-then-atomic-replace). A pre-existing
        # `letter.pdf.part` - a genuine partial download someone left in the
        # inbox - has no matching `letter.pdf`, so the old collision check
        # (dest.exists() / sidecar.exists() only) missed it entirely and the
        # upload's temp write clobbered that `.part` file before the rename
        # to `letter.pdf` ever ran.
        inbox = self.root / 'inbox'
        inbox.mkdir(parents=True, exist_ok=True)
        partial = inbox / 'letter.pdf.part'
        partial.write_bytes(b'a partial download in progress')
        boundary, body = self._multipart('letter.pdf', b'the real upload')
        s, d, _h = self.req('POST', '/api/upload', body=body,
                            headers={'Content-Type': f'multipart/form-data; boundary={boundary}',
                                     'X-FHA-CSRF': self.state.csrf_token})
        self.assertEqual(s, 200)
        self.assertTrue(json.loads(d)['ok'])
        # The pre-existing partial download survives untouched...
        self.assertEqual(partial.read_bytes(), b'a partial download in progress')
        # ...and the new upload landed at a bumped name instead of clobbering it.
        self.assertTrue((inbox / 'letter -2.pdf').is_file())
        self.assertEqual((inbox / 'letter -2.pdf').read_bytes(), b'the real upload')
        self.assertFalse((inbox / 'letter.pdf').exists())

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
        # 'what' lands in the prose body now, not frontmatter, but 'who' still
        # writes a `people:` frontmatter value - an embedded newline there
        # must still collapse to one line rather than splice a second key in.
        boundary, body = self._multipart(
            'note-inject.txt', b'x',
            fields={'what': 'a scan\nliving: hacked', 'who': 'Grandma\nliving: hacked'})
        s, _d, _h = self.req('POST', '/api/upload', body=body,
                             headers={'Content-Type':
                                      f'multipart/form-data; boundary={boundary}',
                                      'X-FHA-CSRF': self.state.csrf_token})
        self.assertEqual(s, 200)
        sidecar_path = self.root / 'inbox' / 'note-inject.notes.md'
        sidecar = sidecar_path.read_text(encoding='utf-8')
        self.assertNotIn('\nliving:', sidecar)
        # The sidecar still parses cleanly as one frontmatter block + body.
        rec = read_record(sidecar_path)
        self.assertEqual(rec['parse_errors'], [])


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

    def test_open_resolves_an_externally_configured_inbox_root(self):
        # P2 codex finding (round 2, PR #30): `gather_inbox` hands the open/
        # process APIs a relative `inbox/<name>` path assuming it always
        # means `archive_root/inbox/<name>` - but fha.yaml's `roots:` may
        # point `inbox` OUTSIDE the archive (same config surface as
        # photos/documents), in which case that assumption is simply wrong
        # and every Inbox button 404'd on a file that genuinely exists.
        ext_tmp = tempfile.TemporaryDirectory()
        try:
            ext_inbox = Path(ext_tmp.name)
            (ext_inbox / 'letter.txt').write_text('hi', encoding='utf-8')
            self.state.fha_config = dict(self.state.fha_config)
            self.state.fha_config['roots'] = dict(self.state.fha_config.get('roots') or {})
            self.state.fha_config['roots']['inbox'] = str(ext_inbox)

            opened = {}
            orig = serve._os_open
            serve._os_open = lambda p: opened.setdefault('p', p)
            try:
                s, _d, _h = self.req(
                    'POST', '/api/open', body=json.dumps({'path': 'inbox/letter.txt'}),
                    headers={'Content-Type': 'application/json',
                             'X-FHA-CSRF': self.state.csrf_token})
                self.assertEqual(s, 200)
                self.assertEqual(Path(opened.get('p')).resolve(),
                                 (ext_inbox / 'letter.txt').resolve())
            finally:
                serve._os_open = orig
        finally:
            ext_tmp.cleanup()


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

    def test_preflight_refuses_working_copy_mode(self):
        # P2 codex finding (round 2, PR #30): fha serve must refuse a
        # working-copy archive up front, naming the fix - not silently
        # start, build an empty snapshot, and 404 every workbench page.
        (self.root / 'WORKING_COPY').write_text('', encoding='utf-8')
        try:
            r = serve.run_serve_preflight(self.root, port=0)
            self.assertFalse(r.ok)
            self.assertEqual(r.data['status'], 'working-copy')
            self.assertTrue(any('working-copy' in m.text for m in r.messages))
        finally:
            (self.root / 'WORKING_COPY').unlink()


class EnsureSnapshotWorkingCopyTests(_ServeCase):
    """P2 codex finding (round 2, PR #30): even with the preflight refusal
    above, the archive can transition into working-copy mode DURING a
    long-running serve session. `ensure_snapshot` must not stamp `.built`
    over a snapshot `run_site` reports as working-copy (ok=True, zero
    pages) - defense in depth alongside the preflight check."""

    def test_working_copy_result_does_not_stamp_built_marker(self):
        self.state.marker.unlink(missing_ok=True)
        real_run_site = self.state.site_mod.run_site
        self.state.site_mod.run_site = lambda *a, **kw: serve.Result(
            ok=True, exit_code=serve.EXIT_CLEAN,
            data={'status': 'working-copy', 'out_dir': str(self.state.snapshot_dir), 'pages': []},
        )
        try:
            serve.ensure_snapshot(self.state)
            self.assertFalse(self.state.marker.exists())
            self.assertTrue(serve.snapshot_is_stale(self.state))
        finally:
            self.state.site_mod.run_site = real_run_site

    def test_ensure_snapshot_returns_the_result_when_a_rebuild_was_attempted(self):
        # P2 codex finding (round 6, PR #30): callers need to tell "already
        # fresh, nothing done" (None) apart from "a rebuild ran" (the
        # run_site Result) so they can refuse a request rather than
        # silently serve a stale/failed snapshot - see GetRefusesStale...
        # below for the HTTP-level half of this fix.
        self.assertIsNone(serve.ensure_snapshot(self.state))   # already fresh from setUp
        self.state.marker.unlink(missing_ok=True)
        result = serve.ensure_snapshot(self.state)
        self.assertIsNotNone(result)
        self.assertTrue(result.ok)


class GetRefusesStaleSnapshotOnRebuildFailureTests(_ServeCase):
    """P2 codex finding (round 6, PR #30): `ensure_snapshot` avoided stamping
    `.built` over a failed/working-copy rebuild (round 2's fix), but every
    `do_GET` route called it and then read `.cache/serve/site/` regardless -
    silently serving whatever snapshot files an earlier, now-stale build
    left behind, with no sign anything was wrong."""

    def tearDown(self):
        # site_mod is the one process-wide cached module (sys.modules
        # caching in _load_site_module), not a fresh copy per ServeState -
        # a run_site patch left in place would leak into the NEXT test's
        # setUp, which builds its own "prior good snapshot" before the test
        # body ever runs.
        if hasattr(self, '_real_run_site'):
            self.state.site_mod.run_site = self._real_run_site
        super().tearDown()

    def _force_rebuild_to_fail(self):
        self.state.marker.unlink(missing_ok=True)   # snapshot_is_stale() -> True
        self._real_run_site = self.state.site_mod.run_site
        self.state.site_mod.run_site = lambda *a, **kw: serve.Result(
            ok=False, exit_code=serve.EXIT_FAILURE,
        ).add('error', 'simulated: fha.yaml is broken')

    def test_static_page_refuses_rather_than_serving_a_stale_snapshot(self):
        # A real snapshot already exists from setUp (a prior good build).
        self.assertTrue(self.state.marker.exists())
        self._force_rebuild_to_fail()
        s, d, _h = self.req('GET', '/')
        self.assertEqual(s, 503)
        self.assertIn('simulated: fha.yaml is broken', d.decode('utf-8'))

    def test_review_page_refuses_rather_than_serving_stale(self):
        self._force_rebuild_to_fail()
        s, d, _h = self.req('GET', '/review')
        self.assertEqual(s, 503)
        self.assertIn('simulated: fha.yaml is broken', d.decode('utf-8'))

    def test_inbox_page_refuses_rather_than_serving_stale(self):
        self._force_rebuild_to_fail()
        s, d, _h = self.req('GET', '/inbox')
        self.assertEqual(s, 503)
        self.assertIn('simulated: fha.yaml is broken', d.decode('utf-8'))

    def test_preflight_port_busy(self):
        # The class already holds self.port; a second bind must be refused.
        r = serve.run_serve_preflight(self.root, port=self.port)
        self.assertFalse(r.ok)
        self.assertEqual(r.data['status'], 'port-busy')
        self.assertTrue(any('busy' in m.text for m in r.messages))


class ConfineAssetPathExternalRootTests(_ServeCase):
    """P2 codex finding (round 2, PR #30): `_confine_asset_path` (the gate
    `process.file` - the Inbox "File as a source…" button - runs every path
    through) has the same externally-configured-root gap as `run_api_open`,
    since both used to join a relative path straight onto `archive_root`."""

    def test_confine_asset_path_resolves_an_external_inbox_root(self):
        ext_tmp = tempfile.TemporaryDirectory()
        try:
            ext_inbox = Path(ext_tmp.name)
            (ext_inbox / 'letter.txt').write_text('hi', encoding='utf-8')
            self.state.fha_config = dict(self.state.fha_config)
            self.state.fha_config['roots'] = dict(self.state.fha_config.get('roots') or {})
            self.state.fha_config['roots']['inbox'] = str(ext_inbox)

            resolved, err = serve._confine_asset_path(self.state, 'inbox/letter.txt')
            self.assertIsNone(err)
            self.assertEqual(resolved.resolve(), (ext_inbox / 'letter.txt').resolve())
        finally:
            ext_tmp.cleanup()


class ProcessVerbExternalRootTests(_ServeCase):
    """P2 codex finding (round 4, PR #30): `_verb_process` ran `raw` through
    `_confine_asset_path` (which understands an externally-configured
    `roots.inbox`, per `ConfineAssetPathExternalRootTests` above) but then
    discarded the resolved path and handed the ENGINE the raw alias string -
    `process.py`'s own `_resolve_input_file` only retries a relative path
    under `archive_root`, so an external inbox root still 404'd inside the
    engine even though the confinement gate itself passed."""

    def test_verb_process_passes_the_confined_path_not_the_raw_alias(self):
        ext_tmp = tempfile.TemporaryDirectory()
        try:
            ext_inbox = Path(ext_tmp.name)
            (ext_inbox / 'scan.jpg').write_bytes(b'not-a-real-jpeg')
            self.state.fha_config = dict(self.state.fha_config)
            self.state.fha_config['roots'] = dict(self.state.fha_config.get('roots') or {})
            self.state.fha_config['roots']['inbox'] = str(ext_inbox)

            seen = {}
            real_run_process = serve.process_mod.run_process

            def spy(ns):
                seen['file'] = ns.file
                return serve.Result(ok=True, exit_code=serve.EXIT_CLEAN)

            serve.process_mod.run_process = spy
            try:
                serve._verb_process(self.state, {'file': 'inbox/scan.jpg'}, True)
            finally:
                serve.process_mod.run_process = real_run_process

            self.assertIn('file', seen)
            self.assertEqual(Path(seen['file']).resolve(), (ext_inbox / 'scan.jpg').resolve())
        finally:
            ext_tmp.cleanup()


class CapturePathVerbTests(_ServeCase):
    """Fix 2: `capture.path` registers a file OUTSIDE the archive on purpose -
    no `_confine_asset_path` gate - but a RELATIVE path must resolve against
    the archive root, not this test process's own working directory."""

    def test_out_of_archive_path_is_not_confined(self):
        outside = Path(self._tmp.name) / 'elsewhere' / 'grandma.jpg'
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_bytes(b'not-a-real-jpeg')
        s, d, _h = self.post_run('capture.path', {'path': str(outside)}, True)
        self.assertEqual(s, 200)
        payload = json.loads(d)
        self.assertTrue(payload['ok'], payload)
        texts = ' '.join(m['text'] for m in payload['messages'])
        self.assertNotIn('outside the archive', texts)

    def test_relative_path_resolves_against_archive_root_not_cwd(self):
        target = self.root / 'some-scan.jpg'
        target.write_bytes(b'bytes')
        # The fixture's inbox/ already holds an unrelated stub - name the new
        # one by the slug `run_capture_path` mints from the target's own
        # stem, rather than assume it is the only *.notes.md in the folder.
        stub_path = self.root / 'inbox' / 'some-scan.notes.md'
        self.assertFalse(stub_path.exists())
        s, d, _h = self.post_run('capture.path', {'path': 'some-scan.jpg'}, False)
        self.assertEqual(s, 200)
        payload = json.loads(d)
        self.assertTrue(payload['ok'], payload)
        self.assertTrue(stub_path.is_file())
        rec = read_record(stub_path)
        self.assertEqual(rec['meta']['asset_path_absolute'],
                         str(target.resolve()).replace('\\', '/'))
        # P2 codex finding (round 6, PR #30): `asset_path` must stay exactly
        # what the human typed - the archive-root resolution above is for
        # the existence check / `asset_path_absolute` only. It used to
        # overwrite `asset_path` with that same machine-specific absolute
        # path, contrary to TOOLING §13b ("asset_path is the path exactly as
        # the human typed it") and to what `fha capture --path` itself
        # stores for the identical relative-path case.
        self.assertEqual(rec['meta']['asset_path'], 'some-scan.jpg')


class ThreadTeeTests(unittest.TestCase):
    """Fix 4: a plain `contextlib.redirect_stdout` is process-global and races
    when two request threads both drive engines that print. `_ThreadTee`
    routes each write by the CALLING thread instead, so a concurrent thread
    with no buffer of its own installed always reaches the real stream."""

    def test_write_from_another_thread_reaches_real_stream_not_the_buffer(self):
        real = io.StringIO()
        tee = serve._ThreadTee(real)
        # A "slow verb" on the main thread holds its own capture buffer...
        held_buffer = io.StringIO()
        tee.set_buffer(held_buffer)
        try:
            done = threading.Event()

            def other_thread_write():
                # ...while a concurrent GET on ANOTHER thread writes with no
                # buffer of its own - it must land in the real stream, never
                # in the buffer the main thread is holding.
                tee.write('from another thread\n')
                done.set()

            t = threading.Thread(target=other_thread_write)
            t.start()
            t.join(timeout=5)
        finally:
            tee.set_buffer(None)
        self.assertTrue(done.is_set(), 'the other thread never finished writing')
        self.assertIn('from another thread', real.getvalue())
        self.assertNotIn('from another thread', held_buffer.getvalue())

    def test_write_goes_to_own_buffer_when_installed(self):
        real = io.StringIO()
        tee = serve._ThreadTee(real)
        buf = io.StringIO()
        tee.set_buffer(buf)
        tee.write('captured line\n')
        tee.set_buffer(None)
        self.assertEqual(buf.getvalue(), 'captured line\n')
        self.assertEqual(real.getvalue(), '')


class ProcessVerbCaptureTests(_ServeCase):
    """Fix 5: `_verb_process` classifies each captured line by its own
    ERROR:/WARNING: prefix (process.py's plain-print convention, no
    structured level to read) - anything else is 'info'. Needs a `_ThreadTee`
    installed on sys.stdout/stderr (as `_cmd_serve` does at startup) for
    `_verb_process` to have anything to capture at all."""

    def setUp(self) -> None:
        super().setUp()
        self._orig_stdout, self._orig_stderr = sys.stdout, sys.stderr
        sys.stdout = serve._ThreadTee(self._orig_stdout)
        sys.stderr = serve._ThreadTee(self._orig_stderr)

    def tearDown(self) -> None:
        sys.stdout, sys.stderr = self._orig_stdout, self._orig_stderr
        super().tearDown()

    def test_captured_lines_classified_by_prefix(self):
        def fake_run_process(ns):
            print('ERROR: boom')
            print('WARNING: careful')
            print('a plain info line')
            return serve.Result(ok=True, exit_code=serve.EXIT_CLEAN)

        orig = serve.process_mod.run_process
        serve.process_mod.run_process = fake_run_process
        try:
            result = serve._verb_process(self.state, {'file': 'fha.yaml'}, dry_run=True)
        finally:
            serve.process_mod.run_process = orig
        levels = {m.text: m.level for m in result.messages}
        self.assertEqual(levels.get('ERROR: boom'), 'error')
        self.assertEqual(levels.get('WARNING: careful'), 'warning')
        self.assertEqual(levels.get('a plain info line'), 'info')


class ReindexAfterTests(_ServeCase):
    """Fix 6: `_reindex_after` reads ONLY `data['source_id']` (no `'source'`
    key guess, which held a file PATH, not an id) - a source-scoped verb
    upserts the one source it named; otherwise a full rebuild. `run_claim`
    now publishes `source_id` (confirmed in this working tree's claim.py), so
    this exercises the real verb end-to-end rather than a synthetic Result."""

    def test_claim_review_upserts_the_named_source_not_a_full_rebuild(self):
        row = self.a_suggested_claim()
        self.assertIsNotNone(row)
        cid = row[0]
        calls = {'upsert': [], 'full': 0}
        orig_upsert, orig_build = index_mod.upsert_source, index_mod.build_index

        def counting_upsert(archive_root, fha_config, sid):
            calls['upsert'].append(sid)
            return orig_upsert(archive_root, fha_config, sid)

        def counting_build(archive_root, fha_config):
            calls['full'] += 1
            return orig_build(archive_root, fha_config)

        serve.index_mod.upsert_source = counting_upsert
        serve.index_mod.build_index = counting_build
        try:
            s, d, _h = self.post_run('claim.review', {'claim_id': cid, 'status': 'accepted'}, False)
        finally:
            serve.index_mod.upsert_source = orig_upsert
            serve.index_mod.build_index = orig_build
        self.assertEqual(s, 200)
        self.assertTrue(json.loads(d)['ok'])
        self.assertEqual(len(calls['upsert']), 1)
        self.assertEqual(calls['full'], 0)

    def test_missing_source_id_falls_back_to_full_rebuild(self):
        # A synthetic Result with no source_id (e.g. an engine that has not
        # been updated to publish it yet) must still reindex - just the slow
        # way - rather than leave the index stale.
        calls = {'upsert': 0, 'full': 0}
        orig_upsert, orig_build = index_mod.upsert_source, index_mod.build_index
        orig_run_claim = serve.claim.run_claim

        def counting_upsert(archive_root, fha_config, sid):
            calls['upsert'] += 1
            return orig_upsert(archive_root, fha_config, sid)

        def counting_build(archive_root, fha_config):
            calls['full'] += 1
            return orig_build(archive_root, fha_config)

        def fake_run_claim(*a, **kw):
            return serve.Result(ok=True, exit_code=serve.EXIT_CLEAN, data={'status': 'ok'})

        serve.index_mod.upsert_source = counting_upsert
        serve.index_mod.build_index = counting_build
        serve.claim.run_claim = fake_run_claim
        try:
            s, d, _h = self.post_run(
                'claim.review', {'claim_id': 'C-0000000000', 'status': 'accepted'}, False)
        finally:
            serve.index_mod.upsert_source = orig_upsert
            serve.index_mod.build_index = orig_build
            serve.claim.run_claim = orig_run_claim
        self.assertEqual(s, 200)
        self.assertTrue(json.loads(d)['ok'])
        self.assertEqual(calls['upsert'], 0)
        self.assertEqual(calls['full'], 1)


class DesignStalenessTests(_ServeCase):
    def test_editing_design_custom_css_marks_snapshot_stale(self):
        self.assertFalse(serve.snapshot_is_stale(self.state))
        design = self.root / 'design'
        design.mkdir(parents=True, exist_ok=True)
        css = design / 'custom.css'
        css.write_text('body { color: red; }', encoding='utf-8')
        # Force the new file's mtime strictly past the snapshot's build time -
        # some filesystems have coarse mtime resolution, and this test must
        # not flake on how fast the two statements above ran.
        future = time.time() + 5
        os.utime(css, (future, future))
        # The assertFalse above primed the staleness memo (cleanup task 1) -
        # reset it so this check does a real walk instead of reusing that
        # now-stale-in-fact-but-still-within-TTL cached answer. A real editor
        # save and the next page load are rarely under a second apart; the
        # test collapses that gap deliberately rather than sleeping.
        with self.state._memo_lock:
            self.state._mtime_memo = None
        self.assertTrue(serve.snapshot_is_stale(self.state))


class HomeEditNewlineTests(_ServeCase):
    """Fix 8: `home.edit` preserves notes/home.md's existing newline style."""

    def test_crlf_authored_home_md_keeps_crlf_after_edit(self):
        home = self.root / 'notes' / 'home.md'
        home.parent.mkdir(parents=True, exist_ok=True)
        home.write_bytes(b'Old intro line.\r\nSecond line.\r\n')
        s, d, _h = self.post_run('home.edit', {'text': 'A brand new intro paragraph.'}, False)
        self.assertEqual(s, 200)
        self.assertTrue(json.loads(d)['ok'])
        raw = home.read_bytes()
        self.assertIn(b'\r\n', raw)
        # Every LF must be part of a CRLF pair - no bare LF snuck in.
        self.assertEqual(raw.count(b'\n'), raw.count(b'\r\n'))

    def test_new_home_md_gets_plain_lf(self):
        home = self.root / 'notes' / 'home.md'
        if home.exists():
            home.unlink()
        s, d, _h = self.post_run('home.edit', {'text': 'First ever intro.'}, False)
        self.assertEqual(s, 200)
        self.assertTrue(json.loads(d)['ok'])
        raw = home.read_bytes()
        self.assertNotIn(b'\r\n', raw)
        self.assertIn(b'\n', raw)


class InboxPairingTests(_ServeCase):
    """Fix 9: `gather_inbox` pairs a sidecar with the ONE other file whose
    stem matches exactly (process.py's own rule) - never a prefix match, and
    never a guess when more than one file shares that stem."""

    def _reset_inbox(self):
        inbox = self.root / 'inbox'
        inbox.mkdir(parents=True, exist_ok=True)
        for p in list(inbox.iterdir()):
            if p.is_file():
                p.unlink()
            else:
                shutil.rmtree(p)
        return inbox

    def test_stem_exact_match_pairs_deterministically(self):
        inbox = self._reset_inbox()
        # `photo.raw.jpg` starts with the same "photo." prefix as the sidecar
        # but has a DIFFERENT stem (`photo.raw`) - only `photo.jpg` (stem
        # `photo`) may pair with `photo.notes.md`.
        (inbox / 'photo.jpg').write_bytes(b'a')
        (inbox / 'photo.raw.jpg').write_bytes(b'b')
        (inbox / 'photo.notes.md').write_text(
            '---\nnoted: 2020-01-01\n---\n\nan old note\n', encoding='utf-8')
        result = serve.gather_inbox(self.state)
        by_name = {item['name']: item for item in result['items']}
        self.assertEqual(len(result['items']), 2)
        self.assertEqual(by_name['photo.jpg']['kind'], 'asset+note')
        self.assertEqual(by_name['photo.jpg']['sidecar'], 'inbox/photo.notes.md')
        self.assertEqual(by_name['photo.raw.jpg']['kind'], 'asset')
        self.assertNotIn('photo.notes.md', by_name)  # folded into the pair above

    def test_ambiguous_stem_lists_sidecar_alone(self):
        inbox = self._reset_inbox()
        (inbox / 'letter.txt').write_bytes(b'a')
        (inbox / 'letter.pdf').write_bytes(b'b')
        (inbox / 'letter.notes.md').write_text(
            '---\nnoted: 2020-01-01\n---\n\nan old note\n', encoding='utf-8')
        result = serve.gather_inbox(self.state)
        by_name = {item['name']: item for item in result['items']}
        self.assertEqual(len(result['items']), 3)
        self.assertEqual(by_name['letter.notes.md']['kind'], 'note')
        self.assertEqual(by_name['letter.txt']['kind'], 'asset')
        self.assertEqual(by_name['letter.pdf']['kind'], 'asset')

    def test_bundle_file_links_include_the_bundle_directory(self):
        # P2 codex finding (round 2, PR #30): a bundle item's `files` are
        # bare basenames living inside the bundle SUBFOLDER, but the
        # inbox.html template built every file link as `inbox/<basename>` -
        # dropping the subfolder, so /api/open 404'd on a file the page had
        # just listed as present.
        inbox = self._reset_inbox()
        bundle = inbox / 'census-bundle'
        bundle.mkdir()
        (bundle / 'notes.md').write_text('---\nnoted: 2020-01-01\n---\n\nbody\n', encoding='utf-8')
        (bundle / 'scan.jpg').write_bytes(b'x')
        s, d, _h = self.req('GET', '/inbox')
        self.assertEqual(s, 200)
        page = d.decode('utf-8')
        self.assertIn('data-wb-open-file="inbox/census-bundle/scan.jpg"', page)
        self.assertIn('data-wb-open-file="inbox/census-bundle/notes.md"', page)
        self.assertNotIn('data-wb-open-file="inbox/scan.jpg"', page)


class PortZeroTests(unittest.TestCase):
    """Fix 10: an explicit `--port 0` must reach preflight/bind, not be
    silently replaced by DEFAULT_PORT (`0 or DEFAULT_PORT` is the bug - 0 is
    falsy). Cheap unit test of the extracted `_resolved_port` helper."""

    def test_explicit_port_zero_is_not_replaced(self):
        args = argparse.Namespace(port=0)
        self.assertEqual(serve._resolved_port(args), 0)

    def test_absent_port_falls_back_to_default(self):
        args = argparse.Namespace()
        self.assertEqual(serve._resolved_port(args), serve.DEFAULT_PORT)

    def test_explicit_nonzero_port_passes_through(self):
        args = argparse.Namespace(port=9001)
        self.assertEqual(serve._resolved_port(args), 9001)


class CmdServeEphemeralPortTests(unittest.TestCase):
    """P2 codex finding (PR #30): `_cmd_serve` must serve on - and print/embed
    - the OS-assigned port when `--port 0` is given, not the literal 0 it was
    asked for. `_resolved_port` already passed 0 through untouched
    (PortZeroTests above); the remaining bug was that `_cmd_serve` built the
    banner URL and the ServeState (whose `port` the snapshot embeds) from
    that requested port BEFORE binding, instead of `httpd.server_address[1]`
    after. The functional server tests elsewhere in this file sidestep
    `_cmd_serve` entirely - they build ServeState/httpd by hand and fix up
    `state.port` themselves (see `_ServeCase.setUp`) - which is exactly why
    this shipped uncaught; this test drives the real CLI entrypoint."""

    @classmethod
    def setUpClass(cls) -> None:
        if not EXAMPLE.is_dir():
            raise unittest.SkipTest('example-archive not present')

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / 'arc'
        shutil.copytree(EXAMPLE, self.root)
        config = load_fha_yaml(self.root, strict=True)
        index_mod.build_index(self.root, config)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_port_0_serves_and_prints_the_bound_port(self) -> None:
        args = argparse.Namespace(root=str(self.root), port=0, no_browser=True)

        class _ImmediateStop(ThreadingHTTPServer):
            """Binds for real (so `server_address[1]` is a genuine OS-assigned
            port) but returns from `serve_forever` at once, so the test
            doesn't block on a server no request will ever reach."""
            def serve_forever(self, poll_interval=0.5):
                raise KeyboardInterrupt

        real_ths = serve.ThreadingHTTPServer
        serve.ThreadingHTTPServer = _ImmediateStop
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                exit_code = serve._cmd_serve(args)
        finally:
            serve.ThreadingHTTPServer = real_ths

        self.assertEqual(exit_code, serve.EXIT_CLEAN)
        banner = out.getvalue()
        self.assertNotIn('127.0.0.1:0/', banner)
        self.assertRegex(banner, r'serving at  http://127\.0\.0\.1:[1-9]\d*/')


class EchoTests(unittest.TestCase):
    """Fix 11: the `--confidence`/`--title` CLI-echo parity additions.
    `_echo_*` are pure string-building functions of the coerced kwargs - no
    server fixture needed."""

    def test_echo_claim_new_includes_confidence_when_given(self):
        echo = serve._echo_claim_new({
            'source_id': 'S-fa1234567b', 'claim_type': 'birth', 'value': '1870',
            'confidence': 'direct',
        })
        self.assertIn('--confidence direct', echo)

    def test_echo_claim_new_omits_confidence_when_absent(self):
        echo = serve._echo_claim_new({
            'source_id': 'S-fa1234567b', 'claim_type': 'birth', 'value': '1870',
        })
        self.assertNotIn('--confidence', echo)

    def test_echo_capture_path_includes_title_when_given(self):
        echo = serve._echo_capture_path(
            {'path': '/library/grandma.jpg', 'title': "Grandma's Wedding"})
        self.assertIn('--title', echo)
        self.assertIn("Grandma's Wedding", echo)

    def test_echo_capture_path_omits_title_when_absent(self):
        echo = serve._echo_capture_path({'path': '/library/grandma.jpg'})
        self.assertNotIn('--title', echo)

    def test_q_escapes_command_substitution_so_it_cannot_run(self):
        # P2 codex finding (round 4, PR #30): a value holding `$(...)` was
        # still expanded by a POSIX shell inside the OLD double-quote form -
        # only `"`/space/tab triggered quoting at all, and the `$` itself was
        # never escaped even when quoted. Copy-pasting the echoed command
        # must never execute anything hidden inside the value.
        echo = serve._echo_person_new({'name': 'bad $(touch pwned)'})
        self.assertIn('\\$(touch pwned)', echo)
        self.assertNotIn('"bad $(touch pwned)"', echo)

    def test_q_escapes_backtick_command_substitution(self):
        echo = serve._echo_person_new({'name': 'bad `touch pwned`'})
        self.assertIn('\\`touch pwned\\`', echo)

    def test_q_quotes_a_value_with_no_spaces_but_a_shell_operator(self):
        # An unquoted value with no space (e.g. `R&D`) used to be echoed bare
        # - `&` backgrounds a command in an unquoted POSIX shell argument.
        echo = serve._echo_person_new({'name': 'R&D'})
        self.assertIn('"R&D"', echo)

    def test_echo_claim_review_quotes_a_plain_words_date(self):
        # P2 codex finding (round 7, PR #30): `--date` was appended bare - a
        # plain-words date like "June 1923" (which `fha claim`/`claim.new`
        # itself accepts and normalizes) split into multiple unquoted shell
        # tokens in the copyable echo, so pasting it ran a different command
        # than the previewed action.
        echo = serve._echo_claim_review({'claim_id': 'C-fa1234567b', 'date': 'June 1923'})
        self.assertIn('"June 1923"', echo)

    def test_echo_claim_new_quotes_a_plain_words_date(self):
        echo = serve._echo_claim_new({
            'source_id': 'S-fa1234567b', 'claim_type': 'birth', 'value': '1870',
            'date': 'June 1923',
        })
        self.assertIn('"June 1923"', echo)

    def test_echo_estimate_quotes_plain_words_birth_and_death(self):
        echo = serve._echo_estimate({
            'person_id': 'P-fa1234567b', 'birth': 'circa 1870', 'death': 'June 1923',
        })
        self.assertIn('"circa 1870"', echo)
        self.assertIn('"June 1923"', echo)

    def test_echoed_command_is_never_truncated(self):
        # P2 codex finding (round 7, PR #30): `_short()` cut any echoed value
        # over 60 chars with an ellipsis before quoting it - the workbench
        # labels this block "This button is exactly", so a copied command
        # for a long claim value/biography edit/note used to write different
        # (truncated) text than what was actually previewed/applied.
        long_value = 'x' * 200
        echo = serve._echo_claim_review({'claim_id': 'C-fa1234567b', 'value': long_value})
        self.assertIn(long_value, echo)
        self.assertNotIn('...', echo)

        echo = serve._echo_person_edit({
            'person_id': 'P-fa1234567b', 'section': 'biography', 'text': long_value,
        })
        self.assertIn(long_value, echo)
        self.assertNotIn('...', echo)

        echo = serve._echo_source_note({'source_id': 'S-fa1234567b', 'text': long_value})
        self.assertIn(long_value, echo)
        self.assertNotIn('...', echo)


class StalenessMemoTests(_ServeCase):
    """Cleanup task 1: `snapshot_is_stale`'s record-tree walk used to run on
    EVERY page GET, and twice on a stale hit (once before `ensure_snapshot`
    takes the lock, again just inside it). `_newest_input_mtime_cached` memos
    the walk for `_MEMO_TTL` seconds; `invalidate_snapshot` drops the memo
    immediately so a serve-side write is never masked by the TTL."""

    def test_memo_prevents_a_second_walk_within_the_ttl(self):
        # Force a cold memo regardless of what setUp's own ensure_snapshot
        # already primed, so the first call below is a guaranteed real walk.
        with self.state._memo_lock:
            self.state._mtime_memo = None
        calls = {'n': 0}
        orig = serve._newest_mtime_under

        def counting(base):
            calls['n'] += 1
            return orig(base)

        serve._newest_mtime_under = counting
        try:
            serve.snapshot_is_stale(self.state)
            first = calls['n']
            self.assertGreater(first, 0, 'the first call after a cold memo must walk the tree')
            serve.snapshot_is_stale(self.state)
            self.assertEqual(calls['n'], first, 'a second call within the TTL must reuse the memo')
        finally:
            serve._newest_mtime_under = orig

    def test_staleness_still_detected_after_a_record_edit(self):
        self.assertFalse(serve.snapshot_is_stale(self.state))
        target = next((self.root / 'sources').rglob('*.md'))
        text = target.read_text(encoding='utf-8')
        target.write_text(text, encoding='utf-8')
        # Force the new mtime strictly past the snapshot's build time - some
        # filesystems have coarse mtime resolution and this must not flake.
        future = time.time() + 5
        os.utime(target, (future, future))
        # The memo from the assertFalse above is still within its TTL - reset
        # it explicitly (matching the human-timescale gap a real edit-then-
        # reload would have) rather than sleep in a test.
        with self.state._memo_lock:
            self.state._mtime_memo = None
        self.assertTrue(serve.snapshot_is_stale(self.state))

    def test_invalidate_snapshot_drops_the_mtime_memo(self):
        serve.snapshot_is_stale(self.state)   # primes the memo
        self.assertIsNotNone(self.state._mtime_memo)
        serve.invalidate_snapshot(self.state)
        self.assertIsNone(self.state._mtime_memo)


class CountsPipelineTests(_ServeCase):
    """Cleanup task 2: gather_review/gather_inbox (full index queries plus
    xref/cooccur detection) used to run 2-3x per /review or /inbox request -
    once for the page's own items, again for the servebar count, again
    inside a snapshot rebuild. Counts are now memoized on ServeState, and the
    /review and /inbox routes thread their own already-gathered item count
    straight into the servebar instead of triggering a second gather."""

    def test_review_request_calls_gather_review_exactly_once(self):
        calls = {'n': 0}
        orig = serve.gather_review

        def counting(state):
            calls['n'] += 1
            return orig(state)

        serve.gather_review = counting
        try:
            s, _d, _h = self.req('GET', '/review')
        finally:
            serve.gather_review = orig
        self.assertEqual(s, 200)
        self.assertEqual(calls['n'], 1)

    def test_inbox_request_calls_gather_inbox_exactly_once(self):
        calls = {'n': 0}
        orig = serve.gather_inbox

        def counting(state):
            calls['n'] += 1
            return orig(state)

        serve.gather_inbox = counting
        try:
            s, _d, _h = self.req('GET', '/inbox')
        finally:
            serve.gather_inbox = orig
        self.assertEqual(s, 200)
        self.assertEqual(calls['n'], 1)

    def test_invalidate_snapshot_drops_both_count_memos(self):
        serve._counts(self.state)   # primes both
        self.assertIsNotNone(self.state._review_count_memo)
        self.assertIsNotNone(self.state._inbox_count_memo)
        serve.invalidate_snapshot(self.state)
        self.assertIsNone(self.state._review_count_memo)
        self.assertIsNone(self.state._inbox_count_memo)


class StreamedFileTests(_ServeCase):
    """Cleanup task 3: `_send_file` now streams via `shutil.copyfileobj`
    instead of reading the whole file into memory first. Byte-identity and
    HEAD-is-headers-only are the observable contract; the streaming itself is
    an implementation detail behind that contract."""

    def test_large_file_served_byte_identical(self):
        photos = self.root / 'photos'
        photos.mkdir(parents=True, exist_ok=True)
        # Several multiples of the 64 KiB stream chunk size so the copy loop
        # actually iterates more than once.
        data = os.urandom(5 * serve._STREAM_CHUNK_SIZE + 1234)
        target = photos / 'big-test-file.bin'
        target.write_bytes(data)
        s, d, h = self.req('GET', '/root/photos/big-test-file.bin')
        self.assertEqual(s, 200)
        self.assertEqual(d, data)
        self.assertEqual(h.get('Content-Length'), str(len(data)))

    def test_head_request_on_a_streamed_file_sends_no_body(self):
        photos = self.root / 'photos'
        photos.mkdir(parents=True, exist_ok=True)
        target = photos / 'head-test-file.bin'
        target.write_bytes(os.urandom(4096))
        s, d, h = self.req('HEAD', '/root/photos/head-test-file.bin')
        self.assertEqual(s, 200)
        self.assertEqual(d, b'')
        self.assertEqual(h.get('Content-Length'), '4096')


class KeepAliveHygieneTests(_ServeCase):
    """Cleanup task 4: two undrained-body paths used to desync HTTP/1.1
    keep-alive - `/api/reindex` never read its body at all, and `_read_body`
    returned None over the cap with nothing drained. Both are exercised here
    by reusing ONE http.client connection across two requests, which is how a
    real browser/fetch keep-alive connection behaves."""

    def test_reindex_drains_its_body_and_the_connection_stays_reusable(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            headers = {'Host': f'127.0.0.1:{self.port}', 'Content-Type': 'application/json',
                      'X-FHA-CSRF': self.state.csrf_token}
            conn.request('POST', '/api/reindex', body=b'{}', headers=headers)
            r1 = conn.getresponse()
            self.assertEqual(r1.status, 200)
            r1.read()
            # If the small body above were left undrained, this second
            # request on the SAME connection would desync - either hang
            # waiting on stray bytes or read garbage as its status line.
            conn.request('GET', '/', headers={'Host': f'127.0.0.1:{self.port}'})
            r2 = conn.getresponse()
            self.assertEqual(r2.status, 200)
            r2.read()
        finally:
            conn.close()

    def test_over_cap_post_gets_413_with_connection_close(self):
        big = b'x' * (64 * 1024 + 1)
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            headers = {'Host': f'127.0.0.1:{self.port}', 'Content-Type': 'application/json',
                      'X-FHA-CSRF': self.state.csrf_token}
            conn.request('POST', '/api/reindex', body=big, headers=headers)
            r = conn.getresponse()
            self.assertEqual(r.status, 413)
            self.assertEqual((r.getheader('Connection') or '').lower(), 'close')
            r.read()
        finally:
            conn.close()


class PlaceVerbTests(_ServeCase):
    """place.set / place.note: the place page's registry write-backs
    (owner request, review 2026-07-16). L-7c1a9f4e22 is the example
    archive's Fairview."""

    LID = 'L-7c1a9f4e22'

    def test_place_set_dry_run_previews_and_writes_nothing(self):
        before = _tree_hash(self.root)
        s, d, _h = self.post_run('place.set',
                                 {'place_id': self.LID, 'lat': '40.1', 'lon': '-95.0'}, True)
        self.assertEqual(s, 200)
        payload = json.loads(d)
        self.assertTrue(payload['ok'])
        self.assertIn('fha places set L-7c1a9f4e22 --coords "40.1, -95.0"',
                      payload['cli_echo'])
        self.assertEqual(_tree_hash(self.root), before)

    def test_place_set_apply_moves_the_pin(self):
        s, d, _h = self.post_run('place.set',
                                 {'place_id': self.LID, 'lat': '40.1', 'lon': '-95.0'}, False)
        self.assertEqual(s, 200)
        self.assertTrue(json.loads(d)['ok'])
        registry = (self.root / 'places' / 'places.yaml').read_text(encoding='utf-8')
        self.assertIn('coords: [40.1, -95.0]', registry)

    def test_place_aka_one_per_line_round_trips_commas(self):
        # P2 codex finding (round 2, PR #31): the modal speaks one alias per
        # LINE, so "Washington, D.C." stays a single alias instead of being
        # resplit into two on an untouched round-trip. The echo names one
        # verbatim --aka per name.
        s, d, _h = self.post_run('place.set',
                                 {'place_id': self.LID,
                                  'aka': 'Washington, D.C.\nOld Fairview'}, False)
        self.assertEqual(s, 200)
        payload = json.loads(d)
        self.assertTrue(payload['ok'])
        self.assertIn('--aka "Washington, D.C." --aka "Old Fairview"',
                      payload['cli_echo'])
        import yaml as _yaml
        registry = _yaml.safe_load(
            (self.root / 'places' / 'places.yaml').read_text(encoding='utf-8'))
        entry = next(e for e in registry if e['id'] == self.LID)
        self.assertEqual(entry['alt_names'], ['Washington, D.C.', 'Old Fairview'])

    def test_place_aka_empty_clears_the_list(self):
        # P2 codex finding (round 3, PR #31): an emptied textarea is an
        # explicit "clear the whole list", submitted as an empty string
        # (data-wb-allowempty) - the echo spells it the CLI way, `--aka -`.
        s, d, _h = self.post_run('place.set',
                                 {'place_id': self.LID, 'aka': ''}, False)
        self.assertEqual(s, 200)
        payload = json.loads(d)
        self.assertTrue(payload['ok'])
        self.assertIn('--aka -', payload['cli_echo'])
        import yaml as _yaml
        registry = _yaml.safe_load(
            (self.root / 'places' / 'places.yaml').read_text(encoding='utf-8'))
        entry = next(e for e in registry if e['id'] == self.LID)
        self.assertEqual(entry.get('alt_names'), [])

    def test_place_history_empty_clears_the_list(self):
        s, d, _h = self.post_run('place.set',
                                 {'place_id': self.LID, 'history': ''}, False)
        self.assertEqual(s, 200)
        payload = json.loads(d)
        self.assertTrue(payload['ok'])
        self.assertIn('--history -', payload['cli_echo'])
        import yaml as _yaml
        registry = _yaml.safe_load(
            (self.root / 'places' / 'places.yaml').read_text(encoding='utf-8'))
        entry = next(e for e in registry if e['id'] == self.LID)
        self.assertEqual(entry.get('history'), [])

    def test_place_note_apply_appends_dated_note(self):
        s, d, _h = self.post_run('place.note',
                                 {'place_id': self.LID, 'text': 'Platted 1858.'}, False)
        self.assertEqual(s, 200)
        payload = json.loads(d)
        self.assertTrue(payload['ok'])
        self.assertIn('fha places note L-7c1a9f4e22 --text "Platted 1858."',
                      payload['cli_echo'])
        registry = (self.root / 'places' / 'places.yaml').read_text(encoding='utf-8')
        self.assertIn('Platted 1858.', registry)

    def test_place_edit_note_rewrites_one_entry(self):
        s, _d, _h = self.post_run('place.note',
                                  {'place_id': self.LID, 'text': 'First finding.'}, False)
        self.assertEqual(s, 200)
        registry = (self.root / 'places' / 'places.yaml').read_text(encoding='utf-8')
        line = next(ln.strip() for ln in registry.splitlines() if 'First finding.' in ln)
        s, d, _h = self.post_run('place.edit_note',
                                 {'place_id': self.LID, 'old_text': line,
                                  'text': 'First finding, corrected.'}, False)
        self.assertEqual(s, 200)
        payload = json.loads(d)
        self.assertTrue(payload['ok'])
        self.assertIn('fha places edit-note', payload['cli_echo'])
        registry = (self.root / 'places' / 'places.yaml').read_text(encoding='utf-8')
        self.assertIn('First finding, corrected.', registry)
        self.assertNotIn('First finding.\n', registry)

    def test_place_set_unknown_id_is_a_plain_refusal(self):
        s, d, _h = self.post_run('place.set',
                                 {'place_id': 'L-bbbbbbbbbb', 'lat': '1', 'lon': '2'}, True)
        self.assertEqual(s, 200)
        payload = json.loads(d)
        self.assertFalse(payload['ok'])
        self.assertIn('fha find', payload['messages'][0]['text'])


class ReciprocalRelateTests(_ServeCase):
    """The workbench mirrors unsourced ties on BOTH records by default
    (owner request, review 2026-07-16): add John as Jane's spouse and it
    shows from John's page too."""

    A = 'P-6f7g8h9jka'   # Warren Calvin Hartley
    B = 'P-c4b26bb4bc'   # Ethel Hartley

    def _person_file(self, pid):
        hits = [p for p in (self.root / 'people').rglob('*.md')
                if pid.lower() in p.name.lower()
                and '_timeline' not in p.name and '_sources-index' not in p.name
                and '_draft-queue' not in p.name]
        self.assertTrue(hits, f'no record file found for {pid}')
        return hits[0]

    def test_relate_defaults_to_reciprocal_and_echoes_it(self):
        s, d, _h = self.post_run('person.relate',
                                 {'person_id': self.A, 'relation_type': 'spouse',
                                  'target_id': self.B}, False)
        self.assertEqual(s, 200)
        payload = json.loads(d)
        self.assertTrue(payload['ok'])
        self.assertIn('--reciprocal', payload['cli_echo'])
        a_text = self._person_file(self.A).read_text(encoding='utf-8')
        b_text = self._person_file(self.B).read_text(encoding='utf-8')
        self.assertIn(self.B, a_text)
        self.assertIn(self.A, b_text)
        self.assertIn('status: hypothesis', a_text)
        self.assertIn('status: hypothesis', b_text)

    def test_explicit_false_still_records_one_sided(self):
        b_before = self._person_file(self.B).read_bytes()
        s, d, _h = self.post_run('person.relate',
                                 {'person_id': self.A, 'relation_type': 'spouse',
                                  'target_id': self.B, 'reciprocal': False}, False)
        self.assertEqual(s, 200)
        self.assertTrue(json.loads(d)['ok'])
        a_text = self._person_file(self.A).read_text(encoding='utf-8')
        self.assertIn(self.B, a_text)
        # The target's record is byte-identical - no mirror was written.
        self.assertEqual(self._person_file(self.B).read_bytes(), b_before)


class AddFamilyRollbackTests(_ServeCase):
    """person.add_family's typed-name path mints the stub, then relates. A
    relate refusal must not leave the fresh stub behind as an orphan - a
    failed combined action may not mutate the archive (P2 codex finding,
    round 1, PR #31)."""

    A = 'P-6f7g8h9jka'   # Warren Calvin Hartley

    def _stub_names(self):
        stubs = self.root / 'people' / 'stubs'
        return {p.name for p in stubs.iterdir()} if stubs.is_dir() else set()

    def test_failed_relate_rolls_the_minted_stub_back(self):
        before = self._stub_names()
        s, d, _h = self.post_run('person.add_family',
                                 {'person_id': self.A, 'relation_type': 'cousin',
                                  'name': 'Orphan Candidate'}, False)
        self.assertEqual(s, 200)
        payload = json.loads(d)
        self.assertFalse(payload['ok'])
        self.assertEqual(self._stub_names(), before)
        text = ' '.join(m['text'] for m in payload['messages'])
        self.assertIn('rolled back', text)

    def test_successful_mint_and_relate_keeps_the_stub(self):
        before = self._stub_names()
        s, d, _h = self.post_run('person.add_family',
                                 {'person_id': self.A, 'relation_type': 'sibling',
                                  'name': 'Kept Sibling'}, False)
        self.assertEqual(s, 200)
        payload = json.loads(d)
        self.assertTrue(payload['ok'])
        self.assertEqual(len(self._stub_names() - before), 1)


class PickFileTests(_ServeCase):
    """/api/pickfile - only the no-dialog paths (a real dialog would hang a
    headless test run): the one-at-a-time lock refusal, and the CSRF gate."""

    def test_busy_lock_is_a_plain_refusal_not_a_second_dialog(self):
        self.state.pick_lock.acquire()
        try:
            headers = {'Content-Type': 'application/json',
                       'X-FHA-CSRF': self.state.csrf_token}
            s, d, _h = self.req('POST', '/api/pickfile', body='{}', headers=headers)
            self.assertEqual(s, 200)
            payload = json.loads(d)
            self.assertFalse(payload['ok'])
            self.assertIn('already open', payload['messages'][0]['text'])
        finally:
            self.state.pick_lock.release()

    def test_pickfile_requires_csrf(self):
        s, _d, _h = self.req('POST', '/api/pickfile', body='{}',
                             headers={'Content-Type': 'application/json'})
        self.assertEqual(s, 403)


if __name__ == '__main__':
    unittest.main()
