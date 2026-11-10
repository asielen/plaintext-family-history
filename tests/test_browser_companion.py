"""Tests for the browser-companion extension (TOOLING_INGESTION §5).

The extension itself is JavaScript that only runs inside a browser, so there is
no in-process way to exercise its DOM/fetch/download code here. What we *can*
verify without a browser is the three things that actually keep the companion
honest:

  1. The MV3 manifest is well-formed and every file it names exists - the most
     common way an unpacked extension silently breaks.
  2. The companion's OUTPUT contract holds: the committed `test-bundle/` (built in
     the exact shape `panel.js`/`bundle.js` write - `page.html` + the schema-2
     asset files + `capture.json`) sweeps cleanly through `fha capture --ingest`.
     The example is the "both" case: a self-contained page copy (role `webpage`)
     AND a separate record evidence file (role `record`), so it must land as a
     SPEC §12.1 inbox BUNDLE FOLDER (`notes.md` + both assets, with per-file role
     hints), the shape `fha process` later dissolves into one source whose
     `files:` inventory lists every asset. This is the seam the extension exists
     to fill (§3), tied here to the live backend.
  3. The committed sample stays PRODUCIBLE by the shipping panel: every JSON key
     the test-bundle capture.json uses must still appear at the actual EMISSION
     SITES in the panel / capture-json source - the panel's asset-entry literal
     and build()'s output-field assignments, extracted by regex - not merely
     somewhere in the file text (the drift guard that pins features like the
     provisional-screenshot flag to the code that must emit them).

Run: python -m unittest tests.test_browser_companion -v   (from the repo root)
"""

import base64
import contextlib
import io
import os
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import capture
from _lib import EXIT_CLEAN, load_fha_yaml, read_record

COMPANION = ROOT / 'browser-companion'
EXAMPLE_BUNDLE = COMPANION / 'test-bundle' / '1880-census-thomas-hartley-20260624-101500'


class ManifestTestCase(unittest.TestCase):
    """The MV3 manifest is valid and self-consistent with the files on disk."""

    def setUp(self) -> None:
        self.manifest = json.loads((COMPANION / 'manifest.json').read_text(encoding='utf-8'))

    def test_is_manifest_v3(self) -> None:
        self.assertEqual(self.manifest['manifest_version'], 3)
        self.assertTrue(self.manifest['name'])
        self.assertTrue(self.manifest['version'])

    def test_least_privilege_permissions(self) -> None:
        perms = set(self.manifest.get('permissions', []))
        # The §5.4 least-privilege set (sidePanel added for the panel UX - see the
        # browser-companion README "Deviations" note).
        self.assertEqual(
            perms, {'activeTab', 'scripting', 'downloads', 'storage', 'sidePanel'})
        # nativeMessaging stays OPTIONAL - the seamless host (§5.7) is opt-in.
        self.assertEqual(self.manifest.get('optional_permissions'), ['nativeMessaging'])

    def test_referenced_files_exist(self) -> None:
        """Every file the manifest points at must be present (no dangling refs)."""
        referenced = [
            self.manifest['background']['service_worker'],
            self.manifest['side_panel']['default_path'],
        ]
        for rel in referenced:
            self.assertTrue((COMPANION / rel).is_file(), f'missing {rel}')

    def test_content_script_and_panel_assets_present(self) -> None:
        # content.js is injected by path from panel.js; the panel loads its libs as
        # classic scripts. A rename that misses one of these breaks the live
        # extension silently, so pin them here.
        for rel in (
            'src/content.js',
            'src/panel.html',
            'src/panel.css',
            'src/panel.js',
            'src/lib/capture-json.js',
            'src/lib/bundle.js',
            'src/lib/native-host.js',
            'src/lib/newspapers.js',
        ):
            self.assertTrue((COMPANION / rel).is_file(), f'missing {rel}')


