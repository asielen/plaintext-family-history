"""Tests for the `fha capture --host` native-messaging host (capture-frontend-06).

The host speaks length-prefixed JSON over stdin/stdout: one write (file a bundle
into inbox/, reusing the ingest path) and two read-only archive queries
(suggestNames, checkUrl). No network, no browser - the framing is exercised with
in-memory byte streams and the queries against a tiny fixture archive.

Run: python -m unittest tests.test_capture_host -v   (from the repo root)
"""

import base64
import io
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import capture
from _lib import load_fha_yaml, read_record

SAMPLES = ROOT / 'tests' / 'fixtures' / 'capture-samples'


def _frame(obj: dict) -> bytes:
    data = json.dumps(obj).encode('utf-8')
    return struct.pack('@I', len(data)) + data


def _unframe_all(blob: bytes) -> list[dict]:
    out, i = [], 0
    while i + 4 <= len(blob):
        (length,) = struct.unpack('@I', blob[i:i + 4])
        i += 4
        out.append(json.loads(blob[i:i + length].decode('utf-8')))
        i += length
    return out


class HostFramingTestCase(unittest.TestCase):
    def test_round_trip(self) -> None:
        buf = io.BytesIO()
        capture._write_native_message(buf, {'action': 'ping', 'n': 1})
        buf.seek(0)
        self.assertEqual(capture._read_native_message(buf), {'action': 'ping', 'n': 1})

    def test_clean_eof_returns_none(self) -> None:
        self.assertIsNone(capture._read_native_message(io.BytesIO(b'')))

    def test_truncated_prefix_raises(self) -> None:
        with self.assertRaises(capture.BundleError):
            capture._read_native_message(io.BytesIO(b'\x01\x02'))

    def test_truncated_body_raises(self) -> None:
        with self.assertRaises(capture.BundleError):
            capture._read_native_message(io.BytesIO(struct.pack('@I', 100) + b'short'))

    def test_oversized_frame_rejected_not_allocated(self) -> None:
        with self.assertRaises(capture.BundleError):
            capture._read_native_message(io.BytesIO(struct.pack('@I', 1 << 30)))


class HostArchiveTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive = Path(self._tmp.name) / 'archive'
        (self.archive / 'people').mkdir(parents=True)
        (self.archive / 'sources').mkdir(parents=True)
        (self.archive / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8')
        self.config = load_fha_yaml(self.archive, strict=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _person(self, fname: str, name: str, *aliases: str) -> None:
        alias_list = ', '.join([f'"{a}"' for a in aliases])
        (self.archive / 'people' / fname).write_text(
            f'---\nid: P-{fname[:8]}\nname: {name}\naliases: [{alias_list}]\n---\n',
            encoding='utf-8')

    def _source(self, fname: str, sid: str, *urls: str) -> None:
        links = ''.join(f'  - url: "{u}"\n    accessed: 2026-06-12\n' for u in urls)
        (self.archive / 'sources' / fname).write_text(
            f'---\nid: {sid}\ntitle: A record\nexternal_links:\n{links}---\n',
            encoding='utf-8')

    # ── ping / dispatch ──────────────────────────────────────────────────────

    def test_ping_and_unknown_action(self) -> None:
        self.assertEqual(capture._host_dispatch(self.archive, self.config,
                                                {'action': 'ping'})['ok'], True)
        bad = capture._host_dispatch(self.archive, self.config, {'action': 'nope'})
        self.assertFalse(bad['ok'])
        self.assertIn('unsupported', bad['error'])

    # ── Capability 1: filing equivalence ─────────────────────────────────────

    def test_ingest_files_a_bundle_and_returns_the_stub(self) -> None:
        page_html = (SAMPLES / 'ancestry.html').read_text(encoding='utf-8')
        msg = {
            'action': 'ingest',
            'bundleName': 'ancestry-rec',
            'pageHtml': page_html,
            'captureJson': {
                'schema': 2, 'url': 'https://www.ancestry.com/rec/1',
                'accessed': '2026-06-24',
                'assets': [{'file': 'record.jpg', 'role': 'record', 'mode': 'manual'}],
            },
            'assets': [{'filename': 'record.jpg',
                        'base64': base64.b64encode(b'\xff\xd8\xff fake').decode()}],
        }
        resp = capture._host_dispatch(self.archive, self.config, msg)
        self.assertTrue(resp['ok'], resp)
        self.assertTrue(resp['stub'].startswith('inbox/'))
        stub = self.archive / resp['stub']
        self.assertTrue(stub.is_file())
        self.assertEqual(read_record(stub)['meta']['repository'], 'Ancestry.com')

    def test_ingest_rejects_missing_capture_json(self) -> None:
        resp = capture._host_dispatch(self.archive, self.config,
                                      {'action': 'ingest', 'pageHtml': '<html></html>'})
        self.assertFalse(resp['ok'])
        self.assertIn('captureJson', resp['error'])

    def test_ingest_rejects_empty_asset_data(self) -> None:
        # A filename with absent/blank base64 must error, not file a 0-byte image.
        msg = {
            'action': 'ingest', 'bundleName': 'rec',
            'captureJson': {'schema': 2, 'url': 'https://x/1', 'accessed': '2026-06-24'},
            'assets': [{'filename': 'record.jpg', 'base64': '   '}],
        }
        resp = capture._host_dispatch(self.archive, self.config, msg)
        self.assertFalse(resp['ok'])
        self.assertIn('no data', resp['error'])

    def test_ingest_rejects_path_traversal_asset(self) -> None:
        # A malicious filename must not escape the bundle dir; it's sanitized to
        # a single segment, so the write stays inside the temp staging folder.
        self.assertEqual(capture._safe_member_name('../../etc/passwd', 'x'), 'passwd')
        self.assertEqual(capture._safe_member_name('a/b/c.jpg', 'x'), 'c.jpg')

    # ── Capability 2: suggestNames ───────────────────────────────────────────

    def test_suggest_names_matches_name_and_alias(self) -> None:
        self._person('sielen01.md', 'Mark B Sielen', 'P-sielen01', 'Marky Sielen')
        self._person('hartley1.md', 'Calvin Hartley', 'P-hartley1')
        resp = capture._host_suggest_names(self.archive, self.config, 'siel', 8)
        self.assertTrue(resp['ok'])
        self.assertIn('Mark B Sielen', resp['names'])
        self.assertIn('Marky Sielen', resp['names'])          # alias matched
        self.assertNotIn('Calvin Hartley', resp['names'])
        self.assertNotIn('P-sielen01', resp['names'])         # id alias never suggested

    def test_suggest_names_respects_limit_and_empty(self) -> None:
        self._person('a.md', 'Anna Smith', 'P-aaaaaa01')
        self._person('b.md', 'Anne Smith', 'P-bbbbbb01')
        self.assertEqual(len(capture._host_suggest_names(self.archive, self.config, 'smith', 1)['names']), 1)
        self.assertEqual(capture._host_suggest_names(self.archive, self.config, 'zzz', 8)['names'], [])

    # ── Capability 3: checkUrl ───────────────────────────────────────────────

    def test_check_url_known_across_tracking_params(self) -> None:
        self._source('s1.md', 'S-aaaa1111',
                     'https://www.ancestry.com/imageviewer/collections/1/images/X?_phsrc=a&pId=9')
        # The same record, different throwaway params + a www/slash difference.
        resp = capture._host_check_url(
            self.archive, self.config,
            'https://ancestry.com/imageviewer/collections/1/images/X/?queryId=zzz')
        self.assertTrue(resp['known'])
        self.assertEqual(resp['source'], 'S-aaaa1111')

    def test_check_url_unknown_is_clean(self) -> None:
        self._source('s1.md', 'S-aaaa1111', 'https://www.ancestry.com/a/b')
        resp = capture._host_check_url(self.archive, self.config, 'https://www.ancestry.com/totally/new')
        self.assertEqual(resp, {'ok': True, 'known': False})

    def test_check_url_distinguishes_clipping_ids(self) -> None:
        # Two clippings off the same Newspapers.com image page differ only by
        # clipping_id; capturing one must not mark the other already-captured.
        self._source('s1.md', 'S-bbbb2222',
                     'https://www.newspapers.com/image/123/?clipping_id=111')
        same = capture._host_check_url(
            self.archive, self.config,
            'https://www.newspapers.com/image/123/?clipping_id=111&_phsrc=x')
        self.assertTrue(same['known'])                       # same clip, throwaway param
        other = capture._host_check_url(
            self.archive, self.config,
            'https://www.newspapers.com/image/123/?clipping_id=222')
        self.assertFalse(other['known'])                     # different clip

    # ── run_host loop over framed streams ────────────────────────────────────

    def test_run_host_serves_then_eofs(self) -> None:
        stdin = io.BytesIO(_frame({'action': 'ping'}) + _frame({'action': 'checkUrl',
                                                                 'url': 'https://x/new'}))
        stdout = io.BytesIO()
        rc = capture.run_host(self.archive, self.config, stdin=stdin, stdout=stdout)
        self.assertEqual(rc, capture.EXIT_CLEAN)
        replies = _unframe_all(stdout.getvalue())
        self.assertEqual(replies[0], {'ok': True, 'v': 1})
        self.assertEqual(replies[1], {'ok': True, 'known': False})

    def test_run_host_reports_garbled_frame(self) -> None:
        stdout = io.BytesIO()
        rc = capture.run_host(self.archive, self.config,
                              stdin=io.BytesIO(struct.pack('@I', 1 << 30)), stdout=stdout)
        self.assertEqual(rc, capture.EXIT_ERRORS)
        self.assertFalse(_unframe_all(stdout.getvalue())[0]['ok'])


class HostInstallTestCase(unittest.TestCase):
    def test_install_writes_manifest_and_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'arch'
            archive.mkdir()
            target = Path(tmp) / 'nm'
            rc = capture._install_host(archive, extension_id='abcdefghij',
                                       manifest_dir=str(target))
            self.assertEqual(rc, capture.EXIT_CLEAN)
            manifest = json.loads((target / f'{capture._NATIVE_HOST_NAME}.json').read_text())
            self.assertEqual(manifest['name'], capture._NATIVE_HOST_NAME)
            self.assertEqual(manifest['type'], 'stdio')
            self.assertEqual(manifest['allowed_origins'], ['chrome-extension://abcdefghij/'])
            self.assertTrue(Path(manifest['path']).is_file())          # launcher exists
            # The launcher points at the real CLI entrypoint, tools/fha.py.
            self.assertIn('fha.py', Path(manifest['path']).read_text())

    def test_install_resolves_relative_manifest_dir_to_absolute(self) -> None:
        # Chrome/Edge require an absolute manifest `path`; a relative
        # --host-manifest-dir must be resolved before the manifest is written.
        import os
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'arch'
            archive.mkdir()
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                rc = capture._install_host(archive, extension_id='abcdefghij',
                                           manifest_dir='nm-rel')
            finally:
                os.chdir(cwd)
            self.assertEqual(rc, capture.EXIT_CLEAN)
            manifest = json.loads(
                (Path(tmp) / 'nm-rel' / f'{capture._NATIVE_HOST_NAME}.json').read_text())
            self.assertTrue(Path(manifest['path']).is_absolute())

    def test_native_manifest_dir_edge_differs_from_chrome(self) -> None:
        chrome = str(capture._native_manifest_dir('chrome')).lower()
        edge = str(capture._native_manifest_dir('edge')).lower()
        self.assertNotEqual(chrome, edge)
        self.assertIn('edge', edge)

    def test_host_rejects_dry_run(self) -> None:
        # --host --dry-run must be refused: the host files live bundles into the
        # inbox, so there is no no-mutation preview to honor.
        import argparse
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'arch'
            (archive / 'people').mkdir(parents=True)
            (archive / 'fha.yaml').write_text('schema: 1\n', encoding='utf-8')
            args = argparse.Namespace(
                root=str(archive), host=True, dry_run=True, install_host=False,
                ingest=False, extension_id=None, host_manifest_dir=None, browser='chrome')
            self.assertEqual(capture._run_capture(args), capture.EXIT_FAILURE)

    def test_install_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'arch'
            archive.mkdir()
            target = Path(tmp) / 'nm'
            rc = capture._install_host(archive, extension_id='abcdefghij',
                                       manifest_dir=str(target), dry_run=True)
            self.assertEqual(rc, capture.EXIT_CLEAN)
            self.assertFalse(target.exists())          # nothing written under dry-run


if __name__ == '__main__':
    unittest.main()
