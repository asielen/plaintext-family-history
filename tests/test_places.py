import argparse
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import places
from index import _DDL


def _make_index(archive_root: Path) -> sqlite3.Connection:
    cache = archive_root / '.cache'
    cache.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(cache / 'index.sqlite'))
    conn.executescript(_DDL)
    conn.row_factory = sqlite3.Row
    return conn


def _add_place(conn, pid, name, within=None, lat=None, lon=None):
    conn.execute(
        'INSERT INTO places(id, name, hierarchy, within, lat, lon) VALUES (?,?,?,?,?,?)',
        (pid, name, None, within, lat, lon),
    )


class _FakeConn:
    """Stub for sqlite3.Connection.execute(...) returning fixed dict rows,
    used to simulate a row type that real SQLite TEXT-affinity columns
    wouldn't actually produce (see test_non_string_within_reports_finding_not_crash)."""

    def __init__(self, rows):
        self._rows = rows

    def execute(self, *_args, **_kwargs):
        return self._rows


def _add_alt_name(conn, pid, alt_name):
    conn.execute('INSERT INTO place_names(place_id, alt_name) VALUES (?,?)', (pid, alt_name))


def _add_claim(conn, cid, place_id=None, place_text=None, date_edtf=None, status='accepted'):
    conn.execute(
        'INSERT INTO claims(id, source_id, type, value, status, place_id, place_text, date_edtf) '
        'VALUES (?,?,?,?,?,?,?,?)',
        (cid, 's-0000000001', 'residence', 'lived there', status, place_id, place_text, date_edtf),
    )


class PlacesLintTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        # The shared resolve_root_arg guard refuses an explicit --root without
        # fha.yaml, so the CLI-path tests need the synthetic root to look like
        # a real archive.
        (self.archive_root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        self.conn = _make_index(self.archive_root)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_clean_registry_has_no_findings(self) -> None:
        _add_place(self.conn, 'l-aaaaaaaaaa', 'Fairview')
        self.conn.commit()
        result = places.run_lint(self.archive_root)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['findings'], [])

    def test_orphan_place_id_on_claim(self) -> None:
        _add_place(self.conn, 'l-aaaaaaaaaa', 'Fairview')
        _add_claim(self.conn, 'c-1111111111', place_id='l-bbbbbbbbbb')
        self.conn.commit()
        result = places.run_lint(self.archive_root)
        codes = [f.code for f in result['findings']]
        self.assertIn('PL001', codes)

    def test_duplicate_name_case_folded(self) -> None:
        _add_place(self.conn, 'l-aaaaaaaaaa', 'Fairview')
        _add_place(self.conn, 'l-bbbbbbbbbb', 'FAIRVIEW')
        self.conn.commit()
        result = places.run_lint(self.archive_root)
        codes = [f.code for f in result['findings']]
        self.assertIn('PL002', codes)

    def test_duplicate_name_via_alt_name(self) -> None:
        _add_place(self.conn, 'l-aaaaaaaaaa', 'Fairview')
        _add_place(self.conn, 'l-bbbbbbbbbb', 'New Town')
        _add_alt_name(self.conn, 'l-bbbbbbbbbb', 'fairview')
        self.conn.commit()
        result = places.run_lint(self.archive_root)
        codes = [f.code for f in result['findings']]
        self.assertIn('PL002', codes)

    def test_same_place_name_and_alt_name_not_flagged(self) -> None:
        _add_place(self.conn, 'l-aaaaaaaaaa', 'Fairview')
        _add_alt_name(self.conn, 'l-aaaaaaaaaa', 'Fairview City')
        self.conn.commit()
        result = places.run_lint(self.archive_root)
        self.assertEqual(result['findings'], [])

    def test_dangling_within(self) -> None:
        _add_place(self.conn, 'l-aaaaaaaaaa', 'Hartley home', within='l-bbbbbbbbbb')
        self.conn.commit()
        result = places.run_lint(self.archive_root)
        codes = [f.code for f in result['findings']]
        self.assertIn('PL003', codes)

    def test_cyclic_within(self) -> None:
        _add_place(self.conn, 'l-aaaaaaaaaa', 'A', within='l-bbbbbbbbbb')
        _add_place(self.conn, 'l-bbbbbbbbbb', 'B', within='l-aaaaaaaaaa')
        self.conn.commit()
        result = places.run_lint(self.archive_root)
        codes = [f.code for f in result['findings']]
        self.assertIn('PL004', codes)
        # the cycle is reported once, not once per node
        self.assertEqual(codes.count('PL004'), 1)

    def test_self_loop_within_is_cyclic(self) -> None:
        _add_place(self.conn, 'l-aaaaaaaaaa', 'A', within='l-aaaaaaaaaa')
        self.conn.commit()
        result = places.run_lint(self.archive_root)
        codes = [f.code for f in result['findings']]
        self.assertIn('PL004', codes)

    def test_non_string_within_reports_finding_not_crash(self) -> None:
        # places.yaml's `within:` is meant to hold an L-id string, but a
        # non-string value reaching the `places` table (e.g. from a future
        # schema/type drift) must not crash normalize_id's .strip() call.
        # SQLite's TEXT column affinity coerces a plain int on INSERT, so
        # the type drift is reproduced directly against the row map rather
        # than through a real INSERT.
        rows, findings = places._within_map(_FakeConn([{'id': 'l-aaaaaaaaaa', 'within': 123}]))
        self.assertEqual(rows, {'l-aaaaaaaaaa': None})
        codes = [f.code for f in findings]
        self.assertIn('PL006', codes)

    def test_within_on_settlement_flagged(self) -> None:
        # Hartley home (micro-place) is within Fairview (settlement).
        # Fairview itself also links within: SomeOtherPlace, which is invalid -
        # a place that's already a within: target can't also point further up.
        _add_place(self.conn, 'l-home', 'Hartley home', within='l-fairview')
        _add_place(self.conn, 'l-fairview', 'Fairview', within='l-other')
        _add_place(self.conn, 'l-other', 'Other Town')
        self.conn.commit()
        result = places.run_lint(self.archive_root)
        codes = [f.code for f in result['findings']]
        self.assertIn('PL005', codes)

    def test_leaf_micro_place_within_settlement_not_flagged(self) -> None:
        _add_place(self.conn, 'l-home', 'Hartley home', within='l-fairview')
        _add_place(self.conn, 'l-fairview', 'Fairview')
        self.conn.commit()
        result = places.run_lint(self.archive_root)
        codes = [f.code for f in result['findings']]
        self.assertNotIn('PL005', codes)

    def test_missing_index_returns_failed(self) -> None:
        empty_root = Path(tempfile.mkdtemp())
        try:
            result = places.run_lint(empty_root)
            self.assertEqual(result['status'], 'failed')
        finally:
            shutil.rmtree(empty_root, ignore_errors=True)

    # Result.exit_code must carry the severity verdict itself (E -> 2, W -> 1,
    # clean -> 0), and the CLI must return exactly that code - a headless caller
    # reading run_lint(...).exit_code used to see 1 even for E-level findings.

    def test_error_findings_exit_code_errors_in_result_and_cli(self) -> None:
        _add_place(self.conn, 'l-aaaaaaaaaa', 'Fairview')
        _add_claim(self.conn, 'c-1111111111', place_id='l-bbbbbbbbbb')   # PL001 (E)
        self.conn.commit()
        result = places.run_lint(self.archive_root)
        self.assertEqual(result.exit_code, places.EXIT_ERRORS)
        args = argparse.Namespace(root=str(self.archive_root))
        self.assertEqual(places._cmd_places_lint(args), places.EXIT_ERRORS)

    def test_warning_only_findings_exit_code_warnings_in_result_and_cli(self) -> None:
        _add_place(self.conn, 'l-aaaaaaaaaa', 'Fairview')
        _add_place(self.conn, 'l-bbbbbbbbbb', 'FAIRVIEW')                # PL002 (W)
        self.conn.commit()
        result = places.run_lint(self.archive_root)
        self.assertEqual(result.exit_code, places.EXIT_WARNINGS)
        args = argparse.Namespace(root=str(self.archive_root))
        self.assertEqual(places._cmd_places_lint(args), places.EXIT_WARNINGS)

    def test_clean_registry_exit_code_clean_in_result_and_cli(self) -> None:
        _add_place(self.conn, 'l-aaaaaaaaaa', 'Fairview')
        self.conn.commit()
        result = places.run_lint(self.archive_root)
        self.assertEqual(result.exit_code, places.EXIT_CLEAN)
        args = argparse.Namespace(root=str(self.archive_root))
        self.assertEqual(places._cmd_places_lint(args), places.EXIT_CLEAN)

    def test_broken_places_fixture_fires_documented_cli_findings(self) -> None:
        fixture = ROOT / 'tests' / 'fixtures' / 'broken-places'
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / 'broken-places'
            shutil.copytree(fixture, work, ignore=shutil.ignore_patterns('.cache'))

            index_result = subprocess.run(
                [sys.executable, str(ROOT / 'tools' / 'fha.py'), 'index', '--root', str(work)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(index_result.returncode, 0, index_result.stderr + index_result.stdout)

            lint_result = subprocess.run(
                [sys.executable, str(ROOT / 'tools' / 'fha.py'), 'places', 'lint', '--root', str(work)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(lint_result.returncode, 2, lint_result.stderr + lint_result.stdout)
            self.assertIn('PL001', lint_result.stdout)
            self.assertIn('PL003', lint_result.stdout)


class PlacesCandidatesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = _make_index(self.archive_root)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_below_threshold_excluded(self) -> None:
        _add_claim(self.conn, 'c-1111111111', place_text='Topeka, Kansas')
        _add_claim(self.conn, 'c-2222222222', place_text='Topeka, Kansas')
        self.conn.commit()
        result = places.run_candidates(self.archive_root, {}, threshold=3)
        self.assertEqual(result['place_text_groups'], [])

    def test_meets_threshold_included(self) -> None:
        for i in range(3):
            _add_claim(self.conn, f'c-{i}111111111', place_text='Topeka, Kansas', date_edtf='1880')
        self.conn.commit()
        result = places.run_candidates(self.archive_root, {}, threshold=3)
        self.assertEqual(len(result['place_text_groups']), 1)
        group = result['place_text_groups'][0]
        self.assertEqual(group['claim_count'], 3)

    def test_word_order_and_abbreviation_variants_cluster_together(self) -> None:
        _add_claim(self.conn, 'c-1111111111', place_text='Topeka, Kansas')
        _add_claim(self.conn, 'c-2222222222', place_text='Kansas, Topeka')
        _add_claim(self.conn, 'c-3333333333', place_text='123 Main St')
        _add_claim(self.conn, 'c-4444444444', place_text='123 Main Street')
        _add_claim(self.conn, 'c-5555555555', place_text='123 Main Street')
        self.conn.commit()
        result = places.run_candidates(self.archive_root, {}, threshold=2)
        labels = {g['claim_count'] for g in result['place_text_groups']}
        self.assertIn(2, labels)
        self.assertIn(3, labels)

    def test_punctuation_variants_cluster_together(self) -> None:
        _add_claim(self.conn, 'c-1111111111', place_text='Topeka, Kansas')
        _add_claim(self.conn, 'c-2222222222', place_text='Topeka Kansas')
        _add_claim(self.conn, 'c-3333333333', place_text='Topeka, Kansas.')
        self.conn.commit()
        result = places.run_candidates(self.archive_root, {}, threshold=3)
        self.assertEqual(len(result['place_text_groups']), 1)
        self.assertEqual(result['place_text_groups'][0]['claim_count'], 3)

    def test_claims_with_place_id_excluded(self) -> None:
        for i in range(3):
            _add_claim(self.conn, f'c-{i}111111111', place_id='l-aaaaaaaaaa', place_text='Topeka, Kansas')
        self.conn.commit()
        result = places.run_candidates(self.archive_root, {}, threshold=3)
        self.assertEqual(result['place_text_groups'], [])

    def test_rejected_and_superseded_claims_excluded(self) -> None:
        for i, status in enumerate(['rejected', 'superseded', 'disputed']):
            _add_claim(self.conn, f'c-{i}111111111', place_text='Topeka, Kansas', status=status)
        self.conn.commit()
        result = places.run_candidates(self.archive_root, {}, threshold=3)
        self.assertEqual(result['place_text_groups'], [])

    def test_date_spread_reported(self) -> None:
        _add_claim(self.conn, 'c-1111111111', place_text='Topeka, Kansas', date_edtf='1870')
        _add_claim(self.conn, 'c-2222222222', place_text='Topeka, Kansas', date_edtf='1900')
        _add_claim(self.conn, 'c-3333333333', place_text='Topeka, Kansas', date_edtf='1880')
        self.conn.commit()
        result = places.run_candidates(self.archive_root, {}, threshold=3)
        group = result['place_text_groups'][0]
        self.assertEqual(group['date_min'], '1870-01-01')
        self.assertEqual(group['date_max'], '1900-12-31')

    def test_groups_field_is_formatted_strings_for_report(self) -> None:
        for i in range(3):
            _add_claim(self.conn, f'c-{i}111111111', place_text='Topeka, Kansas')
        self.conn.commit()
        result = places.run_candidates(self.archive_root, {}, threshold=3)
        self.assertEqual(len(result['groups']), 1)
        self.assertIsInstance(result['groups'][0], str)
        self.assertIn('Topeka', result['groups'][0])

    def test_missing_index_returns_failed(self) -> None:
        empty_root = Path(tempfile.mkdtemp())
        try:
            result = places.run_candidates(empty_root, {})
            self.assertEqual(result['status'], 'failed')
        finally:
            shutil.rmtree(empty_root, ignore_errors=True)

    def test_no_photos_db_skips_gps_clusters_without_error(self) -> None:
        _add_claim(self.conn, 'c-1111111111', place_text='Topeka, Kansas')
        self.conn.commit()
        result = places.run_candidates(self.archive_root, {}, threshold=3)
        self.assertEqual(result['gps_clusters'], [])

    def test_reconcile_missing_photos_excluded_from_gps_clusters(self) -> None:
        # Photos that `fha photoindex reconcile` has flagged vanished (path
        # rewritten to 'MISSING:<path>') must not still count toward a new
        # GPS cluster - there's no on-disk photo left to process.
        self.conn.commit()
        from photoindex import _DDL as _PHOTO_DDL
        cache = self.archive_root / '.cache'
        pconn = sqlite3.connect(str(cache / 'photos.sqlite'))
        pconn.executescript(_PHOTO_DDL)
        for i in range(3):
            pconn.execute(
                'INSERT INTO photos(path, group_id, is_primary, gps_lat, gps_lon) VALUES (?,?,1,?,?)',
                (f'MISSING:photos/p{i}.jpg', f'g{i}', 39.0 + i * 0.00001, -95.0 + i * 0.00001),
            )
        pconn.commit()
        pconn.close()
        result = places.run_candidates(self.archive_root, {}, threshold=3)
        self.assertEqual(result['gps_clusters'], [])

    def test_cli_rejects_explicit_zero_threshold(self) -> None:
        # --threshold 0 must be rejected, not silently coerced to the
        # default 3 (an `int or 3` style fallback would do exactly that).
        args = argparse.Namespace(root=str(self.archive_root), threshold=0)
        self.assertEqual(places._cmd_places_candidates(args), places.EXIT_FAILURE)


def _geo(name, lat, lon, country='US', admin1='', pop=1000, ascii_=None, alts=()):
    return places.GeoRow(
        name=name, asciiname=ascii_ or name, altnames=frozenset(a.lower() for a in alts),
        lat=lat, lon=lon, country=country, admin1=admin1, population=pop,
    )


class GeocodeMatchTests(unittest.TestCase):
    def test_unique_hit(self):
        gz = [_geo('Topeka', 39.05, -95.68, admin1='KS')]
        hit = places._match_place('Topeka', 'Topeka, Kansas, USA', gz)
        self.assertIsInstance(hit, places.GeoRow)
        self.assertEqual(hit.admin1, 'KS')

    def test_no_match(self):
        gz = [_geo('Topeka', 39.05, -95.68, admin1='KS')]
        self.assertIsNone(places._match_place('Nowhere', 'Nowhere, USA', gz))

    def test_ambiguous_same_name_two_states(self):
        gz = [_geo('Fairview', 39.8, -95.6, admin1='KS'),
              _geo('Fairview', 40.0, -74.0, admin1='NJ')]
        self.assertEqual(places._match_place('Fairview', 'Fairview, USA', gz), 'ambiguous')

    def test_state_narrows_ambiguity(self):
        gz = [_geo('Fairview', 39.8, -95.6, admin1='KS'),
              _geo('Fairview', 40.0, -74.0, admin1='NJ')]
        hit = places._match_place('Fairview', 'Fairview, Breton County, Kansas, USA', gz)
        self.assertIsInstance(hit, places.GeoRow)
        self.assertEqual(hit.admin1, 'KS')

    def test_country_filter(self):
        gz = [_geo('London', 51.5, -0.12, country='GB', admin1='ENG'),
              _geo('London', 42.98, -81.24, country='CA', admin1='08')]
        hit = places._match_place('London', 'London, England, United Kingdom', gz)
        self.assertEqual(hit.country, 'GB')

    def test_alt_name_match(self):
        gz = [_geo('Bombay', 19.07, 72.87, country='IN', admin1='16', alts=('Mumbai',))]
        hit = places._match_place('Mumbai', 'Mumbai, India', gz)
        self.assertIsInstance(hit, places.GeoRow)


class GeocodeYamlEditTests(unittest.TestCase):
    def test_insert_coords_and_alt_names(self):
        text = (
            '- id: L-7c1a9f4e22\n'
            '  name: Fairview\n'
            '  hierarchy: Fairview, Kansas, USA\n'
        )
        new, changed = places._apply_geocode_to_yaml(text, 'L-7c1a9f4e22', 39.8, -95.6, ['Fairview City'])
        self.assertTrue(changed)
        self.assertIn('coords: [39.8, -95.6]', new)
        self.assertIn('alt_names: [Fairview City]', new)
        # the id line and name line survive
        self.assertIn('- id: L-7c1a9f4e22', new)
        self.assertIn('name: Fairview', new)

    def test_replace_existing_coords(self):
        text = (
            '- id: L-7c1a9f4e22\n'
            '  name: Fairview\n'
            '  coords: [0.0, 0.0]\n'
        )
        new, changed = places._apply_geocode_to_yaml(text, 'L-7c1a9f4e22', 39.8, -95.6, [])
        self.assertTrue(changed)
        self.assertIn('coords: [39.8, -95.6]', new)
        self.assertNotIn('[0.0, 0.0]', new)

    def test_existing_alt_names_not_clobbered(self):
        text = (
            '- id: L-7c1a9f4e22\n'
            '  name: Fairview\n'
            '  alt_names: [Old Name]\n'
        )
        new, changed = places._apply_geocode_to_yaml(text, 'L-7c1a9f4e22', 39.8, -95.6, ['New Name'])
        self.assertIn('alt_names: [Old Name]', new)
        self.assertNotIn('New Name', new)

    def test_only_target_block_touched(self):
        text = (
            '- id: L-aaaaaaaaaa\n'
            '  name: Other\n'
            '- id: L-7c1a9f4e22\n'
            '  name: Fairview\n'
        )
        new, changed = places._apply_geocode_to_yaml(text, 'L-7c1a9f4e22', 39.8, -95.6, [])
        # coords land in the Fairview block, after its id line, not the Other block
        lines = new.splitlines()
        fv = lines.index('- id: L-7c1a9f4e22')
        self.assertTrue(lines[fv + 1].strip().startswith('coords:'))
        self.assertNotIn('coords:', '\n'.join(lines[:fv]))

    # A uniformly indented registry is valid YAML; the block-end scan used to
    # match only column-0 items, run the block to EOF, and let entry 1's coords
    # rewrite hit a LATER entry's coords line (alt_names appended to the tail).

    def test_indented_registry_edits_only_target_block(self):
        text = (
            '  - id: L-7c1a9f4e22\n'
            '    name: Fairview\n'
            '  - id: L-aaaaaaaaaa\n'
            '    name: Other\n'
            '    coords: [1.0, 2.0]\n'
        )
        new, changed = places._apply_geocode_to_yaml(
            text, 'L-7c1a9f4e22', 39.8, -95.6, ['Fairview City'])
        self.assertTrue(changed)
        lines = new.splitlines()
        fv = lines.index('  - id: L-7c1a9f4e22')
        other = lines.index('  - id: L-aaaaaaaaaa')
        # Entry 1 got the coords and the alt_names, inside its own block.
        self.assertIn('    coords: [39.8, -95.6]', lines[fv:other])
        self.assertIn('    alt_names: [Fairview City]', lines[fv:other])
        # Entry 2 is byte-identical - its coords line survives untouched.
        self.assertEqual(lines[other:], ['  - id: L-aaaaaaaaaa',
                                         '    name: Other',
                                         '    coords: [1.0, 2.0]'])

    def test_indented_registry_nested_list_stays_in_block(self):
        # A deeper-indented dash (a history/alt_names item) must NOT end the
        # block early; the next sibling `- id:` at the same indent does.
        import yaml
        text = (
            '  - id: L-7c1a9f4e22\n'
            '    name: Fairview\n'
            '    history:\n'
            '      - "1867: founded"\n'
            '  - id: L-aaaaaaaaaa\n'
            '    name: Other\n'
        )
        new, changed = places._apply_geocode_to_yaml(
            text, 'L-7c1a9f4e22', 39.8, -95.6, ['Fairview City'])
        self.assertTrue(changed)
        data = yaml.safe_load(new)
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]['coords'], [39.8, -95.6])
        self.assertEqual(data[0]['alt_names'], ['Fairview City'])
        self.assertEqual(data[0]['history'], ['1867: founded'])
        self.assertEqual(data[1], {'id': 'L-aaaaaaaaaa', 'name': 'Other'})

    def test_indented_registry_last_entry(self):
        text = (
            '  - id: L-aaaaaaaaaa\n'
            '    name: Other\n'
            '  - id: L-7c1a9f4e22\n'
            '    name: Fairview\n'
        )
        new, changed = places._apply_geocode_to_yaml(
            text, 'L-7c1a9f4e22', 39.8, -95.6, ['Fairview City'])
        self.assertTrue(changed)
        lines = new.splitlines()
        other = lines.index('  - id: L-aaaaaaaaaa')
        fv = lines.index('  - id: L-7c1a9f4e22')
        self.assertEqual(lines[other:fv], ['  - id: L-aaaaaaaaaa', '    name: Other'])
        self.assertIn('    coords: [39.8, -95.6]', lines[fv:])
        self.assertIn('    alt_names: [Fairview City]', lines[fv:])

    def test_comments_preserved(self):
        text = (
            '# registry header comment\n'
            '- id: L-7c1a9f4e22\n'
            '  name: Fairview  # inline comment\n'
        )
        new, _ = places._apply_geocode_to_yaml(text, 'L-7c1a9f4e22', 39.8, -95.6, [])
        self.assertIn('# registry header comment', new)
        self.assertIn('# inline comment', new)

    def test_unknown_id_no_change(self):
        text = '- id: L-7c1a9f4e22\n  name: Fairview\n'
        new, changed = places._apply_geocode_to_yaml(text, 'L-9999999999', 1.0, 2.0, [])
        self.assertFalse(changed)
        self.assertEqual(new, text)


class GeocodeRunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'places').mkdir()
        self.conn = _make_index(self.root)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _fresh_index(self):
        import os
        os.utime(self.root / '.cache' / 'index.sqlite')

    def _write_gazetteer(self, rows):
        gdir = self.root / '.cache' / 'geonames'
        gdir.mkdir(parents=True, exist_ok=True)
        lines = []
        for r in rows:
            cols = [''] * 19
            cols[1] = r['name']
            cols[2] = r.get('ascii', r['name'])
            cols[3] = r.get('alt', '')
            cols[4] = str(r['lat'])
            cols[5] = str(r['lon'])
            cols[8] = r.get('country', 'US')
            cols[10] = r.get('admin1', '')
            cols[14] = str(r.get('pop', 1000))
            lines.append('\t'.join(cols))
        (gdir / places._GEONAMES_MEMBER).write_text('\n'.join(lines), encoding='utf-8')

    def test_offline_no_gazetteer_when_places_need_coords(self):
        _add_place(self.conn, 'l-7c1a9f4e22', 'Fairview')  # no coords
        self.conn.commit()
        (self.root / 'places' / 'places.yaml').write_text(
            '- id: L-7c1a9f4e22\n  name: Fairview\n', encoding='utf-8')
        self._fresh_index()
        result = places.run_geocode(self.root, {}, offline=True)
        self.assertEqual(result['status'], 'no-gazetteer')
        self.assertEqual(result['written'], 0)

    def test_all_have_coords_is_clean(self):
        _add_place(self.conn, 'l-7c1a9f4e22', 'Fairview', lat=39.8, lon=-95.6)
        self.conn.commit()
        result = places.run_geocode(self.root, {}, offline=True)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['written'], 0)

    def test_decline_writes_nothing(self):
        _add_place(self.conn, 'l-7c1a9f4e22', 'Fairview',
                   within=None)  # no coords
        self.conn.execute("UPDATE places SET hierarchy=? WHERE id=?",
                          ('Fairview, Kansas, USA', 'l-7c1a9f4e22'))
        self.conn.commit()
        yaml_path = self.root / 'places' / 'places.yaml'
        original = '- id: L-7c1a9f4e22\n  name: Fairview\n  hierarchy: Fairview, Kansas, USA\n'
        yaml_path.write_text(original, encoding='utf-8')
        self._write_gazetteer([{'name': 'Fairview', 'lat': 39.8, 'lon': -95.6, 'admin1': 'KS'}])
        self._fresh_index()
        result = places.run_geocode(self.root, {}, offline=True,
                                    confirm=lambda prompt: False)
        self.assertEqual(result['written'], 0)
        self.assertEqual(yaml_path.read_text(encoding='utf-8'), original)

    def test_accept_writes_coords(self):
        _add_place(self.conn, 'l-7c1a9f4e22', 'Fairview')
        self.conn.execute("UPDATE places SET hierarchy=? WHERE id=?",
                          ('Fairview, Kansas, USA', 'l-7c1a9f4e22'))
        self.conn.commit()
        yaml_path = self.root / 'places' / 'places.yaml'
        yaml_path.write_text(
            '- id: L-7c1a9f4e22\n  name: Fairview\n  hierarchy: Fairview, Kansas, USA\n',
            encoding='utf-8')
        self._write_gazetteer([{'name': 'Fairview', 'lat': 39.8, 'lon': -95.6, 'admin1': 'KS'}])
        self._fresh_index()
        result = places.run_geocode(self.root, {}, offline=True,
                                    confirm=lambda prompt: True)
        self.assertEqual(result['written'], 1)
        self.assertIn('coords: [39.8, -95.6]', yaml_path.read_text(encoding='utf-8'))

    def test_not_found_place(self):
        self.conn.commit()
        result = places.run_geocode(self.root, {}, place_id='l-9999999999', offline=True)
        self.assertEqual(result['status'], 'not-found')


