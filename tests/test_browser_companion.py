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
     path the test-bundle capture.json uses must still appear in the panel /
     capture-json source (the drift guard that pins features like the
     provisional-screenshot flag to the code that must emit them).

Run: python -m unittest tests.test_browser_companion -v   (from the repo root)
"""

import json
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


class CaptureJsonProducibilityTestCase(unittest.TestCase):
    """Drift guard: every key the committed sample uses stays panel-producible.

    The test-bundle capture.json is the worked example of the companion's output
    contract (README, TOOLING_INGESTION section 3). The round-trip tests above
    prove the backend still READS it, but nothing proved the shipping extension
    can still WRITE it - which is how the provisional-screenshot feature was
    silently dropped from the panel while the sample, the docs, and the
    capture-json pass-through all kept carrying the field. Pin each key literal
    to the source that must produce it (plain string containment; no JS runtime
    needed, and a vanished literal fails loudly):

      - every top-level key must appear in panel.js or src/lib/capture-json*.js,
        the code that gathers the fields and builds the file;
      - every assets[] entry key must appear in panel.js ITSELF, because the
        panel is the only assembler of the asset list and capture-json's build()
        passes those keys through under the same names - a key present only in
        the build() pass-through has no caller feeding it (exactly the
        provisional regression);
      - every assets[] entry key must also appear in each capture-json*.js
        (the browser build and its kept-in-sync pure twin), or build() would
        drop it on the way out.
    """

    def test_sample_capture_json_keys_appear_in_the_producing_source(self) -> None:
        panel_src = (COMPANION / 'src' / 'panel.js').read_text(encoding='utf-8')
        lib_files = sorted((COMPANION / 'src' / 'lib').glob('capture-json*.js'))
        self.assertTrue(lib_files, 'no capture-json*.js found under src/lib')
        lib_srcs = {p.name: p.read_text(encoding='utf-8') for p in lib_files}
        producer_src = panel_src + ''.join(lib_srcs.values())

        samples = sorted((COMPANION / 'test-bundle').glob('*/capture.json'))
        self.assertTrue(samples, 'no committed test-bundle capture.json found')
        for sample in samples:
            cap = json.loads(sample.read_text(encoding='utf-8'))
            for key in cap:
                self.assertIn(
                    key, producer_src,
                    f'{sample.parent.name}: top-level key {key!r} appears in the '
                    'committed sample but nowhere in panel.js or capture-json*.js '
                    '- the extension can no longer produce the sample it claims '
                    'to mirror')
            for entry in cap.get('assets', []):
                for key in entry:
                    self.assertIn(
                        key, panel_src,
                        f'{sample.parent.name}: assets[] key {key!r} never '
                        'appears in panel.js - the panel no longer feeds it, so '
                        "capture-json's pass-through is dead code and the "
                        'committed sample is not panel-producible')
                    for name, src in lib_srcs.items():
                        self.assertIn(
                            key, src,
                            f'{sample.parent.name}: assets[] key {key!r} is '
                            f'missing from {name} - build() would drop it from '
                            'the emitted capture.json')


if __name__ == '__main__':
    unittest.main()
