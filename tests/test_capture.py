"""Tests for `fha capture` (BUILD_INGESTION.md MG1.1 generic + MG1.2/MG1.3 site recipes).

No network and no third-party HTML library: capture reads HTML handed to it
(here, the anonymized fixtures under tests/fixtures/capture-samples/) and parses
with the stdlib. The CLI's stdin path is exercised by monkeypatching
`sys.stdin`; everything else calls `capture.run_capture` directly.

Run: python -m unittest tests.test_capture -v   (from the repo root)
"""

import contextlib
import io
import shutil
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
from _lib import (
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    load_fha_yaml,
    read_record,
)

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

    # ── MG1.1 generic ─────────────────────────────────────────────────────────

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

        shutil.rmtree(self.archive / 'inbox')
        rc = self._capture(html, source_date='about 1880')
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(read_record(self._only_stub())['meta']['source_date'], '1880~')

        shutil.rmtree(self.archive / 'inbox')
        rc = self._capture(html, source_date='June 1880')
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(read_record(self._only_stub())['meta']['source_date'], '1880-06')

        with self.assertRaises(capture.CaptureError) as type_ctx:
            self._capture(html, source_type='not-a-type')
        self.assertIn('source category', str(type_ctx.exception))
        self.assertIn('census', str(type_ctx.exception))
        self.assertIn('photo', str(type_ctx.exception))

        with self.assertRaises(capture.CaptureError) as date_ctx:
            self._capture(html, source_date='last summer')
        self.assertIn('date the archive can read', str(date_ctx.exception))
        self.assertIn('1880-06-15', str(date_ctx.exception))

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

    def _mode_conflict_rc(self, **flags) -> tuple[int, str]:
        args = SimpleNamespace(root=str(self.archive), **flags)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = capture._run_capture(args)
        return rc, err.getvalue()

    def test_ingest_with_page_flag_refuses_and_stages_nothing(self) -> None:
        rc, err = self._mode_conflict_rc(ingest=True, url='https://x.test/p')
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('--url', err)
        self.assertIn('--ingest', err)
        self.assertIn('two separate commands', err)
        self.assertFalse(list((self.archive / 'inbox').glob('*.notes.md')))

    def test_host_with_page_flag_refuses(self) -> None:
        rc, err = self._mode_conflict_rc(host=True, title='Stray Title')
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('--title', err)
        self.assertIn('--host', err)

    def test_install_host_option_on_ingest_refuses(self) -> None:
        rc, err = self._mode_conflict_rc(ingest=True, extension_id='abc123')
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('--extension-id', err)
        self.assertIn('--ingest', err)

    def test_two_mode_flags_refuse(self) -> None:
        rc, err = self._mode_conflict_rc(host=True, ingest=True)
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('--ingest', err)
        self.assertIn('--host', err)

    def test_path_with_url_refuses_and_writes_nothing(self) -> None:
        # --path is mutually exclusive with the capture-from-page flow.
        rc, err = self._mode_conflict_rc(path='C:/photos/x.jpg', url='https://x.test/p')
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('--path', err)
        self.assertIn('--url', err)
        self.assertIn('two separate commands', err)
        self.assertFalse((self.archive / 'inbox').exists())

    def test_path_with_asset_refuses(self) -> None:
        rc, err = self._mode_conflict_rc(path='C:/photos/x.jpg', asset='scan.html')
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('--path', err)
        self.assertIn('--asset', err)

    def test_note_without_path_refuses(self) -> None:
        # --note only means something alongside --path.
        rc, err = self._mode_conflict_rc(note='a note with nowhere to go')
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('--note', err)

    def test_path_with_title_is_allowed(self) -> None:
        # --title is shared between page capture and --path (it labels either).
        rc, err = self._mode_conflict_rc(ingest=True, path='C:/photos/x.jpg')
        self.assertEqual(rc, EXIT_ERRORS)  # path vs ingest still conflicts...
        self.assertIn('--path', err)
        # ...but --title alongside --path alone is NOT a mode conflict.
        args = SimpleNamespace(
            root=str(self.archive), path=str(self.tmp / 'missing.jpg'),
            title='My Photo', note=None, dry_run=True,
        )
        err2 = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err2):
            rc2 = capture._run_capture(args)
        self.assertEqual(rc2, EXIT_CLEAN)
        self.assertNotIn('--title', err2.getvalue())

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
        # The CLI decodes stdin as UTF-8 (not the locale codec) - an en-dash in
        # a piped page must survive into the stub.
        raw = '<html><title>Smith–Jones</title></html>'.encode('utf-8')

        class _FakeStdin:
            buffer = io.BytesIO(raw)
            def isatty(self):  # noqa: D401 - piped, not a terminal
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

    def test_generic_prefers_ogtitle_over_junk_page_title(self) -> None:
        # EX19 shape: og:title is clean; page.title is a print-shop run-on.
        rc = self._capture(_sample('open-archive-luna'),
                           url='https://maps.example.com/luna/detail/9981')
        self.assertEqual(rc, EXIT_CLEAN)
        meta = read_record(self._only_stub())['meta']
        self.assertEqual(meta['title'], 'National City and Vicinity')
        self.assertEqual(meta['source_date'], '1887')        # harvested from og:description

    def test_generic_strips_site_suffix_and_harvests_title_year(self) -> None:
        # EX18 shape: no og:title; strip the " | Site" chrome and harvest the
        # year from the title.
        rc = self._capture(_sample('open-archive-islandora'),
                           url='https://digital.example.edu/items/fairview-1910')
        self.assertEqual(rc, EXIT_CLEAN)
        meta = read_record(self._only_stub())['meta']
        self.assertEqual(meta['title'], 'Panoramic map of Fairview, 1910')
        self.assertEqual(meta['source_date'], '1910')

    # ── MG1.2 / MG1.3 recipes ───────────────────────────────────────────────────

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

    def test_ancestry_imageviewer_grid_people_and_hint_title(self) -> None:
        # EX5 shape: no household <table>; the detail panel is a grid of
        # grid-cell divs (Surname/Given Name columns). The grid reader recovers
        # the people the table path returns [] for, and the tree-hint <h1> is
        # kept out of the title.
        rc = self._capture(_sample('ancestry-imageviewer'),
                           url='https://www.ancestry.com/imageviewer/collections/2442/images/x')
        self.assertEqual(rc, EXIT_CLEAN)
        meta = read_record(self._only_stub())['meta']
        self.assertEqual(meta['source_type'], 'census')
        self.assertEqual(meta['source_date'], '1940')
        self.assertIn('Calvin Hartley', meta['people'])           # Given Name + Surname
        self.assertIn('Edith Hartley', meta['people'])
        self.assertNotIn('Given Name', meta['people'])            # label never leaks
        self.assertNotIn('Surname', meta['people'])
        self.assertNotIn('Does', meta['title'])                   # hint prompt not the title

    def test_ancestry_offsite_index_pointer(self) -> None:
        # An Ancestry "Index" record embedding a Newspapers.com clip URL surfaces
        # it as an external link so the reviewer can go upstream (WS B.5).
        html = ('<html><head><meta property="og:site_name" content="Ancestry">'
                '<meta property="og:title" content="California Newspapers Index">'
                '</head><body><a href="https://www.newspapers.com/clip/152871046/">'
                'View on Newspapers.com</a></body></html>')
        rc = self._capture(html, url='https://www.ancestry.com/search/collections/9/records/5')
        self.assertEqual(rc, EXIT_CLEAN)
        meta = read_record(self._only_stub())['meta']
        urls = [link['url'] for link in meta['external_links']]
        self.assertIn('https://www.newspapers.com/clip/152871046/', urls)

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
        self.assertNotIn('Birth Date', meta['people'])       # fact label, not a person

    def test_familysearch_content_page_grid_people(self) -> None:
        # EX13 shape: React content panel, no table. The subject comes from the
        # "Given Name:/Surname:" text and the household from the "Others on This
        # Record" data-testid names. title/date/type already work; people was [].
        rc = self._capture(_sample('familysearch-content'),
                           url='https://www.familysearch.org/ark:/61903/3:1:XXYY-ZZZ')
        self.assertEqual(rc, EXIT_CLEAN)
        meta = read_record(self._only_stub())['meta']
        self.assertEqual(meta['source_type'], 'census')
        self.assertEqual(meta['source_date'], '1860')
        self.assertIn('William W Church', meta['people'])         # subject, not []
        self.assertIn('Caleb C Church', meta['people'])           # household member
        self.assertNotIn('Given Name', meta['people'])            # label never leaks

    def test_familysearch_index_page_title_collection_split(self) -> None:
        # EX14 shape: the <title> is the person with the collection quoted; the
        # split recovers the collection so the type re-derives (vital-record,
        # not the generic 'website') and the person is captured, not the labels.
        rc = self._capture(_sample('familysearch-index'),
                           url='https://www.familysearch.org/ark:/61903/1:1:AAAA-111')
        self.assertEqual(rc, EXIT_CLEAN)
        meta = read_record(self._only_stub())['meta']
        self.assertEqual(meta['source_type'], 'vital-record')
        self.assertEqual(meta['source_date'], '1905')
        self.assertEqual(meta['title'], 'California, Birth Index, 1905-1995')
        self.assertIn('Mark B Sielen', meta['people'])
        self.assertNotIn('Event Type', meta['people'])
        self.assertNotIn('Event Place', meta['people'])

    def test_newspapers_recipe(self) -> None:
        rc = self._capture(_sample('newspapers'),
                           url='https://www.newspapers.com/clip/1/x/')
        self.assertEqual(rc, EXIT_CLEAN)
        meta = read_record(self._only_stub())['meta']
        self.assertEqual(meta['source_type'], 'newspaper')
        self.assertEqual(meta['repository'], 'The Fairview Gazette')
        # No "Mon DD, YYYY" phrase in og:description here → year-only fallback
        # (proves the harvest_date path still fires).
        self.assertEqual(meta['source_date'], '1898')

    def test_newspapers_wholepage_full_date_and_clean_citation(self) -> None:
        # EX3 shape: nav-style og:title, full date in og:description, no clip id.
        rc = self._capture(_sample('newspapers-wholepage'),
                           url='https://www.newspapers.com/image/1046785212/')
        self.assertEqual(rc, EXIT_CLEAN)
        meta = read_record(self._only_stub())['meta']
        self.assertEqual(meta['source_date'], '1884-08-07')        # full ISO date
        # The nav-style page title is not quoted into the citation as a headline.
        self.assertNotIn('"Aug 07, 1884', meta['citation'])
        self.assertIn('The Fairview Gazette, 7 Aug 1884, p. 3', meta['citation'])
        # No clipping_id → only the viewer URL, no clip link.
        self.assertEqual([link['url'] for link in meta['external_links']],
                         ['https://www.newspapers.com/image/1046785212/'])

    def test_familysearch_multiword_surname(self) -> None:
        # A spaced surname (Van Buren / De La Cruz) must be kept whole, not
        # truncated to its first token, and must stop at the next fact label.
        sys.path.insert(0, str(ROOT / 'tools'))
        from capture_recipes import familysearch
        self.assertEqual(
            familysearch._subject_from_text('Given Name: Martin Surname: Van Buren Sex: Male'),
            'Martin Van Buren')
        self.assertEqual(
            familysearch._subject_from_text(
                'Given Name: Ana Surname: De La Cruz Birth Date: 1880'),
            'Ana De La Cruz')
        # A possessive relationship label ("Mother's Name:") is a boundary too.
        self.assertEqual(
            familysearch._subject_from_text(
                "Given Name: Martin Surname: Van Buren Mother's Name: Jane"),
            'Martin Van Buren')

    def test_generic_keeps_hyphen_descriptor_and_year(self) -> None:
        # A record title using a plain hyphen ("Jane Smith - 1920 Census") is not
        # site chrome: keep the descriptor and let the year harvest see 1920.
        sys.path.insert(0, str(ROOT / 'tools'))
        import capture as capture_mod
        self.assertEqual(
            capture_mod._strip_site_suffix('Jane Smith - 1920 Census'),
            'Jane Smith - 1920 Census')
        # A genuine " | Site" tail is still stripped.
        self.assertEqual(
            capture_mod._strip_site_suffix('Panoramic map, 1910 | Example Libraries'),
            'Panoramic map, 1910')

    def test_newspapers_parses_sept_abbreviation(self) -> None:
        # "Sept" is a very common dateline abbreviation strptime rejects; the
        # recipe must still recover the full date instead of degrading to a year.
        sys.path.insert(0, str(ROOT / 'tools'))
        from capture_recipes import newspapers
        iso, cite = newspapers._parse_full_date('Sept 05, 1900')
        self.assertEqual(iso, '1900-09-05')
        self.assertEqual(cite, '5 Sep 1900')

    def test_newspapers_clip_emits_public_clip_link(self) -> None:
        # EX9 shape: clipping_id on the URL → durable public clip link emitted.
        rc = self._capture(
            _sample('newspapers-clip'),
            url='https://www.newspapers.com/image/1046785212/?match=1&clipping_id=152871046')
        self.assertEqual(rc, EXIT_CLEAN)
        meta = read_record(self._only_stub())['meta']
        self.assertEqual(meta['source_date'], '1904-03-10')
        # The clip link is the durable, subscription-free citation anchor. The
        # stub frontmatter persists url + accessed per link (not the recipe's
        # `label`), and the viewer URL stays first.
        links = meta['external_links']
        self.assertEqual(links[0]['url'],
                         'https://www.newspapers.com/image/1046785212/?match=1&clipping_id=152871046')
        self.assertEqual(links[1]['url'], 'https://www.newspapers.com/clip/152871046/')

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

    def test_findagrave_date_from_title_and_memorial_park(self) -> None:
        # B2: lifespan only in the title → source_date still populates.
        # B3: a "Memorial Park" burial place is found, and the "Virtual
        # Cemetery" decoy is rejected.
        rc = self._capture(_sample('findagrave-memorial-park'),
                           url='https://www.findagrave.com/memorial/28906345/frances-dodson')
        self.assertEqual(rc, EXIT_CLEAN)
        rec = read_record(self._only_stub())
        meta = rec['meta']
        self.assertEqual(meta['source_date'], '1921')
        # The place hint is the Memorial Park, not the "Virtual Cemetery" decoy.
        self.assertIn('place_text hint): Greenwood Memorial Park', rec['body'])
        self.assertNotIn('place_text hint): Ancestry Virtual Cemetery', rec['body'])

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


