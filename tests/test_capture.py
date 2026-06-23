"""Tests for `fha capture` (BUILD.md M7.5 generic + M7.6/M7.7 site recipes).

No network and no third-party HTML library: capture reads HTML handed to it
(here, the anonymized fixtures under tests/fixtures/capture-samples/) and parses
with the stdlib. The CLI's stdin path is exercised by monkeypatching
`sys.stdin`; everything else calls `capture.run_capture` directly.

Run: python -m unittest tests.test_capture -v   (from the repo root)
"""

import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import capture
from _lib import EXIT_CLEAN, EXIT_ERRORS, EXIT_FAILURE, load_fha_yaml, read_record

SAMPLES = ROOT / 'tests' / 'fixtures' / 'capture-samples'


def _sample(name: str) -> str:
    return (SAMPLES / f'{name}.html').read_text(encoding='utf-8')


class CaptureTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.archive = self.tmp / 'archive'
        self.archive.mkdir()
        (self.archive / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8')
        self.config = load_fha_yaml(self.archive, strict=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _capture(self, html='', **kwargs) -> int:
        params = dict(url=None, title=None, source_type=None, source_date=None, asset=None)
        params.update(kwargs)
        return capture.run_capture(self.archive, self.config, html=html, **params)

    def _only_stub(self) -> Path:
        stubs = list((self.archive / 'inbox').glob('*.notes.md'))
        self.assertEqual(len(stubs), 1, f'expected exactly one stub, got {stubs}')
        return stubs[0]

    # ── M7.5 generic ──────────────────────────────────────────────────────────

    def test_generic_stub_frontmatter_and_log_fallback(self) -> None:
        html = ('<html><head><title>Test Page</title>'
                '<link rel="canonical" href="https://example.com/rec/1"></head>'
                '<body><h1>Heading</h1><p>Body text here.</p>'
                '<script>ignored()</script></body></html>')
        rc = self._capture(html, url='https://example.com/rec/1')
        self.assertEqual(rc, EXIT_CLEAN)

        rec = read_record(self._only_stub())
        self.assertEqual(rec['parse_errors'], [])           # stub re-parses cleanly
        self.assertEqual(rec['meta']['title'], 'Test Page')
        self.assertEqual(rec['meta']['source_type'], 'website')
        self.assertEqual(rec['meta']['repository'], 'example.com')
        self.assertEqual(rec['meta']['external_links'][0]['url'], 'https://example.com/rec/1')
        self.assertIn('Body text here.', rec['body'])
        self.assertNotIn('ignored', rec['body'])            # script body dropped

        # No index present → entry lands in the jsonl fallback.
        log = self.archive / '.cache' / 'capture_log.jsonl'
        self.assertTrue(log.exists())
        self.assertIn('example.com', log.read_text(encoding='utf-8'))

    def test_search_log_written_when_index_present(self) -> None:
        cache = self.archive / '.cache'
        cache.mkdir()
        conn = sqlite3.connect(str(cache / 'index.sqlite'))
        conn.execute(capture._SEARCH_LOG_DDL)
        conn.commit()
        conn.close()

        rc = self._capture('<html><title>Indexed</title></html>',
                           url='https://site.test/p')
        self.assertEqual(rc, EXIT_CLEAN)

        conn = sqlite3.connect(str(cache / 'index.sqlite'))
        rows = conn.execute(
            'SELECT question, result, path, source_id FROM search_log').fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 'Indexed')
        self.assertTrue(rows[0][1].startswith('staged inbox/'))
        self.assertIsNone(rows[0][3])                        # no S-id at capture time
        # Always also appended to the jsonl (durability across a search_log
        # drop/rebuild), even though the index row already exists.
        self.assertTrue((cache / 'capture_log.jsonl').exists())

    def test_flag_overrides_and_unknown_type_refused(self) -> None:
        html = '<html><title>Original</title></html>'
        rc = self._capture(html, url='https://x.test/p', title='My Title',
                           source_type='newspaper', source_date='1880')
        self.assertEqual(rc, EXIT_CLEAN)
        meta = read_record(self._only_stub())['meta']
        self.assertEqual(meta['title'], 'My Title')
        self.assertEqual(meta['source_type'], 'newspaper')
        self.assertEqual(meta['source_date'], '1880')

        with self.assertRaises(capture.CaptureError):
            self._capture(html, source_type='not-a-type')
        with self.assertRaises(capture.CaptureError):
            self._capture(html, source_date='June 1880')

    def test_dry_run_writes_nothing(self) -> None:
        asset = self.tmp / 'scan.jpg'
        asset.write_bytes(b'image')
        rc = self._capture('<html><title>Preview</title></html>',
                           url='https://x.test/p', asset=asset, dry_run=True)
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse((self.archive / 'inbox').exists())
        self.assertFalse((self.archive / '.cache').exists())

    def test_cli_write_failure_returns_tool_failure(self) -> None:
        args = SimpleNamespace(
            root=str(self.archive), url='https://x.test/p', title=None,
            source_type=None, source_date=None, asset=None, dry_run=False,
        )
        with (
            mock.patch('capture._read_html', return_value='<html><title>Fail</title></html>'),
            mock.patch.object(capture.Path, 'write_text', side_effect=OSError('disk full')),
        ):
            rc = capture._run_capture(args)
        self.assertEqual(rc, EXIT_FAILURE)
        self.assertFalse(list((self.archive / 'inbox').glob('*.notes.md')))

    def test_asset_copied_with_matching_stem(self) -> None:
        asset = self.tmp / 'scan.jpg'
        asset.write_bytes(b'\xff\xd8\xff jpeg-ish')
        rc = self._capture('<html><title>Photo Page</title></html>',
                           url='https://x.test/p', asset=asset)
        self.assertEqual(rc, EXIT_CLEAN)
        stub = self._only_stub()
        # Stub and asset share a stem so they pair by basename (SPEC §12.1).
        copied = stub.with_name(stub.name[:-len('.notes.md')] + '.jpg')
        self.assertTrue(copied.exists())
        self.assertEqual(copied.read_bytes(), b'\xff\xd8\xff jpeg-ish')

    def test_asset_collision_affects_stub_stem(self) -> None:
        inbox = self.archive / 'inbox'
        inbox.mkdir()
        (inbox / 'photo-page.jpg').write_bytes(b'existing')
        asset = self.tmp / 'scan.jpg'
        asset.write_bytes(b'new')

        rc = self._capture('<html><title>Photo Page</title></html>',
                           url='https://x.test/p', asset=asset)
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertTrue((inbox / 'photo-page-2.notes.md').exists())
        self.assertTrue((inbox / 'photo-page-2.jpg').exists())
        self.assertEqual((inbox / 'photo-page.jpg').read_bytes(), b'existing')

    def test_slug_collision_uniquifies(self) -> None:
        html = '<html><title>Same Title</title></html>'
        self._capture(html, url='https://x.test/1')
        self._capture(html, url='https://x.test/2')
        stubs = sorted(p.name for p in (self.archive / 'inbox').glob('*.notes.md'))
        self.assertEqual(stubs, ['same-title-2.notes.md', 'same-title.notes.md'])

    def test_cli_reads_utf8_stdin(self) -> None:
        # The CLI decodes stdin as UTF-8 (not the locale codec) — an en-dash in
        # a piped page must survive into the stub.
        raw = '<html><title>Smith–Jones</title></html>'.encode('utf-8')

        class _FakeStdin:
            buffer = io.BytesIO(raw)
            def isatty(self):  # noqa: D401 — piped, not a terminal
                return False

        orig = sys.stdin
        sys.stdin = _FakeStdin()
        try:
            rc = capture._standalone_main(['--root', str(self.archive),
                                           '--url', 'https://x.test/p'])
        finally:
            sys.stdin = orig
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('Smith–Jones', self._only_stub().read_text(encoding='utf-8'))

    # ── M7.6 / M7.7 recipes ─────────────────────────────────────────────────────

    def test_recipe_detection_is_mutually_exclusive(self) -> None:
        recipes = capture._load_site_recipes()
        names = {getattr(m, 'SOURCE_NAME', m.__name__): m for m in recipes}
        self.assertEqual(set(names), {'Ancestry', 'FamilySearch', 'Newspapers.com', 'FindAGrave'})

        urls = {
            'Ancestry': 'https://www.ancestry.com/discoveryui-content/view/1:2',
            'FamilySearch': 'https://www.familysearch.org/ark:/61903/1:1:X',
            'Newspapers.com': 'https://www.newspapers.com/clip/1/x/',
            'FindAGrave': 'https://www.findagrave.com/memorial/1/x',
        }
        samples = {
            'Ancestry': _sample('ancestry'),
            'FamilySearch': _sample('familysearch'),
            'Newspapers.com': _sample('newspapers'),
            'FindAGrave': _sample('findagrave'),
        }
        # Each recipe detects its own sample and rejects the other three.
        for owner, mod in names.items():
            for site, html in samples.items():
                detected = mod.detect(html, urls[site])
                self.assertEqual(detected, owner == site,
                                 f'{owner}.detect on {site} sample was {detected}')

    def test_ancestry_recipe(self) -> None:
        rc = self._capture(_sample('ancestry'),
                           url='https://www.ancestry.com/discoveryui-content/view/1:2')
        self.assertEqual(rc, EXIT_CLEAN)
        meta = read_record(self._only_stub())['meta']
        self.assertEqual(meta['source_type'], 'census')
        self.assertEqual(meta['repository'], 'Ancestry.com')
        self.assertIn('Calvin Hartley', meta['people'])
        self.assertIn('Edith Hartley', meta['people'])
        self.assertEqual(meta['source_date'], '1880')

    def test_familysearch_recipe(self) -> None:
        rc = self._capture(_sample('familysearch'),
                           url='https://www.familysearch.org/ark:/61903/1:1:X')
        self.assertEqual(rc, EXIT_CLEAN)
        meta = read_record(self._only_stub())['meta']
        self.assertEqual(meta['source_type'], 'vital-record')
        self.assertEqual(meta['repository'], 'FamilySearch')
        self.assertIn('Harriet Webb', meta['people'])

    def test_familysearch_recipe_label_value_fact_table(self) -> None:
        # A tree-person page lists facts as label/value rows (`Name | value`,
        # `Father's Name | value`) rather than the record-detail column shape;
        # the value cells must be read as people, not the labels themselves.
        rc = self._capture(_sample('familysearch-tree-person'),
                           url='https://www.familysearch.org/tree/person/details/ABCD-123')
        self.assertEqual(rc, EXIT_CLEAN)
        meta = read_record(self._only_stub())['meta']
        self.assertIn('John Smith', meta['people'])
        self.assertIn('William Smith', meta['people'])
        self.assertIn('Mary Smith', meta['people'])
        self.assertNotIn("Father's Name", meta['people'])
        self.assertNotIn("Mother's Name", meta['people'])

    def test_newspapers_recipe(self) -> None:
        rc = self._capture(_sample('newspapers'),
                           url='https://www.newspapers.com/clip/1/x/')
        self.assertEqual(rc, EXIT_CLEAN)
        meta = read_record(self._only_stub())['meta']
        self.assertEqual(meta['source_type'], 'newspaper')
        self.assertEqual(meta['repository'], 'The Fairview Gazette')
        self.assertEqual(meta['source_date'], '1898')

    def test_findagrave_recipe(self) -> None:
        rc = self._capture(_sample('findagrave'),
                           url='https://www.findagrave.com/memorial/1/x')
        self.assertEqual(rc, EXIT_CLEAN)
        rec = read_record(self._only_stub())
        meta = rec['meta']
        self.assertEqual(meta['repository'], 'Find a Grave')
        self.assertIn('Calvin George Hartley', meta['people'])
        self.assertNotIn('Family Members', meta['people'])   # header row not a person
        self.assertIn('Fairview Cemetery', rec['body'])
        self.assertEqual(meta['source_date'], '1929')

    # ── helpers ──────────────────────────────────────────────────────────────

    def test_visible_text_truncates_on_word_boundary(self) -> None:
        body = '<html><body>' + ('word ' * 600) + '</body></html>'
        text = capture.visible_text(body, cap=100)
        self.assertLessEqual(len(text), 104)
        self.assertTrue(text.endswith('…'))
        self.assertNotIn('wor…', text)                       # not sliced mid-word

    def test_domain_strips_www(self) -> None:
        self.assertEqual(capture.domain_of('https://www.Example.com/x'), 'example.com')
        self.assertEqual(capture.domain_of(None), '')


if __name__ == '__main__':
    unittest.main()