class ExampleBundleTestCase(unittest.TestCase):
    """The committed example bundle is shaped like real output and ingests clean."""

    def test_capture_json_matches_schema_2(self) -> None:
        cap = json.loads((EXAMPLE_BUNDLE / 'capture.json').read_text(encoding='utf-8'))
        # The schema constant the companion emits must equal the backend's.
        self.assertEqual(cap['schema'], capture._CAPTURE_JSON_SCHEMA)
        self.assertIn('url', cap)  # the one required field (§3)
        # Schema 2 carries an assets LIST (not the flat asset_mode/asset_file).
        self.assertIsInstance(cap['assets'], list)
        self.assertGreaterEqual(len(cap['assets']), 2)  # the "both" case
        roles = {a['role'] for a in cap['assets']}
        self.assertIn('webpage', roles)   # the page copy
        self.assertIn('record', roles)    # the evidence file
        # Every named asset file is actually present in the bundle.
        for a in cap['assets']:
            self.assertTrue((EXAMPLE_BUNDLE / a['file']).is_file(),
                            f"missing asset {a['file']}")

    def test_raw_page_html_always_present(self) -> None:
        # page.html (the scrape source) is always saved, separate from the assets.
        self.assertTrue((EXAMPLE_BUNDLE / 'page.html').is_file())

    def test_example_bundle_round_trips_into_a_bundle_folder(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            # run_ingest MOVES swept bundles into .ingested/, so never sweep the
            # committed copy in place - work on a throwaway staging copy.
            staging = tmp / 'staging'
            staging.mkdir()
            shutil.copytree(EXAMPLE_BUNDLE, staging / EXAMPLE_BUNDLE.name)

            archive = tmp / 'archive'
            archive.mkdir()
            (archive / 'fha.yaml').write_text(
                'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8')
            config = load_fha_yaml(archive, strict=True)

            res = capture.run_ingest(archive, config, staging_dir=str(staging))
            self.assertEqual(res.exit_code, EXIT_CLEAN)
            self.assertEqual(res.data['ingested'], 1)

            # The "both" case lands as a §12.1 inbox BUNDLE FOLDER, not a lone
            # sidecar: a folder holding notes.md + both assets.
            inbox = archive / 'inbox'
            folders = [p for p in inbox.iterdir() if p.is_dir()]
            self.assertEqual(len(folders), 1)
            bundle_dir = folders[0]
            self.assertTrue((bundle_dir / 'notes.md').is_file())
            self.assertTrue((bundle_dir / 'page-snapshot.html').is_file())
            self.assertTrue((bundle_dir / 'record.jpg').is_file())
            # page.html is the scrape source, consumed at ingest - NOT filed.
            self.assertFalse((bundle_dir / 'page.html').exists())
            # No lone-sidecar stub was written for this multi-asset capture.
            self.assertEqual(list(inbox.glob('*.notes.md')), [])

            rec = read_record(bundle_dir / 'notes.md')
            self.assertEqual(rec['parse_errors'], [])

            # The human's capture.json fields propagate into the stub.
            self.assertIn('Thomas Hartley', rec['meta']['people'])
            self.assertIn("Bob's great-grandfather's household", rec['body'])
            self.assertEqual(rec['meta']['external_links'][0]['accessed'], '2026-06-24')

            # Per-file role hints (the files: inventory `fha process` reads when it
            # dissolves the bundle) name both assets with their roles.
            files = rec['meta']['files']
            by_name = {f['file']: f.get('role') for f in files}
            self.assertEqual(by_name.get('page-snapshot.html'), 'webpage')
            self.assertEqual(by_name.get('record.jpg'), 'record')

            # Bundle parked, not deleted.
            self.assertTrue((staging / '.ingested' / EXAMPLE_BUNDLE.name).is_dir())

    def test_bundle_folder_dissolves_into_one_source(self) -> None:
        """The ingested bundle folder processes into a single source with both
        assets in its files: inventory - the full intake → source round-trip.

        The photo seam (exiftool) is mocked, as in tests/test_process.py, so the
        record.jpg evidence files without a real exiftool on the test machine.
        """
        import process
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            staging = tmp / 'staging'
            staging.mkdir()
            shutil.copytree(EXAMPLE_BUNDLE, staging / EXAMPLE_BUNDLE.name)

            archive = tmp / 'archive'
            archive.mkdir()
            (archive / 'fha.yaml').write_text(
                'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8')
            config = load_fha_yaml(archive, strict=True)

            capture.run_ingest(archive, config, staging_dir=str(staging))
            bundle_dir = next(p for p in (archive / 'inbox').iterdir() if p.is_dir())

            with mock.patch.object(process, '_run_exiftool_embed_source', return_value=None), \
                    mock.patch.object(process, '_run_exiftool_read_keywords', return_value=[]), \
                    mock.patch.object(process, '_run_exiftool_remove_source', return_value=None):
                exit_code = process.process_bundle(
                    archive, config, bundle_dir, source_date=None, dry_run=False)
            self.assertEqual(exit_code, EXIT_CLEAN)

            # One source record, its files: inventory listing both assets by role.
            sources = list((archive / 'sources').rglob('*.md'))
            self.assertEqual(len(sources), 1)
            rec = read_record(sources[0])
            self.assertEqual(rec['parse_errors'], [])
            roles = {f.get('role') for f in rec['meta']['files']}
            self.assertIn('webpage', roles)
            self.assertIn('record', roles)
            # The bundle folder dissolved (§12.1: grouping migrates to the S-id).
            self.assertFalse(bundle_dir.exists())

    def test_an_ancestry_index_with_no_scan_files_with_its_roles(self) -> None:
        """PR #213 audit #4: an Ancestry record with no scan. Alone, its
        record-data.json was filed as the source's `primary` file under a new
        name, so the role was lost and Seen It's helper (which reads
        `*site-data*.json`) no longer knew the record was filed. The companion
        now always keeps the page copy beside it; this is that bundle, through
        ingest and processing, and back through Seen It's helper."""
        import process
        sys.path.insert(0, str(ROOT / 'seen-it' / 'host'))
        import seenit_host
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            staging = tmp / 'staging'
            bundle = staging / 'ethel-hartley-death-index'
            bundle.mkdir(parents=True)
            url = 'https://www.ancestry.com/search/collections/90002/records/2000001'
            (bundle / 'page.html').write_text('<html><title>California, U.S., Death Index, 1940-1997 - '
                                              'Ancestry.com</title></html>', encoding='utf-8')
            (bundle / 'page-snapshot.html').write_text('<html>copy</html>', encoding='utf-8')
            (bundle / 'record-data.json').write_text(json.dumps({'record': {
                'dbId': '90002', 'recordId': '2000001', 'imageId': None, 'imageSaved': False,
                'url': url, 'name': 'Ethel Hartley'}}), encoding='utf-8')
            (bundle / 'capture.json').write_text(json.dumps({
                'schema': 2, 'url': url, 'title': 'Ethel Hartley - California, U.S., Death Index, 1940-1997',
                'source_type': 'vital-record', 'source_date': '1957-01-07', 'source_class': 'derivative',
                'recipe_hint': 'ancestry',
                'assets': [{'file': 'page-snapshot.html', 'role': 'webpage', 'mode': 'singlefile'},
                           {'file': 'record-data.json', 'role': 'site-data', 'mode': 'ancestry-page-data'}],
            }), encoding='utf-8')
            archive = tmp / 'archive'
            archive.mkdir()
            (archive / 'fha.yaml').write_text(
                'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8')
            config = load_fha_yaml(archive, strict=True)
            capture.run_ingest(archive, config, staging_dir=str(staging))
            bundle_dir = next(p for p in (archive / 'inbox').iterdir() if p.is_dir())
            self.assertEqual(process.process_bundle(archive, config, bundle_dir, source_date=None, dry_run=False),
                             EXIT_CLEAN)
            rec = read_record(next((archive / 'sources').rglob('*.md')))
            by_role = {f.get('role'): f['file'] for f in rec['meta']['files']}
            self.assertEqual(set(by_role), {'webpage', 'site-data'})
            self.assertIn('site-data', Path(by_role['site-data']).name)
            self.assertEqual(rec['meta']['source_class'], 'derivative')
            items, _ = seenit_host.scan_known({'filed': [str(archive / 'documents')]})
            self.assertEqual(items['rec:90002:2000001']['state'], 'filed')
            self.assertEqual(items['rec:90002:2000001']['sid'], rec['meta']['id'])


# The committed photo bundle: exactly what the panel stages on a shared
# photo's viewer page (browser-companion/tests/test-panel-ancestry.js holds
# the two equal, byte for byte).
PHOTO_BUNDLE = (COMPANION / 'test-bundle' /
                'thomas-and-margaret-hartley-at-the-fairview-farm-about-1925-20261110-101500-k3m9px')
PHOTO_MEDIA_ID = '7c0ffee0-1234-4abc-8def-0123456789ab'
PHOTO_VIEWER_URL = ('https://www.ancestry.com/mediaui-viewer/tree/90000001/person/90000002/media/'
                    + PHOTO_MEDIA_ID)
def _rel_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


# How Seen It keys a shared photo from a source's frontmatter address
# (seen-it/host/seenit_host.py ANC_MEDIA_RE, med:{mediaId}).
_SEEN_IT_MEDIA_RE = re.compile(
    r'ancestry\.[a-z.]+/mediaui-viewer/(?:[^?#\s]*/)?media/'
    r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', re.I)


class SharedPhotoTestCase(unittest.TestCase):
    """A photo another member shared on Ancestry files as a PHOTO source.

    The owner: "we should be able to download shared photos as sources and
    seen it should recognize them also." The panel's bundle - the photo, its
    Details as photo-data.json, the page copy - goes through `fha capture
    --ingest` and `fha process` like any capture: one photo source, its
    picture in the photo library under the name it was captured with (the
    archive never renames a photo there), and the viewer's address in its
    frontmatter, which is what Seen It reads to know the photo again.
    """

    def test_a_shared_photo_files_as_a_photo_source_seen_it_can_find(self) -> None:
        import process
        from unittest import mock
        photo_name = f'ancestry-{PHOTO_MEDIA_ID}-photo.jpg'
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            staging = tmp / 'staging'
            staging.mkdir()
            shutil.copytree(PHOTO_BUNDLE, staging / PHOTO_BUNDLE.name)
            archive = tmp / 'archive'
            archive.mkdir()
            (archive / 'fha.yaml').write_text(
                'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8')
            config = load_fha_yaml(archive, strict=True)

            res = capture.run_ingest(archive, config, staging_dir=str(staging))
            self.assertEqual(res.exit_code, EXIT_CLEAN)
            bundle_dir = next(p for p in (archive / 'inbox').iterdir() if p.is_dir())
            with mock.patch.object(process, '_run_exiftool_embed_source', return_value=None), \
                    mock.patch.object(process, '_run_exiftool_read_keywords', return_value=[]), \
                    mock.patch.object(process, '_run_exiftool_remove_source', return_value=None):
                self.assertEqual(process.process_bundle(
                    archive, config, bundle_dir, source_date=None, dry_run=False), EXIT_CLEAN)

            sources = list((archive / 'sources').rglob('*.md'))
            self.assertEqual(len(sources), 1, sources)
            self.assertEqual(sources[0].parent, archive / 'sources' / 'photos')
            rec = read_record(sources[0])
            self.assertEqual(rec['parse_errors'], [])
            meta = rec['meta']
            self.assertEqual(meta['source_type'], 'photo')
            self.assertEqual(meta['title'], 'Thomas and Margaret Hartley at the Fairview farm, about 1925')
            self.assertEqual(meta['source_date'], '1925~')
            # The citation names the year once - the title already says it
            # (PR #258 review: "...about 1925. 1925. Ancestry.com.").
            self.assertEqual(' '.join(str(meta['citation']).split()),
                             'Thomas and Margaret Hartley at the Fairview farm, about 1925. Ancestry.com. '
                             + PHOTO_VIEWER_URL)
            # Who first added the photo is another member's name: kept with
            # Ancestry's data (photo-data.json), not in the record a packet or
            # a shared site carries (PR #258 review, owner-away decision).
            self.assertNotIn('hartley_family_tree', sources[0].read_text(encoding='utf-8'))
            data_file = next((archive / 'documents').rglob('*site-data*.json'))
            self.assertEqual(json.loads(data_file.read_text(encoding='utf-8'))['details']['originallyAddedBy'],
                             'hartley_family_tree on 3 Mar 2015')

            # The picture: in the photo library, under its captured name, the
            # source's main image.
            self.assertTrue((archive / 'photos' / photo_name).is_file())
            by_role = {f['role']: f for f in meta['files']}
            # No copy of Ancestry's page: it lists other members (round 4).
            self.assertEqual(set(by_role), {'front', 'site-data'})
            self.assertEqual(Path(by_role['front']['file']).name, photo_name)
            self.assertTrue(by_role['front'].get('is_primary'))
            self.assertEqual((archive / 'photos' / photo_name).read_bytes(),
                             (PHOTO_BUNDLE / photo_name).read_bytes())

            # The viewer's address, without how she got there, in the
            # frontmatter - the one Seen It turns into med:{mediaId}.
            urls = [link['url'] for link in meta['external_links']]
            self.assertEqual(urls[0], PHOTO_VIEWER_URL)
            self.assertEqual(urls.count(PHOTO_VIEWER_URL), 1)
            self.assertNotIn('?', urls[0])
            m = _SEEN_IT_MEDIA_RE.search(urls[0])
            self.assertIsNotNone(m)
            self.assertEqual('med:' + m.group(1).lower(), 'med:' + PHOTO_MEDIA_ID)
            # Seen It's own helper (PR #256) finds it from the filed record.
            sys.path.insert(0, str(ROOT / 'seen-it' / 'host'))
            import seenit_host
            self.assertIn('med:' + PHOTO_MEDIA_ID, seenit_host._keys_from_url(urls[0]))
            items, _ = seenit_host.scan_known({'sources': [str(archive / 'sources')]})
            self.assertEqual(items['med:' + PHOTO_MEDIA_ID]['state'], 'filed')
            self.assertEqual(items['med:' + PHOTO_MEDIA_ID]['sid'], meta['id'])

            # Other members who saved the photo are named nowhere in the
            # archive - though the staged page.html, the scrape source that is
            # never filed, shows them.
            self.assertIn('cousin_member', (PHOTO_BUNDLE / 'page.html').read_text(encoding='utf-8'))
            for p in archive.rglob('*'):
                if p.is_file() and p.suffix in ('.md', '.json', '.html', '.txt', '.jsonl'):
                    self.assertNotIn('cousin_member', p.read_text(encoding='utf-8'), p)

    def _file_photo_twice(self, second_photo: bytes | None = None, keyword_on_filed: bool = False):
        """File the sample photo bundle, then a second bundle under the same
        photo name - the same bytes, or `second_photo`. `keyword_on_filed`
        gives the filed photo an APP1 block, as fha's SOURCE: keyword write
        does. Returns (first result, second result or refusal text, archive,
        second inbox folder)."""
        import process
        from unittest import mock
        photo_name = f'ancestry-{PHOTO_MEDIA_ID}-photo.jpg'
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, True)
        tmp = Path(td)
        archive = tmp / 'archive'
        archive.mkdir()
        (archive / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8')
        config = load_fha_yaml(archive, strict=True)
        codes = []
        for n in ('1', '2'):
            staging = tmp / ('staging' + n)
            staging.mkdir()
            bundle = staging / (PHOTO_BUNDLE.name + n)
            shutil.copytree(PHOTO_BUNDLE, bundle)
            if n == '2' and second_photo is not None:
                (bundle / photo_name).write_bytes(second_photo)
            capture.run_ingest(archive, config, staging_dir=str(staging))
            bundle_dir = next(p for p in (archive / 'inbox').iterdir() if p.is_dir())
            with mock.patch.object(process, '_run_exiftool_embed_source', return_value=None), \
                    mock.patch.object(process, '_run_exiftool_read_keywords', return_value=[]), \
                    mock.patch.object(process, '_run_exiftool_remove_source', return_value=None):
                try:
                    codes.append(process.process_bundle(archive, config, bundle_dir,
                                                        source_date=None, dry_run=False))
                except process.ProcessError as e:
                    codes.append(str(e))
            if n == '1' and keyword_on_filed:
                filed = archive / 'photos' / photo_name
                data = filed.read_bytes()
                body = b'Exif\x00\x00SOURCE: S-xxxxxxxxxx'
                app1 = b'\xff\xe1' + (len(body) + 2).to_bytes(2, 'big') + body
                filed.write_bytes(data[:2] + app1 + data[2:])
        return codes, archive, bundle_dir

    def test_a_second_copy_of_a_filed_photo_is_refused_in_plain_words(self) -> None:
        """PR #258 round 3 (Codex PRRT_kwDOS6vm6c61wZYJ): the same shared photo
        captured twice. The second bundle cannot be filed - the photo keeps its
        name in the photo library and is never renamed to make room. The
        refusal says the photo is already filed, as which source, and what to
        do with the copy - also after fha wrote its keyword into the filed
        photo (the picture is compared, not the file's bytes)."""
        for keyword in (False, True):
            codes, archive, bundle_dir = self._file_photo_twice(keyword_on_filed=keyword)
            sid = read_record(next((archive / 'sources').rglob('*.md')))['meta']['id']
            self.assertEqual(codes[0], EXIT_CLEAN)
            self.assertIn(f'is already in your photo library, filed as source {sid}', codes[1])
            self.assertIn('is a second copy of the same photo. Delete that folder if it adds nothing', codes[1])
            self.assertIn('Nothing was changed', codes[1])
            self.assertNotIn('destination already exists', codes[1])
            # One source, the filed photo untouched, the copy left for her.
            self.assertEqual(len(list((archive / 'sources').rglob('*.md'))), 1)
            self.assertEqual([p.name for p in (archive / 'photos').iterdir()],
                             [f'ancestry-{PHOTO_MEDIA_ID}-photo.jpg'])
            self.assertTrue(bundle_dir.is_dir())

    def test_another_version_of_a_filed_shared_photo_is_kept_with_its_source(self) -> None:
        """PR #258 round 5 (audit L): a larger download of a shared photo already
        filed - same Ancestry media id, so the same name, different pictures.
        Round 4 called it "a different photo ... rename it", and renaming dead-
        ends in a capture bundle, whose notes.md names the file. It is now
        another version of the same Ancestry photo, and the `--more` command
        the refusal gives files it with that source, as print b, without
        renaming anything in the photo library."""
        import process
        from unittest import mock
        photo_name = f'ancestry-{PHOTO_MEDIA_ID}-photo.jpg'
        larger = (b'\xff\xd8\xff\xdb\x00\x04\x00\x00\xff\xda\x00\x02 a larger fictional scan \xff\xd9')
        for second in (larger, b'not a picture at all'):
            codes, archive, bundle_dir = self._file_photo_twice(second_photo=second)
            sid = read_record(next((archive / 'sources').rglob('*.md')))['meta']['id']
            self.assertEqual(codes[0], EXIT_CLEAN)
            self.assertIn(f'is another version of the same Ancestry photo ({photo_name}), which is already filed '
                          f'as source {sid}; nothing was changed.', codes[1])
            command = (f'`fha process "photos/{photo_name}" --more "{_rel_posix(bundle_dir / photo_name, archive)}" '
                       f'front:b`, then delete the folder {_rel_posix(bundle_dir, archive)}: its photo is then with '
                       f'source {sid}.')
            self.assertIn(command, codes[1])
            self.assertNotIn('rename', codes[1].lower())
            self.assertNotIn('different photo', codes[1])
        # Her next step, as told: the version is kept with that source, as print b.
        keywords = {}
        with mock.patch.object(process, '_run_exiftool_embed_source',
                               side_effect=lambda path, s_id, **k: keywords.__setitem__(Path(path).name, s_id)), \
                mock.patch.object(process, '_run_exiftool_read_keywords',
                                  side_effect=lambda path: ([f'SOURCE: {sid}'] if Path(path).name == photo_name
                                                            and Path(path).parent.name == 'photos' else [])), \
                mock.patch.object(process, '_run_exiftool_remove_source', return_value=None):
            rc = process.attach_more(archive, load_fha_yaml(archive, strict=True), archive / 'photos' / photo_name,
                                     bundle_dir / photo_name, 'front', 'b', dry_run=False)
        self.assertEqual(rc, EXIT_CLEAN)
        print_b = f'ancestry-{PHOTO_MEDIA_ID}-photo-b.jpg'
        self.assertEqual(sorted(p.name for p in (archive / 'photos').iterdir()), sorted([photo_name, print_b]))
        self.assertEqual((archive / 'photos' / print_b).read_bytes(), b'not a picture at all')
        self.assertEqual(keywords.get(print_b), sid)
        files = read_record(next((archive / 'sources').rglob('*.md')))['meta']['files']
        self.assertIn({'file': f'photos/{print_b}', 'role': 'front', 'copy': 'b'},
                      [{k: f[k] for k in ('file', 'role', 'copy') if k in f} for f in files])

    def _file_named_photo_twice(self, name: str, first: bytes, second: bytes):
        """Two capture bundles from somewhere other than Ancestry, each with a
        photo called `name` (a camera's IMG_0001.jpg) and a page copy."""
        import process
        from unittest import mock
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, True)
        tmp = Path(td)
        archive = tmp / 'archive'
        archive.mkdir()
        (archive / 'fha.yaml').write_text('roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8')
        config = load_fha_yaml(archive, strict=True)
        codes = []
        for n, data in (('1', first), ('2', second)):
            staging = tmp / ('staging' + n)
            bundle = staging / f'family-photo-{n}'
            bundle.mkdir(parents=True)
            (bundle / 'page.html').write_text('<html><title>Family photo</title></html>', encoding='utf-8')
            (bundle / 'page-snapshot.html').write_text('<html>copy</html>', encoding='utf-8')
            (bundle / name).write_bytes(data)
            (bundle / 'capture.json').write_text(json.dumps({
                'schema': 2, 'url': f'https://photos.example.org/album/{n}', 'title': f'Family photo {n}',
                'source_type': 'photo',
                'assets': [{'file': name, 'role': 'front', 'mode': 'manual'},
                           {'file': 'page-snapshot.html', 'role': 'webpage', 'mode': 'singlefile'}]}), encoding='utf-8')
            capture.run_ingest(archive, config, staging_dir=str(staging))
            bundle_dir = next(p for p in (archive / 'inbox').iterdir() if p.is_dir())
            with mock.patch.object(process, '_run_exiftool_embed_source', return_value=None), \
                    mock.patch.object(process, '_run_exiftool_read_keywords', return_value=[]), \
                    mock.patch.object(process, '_run_exiftool_remove_source', return_value=None):
                try:
                    codes.append(process.process_bundle(archive, config, bundle_dir, source_date=None, dry_run=False))
                except process.ProcessError as e:
                    codes.append(str(e))
        return codes, archive, bundle_dir

    def test_a_different_photo_with_a_filed_photos_name_is_never_called_a_copy(self) -> None:
        """PR #258 round 4 (audit F): two different scans that share a name -
        two cameras' IMG_0001.jpg - are two photos. Told "a second copy ...
        delete it", she would delete the only copy of the second photo. Only a
        matching picture is a copy; a name alone gets the neutral words. In a
        capture bundle the name is also in notes.md, so the rename the words
        ask for names both places (round 5) - a rename of the file alone is
        refused when the folder is processed again."""
        one = b'\xff\xd8\xff\xe0\x00\x07JFIF\x00\xff\xda\x00\x04\x00\x00first fictional photo\xff\xd9'
        two = b'\xff\xd8\xff\xe0\x00\x07JFIF\x00\xff\xda\x00\x04\x00\x00second fictional photo\xff\xd9'
        codes, archive, bundle_dir = self._file_named_photo_twice('IMG_0001.jpg', one, two)
        sid = read_record(next((archive / 'sources').rglob('*.md')))['meta']['id']
        self.assertEqual(codes[0], EXIT_CLEAN)
        self.assertIn(f'a different photo with the same name (IMG_0001.jpg) is already in your photo library, '
                      f'filed as source {sid}; nothing was changed.', codes[1])
        self.assertIn("rename IMG_0001.jpg in inbox/family-photo-2 - and the same name in that folder's notes.md", codes[1])
        self.assertNotIn('Delete', codes[1])
        self.assertNotIn('copy', codes[1])
        self.assertTrue(bundle_dir.is_dir())

    def test_a_photo_the_tools_cannot_read_is_never_called_different_or_a_copy(self) -> None:
        """PR #258 round 5: neither file's picture can be read (a TIFF or a
        HEIC gives no picture-data hash). The tools cannot tell, so both
        possibilities are named - and deleting only if it is the same photo."""
        codes, archive, bundle_dir = self._file_named_photo_twice('scan_0001.tif', b'II*\x00first', b'II*\x00second')
        sid = read_record(next((archive / 'sources').rglob('*.md')))['meta']['id']
        self.assertEqual(codes[0], EXIT_CLEAN)
        self.assertIn(f'the tools could not tell whether inbox/family-photo-2/scan_0001.tif is the same photo as '
                      f'scan_0001.tif, which is already in your photo library, filed as source {sid}; nothing was '
                      f'changed. If it is the same photo and adds nothing, you can delete the folder', codes[1])
        self.assertIn('If it is a different photo, rename scan_0001.tif', codes[1])
        self.assertNotIn('a different photo with the same name', codes[1])
        self.assertNotIn('second copy', codes[1])

    def _old_addon_bundle(self, staging: Path, *, with_photo: bool) -> Path:
        """A capture of the shared photo's page by her current, older add-on
        (0.6.6 or earlier): an ordinary page capture - page.html and a copy of
        the page, which lists the other members who saved the photo and who
        uploaded it - and, if she chose "save the actual file", the page's
        largest image as record.jpg."""
        page = (ROOT / 'tests' / 'fixtures' / 'capture-samples' / 'ancestry-media-photo.html').read_text(encoding='utf-8')
        bundle = staging / 'thomas-and-margaret-hartley-20261110-101500-old000'
        bundle.mkdir(parents=True)
        (bundle / 'page.html').write_text(page, encoding='utf-8')
        (bundle / 'page-snapshot.html').write_text(page, encoding='utf-8')
        assets = [{'file': 'page-snapshot.html', 'role': 'webpage', 'mode': 'singlefile'}]
        if with_photo:
            shutil.copy(PHOTO_BUNDLE / f'ancestry-{PHOTO_MEDIA_ID}-photo.jpg', bundle / 'record.jpg')
            assets.insert(0, {'file': 'record.jpg', 'role': 'record', 'mode': 'fetch'})
        (bundle / 'capture.json').write_text(json.dumps({
            'schema': 2, 'url': PHOTO_VIEWER_URL + '?destTreeId=90000003&destPersonId=90000004&hid=h1#top',
            'title': 'Thomas and Margaret Hartley at the Fairview farm, about 1925', 'accessed': '2026-11-10',
            'repository': 'ancestry.com', 'recipe_hint': 'ancestry', 'people': [], 'notes': '',
            'assets': assets}), encoding='utf-8')
        return bundle

    def _archive(self, tmp: Path, photos: str = 'photos') -> tuple[Path, dict]:
        archive = tmp / 'archive'
        archive.mkdir()
        (archive / 'fha.yaml').write_text(f'roots:\n  photos: "{photos}"\n  documents: documents\n', encoding='utf-8')
        return archive, load_fha_yaml(archive, strict=True)

    def test_an_older_add_ons_capture_with_no_photo_is_not_filed_at_all(self) -> None:
        """PR #258 rounds 6-7 (P2, P3-D): an older add-on saved only a copy of
        the shared photo's page. Filed, it would be a photo source with no
        photo, which Seen It then showed as "in the archive", so she would
        never go back for the photo. It is not ingested: it is set aside in
        the staging folder's .not-filed/ (beside .ingested/, never deleted),
        she is told once - naming that folder - and later sweeps, doctor and
        Seen It leave it alone."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            staging = tmp / 'staging'
            bundle = self._old_addon_bundle(staging, with_photo=False)
            archive, config = self._archive(tmp)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                res = capture.run_ingest(archive, config, staging_dir=str(staging))
            # What she would see: nothing filed, and the capture kept whole in .not-filed/.
            inbox = archive / 'inbox'
            self.assertFalse(inbox.exists() and any(inbox.iterdir()), 'nothing may be filed')
            aside = staging / '.not-filed' / bundle.name
            self.assertTrue((aside / 'page-snapshot.html').is_file(), 'the capture is kept whole in .not-filed/')
            self.assertEqual(res.data['ingested'], 0)
            self.assertEqual(res.data.get('not_filed'), 1)
            self.assertEqual([o['status'] for o in res.data['bundles']], ['not-filed'])
            self.assertIn("This shared photo was captured with an older version of the add-on, which saved only a "
                          "copy of Ancestry's page (it lists other members) and not the photo. Nothing was filed. "
                          "Update the add-on (fha update-tools, then reload it in chrome://extensions), open the "
                          "photo again and press Capture this photo.", err.getvalue())
            self.assertIn(f"It is kept in {aside.as_posix()}; once you've captured the photo again with the updated "
                          "add-on, you can delete that folder.", err.getvalue())
            self.assertFalse(bundle.exists())
            self.assertTrue((aside / 'page-snapshot.html').is_file(), 'the capture is kept whole, never deleted')
            self.assertFalse((staging / '.ingested').exists())
            inbox = archive / 'inbox'
            self.assertFalse(inbox.exists() and any(inbox.iterdir()), 'nothing may be filed')
            # Said once: the next sweep is silent, and nothing is waiting.
            err2 = io.StringIO()
            with contextlib.redirect_stderr(err2):
                res2 = capture.run_ingest(archive, config, staging_dir=str(staging))
            self.assertNotIn('older version', err2.getvalue())
            self.assertEqual(res2.data.get('bundles', []), [])
            self.assertEqual(capture.staged_bundles(config, str(staging))[1], [])
            sys.path.insert(0, str(ROOT / 'seen-it' / 'host'))
            import seenit_host
            items, _ = seenit_host.scan_known({'sources': [str(archive / 'sources')], 'filed': [str(archive / 'documents')],
                                               'captured': [str(staging)]})
            self.assertNotIn('med:' + PHOTO_MEDIA_ID, items)

    def test_a_shared_photo_citation_never_names_the_page_heading(self) -> None:
        """PR #258 round 10 (audit W, privacy): the viewer page's first <h1> (or
        og:title) can be the sharer's tree name - another member's. The
        citation took its title from there, and the citation is what a shared
        site renders. It now names the photo by capture.json's title, else the
        tab's own <title>; nothing else is read from a shared photo's page."""
        tree = "cousin_member_one's Hartley-Whitcombe Tree"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            staging = tmp / 'staging'
            bundle = staging / PHOTO_BUNDLE.name
            shutil.copytree(PHOTO_BUNDLE, bundle)
            page = (bundle / 'page.html').read_text(encoding='utf-8')
            page = page.replace('<head>', '<head><meta property="og:title" content="' + tree + '">'
                                '<meta property="og:image" content="https://www.ancestry.com/tree-banner.jpg">', 1)
            page = page.replace('<main id="mediaViewer">', '<main id="mediaViewer"><h1>' + tree + '</h1>'
                                '<p>See https://www.newspapers.com/clip/1234/cousin-clip/</p>', 1)
            (bundle / 'page.html').write_text(page, encoding='utf-8')
            archive, config = self._archive(tmp)
            capture.run_ingest(archive, config, staging_dir=str(staging))
            meta = read_record(next((archive / 'inbox').rglob('notes.md')))['meta']
            self.assertEqual(' '.join(str(meta['citation']).split()),
                             'Thomas and Margaret Hartley at the Fairview farm, about 1925. Ancestry.com. ' + PHOTO_VIEWER_URL)
            self.assertEqual([link['url'] for link in meta['external_links']], [PHOTO_VIEWER_URL])
            for p in archive.rglob('*'):
                if p.is_file():
                    self.assertNotIn('Hartley-Whitcombe', p.read_text(encoding='utf-8', errors='replace'), p)

    def test_a_shared_photo_saved_as_jfif_or_webp_is_filed_not_set_aside(self) -> None:
        """PR #258 round 8 (regression from round 7): Chrome on Windows saves a
        "Save image as" JPEG as .jfif, or a photo as .webp. The no-picture rule
        judged the photo by its extension, so a good capture was set aside as
        "only its address was saved" - and capturing again could not help. The
        picture is now judged by its role and its bytes; the native host
        (`fha capture --host`) files it too."""
        photo = f'ancestry-{PHOTO_MEDIA_ID}-photo'
        for ext in ('.jfif', '.webp'):
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                staging = tmp / 'staging'
                bundle = staging / PHOTO_BUNDLE.name
                shutil.copytree(PHOTO_BUNDLE, bundle)
                data = (bundle / (photo + '.jpg')).read_bytes()
                if ext == '.webp':  # a WebP file's own first bytes
                    data = b'RIFF' + (len(data) + 4).to_bytes(4, 'little') + b'WEBP' + data
                (bundle / (photo + '.jpg')).unlink()
                (bundle / (photo + ext)).write_bytes(data)
                cj = json.loads((bundle / 'capture.json').read_text(encoding='utf-8'))
                for a in cj['assets']:
                    if a['file'] == photo + '.jpg':
                        a['file'], a['mode'] = photo + ext, 'manual'
                (bundle / 'capture.json').write_text(json.dumps(cj), encoding='utf-8')
                archive, config = self._archive(tmp)
                res = capture.run_ingest(archive, config, staging_dir=str(staging))
                self.assertEqual(res.data['ingested'], 1, ext)
                self.assertFalse((staging / '.not-filed').exists(), ext)
                self.assertTrue(any((archive / 'inbox').rglob(photo + ext)), ext)
                # The native host, given the same capture, files it too.
                msg = {'captureJson': cj, 'bundleName': 'host-' + ext[1:], 'pageHtml': '<html></html>',
                       'assets': [{'filename': photo + ext, 'base64': base64.b64encode(data).decode()},
                                  {'filename': 'photo-data.json',
                                   'base64': base64.b64encode((PHOTO_BUNDLE / 'photo-data.json').read_bytes()).decode()}]}
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    r = capture._host_ingest(archive, config, msg)
                self.assertTrue(r.get('ok'), (ext, r))

    def test_a_shared_photo_capture_with_no_picture_at_all_is_never_filed(self) -> None:
        """PR #258 round 7: the same refusal whenever a shared-photo capture
        holds no picture - also one with no page copy to leave out (an older
        add-on with the page copy turned off, a citation-and-link capture)."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            staging = tmp / 'staging'
            bundle = staging / 'thomas-old-no-copy'
            bundle.mkdir(parents=True)
            (bundle / 'page.html').write_text('<html><title>Hartley farm</title></html>', encoding='utf-8')
            (bundle / 'capture.json').write_text(json.dumps({
                'schema': 2, 'url': PHOTO_VIEWER_URL + '?destTreeId=90000003', 'title': 'Hartley farm',
                'accessed': '2026-11-10', 'recipe_hint': 'ancestry', 'assets': []}), encoding='utf-8')
            archive, config = self._archive(tmp)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                res = capture.run_ingest(archive, config, staging_dir=str(staging))
            self.assertEqual(res.data['ingested'], 0)
            self.assertIn('This shared photo was captured without the photo itself', err.getvalue())
            self.assertTrue((staging / '.not-filed' / bundle.name / 'capture.json').is_file())
            inbox = archive / 'inbox'
            self.assertFalse(inbox.exists() and any(inbox.iterdir()), 'nothing may be filed')

    def test_a_root_named_path_finds_its_configured_folder_and_a_typed_path_still_wins(self) -> None:
        """PR #258 round 7 (section 13 gap): the alias resolution on its own,
        apart from any refusal's wording. `fha process photos/...` with the
        photo library outside the archive: found in that library. A path that
        exists as typed is always the one used."""
        import process
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            library = tmp / 'Family Photos'
            library.mkdir()
            (library / 'portrait.jpg').write_bytes(b'library copy')
            archive, config = self._archive(tmp, photos=library.as_posix())
            elsewhere = tmp / 'elsewhere'
            (elsewhere / 'photos').mkdir(parents=True)
            cwd = os.getcwd()
            try:
                os.chdir(tmp)  # neither the archive nor the library
                rc = process._standalone_main(['photos/portrait.jpg', '--dry-run', '--root', str(archive)])
                self.assertEqual(rc, EXIT_CLEAN)
                # As typed, it exists here: that is the file meant.
                os.chdir(elsewhere)
                (elsewhere / 'photos' / 'portrait.jpg').write_bytes(b'the one she typed')
                found, _ = process._resolve_input_file('photos/portrait.jpg', archive, fha_config=config)
                self.assertEqual(found, (elsewhere / 'photos' / 'portrait.jpg').resolve())
                (elsewhere / 'photos' / 'portrait.jpg').unlink()
                found, _ = process._resolve_input_file('photos/portrait.jpg', archive, fha_config=config)
                self.assertEqual(found, (library / 'portrait.jpg').resolve())
            finally:
                os.chdir(cwd)

    def test_an_older_add_ons_copy_of_a_shared_photos_page_never_reaches_the_archive(self) -> None:
        """PR #258 round 5 (audit M), round 6: when the older add-on's capture
        does hold a picture, the copy of the page is left out (it stays in the
        staging folder, parked, never deleted) and the notes say so. Round 6
        sweep: the address keeps only its path through /media/{id} - its query
        (destTreeId, hid) names her own tree - in the citation and the links."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            staging = tmp / 'staging'
            bundle = self._old_addon_bundle(staging, with_photo=True)
            archive, config = self._archive(tmp)
            res = capture.run_ingest(archive, config, staging_dir=str(staging))
            self.assertEqual(res.exit_code, EXIT_CLEAN)
            texts = {str(p.relative_to(archive)): p.read_text(encoding='utf-8', errors='replace')
                     for p in archive.rglob('*') if p.is_file()}
            self.assertTrue(texts)
            for name, text in texts.items():
                for member in ('cousin_member_one', 'cousin_member_two', 'Saved by', 'hartley_family_tree',
                               'destTreeId', '90000003', 'hid=h1'):
                    self.assertNotIn(member, text, name)
            stub = next(t for n, t in texts.items() if n.endswith('.notes.md'))
            self.assertIn("The copy of Ancestry's page was left out: it lists other members who saved this photo. "
                          "The photo's address is kept.", stub)
            meta = read_record(next((archive / 'inbox').glob('*.notes.md')))['meta']
            self.assertEqual([link['url'] for link in meta['external_links']], [PHOTO_VIEWER_URL])
            self.assertIn(PHOTO_VIEWER_URL, meta['citation'])
            # The copy is still in the staging folder, parked with the bundle.
            self.assertTrue((staging / '.ingested' / bundle.name / 'page-snapshot.html').is_file())

    def test_every_command_a_refusal_prints_works_as_typed_with_the_photo_library_elsewhere(self) -> None:
        """PR #258 round 6 (P2): her photo library is not inside the archive
        folder. The "another version" refusal's own command failed as typed
        ("file not found: photos/..."). Every root-named path a command takes
        - photos/..., inbox/... - now finds its configured folder. Followed to
        the end: the version is filed, the folder is deleted as told, a third
        version is offered the next free print letter, and a folder whose
        photo was already filed gets plain words instead of "file hints for
        missing assets"."""
        import process
        import shlex
        from unittest import mock
        photo_name = f'ancestry-{PHOTO_MEDIA_ID}-photo.jpg'
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            library = tmp / 'Family Photos'
            library.mkdir()
            archive, config = self._archive(tmp, photos=library.as_posix())
            sid = None

            def keywords(path):
                p = Path(path)
                return [f'SOURCE: {sid}'] if sid and p.parent == library and p.name == photo_name else []

            def process_folder(n: str, picture: bytes | None):
                staging = tmp / ('staging' + n)
                staging.mkdir()
                bundle = staging / (PHOTO_BUNDLE.name + n)
                shutil.copytree(PHOTO_BUNDLE, bundle)
                if picture is not None:
                    (bundle / photo_name).write_bytes(picture)
                capture.run_ingest(archive, config, staging_dir=str(staging))
                folder = next(p for p in (archive / 'inbox').iterdir() if p.is_dir() and p.name != 'processed')
                try:
                    return folder, process.process_bundle(archive, config, folder, source_date=None, dry_run=False)
                except process.ProcessError as e:
                    return folder, str(e)

            def larger(tag: bytes) -> bytes:
                return b'\xff\xd8\xff\xdb\x00\x04\x00\x00\xff\xda\x00\x02 ' + tag + b' \xff\xd9'

            def follow(message: str) -> int:
                cmd = re.search(r'`fha process ([^`]*--more[^`]*)`', message).group(1)
                cwd = os.getcwd()
                os.chdir(tmp)  # neither the archive nor the library: the path must resolve on its own
                try:
                    return process._standalone_main(shlex.split(cmd) + ['--root', str(archive)])
                finally:
                    os.chdir(cwd)

            with mock.patch.object(process, '_run_exiftool_embed_source', return_value=None), \
                    mock.patch.object(process, '_run_exiftool_read_keywords', side_effect=keywords), \
                    mock.patch.object(process, '_run_exiftool_remove_source', return_value=None):
                _, rc = process_folder('1', None)
                self.assertEqual(rc, EXIT_CLEAN)
                sid = read_record(next((archive / 'sources').rglob('*.md')))['meta']['id']
                folder, refusal = process_folder('2', larger(b'second version'))
                self.assertIn(f'front:b`, then delete the folder {_rel_posix(folder, archive)}', refusal)
                self.assertEqual(follow(refusal), EXIT_CLEAN, refusal)
                self.assertTrue((library / f'ancestry-{PHOTO_MEDIA_ID}-photo-b.jpg').is_file())
                # Not deleted yet: processing it again says why, plainly.
                try:
                    process.process_bundle(archive, config, folder, source_date=None, dry_run=False)
                    leftover = ''
                except process.ProcessError as e:
                    leftover = str(e)
                self.assertIn(f'the notes in {folder.name} name a file that is no longer in that folder: {photo_name}',
                              leftover)
                self.assertIn('this folder has nothing left to file: delete it', leftover)
                self.assertNotIn('file hints for missing assets', leftover)
                shutil.rmtree(folder)  # as told
                # A third version: the next free print letter, never b again.
                folder3, refusal3 = process_folder('3', larger(b'third version'))
                self.assertIn('front:c`', refusal3)
                self.assertEqual(follow(refusal3), EXIT_CLEAN, refusal3)
            self.assertEqual(sorted(p.name for p in library.iterdir()),
                             sorted([photo_name, f'ancestry-{PHOTO_MEDIA_ID}-photo-b.jpg',
                                     f'ancestry-{PHOTO_MEDIA_ID}-photo-c.jpg']))
            files = read_record(next((archive / 'sources').rglob('*.md')))['meta']['files']
            self.assertEqual(sorted(f.get('copy') or '' for f in files if f['role'] == 'front'), ['', 'b', 'c'])

    def test_a_captured_ancestry_record_cites_as_a_record_on_wikitree(self) -> None:
        """PR #258 review: `fha process` now keeps a capture's page address, and
        `fha wikitree` turned a RECORD's address into {{Ancestry Image}} with the
        record's number where the image's goes - a WikiTree reader following the
        citation reached the wrong page. The 1880 census sample, filed: its
        reference carries {{Ancestry Record|db|record}}."""
        import process
        import wikitree
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            staging = tmp / 'staging'
            staging.mkdir()
            shutil.copytree(EXAMPLE_BUNDLE, staging / EXAMPLE_BUNDLE.name)
            archive = tmp / 'archive'
            archive.mkdir()
            (archive / 'fha.yaml').write_text(
                'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8')
            config = load_fha_yaml(archive, strict=True)
            capture.run_ingest(archive, config, staging_dir=str(staging))
            bundle_dir = next(p for p in (archive / 'inbox').iterdir() if p.is_dir())
            with mock.patch.object(process, '_run_exiftool_embed_source', return_value=None), \
                    mock.patch.object(process, '_run_exiftool_read_keywords', return_value=[]), \
                    mock.patch.object(process, '_run_exiftool_remove_source', return_value=None):
                self.assertEqual(process.process_bundle(
                    archive, config, bundle_dir, source_date=None, dry_run=False), EXIT_CLEAN)
            md = next((archive / 'sources').rglob('*.md'))
            ref = wikitree._source_reference(archive, {'path': str(md.relative_to(archive)), 'title': 't', 'id': 'S-x'})
            self.assertIn('{{Ancestry Record|6742|12345}}', ref)
            self.assertNotIn('Ancestry Image', ref)


