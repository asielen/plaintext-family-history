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