class HaversineTests(unittest.TestCase):
    def test_zero_distance(self) -> None:
        self.assertAlmostEqual(places._haversine_meters(39.5631, -95.1216, 39.5631, -95.1216), 0.0, places=3)

    def test_known_short_distance(self) -> None:
        # ~0.001 deg latitude is roughly 111 meters
        d = places._haversine_meters(39.5631, -95.1216, 39.5641, -95.1216)
        self.assertGreater(d, 90)
        self.assertLess(d, 130)


_SET_REGISTRY = (
    '# Place registry - a hand comment that must survive every edit\n'
    '- id: L-7c1a9f4e22\n'
    '  name: Fairview\n'
    '  coords: [39.8, -95.6]\n'
    '  hierarchy: Fairview, Breton County, Kansas, USA\n'
    '  alt_names: [Fairview City]\n'
    '  history:\n'
    '    - {period: "1858/1861", hierarchy: "Fairview, Breton Co., Kansas Territory, USA"}\n'
    '  notes: fictional town\n'
    '- id: L-9999999999\n'
    '  name: Elsewhere\n'
    '  coords: [1.0, 2.0]\n'
)


class PlaceSetNoteTests(unittest.TestCase):
    """fha places set / note: the human-directed registry write-backs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'places').mkdir(parents=True)
        self.registry = self.root / 'places' / 'places.yaml'
        self.registry.write_text(_SET_REGISTRY, encoding='utf-8', newline='')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _parsed(self):
        import yaml
        return {e['id']: e for e in yaml.safe_load(self.registry.read_text(encoding='utf-8'))}

    def test_set_coords_touches_only_the_target_block(self) -> None:
        result = places.run_place_set(self.root, 'L-7c1a9f4e22', coords='40.1, -95.0')
        self.assertEqual(result.exit_code, 0)
        text = self.registry.read_text(encoding='utf-8')
        self.assertIn('# Place registry - a hand comment', text)
        parsed = self._parsed()
        self.assertEqual(parsed['L-7c1a9f4e22']['coords'], [40.1, -95.0])
        self.assertEqual(parsed['L-9999999999']['coords'], [1.0, 2.0])

    def test_set_coords_out_of_range_refused(self) -> None:
        before = self.registry.read_bytes()
        result = places.run_place_set(self.root, 'L-7c1a9f4e22', coords='95.0, 10.0')
        self.assertEqual(result.exit_code, 3)
        self.assertIn('out of range', result.messages[0].text)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_set_aka_replaces_the_whole_list(self) -> None:
        result = places.run_place_set(self.root, 'L-7c1a9f4e22',
                                      aka=['Fairview City', 'Old Fairview'])
        self.assertEqual(result.exit_code, 0)
        parsed = self._parsed()
        self.assertEqual(parsed['L-7c1a9f4e22']['alt_names'],
                         ['Fairview City', 'Old Fairview'])

    def test_set_aka_and_history_empty_lists_clear(self) -> None:
        # The engine half of the workbench's "delete every line to clear"
        # (and the CLI's lone `--aka -` / `--history -` sentinel).
        result = places.run_place_set(self.root, 'L-7c1a9f4e22', aka=[], history=[])
        self.assertEqual(result.exit_code, 0)
        parsed = self._parsed()
        self.assertEqual(parsed['L-7c1a9f4e22'].get('alt_names'), [])
        self.assertEqual(parsed['L-7c1a9f4e22'].get('history'), [])

    def test_cli_dash_sentinel_clears_both_lists(self) -> None:
        import argparse as _ap
        import io as _io
        from contextlib import redirect_stdout
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        args = _ap.Namespace(root=str(self.root), place_id='L-7c1a9f4e22',
                             coords=None, aka=['-'], history=['-'], dry_run=False)
        with redirect_stdout(_io.StringIO()):
            rc = places._cmd_places_set(args)
        self.assertEqual(rc, 0)
        parsed = self._parsed()
        self.assertEqual(parsed['L-7c1a9f4e22'].get('alt_names'), [])
        self.assertEqual(parsed['L-7c1a9f4e22'].get('history'), [])

    def test_set_aka_name_with_comma_stays_one_alias(self) -> None:
        # P2 codex finding (round 2, PR #31): each name is verbatim - a
        # comma inside it ("Washington, D.C.") must survive the write and
        # parse back as ONE alias, never resplit.
        result = places.run_place_set(self.root, 'L-7c1a9f4e22',
                                      aka=['Washington, D.C.'])
        self.assertEqual(result.exit_code, 0)
        parsed = self._parsed()
        self.assertEqual(parsed['L-7c1a9f4e22']['alt_names'], ['Washington, D.C.'])

    def test_set_history_replaces_from_pipe_lines(self) -> None:
        result = places.run_place_set(
            self.root, 'L-7c1a9f4e22',
            history=['1854/1858 | Fairview settlement, Kansas Territory',
                     '1858/1861 | Fairview, Breton Co., Kansas Territory, USA'])
        self.assertEqual(result.exit_code, 0)
        parsed = self._parsed()
        hist = parsed['L-7c1a9f4e22']['history']
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[0]['period'], '1854/1858')
        self.assertEqual(hist[1]['hierarchy'], 'Fairview, Breton Co., Kansas Territory, USA')

    def test_set_history_refuses_an_unreadable_period(self) -> None:
        # P2 codex finding (round 1, PR #31): a typo period ("1858??") used to
        # be written verbatim; edtf_bounds() then indexed it at the all-time
        # 0001..9999 bounds, silently scrambling names-over-time order.
        before = self.registry.read_bytes()
        result = places.run_place_set(
            self.root, 'L-7c1a9f4e22',
            history=['1858?? | Fairview, Kansas Territory'])
        self.assertEqual(result.exit_code, 3)
        self.assertIn('history period', result.messages[0].text)
        self.assertIn('1858/1861', result.messages[0].text)   # the range example
        self.assertEqual(self.registry.read_bytes(), before)

    def test_set_history_normalizes_a_loose_period(self) -> None:
        # Loose human wording is read the same way claim dates are.
        result = places.run_place_set(
            self.root, 'L-7c1a9f4e22',
            history=['circa 1858 | Fairview settlement, Kansas Territory'])
        self.assertEqual(result.exit_code, 0)
        hist = self._parsed()['L-7c1a9f4e22']['history']
        self.assertEqual(hist[0]['period'], '1858~')

    def test_set_nothing_refused(self) -> None:
        result = places.run_place_set(self.root, 'L-7c1a9f4e22')
        self.assertEqual(result.exit_code, 3)
        self.assertIn('nothing to change', result.messages[0].text)

    def test_unknown_place_exit1_next_step(self) -> None:
        result = places.run_place_set(self.root, 'L-bbbbbbbbbb', coords='1, 2')
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.data['status'], 'not-found')
        self.assertEqual(result.messages[0].next_step, 'fha find L-bbbbbbbbbb')

    def test_dry_run_writes_nothing_and_shows_diff(self) -> None:
        before = self.registry.read_bytes()
        result = places.run_place_set(self.root, 'L-7c1a9f4e22',
                                      coords='40.1, -95.0', dry_run=True)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.data['status'], 'dry-run')
        self.assertEqual(self.registry.read_bytes(), before)
        joined = '\n'.join(m.text for m in result.messages)
        self.assertIn('+  coords: [40.1, -95.0]', joined)

    def test_note_appends_dated_paragraph_keeping_old_notes(self) -> None:
        result = places.run_place_note(self.root, 'L-7c1a9f4e22',
                                       'Platted 1858 per the county history.')
        self.assertEqual(result.exit_code, 0)
        parsed = self._parsed()
        notes = parsed['L-7c1a9f4e22']['notes']
        self.assertIn('fictional town', notes)
        self.assertIn('Platted 1858 per the county history.', notes)
        # Dated: the appended paragraph starts with an ISO date stamp.
        import re as _re
        self.assertTrue(_re.search(r'\d{4}-\d{2}-\d{2}: Platted 1858', notes))
        # The rest of the block survived the notes rewrite.
        self.assertEqual(parsed['L-7c1a9f4e22']['coords'], [39.8, -95.6])

    def test_note_creates_the_key_when_absent(self) -> None:
        result = places.run_place_note(self.root, 'L-9999999999', 'First note.')
        self.assertEqual(result.exit_code, 0)
        parsed = self._parsed()
        self.assertIn('First note.', parsed['L-9999999999']['notes'])

    def test_note_empty_text_refused(self) -> None:
        result = places.run_place_note(self.root, 'L-7c1a9f4e22', '   ')
        self.assertEqual(result.exit_code, 3)

    def test_edit_note_rewrites_only_the_named_entry(self) -> None:
        places.run_place_note(self.root, 'L-7c1a9f4e22', 'Second note.')
        result = places.run_place_edit_note(
            self.root, 'L-7c1a9f4e22', 'fictional town', 'fictional town (example fixture)')
        self.assertEqual(result.exit_code, 0)
        notes = self._parsed()['L-7c1a9f4e22']['notes']
        self.assertIn('fictional town (example fixture)', notes)
        self.assertIn('Second note.', notes)

    def test_edit_note_not_found_refused_nothing_written(self) -> None:
        before = self.registry.read_bytes()
        result = places.run_place_edit_note(
            self.root, 'L-7c1a9f4e22', 'Never written.', 'x')
        self.assertEqual(result.exit_code, 3)
        self.assertIn('not found', result.messages[0].text)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_edit_note_empty_replacement_refused(self) -> None:
        result = places.run_place_edit_note(
            self.root, 'L-7c1a9f4e22', 'fictional town', '  ')
        self.assertEqual(result.exit_code, 3)


if __name__ == '__main__':
    unittest.main()