# ── Drift-guard extraction (emission sites, not whole files) ──────────────────
#
# The producibility guard below must assert keys against the exact code that
# EMITS them, so these regexes carve the emission sites out of the JS first.
# Each extraction is asserted non-empty by its test: a refactor that moves an
# emission site fails loudly ("update the extraction") instead of silently
# turning the guard vacuous.

# A property key inside an extracted object-literal body: `file:` / `'file':` /
# `"file":`. Only ever run on a small extracted block, never a whole file.
_JS_OBJECT_KEY_RE = re.compile(r"(?:'([A-Za-z_]\w*)'|\"([A-Za-z_]\w*)\"|\b([A-Za-z_]\w*))\s*:")

# panel.js: the capture fields literal (`const fields = {...};`), then the
# asset-entry object inside its `assets: assets.map((a) => ({...}))` - the one
# place the panel shapes the schema-2 asset entries handed to build().
_PANEL_FIELDS_RE = re.compile(r'const\s+fields\s*=\s*\{(.*?)\};', re.S)
_PANEL_ASSETS_MAP_RE = re.compile(
    r'assets\s*:\s*assets\.map\(\s*\(\s*a\s*\)\s*=>\s*\(\{(.*?)\}\)', re.S)

# capture-json*.js build(): the whole function up to its `return out;`, the
# `const out = {...}` seed, the `out.<key> = ...` field assignments, and the
# asset-entry construction (`const entry = {...}` seed + `entry.<key> = ...`
# guards up to `return entry`).
_LIB_BUILD_RE = re.compile(r'function\s+build\s*\(.*?return\s+out\s*;', re.S)
_LIB_OUT_SEED_RE = re.compile(r'const\s+out\s*=\s*\{(.*?)\}', re.S)
_LIB_OUT_ASSIGN_RE = re.compile(r"out\.([A-Za-z_]\w*)\s*=|out\[\s*['\"]([A-Za-z_]\w*)['\"]\s*\]\s*=")
_LIB_ENTRY_RE = re.compile(r'const\s+entry\s*=\s*\{(.*?)\}(.*?)return\s+entry', re.S)
_LIB_ENTRY_ASSIGN_RE = re.compile(
    r"entry\.([A-Za-z_]\w*)\s*=|entry\[\s*['\"]([A-Za-z_]\w*)['\"]\s*\]\s*=")


