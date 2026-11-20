import argparse
import contextlib
import io
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import places
from index import _DDL
from _lib import index_manifest_path, record_path_manifest, shell_quote, write_path_manifest


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


def _add_claim(conn, cid, place_id=None, place_text=None, date_edtf=None, status='accepted',
               reviewed=None):
    conn.execute(
        'INSERT INTO claims(id, source_id, type, value, status, place_id, place_text, date_edtf, '
        'reviewed) VALUES (?,?,?,?,?,?,?,?,?)',
        (cid, 's-0000000001', 'residence', 'lived there', status, place_id, place_text, date_edtf,
         reviewed),
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

    def test_suggested_claims_excluded_by_default(self) -> None:
        # Codex review, PR #150 (following issue #79 point 4): the everyday
        # view must keep excluding still-unreviewed claims - only the
        # setup-interview skill's one-time backlog scan opts into counting
        # them, via --include-suggested.
        for i in range(3):
            _add_claim(self.conn, f'c-{i}111111111', place_text='Topeka, Kansas', status='suggested')
        self.conn.commit()
        result = places.run_candidates(self.archive_root, {}, threshold=3)
        self.assertEqual(result['place_text_groups'], [])

    def test_include_suggested_widens_the_cluster(self) -> None:
        # A fresh `fha gedcom import` mints every claim as `suggested` - the
        # exact scenario the setup-interview skill's step 0 must be able to
        # see. include_suggested=True must count them without excluding the
        # normal accepted and parked (suggested + reviewed:) claims.
        _add_claim(self.conn, 'c-0111111111', place_text='Topeka, Kansas', status='accepted')
        _add_claim(self.conn, 'c-1111111111', place_text='Topeka, Kansas', status='suggested',
                   reviewed='2026-03-01')
        _add_claim(self.conn, 'c-2111111111', place_text='Topeka, Kansas', status='suggested')
        self.conn.commit()
        without = places.run_candidates(self.archive_root, {}, threshold=3)
        self.assertEqual(without['place_text_groups'], [])   # only 2 count by default
        result = places.run_candidates(self.archive_root, {}, threshold=3, include_suggested=True)
        self.assertEqual(len(result['place_text_groups']), 1)
        self.assertEqual(result['place_text_groups'][0]['claim_count'], 3)

    def test_include_suggested_still_excludes_rejected_and_superseded(self) -> None:
        _add_claim(self.conn, 'c-0111111111', place_text='Topeka, Kansas', status='suggested')
        _add_claim(self.conn, 'c-1111111111', place_text='Topeka, Kansas', status='suggested')
        _add_claim(self.conn, 'c-2111111111', place_text='Topeka, Kansas', status='rejected')
        self.conn.commit()
        result = places.run_candidates(self.archive_root, {}, threshold=3, include_suggested=True)
        self.assertEqual(result['place_text_groups'], [])   # only 2 suggested, below threshold 3

    def test_cli_include_suggested_flag_reaches_the_engine(self) -> None:
        (self.archive_root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        for i in range(3):
            _add_claim(self.conn, f'c-{i}111111111', place_text='Topeka, Kansas', status='suggested')
        self.conn.commit()
        args = argparse.Namespace(root=str(self.archive_root), threshold=3, include_suggested=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = places._cmd_places_candidates(args)
        self.assertEqual(rc, places.EXIT_CLEAN)
        # The real guard: the CLI flag must actually reach run_candidates and
        # widen the cluster, not just parse without error.
        self.assertIn('Found 1 candidate place-text cluster', buf.getvalue())
        self.assertIn('Topeka, Kansas - 3 claim(s)', buf.getvalue())

    def test_date_spread_reported(self) -> None:
        _add_claim(self.conn, 'c-1111111111', place_text='Topeka, Kansas', date_edtf='1870')
        _add_claim(self.conn, 'c-2222222222', place_text='Topeka, Kansas', date_edtf='1900')
        _add_claim(self.conn, 'c-3333333333', place_text='Topeka, Kansas', date_edtf='1880')
        self.conn.commit()
        result = places.run_candidates(self.archive_root, {}, threshold=3)
        group = result['place_text_groups'][0]
        self.assertEqual(group['date_min'], '1870-01-01')
        self.assertEqual(group['date_max'], '1900-12-31')

    def test_before_and_after_dates_never_make_a_backwards_spread(self) -> None:
        # Codex, #218: "before 1900" and "after 1920" at one place read as the
        # backwards spread 1920-01-01/1900-12-31. The spread runs forward over
        # the dates the claims name; a side stays open only when every dated
        # claim is open on it.
        from _lib import format_date_spread
        cases = (
            (('[..1900]', '[1920..]', '1910'), '1900-12-31', '1920-01-01'),
            (('[1920..]', '[1930..]', '[1925..]'), '1920-01-01', None),
            (('[..1900]', '[..1890]', '[..1895]'), None, '1900-12-31'),
            (('1870', '[1920..]', '1880'), '1870-01-01', '1920-01-01'),
        )
        for dates, want_min, want_max in cases:
            with self.subTest(dates=dates):
                self.conn.execute('DELETE FROM claims')
                for i, d in enumerate(dates):
                    _add_claim(self.conn, f'c-{i}111111111', place_text='Oak Hill',
                               date_edtf=d)
                self.conn.commit()
                group = places.run_candidates(self.archive_root, {}, threshold=3)[
                    'place_text_groups'][0]
                self.assertEqual((group['date_min'], group['date_max']),
                                 (want_min, want_max))
                spread = format_date_spread(group['date_min'], group['date_max'])
                self.assertNotIn('9999', spread)
                self.assertNotIn('0001', spread)

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


# ── Geocode: synthetic GeoNames files (tests never touch the network) ─────────

_COUNTRY_INFO = (
    '#ISO\tISO3\tISO-Numeric\tfips\tCountry\tCapital\n'
    'US\tUSA\t840\tUS\tUnited States\tWashington\n'
    'PL\tPOL\t616\tPL\tPoland\tWarsaw\n'
    'GE\tGEO\t268\tGG\tGeorgia\tTbilisi\n'
    'IL\tISR\t376\tIS\tIsrael\tJerusalem\n'
    'DE\tDEU\t276\tGM\tGermany\tBerlin\n'
)
_ADMIN1 = (
    'US.KS\tKansas\tKansas\t4273857\n'
    'US.NJ\tNew Jersey\tNew Jersey\t5101760\n'
    'US.NY\tNew York\tNew York\t5128638\n'
    'US.GA\tGeorgia\tGeorgia\t4197000\n'
    'PL.81\tPodlasie\tPodlasie\t858789\n'
    'DE.16\tBerlin\tBerlin\t2950157\n'
    'DE.04\tHamburg\tHamburg\t2911297\n'
)
_ADMIN2 = (
    'US.KS.091\tBreton County\tBreton County\t1\n'
    'US.NJ.025\tMonmouth County\tMonmouth County\t2\n'
    'US.NY.061\tNew York County\tNew York County\t3\n'
    'US.NY.017\tCarrow County\tCarrow County\t4\n'
    'US.GA.121\tFulton County\tFulton County\t5\n'
    'PL.81.2002\tPowiat białostocki\tPowiat bialostocki\t6\n'
)

# (gid, name, ascii, alts, lat, lon, fclass, fcode, admin1, admin2, admin3, pop)
_US_FEATURES = [
    (100, 'United States', 'United States', 'USA,America', 39.76, -98.5, 'A', 'PCLI', '00', '', '', 300000000),
    (101, 'Kansas', 'Kansas', 'KS', 38.5, -98.0, 'A', 'ADM1', 'KS', '', '', 2900000),
    (102, 'Breton County', 'Breton County', '', 39.5, -95.2, 'A', 'ADM2', 'KS', '091', '', 50000),
    (103, 'Fairview', 'Fairview', 'Fairview City', 39.5631, -95.1216, 'P', 'PPL', 'KS', '091', '', 900),
    (104, 'Fairview', 'Fairview', '', 40.3687, -74.0471, 'P', 'PPL', 'NJ', '025', '', 3800),
    (105, 'Fairview Heights', 'Fairview Heights', '', 39.57, -95.13, 'P', 'PPLX', 'KS', '091', '', 0),
    (106, 'Fairview', 'Fairview', '', 39.56, -95.12, 'P', 'PPLX', 'KS', '091', '', 0),
    (107, 'Kansas', 'Kansas', '', 40.07, -74.05, 'P', 'PPL', 'NJ', '025', '', 10),
    (108, 'New York', 'New York', 'NY', 43.0, -75.5, 'A', 'ADM1', 'NY', '', '', 19000000),
    (109, 'New York City', 'New York City', 'New York,NYC', 40.71427, -74.00597, 'P', 'PPL', 'NY', '061', '', 8000000),
    (110, 'New York County', 'New York County', '', 40.77, -73.97, 'A', 'ADM2', 'NY', '061', '', 1600000),
    (111, 'Easton', 'Easton', '', 42.98, -76.10, 'P', 'PPL', 'NY', '017', '', 2000),
    (112, 'Coleville', 'Coleville', 'Easton', 42.90, -76.20, 'P', 'PPL', 'NY', '017', '', 500),
    (113, 'Atlanta', 'Atlanta', '', 33.749, -84.388, 'P', 'PPLA', 'GA', '121', '', 400000),
    (114, 'Georgia', 'Georgia', 'GA', 32.75, -83.5, 'A', 'ADM1', 'GA', '', '', 10000000),
    (115, 'Tykwin', 'Tykwin', '', 38.9, -95.3, 'P', 'PPL', 'KS', '091', '', 40),
    (116, 'Oak Hill Cemetery', 'Oak Hill Cemetery', '', 39.565, -95.125, 'S', 'CMTY', 'KS', '091', '', 0),
    (117, 'Oak Hill Cemetery', 'Oak Hill Cemetery', '', 40.37, -74.05, 'S', 'CMTY', 'NJ', '025', '', 0),
    (118, 'Saint Mary Church', 'Saint Mary Church', '', 39.564, -95.122, 'S', 'CH', 'KS', '091', '', 0),
    (119, 'Fairview Inn', 'Fairview Inn', '', 39.563, -95.121, 'S', 'HTL', 'KS', '091', '', 0),
    (120, 'Ashby', 'Ashby', '', 39.6, -95.2, 'S', 'CH', 'KS', '091', '', 0),
    (121, 'Ashby', 'Ashby', '', 39.61, -95.21, 'P', 'PPLH', 'KS', '091', '', 0),
]
# Two city-states: each is a state and a city inside it.
_DE_FEATURES = [
    (300, 'Federal Republic of Germany', 'Federal Republic of Germany', 'Germany,Deutschland', 51.5, 10.5, 'A', 'PCLI', '00', '', '', 82000000),
    (301, 'Berlin', 'Berlin', '', 52.5, 13.4, 'A', 'ADM1', '16', '', '', 3400000),
    (302, 'Berlin', 'Berlin', '', 52.52437, 13.41053, 'P', 'PPLC', '16', '00', '', 3400000),
    (303, 'Hamburg', 'Hamburg', '', 53.5, 10.0, 'A', 'ADM1', '04', '', '', 1700000),
    (304, 'Hamburg', 'Hamburg', '', 53.57532, 10.01534, 'P', 'PPLA', '04', '00', '', 1700000),
]
_PL_FEATURES = [
    (200, 'Republic of Poland', 'Republic of Poland', 'Poland,Polska', 52.0, 20.0, 'A', 'PCLI', '00', '', '', 38000000),
    (201, 'Podlaskie Voivodeship', 'Podlaskie Voivodeship', 'Podlaskie,Podlasie', 53.25, 23.0, 'A', 'ADM1', '81', '', '', 1100000),
    (202, 'Powiat białostocki', 'Powiat bialostocki', '', 53.1, 23.0, 'A', 'ADM2', '81', '2002', '', 150000),
    (203, 'Tykwin', 'Tykwin', 'Gmina Tykwin', 53.19, 22.76, 'A', 'ADM3', '81', '2002', '200212', 6000),
    (204, 'Tykwin', 'Tykwin', '', 53.2, 22.77, 'P', 'PPL', '81', '2002', '200212', 2000),
    (205, 'Wola Mała', 'Wola Mala', '', 53.17865, 22.73223, 'P', 'PPL', '81', '2002', '200212', 0),
]


def _dump_lines(rows, cc):
    out = []
    for gid, name, ascii_, alts, lat, lon, fcl, fco, a1, a2, a3, pop in rows:
        cols = [str(gid), name, ascii_, alts, str(lat), str(lon), fcl, fco, cc, '',
                a1, a2, a3, '', str(pop), '', '0', 'UTC', '2026-01-01']
        out.append('\t'.join(cols))
    # A feature class geocode never keeps (a stream): must be filtered out.
    out.append('\t'.join(['999', 'Fairview Creek', 'Fairview Creek', '', '39.5', '-95.1',
                          'H', 'STM', cc, '', '', '', '', '', '0', '', '0', 'UTC', '2026-01-01']))
    return '\n'.join(out) + '\n'


def _write_geonames(gdir: Path, countries=('US', 'PL', 'DE')) -> None:
    """The three name files plus one {CC}.zip per country, shaped like the
    real GeoNames export."""
    import zipfile
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / 'countryInfo.txt').write_text(_COUNTRY_INFO, encoding='utf-8')
    (gdir / 'admin1CodesASCII.txt').write_text(_ADMIN1, encoding='utf-8')
    (gdir / 'admin2Codes.txt').write_text(_ADMIN2, encoding='utf-8')
    rows = {'US': _US_FEATURES, 'PL': _PL_FEATURES, 'DE': _DE_FEATURES}
    for cc in countries:
        with zipfile.ZipFile(gdir / f'{cc}.zip', 'w') as zf:
            zf.writestr(f'{cc}.txt', _dump_lines(rows[cc], cc))
            zf.writestr('readme.txt', 'synthetic')


def _row(name, hierarchy=None):
    return {'name': name, 'hierarchy': hierarchy}


class _GazetteerCase(unittest.TestCase):
    """A small synthetic gazetteer (US and Poland), loaded offline."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write_geonames(self.root / '.cache' / 'geonames')
        rows = [_row('Fairview', 'Fairview, Breton County, Kansas, USA'),
                _row('Tykwin', 'Tykwin, Poland'), _row('Berlin', 'Berlin, Germany')]
        self.gaz, notes = places._prepare_gazetteer(
            self.root, rows, offline=True, progress=lambda _l: None)
        self.assertIsNotNone(self.gaz, notes)

    def tearDown(self):
        self.gaz.conn.close()
        self._tmp.cleanup()


class GeocodeMatchTests(_GazetteerCase):
    """_match_place: one fitting candidate is a hit, never a guess."""

    def match(self, name, hierarchy=None):
        return places._match_place(name, hierarchy, self.gaz)

    def test_full_hierarchy_is_a_unique_hit(self):
        m = self.match('Fairview', 'Fairview, Breton County, Kansas, USA')
        self.assertEqual(m.status, 'hit')
        self.assertEqual((m.hit.lat, m.hit.lon), (39.5631, -95.1216))

    def test_county_word_and_country_are_optional(self):
        for hierarchy in ('Fairview, Breton, Kansas', 'Fairview, Breton Co., Kansas, USA',
                          'Fairview, Kansas, U.S.A.'):
            with self.subTest(hierarchy=hierarchy):
                m = self.match('Fairview', hierarchy)
                self.assertEqual(m.status, 'hit')
                self.assertEqual(m.hit.gid, 103)

    def test_same_name_in_two_states_is_ambiguous(self):
        m = self.match('Fairview', 'Fairview, USA')
        self.assertEqual(m.status, 'ambiguous')
        self.assertEqual({g.gid for g in m.candidates}, {103, 104})

    def test_conflicting_hierarchy_part_is_not_a_hit(self):
        m = self.match('Fairview', 'Fairview, Monmouth County, Kansas, USA')
        self.assertEqual(m.status, 'partial')
        self.assertIsNone(m.hit)
        self.assertTrue(m.unmatched)

    def test_unknown_name(self):
        self.assertEqual(self.match('Nowhere', 'Nowhere, Kansas, USA').status, 'none')

    def test_a_state_is_a_general_place_not_a_town_of_that_name(self):
        # Owner decision: a state needs no coordinates.
        self.assertEqual(self.match('Kansas', 'Kansas, USA').status, 'general')
        self.assertEqual(self.match('Kansas').status, 'general')

    def test_county_place_matches_the_county(self):
        m = self.match('Breton County', 'Breton County, Kansas, USA')
        self.assertEqual(m.status, 'hit')
        self.assertEqual(m.hit.gid, 102)

    def test_country_on_its_own_is_a_general_place(self):
        self.assertEqual(self.match('Poland', 'Poland').status, 'general')
        self.assertEqual(self.match('England').status, 'general')

    def test_historic_region_is_a_general_place(self):
        self.assertEqual(self.match('Prussia').status, 'general')
        self.assertEqual(self.match('Galicia', 'Galicia').status, 'general')

    def test_town_named_like_its_county_and_state(self):
        # "New York, New York, USA" is the city: the state cannot be its own
        # state, and New York County says "County" where the place does not.
        m = self.match('New York', 'New York, New York, USA')
        self.assertEqual(m.status, 'hit')
        self.assertEqual(m.hit.gid, 109)

    def test_state_with_its_own_city_reads_as_the_city(self):
        # New York City lies in New York State, so "New York, USA" is the city.
        m = self.match('New York', 'New York, USA')
        self.assertEqual((m.status, m.hit.gid), ('hit', 109))

    def test_country_written_in_the_name_is_read_like_a_hierarchy(self):
        # "Tykwin, Poland" with no hierarchy is the Polish town, never the
        # Kansas namesake.
        m = self.match('Tykwin, Poland')
        self.assertEqual(m.status, 'hit')
        self.assertEqual(m.hit.country, 'PL')

    def test_place_naming_no_country_goes_to_review(self):
        # One entry fits, but nothing says which country: the human decides.
        m = self.match('Easton')
        self.assertEqual(m.status, 'ambiguous')
        self.assertEqual(m.candidates[0].gid, 111)

    def test_cemetery_matches_when_its_town_county_and_state_agree(self):
        m = self.match('Oak Hill Cemetery',
                       'Oak Hill Cemetery, Fairview, Breton County, Kansas, USA')
        self.assertEqual(m.status, 'hit')
        self.assertEqual(m.hit.gid, 116)
        m = self.match('Saint Mary Church', 'Saint Mary Church, Fairview, Kansas, USA')
        self.assertEqual(m.status, 'hit')
        self.assertEqual(m.hit.gid, 118)

    def test_cemetery_never_matches_on_its_name_alone(self):
        self.assertNotEqual(self.match('Oak Hill Cemetery').status, 'hit')
        self.assertEqual(self.match('Oak Hill Cemetery', 'Oak Hill Cemetery, USA').status,
                         'ambiguous')
        # A town the cemetery's county does not hold is a disagreement.
        self.assertEqual(self.match('Oak Hill Cemetery',
                                    'Oak Hill Cemetery, Easton, Kansas, USA').status, 'partial')

    def test_village_with_no_country_uses_loaded_countries(self):
        # "podlaskie" is a spelling only the loaded Polish data knows; the
        # parish word and diacritics are tolerated.
        m = self.match('Wola Mala', 'Wola Mala, parafia Tykwin, podlaskie')
        self.assertEqual(m.status, 'hit')
        self.assertEqual(m.hit.gid, 205)

    def test_town_outranks_the_commune_of_the_same_name(self):
        m = self.match('Tykwin', 'Tykwin, Poland')
        self.assertEqual(m.status, 'hit')
        self.assertEqual(m.hit.fclass, 'P')

    def test_own_name_outranks_an_alternate_spelling(self):
        m = self.match('Easton', 'Easton, Carrow County, New York, USA')
        self.assertEqual(m.status, 'hit')
        self.assertEqual(m.hit.gid, 111)

    def test_living_town_outranks_a_neighbourhood(self):
        m = self.match('Fairview', 'Fairview, Kansas, USA')
        self.assertEqual(m.hit.gid, 103)

    def test_name_with_commas_is_looked_up_by_its_first_part(self):
        m = self.match('Fairview, Kansas', 'Fairview, Breton County, Kansas, USA')
        self.assertEqual(m.status, 'hit')

    def test_state_that_shares_a_country_name_searches_both(self):
        m = self.match('Atlanta', 'Atlanta, Georgia')
        self.assertEqual(m.status, 'hit')
        self.assertEqual(m.hit.gid, 113)

    def test_city_that_is_also_its_state_is_a_town(self):
        # Berlin, Hamburg and New York are states with a city of the same
        # name inside them: the city is geocoded.
        m = self.match('Berlin', 'Berlin, Germany')
        self.assertEqual((m.status, m.hit.gid), ('hit', 302))
        # Alone, the name says no country: the city is listed for review.
        m = self.match('Hamburg')
        self.assertEqual((m.status, [g.gid for g in m.candidates]), ('ambiguous', [304]))
        m = self.match('New York')
        self.assertEqual((m.status, [g.gid for g in m.candidates]), ('ambiguous', [109]))

    def test_lone_state_name_never_implies_its_country(self):
        # A bare "Berlin" in a US registry may be Berlin, Kansas: it goes to
        # review with every Berlin, never straight to Germany.
        self.assertEqual(places._place_countries('berlin', [], self.gaz), set())
        extra = (130, 'Berlin', 'Berlin', '', 39.6, -95.3, 'P', 'PPL', 'KS', '091', '', 500)
        self.gaz.conn.execute('INSERT INTO features VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                              (extra[0], 'US', *extra[1:3], *extra[4:8], *extra[8:12]))
        self.gaz.conn.execute("INSERT INTO names VALUES ('berlin', 'US', 130)")
        m = self.match('Berlin')
        self.assertEqual(m.status, 'ambiguous')
        self.assertEqual({g.gid for g in m.candidates}, {302, 130})

    def test_state_with_a_namesake_town_elsewhere_stays_general(self):
        # The village of Kansas in New Jersey does not make Kansas a town.
        self.assertEqual(self.match('Kansas', 'Kansas, USA').status, 'general')
        self.assertNotIn('posen', places._HISTORIC_REGIONS)   # Posen is also a city

    def test_historical_town_outranks_a_church_of_the_same_name(self):
        m = self.match('Ashby', 'Ashby, Breton County, Kansas, USA')
        self.assertEqual((m.status, m.hit.gid), ('hit', 121))

    def test_church_fits_when_its_namesake_town_is_elsewhere(self):
        # A town called Grace in New Jersey does not hide the church of
        # Grace in Fairview, Kansas.
        rows = [(131, 'Grace', 'Grace', '', 40.3, -74.0, 'P', 'PPL', 'NJ', '025', '', 100),
                (132, 'Grace', 'Grace', '', 39.562, -95.123, 'S', 'CH', 'KS', '091', '', 0)]
        for gid, name, ascii_, _a, lat, lon, fcl, fco, a1, a2, a3, pop in rows:
            self.gaz.conn.execute('INSERT INTO features VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                                  (gid, 'US', name, ascii_, lat, lon, fcl, fco, a1, a2, a3, pop))
            self.gaz.conn.execute("INSERT INTO names VALUES ('grace', 'US', ?)", (gid,))
        m = self.match('Grace', 'Grace, Fairview, Kansas, USA')
        self.assertEqual((m.status, m.hit.gid), ('hit', 132))

    def test_all_fitting_candidates_are_returned(self):
        m = self.match('Fairview', 'Fairview, USA')
        self.assertEqual(len(m.candidates), 2)

    def test_two_letter_state_code_is_not_a_country(self):
        # "IL" is Illinois to a genealogist, never Israel.
        self.assertNotIn('IL', places._place_countries('fairview', ['IL'], self.gaz))


class GeocodeFillTests(_GazetteerCase):
    """_filled_hierarchy: missing levels added, existing words never touched."""

    def fill(self, name, hierarchy):
        m = places._match_place(name, hierarchy, self.gaz)
        self.assertEqual(m.status, 'hit')
        return places._filled_hierarchy(name, hierarchy, m, self.gaz)

    def test_missing_county_and_country_slot_in_place(self):
        self.assertEqual(self.fill('Fairview', 'Fairview, Kansas'),
                         'Fairview, Breton County, Kansas, United States')

    def test_registry_spelling_of_the_country_is_preferred(self):
        places._registry_country_spellings(
            [_row('Easton', 'Easton, New York, USA')], self.gaz)
        self.assertEqual(self.fill('Fairview', 'Fairview, Kansas'),
                         'Fairview, Breton County, Kansas, USA')

    def test_existing_text_kept_word_for_word(self):
        self.assertEqual(self.fill('Fairview', 'Fairview, Breton Co., Kansas'),
                         'Fairview, Breton Co., Kansas, United States')

    def test_nothing_missing_proposes_nothing(self):
        self.assertIsNone(self.fill('Fairview', 'Fairview, Breton County, Kansas, USA'))

    def test_country_in_the_name_with_no_hierarchy_gets_one(self):
        self.assertEqual(self.fill('Wola Mała, Poland', None),
                         'Wola Mała, Powiat białostocki, Podlasie, Poland')

    def test_division_gets_only_the_levels_above_it(self):
        self.assertEqual(self.fill('Breton County', 'Breton County, Kansas'),
                         'Breton County, Kansas, United States')

    def test_city_state_does_not_repeat_its_own_name(self):
        self.assertIsNone(self.fill('Berlin', 'Berlin, Germany'))

    def test_city_inside_a_same_named_state_keeps_the_state(self):
        # New York City has a county between it and New York State, so the
        # state is a real level of its hierarchy.
        self.assertEqual(self.fill('New York', 'New York, USA'),
                         'New York, New York County, New York, USA')

    def test_cemetery_keeps_its_town_and_gains_the_rest(self):
        self.assertEqual(self.fill('Oak Hill Cemetery', 'Oak Hill Cemetery, Fairview, Kansas'),
                         'Oak Hill Cemetery, Fairview, Breton County, Kansas, United States')

    def test_part_that_could_be_two_levels_adds_neither(self):
        # "New York" is both the county and the state of the city.
        self.assertIsNone(self.fill('New York', 'New York, New York, USA'))


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

    def test_comment_line_after_the_id_does_not_set_the_indent(self):
        import yaml
        text = ('- id: L-7c1a9f4e22\n    # moved from the old notebook\n'
                '  name: Fairview\n  hierarchy: Fairview, Kansas\n')
        new, changed = places._apply_geocode_to_yaml(
            text, 'L-7c1a9f4e22', 39.5, -95.1, ['Fair #view'],
            'Fairview, Breton County, Kansas, USA')
        entry = yaml.safe_load(new)[0]
        self.assertEqual(entry['hierarchy'], 'Fairview, Breton County, Kansas, USA')
        self.assertEqual(entry['coords'], [39.5, -95.1])
        self.assertEqual(entry['alt_names'], ['Fair #view'])
        self.assertIn('# moved from the old notebook', new)

    def test_empty_alt_names_list_is_filled(self):
        import yaml
        text = '- id: L-7c1a9f4e22\n  name: Fairview\n  alt_names: []\n'
        new, _ = places._apply_geocode_to_yaml(text, 'L-7c1a9f4e22', 1.0, 2.0, ['Fairview City'])
        self.assertEqual(yaml.safe_load(new)[0]['alt_names'], ['Fairview City'])

    def test_filled_hierarchy_replaces_only_that_key(self):
        import yaml
        text = (
            '# header\n'
            '- id: L-7c1a9f4e22\n'
            '  name: Fairview  # the town\n'
            '  hierarchy: Fairview, Kansas\n'
            '- id: L-aaaaaaaaaa\n'
            '  name: Other\n'
            '  hierarchy: Other, Kansas\n'
        )
        new, changed = places._apply_geocode_to_yaml(
            text, 'L-7c1a9f4e22', 39.8, -95.6, [],
            'Fairview, Breton County, Kansas, USA')
        self.assertTrue(changed)
        data = yaml.safe_load(new)
        self.assertEqual(data[0]['hierarchy'], 'Fairview, Breton County, Kansas, USA')
        self.assertEqual(data[1]['hierarchy'], 'Other, Kansas')
        self.assertIn('# the town', new)
        self.assertIn('# header', new)

    def test_hierarchy_added_when_absent(self):
        import yaml
        text = '- id: L-7c1a9f4e22\n  name: Fairview\n'
        new, _ = places._apply_geocode_to_yaml(
            text, 'L-7c1a9f4e22', 39.8, -95.6, [], 'Fairview, Kansas, USA')
        self.assertEqual(yaml.safe_load(new)[0]['hierarchy'], 'Fairview, Kansas, USA')

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


_GEO_REGISTRY = (
    '# hand comment\n'
    '- id: L-7c1a9f4e22\n'
    '  name: Fairview\n'
    '  hierarchy: Fairview, Kansas, USA\n'
    '- id: L-8d2b0a5f33\n'
    '  name: Wola Mała\n'
    '  hierarchy: Wola Mała, Poland\n'
    '- id: L-9e3c1b6a44\n'
    '  name: Fairview\n'
    '  hierarchy: Fairview, USA\n'
    '- id: L-ab4d2c7b55\n'
    '  name: Nowhere\n'
    '  hierarchy: Nowhere, Kansas, USA\n'
)


class GeocodeRunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'places').mkdir()
        self.conn = _make_index(self.root)
        self.gdir = self.root / '.cache' / 'geonames'
        self.yaml_path = self.root / 'places' / 'places.yaml'

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _fresh_index(self):
        import os
        os.utime(self.root / '.cache' / 'index.sqlite')
        # This synthetic index.sqlite is hand-built via raw DDL, bypassing
        # build_index/upsert_source (the only writers of the path manifest),
        # so write the manifest here or open_index_db reads places.yaml as
        # newly added, i.e. stale.
        write_path_manifest(
            index_manifest_path(self.root), record_path_manifest(self.root))

    def _registry(self, text=_GEO_REGISTRY):
        import yaml
        self.yaml_path.write_text(text, encoding='utf-8')
        for entry in yaml.safe_load(text):
            self.conn.execute(
                'INSERT INTO places(id, name, hierarchy, within, lat, lon) VALUES (?,?,?,?,?,?)',
                (entry['id'].lower(), entry['name'], entry.get('hierarchy'), None,
                 (entry.get('coords') or [None, None])[0],
                 (entry.get('coords') or [None, None])[1]))
            for alt in entry.get('alt_names') or []:
                _add_alt_name(self.conn, entry['id'].lower(), alt)
        self.conn.commit()
        self._fresh_index()

    def _run(self, answers=(True,), **kw):
        prompts = []

        def confirm(prompt):
            prompts.append(prompt)
            return answers[len(prompts) - 1]
        result = places.run_geocode(self.root, {}, offline=kw.pop('offline', True),
                                    confirm=confirm, **kw)
        return result, prompts

    def test_offline_no_gazetteer_when_places_need_coords(self):
        self._registry()
        result, prompts = self._run()
        self.assertEqual(result['status'], 'no-gazetteer')
        self.assertEqual(result['written'], 0)
        self.assertEqual(prompts, [])
        self.assertIn('--offline', '\n'.join(result['messages']))

    def test_all_have_coords_is_clean(self):
        _add_place(self.conn, 'l-7c1a9f4e22', 'Fairview', lat=39.8, lon=-95.6)
        self.conn.commit()
        result = places.run_geocode(self.root, {}, offline=True)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['written'], 0)

    def test_one_question_for_the_whole_batch_and_one_write(self):
        import yaml
        self._registry()
        _write_geonames(self.gdir)
        result, prompts = self._run()
        self.assertEqual(len(prompts), 1)
        self.assertIn('Write coordinates for these 2 places?', prompts[0])
        self.assertIn('l-7c1a9f4e22', prompts[0])
        self.assertIn('l-8d2b0a5f33', prompts[0])
        self.assertEqual(result['written'], 2)
        text = self.yaml_path.read_text(encoding='utf-8')
        self.assertIn('# hand comment', text)
        data = {e['id']: e for e in yaml.safe_load(text)}
        self.assertEqual(data['L-7c1a9f4e22']['coords'], [39.5631, -95.1216])
        self.assertEqual(data['L-7c1a9f4e22']['hierarchy'],
                         'Fairview, Breton County, Kansas, USA')
        # GeoNames spells it the registry's way, so no alt name is added.
        self.assertNotIn('alt_names', data['L-7c1a9f4e22'])
        self.assertEqual(data['L-8d2b0a5f33']['coords'], [53.17865, 22.73223])
        self.assertEqual(data['L-8d2b0a5f33']['hierarchy'],
                         'Wola Mała, Powiat białostocki, Podlasie, Poland')
        self.assertEqual(data['L-8d2b0a5f33']['alt_names'], ['Wola Mala'])
        # The ambiguous and unknown places are untouched.
        self.assertNotIn('coords', data['L-9e3c1b6a44'])
        self.assertNotIn('coords', data['L-ab4d2c7b55'])

    def test_ambiguous_place_lists_the_command_that_settles_each_candidate(self):
        self._registry()
        _write_geonames(self.gdir)
        result, _ = self._run(answers=(False,))
        out = '\n'.join(result['messages'])
        expected = 'fha places set l-9e3c1b6a44 --coords ' + shell_quote('39.5631, -95.1216')
        self.assertIn(expected, out)
        self.assertIn('fha places set l-9e3c1b6a44 --coords '
                      + shell_quote('40.3687, -74.0471'), out)
        self.assertIn('l-ab4d2c7b55 Nowhere', out)

    def test_decline_writes_nothing(self):
        self._registry()
        _write_geonames(self.gdir)
        result, prompts = self._run(answers=(False,))
        self.assertEqual(len(prompts), 1)
        self.assertEqual(result['written'], 0)
        self.assertEqual(self.yaml_path.read_text(encoding='utf-8'), _GEO_REGISTRY)

    def test_dry_run_shows_the_table_and_writes_nothing(self):
        self._registry()
        _write_geonames(self.gdir)
        result, prompts = self._run(dry_run=True)
        self.assertEqual(prompts, [])
        self.assertEqual(result['written'], 2)
        out = '\n'.join(result['messages'])
        self.assertIn('l-7c1a9f4e22', out)
        self.assertIn('Breton County', out)
        self.assertIn('[dry-run]', out)
        self.assertEqual(self.yaml_path.read_text(encoding='utf-8'), _GEO_REGISTRY)

    def test_failed_write_leaves_the_registry_whole(self):
        self._registry()
        _write_geonames(self.gdir)
        with mock.patch.object(places, 'write_text_exact_atomic',
                               side_effect=OSError('disk full')):
            result, _ = self._run()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['written'], 0)
        self.assertEqual(self.yaml_path.read_text(encoding='utf-8'), _GEO_REGISTRY)

    def test_curated_alt_names_are_not_extended(self):
        import yaml
        self._registry('- id: L-8d2b0a5f33\n  name: Wola Mała\n'
                       '  hierarchy: Wola Mała, Poland\n  alt_names: [Wola]\n')
        _write_geonames(self.gdir)
        result, _ = self._run()
        self.assertEqual(result['written'], 1)
        entry = yaml.safe_load(self.yaml_path.read_text(encoding='utf-8'))[0]
        self.assertEqual(entry['alt_names'], ['Wola'])

    def test_edit_made_while_the_question_is_open_survives(self):
        import yaml
        self._registry()
        _write_geonames(self.gdir)

        def confirm(prompt):
            text = self.yaml_path.read_text(encoding='utf-8')
            text = text.replace('  name: Nowhere\n',
                                '  name: Nowhere\n  notes: typed in the workbench\n')
            self.yaml_path.write_text(text, encoding='utf-8')
            return True
        result = places.run_geocode(self.root, {}, offline=True, confirm=confirm)
        data = {e['id']: e for e in yaml.safe_load(self.yaml_path.read_text(encoding='utf-8'))}
        self.assertEqual(data['L-ab4d2c7b55']['notes'], 'typed in the workbench')
        self.assertIn('coords', data['L-7c1a9f4e22'])
        self.assertEqual(result['written'], 2)

    def test_place_changed_while_the_question_is_open_is_skipped(self):
        import yaml
        self._registry()
        _write_geonames(self.gdir)

        def confirm(prompt):
            text = self.yaml_path.read_text(encoding='utf-8')
            text = text.replace('  hierarchy: Fairview, Kansas, USA\n',
                                '  hierarchy: Fairview, Kansas, USA\n  coords: [1.0, 2.0]\n')
            self.yaml_path.write_text(text, encoding='utf-8')
            return True
        result = places.run_geocode(self.root, {}, offline=True, confirm=confirm)
        data = {e['id']: e for e in yaml.safe_load(self.yaml_path.read_text(encoding='utf-8'))}
        self.assertEqual(data['L-7c1a9f4e22']['coords'], [1.0, 2.0])
        self.assertIn('coords', data['L-8d2b0a5f33'])
        self.assertEqual(result['written'], 1)
        self.assertIn('l-7c1a9f4e22', '\n'.join(result['messages']))

    def test_damaged_dump_is_one_plain_line_and_the_rest_carry_on(self):
        import zipfile
        self._registry()
        _write_geonames(self.gdir)
        zp = self.gdir / 'US.zip'
        data = zipfile.ZipFile(zp).read('US.txt') * 50
        with zipfile.ZipFile(zp, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('US.txt', data)
        raw = bytearray(zp.read_bytes())
        for i in range(200, 260):
            raw[i] ^= 0xFF
        zp.write_bytes(bytes(raw))
        result, _ = self._run(dry_run=True)
        out = '\n'.join(result['messages'])
        self.assertIn('US.zip could not be read', out)
        self.assertIn('l-8d2b0a5f33', out)   # Poland still matched
        self.assertEqual(result['written'], 1)

    def test_damaged_cache_is_a_plain_message_and_closes_the_cache(self):
        self._registry()
        _write_geonames(self.gdir)
        with mock.patch.object(places._Gazetteer, 'features_named',
                               side_effect=sqlite3.DatabaseError('database disk image is malformed')):
            result, prompts = self._run(dry_run=True)
        self.assertEqual(result['status'], 'no-gazetteer')
        self.assertIn('gazetteer.sqlite', '\n'.join(result['messages']))
        # Closed: on Windows an open connection would block this delete.
        (self.gdir / places._GAZETTEER_DB).unlink()

    def test_empty_alt_names_list_shows_and_writes_the_same(self):
        import yaml
        self._registry('- id: L-8d2b0a5f33\n  name: Wola Mała\n'
                       '  hierarchy: Wola Mała, Poland\n  alt_names: []\n')
        _write_geonames(self.gdir)
        result, prompts = self._run()
        self.assertIn('also known as Wola Mala', prompts[0])
        entry = yaml.safe_load(self.yaml_path.read_text(encoding='utf-8'))[0]
        self.assertEqual(entry['alt_names'], ['Wola Mala'])

    def test_general_places_get_one_summary_line(self):
        self._registry('- id: L-7c1a9f4e22\n  name: Poland\n'
                       '- id: L-8d2b0a5f33\n  name: Kansas\n  hierarchy: Kansas, USA\n'
                       '- id: L-9e3c1b6a44\n  name: Prussia\n'
                       '- id: L-ab4d2c7b55\n  name: Fairview\n'
                       '  hierarchy: Fairview, Breton County, Kansas, USA\n')
        _write_geonames(self.gdir)
        result, prompts = self._run(dry_run=True)
        out = '\n'.join(result['messages'])
        self.assertIn('3 general places (countries, states, regions) need no coordinates', out)
        self.assertEqual(result['written'], 1)
        self.assertNotIn('Not found', out)
        self.assertNotIn('Poland  ', out)

    def test_general_places_download_no_country(self):
        self._registry('- id: L-7c1a9f4e22\n  name: Poland\n'
                       '- id: L-8d2b0a5f33\n  name: Prussia\n')
        source = self.root / 'source-files'
        _write_geonames(source)
        fetched = []

        def fake_fetch(url, dest):
            fetched.append(url.rsplit('/', 1)[1])
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes((source / dest.name).read_bytes())
            return None
        with mock.patch.object(places, '_fetch_url', side_effect=fake_fetch):
            result, prompts = self._run(offline=False)
        self.assertEqual(sorted(fetched), ['admin1CodesASCII.txt', 'admin2Codes.txt',
                                           'countryInfo.txt'])
        self.assertEqual(prompts, [])
        self.assertIn('2 general places', '\n'.join(result['messages']))

    def test_country_in_the_name_downloads_that_country(self):
        self._registry('- id: L-8d2b0a5f33\n  name: Tykwin, Poland\n')
        source = self.root / 'source-files'
        _write_geonames(source)
        fetched = []

        def fake_fetch(url, dest):
            fetched.append(url.rsplit('/', 1)[1])
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes((source / dest.name).read_bytes())
            return None
        with mock.patch.object(places, '_fetch_url', side_effect=fake_fetch):
            result, _ = self._run(offline=False, dry_run=True)
        self.assertIn('PL.zip', fetched)
        self.assertNotIn('US.zip', fetched)
        self.assertEqual(result['written'], 1)

    def test_edit_made_while_the_data_loads_is_kept(self):
        # The human edits the place while the dumps are read; the yes must
        # not write the hierarchy the index had back over it.
        import yaml
        self._registry('- id: L-7c1a9f4e22\n  name: Fairview\n  hierarchy: Fairview, Kansas\n')
        _write_geonames(self.gdir)
        edited = []

        def progress(line):
            if not edited:
                text = self.yaml_path.read_text(encoding='utf-8')
                text = text.replace('  hierarchy: Fairview, Kansas\n',
                                    '  hierarchy: Fairview, Breton Co., Kansas Territory, USA\n'
                                    '  coords: [39.8, -95.6]\n')
                self.yaml_path.write_text(text, encoding='utf-8')
                edited.append(line)
        result = places.run_geocode(self.root, {}, offline=True, confirm=lambda p: True,
                                    progress=progress)
        self.assertTrue(edited)
        entry = yaml.safe_load(self.yaml_path.read_text(encoding='utf-8'))[0]
        self.assertEqual(entry['hierarchy'], 'Fairview, Breton Co., Kansas Territory, USA')
        self.assertEqual(entry['coords'], [39.8, -95.6])
        self.assertEqual(result['written'], 0)
        self.assertIn('l-7c1a9f4e22', '\n'.join(result['messages']))

    def test_church_proposal_says_it_is_a_church(self):
        self._registry('- id: L-7c1a9f4e22\n  name: Saint Mary Church\n'
                       '  hierarchy: Saint Mary Church, Fairview, Kansas, USA\n')
        _write_geonames(self.gdir)
        result, prompts = self._run(dry_run=True)
        self.assertEqual(result['written'], 1)
        self.assertIn('the church', '\n'.join(result['messages']))

    def _many_fairviews(self, n):
        return [(400 + i, 'Fairview', 'Fairview', '', 30.0 + i, -90.0, 'P', 'PPL', 'NJ',
                 '025', '', 10 + i) for i in range(n)]

    def test_candidates_beyond_the_shown_ten_are_counted_not_dropped(self):
        extra = self._many_fairviews(10)   # 12 fitting Fairviews in all
        _US_FEATURES.extend(extra)
        try:
            self._registry('- id: L-9e3c1b6a44\n  name: Fairview\n  hierarchy: Fairview, USA\n')
            _write_geonames(self.gdir)
            result, _ = self._run(dry_run=True)
            out = '\n'.join(result['messages'])
            self.assertEqual(out.count('fha places set l-9e3c1b6a44'), 10)
            self.assertIn('and 2 more', out)
            self.assertIn('fha places geocode --place l-9e3c1b6a44', out)
            result, _ = self._run(dry_run=True, place_id='l-9e3c1b6a44')
            out = '\n'.join(result['messages'])
            self.assertEqual(out.count('fha places set l-9e3c1b6a44'), 12)
            self.assertNotIn('more; to see them all', out)
        finally:
            del _US_FEATURES[-len(extra):]

    def test_place_naming_no_country_with_nothing_saved_says_what_to_add(self):
        self._registry('- id: L-9e3c1b6a44\n  name: Fairview\n  hierarchy: Fairview, Breton County\n')
        _write_geonames(self.gdir, countries=())
        result, _ = self._run(place_id='l-9e3c1b6a44')
        out = '\n'.join(result['messages'])
        self.assertIn('name no country', out)
        self.assertIn('add the country', out)
        self.assertNotIn('loaded for :', out)
        self.assertNotIn('loaded for )', out)

    def test_hierarchy_with_a_hand_comment_is_not_rewritten(self):
        self._registry('- id: L-7c1a9f4e22\n  name: Fairview\n'
                       '  hierarchy: Fairview, Kansas  # as the 1880 census has it\n')
        _write_geonames(self.gdir)
        result, prompts = self._run()
        self.assertEqual(result['written'], 1)
        self.assertNotIn('hierarchy "', prompts[0])
        text = self.yaml_path.read_text(encoding='utf-8')
        self.assertIn('  hierarchy: Fairview, Kansas  # as the 1880 census has it\n', text)
        self.assertIn('coords: [39.5631, -95.1216]', text)

    def test_alt_name_another_place_answers_to_is_not_added(self):
        # Adding "Wola Mala" to the first place would make two places share
        # a name (lint PL002) and stop claims linking to either.
        import yaml
        self._registry('- id: L-8d2b0a5f33\n  name: Wola Mała\n'
                       '  hierarchy: Wola Mała, Poland\n'
                       '- id: L-ab4d2c7b55\n  name: Wola Mala\n  coords: [50.0, 20.0]\n')
        _write_geonames(self.gdir)
        result, _ = self._run()
        self.assertEqual(result['written'], 1)
        entry = yaml.safe_load(self.yaml_path.read_text(encoding='utf-8'))[0]
        self.assertNotIn('alt_names', entry)

    def test_single_place_with_coords_shows_what_it_replaces(self):
        self._registry('- id: L-7c1a9f4e22\n  name: Fairview\n'
                       '  coords: [1.0, 2.0]\n  hierarchy: Fairview, Breton County, Kansas, USA\n')
        _write_geonames(self.gdir)
        result, prompts = self._run(place_id='l-7c1a9f4e22')
        self.assertIn('replaces 1, 2', prompts[0])
        self.assertEqual(result['written'], 1)

    def test_offline_skips_a_country_not_saved_and_matches_the_rest(self):
        self._registry()
        _write_geonames(self.gdir, countries=('US',))
        result, prompts = self._run(dry_run=True)
        out = '\n'.join(result['messages'])
        self.assertIn('No GeoNames data saved for Poland', out)
        self.assertEqual(result['written'], 1)
        self.assertEqual(prompts, [])

    def test_downloads_only_the_countries_the_registry_names(self):
        self._registry('- id: L-7c1a9f4e22\n  name: Fairview\n'
                       '  hierarchy: Fairview, Breton County, Kansas, USA\n')
        source = self.root / 'source-files'
        _write_geonames(source)
        fetched = []

        def fake_fetch(url, dest):
            fetched.append(url.rsplit('/', 1)[1])
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes((source / dest.name).read_bytes())
            return None
        with mock.patch.object(places, '_fetch_url', side_effect=fake_fetch):
            result, _ = self._run(offline=False, dry_run=True)
            self.assertEqual(sorted(fetched), ['US.zip', 'admin1CodesASCII.txt',
                                               'admin2Codes.txt', 'countryInfo.txt'])
            self.assertEqual(result['written'], 1)
            # A second run reads the saved data and fetches nothing more.
            fetched.clear()
            self._run(offline=False, dry_run=True)
            self.assertEqual(fetched, [])

    def test_download_failure_is_a_plain_message(self):
        self._registry()
        with mock.patch.object(places, '_fetch_url', return_value='no route to host'):
            result, prompts = self._run(offline=False)
        self.assertEqual(result['status'], 'no-gazetteer')
        self.assertEqual(prompts, [])
        self.assertIn('Could not download', '\n'.join(result['messages']))

    def test_real_fetch_failure_returns_a_reason_not_an_exception(self):
        dest = self.root / 'x.txt'
        with mock.patch.object(places.urllib.request, 'urlopen',
                               side_effect=OSError('offline')):
            self.assertEqual(places._fetch_url('https://example.invalid/x', dest), 'offline')
        self.assertFalse(dest.exists())
        self.assertFalse((self.root / 'x.txt.part').exists())

    def test_cache_from_another_schema_is_rebuilt_from_the_saved_dumps(self):
        self._registry()
        _write_geonames(self.gdir)
        self._run(dry_run=True)
        db = sqlite3.connect(str(self.gdir / places._GAZETTEER_DB))
        db.execute("UPDATE meta SET v = 'old' WHERE k = 'schema'")
        db.commit()
        db.close()
        result, _ = self._run(dry_run=True)
        self.assertEqual(result['written'], 2)

    def test_not_found_place(self):
        self.conn.commit()
        result = places.run_geocode(self.root, {}, place_id='l-9999999999', offline=True)
        self.assertEqual(result['status'], 'not-found')

    def test_dump_keeps_towns_divisions_churches_and_cemeteries(self):
        _write_geonames(self.gdir)
        db = places._open_gazetteer_db(self.gdir / places._GAZETTEER_DB)
        try:
            self.assertIsNone(places._import_country(db, self.gdir / 'US.zip', 'US'))
            classes = {r[0] for r in db.execute('SELECT DISTINCT fclass FROM features')}
            self.assertEqual(classes, {'P', 'A', 'S'})
            codes = {r[0] for r in db.execute("SELECT fcode FROM features WHERE fclass = 'S'")}
            self.assertEqual(codes, {'CMTY', 'CH'})
        finally:
            db.close()

    def test_unreadable_dump_leaves_the_country_unloaded(self):
        self.gdir.mkdir(parents=True)
        (self.gdir / 'US.zip').write_bytes(b'not a zip')
        db = places._open_gazetteer_db(self.gdir / places._GAZETTEER_DB)
        try:
            self.assertIsNotNone(places._import_country(db, self.gdir / 'US.zip', 'US'))
            self.assertIsNone(db.execute("SELECT 1 FROM loaded WHERE cc = 'US'").fetchone())
        finally:
            db.close()


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

    def test_clearing_absent_lists_is_a_no_op(self) -> None:
        # Final-review finding (PR #31): an untouched Apply of the aka /
        # names-over-time modal posts an empty field (data-wb-allowempty),
        # which must NOT write 'alt_names: []' / 'history: []' onto a place
        # that never had those keys - a no-op action may not mutate the
        # registry.
        before = self.registry.read_bytes()
        result = places.run_place_set(self.root, 'L-9999999999',
                                      aka=[''], history=[''])
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertIn('no change', ' '.join(m.text for m in result.messages))

    def test_history_period_without_name_is_refused(self) -> None:
        # Final-review finding (PR #31): a 'PERIOD | ' line (period, empty
        # hierarchy) used to be silently dropped - and since the workbench
        # prefills a hand-authored period-only registry entry in exactly
        # that shape, any unrelated history edit silently deleted it. Now a
        # plain refusal, nothing written.
        before = self.registry.read_bytes()
        result = places.run_place_set(self.root, 'L-7c1a9f4e22',
                                      history=['1900 | '])
        self.assertEqual(result.exit_code, 3)
        self.assertIn('no place name', result.messages[0].text)
        self.assertEqual(self.registry.read_bytes(), before)

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
        self.assertEqual(result.messages[0].next_step, 'fha find l-bbbbbbbbbb')

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

    def test_note_long_paragraph_stays_on_one_line(self) -> None:
        # The registry is a file he can open and edit, so a long note is one
        # line in the `notes: |` block, never broken at a column.
        long_note = ' '.join(['Platted 1858 beside the Plains Junction Railroad depot.'] * 4)
        places.run_place_note(self.root, 'L-7c1a9f4e22', long_note)
        raw = self.registry.read_text(encoding='utf-8')
        self.assertTrue(any(line.strip().endswith(long_note) for line in raw.splitlines()))
        self.assertIn(long_note, self._parsed()['L-7c1a9f4e22']['notes'])

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

    def test_edit_note_one_sentence_asks_for_the_whole_paragraph(self) -> None:
        # Issue #272, the place twin: `fictional town` is the fixture's whole
        # note, so `town` alone is only part of it.
        before = self.registry.read_bytes()
        result = places.run_place_edit_note(self.root, 'L-7c1a9f4e22', 'town', 'x')
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(result.data['reason'], 'entry-partial')
        msg = result.messages[0].text
        self.assertIn('whole paragraph', msg)
        self.assertIn('starts "fictional town"', msg)
        self.assertNotIn('Reload', msg)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_edit_note_text_inside_several_notes_says_how_many(self) -> None:
        places.run_place_note(self.root, 'L-7c1a9f4e22', 'Another town record.')
        result = places.run_place_edit_note(self.root, 'L-7c1a9f4e22', 'town', 'x')
        self.assertEqual(result.exit_code, 3)
        self.assertIn('inside 2 different notes', result.messages[0].text)

    def test_edit_note_whole_paragraph_on_one_line_is_that_note(self) -> None:
        self.registry.write_text(
            _SET_REGISTRY.replace('  notes: fictional town\n',
                                  '  notes: |\n    fictional town,\n    for the tests\n'),
            encoding='utf-8', newline='')
        result = places.run_place_edit_note(
            self.root, 'L-7c1a9f4e22', 'fictional town, for the tests', 'Rewritten.')
        self.assertEqual(result.exit_code, 0, [m.text for m in result.messages])
        self.assertEqual(self._parsed()['L-7c1a9f4e22']['notes'].strip(), 'Rewritten.')

    def test_edit_note_covering_two_whole_notes_asks_for_one(self) -> None:
        places.run_place_note(self.root, 'L-7c1a9f4e22', 'Second note.')
        second = self._parsed()['L-7c1a9f4e22']['notes'].strip().split('\n\n')[1]
        before = self.registry.read_bytes()
        result = places.run_place_edit_note(
            self.root, 'L-7c1a9f4e22', 'fictional town\n\n' + second, 'x')
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(result.data['reason'], 'entry-spans')
        self.assertIn('covers 2 notes', result.messages[0].text)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_edit_note_two_whole_paragraphs_never_loosely_match_a_third(self) -> None:
        self.registry.write_text(
            _SET_REGISTRY.replace(
                '  notes: fictional town\n',
                '  notes: |\n    Checked the census. Nothing found.\n\n'
                '    Checked the census.\n\n    Nothing found.\n'),
            encoding='utf-8', newline='')
        before = self.registry.read_bytes()
        result = places.run_place_edit_note(
            self.root, 'L-7c1a9f4e22', 'Checked the census.\n\nNothing found.', 'x')
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(result.data['reason'], 'entry-spans')
        self.assertEqual(self.registry.read_bytes(), before)

    def test_edit_note_true_miss_is_a_cli_not_found_message(self) -> None:
        result = places.run_place_edit_note(
            self.root, 'L-7c1a9f4e22', 'Never written anywhere.', 'x')
        self.assertEqual(result.data['reason'], 'entry-not-found')
        self.assertIn('not even in part', result.messages[0].text)
        self.assertNotIn('Reload the page', result.messages[0].text)

    def test_edit_note_help_says_whole_paragraph(self) -> None:
        import contextlib
        import io
        import fha
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            fha.main(['places', 'edit-note', '--help'])
        self.assertIn('whole paragraph', ' '.join(out.getvalue().split()))

    def test_edit_note_empty_replacement_refused(self) -> None:
        result = places.run_place_edit_note(
            self.root, 'L-7c1a9f4e22', 'fictional town', '  ')
        self.assertEqual(result.exit_code, 3)


if __name__ == '__main__':
    unittest.main()
