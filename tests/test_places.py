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
        # Fairview itself also links within: SomeOtherPlace, which is invalid —
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
        # GPS cluster — there's no on-disk photo left to process.
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
        result = places.run_geocode(self.root, {}, all_places=True, offline=True)
        self.assertEqual(result['status'], 'no-gazetteer')
        self.assertEqual(result['written'], 0)

    def test_all_have_coords_is_clean(self):
        _add_place(self.conn, 'l-7c1a9f4e22', 'Fairview', lat=39.8, lon=-95.6)
        self.conn.commit()
        result = places.run_geocode(self.root, {}, all_places=True, offline=True)
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
        result = places.run_geocode(self.root, {}, all_places=True, offline=True,
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
        result = places.run_geocode(self.root, {}, all_places=True, offline=True,
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


if __name__ == '__main__':
    unittest.main()