def _object_literal_keys(block: str) -> set[str]:
    """Property-key literals in an extracted JS object body (bare or quoted)."""
    return {next(g for g in m.groups() if g) for m in _JS_OBJECT_KEY_RE.finditer(block)}


def _assign_keys(matches: list[tuple[str, str]]) -> set[str]:
    """Keys from `x.key = ...` / `x['key'] = ...` findall tuples."""
    return {a or b for a, b in matches}


class CaptureJsonProducibilityTestCase(unittest.TestCase):
    """Drift guard: every key the committed sample uses stays panel-producible.

    The test-bundle capture.json is the worked example of the companion's output
    contract (README, TOOLING_INGESTION section 3). The round-trip tests above
    prove the backend still READS it, but nothing proved the shipping extension
    can still WRITE it - which is how the provisional-screenshot feature was
    silently dropped from the panel while the sample, the docs, and the
    capture-json pass-through all kept carrying the field.

    An earlier version of this guard asserted plain substring containment over
    the whole panel.js / capture-json*.js text. That was tautological for the
    generic keys: `file`, `mode`, `role`, and `url` all occur in those files as
    incidental identifiers (droppedAsset.filename, evidenceMode(), fetchAsset
    urls, ...), so the guard kept passing with the actual emission line deleted.
    This version extracts the emission sites first and asserts each sample key
    appears as a real property-key literal INSIDE the extracted block only:

      - panel.js: the asset-entry literal in `assets: assets.map(...)` within
        `const fields = {...}` - the only assembler of the schema-2 asset list;
      - each capture-json*.js (browser build + kept-in-sync pure twin):
        build()'s top-level output fields (`const out = {...}` seed plus the
        `out.<key> = ...` assignments) and its asset-entry construction
        (`const entry = {...}` seed plus the `entry.<key> = ...` guards).

    How the non-tautology was validated: each test asserts its extraction
    matched and produced a non-empty key set, and keys are taken ONLY from the
    extracted block; the rewrite was mutation-checked by deleting `mode: a.mode`
    from a scratch copy of panel.js's map literal (and the `entry.mode` guard
    from a scratch capture-json.js), which makes the matching assertion here
    fail while the old whole-file substring check kept passing.
    """

    def _samples(self) -> list[tuple[str, dict]]:
        samples = sorted((COMPANION / 'test-bundle').glob('*/capture.json'))
        self.assertTrue(samples, 'no committed test-bundle capture.json found')
        return [(p.parent.name, json.loads(p.read_text(encoding='utf-8')))
                for p in samples]

    def _lib_sources(self) -> dict[str, str]:
        lib_files = sorted((COMPANION / 'src' / 'lib').glob('capture-json*.js'))
        self.assertTrue(lib_files, 'no capture-json*.js found under src/lib')
        return {p.name: p.read_text(encoding='utf-8') for p in lib_files}

    def test_assets_entry_keys_are_emitted_by_the_panel(self) -> None:
        panel_src = (COMPANION / 'src' / 'panel.js').read_text(encoding='utf-8')
        fields_ms = list(_PANEL_FIELDS_RE.finditer(panel_src))
        self.assertTrue(
            fields_ms,
            'panel.js: no `const fields = {...};` literal found - the capture '
            'fields assembly moved; point _PANEL_FIELDS_RE at the new shape')
        # Every capture path assembles its own fields literal (the generic
        # page, each site's record, a shared photo); a sample is producible
        # when some path emits each of its keys.
        maps = [m for f in fields_ms for m in [_PANEL_ASSETS_MAP_RE.search(f.group(1))] if m]
        self.assertTrue(
            maps,
            'panel.js: no `assets: assets.map((a) => ({...}))` inside a '
            'fields literal - the asset-entry emission moved; point '
            '_PANEL_ASSETS_MAP_RE at the new shape')
        keys = set().union(*(_object_literal_keys(m.group(1)) for m in maps))
        # The panel says `primary`; build() writes it as `is_primary`.
        if 'primary' in keys:
            keys.add('is_primary')
        self.assertTrue(keys, 'panel.js: extracted asset-entry literal has no keys')
        for bundle, cap in self._samples():
            for entry in cap.get('assets', []):
                for key in entry:
                    self.assertIn(
                        key, keys,
                        f'{bundle}: assets[] key {key!r} is not a property key '
                        "of panel.js's asset-entry literal (the assets.map "
                        'inside const fields) - the panel no longer emits it, '
                        'so the committed sample is not panel-producible')

    def test_assets_entry_keys_survive_the_build_passthrough(self) -> None:
        for name, src in self._lib_sources().items():
            entry_m = _LIB_ENTRY_RE.search(src)
            self.assertIsNotNone(
                entry_m,
                f'{name}: no `const entry = {{...}} ... return entry` block '
                'found - the asset-entry construction moved; point '
                '_LIB_ENTRY_RE at the new shape')
            keys = _object_literal_keys(entry_m.group(1))
            keys |= _assign_keys(_LIB_ENTRY_ASSIGN_RE.findall(entry_m.group(2)))
            self.assertTrue(keys, f'{name}: extracted entry construction has no keys')
            for bundle, cap in self._samples():
                for entry in cap.get('assets', []):
                    for key in entry:
                        self.assertIn(
                            key, keys,
                            f'{bundle}: assets[] key {key!r} is not built onto '
                            f"`entry` in {name}'s build() - it would be dropped "
                            'from the emitted capture.json')

    def test_top_level_keys_are_build_output_fields(self) -> None:
        for name, src in self._lib_sources().items():
            build_m = _LIB_BUILD_RE.search(src)
            self.assertIsNotNone(
                build_m,
                f'{name}: no `function build(...) ... return out;` found - '
                'the builder moved; point _LIB_BUILD_RE at the new shape')
            body = build_m.group(0)
            keys = _assign_keys(_LIB_OUT_ASSIGN_RE.findall(body))
            seed_m = _LIB_OUT_SEED_RE.search(body)
            if seed_m is not None:
                keys |= _object_literal_keys(seed_m.group(1))
            self.assertTrue(keys, f'{name}: build() assigns no fields onto `out`?')
            for bundle, cap in self._samples():
                for key in cap:
                    self.assertIn(
                        key, keys,
                        f'{bundle}: top-level key {key!r} is never assigned '
                        f"onto `out` in {name}'s build() - the extension can "
                        'no longer produce the sample it claims to mirror')


