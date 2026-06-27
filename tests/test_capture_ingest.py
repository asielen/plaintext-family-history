"""Tests for `fha capture --ingest` (BUILD.md M7.9, TOOLING_INGESTION §6).

The sweep reads staged bundles (`page.html` + optional `asset.*` + `capture.json`)
and feeds each through `run_capture` wholesale, then parks the bundle in
`.ingested/`. No network, no browser: bundles are built in a temp staging dir
here, reusing the committed capture-samples HTML as the raw `page.html`.

Run: python -m unittest tests.test_capture_ingest -v   (from the repo root)
"""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import capture
from _lib import EXIT_CLEAN, EXIT_ERRORS, load_fha_yaml, read_record

SAMPLES = ROOT / 'tests' / 'fixtures' / 'capture-samples'


def _sample(name: str) -> str:
    return (SAMPLES / f'{name}.html').read_text(encoding='utf-8')


def _make_archive(tmp: Path) -> tuple[Path, dict]:
    archive = tmp / 'archive'
    archive.mkdir()
    (archive / 'fha.yaml').write_text(
        'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8')
    return archive, load_fha_yaml(archive, strict=True)


def _make_bundle(staging: Path, name: str, *, page_html: str, capture_json: dict,
                 asset: tuple[str, bytes] | None = None) -> Path:
    bundle = staging / name
    bundle.mkdir(parents=True)
    (bundle / 'page.html').write_text(page_html, encoding='utf-8')
    (bundle / 'capture.json').write_text(json.dumps(capture_json), encoding='utf-8')
    if asset is not None:
        (bundle / asset[0]).write_bytes(asset[1])
    return bundle


class IngestTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.archive, self.config = _make_archive(self.tmp)
        self.staging = self.tmp / 'staging'
        self.staging.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _ingest(self, dry_run=False) -> capture.Result:
        return capture.run_ingest(self.archive, self.config,
                                  staging_dir=str(self.staging), dry_run=dry_run)

    def _stubs(self) -> list[Path]:
        return sorted((self.archive / 'inbox').glob('*.notes.md'))

    def _two_clean_bundles(self) -> None:
        _make_bundle(
            self.staging, 'census-20260624',
            page_html=_sample('ancestry'),
            capture_json={'url': 'https://www.ancestry.com/rec/1',
                          'accessed': '2026-06-24',
                          'asset_mode': 'manual', 'asset_file': 'asset.jpg',
                          'people': ['Thomas Hartley', 'Margaret Hartley'],
                          'notes': 'The household I was looking for.'},
            asset=('asset.jpg', b'\xff\xd8\xff\xe0 fake jpeg'))
        _make_bundle(
            self.staging, 'obit-20260624',
            page_html=_sample('findagrave'),
            capture_json={'url': 'https://www.findagrave.com/memorial/1',
                          'accessed': '2026-06-24',
                          'asset_mode': 'singlefile', 'asset_file': 'asset.html'},
            asset=('asset.html', b'<html>inlined snapshot</html>'))

    # ── core sweep ──────────────────────────────────────────────────────────────

    def test_clean_sweep_files_stubs_and_parks_bundles(self) -> None:
        self._two_clean_bundles()
        res = self._ingest()
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        self.assertEqual(res.data['ingested'], 2)

        stubs = self._stubs()
        self.assertEqual(len(stubs), 2)
        for stub in stubs:
            self.assertEqual(read_record(stub)['parse_errors'], [])

        # Assets copied alongside their stubs.
        assets = [p for p in (self.archive / 'inbox').iterdir()
                  if p.suffix in ('.jpg', '.html')]
        self.assertEqual(len(assets), 2)

        # Bundles parked, not deleted; none left in the staging root.
        parked = sorted((self.staging / '.ingested').iterdir())
        self.assertEqual([p.name for p in parked], ['census-20260624', 'obit-20260624'])
        self.assertEqual(capture._iter_bundles(self.staging), [])

    def test_capture_json_overrides_win(self) -> None:
        self._two_clean_bundles()
        self._ingest()
        # Find the census stub by its people hint.
        census = next(s for s in self._stubs()
                      if 'Thomas Hartley' in s.read_text(encoding='utf-8'))
        rec = read_record(census)
        # notes → body; curated people → people:; accessed → link accessed date
        self.assertIn('The household I was looking for.', rec['body'])
        self.assertIn('Thomas Hartley', rec['meta']['people'])
        self.assertEqual(rec['meta']['external_links'][0]['accessed'], '2026-06-24')

    def test_ingest_stub_is_byte_identical_to_paste_fallback(self) -> None:
        """The seam's core guarantee: ingest produces the paste path's stub exactly."""
        cap = {'url': 'https://www.ancestry.com/rec/1', 'accessed': '2026-06-24',
               'asset_mode': 'none', 'people': ['Thomas Hartley'],
               'notes': 'A note.'}
        _make_bundle(self.staging, 'b-1', page_html=_sample('ancestry'), capture_json=cap)
        self._ingest()
        ingested_stub = self._stubs()[0].read_text(encoding='utf-8')

        # Same inputs straight through run_capture in a fresh archive.
        other = self.tmp / 'other'
        other.mkdir()
        arch2, cfg2 = _make_archive(other)
        capture.run_capture(
            arch2, cfg2, url=cap['url'], title=None, source_type=None,
            source_date=None, asset=None, html=_sample('ancestry'),
            accessed=cap['accessed'], notes=cap['notes'], people=cap['people'])
        paste_stub = next((arch2 / 'inbox').glob('*.notes.md')).read_text(encoding='utf-8')

        self.assertEqual(ingested_stub, paste_stub)

    # ── dry-run ───────────────────────────────────────────────────────────────

    def test_dry_run_writes_nothing(self) -> None:
        self._two_clean_bundles()
        res = self._ingest(dry_run=True)
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        self.assertFalse((self.archive / 'inbox').exists())
        self.assertFalse((self.staging / '.ingested').exists())
        # Bundles still in place.
        self.assertEqual(len(capture._iter_bundles(self.staging)), 2)

    # ── idempotency ───────────────────────────────────────────────────────────

    def test_idempotent_second_run_ingests_nothing(self) -> None:
        self._two_clean_bundles()
        self._ingest()
        res2 = self._ingest()
        self.assertEqual(res2.exit_code, EXIT_CLEAN)
        self.assertEqual(res2.data['ingested'], 0)
        self.assertEqual(len(self._stubs()), 2)  # no duplicates

    def test_same_named_bundle_after_ingest_is_skipped(self) -> None:
        _make_bundle(self.staging, 'dup', page_html=_sample('ancestry'),
                     capture_json={'url': 'https://x/1', 'asset_mode': 'none'})
        self._ingest()
        # A new bundle reuses a parked name → skipped, not clobbered.
        _make_bundle(self.staging, 'dup', page_html=_sample('ancestry'),
                     capture_json={'url': 'https://x/2', 'asset_mode': 'none'})
        res = self._ingest()
        self.assertEqual(res.data['skipped'], 1)
        self.assertEqual(res.data['ingested'], 0)
        self.assertEqual(len(self._stubs()), 1)

    # ── resilience ──────────────────────────────────────────────────────────────

    def test_malformed_bundle_reported_and_left_in_place(self) -> None:
        # Missing page.html.
        bad = self.staging / 'bad-1'
        bad.mkdir()
        (bad / 'capture.json').write_text('{"url": "https://x"}', encoding='utf-8')
        # Broken capture.json.
        bad2 = self.staging / 'bad-2'
        bad2.mkdir()
        (bad2 / 'page.html').write_text('<html></html>', encoding='utf-8')
        (bad2 / 'capture.json').write_text('{not json', encoding='utf-8')
        # A good one alongside.
        _make_bundle(self.staging, 'good', page_html=_sample('ancestry'),
                     capture_json={'url': 'https://x/ok', 'asset_mode': 'none'})

        res = self._ingest()
        self.assertEqual(res.exit_code, EXIT_ERRORS)   # non-clean: something failed
        self.assertEqual(res.data['ingested'], 1)       # the good one filed
        self.assertEqual(res.data['failed'], 2)
        self.assertEqual(len(self._stubs()), 1)
        # The two bad bundles stay in place.
        self.assertTrue(bad.exists())
        self.assertTrue(bad2.exists())

    def test_pointer_only_bundle_flags_asset_elsewhere(self) -> None:
        _make_bundle(self.staging, 'pointer', page_html=_sample('ancestry'),
                     capture_json={'url': 'https://x/held-at-courthouse',
                                   'asset_mode': 'none'})
        self._ingest()
        rec = read_record(self._stubs()[0])
        self.assertTrue(rec['meta'].get('asset_elsewhere'))

    # ── CLI dispatch + config/default resolution ────────────────────────────────

    def test_cli_ingest_dispatch(self) -> None:
        _make_bundle(self.staging, 'b', page_html=_sample('ancestry'),
                     capture_json={'url': 'https://x/1', 'asset_mode': 'none'})
        args = SimpleNamespace(root=str(self.archive), ingest=str(self.staging),
                               dry_run=False)
        self.assertEqual(capture._run_capture(args), EXIT_CLEAN)
        self.assertEqual(len(self._stubs()), 1)

    def test_capture_staging_config_key(self) -> None:
        d = capture._resolve_staging_dir(None, {'capture_staging': str(self.staging)})
        self.assertEqual(d, self.staging.resolve())

    def test_default_staging_when_unset(self) -> None:
        d = capture._resolve_staging_dir(None, {})
        self.assertEqual(d, (Path('~/Downloads/fha-inbox').expanduser().resolve()))

    def test_missing_staging_dir_is_clean_noop(self) -> None:
        res = capture.run_ingest(self.archive, self.config,
                                 staging_dir=str(self.tmp / 'nope'), dry_run=False)
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        self.assertEqual(res.data['status'], 'no-staging')

    # ── capture.json schema versioning ──────────────────────────────────────────

    def test_newer_schema_warns_but_still_ingests(self) -> None:
        _make_bundle(self.staging, 'future', page_html=_sample('ancestry'),
                     capture_json={'schema': capture._CAPTURE_JSON_SCHEMA + 5,
                                   'url': 'https://x/1', 'asset_mode': 'none'})
        err = io.StringIO()
        with mock.patch('sys.stderr', err):
            res = self._ingest()
        self.assertEqual(res.data['ingested'], 1)        # never refused
        self.assertIn('newer than this tool', err.getvalue())

    def test_current_and_absent_schema_are_silent(self) -> None:
        _make_bundle(self.staging, 'cur', page_html=_sample('ancestry'),
                     capture_json={'schema': capture._CAPTURE_JSON_SCHEMA,
                                   'url': 'https://x/1', 'asset_mode': 'none'})
        err = io.StringIO()
        with mock.patch('sys.stderr', err):
            self._ingest()
        self.assertNotIn('newer than this tool', err.getvalue())

    # ── doctor staged-captures nudge ────────────────────────────────────────────

    def test_staged_bundles_helper(self) -> None:
        self._two_clean_bundles()
        staging, pending = capture.staged_bundles(
            {'capture_staging': str(self.staging)})
        self.assertEqual(staging, self.staging.resolve())
        self.assertEqual(len(pending), 2)
        # After a sweep, the helper reports none pending (parked names excluded).
        self._ingest()
        _, pending2 = capture.staged_bundles({'capture_staging': str(self.staging)})
        self.assertEqual(pending2, [])

    def test_doctor_warns_on_pending_bundles(self) -> None:
        import doctor
        self._two_clean_bundles()
        (self.archive / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n  documents: documents\n'
            f'capture_staging: "{self.staging.as_posix()}"\n', encoding='utf-8')
        config = load_fha_yaml(self.archive, strict=True)
        res = doctor.run_doctor(self.archive, config)
        check = next(c for c in res.data['checks'] if c['id'] == 'staged-captures')
        self.assertEqual(check['status'], 'warn')
        self.assertEqual(check['next_step'], 'fha capture --ingest')
        self.assertTrue(any('staged captures: 2 bundle' in ln for ln in res.data['lines']))


if __name__ == '__main__':
    unittest.main()