class CaptureRootGuardTestCase(unittest.TestCase):
    """`fha capture --root <non-archive>` must refuse (exit 3) and create
    NOTHING (round-2 finding 10). Empirically, before the shared
    resolve_root_arg guard: exit 0 and a stub staged into `<typo>/inbox` -
    real capture evidence filed into a folder that is not the archive."""

    def test_non_archive_root_refused_and_stages_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            typo = Path(tmp) / 'typo-root'
            typo.mkdir()
            err = io.StringIO()
            with (
                mock.patch('capture._read_html',
                           return_value='<html><title>T</title></html>'),
                contextlib.redirect_stderr(err),
            ):
                rc = capture._run_capture(SimpleNamespace(
                    root=str(typo), url='https://x.test/p', title=None,
                    source_type=None, source_date=None, asset=None, dry_run=False,
                ))
            self.assertEqual(rc, EXIT_FAILURE)
            # The empirical heart of the finding: no inbox, no .cache, nothing.
            self.assertEqual(list(typo.iterdir()), [])
            self.assertIn('does not look like an archive', err.getvalue())
            self.assertIn('fha capture', err.getvalue())

    def test_root_with_fha_yaml_still_captures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
            with mock.patch('capture._read_html',
                            return_value='<html><title>Kept Page</title></html>'):
                rc = capture._run_capture(SimpleNamespace(
                    root=str(root), url='https://x.test/p', title=None,
                    source_type=None, source_date=None, asset=None, dry_run=False,
                ))
            self.assertEqual(rc, EXIT_CLEAN)
            self.assertTrue(list((root / 'inbox').glob('*.notes.md')))