class PointerOnlyCaptureTestCase(unittest.TestCase):
    """The panel's pointer-only capture files as a citation/link pointer stub.

    §5.3's "none" (TOOLING §13b case (c)): page-copy off + "No, the page copy
    is the record" stages page.html plus a capture.json with an EMPTY assets
    list - exactly what capture-json.js build() emits for that state. Ingest
    must file it as a lone sidecar stub flagged `asset_elsewhere: true` (the
    deliberate-no-companion marker `fha process` requires before minting a
    no-asset source), carrying the citation and link.
    """

    def test_empty_assets_bundle_files_as_pointer_stub(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            staging = tmp / 'staging'
            bundle = staging / 'deed-index-1854-20260727-120000-000'
            bundle.mkdir(parents=True)
            (bundle / 'page.html').write_text(
                '<!DOCTYPE html>\n<html><head><title>Deed index 1854</title>'
                '</head><body><h1>Deed index 1854</h1></body></html>\n',
                encoding='utf-8')
            (bundle / 'capture.json').write_text(json.dumps({
                'schema': capture._CAPTURE_JSON_SCHEMA,
                'url': 'https://county.example.gov/deeds/1854',
                'accessed': '2026-07-27',
                'assets': [],
            }, indent=2), encoding='utf-8')

            archive = tmp / 'archive'
            archive.mkdir()
            (archive / 'fha.yaml').write_text(
                'roots:\n  photos: photos\n  documents: documents\n',
                encoding='utf-8')
            config = load_fha_yaml(archive, strict=True)

            res = capture.run_ingest(archive, config, staging_dir=str(staging))
            self.assertEqual(res.exit_code, EXIT_CLEAN)
            self.assertEqual(res.data['ingested'], 1)

            stubs = list((archive / 'inbox').glob('*.notes.md'))
            self.assertEqual(len(stubs), 1, stubs)
            rec = read_record(stubs[0])
            self.assertEqual(rec['parse_errors'], [])
            self.assertTrue(rec['meta'].get('asset_elsewhere'),
                            'pointer stub must flag asset_elsewhere: true')
            self.assertEqual(rec['meta']['external_links'][0]['url'],
                             'https://county.example.gov/deeds/1854')
            self.assertEqual(rec['meta']['external_links'][0]['accessed'],
                             '2026-07-27')
            # No asset files were filed alongside (pointer-only by contract).
            self.assertEqual(
                [p.name for p in (archive / 'inbox').iterdir()],
                [stubs[0].name])


class SnapshotUrlRewriteTestCase(unittest.TestCase):
    """The saved snapshot must anchor every URL it keeps.

    The JS-level behaviour is tested by the node suite
    (`npm --prefix browser-companion test`, tests/test-srcset.js and
    tests/test-snapshot-urls.js). These are the structural guards that also fire
    for anyone running only the Python tests: the two shapes that made the
    preserved copy quietly useless, pinned so they cannot come back.
    """

    def setUp(self) -> None:
        self.content = (COMPANION / 'src' / 'content.js').read_text(encoding='utf-8')

    def test_a_page_that_declares_a_base_is_still_rewritten(self) -> None:
        # The snapshot used to skip ALL URL rewriting whenever the page carried a
        # <base>. A relative one (`<base href="/records/">`) resolves against the
        # live site in the browser and against the local filesystem once the copy
        # is opened from file://, so every non-inlined link and image in the
        # saved page broke. Now the base itself is absolutized and the rewrite
        # runs regardless.
        self.assertNotIn("if (!clone.querySelector('head base'))", self.content)
        self.assertIn("clone.querySelector('base[href]')", self.content)

    def test_srcset_is_parsed_never_split_on_commas(self) -> None:
        # A comma is legal inside a candidate URL (`data:` URLs always carry one),
        # so splitting on commas cut one URL into fragments and rewrote each as a
        # path of its own.
        self.assertNotIn(".split(',')", self.content)
        self.assertIn('function parseSrcset(', self.content)
        self.assertIn('rewriteSrcset(', self.content)

    def test_the_url_sweep_covers_the_easily_missed_attributes(self) -> None:
        # Attributes a snapshot forgets one at a time: each one left relative is
        # a dead reference in the preserved page.
        for attr in ("['form', 'action']", "['button', 'formaction']",
                     "['object', 'data']", "['video', 'poster']"):
            self.assertIn(attr, self.content, attr)
        for marker in ('imagesrcset', "'xlink:href'", 'http-equiv', 'absolutizeCss'):
            self.assertIn(marker, self.content, marker)

    def test_markup_carried_inside_an_attribute_is_disarmed_too(self) -> None:
        # An <iframe srcdoc="..."> carries a whole second document as an
        # ATTRIBUTE STRING, so every DOM pass in the snapshot builder walks
        # straight past it - the script removal, the handler disarm, the URL
        # sweep, all of it. The frame sandbox then granted allow-scripts, which
        # made that untouched markup live again in the saved file.
        #
        # This guard lives in the Python suite on purpose: CI installs no node
        # (tests.yml says so outright), so the 100+ browser-companion node tests
        # never run there. A structural pin here is the only thing standing
        # between this gap and a silent return.
        self.assertIn('disarmSrcdocMarkup(', self.content)
        self.assertIn('iframe[srcdoc]', self.content)
        # A frame whose markup we carry and have already disarmed gains nothing
        # legitimate from script; a `src` frame often IS the record viewer and
        # keeps it.
        self.assertIn('frameSandboxValue(', self.content)

    def test_no_local_disk_path_can_enter_a_snapshot(self) -> None:
        # AGENTS.md privacy rule: a local absolute path must never reach a file
        # that lands in the archive. Capturing a page opened from disk would
        # otherwise write this machine's folder names into the saved copy.
        self.assertIn("resolved.protocol === 'file:'", self.content)

    def test_the_srcset_parser_has_a_canonical_home_and_a_sync_guard(self) -> None:
        # content.js is injected and cannot import, so it carries a copy. The
        # copy sits between sync markers the node guard extracts and re-runs.
        self.assertTrue((COMPANION / 'src' / 'lib' / 'srcset.js').is_file())
        self.assertIn('// FHA-SYNC-BEGIN srcset', self.content)
        self.assertIn('// FHA-SYNC-END srcset', self.content)
        sync = (COMPANION / 'tests' / 'test-sync.js').read_text(encoding='utf-8')
        self.assertIn('FHA-SYNC-BEGIN srcset', sync)


class HandoffLocationTestCase(unittest.TestCase):
    """The handoff command names where the bundle IS, never where it might be.

    Chrome's download directory is a browser setting: moved to OneDrive, to a
    second volume, or carrying a localized folder name on plenty of machines.
    The panel used to synthesize `~/Downloads/<folder>` into the copyable
    command, so a capture that succeeded advertised a sweep of a folder it had
    never been written to - `fha capture --ingest` then found nothing, with the
    bundle sitting safely somewhere else and nothing on screen to say where.
    The browser answers the question itself (`chrome.downloads.search` returns
    the completed file's absolute path), so the command is built from that, and
    from nothing when it is absent.

    These are structural pins, kept alongside the node tests
    (`npm --prefix browser-companion test`, tests/test-capture-json.js) because
    CI installs no node: the behavioural coverage there never runs in CI, and
    this is what stands between the guess coming back and nobody noticing.
    """

    def setUp(self) -> None:
        self.pure = (COMPANION / 'src' / 'lib' / 'capture-json-pure.js').read_text(
            encoding='utf-8')
        self.browser = (COMPANION / 'src' / 'lib' / 'capture-json.js').read_text(
            encoding='utf-8')
        self.bundle = (COMPANION / 'src' / 'lib' / 'bundle.js').read_text(encoding='utf-8')
        self.panel = (COMPANION / 'src' / 'panel.js').read_text(encoding='utf-8')

    def test_no_home_directory_path_is_synthesized_into_a_command(self) -> None:
        for name, text in (('capture-json-pure.js', self.pure),
                           ('capture-json.js', self.browser),
                           ('panel.js', self.panel),
                           ('bundle.js', self.bundle)):
            self.assertNotIn('~/Downloads', text, name)

    def test_the_real_download_path_is_read_from_the_browser(self) -> None:
        # bundle.js asks the downloads API where the file actually went and
        # hands that up; the panel turns it into the staging folder.
        self.assertIn('chrome.downloads.search', self.bundle)
        self.assertIn('filePath', self.bundle)
        self.assertIn('stagedPaths(result.filePath)', self.panel)

    def test_the_command_is_built_from_the_reported_path_not_the_setting(self) -> None:
        # The folder setting cannot say where Downloads is, so it is not what
        # the command is built from.
        self.assertIn('ingestCommand(state.stagedDir)', self.panel)
        self.assertNotIn('ingestCommand(state.folder)', self.panel)
        # Both the pure twin and the browser file take the reported directory.
        for name, text in (('capture-json-pure.js', self.pure),
                           ('capture-json.js', self.browser)):
            self.assertIn('function ingestCommand(stagingDir, nav)', text, name)
            self.assertIn('function stagedPaths(filePath)', text, name)

    def test_an_unknown_location_gets_a_hint_naming_the_places_to_look(self) -> None:
        # With no reported path the command asserts nothing, so the human is
        # pointed at his browser's own download setting and at the archive's
        # `capture_staging:` key - the two places that can actually answer.
        self.assertIn('cmd-hint', (COMPANION / 'src' / 'panel.html').read_text(
            encoding='utf-8'))
        self.assertIn('ingestHint(state.folder, state.stagedDir)', self.panel)
        for name, text in (('capture-json-pure.js', self.pure),
                           ('capture-json.js', self.browser)):
            self.assertIn('capture_staging', text, name)


def _strip_js_comments(text: str) -> str:
    """Drop `//` and `/* */` comments so a sweep reads only what SHIPS.

    The companion's source is heavily commented, and nearly every comment
    discusses `fha capture --ingest` by name. Those are prose about the command
    and are correct as they stand (the repo writes commands bare in prose); what
    must carry a launcher prefix is the text the panel puts on screen. Stripping
    comments is what separates the two.

    Deliberately naive - no string-literal awareness - which is safe in one
    direction only: a `//` inside a string (a URL) truncates that line, so the
    sweep can lose a line but never invent a violation. The one such case here
    is disarmed by the caller's own regex, which needs `fha ` + a verb.
    """
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    return '\n'.join(re.sub(r'//.*$', '', line) for line in text.splitlines())


# An `fha` invocation offered to a human: the bare name, followed by a verb,
# with no `./` or `.\` in front of it. The lookbehind is what lets `fhaCmd(`,
# `fha.yaml`, `window.FHA` and an already-prefixed `./fha` all pass.
_BARE_FHA = re.compile(
    r'(?<![\w./\\])fha\s+(?:capture|lint|index|process|doctor|report|find|views)\b')


class LauncherPrefixTestCase(unittest.TestCase):
    """A command the panel offers must be one the human's shell can run.

    `fha` is a launcher FILE at the archive root - `fha` (sh) and `fha.cmd`
    (Windows), both shipped by `tools/scaffold.py` - and it is never put on
    PATH. AGENTS.md's execution rules spell the consequence out: `./fha` on
    macOS/Linux, `.\\fha` in Windows PowerShell, a bare `fha` only in the
    Windows Command Prompt, which is the one shell that searches the current
    directory. The handoff card used to offer the bare form to everyone, so a
    capture that had just succeeded handed most users a command-not-found.

    These are structural pins for the same reason the sibling
    HandoffLocationTestCase is: CI installs no node, so
    `browser-companion/tests/test-capture-json.js` - which owns the behavioural
    coverage - never runs here. This class is what actually guards the fix.
    """

    def setUp(self) -> None:
        self.pure = (COMPANION / 'src' / 'lib' / 'capture-json-pure.js').read_text(
            encoding='utf-8')
        self.browser = (COMPANION / 'src' / 'lib' / 'capture-json.js').read_text(
            encoding='utf-8')
        self.panel_js = (COMPANION / 'src' / 'panel.js').read_text(encoding='utf-8')
        self.panel_html = (COMPANION / 'src' / 'panel.html').read_text(encoding='utf-8')

    def test_the_copied_command_is_built_from_a_launcher_not_a_bare_name(self) -> None:
        # The literal the card used to emit. Its absence is the fix.
        for name, text in (('capture-json-pure.js', self.pure),
                           ('capture-json.js', self.browser)):
            self.assertNotIn("'fha capture --ingest'", text, name)
            self.assertIn('launcher(nav)', text, name)
            # Both project spellings, and only those two - not a third.
            self.assertIn("'./fha'", text, name)
            self.assertIn("'.\\\\fha'", text, name)

    def test_the_two_spellings_are_the_ones_the_owner_docs_teach(self) -> None:
        # A third spelling would be worse than the bug, so the forms are pinned
        # against the docs an archive owner actually reads.
        cheatsheet = (ROOT / 'CHEATSHEET.md').read_text(encoding='utf-8')
        self.assertIn('`./fha <command>`', cheatsheet)
        self.assertIn('`.\\fha <command>`', cheatsheet)

    def test_windows_gets_the_form_that_runs_in_both_windows_shells(self) -> None:
        # PowerShell refuses a bare current-directory command; cmd.exe resolves
        # a path-qualified `.\fha` through PATHEXT to fha.cmd just as it
        # resolves the bare name. So `.\fha` strands nobody on Windows and the
        # bare form strands every PowerShell user - hence which one is emitted.
        for name, text in (('capture-json-pure.js', self.pure),
                           ('capture-json.js', self.browser)):
            self.assertIn("LAUNCHER_WINDOWS = '.\\\\fha'", text, name)
            self.assertIn("LAUNCHER_POSIX = './fha'", text, name)
            # Unknown platform falls to the POSIX form, which PowerShell also
            # accepts - the failure mode that leaves fewest people stranded.
            self.assertIn('if (!n) return LAUNCHER_POSIX;', text, name)

    def test_the_platform_is_read_never_guessed(self) -> None:
        # The extension cannot know the shell and does not pretend to; it reads
        # the OS, which is what decides the separator.
        for name, text in (('capture-json-pure.js', self.pure),
                           ('capture-json.js', self.browser)):
            self.assertIn('userAgentData', text, name)
            self.assertIn('n.platform', text, name)
            self.assertIn('n.userAgent', text, name)

    def test_no_shipped_string_hands_the_human_a_bare_fha_command(self) -> None:
        """The sweep: every command the panel RENDERS carries its prefix."""
        offenders = []
        for name, text in (('panel.js', _strip_js_comments(self.panel_js)),
                           ('capture-json.js', _strip_js_comments(self.browser)),
                           ('capture-json-pure.js', _strip_js_comments(self.pure))):
            for hit in _BARE_FHA.finditer(text):
                offenders.append(f'{name}: {hit.group(0)!r}')
        # panel.html, minus its HTML comments.
        html = re.sub(r'<!--.*?-->', '', self.panel_html, flags=re.S)
        for hit in _BARE_FHA.finditer(html):
            offenders.append(f'panel.html: {hit.group(0)!r}')
        self.assertEqual(
            [], offenders,
            'these render a bare `fha …` to the human, which only a Windows '
            'Command Prompt can run: ' + '; '.join(offenders))

    def test_the_panel_builds_every_command_through_one_helper(self) -> None:
        # One place to be right, so the next command added inherits the prefix.
        self.assertIn('function fhaCmd(rest)', self.panel_js)
        self.assertIn("captureJson.launcher() + ' ' + rest", self.panel_js)
        # The settings drawer's two static samples are filled at init.
        self.assertIn('cmd-install-host', self.panel_html)
        self.assertIn('cmd-ingest-plain', self.panel_html)
        self.assertIn('fillStaticCmds()', self.panel_js)

    def test_the_installed_readme_shows_every_shell_not_just_one(self) -> None:
        """README-ARCHIVE.md ships into archives, so it must not teach the bug.

        The doc used to open its ingest step with a fenced `fha capture
        --ingest` - the bare form - and correct itself in prose underneath. The
        block is what gets copied, so the block carries all three forms, the way
        GETTING_STARTED.md and docs/SETUP_FROM_ZIP.md write theirs.
        """
        text = (COMPANION / 'README-ARCHIVE.md').read_text(encoding='utf-8')
        blocks = [b for b in re.findall(r'```[a-z]*\n(.*?)```', text, flags=re.S)
                  if 'capture --ingest' in b]
        self.assertTrue(blocks, 'no fenced ingest command block in README-ARCHIVE.md')
        for block in blocks:
            self.assertIn('./fha capture --ingest', block)
            self.assertIn('.\\fha capture --ingest', block)


class BundleIdentityTestCase(unittest.TestCase):
    """A staged bundle folder is unique per CAPTURE, not per (title, millisecond).

    `chrome.downloads.download` is the extension's only way to write a file, and
    it takes a Downloads-relative path with `conflictAction: 'uniquify'`. That
    setting renames each FILE on its own, never the folder - so when two
    captures compute the same `<slug>-<timestamp>` folder (two side panels in
    two browser windows, or a clock adjustment handing the same millisecond out
    twice), Chrome merges them into ONE directory and uniquifies the members:

        1880-census-thomas-20260727-101500-007/
            page.html          record.jpg          capture.json
            page (1).html      record (1).jpg      capture (1).json

    Nothing about that folder looks broken. `fha capture --ingest` reads the one
    file literally named `capture.json`, resolves `assets[].file` to the one
    literally named `record.jpg` (`capture._resolve_bundle_assets`), files a
    complete-looking source, and parks the whole folder in `.ingested/`. Two
    things go wrong silently: the second capture is never read at all, and -
    because the assets are written in their own order, interleaved - the
    surviving `record.jpg` can be the OTHER capture's evidence sitting under
    this capture's citation. Neither leaves a trace to notice later.

    The fix has to make the folder itself unique, so a per-capture random token
    joins the slug and the timestamp. These are the structural pins; the
    behaviour is covered by the node suite (`npm --prefix browser-companion
    test`, tests/test-capture-json.js), which CI never runs because it installs
    no node.
    """

    def setUp(self) -> None:
        self.libs = {
            p.name: p.read_text(encoding='utf-8')
            for p in sorted((COMPANION / 'src' / 'lib').glob('capture-json*.js'))
        }
        self.assertTrue(self.libs, 'no capture-json*.js found under src/lib')
        self.panel = (COMPANION / 'src' / 'panel.js').read_text(encoding='utf-8')
        self.bundle = (COMPANION / 'src' / 'lib' / 'bundle.js').read_text(encoding='utf-8')

    def test_the_bundle_name_is_not_title_and_clock_alone(self) -> None:
        # Both the browser file and its kept-in-sync pure twin.
        for name, src in self.libs.items():
            body = re.search(r'function\s+bundleName\s*\([^)]*\)\s*\{(.*?)\n\s*\}', src, re.S)
            self.assertIsNotNone(
                body, f'{name}: no `function bundleName(...) {{ ... }}` found - it '
                      'moved; point this pin at the new shape')
            self.assertIn(
                'randomToken', body.group(1),
                f'{name}: bundleName is built from the title and the clock only. Two '
                'captures made in the same millisecond would share a folder, and '
                "Chrome's uniquify would then rename the FILES inside it - leaving a "
                'bundle whose capture.json points at asset names that no longer exist.')

    def test_the_token_is_drawn_from_a_real_random_source(self) -> None:
        for name, src in self.libs.items():
            self.assertTrue('function randomToken(' in src,
                            f'{name}: no randomToken() to break a folder collision')
            self.assertTrue(
                'getRandomValues' in src,
                f'{name}: the collision-breaking token needs a real random source')

    def test_nothing_inside_a_bundle_is_named_from_the_page_or_the_clock(self) -> None:
        """The folder is the only content-derived name, so no sibling can be renamed.

        `capture.json` names its assets by `assets[].file`. If those filenames
        were themselves derived from the title or the clock, uniquify could
        rename one and leave the JSON pointing at a name that is not there. They
        are fixed literals instead - `page-snapshot.html` and `record.<ext>` -
        so a unique folder is sufficient to make the whole bundle safe.
        """
        self.assertIn("filename: 'page-snapshot.html'", self.panel)
        self.assertRegex(self.panel, r"filename:\s*'record\.'\s*\+")
        # bundle.js uses the bundle name in exactly ONE place - building the
        # directory - and writes every member under it by the member's own name.
        self.assertIn("'/' + spec.bundleName", self.bundle)
        self.assertIn("dir + '/' + asset.filename", self.bundle)
        self.assertEqual(
            self.bundle.count('spec.bundleName'), 1,
            'bundle.js uses the bundle name somewhere other than the directory it '
            'builds; a member named after the bundle would be content-derived too')

    def test_every_sample_asset_name_is_one_the_panel_writes_literally(self) -> None:
        """The committed sample cannot reference a name uniquify could have moved.

        A shared photo's picture is the one exception, and not a name from the
        page's words or the clock: it is named for the photo's media id (a
        GUID, checked by lib/ancestry-record.js ancPhotoFileStem), because it
        keeps that name for good in the photo library, where `record.jpg`
        would collide with the next shared photo.
        """
        allowed = re.compile(
            r'^(page-snapshot\.html|record\.[A-Za-z0-9]+|photo-data\.json|'
            r'ancestry-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-photo\.[a-z0-9]+)$')
        samples = sorted((COMPANION / 'test-bundle').glob('*/capture.json'))
        self.assertTrue(samples, 'no committed test-bundle capture.json found')
        for path in samples:
            cap = json.loads(path.read_text(encoding='utf-8'))
            for entry in cap.get('assets', []):
                self.assertRegex(
                    entry['file'], allowed,
                    f"{path.parent.name}: assets[].file {entry['file']!r} is not one of "
                    'the fixed names the panel writes, so it may be a name derived '
                    'from page content - which uniquify can rename out from under the '
                    'capture.json that points at it')


class ArchiveReadmeTestCase(unittest.TestCase):
    """The README an archive receives is the owner's, not the project's."""

    def test_the_owner_readme_exists_and_avoids_workshop_references(self) -> None:
        owner = COMPANION / 'README-ARCHIVE.md'
        self.assertTrue(owner.is_file(),
                        'the installed capture README must exist')
        text = owner.read_text(encoding='utf-8')
        for absent in ('TOOLING_INGESTION', 'ANCESTRY-AUTOFETCH-TEST',
                       'test-bundle', 'test_browser_companion'):
            self.assertNotIn(absent, text, absent)
        self.assertIn('Load unpacked', text)
        self.assertIn('fha capture --ingest', text)


class SourceTypeVocabularyTestCase(unittest.TestCase):
    """The panel's own source_type picker must not lag `_lib.SOURCE_TYPES`.

    #114 follow-up (Codex review, PR #178): `ephemera` joined the controlled
    vocabulary but panel.js's own friendly-label list - the capture panel's
    filing surface, independent of the workbench modal - kept teaching the
    old, shorter set. Nothing caught the drift because the two were never
    compared; this pins them together so a future SOURCE_TYPES addition
    fails here instead of silently going unpickable from the panel.
    """

    def test_panel_source_types_cover_the_controlled_vocabulary(self) -> None:
        import _lib
        panel_src = (COMPANION / 'src' / 'panel.js').read_text(encoding='utf-8')
        m = re.search(r'const SOURCE_TYPES = \[(.*?)\];', panel_src, re.S)
        self.assertIsNotNone(m, 'panel.js: no `const SOURCE_TYPES = [...]` literal found')
        values = set(re.findall(r"\['([\w-]+)',", m.group(1)))
        missing = _lib.SOURCE_TYPES - values
        self.assertEqual(missing, set(),
                          f'panel.js SOURCE_TYPES missing: {missing}')


if __name__ == '__main__':
    unittest.main()