class CapturePathTestCase(unittest.TestCase):
    """`fha capture --path` - register a must-never-move asset (TOOLING §13b's
    "the photo library is never reorganized" case). No HTML, no stdin, no
    asset copy - exactly one pointer stub in the inbox."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.archive = self.tmp / 'archive'
        self.archive.mkdir()
        (self.archive / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8')
        self.config = load_fha_yaml(self.archive, strict=True)
        self.target = self.tmp / 'library' / 'grandma-wedding.jpg'
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b'not-a-real-jpeg')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _only_stub(self) -> Path:
        stubs = list((self.archive / 'inbox').glob('*.notes.md'))
        self.assertEqual(len(stubs), 1, f'expected exactly one stub, got {stubs}')
        return stubs[0]

    def test_writes_one_pointer_stub_and_never_touches_target(self) -> None:
        before = self.target.read_bytes()
        rc = capture.run_capture_path(
            self.archive, self.config, path=str(self.target)).exit_code
        self.assertEqual(rc, EXIT_CLEAN)

        inbox_files = list((self.archive / 'inbox').iterdir())
        self.assertEqual(len(inbox_files), 1)          # ONE file only
        stub = self._only_stub()
        self.assertEqual(stub.name, 'grandma-wedding.notes.md')

        rec = read_record(stub)
        self.assertEqual(rec['parse_errors'], [])
        self.assertTrue(rec['meta']['asset_elsewhere'])
        self.assertEqual(
            rec['meta']['asset_path'], str(self.target).replace('\\', '/'))
        self.assertEqual(
            rec['meta']['asset_path_absolute'],
            str(self.target.resolve()).replace('\\', '/'))

        # The target itself is completely untouched.
        self.assertTrue(self.target.is_file())
        self.assertEqual(self.target.read_bytes(), before)

    def test_note_and_title_land_in_the_stub(self) -> None:
        rc = capture.run_capture_path(
            self.archive, self.config, path=str(self.target),
            note='Found in the cedar chest.', title='Grandma\'s Wedding',
        ).exit_code
        self.assertEqual(rc, EXIT_CLEAN)
        rec = read_record(self._only_stub())
        self.assertEqual(rec['meta']['title'], "Grandma's Wedding")
        self.assertIn('Found in the cedar chest.', rec['body'])

    def test_no_note_gets_a_placeholder_body(self) -> None:
        rc = capture.run_capture_path(
            self.archive, self.config, path=str(self.target)).exit_code
        self.assertEqual(rc, EXIT_CLEAN)
        rec = read_record(self._only_stub())
        self.assertIn('no note given', rec['body'])

    def test_stem_collision_gets_dash_two(self) -> None:
        inbox = self.archive / 'inbox'
        inbox.mkdir()
        (inbox / 'grandma-wedding.notes.md').write_text(
            '---\nasset_elsewhere: true\n---\n\nan older registration\n',
            encoding='utf-8')
        rc = capture.run_capture_path(
            self.archive, self.config, path=str(self.target)).exit_code
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertTrue((inbox / 'grandma-wedding-2.notes.md').exists())
        # The original stub is untouched.
        self.assertIn('an older registration',
                      (inbox / 'grandma-wedding.notes.md').read_text(encoding='utf-8'))

    def test_missing_target_warns_but_still_writes(self) -> None:
        # The house engine contract: run_capture_path returns a Result and
        # never prints - the warning lives in Result.messages, not stderr.
        missing = self.tmp / 'library' / 'does-not-exist.jpg'
        result = capture.run_capture_path(self.archive, self.config, path=str(missing))
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        texts = ' '.join(m.text for m in result.messages)
        self.assertIn('not found right now', texts)
        self.assertIn('may be unplugged', texts)
        self.assertTrue(any(m.level == 'warning' for m in result.messages))
        # Forgiving: the stub is still written despite the warning.
        stub = self._only_stub()
        self.assertTrue(stub.read_text(encoding='utf-8'))

    def test_dry_run_writes_nothing(self) -> None:
        result = capture.run_capture_path(
            self.archive, self.config, path=str(self.target), dry_run=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertFalse((self.archive / 'inbox').exists())
        texts = ' '.join(m.text for m in result.messages)
        self.assertIn('grandma-wedding.notes.md', texts)
        self.assertIn('asset_path', texts)

    def test_dry_run_on_missing_target_still_exits_clean_but_warns(self) -> None:
        # Every other dry-run branch in this module reports a clean preview
        # regardless of what a live run would warn about; --path matches.
        missing = self.tmp / 'library' / 'does-not-exist.jpg'
        result = capture.run_capture_path(
            self.archive, self.config, path=str(missing), dry_run=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        texts = ' '.join(m.text for m in result.messages)
        self.assertIn('not found right now', texts)
        self.assertFalse((self.archive / 'inbox').exists())

    def test_cli_end_to_end(self) -> None:
        args = SimpleNamespace(
            root=str(self.archive), path=str(self.target), note='via CLI',
            title=None, dry_run=False,
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = capture._run_capture(args)
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('Registered', out.getvalue())
        rec = read_record(self._only_stub())
        self.assertIn('via CLI', rec['body'])


class LabelGuardTestCase(unittest.TestCase):
    """The shared `_common.py` label-as-people guard (capture-frontend-01 WS-A).

    Field labels ("Birth Date", "Event Type", …) leaked into the people list
    across the Ancestry / FamilySearch / Find a Grave recipes. The guard is
    site-neutral, so it is tested directly against `_common`.
    """

    # Real labels seen leaking in the EX1/EX2/EX4/EX6 corpus - none may pass.
    LABELS = [
        'Birth Date', 'Death Date', 'Residence Place', 'Residence Date',
        'Marital Status', 'Event Type', 'Event Place', 'Newspaper Title',
        'Estimated Birth Year', 'Highest Grade Completed',
        'Relation to Head of House', "Father's Name", "Mother's Name",
        'Name at Birth', 'Household Members', 'Family Members',
    ]
    # Genuine names, including surnames that brush against label vocabulary.
    NAMES = [
        'Calvin Hartley', 'Edith May Hartley', 'Harriet Webb',
        "Mary O'Brien", 'Jean-Luc Picard', 'John Q. Adams',
        'Larry Page', 'John Ward', 'Sarah Young',
        # Surnames that collide with field-label tail words: must still pass.
        'Mary Place', 'John Race', 'Anna Roll',
    ]

    def setUp(self) -> None:
        sys.path.insert(0, str(ROOT / 'tools'))
        from capture_recipes import _common
        self.common = _common

    def test_labels_never_look_like_names(self) -> None:
        for label in self.LABELS:
            self.assertTrue(self.common.is_field_label(label), f'{label!r} not a label')
            self.assertFalse(self.common.looks_like_name(label),
                             f'{label!r} leaked as a name')

    def test_real_names_pass(self) -> None:
        for name in self.NAMES:
            self.assertFalse(self.common.is_field_label(name), f'{name!r} flagged as label')
            self.assertTrue(self.common.looks_like_name(name), f'{name!r} rejected as a name')

    def test_label_value_rows_read_the_value(self) -> None:
        rows = [
            ['Name', 'John Smith'],
            ["Father's Name", 'William Smith'],
            ['Birth Date', '1850'],
            ['Marital Status', 'Married'],
            ['Event Type', 'Birth'],
        ]
        self.assertEqual(self.common.people_from_table(rows),
                         ['John Smith', 'William Smith'])

    def test_household_rows_read_the_name(self) -> None:
        # A genuine household/index table: first column IS the person.
        rows = [
            ['Name', 'Relationship', 'Age'],          # header row, skipped
            ['Calvin Hartley', 'Head', '45'],
            ['Edith Hartley', 'Wife', '42'],
        ]
        self.assertEqual(self.common.people_from_table(rows),
                         ['Calvin Hartley', 'Edith Hartley'])

    def test_bare_label_with_no_value_is_dropped(self) -> None:
        # A known label in a single-column row must never leak as a person.
        self.assertEqual(self.common.people_from_table([['Birth Date'], ['Event Type']]), [])

    def test_collision_surname_in_household_row_survives(self) -> None:
        # A person whose surname doubles as a field-label word ("Place") must
        # not be mistaken for a label and dropped from a household table.
        rows = [
            ['Name', 'Relationship', 'Age'],          # header row, skipped
            ['Mary Place', 'Head', '38'],
            ['John Race', 'Boarder', '24'],
        ]
        self.assertEqual(self.common.people_from_table(rows),
                         ['Mary Place', 'John Race'])

    def test_domain_place_label_still_caught(self) -> None:
        # "<domain> Place" remains a label even though bare "Place" is a surname.
        for label in ('Birth Place', 'Event Place', 'Residence Place'):
            self.assertTrue(self.common.is_field_label(label), f'{label!r} not a label')

    def test_place_value_is_not_promoted_to_a_person(self) -> None:
        # A place/date/status label's value is NOT a person; only a name-bearing
        # label ("Father's Name") reads its value cell.
        rows = [
            ['Birth Place', 'New York'],
            ['Event Place', 'Los Angeles'],
            ['Marital Status', 'Married'],
            ["Father's Name", 'William Smith'],   # name-bearing → value IS read
        ]
        self.assertEqual(self.common.people_from_table(rows), ['William Smith'])


if __name__ == '__main__':
    unittest.main()
