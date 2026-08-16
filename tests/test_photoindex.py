import argparse
import builtins
import contextlib
import inspect
import io
import os
import subprocess
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

from _lib import INDEX_SCHEMA_VERSION  # the synthetic index stamps must track the real schema

import index
import photoindex
from _lib import (
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    newest_person_record_mtime,
    parse_media_filename,
    photoindex_status,
    resolve_path,
)


def _copy_fixture(tmp: Path) -> Path:
    """Copy the photo fixture so tests can freely create cache files."""
    src = ROOT / 'tests' / 'fixtures' / 'photo-fixture'
    dst = tmp / 'photo-fixture'
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns('.cache'))
    return dst


class PhotoindexTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_run_exiftool = photoindex._run_exiftool

    def tearDown(self) -> None:
        photoindex._run_exiftool = self._orig_run_exiftool

    def test_absent_index_read_helpers_return_failure_exit_code(self) -> None:
        # Headless callers return Result.exit_code directly, so the read helpers
        # must report an absent photos.sqlite as EXIT_FAILURE, not a clean 0.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))   # no scan → photos.sqlite absent
            cfg = {'roots': {'photos': 'photos'}}
            self.assertEqual(photoindex.run_find(archive, cfg, person='P-de957bcda1').exit_code,
                             EXIT_FAILURE)
            self.assertEqual(photoindex.run_reconcile(archive, cfg).exit_code, EXIT_FAILURE)

    def test_missing_photos_root_returns_warning_exit_code(self) -> None:
        # A missing photos root is a warning (mirrors _cmd_scan/_cmd_reconcile),
        # not a hard failure.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'nonexistent-dir'}}
            scan = photoindex.run_scan(archive, cfg)
            self.assertFalse(scan['root_found'])
            self.assertEqual(scan.exit_code, EXIT_WARNINGS)

    def test_keyword_to_edtf_preserves_component_approximation(self) -> None:
        # SPEC §20 date-mapping table - a per-component best-guess marker must
        # land on the right component (1960!-05~ -> 1960-~05), not collapse to a
        # trailing '~' or get dropped when a confident component follows.
        self.assertEqual(photoindex._keyword_to_edtf('1942!-11!-25!'), '1942-11-25')
        self.assertEqual(photoindex._keyword_to_edtf('1960!-05!'), '1960-05')
        self.assertEqual(photoindex._keyword_to_edtf('1960!-05~'), '1960-~05')
        self.assertEqual(photoindex._keyword_to_edtf('1960!'), '1960')
        self.assertEqual(photoindex._keyword_to_edtf('1960~'), '1960~')
        self.assertEqual(photoindex._keyword_to_edtf('1960!-05!-12~'), '1960-05-~12')

    def test_normalize_subtree_arg_accepts_relative_alias_and_windows_forms(self) -> None:
        norm = photoindex._normalize_subtree_arg
        self.assertEqual(norm('Woodbury/1950s'), 'photos/Woodbury/1950s')
        self.assertEqual(norm('photos/Woodbury'), 'photos/Woodbury')
        self.assertEqual(norm('Woodbury\\1950s'), 'photos/Woodbury/1950s')
        self.assertEqual(norm('./Woodbury/'), 'photos/Woodbury')
        self.assertEqual(norm('photos'), 'photos')
        with self.assertRaises(ValueError):
            norm('/')
        with self.assertRaises(ValueError):
            norm('   ')

    def test_photos_ignore_excludes_a_subtree_and_prunes_stale_rows(self) -> None:
        # The motivating case for #35: a bulk export inside the photos root
        # must be excludable without narrowing roots: (which orphans filed
        # assets, #36). Adding the pattern later must also remove the rows an
        # earlier scan already catalogued.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            bulk = archive / 'photos' / 'Bulk Export'
            bulk.mkdir()
            (bulk / 'recent_0001.jpg').write_bytes(b'x')
            (bulk / 'recent_0002.jpg').write_bytes(b'x')

            photoindex._run_exiftool = lambda paths: [
                {'SourceFile': str(p)} for p in paths
            ]

            # First scan without the ignore: everything is catalogued.
            summary = photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})
            self.assertEqual(summary['total'], 6)

            # Second scan with the ignore: the subtree is skipped AND its
            # previously catalogued rows are swept out.
            cfg = {'roots': {'photos': 'photos'}, 'photos_ignore': ['Bulk Export']}
            summary = photoindex.run_scan(archive, cfg)
            self.assertEqual(summary['total'], 4)
            self.assertEqual(summary['removed'], 2)
            self.assertEqual(summary['ignore_patterns'], ['Bulk Export'])

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                leftover = conn.execute(
                    "SELECT COUNT(*) FROM photos WHERE path LIKE '%Bulk Export%'"
                ).fetchone()[0]
                self.assertEqual(leftover, 0)
            finally:
                conn.close()

            # A single string is accepted; garbage shapes are a clean error.
            summary = photoindex.run_scan(
                archive, {'roots': {'photos': 'photos'}, 'photos_ignore': 'Bulk Export'})
            self.assertEqual(summary['total'], 4)
            with self.assertRaisesRegex(RuntimeError, 'photos_ignore'):
                photoindex.run_scan(
                    archive, {'roots': {'photos': 'photos'}, 'photos_ignore': {'a': 1}})

    def test_find_and_triage_scope_by_under_and_not_under(self) -> None:
        # #35's query-time half: --under scopes to a subtree, --not-under
        # excludes one, on find and triage alike.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            old_dir = archive / 'photos' / 'Woodbury'
            new_dir = archive / 'photos' / 'Bulk Export'
            old_dir.mkdir()
            new_dir.mkdir()
            (old_dir / 'ancestor_portrait.jpg').write_bytes(b'x')
            (new_dir / 'recent_snap.jpg').write_bytes(b'x')

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                return [
                    {'SourceFile': str(p), 'Caption-Abstract': 'farm scene'}
                    for p in paths
                ]

            photoindex._run_exiftool = fake_exiftool
            cfg = {'roots': {'photos': 'photos'}}
            photoindex.run_scan(archive, cfg)

            hits = photoindex.run_find(archive, cfg, text='farm', under='Woodbury')
            self.assertEqual(
                [r['path'] for r in hits['rows']],
                ['photos/Woodbury/ancestor_portrait.jpg'])

            hits = photoindex.run_find(
                archive, cfg, text='farm', not_under='Bulk Export')
            self.assertNotIn(
                'photos/Bulk Export/recent_snap.jpg',
                [r['path'] for r in hits['rows']])
            self.assertIn(
                'photos/Woodbury/ancestor_portrait.jpg',
                [r['path'] for r in hits['rows']])

            # --under with no other filter is a valid filter on its own.
            hits = photoindex.run_find(archive, cfg, under='Woodbury')
            self.assertEqual(
                [r['path'] for r in hits['rows']],
                ['photos/Woodbury/ancestor_portrait.jpg'])

            res = photoindex.run_triage(archive, cfg, top=10, under='Woodbury')
            self.assertEqual(
                [c['path'] for c in res['candidates']],
                ['photos/Woodbury/ancestor_portrait.jpg'])
            res = photoindex.run_triage(archive, cfg, top=10, not_under='Bulk Export')
            self.assertNotIn(
                'photos/Bulk Export/recent_snap.jpg',
                [c['path'] for c in res['candidates']])

    def test_placeholder_keyword_resolves_against_exif_date(self) -> None:
        # SPEC §20: the DATE: keyword states precision only ('Y!M!D?'); the
        # value lives in EXIF DateTimeOriginal (exiftool's 'YYYY:MM:DD HH:MM:SS'
        # form, not ISO). Resolution stops at the first unconfirmed component
        # and keeps a '~' on the right one; time parts (H, then M=minutes, S)
        # are accepted and ignored.
        r = photoindex._placeholder_to_edtf
        self.assertEqual(r('Y!M!D!', '1942:11:25 10:00:00'), '1942-11-25')
        self.assertEqual(r('Y!M!D?', '1916:06:10 10:53:21'), '1916-06')
        self.assertEqual(r('Y!M?D?', '1916:06:10 10:53:21'), '1916')
        self.assertEqual(r('Y!', '1960:05:01 00:00:00'), '1960')
        self.assertEqual(r('Y~', '1960:01:01 00:00:00'), '1960~')
        self.assertEqual(r('Y!M~', '1960:05:01 00:00:00'), '1960-~05')
        self.assertEqual(r('Y!M~D?', '1942:03:15 00:00:00'), '1942-~03')
        self.assertEqual(r('Y!M!D~', '1960:05:12 00:00:00'), '1960-05-~12')
        self.assertEqual(r('Y!M!D?H!M!', '1916:06:10 10:53:21'), '1916-06')
        self.assertEqual(r('y!m!d!', '1942:11:25 10:00:00'), '1942-11-25')
        self.assertEqual(r('Y!M!D!', '1942-11-25T10:00:00'), '1942-11-25')
        self.assertIsNone(r('Y?', '1960:05:01 00:00:00'))
        self.assertIsNone(r('Y!M!D!', None))
        self.assertIsNone(r('Y!M!D!', 'not a date'))
        self.assertIsNone(r('1880!', '1960:05:01 00:00:00'))   # digit form: not a placeholder

    def test_omitted_precision_marker_is_unknown_not_confident(self) -> None:
        # SPEC §20 rule 1: '?' and an OMITTED marker both mean unknown, and
        # rule 2 says the forced full YYYY-MM-DD written into EXIF never
        # becomes truth. So an unmarked component must be dropped exactly like
        # a '?' one: 'Y!M' keeps only its confirmed year, and 'YMD' - which
        # confirms nothing - resolves to no date at all rather than promoting
        # a scanner clock to an exact archive date.
        r = photoindex._placeholder_to_edtf
        self.assertEqual(r('Y!M', '1916:06:10 10:53:21'), '1916')
        self.assertEqual(r('Y!MD', '1916:06:10 10:53:21'), '1916')
        self.assertEqual(r('Y!M!D', '1916:06:10 10:53:21'), '1916-06')
        self.assertEqual(r('Y!M~D', '1942:03:15 00:00:00'), '1942-~03')
        self.assertIsNone(r('YMD', '1942:11:25 10:00:00'))
        self.assertIsNone(r('YM', '1942:11:25 10:00:00'))
        self.assertIsNone(r('Y', '1942:11:25 10:00:00'))

    def test_scan_leaves_an_unconfirmed_placeholder_photo_undated(self) -> None:
        # End to end: a photo whose DATE: keyword affirms nothing must land in
        # the catalog undated, and must not answer --edtf on its EXIF year.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            rows = {
                'wedding_1902.jpg': {
                    'Keywords': ['DATE: YMD'],
                    'DateTimeOriginal': '2021:03:02 09:00:00',
                },
                'family_reunion.jpg': {
                    'Keywords': ['DATE: Y!M'],
                    'DateTimeOriginal': '1955:08:04 12:00:00',
                },
            }
            photoindex._run_exiftool = lambda paths: [
                {'SourceFile': str(p), **rows.get(p.name, {})} for p in paths
            ]
            cfg = {'roots': {'photos': 'photos'}}
            photoindex.run_scan(archive, cfg)

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                edtf_by_name = {
                    Path(path).name: edtf
                    for path, edtf in conn.execute('SELECT path, edtf FROM photos')
                }
            finally:
                conn.close()
            self.assertIsNone(edtf_by_name['wedding_1902.jpg'])
            self.assertEqual(edtf_by_name['family_reunion.jpg'], '1955')

            res = photoindex.run_find(archive, cfg, edtf='2021')
            self.assertEqual(res['rows'], [])
            res = photoindex.run_find(archive, cfg, edtf='1955')
            self.assertTrue(any('family_reunion' in r['path'] for r in res['rows']), res.data)

    def test_resolve_photo_edtf_needs_a_keyword_in_the_letter_form(self) -> None:
        # Two archive-owner decisions, in one function.
        #
        # 2026-08-15: the keyword's presence marks a date as REVIEWED. EXIF
        # alone never resolves - a scanner clock must not enter the same field
        # as human-confirmed fact, and a 1925 print dated 2021 is worse than
        # undated.
        #
        # 2026-08-16: the LETTER form is the only form read. SPEC §20 rule 1
        # defines the grammar as per-component precision letters and nothing
        # else, so a keyword carrying digits is not evidence of a date, however
        # readable it looks to a human.
        r = photoindex._resolve_photo_edtf
        self.assertIsNone(r(None, '2009:04:20 10:58:33'))
        self.assertIsNone(r('', '2009:04:20 10:58:33'))
        self.assertEqual(r('Y!M!D!', '2009:04:20 10:58:33'), '2009-04-20')
        self.assertIsNone(r('Y!M!D!', None))
        # Digit-bearing keywords: all outside the grammar, all undated.
        self.assertIsNone(r('1880', '2009:04:20 10:58:33'))
        self.assertIsNone(r('1880!', '2009:04:20 10:58:33'))
        self.assertIsNone(r('1942!-11!-25!', '2009:04:20 10:58:33'))
        self.assertIsNone(r('[..1900]', '2009:04:20 10:58:33'))
        self.assertIsNone(r('1880', None))

    def test_spec_20_keyword_table_resolves_row_by_row(self) -> None:
        # SPEC §20 rule 1's table is the whole contract for what a DATE:
        # keyword may say. Walk it row by row so a change to the resolver
        # cannot quietly drift away from the spec it implements. The
        # parenthesised dates in the table's left column are a gloss on the
        # EXIF value each row is being resolved against, not a second syntax.
        r = photoindex._resolve_photo_edtf
        self.assertEqual(r('Y!M!D!', '1942:11:25 10:00:00'), '1942-11-25')
        self.assertEqual(r('Y!M!', '1960:05:01 00:00:00'), '1960-05')
        self.assertEqual(r('Y!M~', '1960:05:01 00:00:00'), '1960-~05')
        self.assertEqual(r('Y!', '1960:05:01 00:00:00'), '1960')
        # The table calls 'Y!' the same as 'Y!M?D?' - spelled out, same answer.
        self.assertEqual(r('Y!M?D?', '1960:05:01 00:00:00'), '1960')
        # 'Y~' has two readings in the table, circa and decade; the archive
        # stores the circa one, which keeps the known year visible.
        self.assertEqual(r('Y~', '1960:05:01 00:00:00'), '1960~')

    def test_nonspec_date_keywords_are_counted_and_leave_photos_undated(self) -> None:
        # End to end for the 2026-08-16 rule: three keywords a human might
        # plausibly type (a bare year, the AI pipeline's digit-plus-marker
        # form, a raw EDTF string) all leave their photo undated - and the
        # scan says how many, so an owner who can read a date on the photo is
        # not left wondering why the catalog never got one.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            rows = {
                'portrait_1880.jpg': {
                    'Keywords': ['DATE: 1880'],
                    'DateTimeOriginal': '1880:01:01 00:00:00',
                },
                'portrait_1880-back.jpg': {
                    'Keywords': ['DATE: 1942!-11!-25!'],
                    'DateTimeOriginal': '1942:11:25 00:00:00',
                },
                'wedding_1902.jpg': {
                    'Keywords': ['DATE: [..1900]'],
                    'DateTimeOriginal': '1902:06:14 00:00:00',
                },
                # One spec-conformant photo, so the count is of the non-spec
                # keywords and not simply of every keyworded photo.
                'family_reunion.jpg': {
                    'Keywords': ['DATE: Y!'],
                    'DateTimeOriginal': '1955:08:04 12:00:00',
                },
            }
            photoindex._run_exiftool = lambda paths: [
                {'SourceFile': str(p), **rows.get(p.name, {})} for p in paths
            ]
            cfg = {'roots': {'photos': 'photos'}}
            summary = photoindex.run_scan(archive, cfg)

            self.assertEqual(summary['nonspec_date_keywords'], 3)
            self.assertEqual(summary['dated_groups'], 1)

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                edtf_by_name = {
                    Path(path).name: edtf
                    for path, edtf in conn.execute('SELECT path, edtf FROM photos')
                }
            finally:
                conn.close()
            self.assertIsNone(edtf_by_name['portrait_1880.jpg'])
            self.assertIsNone(edtf_by_name['portrait_1880-back.jpg'])
            self.assertIsNone(edtf_by_name['wedding_1902.jpg'])
            self.assertEqual(edtf_by_name['family_reunion.jpg'], '1955')

            # And nothing answers a date query on the year those keywords name.
            self.assertEqual(photoindex.run_find(archive, cfg, edtf='1880')['rows'], [])
            self.assertEqual(photoindex.run_find(archive, cfg, edtf='1942')['rows'], [])

    def test_cmd_scan_explains_nonspec_date_keywords_only_when_there_are_some(self) -> None:
        # The count is only useful if the owner is told what it means and what
        # to do about it, and only when it is non-zero - a note on every clean
        # scan is noise that trains him to skip the notes that matter.
        def scan_output(archive: Path) -> str:
            args = type('Args', (), {'root': str(archive), 'full': False})()
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                photoindex._cmd_scan(args)
            return out.getvalue()

        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            photoindex._run_exiftool = lambda paths: [
                {'SourceFile': str(p), 'Keywords': ['DATE: 1880!']} for p in paths
            ]
            text = scan_output(archive)
            self.assertIn('does not read a date from', text)
            self.assertIn("'DATE: Y!M!D!'", text)      # the form he should use
            self.assertIn('fha photoindex', text)      # and the next step

        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            photoindex._run_exiftool = lambda paths: [
                {'SourceFile': str(p), 'Keywords': ['DATE: Y!'],
                 'DateTimeOriginal': '1880:01:01 00:00:00'} for p in paths
            ]
            text = scan_output(archive)
            self.assertNotIn('does not read a date from', text)

    def test_scan_resolves_keyworded_dates_and_leaves_exif_only_undated(self) -> None:
        # The catalog's date features all read `edtf`. Keyworded photos must
        # populate it from pattern + EXIF (#40); EXIF-only photos stay NULL by
        # design; and the group report distinguishes 'nothing to compare'
        # (NULL) from 'compared, no conflict' (0).
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                rows = {
                    # EXIF only, no keyword -> undated by design.
                    'family_reunion.jpg': {'DateTimeOriginal': '2009:04:20 10:58:33'},
                    # Precision keyword + EXIF value -> resolved.
                    'wedding_1902.jpg': {
                        'Keywords': ['DATE: Y!M!D?'],
                        'DateTimeOriginal': '1902:06:14 00:00:00',
                    },
                    # Front keyworded, back EXIF-only: the group resolves to
                    # the front's date; with one dated variant there is
                    # nothing to compare, so conflict is unknown (NULL).
                    'portrait_1880.jpg': {
                        'Keywords': ['DATE: Y~'],
                        'DateTimeOriginal': '1880:01:01 00:00:00',
                    },
                    'portrait_1880-back.jpg': {'DateTimeOriginal': '2010:05:06 07:08:09'},
                }
                return [{'SourceFile': str(p), **rows.get(p.name, {})} for p in paths]

            photoindex._run_exiftool = fake_exiftool
            summary = photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})
            self.assertEqual(summary['dated_groups'], 2)
            self.assertEqual(summary['conflicts'], 0)

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                edtf_by_name = {
                    Path(path).name: edtf
                    for path, edtf in conn.execute('SELECT path, edtf FROM photos')
                }
                self.assertIsNone(edtf_by_name['family_reunion.jpg'])
                self.assertEqual(edtf_by_name['wedding_1902.jpg'], '1902-06')
                self.assertEqual(edtf_by_name['portrait_1880.jpg'], '1880~')
                self.assertIsNone(edtf_by_name['portrait_1880-back.jpg'])

                resolved, conflict = conn.execute(
                    "SELECT edtf_resolved, date_conflict FROM photo_groups "
                    "WHERE group_id LIKE 'STEM:%portrait_1880%'"
                ).fetchone()
                self.assertEqual(resolved, '1880~')
                self.assertIsNone(conflict)
            finally:
                conn.close()

            # find --edtf now matches a keyworded photo - the headline symptom
            # of #40 was that it could never match anything.
            res = photoindex.run_find(
                archive, {'roots': {'photos': 'photos'}}, edtf='1902')
            self.assertTrue(
                any('wedding_1902' in r['path'] for r in res['rows']), res.data)
            res = photoindex.run_find(
                archive, {'roots': {'photos': 'photos'}}, edtf='2009')
            self.assertEqual(res['rows'], [])

    def test_scan_backfills_edtf_for_rows_scraped_before_the_fix(self) -> None:
        # A catalog scanned before #40 holds date_pattern + exif_date but edtf
        # NULL on every keyworded row, and an incremental scan never revisits
        # unchanged files - the backfill must heal them from the stored
        # columns, no exiftool needed.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                return [
                    {'SourceFile': str(p), 'Keywords': ['DATE: Y!M!D!'],
                     'DateTimeOriginal': '1998:07:15 12:00:00'}
                    for p in paths
                ]

            photoindex._run_exiftool = fake_exiftool
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            db = archive / '.cache' / 'photos.sqlite'
            conn = sqlite3.connect(db)
            conn.execute('UPDATE photos SET edtf=NULL')
            conn.execute('UPDATE photo_groups SET edtf_resolved=NULL')
            conn.commit()
            conn.close()

            # Nothing on disk changed, so nothing is re-scraped - the heal
            # must come from the backfill, not from exiftool.
            def no_exiftool(paths: list[Path]) -> list[dict]:
                raise AssertionError('unchanged files must not be re-scraped')

            photoindex._run_exiftool = no_exiftool
            summary = photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})
            self.assertEqual(summary['scraped'], 0)
            self.assertEqual(summary['dated_groups'], summary['groups'])

            conn = sqlite3.connect(db)
            try:
                undated = conn.execute(
                    'SELECT COUNT(*) FROM photos WHERE edtf IS NULL').fetchone()[0]
                self.assertEqual(undated, 0)
            finally:
                conn.close()

    def test_scan_groups_variants_flags_date_conflict_and_indexes_pid_keyword(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                rows = {
                    # Front says 1880, back says 1881 - one physical photo
                    # whose two scans disagree, which is what date_conflict is
                    # for. The year is confident on both (SPEC §20 'Y!'); the
                    # value comes from each scan's own EXIF date.
                    'portrait_1880.jpg': {
                        'Keywords': ['DATE: Y!'],
                        'DateTimeOriginal': '1880:01:01 00:00:00',
                        'Title': 'Portrait front',
                    },
                    'portrait_1880-back.jpg': {
                        'Keywords': ['DATE: Y!'],
                        'DateTimeOriginal': '1881:01:01 00:00:00',
                        'Title': 'Portrait back',
                    },
                    'wedding_1902.jpg': {
                        'Keywords': ['SOURCE: S-123456789a', 'DATE: Y!'],
                        'DateTimeOriginal': '1902:01:01 00:00:00',
                        'Caption-Abstract': 'Wedding party',
                    },
                    'family_reunion.jpg': {
                        'Keywords': ['P-de957bcda1'],
                        'Caption-Abstract': 'Family reunion',
                        'RegionInfo': {
                            'RegionList': [
                                {
                                    'Name': 'Grandma',
                                    'Type': 'Face',
                                    'Area': {'X': 0.1, 'Y': 0.2, 'W': 0.3, 'H': 0.4},
                                },
                            ],
                        },
                    },
                }
                return [
                    {'SourceFile': str(p), **rows[p.name]}
                    for p in paths
                ]

            photoindex._run_exiftool = fake_exiftool

            summary = photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            self.assertEqual(summary['total'], 4)
            self.assertEqual(summary['scraped'], 4)
            self.assertEqual(summary['groups'], 3)
            self.assertEqual(summary['conflicts'], 1)

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                all_paths = [row[0] for row in conn.execute('SELECT path FROM photos')]
                self.assertTrue(all_paths)
                for stored_path in all_paths:
                    self.assertTrue(stored_path.startswith('photos/'), stored_path)
                    self.assertNotIn('\\', stored_path)
                    self.assertFalse(Path(stored_path).is_absolute(), stored_path)

                portrait_rows = conn.execute(
                    "SELECT path, is_primary, variant_role FROM photos "
                    "WHERE path LIKE '%portrait_1880%' ORDER BY path"
                ).fetchall()
                self.assertEqual(len(portrait_rows), 2)
                self.assertEqual(
                    [row[2] for row in portrait_rows],
                    ['back', None],
                )
                self.assertEqual(sum(row[1] for row in portrait_rows), 1)

                conflicts = conn.execute(
                    'SELECT COUNT(*) FROM photo_groups WHERE date_conflict=1'
                ).fetchone()[0]
                self.assertEqual(conflicts, 1)

                people = conn.execute(
                    'SELECT person_ref, via FROM photo_people ORDER BY person_ref'
                ).fetchall()
                self.assertEqual(people, [('p-de957bcda1', 'pid-keyword')])

                fts_rows = conn.execute('SELECT COUNT(*) FROM photo_fts').fetchone()[0]
                self.assertEqual(fts_rows, 4)

                regions = conn.execute(
                    'SELECT name, region_type, area_json FROM photo_face_regions'
                ).fetchall()
                self.assertEqual(
                    regions,
                    [('Grandma', 'Face', '{"H":0.4,"W":0.3,"X":0.1,"Y":0.2}')],
                )
            finally:
                conn.close()

    def test_negative_with_copy_letter_is_stored_at_stem_level(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            cache_dir = Path(d) / '.cache'
            conn, _needs_face_backfill, _rebuilt_reason = photoindex._get_db(cache_dir)
            try:
                for path in ('portrait_1880b-negative.jpg', 'portrait_1880-back.jpg'):
                    conn.execute(
                        'INSERT INTO photos(path, mtime, size, group_id, is_primary, '
                        'variant_copy, variant_role) VALUES (?,0,0,NULL,0,NULL,NULL)',
                        (path,),
                    )
                photoindex._group_photos(conn)
                rows = {
                    path: (variant_copy, variant_role)
                    for path, variant_copy, variant_role in conn.execute(
                        'SELECT path, variant_copy, variant_role FROM photos'
                    )
                }
                negative_copy, negative_role = rows['portrait_1880b-negative.jpg']
                self.assertIsNone(negative_copy)
                self.assertEqual(negative_role, 'negative')
            finally:
                conn.close()

    def test_source_tagged_file_groups_with_untagged_stem_sibling(self) -> None:
        # Only the front carries a SOURCE: S-id; the back is an untagged stem
        # sibling in the same directory. They must land in one group, not
        # split into a SOURCE: group and a separate STEM: group.
        with tempfile.TemporaryDirectory() as d:
            cache_dir = Path(d) / '.cache'
            conn, _needs_face_backfill, _rebuilt_reason = photoindex._get_db(cache_dir)
            try:
                conn.execute(
                    'INSERT INTO photos(path, mtime, size, source_id, group_id, '
                    'is_primary, variant_copy, variant_role) '
                    "VALUES ('wedding_1902.jpg',0,0,'S-123456789a',NULL,0,NULL,NULL)"
                )
                conn.execute(
                    'INSERT INTO photos(path, mtime, size, source_id, group_id, '
                    'is_primary, variant_copy, variant_role) '
                    "VALUES ('wedding_1902-back.jpg',0,0,NULL,NULL,0,NULL,NULL)"
                )
                photoindex._group_photos(conn)
                rows = conn.execute(
                    'SELECT path, group_id FROM photos ORDER BY path'
                ).fetchall()
                group_ids = {path: group_id for path, group_id in rows}
                self.assertEqual(
                    group_ids['wedding_1902-back.jpg'], group_ids['wedding_1902.jpg']
                )
                self.assertEqual(group_ids['wedding_1902.jpg'], 'SOURCE:S-123456789a')
            finally:
                conn.close()

    def test_person_match_on_one_variant_propagates_to_whole_group(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                rows = {
                    'portrait_1880.jpg': {},
                    'portrait_1880-back.jpg': {'Keywords': ['P-de957bcda1']},
                    'wedding_1902.jpg': {},
                    'family_reunion.jpg': {},
                }
                return [
                    {'SourceFile': str(p), **rows[p.name]}
                    for p in paths
                ]

            photoindex._run_exiftool = fake_exiftool
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                people = conn.execute(
                    "SELECT path, person_ref, via FROM photo_people "
                    "WHERE path LIKE '%portrait_1880%' ORDER BY path"
                ).fetchall()
                self.assertEqual(
                    people,
                    [
                        ('photos/portrait_1880-back.jpg', 'p-de957bcda1', 'pid-keyword'),
                        ('photos/portrait_1880.jpg', 'p-de957bcda1', 'pid-keyword'),
                    ],
                )
            finally:
                conn.close()

    def test_media_filename_parser_covers_documented_suffixes(self) -> None:
        back = parse_media_filename('portrait_1880_back')
        self.assertEqual(back.base_id, 'portrait_1880')
        self.assertEqual(back.part_kind, 'back')

        bw = parse_media_filename('portrait_1880-bw-crop')
        self.assertEqual(bw.base_id, 'portrait_1880')
        self.assertEqual(bw.part_kind, 'bw')
        self.assertTrue(bw.is_crop)

        freeform = parse_media_filename('portrait_1880b-restored')
        self.assertEqual(freeform.base_id, 'portrait_1880')
        self.assertEqual(freeform.variant_id, 'b')
        self.assertEqual(freeform.part_kind, 'freeform')
        self.assertEqual(freeform.freeform_role, 'restored')

        dash_variant = parse_media_filename('portrait_1880-b')
        self.assertEqual(dash_variant.base_id, 'portrait_1880')
        self.assertEqual(dash_variant.variant_id, 'b')
        self.assertIsNone(dash_variant.freeform_role)

        dash_variant_crop = parse_media_filename('portrait_1880-b-crop')
        self.assertEqual(dash_variant_crop.base_id, 'portrait_1880')
        self.assertEqual(dash_variant_crop.variant_id, 'b')
        self.assertTrue(dash_variant_crop.is_crop)
        self.assertIsNone(dash_variant_crop.freeform_role)

    def test_underscore_letter_suffix_is_not_a_copy_variant(self) -> None:
        # TOOLING §6 only documents '-b' (dash) or a bare letter right after a
        # digit ('034b') as copy-variant grammar - 'scan_a'/'scan_b' must stay
        # distinct base_ids instead of collapsing into variants of 'scan'.
        scan_a = parse_media_filename('scan_a')
        self.assertEqual(scan_a.base_id, 'scan_a')
        self.assertIsNone(scan_a.variant_id)

        scan_b = parse_media_filename('scan_b')
        self.assertEqual(scan_b.base_id, 'scan_b')
        self.assertIsNone(scan_b.variant_id)

    def test_newest_person_record_mtime_ignores_companion_files(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = Path(d)
            people_dir = archive / 'people'
            people_dir.mkdir()
            profile = people_dir / 'hartley__thomas_edward_P-de957bcda1.md'
            profile.write_text('---\nid: P-de957bcda1\n---\n', encoding='utf-8')
            os.utime(profile, (1, 1))

            baseline = newest_person_record_mtime(archive)
            self.assertEqual(baseline, 1.0)

            for companion_path in (
                people_dir / 'hartley__thomas_edward_timeline_P-de957bcda1.md',
                people_dir / 'hartley__thomas_edward_research_P-de957bcda1.md',
                people_dir / 'hartley__thomas_edward_sources-index_P-de957bcda1.md',
                people_dir / 'hartley__thomas_edward_draft-queue_P-de957bcda1.md',
                people_dir / 'sources-index.md',
            ):
                companion_path.write_text('GENERATED\n', encoding='utf-8')
                os.utime(companion_path, (baseline + 100, baseline + 100))

            # Touching only generated companion files must not bump the
            # profile-record freshness watermark.
            self.assertEqual(newest_person_record_mtime(archive), baseline)

    def test_photoindex_status_is_stale_after_person_index_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            fha_config = {'roots': {'photos': 'photos'}}
            photoindex._run_exiftool = lambda paths: [
                {'SourceFile': str(p)} for p in paths
            ]
            photoindex.run_scan(archive, fha_config)

            status, _lag = photoindex_status(archive, fha_config)
            self.assertEqual(status, 'fresh')

            # Simulate a person-record edit that rebuilds index.sqlite after
            # the photoindex scan: photo_people would now be derived from
            # stale data until the next `fha photoindex` run.
            cache = archive / '.cache'
            index_db = cache / 'index.sqlite'
            sqlite3.connect(index_db).close()
            photos_mtime = (cache / 'photos.sqlite').stat().st_mtime
            os.utime(index_db, (photos_mtime + 10, photos_mtime + 10))

            status, lag = photoindex_status(archive, fha_config)
            self.assertEqual(status, 'stale')
            self.assertGreater(lag, 0)

    def test_photoindex_status_is_stale_after_source_people_edit(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            fha_config = {'roots': {'photos': 'photos'}}
            photoindex._run_exiftool = lambda paths: [
                {'SourceFile': str(p)} for p in paths
            ]
            photoindex.run_scan(archive, fha_config)

            status, _lag = photoindex_status(archive, fha_config)
            self.assertEqual(status, 'fresh')

            sources_dir = archive / 'sources' / 'photos'
            sources_dir.mkdir(parents=True, exist_ok=True)
            source_record = sources_dir / 'wedding_1902_S-123456789a.md'
            source_record.write_text(
                '---\ntitle: Wedding 1902\npeople:\n  - P-de957bcda1\n---\n',
                encoding='utf-8',
            )
            photos_mtime = (archive / '.cache' / 'photos.sqlite').stat().st_mtime
            os.utime(source_record, (photos_mtime + 10, photos_mtime + 10))

            status, lag = photoindex_status(archive, fha_config)
            self.assertEqual(status, 'stale')
            self.assertGreater(lag, 0)

    def test_row_to_photo_falls_back_to_xmp_description_for_caption(self) -> None:
        with_caption = photoindex._row_to_photo(
            {'Caption-Abstract': 'IPTC caption', 'Description': 'XMP description'}, 0.0, 0,
        )
        self.assertEqual(with_caption['caption'], 'IPTC caption')

        description_only = photoindex._row_to_photo({'Description': 'XMP description'}, 0.0, 0)
        self.assertEqual(description_only['caption'], 'XMP description')

        neither = photoindex._row_to_photo({}, 0.0, 0)
        self.assertIsNone(neither['caption'])

    def test_full_rescan_matches_incremental_state(self) -> None:
        """`--full` bypasses the mtime/size skip but must converge to the same
        cache state as an incremental scan that already scraped everything."""
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            fha_config = {'roots': {'photos': 'photos'}}
            calls = {'count': 0}

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                calls['count'] += len(paths)
                rows = {
                    'portrait_1880.jpg': {
                        'Keywords': ['DATE: Y!'],
                        'DateTimeOriginal': '1880:01:01 00:00:00',
                    },
                    'portrait_1880-back.jpg': {
                        'Keywords': ['DATE: Y!'],
                        'DateTimeOriginal': '1881:01:01 00:00:00',
                    },
                    'wedding_1902.jpg': {},
                    'family_reunion.jpg': {'Caption-Abstract': 'reunion photo'},
                }
                return [{'SourceFile': str(p), **rows.get(p.name, {})} for p in paths]

            photoindex._run_exiftool = fake_exiftool
            photoindex.run_scan(archive, fha_config)
            first_scrape_calls = calls['count']
            self.assertGreater(first_scrape_calls, 0)

            def snapshot() -> dict:
                conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
                try:
                    tables = (
                        'photos', 'photo_groups', 'photo_keywords',
                        'photo_face_regions', 'photo_people', 'photo_fts',
                    )
                    return {
                        t: sorted(conn.execute(f'SELECT * FROM {t}').fetchall())
                        for t in tables
                    }
                finally:
                    conn.close()

            incremental_state = snapshot()

            # Nothing changed on disk: an incremental rescan must not re-scrape.
            calls['count'] = 0
            photoindex.run_scan(archive, fha_config)
            self.assertEqual(calls['count'], 0)
            self.assertEqual(snapshot(), incremental_state)

            # `--full` rescans every file regardless, and must land on the same state.
            photoindex.run_scan(archive, fha_config, full=True)
            self.assertEqual(calls['count'], first_scrape_calls)
            self.assertEqual(snapshot(), incremental_state)

    def test_grouping_stem_keeps_freeform_suffix_distinct(self) -> None:
        family = parse_media_filename('smith-family')
        house = parse_media_filename('smith-house')
        self.assertEqual(family.base_id, house.base_id)
        self.assertNotEqual(photoindex._grouping_stem(family), photoindex._grouping_stem(house))

        back = parse_media_filename('portrait_1880_back')
        self.assertEqual(photoindex._grouping_stem(back), 'portrait_1880')

    def test_person_resolution_dedupes_by_confidence_order(self) -> None:
        rows = photoindex._resolve_photo_people(
            ['P-AAAAAAAAAA'],
            [('Grandma', 'Face')],
            {'Grandma': {'p-aaaaaaaaaa'}},
            {'Grandma': {'p-aaaaaaaaaa'}},
        )
        self.assertEqual(rows, [('p-aaaaaaaaaa', 'pid-keyword')])

    def test_ambiguous_face_tag_does_not_fall_back_to_name_match(self) -> None:
        rows = photoindex._resolve_photo_people(
            [],
            [('Grandma', 'Face')],
            {'Grandma': {'p-aaaaaaaaaa', 'p-bbbbbbbbbb'}},
            {'Grandma': {'p-aaaaaaaaaa'}},
        )
        self.assertEqual(rows, [])

    def test_unique_face_tag_does_not_also_fall_back_to_name_match(self) -> None:
        # 'Jack' resolves uniquely via person_face_tags to P-a. Face-tag
        # resolution is the higher-confidence tier, so name/name_variant
        # matching must not also run for the same region name and attach an
        # unrelated person (P-b) to the same face.
        rows = photoindex._resolve_photo_people(
            [],
            [('Jack', 'Face')],
            {'Jack': {'p-aaaaaaaaaa'}},
            {'Jack': {'p-bbbbbbbbbb'}},
        )
        self.assertEqual(rows, [('p-aaaaaaaaaa', 'face-tag')])

    def test_stale_index_is_not_used_for_weak_face_or_name_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cache = archive / '.cache'
            cache.mkdir(exist_ok=True)
            index_db = cache / 'index.sqlite'
            conn = sqlite3.connect(index_db)
            try:
                conn.executescript(
                    f"""
                    PRAGMA user_version={INDEX_SCHEMA_VERSION};
                    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    INSERT INTO meta(key, value) VALUES ('schema_version', '{INDEX_SCHEMA_VERSION}');
                    CREATE TABLE persons(id TEXT, name TEXT);
                    CREATE TABLE person_face_tags(person_id TEXT, tag TEXT);
                    CREATE TABLE person_variants(person_id TEXT, variant TEXT);
                    INSERT INTO persons(id, name) VALUES ('P-aaaaaaaaaa', 'Grandma');
                    INSERT INTO person_face_tags(person_id, tag) VALUES ('P-aaaaaaaaaa', 'Grandma');
                    """
                )
                conn.commit()
            finally:
                conn.close()

            people_dir = archive / 'people'
            people_dir.mkdir(exist_ok=True)
            person_file = people_dir / 'grandma__example_P-aaaaaaaaaa.md'
            person_file.write_text('---\nid: P-aaaaaaaaaa\nname: Grandma\n---\n', encoding='utf-8')
            os.utime(index_db, (1, 1))

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                return [
                    {
                        'SourceFile': str(p),
                        'RegionInfo': {
                            'RegionList': [{'Name': 'Grandma', 'Type': 'Face'}],
                        } if p.name == 'family_reunion.jpg' else {},
                    }
                    for p in paths
                ]

            photoindex._run_exiftool = fake_exiftool

            summary = photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            self.assertEqual(summary['scraped'], 4)
            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                people = conn.execute('SELECT person_ref, via FROM photo_people').fetchall()
                self.assertEqual(people, [])
            finally:
                conn.close()

    def test_newer_fresh_index_refreshes_weak_person_resolution_from_cached_regions(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            people_dir = archive / 'people'
            people_dir.mkdir(exist_ok=True)
            person_file = people_dir / 'grandma__example_P-aaaaaaaaaa.md'
            person_file.write_text('---\nid: P-aaaaaaaaaa\nname: Grandma\n---\n', encoding='utf-8')

            calls: list[int] = []

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                calls.append(len(paths))
                return [
                    {
                        'SourceFile': str(p),
                        'RegionInfo': {
                            'RegionList': [{'Name': 'Grandma', 'Type': 'Face'}],
                        } if p.name == 'family_reunion.jpg' else {},
                    }
                    for p in paths
                ]

            photoindex._run_exiftool = fake_exiftool
            first_summary = photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})
            self.assertEqual(first_summary['scraped'], 4)

            cache = archive / '.cache'
            index_db = cache / 'index.sqlite'
            conn = sqlite3.connect(index_db)
            try:
                conn.executescript(
                    f"""
                    PRAGMA user_version={INDEX_SCHEMA_VERSION};
                    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    INSERT INTO meta(key, value) VALUES ('schema_version', '{INDEX_SCHEMA_VERSION}');
                    CREATE TABLE persons(id TEXT, name TEXT);
                    CREATE TABLE person_face_tags(person_id TEXT, tag TEXT);
                    CREATE TABLE person_variants(person_id TEXT, variant TEXT);
                    INSERT INTO persons(id, name) VALUES ('p-aaaaaaaaaa', 'Grandma');
                    INSERT INTO person_face_tags(person_id, tag) VALUES ('p-aaaaaaaaaa', 'Grandma');
                    """
                )
                conn.commit()
            finally:
                conn.close()

            os.utime(index_db, None)

            second_summary = photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            self.assertEqual(calls, [4])
            self.assertEqual(second_summary['scraped'], 0)
            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                people = conn.execute(
                    'SELECT person_ref, via FROM photo_people ORDER BY person_ref'
                ).fetchall()
                self.assertEqual(people, [('p-aaaaaaaaaa', 'face-tag')])
            finally:
                conn.close()

    def test_tag_person_rebuild_preserves_other_photos_weak_matches_when_index_goes_stale(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            people_dir = archive / 'people'
            people_dir.mkdir(exist_ok=True)
            (people_dir / 'grandma__example_P-aaaaaaaaaa.md').write_text(
                '---\nid: P-aaaaaaaaaa\nname: Grandma\n---\n', encoding='utf-8',
            )
            (people_dir / 'other__example_P-bbbbbbbbbb.md').write_text(
                '---\nid: P-bbbbbbbbbb\n---\n', encoding='utf-8',
            )

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                return [
                    {
                        'SourceFile': str(p),
                        'RegionInfo': {
                            'RegionList': [{'Name': 'Grandma', 'Type': 'Face'}],
                        } if p.name == 'family_reunion.jpg' else {},
                    }
                    for p in paths
                ]

            photoindex._run_exiftool = fake_exiftool
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            cache = archive / '.cache'
            index_db = cache / 'index.sqlite'
            conn = sqlite3.connect(index_db)
            try:
                conn.executescript(
                    f"""
                    PRAGMA user_version={INDEX_SCHEMA_VERSION};
                    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    INSERT INTO meta(key, value) VALUES ('schema_version', '{INDEX_SCHEMA_VERSION}');
                    CREATE TABLE persons(id TEXT, name TEXT);
                    CREATE TABLE person_face_tags(person_id TEXT, tag TEXT);
                    CREATE TABLE person_variants(person_id TEXT, variant TEXT);
                    INSERT INTO persons(id, name) VALUES ('p-aaaaaaaaaa', 'Grandma');
                    INSERT INTO person_face_tags(person_id, tag) VALUES ('p-aaaaaaaaaa', 'Grandma');
                    """
                )
                conn.commit()
            finally:
                conn.close()
            os.utime(index_db, None)

            # Fresh index -> family_reunion.jpg picks up the weak face-tag match.
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})
            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                people = conn.execute(
                    'SELECT person_ref, via FROM photo_people ORDER BY person_ref'
                ).fetchall()
                self.assertEqual(people, [('p-aaaaaaaaaa', 'face-tag')])
            finally:
                conn.close()

            # A newer person record makes index.sqlite stale again.
            (people_dir / 'third__example_P-cccccccccc.md').write_text(
                '---\nid: P-cccccccccc\n---\n', encoding='utf-8',
            )
            index_mtime = index_db.stat().st_mtime
            os.utime(
                people_dir / 'third__example_P-cccccccccc.md',
                (index_mtime + 10, index_mtime + 10),
            )

            # Tagging an unrelated photo (portrait_1880.jpg) with a different
            # P-id triggers apply_tag_person's _rebuild_photo_people while the
            # index is stale. That must not wipe out family_reunion.jpg's
            # already-screened weak match for Grandma - tag-person bulk work
            # is incremental, and most of the archive starts out resolved only
            # via these weaker tiers.
            photoindex._run_exiftool_write = lambda paths, kw: {p: None for p in paths}
            result = photoindex.apply_tag_person(
                archive, {'roots': {'photos': 'photos'}}, 'p-bbbbbbbbbb',
                ['photos/portrait_1880.jpg'],
            )
            self.assertEqual(result['tagged'], ['photos/portrait_1880.jpg'])

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                people = conn.execute(
                    "SELECT path, person_ref, via FROM photo_people WHERE person_ref='p-aaaaaaaaaa'"
                ).fetchall()
                self.assertEqual(people, [('photos/family_reunion.jpg', 'p-aaaaaaaaaa', 'face-tag')])
                tagged = conn.execute(
                    "SELECT path, via FROM photo_people WHERE person_ref='p-bbbbbbbbbb' "
                    "AND path='photos/portrait_1880.jpg'"
                ).fetchone()
                self.assertEqual(tagged, ('photos/portrait_1880.jpg', 'pid-keyword'))
            finally:
                conn.close()

    def test_unrelated_record_edit_does_not_drop_weak_person_matches(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            people_dir = archive / 'people'
            people_dir.mkdir(exist_ok=True)
            person_file = people_dir / 'grandma__example_P-aaaaaaaaaa.md'
            person_file.write_text('---\nid: P-aaaaaaaaaa\nname: Grandma\n---\n', encoding='utf-8')

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                return [
                    {
                        'SourceFile': str(p),
                        'RegionInfo': {
                            'RegionList': [{'Name': 'Grandma', 'Type': 'Face'}],
                        } if p.name == 'family_reunion.jpg' else {},
                    }
                    for p in paths
                ]

            photoindex._run_exiftool = fake_exiftool
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            cache = archive / '.cache'
            index_db = cache / 'index.sqlite'
            conn = sqlite3.connect(index_db)
            try:
                conn.executescript(
                    f"""
                    PRAGMA user_version={INDEX_SCHEMA_VERSION};
                    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    INSERT INTO meta(key, value) VALUES ('schema_version', '{INDEX_SCHEMA_VERSION}');
                    CREATE TABLE persons(id TEXT, name TEXT);
                    CREATE TABLE person_face_tags(person_id TEXT, tag TEXT);
                    CREATE TABLE person_variants(person_id TEXT, variant TEXT);
                    INSERT INTO persons(id, name) VALUES ('p-aaaaaaaaaa', 'Grandma');
                    INSERT INTO person_face_tags(person_id, tag) VALUES ('p-aaaaaaaaaa', 'Grandma');
                    """
                )
                conn.commit()
            finally:
                conn.close()
            os.utime(index_db, None)

            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            # Editing an unrelated record (not a person record) must not make
            # index.sqlite look stale and wipe the weak face-tag match.
            sources_dir = archive / 'sources'
            sources_dir.mkdir(exist_ok=True)
            source_file = sources_dir / 'unrelated__example_S-bbbbbbbbbb.md'
            source_file.write_text('---\nid: S-bbbbbbbbbb\n---\n', encoding='utf-8')
            index_mtime = index_db.stat().st_mtime
            os.utime(source_file, (index_mtime + 10, index_mtime + 10))

            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                people = conn.execute(
                    'SELECT person_ref, via FROM photo_people ORDER BY person_ref'
                ).fetchall()
                self.assertEqual(people, [('p-aaaaaaaaaa', 'face-tag')])
            finally:
                conn.close()

    def test_old_schema_photos_sqlite_is_recreated(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cache = archive / '.cache'
            cache.mkdir()
            db_path = cache / 'photos.sqlite'
            conn = sqlite3.connect(db_path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE photos(path TEXT PRIMARY KEY, mtime REAL, size INTEGER);
                    CREATE VIRTUAL TABLE photo_fts USING fts5(path, title, caption, user_comment, keywords);
                    INSERT INTO photos(path, mtime, size) VALUES ('stale.jpg', 1, 1);
                    """
                )
                conn.commit()
            finally:
                conn.close()

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                return [{'SourceFile': str(p)} for p in paths]

            photoindex._run_exiftool = fake_exiftool
            summary = photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            self.assertEqual(summary['scraped'], 4)
            conn = sqlite3.connect(db_path)
            try:
                columns = {
                    row[1] for row in conn.execute('PRAGMA table_info(photos)').fetchall()
                }
                self.assertIn('title', columns)
                self.assertIsNotNone(
                    conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='photo_face_regions'"
                    ).fetchone()
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM photos WHERE path='stale.jpg'").fetchone()[0],
                    0,
                )
            finally:
                conn.close()

    def test_photo_fts_with_wrong_columns_is_recreated(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cache = archive / '.cache'
            cache.mkdir()
            db_path = cache / 'photos.sqlite'
            conn = sqlite3.connect(db_path)
            try:
                # A queryable but schema-incompatible photo_fts (missing 'keywords')
                # must not be reused: the scanner inserts into all four FTS columns.
                conn.executescript(
                    """
                    CREATE TABLE photos(path TEXT PRIMARY KEY, mtime REAL, size INTEGER,
                      title TEXT, caption TEXT, user_comment TEXT, exif_date TEXT,
                      date_pattern TEXT, edtf TEXT, sublocation TEXT, city TEXT,
                      state TEXT, country TEXT, gps_lat REAL, gps_lon REAL,
                      source_id TEXT, group_id TEXT, is_primary INTEGER, variant_copy TEXT,
                      variant_role TEXT);
                    CREATE TABLE photo_groups(group_id TEXT PRIMARY KEY, primary_path TEXT,
                      edtf_resolved TEXT, date_conflict INTEGER, file_count INTEGER);
                    CREATE TABLE photo_keywords(path TEXT, keyword TEXT);
                    CREATE TABLE photo_face_regions(path TEXT, name TEXT, region_type TEXT, area_json TEXT);
                    CREATE TABLE photo_people(path TEXT, person_ref TEXT, via TEXT);
                    CREATE VIRTUAL TABLE photo_fts USING fts5(path, title, caption);
                    """
                )
                conn.commit()
            finally:
                conn.close()

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                return [{'SourceFile': str(p)} for p in paths]

            photoindex._run_exiftool = fake_exiftool
            summary = photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            self.assertEqual(summary['scraped'], 4)
            conn = sqlite3.connect(db_path)
            try:
                columns = {
                    row[1] for row in conn.execute('PRAGMA table_info(photo_fts)').fetchall()
                }
                self.assertIn('keywords', columns)
            finally:
                conn.close()

    def test_run_exiftool_fails_on_a_genuine_exiftool_error(self) -> None:
        # rc != 0 with no JSON and no per-file error lines = exiftool itself
        # broke (bad option, broken install): fatal, with its stderr.
        class FakeProc:
            returncode = 1
            stdout = b''
            stderr = b'Unknown option: -bogus'

        orig_run = subprocess.run
        subprocess.run = lambda *a, **k: FakeProc()
        try:
            with self.assertRaisesRegex(RuntimeError, 'exiftool failed'):
                photoindex._run_exiftool([Path('missing.jpg')])
        finally:
            subprocess.run = orig_run

    def test_run_exiftool_all_unreadable_batch_is_empty_not_fatal(self) -> None:
        # The common #34 shape: an incremental scan whose ONE changed file is
        # locked, or a one-file tail batch. exiftool exits 1 with per-file
        # errors and NO JSON; that is the caller's skip-and-warn case, not a
        # tool failure - and raising here used to discard every earlier
        # batch's inserts too (the scan commits at the end).
        class FakeProc:
            returncode = 1
            stdout = b''
            stderr = (b'Error: File not found - a.jpg\n'
                      b'    0 image files read\n    1 files could not be read\n')

        orig_run = subprocess.run
        subprocess.run = lambda *a, **k: FakeProc()
        try:
            self.assertEqual(photoindex._run_exiftool([Path('a.jpg')]), [])
        finally:
            subprocess.run = orig_run

    def test_run_exiftool_passes_paths_on_stdin_never_the_command_line(self) -> None:
        # 500 realistic Windows paths overflow the 32,767-char command-line
        # cap (#34), so the file list must travel via `-@ -` on stdin - the
        # command line then stays bounded no matter how many paths are passed,
        # and no temp file (whose own path exiftool would decode under
        # -charset filename=utf8) is involved.
        long_dir = 'D:/Family Photos/' + ('a descriptive folder name/' * 3)
        paths = [Path(f'{long_dir}photo with a long name {i:04}.jpg') for i in range(500)]
        # A decomposed-Unicode name (combining grave, U+0300) must round-trip.
        paths.append(Path(long_dir + 'de G.a\u0300 D. 1945.jpg'))
        seen: dict[str, object] = {}

        class FakeProc:
            returncode = 0
            stdout = b'[]'
            stderr = b''

        def fake_run(cmd, **kwargs):
            seen['cmd'] = cmd
            seen['input'] = kwargs.get('input')
            return FakeProc()

        orig_run = subprocess.run
        subprocess.run = fake_run
        try:
            photoindex._run_exiftool(paths)
        finally:
            subprocess.run = orig_run

        cmd = seen['cmd']
        self.assertLess(len(' '.join(str(c) for c in cmd)), 2000)
        for p in paths:
            self.assertNotIn(str(p), cmd)
        at = cmd.index('-@')
        self.assertEqual(cmd[at + 1], '-')
        charset_at = cmd.index('-charset')
        self.assertEqual(cmd[charset_at + 1], 'filename=utf8')
        self.assertIn('-Error', cmd)
        self.assertIsInstance(seen['input'], bytes)
        lines = seen['input'].decode('utf-8', 'surrogateescape').splitlines()
        self.assertEqual(lines, [str(p) for p in paths])

    def test_run_exiftool_never_blames_a_present_binary_for_winerror_206(self) -> None:
        # CreateProcess's WinError 206 (command line too long) reaches Python
        # as FileNotFoundError, which used to be reported as "exiftool is not
        # installed" while doctor said it was healthy (#34). The defensive
        # branch must name the real cause instead.
        def fake_run(cmd, **kwargs):
            e = FileNotFoundError(2, 'The filename or extension is too long')
            e.winerror = 206
            raise e

        orig_run = subprocess.run
        subprocess.run = fake_run
        try:
            with self.assertRaisesRegex(RuntimeError, 'command line was too long'):
                photoindex._run_exiftool([Path('a.jpg')])
        finally:
            subprocess.run = orig_run

    def test_run_exiftool_keeps_partial_results_on_error_exit(self) -> None:
        # exiftool exits non-zero when ANY file fails while still emitting
        # valid JSON for the ones it read - a single unreadable file must not
        # discard the rest of the batch (#34). Undecodable stderr bytes must
        # not turn into a UnicodeDecodeError either.
        class FakeProc:
            returncode = 1
            stdout = b'[{"SourceFile": "a.jpg"}, {"SourceFile": "b.jpg"}]'
            stderr = b'Error: File not found - c\xe9.jpg\xff'

        orig_run = subprocess.run
        subprocess.run = lambda *a, **k: FakeProc()
        try:
            rows = photoindex._run_exiftool([Path('a.jpg'), Path('b.jpg'), Path('c.jpg')])
        finally:
            subprocess.run = orig_run
        self.assertEqual([r['SourceFile'] for r in rows], ['a.jpg', 'b.jpg'])

    def test_scan_treats_exiftool_error_rows_as_unreadable(self) -> None:
        # exiftool emits a SourceFile-only row WITH an `Error` field for a
        # file it opened but could not read (empty, truncated). That row must
        # count as unreadable and never overwrite a good prior row with
        # blanks - the file still exists, so its stale metadata is kept.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            photoindex._run_exiftool = lambda paths: [
                {'SourceFile': str(p), 'Title': f'good {p.name}'} for p in paths]
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            def error_row_exiftool(paths: list[Path]) -> list[dict]:
                return [
                    {'SourceFile': str(p), 'Error': 'File is empty'}
                    if p.name == 'family_reunion.jpg'
                    else {'SourceFile': str(p), 'Title': f'good {p.name}'}
                    for p in paths
                ]

            photoindex._run_exiftool = error_row_exiftool
            summary = photoindex.run_scan(
                archive, {'roots': {'photos': 'photos'}}, full=True)
            self.assertEqual(summary['unreadable'], 1)
            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                title = conn.execute(
                    "SELECT title FROM photos WHERE path LIKE '%family_reunion.jpg'"
                ).fetchone()[0]
                self.assertEqual(title, 'good family_reunion.jpg')
            finally:
                conn.close()

    def test_photos_ignore_coerces_scalars_and_matches_case_insensitively(self) -> None:
        # YAML reads `- 2019` as an int; the intent is unambiguous. And a
        # photo library sits on a case-insensitive filesystem on Windows and
        # macOS alike, so 'flickr export' must prune 'Flickr Export'.
        pats = photoindex._photos_ignore_patterns({'photos_ignore': [2019, 'Flickr Export']})
        self.assertEqual(pats, ['2019', 'Flickr Export'])
        self.assertEqual(photoindex._photos_ignore_patterns({'photos_ignore': 2019}), ['2019'])
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'Flickr Export').mkdir()
            (root / 'Flickr Export' / 'x.jpg').write_bytes(b'x')
            (root / 'Keep').mkdir()
            (root / 'Keep' / 'y.jpg').write_bytes(b'x')
            (root / 'Scans [2019]').mkdir()
            (root / 'Scans [2019]' / 'z.jpg').write_bytes(b'x')
            got = sorted(p.name for p in photoindex._iter_photo_files(
                root, ['flickr export', 'Scans [[]2019]']))
            self.assertEqual(got, ['y.jpg'])

    def test_catalog_carries_group_and_path_indexes(self) -> None:
        # Without idx_photos_group_id, _candidate_groups()'s correlated NOT
        # EXISTS is O(groups x photos) and triage never returns on a real
        # library (#41). The IF NOT EXISTS DDL runs on every open, so a cache
        # created before the indexes existed gains them on its next use with
        # no schema bump or rescan.
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d) / '.cache'
            conn, _backfill, _reason = photoindex._get_db(cache)
            try:
                names = {
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'")
                }
            finally:
                conn.close()
            for expected in (
                'idx_photos_group_id', 'idx_photo_keywords_path',
                'idx_photo_people_path', 'idx_photo_face_regions_path',
            ):
                self.assertIn(expected, names)

            # Simulate the pre-index cache: drop them, reopen, expect them back.
            conn = sqlite3.connect(cache / 'photos.sqlite')
            for name in (
                'idx_photos_group_id', 'idx_photo_keywords_path',
                'idx_photo_people_path', 'idx_photo_face_regions_path',
            ):
                conn.execute(f'DROP INDEX {name}')
            conn.commit()
            conn.close()

            conn, _backfill, reason = photoindex._get_db(cache)
            try:
                names = {
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'")
                }
            finally:
                conn.close()
            self.assertIsNone(reason, 'index-less cache must heal, not rebuild')
            self.assertIn('idx_photos_group_id', names)

    def test_corrupt_photos_sqlite_is_recreated(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cache = archive / '.cache'
            cache.mkdir()
            db_path = cache / 'photos.sqlite'
            db_path.write_text('not sqlite', encoding='utf-8')

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                return [{'SourceFile': str(p)} for p in paths]

            photoindex._run_exiftool = fake_exiftool
            summary = photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            self.assertEqual(summary['scraped'], 4)
            conn = sqlite3.connect(db_path)
            try:
                count = conn.execute('SELECT COUNT(*) FROM photos').fetchone()[0]
                self.assertEqual(count, 4)
            finally:
                conn.close()

    def test_cache_directory_creation_failure_raises_runtime_error(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            # A plain file named '.cache' blocks mkdir() with a clean failure
            # (e.g. NotADirectoryError/FileExistsError) instead of a raw
            # traceback escaping run_scan.
            (archive / '.cache').write_text('not a directory', encoding='utf-8')

            with self.assertRaises(RuntimeError):
                photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

    def test_stat_failure_during_scan_raises_runtime_error(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            target = archive / 'photos' / 'family_reunion.jpg'

            orig_stat = Path.stat

            def failing_stat(self, *a, **k):
                # Target only the explicit `p.stat()` call run_scan's loop
                # makes to read mtime/size, not whatever stat-like check
                # is_file() does during file discovery (`_iter_photo_files`)
                # - on some platforms/pathlib versions is_file() resolves
                # to a C-level syscall that never reaches Path.stat() at
                # all, so counting total calls to this file is not
                # portable; identifying run_scan's own frame is.
                if self.name == 'family_reunion.jpg' and inspect.stack()[1].function == 'run_scan':
                    raise OSError('permission denied')
                return orig_stat(self, *a, **k)

            Path.stat = failing_stat
            try:
                with self.assertRaisesRegex(RuntimeError, 'could not stat'):
                    photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})
            finally:
                Path.stat = orig_stat
            self.assertTrue(target.exists())

    def test_cmd_scan_cli_reports_clean_error_on_sqlite_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                return [{'SourceFile': str(p)} for p in paths]

            photoindex._run_exiftool = fake_exiftool

            orig_group_photos = photoindex._group_photos

            def failing_group_photos(conn):
                raise sqlite3.OperationalError('database is locked')

            photoindex._group_photos = failing_group_photos
            try:
                args = type('Args', (), {'root': str(archive), 'full': False})()
                code = photoindex._cmd_scan(args)
                self.assertEqual(code, photoindex.EXIT_FAILURE)
            finally:
                photoindex._group_photos = orig_group_photos

    def test_unreadable_file_is_skipped_with_stale_row_kept_not_fatal(self) -> None:
        # One corrupt/locked file must not make the whole catalog unbuildable
        # (#34): the scan records it, skips it, finishes - and any prior cache
        # row for it survives (the file still exists on disk, so the stale-row
        # sweep leaves it alone; stale metadata beats none).
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))

            def first_exiftool(paths: list[Path]) -> list[dict]:
                return [
                    {'SourceFile': str(p), 'Title': f'first {p.name}'}
                    for p in paths
                ]

            photoindex._run_exiftool = first_exiftool
            first_summary = photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})
            self.assertEqual(first_summary['scraped'], 4)
            self.assertEqual(first_summary['unreadable'], 0)

            changed = archive / 'photos' / 'family_reunion.jpg'
            os.utime(changed, None)

            def missing_one_exiftool(paths: list[Path]) -> list[dict]:
                return [
                    {'SourceFile': str(p), 'Title': f'second {p.name}'}
                    for p in paths
                    if p.name != 'family_reunion.jpg'
                ]

            photoindex._run_exiftool = missing_one_exiftool
            summary = photoindex.run_scan(
                archive, {'roots': {'photos': 'photos'}}, full=True)

            self.assertEqual(summary['unreadable'], 1)
            self.assertEqual(len(summary['unreadable_sample']), 1)
            self.assertIn('family_reunion.jpg', summary['unreadable_sample'][0])
            self.assertEqual(summary['scraped'], 3)
            self.assertEqual(summary['unchanged'], 0)

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                title = conn.execute(
                    "SELECT title FROM photos WHERE path LIKE '%family_reunion.jpg'"
                ).fetchone()[0]
                self.assertEqual(title, 'first family_reunion.jpg')
                refreshed = conn.execute(
                    "SELECT COUNT(*) FROM photos WHERE title LIKE 'second %'"
                ).fetchone()[0]
                self.assertEqual(refreshed, 3)
            finally:
                conn.close()

    def test_cmd_scan_warns_and_exits_warnings_on_unreadable_files(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))

            def missing_one_exiftool(paths: list[Path]) -> list[dict]:
                return [
                    {'SourceFile': str(p)}
                    for p in paths
                    if p.name != 'family_reunion.jpg'
                ]

            photoindex._run_exiftool = missing_one_exiftool
            args = type('Args', (), {'root': str(archive), 'full': False})()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                code = photoindex._cmd_scan(args)
            self.assertEqual(code, EXIT_WARNINGS)
            self.assertIn('could not read 1 file(s)', stderr.getvalue())
            self.assertIn('family_reunion.jpg', stderr.getvalue())

    def test_photoindex_subcommands_are_registered_in_the_cli(self) -> None:
        """`fha photoindex <subcommand> --help` should resolve for every M3.1-M3.6 subcommand.

        set-summary is additionally exercised through the standalone entry point
        (`python tools/photoindex.py`) - both parsers share _add_photoindex_args,
        and this pins that a new subcommand reached both front doors."""
        for name in ('find', 'gallery', 'triage', 'report', 'reconcile', 'tag-person', 'set-summary'):
            with self.subTest(name=name):
                proc = subprocess.run(
                    [sys.executable, 'tools/fha.py', 'photoindex', name, '--help'],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = subprocess.run(
            [sys.executable, 'tools/photoindex.py', 'set-summary', '--help'],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding='utf-8',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def _scan_with_find_fixture(self, archive: Path) -> None:
        """Scan with a fixed exiftool payload exercising person/keyword/edtf/text filters."""
        def fake_exiftool(paths: list[Path]) -> list[dict]:
            rows = {
                'portrait_1880.jpg': {
                    'Keywords': ['DATE: Y!'],
                    'DateTimeOriginal': '1880:01:01 00:00:00',
                    'Title': 'Portrait front',
                },
                'portrait_1880-back.jpg': {
                    'Keywords': ['DATE: Y!'],
                    'DateTimeOriginal': '1880:01:01 00:00:00',
                    'Caption-Abstract': 'cemetery visit',
                },
                'wedding_1902.jpg': {
                    'Keywords': ['SOURCE: S-123456789a', 'DATE: Y!'],
                    'DateTimeOriginal': '1902:01:01 00:00:00',
                    'Caption-Abstract': 'Wedding party',
                },
                'family_reunion.jpg': {
                    'Keywords': ['P-de957bcda1'], 'Caption-Abstract': 'Family reunion',
                },
            }
            return [{'SourceFile': str(p), **rows[p.name]} for p in paths]

        photoindex._run_exiftool = fake_exiftool
        photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

    def test_find_by_person_returns_groups_primary_path(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_find_fixture(archive)

            result = photoindex.run_find(
                archive, {'roots': {'photos': 'photos'}}, person='p-de957bcda1',
            )

            self.assertEqual(result['status'], 'fresh')
            self.assertEqual([r['path'] for r in result['rows']], ['photos/family_reunion.jpg'])

    def test_find_by_text_returns_caption_hit_at_group_primary(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_find_fixture(archive)

            result = photoindex.run_find(
                archive, {'roots': {'photos': 'photos'}}, text='cemetery',
            )

            # 'cemetery' is only on the back variant, but the group's
            # primary (front) path is what the default, deduped view returns.
            self.assertEqual([r['path'] for r in result['rows']], ['photos/portrait_1880.jpg'])

    def test_find_by_edtf_bounds_overlap_dedupes_to_one_group(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_find_fixture(archive)

            result = photoindex.run_find(
                archive, {'roots': {'photos': 'photos'}}, edtf='188X',
            )

            self.assertEqual([r['path'] for r in result['rows']], ['photos/portrait_1880.jpg'])

            files_result = photoindex.run_find(
                archive, {'roots': {'photos': 'photos'}}, edtf='188X', files=True,
            )
            self.assertEqual(
                sorted(r['path'] for r in files_result['rows']),
                ['photos/portrait_1880-back.jpg', 'photos/portrait_1880.jpg'],
            )

    def test_find_combines_filters_with_and(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_find_fixture(archive)

            result = photoindex.run_find(
                archive, {'roots': {'photos': 'photos'}}, edtf='188X', text='cemetery',
            )

            self.assertEqual([r['path'] for r in result['rows']], ['photos/portrait_1880.jpg'])

            no_match = photoindex.run_find(
                archive, {'roots': {'photos': 'photos'}}, edtf='1902', text='cemetery',
            )
            self.assertEqual(no_match['rows'], [])

    def test_find_requires_at_least_one_filter(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_find_fixture(archive)

            with self.assertRaises(ValueError):
                photoindex.run_find(archive, {'roots': {'photos': 'photos'}})

    def test_find_on_absent_index_reports_absent_status(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))

            result = photoindex.run_find(
                archive, {'roots': {'photos': 'photos'}}, keyword='date',
            )

            self.assertEqual(result['status'], 'absent')
            self.assertEqual(result['rows'], [])

    def test_cmd_find_cli_prints_match_and_exits_clean(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_find_fixture(archive)

            args = type('Args', (), {
                'root': str(archive),
                'person': 'P-de957bcda1',
                'keyword': None,
                'edtf': None,
                'text': None,
                'files': False,
            })()

            code = photoindex._cmd_find(args)

            self.assertEqual(code, 0)

    def test_cmd_find_cli_invalid_person_id_fails(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_find_fixture(archive)

            args = type('Args', (), {
                'root': str(archive),
                'person': 'not-an-id',
                'keyword': None,
                'edtf': None,
                'text': None,
                'files': False,
            })()

            code = photoindex._cmd_find(args)

            self.assertEqual(code, photoindex.EXIT_FAILURE)

    def test_find_normalizes_person_id_case(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_find_fixture(archive)

            result = photoindex.run_find(
                archive, {'roots': {'photos': 'photos'}}, person='P-DE957BCDA1',
            )

            self.assertEqual([r['path'] for r in result['rows']], ['photos/family_reunion.jpg'])

    def test_cmd_find_cli_on_absent_index_exits_failure(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))

            args = type('Args', (), {
                'root': str(archive),
                'person': None,
                'keyword': 'date',
                'edtf': None,
                'text': None,
                'files': False,
            })()

            code = photoindex._cmd_find(args)

            self.assertEqual(code, photoindex.EXIT_FAILURE)

    def test_cmd_find_cli_on_corrupt_index_exits_failure(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_find_fixture(archive)
            (archive / '.cache' / 'photos.sqlite').write_bytes(b'not a sqlite database')

            args = type('Args', (), {
                'root': str(archive),
                'person': None,
                'keyword': 'date',
                'edtf': None,
                'text': None,
                'files': False,
            })()

            code = photoindex._cmd_find(args)

            self.assertEqual(code, photoindex.EXIT_FAILURE)

    def test_cmd_find_cli_on_stale_index_warns_but_still_returns_rows(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_find_fixture(archive)

            cache = archive / '.cache'
            index_db = cache / 'index.sqlite'
            sqlite3.connect(index_db).close()
            photos_mtime = (cache / 'photos.sqlite').stat().st_mtime
            os.utime(index_db, (photos_mtime + 10, photos_mtime + 10))

            args = type('Args', (), {
                'root': str(archive),
                'person': 'P-de957bcda1',
                'keyword': None,
                'edtf': None,
                'text': None,
                'files': False,
            })()

            code = photoindex._cmd_find(args)

            self.assertEqual(code, photoindex.EXIT_CLEAN)

    def test_find_combines_filters_at_group_level_across_variants(self) -> None:
        """Two filters matching different variants of one photo still match the group.

        Regression for the raw-path intersection: the date lives only on the front
        scan's keyword and the caption text only on the back scan, so no single raw
        path satisfies both --edtf and --text, yet they are one logical photo.
        """
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                rows = {
                    'portrait_1880.jpg': {
                        'Keywords': ['DATE: Y!'],
                        'DateTimeOriginal': '1880:01:01 00:00:00',
                    },
                    'portrait_1880-back.jpg': {'Caption-Abstract': 'cemetery visit'},
                    'wedding_1902.jpg': {
                        'Keywords': ['DATE: Y!'],
                        'DateTimeOriginal': '1902:01:01 00:00:00',
                    },
                    'family_reunion.jpg': {'Caption-Abstract': 'Family reunion'},
                }
                return [{'SourceFile': str(p), **rows[p.name]} for p in paths]

            photoindex._run_exiftool = fake_exiftool
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            result = photoindex.run_find(
                archive, {'roots': {'photos': 'photos'}}, edtf='188X', text='cemetery',
            )

            self.assertEqual([r['path'] for r in result['rows']], ['photos/portrait_1880.jpg'])

    def test_find_files_expands_matched_group_to_all_variants(self) -> None:
        """--files lists sibling variants of a matched group even if they didn't match.

        The front scan carries the DATE keyword and the back scan is untagged, so
        only the front raw-matches --edtf; --files must still return both files
        because they are variants of one matched logical photo.
        """
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                rows = {
                    'portrait_1880.jpg': {
                        'Keywords': ['DATE: Y!'],
                        'DateTimeOriginal': '1880:01:01 00:00:00',
                    },
                    'portrait_1880-back.jpg': {'Caption-Abstract': 'untagged back'},
                    'wedding_1902.jpg': {
                        'Keywords': ['DATE: Y!'],
                        'DateTimeOriginal': '1902:01:01 00:00:00',
                    },
                    'family_reunion.jpg': {'Caption-Abstract': 'Family reunion'},
                }
                return [{'SourceFile': str(p), **rows[p.name]} for p in paths]

            photoindex._run_exiftool = fake_exiftool
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            result = photoindex.run_find(
                archive, {'roots': {'photos': 'photos'}}, edtf='188X', files=True,
            )

            self.assertEqual(
                sorted(r['path'] for r in result['rows']),
                ['photos/portrait_1880-back.jpg', 'photos/portrait_1880.jpg'],
            )

    def test_find_text_does_not_match_filename_path(self) -> None:
        """--text searches metadata only; a term present only in the path must not match.

        photo_fts also indexes `path`, so an unscoped MATCH on 'wedding' would hit
        photos/wedding_1902.jpg via its filename even though its caption never says
        'wedding'. The column-filtered query must return no rows here.
        """
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                rows = {
                    'portrait_1880.jpg': {'Title': 'Portrait front'},
                    'portrait_1880-back.jpg': {'Caption-Abstract': 'back'},
                    'wedding_1902.jpg': {'Caption-Abstract': 'Reception party'},
                    'family_reunion.jpg': {'Caption-Abstract': 'gathering'},
                }
                return [{'SourceFile': str(p), **rows[p.name]} for p in paths]

            photoindex._run_exiftool = fake_exiftool
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            result = photoindex.run_find(
                archive, {'roots': {'photos': 'photos'}}, text='wedding',
            )

            self.assertEqual(result['rows'], [])

    def test_cmd_find_cli_incompatible_schema_reported_even_on_no_match(self) -> None:
        """An incompatible cache is reported even when the filter matches nothing."""
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cache = archive / '.cache'
            cache.mkdir(exist_ok=True)
            conn = sqlite3.connect(cache / 'photos.sqlite')
            conn.executescript(
                'CREATE TABLE photos(path TEXT);'
                'CREATE TABLE photo_face_regions(path TEXT);'
                'CREATE TABLE photo_fts(path TEXT, body TEXT);'
                'CREATE TABLE photo_groups(group_id TEXT);'
                'CREATE TABLE photo_keywords(path TEXT, keyword TEXT);'
                'CREATE TABLE photo_people(path TEXT, person_ref TEXT);'
            )
            conn.commit()
            conn.close()

            args = type('Args', (), {
                'root': str(archive),
                'person': None,
                'keyword': 'no-such-keyword',      # matches nothing
                'edtf': None,
                'text': None,
                'files': False,
            })()

            code = photoindex._cmd_find(args)

            self.assertEqual(code, photoindex.EXIT_FAILURE)

    def test_find_text_treats_punctuation_as_literal(self) -> None:
        """--text with punctuation matches the literal string, not FTS operators.

        Pre-fix, splicing `P-de957bcda1` into the FTS expression made `-` parse as
        syntax and raised OperationalError; the term must instead match the cached
        keyword literally.
        """
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_find_fixture(archive)

            result = photoindex.run_find(
                archive, {'roots': {'photos': 'photos'}}, text='P-de957bcda1',
            )

            self.assertEqual([r['path'] for r in result['rows']], ['photos/family_reunion.jpg'])

    def test_find_stale_when_person_record_newer_than_photo_cache(self) -> None:
        """A profile edited after the last scan makes photo_people stale → warn.

        photo_people's face-tag/name-match tiers derive from person records via
        index.sqlite. If a profile changes but `fha index`/`fha photoindex` aren't
        rerun, find would otherwise serve stale weak matches as 'fresh'.
        """
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_find_fixture(archive)

            people_dir = archive / 'people'
            people_dir.mkdir(exist_ok=True)
            profile = people_dir / 'hartley__thomas_edward_P-de957bcda1.md'
            profile.write_text('---\nid: P-de957bcda1\n---\n', encoding='utf-8')
            photos_mtime = (archive / '.cache' / 'photos.sqlite').stat().st_mtime
            os.utime(profile, (photos_mtime + 10, photos_mtime + 10))

            status, _lag = photoindex_status(archive, {'roots': {'photos': 'photos'}})
            self.assertEqual(status, 'stale')

            result = photoindex.run_find(
                archive, {'roots': {'photos': 'photos'}}, person='p-de957bcda1',
            )
            self.assertEqual(result['status'], 'stale')

    def test_find_rejects_invalid_edtf(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_find_fixture(archive)

            with self.assertRaises(ValueError):
                photoindex.run_find(
                    archive, {'roots': {'photos': 'photos'}}, edtf='banana',
                )

    def test_cmd_find_cli_invalid_edtf_fails(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_find_fixture(archive)

            args = type('Args', (), {
                'root': str(archive),
                'person': None,
                'keyword': None,
                'edtf': 'banana',
                'text': None,
                'files': False,
            })()

            code = photoindex._cmd_find(args)

            self.assertEqual(code, photoindex.EXIT_FAILURE)

    def test_cmd_find_cli_rejects_non_person_id(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_find_fixture(archive)

            args = type('Args', (), {
                'root': str(archive),
                'person': 'S-123456789a',     # syntactically valid id, wrong type
                'keyword': None,
                'edtf': None,
                'text': None,
                'files': False,
            })()

            code = photoindex._cmd_find(args)

            self.assertEqual(code, photoindex.EXIT_FAILURE)

    def test_find_subcommand_preserves_parent_root(self) -> None:
        """`fha photoindex --root X find ...` must keep X, not reset it to None."""
        parser = argparse.ArgumentParser()
        photoindex._add_photoindex_args(parser)

        args = parser.parse_args(
            ['--root', '/some/archive', 'find', '--person', 'P-de957bcda1']
        )

        self.assertEqual(args.root, '/some/archive')
        self.assertEqual(args.func, photoindex._cmd_find)

    def test_cmd_find_cli_on_incompatible_schema_exits_failure(self) -> None:
        """A cache whose tables exist but whose columns don't is reported, not a traceback."""
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cache = archive / '.cache'
            cache.mkdir(exist_ok=True)
            conn = sqlite3.connect(cache / 'photos.sqlite')
            # All probed tables exist (so photoindex_status passes), but `photos`
            # is missing the columns the query selects.
            conn.executescript(
                'CREATE TABLE photos(path TEXT);'
                'CREATE TABLE photo_face_regions(path TEXT);'
                'CREATE TABLE photo_fts(path TEXT, body TEXT);'
                'CREATE TABLE photo_groups(group_id TEXT);'
                'CREATE TABLE photo_keywords(path TEXT, keyword TEXT);'
                'CREATE TABLE photo_people(path TEXT, person_ref TEXT);'
            )
            conn.execute("INSERT INTO photos(path) VALUES ('photos/x.jpg')")
            conn.execute(
                "INSERT INTO photo_keywords(path, keyword) VALUES ('photos/x.jpg', 'date 1880')"
            )
            conn.commit()
            conn.close()

            args = type('Args', (), {
                'root': str(archive),
                'person': None,
                'keyword': 'date',
                'edtf': None,
                'text': None,
                'files': False,
            })()

            code = photoindex._cmd_find(args)

            self.assertEqual(code, photoindex.EXIT_FAILURE)

    def test_missing_photos_root_cli_returns_warning(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            shutil.rmtree(archive / 'photos')
            args = type('Args', (), {
                'root': str(archive),
                'full': False,
            })()

            code = photoindex._cmd_scan(args)

            self.assertEqual(code, 1)

    def test_triage_ranks_unprocessed_groups_and_excludes_sourced_ones(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_find_fixture(archive)

            result = photoindex.run_triage(archive, {'roots': {'photos': 'photos'}})

            self.assertEqual(result['status'], 'fresh')
            paths = [c['path'] for c in result['candidates']]
            # wedding_1902.jpg already carries a SOURCE: keyword (processed) - excluded.
            self.assertNotIn('photos/wedding_1902.jpg', paths)
            self.assertEqual(
                sorted(paths),
                ['photos/family_reunion.jpg', 'photos/portrait_1880.jpg'],
            )

            by_path = {c['path']: c for c in result['candidates']}
            # family_reunion: +3 caption, +2 pid-keyword = 5
            self.assertEqual(by_path['photos/family_reunion.jpg']['score'], 5)
            self.assertIn('caption', by_path['photos/family_reunion.jpg']['signals'])
            self.assertIn('pid-keyword', by_path['photos/family_reunion.jpg']['signals'])
            # portrait group: +3 caption (back), +1 confident date, +1 back-variant = 5
            self.assertEqual(by_path['photos/portrait_1880.jpg']['score'], 5)
            self.assertIn('back-variant', by_path['photos/portrait_1880.jpg']['signals'])

    def test_triage_back_variant_signal_reads_the_role_not_its_first_letters(self) -> None:
        # 'back-variant' means a back scan exists (there is writing on the
        # reverse worth reading). The stored variant_role is the TOOLING §6
        # compound: the part-kind, plus '-crop' when the scan is a crop of it -
        # so 'back' and 'back-crop' are the whole vocabulary that means back. A
        # freeform suffix becomes the role verbatim ('-backdrop' -> 'backdrop'),
        # so a prefix test scored a backdrop shot as a back scan and ranked it
        # above photos that really do carry writing on the reverse. `fha
        # process` folder triage scores the same signal off the parsed
        # part_kind, and the two must not disagree.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            photos = archive / 'photos'
            for name in ('church_1930.jpg', 'church_1930-backdrop.jpg',
                         'school_1930.jpg', 'school_1930-back.jpg'):
                shutil.copyfile(photos / 'portrait_1880.jpg', photos / name)
            photoindex._run_exiftool = lambda paths: [
                {'SourceFile': str(p)} for p in paths
            ]
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            result = photoindex.run_triage(archive, {'roots': {'photos': 'photos'}})
            by_path = {c['path']: c for c in result['candidates']}
            self.assertNotIn(
                'back-variant', by_path['photos/church_1930-backdrop.jpg']['signals'])
            # A real back scan still counts, by the same rule.
            self.assertIn(
                'back-variant', by_path['photos/school_1930.jpg']['signals'])

    def test_triage_excludes_a_group_made_entirely_of_missing_rows(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_find_fixture(archive)

            (archive / 'photos' / 'family_reunion.jpg').unlink()
            photoindex.run_reconcile(archive, {'roots': {'photos': 'photos'}})

            # family_reunion's group now contains only a 'MISSING:'-prefixed
            # row (no on-disk file survives in it) - triage must not suggest
            # `fha process` on a synthetic path nothing can actually process.
            result = photoindex.run_triage(archive, {'roots': {'photos': 'photos'}})
            paths = [c['path'] for c in result['candidates']]
            self.assertNotIn('MISSING:photos/family_reunion.jpg', paths)
            self.assertNotIn('photos/family_reunion.jpg', paths)

    def test_triage_top_limits_results(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_find_fixture(archive)

            result = photoindex.run_triage(archive, {'roots': {'photos': 'photos'}}, top=1)

            self.assertEqual(len(result['candidates']), 1)

    def test_triage_rejects_non_positive_top(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_find_fixture(archive)

            with self.assertRaises(ValueError):
                photoindex.run_triage(archive, {'roots': {'photos': 'photos'}}, top=0)

            args = type('Args', (), {'root': str(archive), 'top': -1})()
            code = photoindex._cmd_triage(args)
            self.assertEqual(code, photoindex.EXIT_FAILURE)

    def test_candidate_groups_are_not_null_poisoned_by_malformed_cache_row(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_find_fixture(archive)
            conn = sqlite3.connect(str(archive / '.cache' / 'photos.sqlite'))
            conn.row_factory = sqlite3.Row
            try:
                conn.execute(
                    'INSERT INTO photos(path, mtime, size, source_id, group_id) '
                    'VALUES (?,?,?,?,NULL)',
                    ('photos/orphaned-cache-row.jpg', 0, 0, 'S-123456789a'),
                )
                conn.commit()

                paths = {row['primary_path'] for row in photoindex._candidate_groups(conn)}
            finally:
                conn.close()

            self.assertIn('photos/family_reunion.jpg', paths)
            self.assertIn('photos/portrait_1880.jpg', paths)
            self.assertNotIn('photos/wedding_1902.jpg', paths)

    def test_triage_ai_only_comment_without_caption_is_penalized(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                rows = {
                    'portrait_1880.jpg': {'UserComment': 'AI: a portrait of two people'},
                    'portrait_1880-back.jpg': {},
                    'wedding_1902.jpg': {},
                    'family_reunion.jpg': {},
                }
                return [{'SourceFile': str(p), **rows[p.name]} for p in paths]

            photoindex._run_exiftool = fake_exiftool
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            result = photoindex.run_triage(archive, {'roots': {'photos': 'photos'}})
            by_path = {c['path']: c for c in result['candidates']}
            # -2 ai-only, +1 back-variant (portrait_1880-back.jpg) = -1
            self.assertEqual(by_path['photos/portrait_1880.jpg']['score'], -1)
            self.assertIn('ai-only', by_path['photos/portrait_1880.jpg']['signals'])

    def test_triage_on_absent_index_reports_absent_status(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))

            result = photoindex.run_triage(archive, {'roots': {'photos': 'photos'}})

            self.assertEqual(result['status'], 'absent')
            self.assertEqual(result['candidates'], [])

    def test_cmd_triage_cli_prints_candidates_and_exits_clean(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_find_fixture(archive)

            args = type('Args', (), {'root': str(archive), 'top': 10})()
            code = photoindex._cmd_triage(args)

            self.assertEqual(code, 0)

    def test_cmd_triage_cli_on_absent_index_exits_failure(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))

            args = type('Args', (), {'root': str(archive), 'top': 10})()
            code = photoindex._cmd_triage(args)

            self.assertEqual(code, photoindex.EXIT_FAILURE)

    def test_report_lists_only_groups_with_date_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                rows = {
                    'portrait_1880.jpg': {
                        'Keywords': ['DATE: Y!'],
                        'DateTimeOriginal': '1880:01:01 00:00:00',
                    },
                    'portrait_1880-back.jpg': {
                        'Keywords': ['DATE: Y!'],
                        'DateTimeOriginal': '1881:01:01 00:00:00',
                        'Caption-Abstract': 'written 1881',
                    },
                    'wedding_1902.jpg': {
                        'Keywords': ['SOURCE: S-123456789a', 'DATE: Y!'],
                        'DateTimeOriginal': '1902:01:01 00:00:00',
                    },
                    'family_reunion.jpg': {},
                }
                return [{'SourceFile': str(p), **rows[p.name]} for p in paths]

            photoindex._run_exiftool = fake_exiftool
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            result = photoindex.run_report(archive, {'roots': {'photos': 'photos'}})

            self.assertEqual(result['status'], 'fresh')
            self.assertEqual(len(result['conflicts']), 1)
            conflict = result['conflicts'][0]
            self.assertEqual(conflict['primary_path'], 'photos/portrait_1880.jpg')
            photo_paths = sorted(p['path'] for p in conflict['photos'])
            self.assertEqual(
                photo_paths,
                ['photos/portrait_1880-back.jpg', 'photos/portrait_1880.jpg'],
            )
            by_path = {p['path']: p for p in conflict['photos']}
            self.assertEqual(by_path['photos/portrait_1880.jpg']['edtf'], '1880')
            self.assertEqual(by_path['photos/portrait_1880-back.jpg']['caption'], 'written 1881')

    def test_report_on_absent_index_reports_absent_status(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))

            result = photoindex.run_report(archive, {'roots': {'photos': 'photos'}})

            self.assertEqual(result['status'], 'absent')
            self.assertEqual(result['conflicts'], [])

    def test_cmd_report_cli_prints_conflicts_and_exits_clean(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                rows = {
                    'portrait_1880.jpg': {
                        'Keywords': ['DATE: Y!'],
                        'DateTimeOriginal': '1880:01:01 00:00:00',
                    },
                    'portrait_1880-back.jpg': {
                        'Keywords': ['DATE: Y!'],
                        'DateTimeOriginal': '1881:01:01 00:00:00',
                    },
                    'wedding_1902.jpg': {},
                    'family_reunion.jpg': {},
                }
                return [{'SourceFile': str(p), **rows[p.name]} for p in paths]

            photoindex._run_exiftool = fake_exiftool
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            args = type('Args', (), {'root': str(archive)})()
            code = photoindex._cmd_report(args)

            self.assertEqual(code, 0)

    def test_cmd_report_cli_on_absent_index_exits_failure(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))

            args = type('Args', (), {'root': str(archive)})()
            code = photoindex._cmd_report(args)

            self.assertEqual(code, photoindex.EXIT_FAILURE)

    # ── reconcile (BUILD.md M3.4) ─────────────────────────────────────────

    def test_reconcile_rematches_moved_file_by_source_id(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                rows = {
                    'wedding_1902.jpg': {'Keywords': ['SOURCE: S-123456789a']},
                }
                return [{'SourceFile': str(p), **rows.get(p.name, {})} for p in paths]

            photoindex._run_exiftool = fake_exiftool
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            # Simulate the file moving outside fha: same SOURCE: keyword, new name.
            old = archive / 'photos' / 'wedding_1902.jpg'
            new = archive / 'photos' / 'wedding_renamed.jpg'
            old.rename(new)

            def reconcile_exiftool(paths: list[Path]) -> list[dict]:
                return [
                    {'SourceFile': str(p), 'Keywords': ['SOURCE: S-123456789a']}
                    for p in paths
                ]

            photoindex._run_exiftool = reconcile_exiftool
            result = photoindex.run_reconcile(
                archive, {'roots': {'photos': 'photos'}}, with_exif=True,
            )

            # Depending on filesystem mtime resolution the rename may or may not
            # bump the photos root's mtime past the cache's; either way reconcile
            # must still run (only absent/unreadable short-circuit).
            self.assertIn(result['status'], ('fresh', 'stale'))
            self.assertEqual(
                result['rematched'], [('photos/wedding_1902.jpg', 'photos/wedding_renamed.jpg')],
            )
            self.assertEqual(result['missing'], [])
            self.assertEqual(result['new_count'], 0)

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                paths = [row[0] for row in conn.execute('SELECT path FROM photos')]
                self.assertIn('photos/wedding_renamed.jpg', paths)
                self.assertNotIn('photos/wedding_1902.jpg', paths)

                # The renamed file was its group's primary_path; that must move too,
                # or `photo_groups` would keep pointing at a path with no `photos` row.
                primary = conn.execute(
                    "SELECT primary_path FROM photo_groups WHERE group_id LIKE 'SOURCE:%'"
                ).fetchone()[0]
                self.assertEqual(primary, 'photos/wedding_renamed.jpg')
            finally:
                conn.close()

    def test_reconcile_rematch_updates_photo_fts_path(self) -> None:
        """A rematch must move `photo_fts.path` too, or `find --text` keeps
        matching the pre-reconcile path indefinitely (it is never rebuilt
        until the next full scan)."""
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                rows = {
                    'wedding_1902.jpg': {
                        'Keywords': ['SOURCE: S-123456789a'],
                        'Caption-Abstract': 'Reception party',
                    },
                }
                return [{'SourceFile': str(p), **rows.get(p.name, {})} for p in paths]

            photoindex._run_exiftool = fake_exiftool
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            old = archive / 'photos' / 'wedding_1902.jpg'
            new = archive / 'photos' / 'wedding_renamed.jpg'
            old.rename(new)

            photoindex._run_exiftool = lambda paths: [
                {'SourceFile': str(p), 'Keywords': ['SOURCE: S-123456789a']} for p in paths
            ]
            result = photoindex.run_reconcile(
                archive, {'roots': {'photos': 'photos'}}, with_exif=True,
            )
            self.assertEqual(
                result['rematched'], [('photos/wedding_1902.jpg', 'photos/wedding_renamed.jpg')],
            )

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                rows = conn.execute(
                    "SELECT path, caption FROM photo_fts WHERE path='photos/wedding_renamed.jpg'"
                ).fetchall()
                self.assertEqual(rows, [('photos/wedding_renamed.jpg', 'Reception party')])
                stale = conn.execute(
                    "SELECT 1 FROM photo_fts WHERE path='photos/wedding_1902.jpg'"
                ).fetchone()
                self.assertIsNone(stale)
            finally:
                conn.close()

    def test_reconcile_without_with_exif_does_not_rematch(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                rows = {'wedding_1902.jpg': {'Keywords': ['SOURCE: S-123456789a']}}
                return [{'SourceFile': str(p), **rows.get(p.name, {})} for p in paths]

            photoindex._run_exiftool = fake_exiftool
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            old = archive / 'photos' / 'wedding_1902.jpg'
            new = archive / 'photos' / 'wedding_renamed.jpg'
            old.rename(new)

            result = photoindex.run_reconcile(
                archive, {'roots': {'photos': 'photos'}}, with_exif=False,
            )

            self.assertEqual(result['rematched'], [])
            self.assertEqual(result['missing'], ['MISSING:photos/wedding_1902.jpg'])
            self.assertEqual(result['new_count'], 1)

    def test_reconcile_unmatchable_file_with_no_source_id_is_marked_missing(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            photoindex._run_exiftool = lambda paths: [{'SourceFile': str(p)} for p in paths]
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            (archive / 'photos' / 'portrait_1880.jpg').unlink()

            result = photoindex.run_reconcile(
                archive, {'roots': {'photos': 'photos'}}, with_exif=True,
            )

            self.assertEqual(result['rematched'], [])
            self.assertIn('MISSING:photos/portrait_1880.jpg', result['missing'])

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                row = conn.execute(
                    "SELECT path FROM photos WHERE path='MISSING:photos/portrait_1880.jpg'"
                ).fetchone()
                self.assertIsNotNone(row)
            finally:
                conn.close()

    def test_reconcile_missing_flag_updates_photo_fts_path(self) -> None:
        """Flagging a row MISSING: must also re-key its photo_fts row, or a
        `find --text` hit on its caption would still print the dead path."""
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            photoindex._run_exiftool = lambda paths: [
                {'SourceFile': str(p), 'Caption-Abstract': 'Family portrait'}
                if p.name == 'portrait_1880.jpg' else {'SourceFile': str(p)}
                for p in paths
            ]
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            (archive / 'photos' / 'portrait_1880.jpg').unlink()

            result = photoindex.run_reconcile(
                archive, {'roots': {'photos': 'photos'}}, with_exif=True,
            )
            self.assertIn('MISSING:photos/portrait_1880.jpg', result['missing'])

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                row = conn.execute(
                    "SELECT caption FROM photo_fts WHERE path='MISSING:photos/portrait_1880.jpg'"
                ).fetchone()
                self.assertEqual(row, ('Family portrait',))
                stale = conn.execute(
                    "SELECT 1 FROM photo_fts WHERE path='photos/portrait_1880.jpg'"
                ).fetchone()
                self.assertIsNone(stale)
            finally:
                conn.close()

    def test_reconcile_already_missing_row_remains_eligible_without_double_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            photoindex._run_exiftool = lambda paths: [{'SourceFile': str(p)} for p in paths]
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            (archive / 'photos' / 'portrait_1880.jpg').unlink()
            fha_config = {'roots': {'photos': 'photos'}}
            first = photoindex.run_reconcile(archive, fha_config)
            self.assertEqual(first['missing'], ['MISSING:photos/portrait_1880.jpg'])

            # A still-missing row stays reported (and eligible for a future
            # --with-exif rematch) rather than being silently dropped, and it
            # is never wrapped in a second MISSING: prefix.
            second = photoindex.run_reconcile(archive, fha_config)
            self.assertEqual(second['missing'], ['MISSING:photos/portrait_1880.jpg'])

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM photos WHERE path LIKE 'MISSING:MISSING:%'"
                ).fetchone()[0]
                self.assertEqual(count, 0)
            finally:
                conn.close()

    def test_reconcile_recomputes_group_date_after_its_dated_variant_goes_missing(self) -> None:
        # A group's edtf_resolved must never keep reflecting a variant that
        # reconcile just flagged MISSING: - _move_cached_path renames path
        # text only, so without _recompute_group_fields the group's resolved
        # date would silently survive its own source file's disappearance.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                rows = {'portrait_1880.jpg': {
                    'Keywords': ['DATE: Y!'],
                    'DateTimeOriginal': '1955:01:01 00:00:00',
                }}
                return [{'SourceFile': str(p), **rows.get(p.name, {})} for p in paths]

            photoindex._run_exiftool = fake_exiftool
            photoindex.run_scan(archive, cfg)

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                before = conn.execute(
                    "SELECT edtf_resolved FROM photo_groups WHERE group_id LIKE 'STEM:%portrait_1880%'"
                ).fetchone()[0]
                self.assertEqual(before, '1955')
            finally:
                conn.close()

            # The dated front scan vanishes; the undated back scan survives.
            (archive / 'photos' / 'portrait_1880.jpg').unlink()
            result = photoindex.run_reconcile(archive, cfg, with_exif=False)
            self.assertEqual(result['missing'], ['MISSING:photos/portrait_1880.jpg'])

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                after = conn.execute(
                    "SELECT edtf_resolved FROM photo_groups WHERE group_id LIKE 'STEM:%portrait_1880%'"
                ).fetchone()[0]
                self.assertIsNone(after)
            finally:
                conn.close()

    def test_reconcile_clears_date_conflict_when_conflicting_variant_goes_missing(self) -> None:
        # A group's date_conflict badge must clear once the variant it
        # conflicted with is gone, not keep flagging a conflict against a
        # file that no longer exists.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                rows = {
                    'portrait_1880.jpg': {
                        'Keywords': ['DATE: Y!'],
                        'DateTimeOriginal': '1850:01:01 00:00:00',
                    },
                    'portrait_1880-back.jpg': {
                        'Keywords': ['DATE: Y!'],
                        'DateTimeOriginal': '1950:01:01 00:00:00',
                    },
                }
                return [{'SourceFile': str(p), **rows.get(p.name, {})} for p in paths]

            photoindex._run_exiftool = fake_exiftool
            photoindex.run_scan(archive, cfg)

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                before = conn.execute(
                    "SELECT date_conflict FROM photo_groups WHERE group_id LIKE 'STEM:%portrait_1880%'"
                ).fetchone()[0]
                self.assertEqual(before, 1)
            finally:
                conn.close()

            # The non-primary back scan (1950) vanishes; only 1850 remains.
            (archive / 'photos' / 'portrait_1880-back.jpg').unlink()
            result = photoindex.run_reconcile(archive, cfg, with_exif=False)
            self.assertEqual(result['missing'], ['MISSING:photos/portrait_1880-back.jpg'])

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                row = conn.execute(
                    "SELECT edtf_resolved, date_conflict FROM photo_groups "
                    "WHERE group_id LIKE 'STEM:%portrait_1880%'"
                ).fetchone()
                self.assertEqual(row[0], '1850')
                # One dated variant left = nothing to compare: the three-state
                # date_conflict (#40) records that as NULL/unknown, not as the
                # affirmative "compared and they agree" that 0 now means.
                self.assertIsNone(row[1])
            finally:
                conn.close()

    def test_ordinary_scan_preserves_missing_row_for_a_later_exif_rematch(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            photoindex._run_exiftool = lambda paths: [{'SourceFile': str(p)} for p in paths]
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            (archive / 'photos' / 'portrait_1880.jpg').unlink()
            fha_config = {'roots': {'photos': 'photos'}}
            result = photoindex.run_reconcile(archive, fha_config)
            self.assertEqual(result['missing'], ['MISSING:photos/portrait_1880.jpg'])

            # An ordinary scan run between a no-exif reconcile and a later
            # --with-exif retry must not purge the MISSING: row - that key
            # never matches a real on-disk alias, so a naive cache-removal
            # pass would otherwise erase the source_id/path history the
            # later rematch needs.
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                row = conn.execute(
                    "SELECT 1 FROM photos WHERE path='MISSING:photos/portrait_1880.jpg'"
                ).fetchone()
                self.assertIsNotNone(row)
            finally:
                conn.close()

    def test_ordinary_scan_drops_missing_row_once_its_alias_is_restored(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            photoindex._run_exiftool = lambda paths: [{'SourceFile': str(p)} for p in paths]
            fha_config = {'roots': {'photos': 'photos'}}
            photoindex.run_scan(archive, fha_config)

            photo = archive / 'photos' / 'portrait_1880.jpg'
            saved = photo.read_bytes()
            photo.unlink()
            result = photoindex.run_reconcile(archive, fha_config)
            self.assertEqual(result['missing'], ['MISSING:photos/portrait_1880.jpg'])

            # The file reappears at the exact alias the MISSING: row
            # remembers. An ordinary scan re-discovers it as a fresh,
            # untracked file and must also drop the now-superseded MISSING:
            # row - otherwise the two rows fight over the same group/primary
            # path and `find`/triage can keep surfacing the dead row.
            photo.write_bytes(saved)
            photoindex.run_scan(archive, fha_config)

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                missing_row = conn.execute(
                    "SELECT 1 FROM photos WHERE path='MISSING:photos/portrait_1880.jpg'"
                ).fetchone()
                self.assertIsNone(missing_row)
                restored_row = conn.execute(
                    "SELECT 1 FROM photos WHERE path='photos/portrait_1880.jpg'"
                ).fetchone()
                self.assertIsNotNone(restored_row)
            finally:
                conn.close()

    def test_scan_sweeps_a_missing_row_whose_subtree_is_now_ignored(self) -> None:
        # The other escape from the MISSING: preservation branch. A vanished
        # photo keeps its row so a later `reconcile --with-exif` can heal it -
        # but once its former subtree is in photos_ignore, nothing will ever
        # heal it (the ignore-aware walk guarantees that alias never comes
        # back), while its caption, keywords and person matches stay
        # searchable. photos_ignore is a freshness dependency, so the edit
        # marks the catalog stale and asks for a rescan; without this sweep
        # that rescan cannot resolve the staleness for these rows.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            bulk = archive / 'photos' / 'Bulk Export'
            bulk.mkdir()
            (bulk / 'recent_0001.jpg').write_bytes(b'x')

            photoindex._run_exiftool = lambda paths: [
                {'SourceFile': str(p), 'Caption-Abstract': 'a bulk export scan'}
                for p in paths
            ]
            plain = {'roots': {'photos': 'photos'}}
            photoindex.run_scan(archive, plain)

            # The file goes away, so reconcile parks its row under the
            # synthetic key rather than dropping the metadata.
            (bulk / 'recent_0001.jpg').unlink()
            result = photoindex.run_reconcile(archive, plain)
            self.assertEqual(
                result['missing'], ['MISSING:photos/Bulk Export/recent_0001.jpg'])

            # A second MISSING: row OUTSIDE the ignored subtree - the control.
            portrait = archive / 'photos' / 'portrait_1880.jpg'
            saved = portrait.read_bytes()
            portrait.unlink()
            photoindex.run_reconcile(archive, plain)

            ignored = {'roots': {'photos': 'photos'}, 'photos_ignore': ['Bulk Export']}
            photoindex.run_scan(archive, ignored)

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                self.assertIsNone(conn.execute(
                    "SELECT 1 FROM photos WHERE path LIKE 'MISSING:%Bulk Export%'"
                ).fetchone())
                # Its text went with it: an ignored subtree must not keep
                # answering `fha find --text`.
                self.assertIsNone(conn.execute(
                    "SELECT 1 FROM photo_fts WHERE path LIKE 'MISSING:%Bulk Export%'"
                ).fetchone())
                # The control row is untouched - only the ignored subtree is swept.
                self.assertIsNotNone(conn.execute(
                    "SELECT 1 FROM photos WHERE path='MISSING:photos/portrait_1880.jpg'"
                ).fetchone())
            finally:
                conn.close()

            # The symmetric half: with the pattern gone the subtree is no
            # longer ignored, so a MISSING: row inside it is bookkeeping again
            # and a scan must preserve it exactly like any other.
            (bulk / 'recent_0002.jpg').write_bytes(b'y')
            photoindex.run_scan(archive, plain)
            (bulk / 'recent_0002.jpg').unlink()
            result = photoindex.run_reconcile(archive, plain)
            self.assertIn('MISSING:photos/Bulk Export/recent_0002.jpg', result['missing'])
            portrait.write_bytes(saved)
            photoindex.run_scan(archive, plain)

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                self.assertIsNotNone(conn.execute(
                    "SELECT 1 FROM photos "
                    "WHERE path='MISSING:photos/Bulk Export/recent_0002.jpg'"
                ).fetchone())
            finally:
                conn.close()

    def test_reconcile_does_not_report_an_ignored_subtree_as_lost_photos(self) -> None:
        # The other walker the same knob has to reach. photos_ignore prunes
        # reconcile's disk walk (an ignored file is never offered as a rematch
        # candidate or counted as new), so without the same rule on the cached
        # side every row for a just-excluded subtree looks like a photo that
        # vanished: reconcile would flag them MISSING: and warn that the
        # library has lost files, when nothing was lost and the human merely
        # asked the archive to stop looking there. Membership is the scan's to
        # settle; reconcile says nothing about it.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            bulk = archive / 'photos' / 'Bulk Export'
            bulk.mkdir()
            (bulk / 'recent_0001.jpg').write_bytes(b'x')
            photoindex._run_exiftool = lambda paths: [
                {'SourceFile': str(p)} for p in paths
            ]
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            ignored = {'roots': {'photos': 'photos'}, 'photos_ignore': ['Bulk Export']}
            result = photoindex.run_reconcile(archive, ignored)
            self.assertEqual(result['missing'], [])
            self.assertEqual(result.exit_code, EXIT_CLEAN)

            # A photo that really did vanish is still reported, ignore list or not.
            (archive / 'photos' / 'portrait_1880.jpg').unlink()
            result = photoindex.run_reconcile(archive, ignored)
            self.assertEqual(result['missing'], ['MISSING:photos/portrait_1880.jpg'])

    def test_reconcile_with_exif_can_later_rematch_an_already_missing_row(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))

            # Real exiftool reads embedded metadata, unaffected by a later
            # rename, so the fake matches on either filename the photo has
            # carried rather than just its name at scan time.
            def fake_exiftool(paths: list[Path]) -> list[dict]:
                tagged = {'wedding_1902.jpg', 'wedding_renamed.jpg'}
                rows = {name: {'Keywords': ['SOURCE: S-123456789a']} for name in tagged}
                return [{'SourceFile': str(p), **rows.get(p.name, {})} for p in paths]

            photoindex._run_exiftool = fake_exiftool
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            old = archive / 'photos' / 'wedding_1902.jpg'
            new = archive / 'photos' / 'wedding_renamed.jpg'
            old.rename(new)

            fha_config = {'roots': {'photos': 'photos'}}
            first = photoindex.run_reconcile(archive, fha_config, with_exif=False)
            self.assertEqual(first['missing'], ['MISSING:photos/wedding_1902.jpg'])

            # A row already flagged MISSING: on a previous (no-exif) run must
            # still be a rematch candidate once --with-exif is used later.
            second = photoindex.run_reconcile(archive, fha_config, with_exif=True)
            self.assertEqual(
                second['rematched'], [('MISSING:photos/wedding_1902.jpg', 'photos/wedding_renamed.jpg')],
            )
            self.assertEqual(second['missing'], [])

    def test_reconcile_rematch_with_no_remaining_untracked_still_stays_stale(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                tagged = {'wedding_1902.jpg', 'wedding_renamed.jpg'}
                rows = {name: {'Keywords': ['SOURCE: S-123456789a']} for name in tagged}
                return [{'SourceFile': str(p), **rows.get(p.name, {})} for p in paths]

            photoindex._run_exiftool = fake_exiftool
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            old = archive / 'photos' / 'wedding_1902.jpg'
            new = archive / 'photos' / 'wedding_renamed.jpg'
            old.rename(new)

            # Simulate content edited at move time: the file's real mtime now
            # postdates the cache row's stored mtime, but the row's mtime/size
            # columns are never refreshed by a path rename.
            edited = (archive / '.cache' / 'photos.sqlite').stat().st_mtime + 10
            os.utime(new, (edited, edited))

            fha_config = {'roots': {'photos': 'photos'}}
            result = photoindex.run_reconcile(archive, fha_config, with_exif=True)
            self.assertEqual(
                result['rematched'], [('photos/wedding_1902.jpg', 'photos/wedding_renamed.jpg')],
            )
            self.assertEqual(result['new_count'], 0)

            # Even though nothing is left untracked, a rematched file whose
            # content may have changed must keep the catalog 'stale' until an
            # ordinary scan re-scrapes it - otherwise find/doctor would report
            # a fresh index pointing at outdated caption/date metadata.
            status, _lag = photoindex.photoindex_status(archive, fha_config)
            self.assertEqual(status, 'stale')

    def test_reconcile_survives_untracked_file_disappearing_before_mtime_pullback(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            photoindex._run_exiftool = lambda paths: [{'SourceFile': str(p)} for p in paths]
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            shutil.copy(
                archive / 'photos' / 'portrait_1880.jpg',
                archive / 'photos' / 'brand_new.jpg',
            )

            # An external/removable photo root can lose a file (or make it
            # unreadable) between the initial on-disk listing and the
            # post-commit mtime pullback pass; that race must degrade to
            # "skip the pullback", not a raw OSError traceback after the
            # cache mutation has already landed. Swap in a stand-in for the
            # untracked file's Path whose .stat() always raises, mirroring
            # exactly what reconcile sees if the file vanishes mid-run,
            # without disturbing the cache-mutation logic exercised above it.
            class _VanishingStat:
                def stat(self) -> 'os.stat_result':
                    raise OSError('vanished mid-reconcile')

            orig_on_disk_aliases = photoindex._on_disk_aliases

            def patched_on_disk_aliases(photos_root, fha_config, archive_root,
                                        unreadable_dirs=None):
                aliases = orig_on_disk_aliases(
                    photos_root, fha_config, archive_root, unreadable_dirs)
                aliases['photos/brand_new.jpg'] = _VanishingStat()
                return aliases

            photoindex._on_disk_aliases = patched_on_disk_aliases
            try:
                result = photoindex.run_reconcile(archive, {'roots': {'photos': 'photos'}})
            finally:
                photoindex._on_disk_aliases = orig_on_disk_aliases

            self.assertEqual(result['new_count'], 1)

    def test_reconcile_dry_run_reports_plan_without_mutating_cache(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            photoindex._run_exiftool = lambda paths: [{'SourceFile': str(p)} for p in paths]
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            (archive / 'photos' / 'portrait_1880.jpg').unlink()

            result = photoindex.run_reconcile(
                archive, {'roots': {'photos': 'photos'}}, dry_run=True,
            )
            self.assertEqual(result['missing'], ['MISSING:photos/portrait_1880.jpg'])

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                row = conn.execute(
                    "SELECT 1 FROM photos WHERE path='photos/portrait_1880.jpg'"
                ).fetchone()
                self.assertIsNotNone(row)
                still_missing = conn.execute(
                    "SELECT 1 FROM photos WHERE path='MISSING:photos/portrait_1880.jpg'"
                ).fetchone()
                self.assertIsNone(still_missing)
            finally:
                conn.close()

    def test_reconcile_refuses_when_photos_root_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            photoindex._run_exiftool = lambda paths: [{'SourceFile': str(p)} for p in paths]
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            shutil.rmtree(archive / 'photos')

            result = photoindex.run_reconcile(archive, {'roots': {'photos': 'photos'}})
            self.assertFalse(result['root_found'])
            self.assertEqual(result['missing'], [])
            self.assertEqual(result['new_count'], 0)

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                row = conn.execute(
                    "SELECT 1 FROM photos WHERE path='photos/portrait_1880.jpg'"
                ).fetchone()
                self.assertIsNotNone(row)
            finally:
                conn.close()

    def test_reconcile_keeps_status_stale_when_new_files_remain_unindexed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            photoindex._run_exiftool = lambda paths: [{'SourceFile': str(p)} for p in paths]
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            # A rename triggers a real cache mutation (missing-flagging) in
            # the same run that also leaves an untracked new file behind.
            (archive / 'photos' / 'wedding_1902.jpg').rename(
                archive / 'photos' / 'wedding_renamed.jpg',
            )
            shutil.copy(
                archive / 'photos' / 'portrait_1880.jpg',
                archive / 'photos' / 'brand_new.jpg',
            )
            fha_config = {'roots': {'photos': 'photos'}}

            result = photoindex.run_reconcile(archive, fha_config)
            self.assertEqual(result['new_count'], 2)

            status, _lag = photoindex.photoindex_status(archive, fha_config)
            self.assertEqual(status, 'stale')

    def test_reconcile_new_untracked_file_is_reported_not_scraped(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            photoindex._run_exiftool = lambda paths: [{'SourceFile': str(p)} for p in paths]
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            shutil.copy(
                archive / 'photos' / 'portrait_1880.jpg',
                archive / 'photos' / 'brand_new.jpg',
            )

            result = photoindex.run_reconcile(archive, {'roots': {'photos': 'photos'}})

            self.assertEqual(result['new_count'], 1)
            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                row = conn.execute(
                    "SELECT 1 FROM photos WHERE path='photos/brand_new.jpg'"
                ).fetchone()
                self.assertIsNone(row)
            finally:
                conn.close()

    def test_reconcile_with_exif_attaches_untracked_source_tagged_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            photoindex._run_exiftool = lambda paths: [{'SourceFile': str(p)} for p in paths]
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            shutil.copy(
                archive / 'photos' / 'portrait_1880.jpg',
                archive / 'photos' / 'brand_new.jpg',
            )

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                return [
                    {'SourceFile': str(p), 'Keywords': ['SOURCE: S-aaaaaaaaaa']}
                    if p.name == 'brand_new.jpg' else {'SourceFile': str(p)}
                    for p in paths
                ]

            photoindex._run_exiftool = fake_exiftool
            result = photoindex.run_reconcile(
                archive, {'roots': {'photos': 'photos'}}, with_exif=True,
            )

            self.assertEqual(result['new_count'], 1)
            self.assertEqual(
                result['new_sourced'], {'s-aaaaaaaaaa': ['photos/brand_new.jpg']},
            )
            self.assertEqual(result['new_unsourced'], [])

    def test_reconcile_with_exif_batches_untracked_files_through_exiftool(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            photoindex._run_exiftool = lambda paths: [{'SourceFile': str(p)} for p in paths]
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            for i in range(5):
                shutil.copy(
                    archive / 'photos' / 'portrait_1880.jpg',
                    archive / 'photos' / f'brand_new_{i}.jpg',
                )

            call_sizes: list[int] = []

            def counting_exiftool(paths: list[Path]) -> list[dict]:
                call_sizes.append(len(paths))
                return [{'SourceFile': str(p)} for p in paths]

            photoindex._run_exiftool = counting_exiftool
            orig_batch_size = photoindex._EXIFTOOL_BATCH_SIZE
            photoindex._EXIFTOOL_BATCH_SIZE = 2
            try:
                result = photoindex.run_reconcile(
                    archive, {'roots': {'photos': 'photos'}}, with_exif=True,
                )
            finally:
                photoindex._EXIFTOOL_BATCH_SIZE = orig_batch_size

            # 5 untracked files with a batch size of 2 must be scraped across
            # multiple bounded exiftool calls, not one command line sized to
            # the whole untracked set.
            self.assertEqual(call_sizes, [2, 2, 1])
            self.assertEqual(result['new_count'], 5)

    def test_reconcile_on_absent_index_reports_absent_status(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            result = photoindex.run_reconcile(archive, {'roots': {'photos': 'photos'}})
            self.assertEqual(result['status'], 'absent')

    def test_cmd_reconcile_cli_propagates_exiftool_failure(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            photoindex._run_exiftool = lambda paths: [{'SourceFile': str(p)} for p in paths]
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            (archive / 'photos' / 'wedding_1902.jpg').rename(
                archive / 'photos' / 'wedding_renamed.jpg'
            )

            def broken_exiftool(paths: list[Path]) -> list[dict]:
                raise RuntimeError('fha photoindex requires exiftool on PATH')

            photoindex._run_exiftool = broken_exiftool

            args = type('Args', (), {'root': str(archive), 'with_exif': True})()
            code = photoindex._cmd_reconcile(args)

            self.assertEqual(code, photoindex.EXIT_FAILURE)

    def test_cmd_reconcile_cli_reports_missing_with_warning_exit(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            photoindex._run_exiftool = lambda paths: [{'SourceFile': str(p)} for p in paths]
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})
            (archive / 'photos' / 'portrait_1880.jpg').unlink()

            args = type('Args', (), {'root': str(archive), 'with_exif': False})()
            code = photoindex._cmd_reconcile(args)

            self.assertEqual(code, photoindex.EXIT_WARNINGS)

    # ── tag-person (BUILD.md M3.4) ────────────────────────────────────────

    def _scan_with_face_tag_fixture(self, archive: Path) -> None:
        def fake_exiftool(paths: list[Path]) -> list[dict]:
            rows = {
                'family_reunion.jpg': {
                    'RegionInfo': {
                        'RegionList': [{'Name': 'Grandma', 'Type': 'Face'}],
                    },
                },
            }
            return [{'SourceFile': str(p), **rows.get(p.name, {})} for p in paths]

        people_dir = archive / 'people'
        people_dir.mkdir(exist_ok=True)
        (people_dir / 'grandma_P-de957bcda1.md').write_text(
            '---\nid: P-de957bcda1\n---\n', encoding='utf-8',
        )

        photoindex._run_exiftool = fake_exiftool
        photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

    def test_tag_person_plan_from_face_tag_returns_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_face_tag_fixture(archive)

            plan = photoindex.run_tag_person_plan(
                archive, {'roots': {'photos': 'photos'}}, 'P-de957bcda1',
                from_face_tag='Grandma',
            )

            self.assertEqual(plan['candidates'], ['photos/family_reunion.jpg'])
            self.assertEqual(plan['already_tagged'], [])

    def test_tag_person_plan_excludes_already_tagged(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_face_tag_fixture(archive)

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            conn.execute(
                "INSERT INTO photo_keywords(path, keyword) "
                "VALUES ('photos/family_reunion.jpg', 'p-de957bcda1')"
            )
            conn.commit()
            conn.close()

            plan = photoindex.run_tag_person_plan(
                archive, {'roots': {'photos': 'photos'}}, 'P-de957bcda1',
                from_face_tag='Grandma',
            )

            self.assertEqual(plan['candidates'], [])
            self.assertEqual(plan['already_tagged'], ['photos/family_reunion.jpg'])

    def test_tag_person_plan_does_not_skip_group_sibling_without_own_keyword(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_face_tag_fixture(archive)

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            # Simulate _rebuild_photo_people's group propagation: photo_people
            # carries the pid-keyword match for a sibling that never actually
            # had the keyword written into its own file.
            conn.execute(
                "INSERT INTO photo_people(path, person_ref, via) "
                "VALUES ('photos/family_reunion.jpg', 'p-de957bcda1', 'pid-keyword')"
            )
            conn.commit()
            conn.close()

            plan = photoindex.run_tag_person_plan(
                archive, {'roots': {'photos': 'photos'}}, 'P-de957bcda1',
                from_face_tag='Grandma',
            )

            self.assertEqual(plan['candidates'], ['photos/family_reunion.jpg'])
            self.assertEqual(plan['already_tagged'], [])

    def test_tag_person_plan_requires_exactly_one_selector(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_face_tag_fixture(archive)

            with self.assertRaises(ValueError):
                photoindex.run_tag_person_plan(
                    archive, {'roots': {'photos': 'photos'}}, 'P-de957bcda1',
                )
            with self.assertRaises(ValueError):
                photoindex.run_tag_person_plan(
                    archive, {'roots': {'photos': 'photos'}}, 'P-de957bcda1',
                    from_face_tag='Grandma', paths=['photos/family_reunion.jpg'],
                )

    def test_tag_person_plan_rejects_invalid_person_id(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_face_tag_fixture(archive)

            with self.assertRaises(ValueError):
                photoindex.run_tag_person_plan(
                    archive, {'roots': {'photos': 'photos'}}, 'S-123456789a',
                    from_face_tag='Grandma',
                )

    def test_tag_person_plan_rejects_unknown_person_id(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_face_tag_fixture(archive)

            with self.assertRaises(ValueError):
                photoindex.run_tag_person_plan(
                    archive, {'roots': {'photos': 'photos'}}, 'P-0000000000',
                    from_face_tag='Grandma',
                )

    def test_tag_person_plan_rejects_id_mentioned_only_in_note_text(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_face_tag_fixture(archive)

            # A P-id that appears only as a stray mention in a non-person-record
            # file (e.g. a research note) must not pass validation -- only an
            # actual people/ profile record names a real person.
            notes_dir = archive / 'notes'
            notes_dir.mkdir(exist_ok=True)
            (notes_dir / 'misc.md').write_text(
                'See P-aaaaaaaaaa for context.\n', encoding='utf-8',
            )

            with self.assertRaises(ValueError):
                photoindex.run_tag_person_plan(
                    archive, {'roots': {'photos': 'photos'}}, 'P-aaaaaaaaaa',
                    from_face_tag='Grandma',
                )

    def test_tag_person_plan_blocks_on_stale_index(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_face_tag_fixture(archive)
            # Touch the person record after the scan so the cache is stale.
            # Set an explicit future mtime rather than os.utime(path, None):
            # on a coarse/virtualized clock "now" can land exactly on the
            # cache's own last-write timestamp instead of strictly after it.
            db_mtime = (archive / '.cache' / 'photos.sqlite').stat().st_mtime
            future = db_mtime + 5
            os.utime(archive / 'people' / 'grandma_P-de957bcda1.md', (future, future))

            args = type('Args', (), {
                'root': str(archive), 'person_id': 'P-de957bcda1',
                'from_face_tag': 'Grandma', 'paths': None, 'dry_run': True,
            })()
            orig_apply = photoindex.apply_tag_person
            photoindex.apply_tag_person = lambda *a, **k: (_ for _ in ()).throw(
                AssertionError('apply_tag_person must not be called on a stale index')
            )
            try:
                code = photoindex._cmd_tag_person(args)
            finally:
                photoindex.apply_tag_person = orig_apply
            self.assertEqual(code, photoindex.EXIT_FAILURE)

    def test_tag_person_plan_resolves_explicit_paths(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_face_tag_fixture(archive)

            plan = photoindex.run_tag_person_plan(
                archive, {'roots': {'photos': 'photos'}}, 'P-de957bcda1',
                paths=['photos/family_reunion.jpg'],
            )

            self.assertEqual(plan['candidates'], ['photos/family_reunion.jpg'])

    def test_tag_person_plan_dedupes_repeated_paths(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_face_tag_fixture(archive)

            plan = photoindex.run_tag_person_plan(
                archive, {'roots': {'photos': 'photos'}}, 'P-de957bcda1',
                paths=['photos/family_reunion.jpg', 'photos/family_reunion.jpg'],
            )

            self.assertEqual(plan['candidates'], ['photos/family_reunion.jpg'])

    def test_tag_person_plan_rejects_unknown_path(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_face_tag_fixture(archive)

            with self.assertRaises(ValueError):
                photoindex.run_tag_person_plan(
                    archive, {'roots': {'photos': 'photos'}}, 'P-de957bcda1',
                    paths=['photos/does_not_exist.jpg'],
                )

    def test_apply_tag_person_writes_keyword_and_updates_cache(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_face_tag_fixture(archive)

            calls: list[tuple[list[Path], str]] = []

            def _fake_write(paths: list[Path], kw: str) -> dict:
                calls.append((paths, kw))
                return {p: None for p in paths}

            orig_write = photoindex._run_exiftool_write
            photoindex._run_exiftool_write = _fake_write
            try:
                result = photoindex.apply_tag_person(
                    archive, {'roots': {'photos': 'photos'}}, 'p-de957bcda1',
                    ['photos/family_reunion.jpg'],
                )
            finally:
                photoindex._run_exiftool_write = orig_write

            self.assertEqual(result['tagged'], ['photos/family_reunion.jpg'])
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1], 'P-de957bcda1')

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                people = conn.execute(
                    "SELECT person_ref, via FROM photo_people WHERE path='photos/family_reunion.jpg'"
                ).fetchall()
                self.assertEqual(people, [('p-de957bcda1', 'pid-keyword')])
                keywords = conn.execute(
                    "SELECT keyword FROM photo_keywords WHERE path='photos/family_reunion.jpg' "
                    "AND keyword='P-de957bcda1'"
                ).fetchall()
                self.assertEqual(len(keywords), 1)
            finally:
                conn.close()

    def test_apply_tag_person_refreshes_photo_fts(self) -> None:
        """The new P-id keyword must reach `photo_fts.keywords` immediately,
        or `find --text` on the P-id stays blind until the next full scan."""
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_face_tag_fixture(archive)

            orig_write = photoindex._run_exiftool_write
            photoindex._run_exiftool_write = lambda paths, kw: {p: None for p in paths}
            try:
                photoindex.apply_tag_person(
                    archive, {'roots': {'photos': 'photos'}}, 'p-de957bcda1',
                    ['photos/family_reunion.jpg'],
                )
            finally:
                photoindex._run_exiftool_write = orig_write

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                row = conn.execute(
                    "SELECT keywords FROM photo_fts WHERE path='photos/family_reunion.jpg'"
                ).fetchone()
                self.assertIn('P-de957bcda1', row[0])
            finally:
                conn.close()

    def test_apply_tag_person_partial_exiftool_failure_keeps_successful_writes_cached(self) -> None:
        """One file failing the embedded write must not discard the cache
        update for the other candidates that succeeded (AGENTS_TOOLING:
        partial success must be reported clearly, not swallowed)."""
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_face_tag_fixture(archive)

            def _fake_write(paths: list[Path], kw: str) -> dict:
                return {
                    p: ('locked file' if p.name == 'wedding_1902.jpg' else None)
                    for p in paths
                }

            orig_write = photoindex._run_exiftool_write
            photoindex._run_exiftool_write = _fake_write
            try:
                result = photoindex.apply_tag_person(
                    archive, {'roots': {'photos': 'photos'}}, 'p-de957bcda1',
                    ['photos/family_reunion.jpg', 'photos/wedding_1902.jpg'],
                )
            finally:
                photoindex._run_exiftool_write = orig_write

            self.assertEqual(result['tagged'], ['photos/family_reunion.jpg'])
            self.assertEqual(result['failed'], [('photos/wedding_1902.jpg', 'locked file')])

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                tagged = conn.execute(
                    "SELECT 1 FROM photo_people WHERE path='photos/family_reunion.jpg' "
                    "AND person_ref='p-de957bcda1' AND via='pid-keyword'"
                ).fetchone()
                self.assertIsNotNone(tagged)
                not_tagged = conn.execute(
                    "SELECT 1 FROM photo_people WHERE path='photos/wedding_1902.jpg' "
                    "AND person_ref='p-de957bcda1'"
                ).fetchone()
                self.assertIsNone(not_tagged)
            finally:
                conn.close()

    def test_apply_tag_person_reports_cache_failure_after_in_file_write(self) -> None:
        """A cache failure after the original file is already written must
        surface as a RuntimeError naming the already-tagged path, not an
        uncaught sqlite3.Error traceback."""
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_face_tag_fixture(archive)

            orig_write = photoindex._run_exiftool_write
            photoindex._run_exiftool_write = lambda paths, kw: {p: None for p in paths}
            orig_rebuild = photoindex._rebuild_photo_people
            photoindex._rebuild_photo_people = lambda conn, root: (
                _ for _ in ()).throw(sqlite3.OperationalError('database is locked'))
            try:
                with self.assertRaises(RuntimeError) as ctx:
                    photoindex.apply_tag_person(
                        archive, {'roots': {'photos': 'photos'}}, 'p-de957bcda1',
                        ['photos/family_reunion.jpg'],
                    )
                self.assertIn('photos/family_reunion.jpg', str(ctx.exception))
            finally:
                photoindex._run_exiftool_write = orig_write
                photoindex._rebuild_photo_people = orig_rebuild

    def test_apply_tag_person_records_tagged_before_cache_insert_attempt(self) -> None:
        """`tagged` must reflect every file whose exiftool write succeeded
        before its own cache insert is attempted, so a cache failure on a
        later candidate's insert still names every already-written file in
        the RuntimeError's recovery list - not just the earlier ones whose
        insert also happened to succeed first."""
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_face_tag_fixture(archive)

            orig_write = photoindex._run_exiftool_write
            photoindex._run_exiftool_write = lambda paths, kw: {p: None for p in paths}

            orig_connect = sqlite3.connect

            class _FailingOnSecondInsert:
                def __init__(self, real_conn: sqlite3.Connection) -> None:
                    self._real = real_conn
                    self._inserts = 0

                def execute(self, sql, *args, **kwargs):
                    if sql.startswith('INSERT INTO photo_keywords'):
                        self._inserts += 1
                        if self._inserts == 2:
                            raise sqlite3.OperationalError('database is locked')
                    return self._real.execute(sql, *args, **kwargs)

                def __getattr__(self, name):
                    return getattr(self._real, name)

            sqlite3.connect = lambda path: _FailingOnSecondInsert(orig_connect(path))
            try:
                with self.assertRaises(RuntimeError) as ctx:
                    photoindex.apply_tag_person(
                        archive, {'roots': {'photos': 'photos'}}, 'p-de957bcda1',
                        ['photos/family_reunion.jpg', 'photos/wedding_1902.jpg'],
                    )
            finally:
                sqlite3.connect = orig_connect
                photoindex._run_exiftool_write = orig_write

            self.assertIn('photos/family_reunion.jpg', str(ctx.exception))
            self.assertIn('photos/wedding_1902.jpg', str(ctx.exception))

    def test_cmd_tag_person_dry_run_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_face_tag_fixture(archive)

            args = type('Args', (), {
                'root': str(archive), 'person_id': 'P-de957bcda1',
                'from_face_tag': 'Grandma', 'paths': None, 'dry_run': True,
            })()

            orig_apply = photoindex.apply_tag_person
            photoindex.apply_tag_person = lambda *a, **k: (_ for _ in ()).throw(
                AssertionError('apply_tag_person must not be called in --dry-run')
            )
            try:
                code = photoindex._cmd_tag_person(args)
            finally:
                photoindex.apply_tag_person = orig_apply

            self.assertEqual(code, photoindex.EXIT_CLEAN)

    def test_cmd_tag_person_declines_confirm_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_face_tag_fixture(archive)

            args = type('Args', (), {
                'root': str(archive), 'person_id': 'P-de957bcda1',
                'from_face_tag': 'Grandma', 'paths': None, 'dry_run': False,
            })()

            orig_input = builtins.input
            builtins.input = lambda prompt='': 'n'
            try:
                code = photoindex._cmd_tag_person(args)
            finally:
                builtins.input = orig_input

            self.assertEqual(code, photoindex.EXIT_CLEAN)
            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM photo_people WHERE via='pid-keyword'"
                ).fetchone()[0]
                self.assertEqual(count, 0)
            finally:
                conn.close()

    def test_cmd_tag_person_confirms_and_writes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan_with_face_tag_fixture(archive)

            args = type('Args', (), {
                'root': str(archive), 'person_id': 'P-de957bcda1',
                'from_face_tag': 'Grandma', 'paths': None, 'dry_run': False,
            })()

            orig_write = photoindex._run_exiftool_write
            photoindex._run_exiftool_write = lambda paths, kw: {p: None for p in paths}
            orig_input = builtins.input
            builtins.input = lambda prompt='': 'y'
            try:
                code = photoindex._cmd_tag_person(args)
            finally:
                photoindex._run_exiftool_write = orig_write
                builtins.input = orig_input

            self.assertEqual(code, photoindex.EXIT_CLEAN)
            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                row = conn.execute(
                    "SELECT via FROM photo_people WHERE path='photos/family_reunion.jpg' "
                    "AND person_ref='p-de957bcda1'"
                ).fetchone()
                self.assertEqual(row[0], 'pid-keyword')
            finally:
                conn.close()


    # ── source-people resolution ───────────────────────────────────────────────

    def test_source_people_resolution_from_source_record(self) -> None:
        """A photo with source_id pointing to a record with people: list gets
        a source-people entry in photo_people even with no P-id keyword."""
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            fha_config = {'roots': {'photos': 'photos'}}

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                rows = {
                    'portrait_1880.jpg': {},
                    'portrait_1880-back.jpg': {},
                    # wedding carries SOURCE keyword so source_id gets populated
                    'wedding_1902.jpg': {'Keywords': ['SOURCE: S-123456789a']},
                    'family_reunion.jpg': {},
                }
                return [{'SourceFile': str(p), **rows[p.name]} for p in paths]

            photoindex._run_exiftool = fake_exiftool
            photoindex.run_scan(archive, fha_config)

            # Create source record with people: list
            sources_dir = archive / 'sources'
            sources_dir.mkdir(exist_ok=True)
            source_record = sources_dir / 'wedding_1902_S-123456789a.md'
            source_record.write_text(
                '---\ntitle: Wedding 1902\npeople:\n  - P-de957bcda1\n---\n',
                encoding='utf-8',
            )

            # Re-run scan - photos unchanged (mtime), but _rebuild_photo_people re-reads source files.
            photoindex.run_scan(archive, fha_config, full=True)

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                row = conn.execute(
                    "SELECT person_ref, via FROM photo_people "
                    "WHERE path LIKE '%wedding_1902.jpg'"
                ).fetchone()
                self.assertIsNotNone(row, 'Expected a photo_people entry for wedding_1902.jpg')
                self.assertEqual(row[0], 'p-de957bcda1')
                self.assertEqual(row[1], 'source-people')
            finally:
                conn.close()

    def test_source_people_resolves_name_style_wikilink_via_index(self) -> None:
        """A `people: ["[[Ken Smith]]"]` name link resolves to its P-id through the
        clash-aware alias map in index.sqlite. The aliases table lives in the index
        DB, not the photos.sqlite connection _rebuild_photo_people holds, so this
        would crash (no such table: aliases) before the resolver was split out."""
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            fha_config = {'roots': {'photos': 'photos'}}

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                rows = {
                    'portrait_1880.jpg': {},
                    'portrait_1880-back.jpg': {},
                    'wedding_1902.jpg': {'Keywords': ['SOURCE: S-123456789a']},
                    'family_reunion.jpg': {},
                }
                return [{'SourceFile': str(p), **rows[p.name]} for p in paths]

            photoindex._run_exiftool = fake_exiftool
            photoindex.run_scan(archive, fha_config)

            # Person record (so the name resolves) + source record naming the
            # person by NAME, not P-id.
            people_dir = archive / 'people'
            people_dir.mkdir(exist_ok=True)
            (people_dir / 'smith__ken_P-de957bcda1.md').write_text(
                '---\nid: P-de957bcda1\nname: Ken Smith\nliving: false\n---\n',
                encoding='utf-8',
            )
            sources_dir = archive / 'sources'
            sources_dir.mkdir(exist_ok=True)
            (sources_dir / 'wedding_1902_S-123456789a.md').write_text(
                '---\nid: S-123456789a\ntitle: Wedding 1902\nsource_type: photo\n'
                'people: ["[[Ken Smith]]"]\n---\n## Claims\n```yaml\n```\n',
                encoding='utf-8',
            )

            # Build the index so the aliases table exists and is fresh, then
            # rebuild photo_people from it.
            index.build_index(archive, fha_config)
            photoindex.run_scan(archive, fha_config, full=True)

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                row = conn.execute(
                    "SELECT person_ref, via FROM photo_people "
                    "WHERE path LIKE '%wedding_1902.jpg'"
                ).fetchone()
                self.assertIsNotNone(row, 'name-style people link should resolve to a P-id')
                self.assertEqual(row[0], 'p-de957bcda1')
                self.assertEqual(row[1], 'source-people')
            finally:
                conn.close()

    def test_source_people_ambiguous_name_draws_no_edge(self) -> None:
        """A name shared by two people is a clash: like `fha index`, it must resolve
        to nothing rather than silently pick one person (SPEC §7)."""
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            fha_config = {'roots': {'photos': 'photos'}}

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                rows = {
                    'portrait_1880.jpg': {},
                    'portrait_1880-back.jpg': {},
                    'wedding_1902.jpg': {'Keywords': ['SOURCE: S-123456789a']},
                    'family_reunion.jpg': {},
                }
                return [{'SourceFile': str(p), **rows[p.name]} for p in paths]

            photoindex._run_exiftool = fake_exiftool
            photoindex.run_scan(archive, fha_config)

            people_dir = archive / 'people'
            people_dir.mkdir(exist_ok=True)
            (people_dir / 'smith__john_a_P-aaaaaaaaaa.md').write_text(
                '---\nid: P-aaaaaaaaaa\nname: John Smith\nliving: false\n---\n',
                encoding='utf-8',
            )
            (people_dir / 'smith__john_b_P-bbbbbbbbbb.md').write_text(
                '---\nid: P-bbbbbbbbbb\nname: John Smith\nliving: false\n---\n',
                encoding='utf-8',
            )
            sources_dir = archive / 'sources'
            sources_dir.mkdir(exist_ok=True)
            (sources_dir / 'wedding_1902_S-123456789a.md').write_text(
                '---\nid: S-123456789a\ntitle: Wedding 1902\nsource_type: photo\n'
                'people: ["[[John Smith]]"]\n---\n## Claims\n```yaml\n```\n',
                encoding='utf-8',
            )

            index.build_index(archive, fha_config)
            photoindex.run_scan(archive, fha_config, full=True)

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                rows = conn.execute(
                    "SELECT person_ref FROM photo_people "
                    "WHERE path LIKE '%wedding_1902.jpg' AND via = 'source-people'"
                ).fetchall()
                self.assertEqual(rows, [], 'ambiguous name must not resolve to any person')
            finally:
                conn.close()

    def test_source_people_authoritative_without_face_regions(self) -> None:
        """source-people resolves even when photo_face_regions is empty (no XMP regions)."""
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            fha_config = {'roots': {'photos': 'photos'}}

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                # wedding has no RegionInfo - no face regions at all
                rows = {
                    'portrait_1880.jpg': {},
                    'portrait_1880-back.jpg': {},
                    'wedding_1902.jpg': {'Keywords': ['SOURCE: S-123456789a']},
                    'family_reunion.jpg': {},
                }
                return [{'SourceFile': str(p), **rows[p.name]} for p in paths]

            photoindex._run_exiftool = fake_exiftool
            photoindex.run_scan(archive, fha_config)

            sources_dir = archive / 'sources'
            sources_dir.mkdir(exist_ok=True)
            (sources_dir / 'wedding_1902_S-123456789a.md').write_text(
                '---\ntitle: Wedding 1902\npeople:\n  - P-de957bcda1\n---\n',
                encoding='utf-8',
            )

            photoindex.run_scan(archive, fha_config, full=True)

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                # Verify no face regions for this photo
                regions = conn.execute(
                    "SELECT COUNT(*) FROM photo_face_regions WHERE path LIKE '%wedding_1902.jpg'"
                ).fetchone()[0]
                self.assertEqual(regions, 0)

                # And source-people still resolves
                row = conn.execute(
                    "SELECT person_ref, via FROM photo_people "
                    "WHERE path LIKE '%wedding_1902.jpg'"
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row[0], 'p-de957bcda1')
                self.assertEqual(row[1], 'source-people')
            finally:
                conn.close()

    def test_source_people_not_duplicated_when_also_pid_keyword(self) -> None:
        """If a P-id is already embedded as a keyword, source-people doesn't add a duplicate."""
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            fha_config = {'roots': {'photos': 'photos'}}

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                rows = {
                    'portrait_1880.jpg': {},
                    'portrait_1880-back.jpg': {},
                    # Both SOURCE keyword AND the P-id keyword already embedded
                    'wedding_1902.jpg': {
                        'Keywords': ['SOURCE: S-123456789a', 'P-de957bcda1'],
                    },
                    'family_reunion.jpg': {},
                }
                return [{'SourceFile': str(p), **rows[p.name]} for p in paths]

            photoindex._run_exiftool = fake_exiftool
            photoindex.run_scan(archive, fha_config)

            sources_dir = archive / 'sources'
            sources_dir.mkdir(exist_ok=True)
            (sources_dir / 'wedding_1902_S-123456789a.md').write_text(
                '---\ntitle: Wedding 1902\npeople:\n  - P-de957bcda1\n---\n',
                encoding='utf-8',
            )

            photoindex.run_scan(archive, fha_config, full=True)

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                rows = conn.execute(
                    "SELECT person_ref, via FROM photo_people "
                    "WHERE path LIKE '%wedding_1902.jpg'"
                ).fetchall()
                # Only one row - pid-keyword wins (same priority, deduped by _resolve)
                person_refs = [row[0] for row in rows]
                self.assertEqual(person_refs.count('p-de957bcda1'), 1)
                # The pid-keyword tier wins (not source-people) since pid-keyword is resolved first
                self.assertEqual(rows[0][1], 'pid-keyword')
            finally:
                conn.close()


    # ── Subtree filters vs. reconcile's MISSING: rows ────────────────────

    def _stage_missing_photos(self, archive: Path, cfg: dict) -> None:
        """Scan a small library, then let two of its photos vanish.

        Leaves the catalog holding one MISSING: row inside a SOURCE-keyed
        group that spans two folders (so the group's surviving member is
        OUTSIDE the subtree under test) and one MISSING: row that is a group
        of its own. Both carry the 'tintype' keyword so a filter can select
        them without depending on the subtree logic being tested.
        """
        woodbury = archive / 'photos' / 'Woodbury'
        woodbury.mkdir()
        elsewhere = archive / 'photos' / 'Elsewhere'
        elsewhere.mkdir()
        for path in (woodbury / 'attic_scan.jpg', woodbury / 'lone_print.jpg',
                     elsewhere / 'shoebox_scan.jpg'):
            path.write_bytes(b'x')

        rows = {
            'attic_scan.jpg': {'Keywords': ['SOURCE: S-123456789a', 'tintype']},
            'shoebox_scan.jpg': {'Keywords': ['SOURCE: S-123456789a', 'tintype']},
            'lone_print.jpg': {'Keywords': ['tintype']},
        }
        photoindex._run_exiftool = lambda paths: [
            {'SourceFile': str(p), **rows.get(p.name, {})} for p in paths
        ]
        photoindex.run_scan(archive, cfg)

        (woodbury / 'attic_scan.jpg').unlink()
        (woodbury / 'lone_print.jpg').unlink()
        result = photoindex.run_reconcile(archive, cfg)
        self.assertEqual(
            sorted(result['missing']),
            ['MISSING:photos/Woodbury/attic_scan.jpg',
             'MISSING:photos/Woodbury/lone_print.jpg'],
        )

    def test_under_filter_matches_a_reconciled_missing_photo(self) -> None:
        # Reconcile keeps a vanished photo queryable under a synthetic
        # 'MISSING:photos/…' key, and `find` deliberately keeps those rows
        # findable - so --under must compare on the alias underneath the
        # prefix. Otherwise the one filter that scopes a query to a folder is
        # the one filter a missing photo drops out of, including the group it
        # belongs to when its surviving variant lives elsewhere.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            self._stage_missing_photos(archive, cfg)

            res = photoindex.run_find(archive, cfg, keyword='tintype', under='Woodbury')
            self.assertEqual(
                [r['path'] for r in res['rows']],
                ['MISSING:photos/Woodbury/lone_print.jpg',
                 'photos/Elsewhere/shoebox_scan.jpg'],
            )

            # --files lists the vanished variant itself, not just its group.
            res = photoindex.run_find(
                archive, cfg, keyword='tintype', under='Woodbury', files=True)
            self.assertIn('MISSING:photos/Woodbury/attic_scan.jpg',
                          [r['path'] for r in res['rows']])

    def test_not_under_filter_excludes_a_reconciled_missing_photos_group(self) -> None:
        # The exclusion half of the same rule: a group with any variant inside
        # --not-under is dropped, and a MISSING: variant still counts as being
        # inside the folder it vanished from.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            self._stage_missing_photos(archive, cfg)

            res = photoindex.run_find(
                archive, cfg, keyword='tintype', not_under='Woodbury')
            self.assertEqual([r['path'] for r in res['rows']], [])

            # And the exclusion is genuinely about the folder, not about
            # missing rows in general: excluding the other folder leaves the
            # Woodbury-only group behind.
            res = photoindex.run_find(
                archive, cfg, keyword='tintype', not_under='Elsewhere')
            self.assertEqual(
                [r['path'] for r in res['rows']],
                ['MISSING:photos/Woodbury/lone_print.jpg'],
            )

    def test_scan_keeps_a_missing_variant_grouped_with_its_living_siblings(self) -> None:
        # A rescan after reconcile re-derives every group from the stored
        # paths. Parsing 'MISSING:photos/portrait_1880.jpg' raw would file the
        # vanished front scan under a folder called 'MISSING:photos' and split
        # it out of its own physical photo - losing the caption/date history
        # reconcile kept it for. The group must also refuse to take its date
        # back from the vanished variant, matching what reconcile itself
        # computed (_recompute_group_fields).
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                rows = {'portrait_1880.jpg': {
                    'Keywords': ['DATE: Y!'],
                    'DateTimeOriginal': '1955:01:01 00:00:00',
                }}
                return [{'SourceFile': str(p), **rows.get(p.name, {})} for p in paths]

            photoindex._run_exiftool = fake_exiftool
            photoindex.run_scan(archive, cfg)
            (archive / 'photos' / 'portrait_1880.jpg').unlink()
            photoindex.run_reconcile(archive, cfg)

            photoindex.run_scan(archive, cfg)

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                groups = dict(conn.execute(
                    "SELECT path, group_id FROM photos WHERE path LIKE '%portrait_1880%'"
                ).fetchall())
                self.assertEqual(
                    groups['MISSING:photos/portrait_1880.jpg'],
                    groups['photos/portrait_1880-back.jpg'],
                )
                primary, resolved = conn.execute(
                    'SELECT primary_path, edtf_resolved FROM photo_groups WHERE group_id=?',
                    (groups['photos/portrait_1880-back.jpg'],),
                ).fetchone()
                # The file a human can actually open leads the group, and the
                # vanished variant's 1955 is not resurrected as its date.
                self.assertEqual(primary, 'photos/portrait_1880-back.jpg')
                self.assertIsNone(resolved)
            finally:
                conn.close()

    def test_tag_person_never_offers_a_missing_photo(self) -> None:
        # tag-person writes a keyword INTO a file. A vanished photo keeps its
        # cached face regions, so the face-tag selector would otherwise
        # preview a write to a path that is not on disk; and a --paths
        # argument naming one must be refused with a next step, not handed to
        # exiftool.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                region = {'RegionInfo': {'RegionList': [
                    {'Name': 'Maggie', 'Type': 'Face'}]}}
                return [
                    {'SourceFile': str(p), **(region if p.name == 'portrait_1880.jpg' else {})}
                    for p in paths
                ]

            people_dir = archive / 'people'
            people_dir.mkdir(exist_ok=True)
            (people_dir / 'maggie_P-de957bcda1.md').write_text(
                '---\nid: P-de957bcda1\n---\n', encoding='utf-8',
            )

            photoindex._run_exiftool = fake_exiftool
            photoindex.run_scan(archive, cfg)
            (archive / 'photos' / 'portrait_1880.jpg').unlink()
            photoindex.run_reconcile(archive, cfg)

            with self.assertRaisesRegex(ValueError, 'no photo carries a face region'):
                photoindex.run_tag_person_plan(
                    archive, cfg, 'P-de957bcda1', from_face_tag='Maggie')
            with self.assertRaisesRegex(ValueError, 'not on disk'):
                photoindex.run_tag_person_plan(
                    archive, cfg, 'P-de957bcda1',
                    paths=['MISSING:photos/portrait_1880.jpg'])

    # ── Freshness watermark ──────────────────────────────────────────────

    def test_ignored_subtree_change_leaves_the_catalog_fresh(self) -> None:
        # photos_ignore prunes a subtree from the scan, so a file that changes
        # inside it changes nothing the catalog holds. Letting it drive the
        # freshness watermark would mark an otherwise-current catalog stale -
        # which makes `fha find --text` skip every cataloged photo caption
        # until a rescan that has nothing to do - and would walk the very
        # subtree the setting exists to avoid walking.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            bulk = archive / 'photos' / 'Bulk Export'
            bulk.mkdir()
            (bulk / 'recent_0001.jpg').write_bytes(b'x')
            cfg = {'roots': {'photos': 'photos'}, 'photos_ignore': ['Bulk Export']}

            photoindex._run_exiftool = lambda paths: [
                {'SourceFile': str(p)} for p in paths]
            photoindex.run_scan(archive, cfg)
            self.assertEqual(photoindex_status(archive, cfg)[0], 'fresh')

            # A change under the ignored subtree - new file, changed file, and
            # the directory mtime bump both of those carry.
            later = time.time() + 120
            (bulk / 'recent_0002.jpg').write_bytes(b'x')
            os.utime(bulk / 'recent_0001.jpg', (later, later))
            os.utime(bulk / 'recent_0002.jpg', (later, later))
            os.utime(bulk, (later, later))
            self.assertEqual(photoindex_status(archive, cfg)[0], 'fresh')

            # A change to a photo that IS catalogued still marks it stale, and
            # dropping the ignore pattern brings the subtree back into scope.
            self.assertEqual(
                photoindex_status(archive, {'roots': {'photos': 'photos'}})[0], 'stale')
            os.utime(archive / 'photos' / 'portrait_1880.jpg', (later, later))
            self.assertEqual(photoindex_status(archive, cfg)[0], 'stale')

    def _scan_with(self, archive: Path, cfg: dict) -> None:
        """Scan `archive` with every photo readable (the drift tests' setup)."""
        photoindex._run_exiftool = lambda paths: [
            {'SourceFile': str(p)} for p in paths]
        photoindex.run_scan(archive, cfg)

    def test_adding_a_photos_ignore_pattern_stales_the_catalog(self) -> None:
        # The catalog was built holding the bulk export; adding the pattern
        # means those rows should not be there any more. Nothing on disk
        # changed, and the pruned walk now cannot even see the files, so the
        # mtime watermark says 'fresh' while `fha find` keeps serving the rows
        # the setting was meant to remove. The stored build configuration is
        # what notices.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            bulk = archive / 'photos' / 'Bulk Export'
            bulk.mkdir()
            (bulk / 'recent_0001.jpg').write_bytes(b'x')
            plain = {'roots': {'photos': 'photos'}}
            self._scan_with(archive, plain)
            self.assertEqual(photoindex_status(archive, plain)[0], 'fresh')

            narrowed = {'roots': {'photos': 'photos'},
                        'photos_ignore': ['Bulk Export']}
            self.assertEqual(photoindex_status(archive, narrowed)[0], 'stale')

            # And the rescan the human is now told to run settles it.
            self._scan_with(archive, narrowed)
            self.assertEqual(photoindex_status(archive, narrowed)[0], 'fresh')

    def test_removing_a_photos_ignore_pattern_stales_the_catalog(self) -> None:
        # The direction that never heals on its own: the un-ignored files are
        # older than photos.sqlite, so they can never raise the watermark. Left
        # to mtimes alone this catalog reads 'fresh' forever while the photos
        # the human just brought back into scope stay uncatalogued.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            old_box = archive / 'photos' / 'Old Box'
            old_box.mkdir()
            hidden = old_box / 'grandpa_1912.jpg'
            hidden.write_bytes(b'x')
            long_ago = time.time() - 86400
            os.utime(hidden, (long_ago, long_ago))
            os.utime(old_box, (long_ago, long_ago))

            narrowed = {'roots': {'photos': 'photos'}, 'photos_ignore': ['Old Box']}
            self._scan_with(archive, narrowed)
            self.assertEqual(photoindex_status(archive, narrowed)[0], 'fresh')

            plain = {'roots': {'photos': 'photos'}}
            self.assertEqual(photoindex_status(archive, plain)[0], 'stale')

            self._scan_with(archive, plain)
            self.assertEqual(photoindex_status(archive, plain)[0], 'fresh')
            conn = sqlite3.connect(str(archive / '.cache' / 'photos.sqlite'))
            try:
                rows = [r[0] for r in conn.execute('SELECT path FROM photos')]
            finally:
                conn.close()
            self.assertIn('photos/Old Box/grandpa_1912.jpg', rows)

    def test_repointing_the_photos_root_stales_the_catalog(self) -> None:
        # Same class as photos_ignore: `roots: photos` decides which files the
        # catalog holds, the aliases stored for the old root look exactly like
        # aliases for the new one, and repointing it touches no file's mtime.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            other = archive / 'other-photos'
            other.mkdir()
            moved = other / 'aunt_ada.jpg'
            moved.write_bytes(b'x')
            long_ago = time.time() - 86400
            os.utime(moved, (long_ago, long_ago))
            os.utime(other, (long_ago, long_ago))

            first = {'roots': {'photos': 'photos'}}
            self._scan_with(archive, first)
            self.assertEqual(photoindex_status(archive, first)[0], 'fresh')

            repointed = {'roots': {'photos': 'other-photos'}}
            self.assertEqual(photoindex_status(archive, repointed)[0], 'stale')

    def test_reordering_photos_ignore_is_not_a_configuration_change(self) -> None:
        # The comparison is on the pattern SET: re-typing the same two lines in
        # the other order excludes exactly the same files, and telling the
        # human to rebuild an 88,000-file catalog over it would be the tool
        # crying wolf.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'},
                   'photos_ignore': ['Bulk Export', '*.tif']}
            self._scan_with(archive, cfg)
            reordered = {'roots': {'photos': 'photos'},
                         'photos_ignore': ['*.tif', 'Bulk Export']}
            self.assertEqual(photoindex_status(archive, reordered)[0], 'fresh')

    def test_catalog_from_an_older_build_is_not_reported_stale(self) -> None:
        # A photos.sqlite written before the build configuration was stored
        # cannot be compared against anything. Reporting drift we cannot
        # actually detect would nag every existing archive into a rescan on
        # upgrade; the next scan stamps it and the check arms itself.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            self._scan_with(archive, cfg)
            conn = sqlite3.connect(str(archive / '.cache' / 'photos.sqlite'))
            try:
                conn.execute('DELETE FROM meta WHERE key=?',
                             (photoindex.PHOTOINDEX_CONFIG_KEY,))
                conn.commit()
            finally:
                conn.close()
            narrowed = {'roots': {'photos': 'photos'},
                        'photos_ignore': ['Bulk Export']}
            self.assertEqual(photoindex_status(archive, narrowed)[0], 'fresh')

    def test_stale_from_a_setting_change_says_which_setting(self) -> None:
        # "Run fha photoindex" with no reason reads as nagging to someone who
        # just edited one line of fha.yaml and touched no photo at all.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            plain = {'roots': {'photos': 'photos'}}
            self._scan_with(archive, plain)
            narrowed = {'roots': {'photos': 'photos'},
                        'photos_ignore': ['Bulk Export']}
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = photoindex._print_photoindex_status(
                    'stale', archive_root=archive, fha_config=narrowed)
            self.assertIsNone(code)
            self.assertIn('photos_ignore', out.getvalue())
            self.assertIn('fha photoindex', out.getvalue())

    def test_unreadable_new_file_keeps_the_catalog_stale(self) -> None:
        # A new or changed photo exiftool could not read never reached the
        # catalog, but the commit stamps photos.sqlite with 'now' - so status
        # would call the catalog fresh and nothing would ever ask for another
        # scan. The watermark is pulled back behind the unread file instead.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            readable = lambda paths: [{'SourceFile': str(p)} for p in paths]
            photoindex._run_exiftool = readable
            photoindex.run_scan(archive, cfg)
            self.assertEqual(photoindex_status(archive, cfg)[0], 'fresh')

            shutil.copy(archive / 'photos' / 'portrait_1880.jpg',
                        archive / 'photos' / 'brand_new.jpg')
            photoindex._run_exiftool = lambda paths: [
                {'SourceFile': str(p)} for p in paths if p.name != 'brand_new.jpg']
            summary = photoindex.run_scan(archive, cfg)
            self.assertEqual(summary['unreadable'], 1)
            self.assertEqual(summary['unreadable_unindexed'], 1)
            self.assertEqual(photoindex_status(archive, cfg)[0], 'stale')

            # Reading it later heals the catalog - the pullback is a retry
            # marker, not a permanent condition.
            photoindex._run_exiftool = readable
            summary = photoindex.run_scan(archive, cfg)
            self.assertEqual(summary['unreadable'], 0)
            self.assertEqual(photoindex_status(archive, cfg)[0], 'fresh')

    def test_a_failed_stale_hold_is_reported_not_swallowed(self) -> None:
        # Sweep, round 5 (false success): the pullback that keeps the catalog
        # marked out of date is itself a filesystem write, and it can fail (a
        # read-only .cache, a mount that refuses utime). The failure was caught
        # and dropped, so photos.sqlite kept the commit's own "now" timestamp -
        # `photoindex_status` then reports 'fresh' and `fha find --text`
        # answers from a catalog that never saw the new photo, with nothing on
        # screen and a clean exit code to say so.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            photoindex._run_exiftool = lambda paths: [
                {'SourceFile': str(p)} for p in paths]
            photoindex.run_scan(archive, cfg)

            shutil.copy(archive / 'photos' / 'portrait_1880.jpg',
                        archive / 'photos' / 'brand_new.jpg')
            photoindex._run_exiftool = lambda paths: [
                {'SourceFile': str(p)} for p in paths if p.name != 'brand_new.jpg']
            with mock.patch.object(photoindex.os, 'utime',
                                   side_effect=OSError(1, 'Operation not permitted')):
                summary = photoindex.run_scan(archive, cfg)

            self.assertEqual(summary['unreadable_unindexed'], 1)
            self.assertTrue(summary['stale_hold_failed'])
            # The danger the flag stands for is real: the catalog now reads
            # fresh even though the new photo never reached it.
            self.assertEqual(photoindex_status(archive, cfg)[0], 'fresh')

    def test_cmd_scan_names_a_failed_stale_hold_and_the_next_step(self) -> None:
        # The interface half: a swallowed pullback must not exit clean, and the
        # human must be told which command puts the catalog right.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            photoindex._run_exiftool = lambda paths: [
                {'SourceFile': str(p)} for p in paths]
            photoindex.run_scan(archive, cfg)

            shutil.copy(archive / 'photos' / 'portrait_1880.jpg',
                        archive / 'photos' / 'brand_new.jpg')
            photoindex._run_exiftool = lambda paths: [
                {'SourceFile': str(p)} for p in paths if p.name != 'brand_new.jpg']
            args = type('Args', (), {'root': str(archive), 'full': False})()
            stderr = io.StringIO()
            with mock.patch.object(photoindex.os, 'utime',
                                   side_effect=OSError(1, 'Operation not permitted')):
                with contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(stderr):
                    code = photoindex._cmd_scan(args)
            self.assertEqual(code, EXIT_WARNINGS)
            text = stderr.getvalue()
            self.assertIn('fha photoindex --full', text)
            self.assertNotIn('Traceback', text)

    def test_reconcile_reports_a_failed_stale_hold(self) -> None:
        # The same pullback, the same swallow, in reconcile's half of the pair:
        # files it rematched or left untracked are not reflected in the cache,
        # so the cache must not be allowed to look current in silence.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            photoindex._run_exiftool = lambda paths: [
                {'SourceFile': str(p)} for p in paths]
            photoindex.run_scan(archive, cfg)
            shutil.copy(archive / 'photos' / 'portrait_1880.jpg',
                        archive / 'photos' / 'brand_new.jpg')

            with mock.patch.object(photoindex.os, 'utime',
                                   side_effect=OSError(1, 'Operation not permitted')):
                result = photoindex.run_reconcile(archive, cfg)

            self.assertEqual(result['new_count'], 1)
            self.assertTrue(result['stale_hold_failed'])
            self.assertEqual(result.exit_code, EXIT_WARNINGS)

    def test_unreadable_but_unchanged_file_does_not_hold_the_catalog_stale(self) -> None:
        # The other side of the same rule: under --full an unreadable photo is
        # re-sent to exiftool even though its cached row already matches the
        # file on disk. That row is as current as it can be, so a permanently
        # damaged photo must not leave the catalog stale forever.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            photoindex._run_exiftool = lambda paths: [
                {'SourceFile': str(p)} for p in paths]
            photoindex.run_scan(archive, cfg)

            photoindex._run_exiftool = lambda paths: [
                {'SourceFile': str(p)} for p in paths if p.name != 'family_reunion.jpg']
            summary = photoindex.run_scan(archive, cfg, full=True)
            self.assertEqual(summary['unreadable'], 1)
            self.assertEqual(summary['unreadable_unindexed'], 0)
            self.assertEqual(photoindex_status(archive, cfg)[0], 'fresh')

    def test_cmd_scan_names_the_held_stale_catalog_and_the_next_step(self) -> None:
        # The human sees a stale-index warning from `fha find` next; the scan
        # that caused it must say so and name the fix.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            photoindex._run_exiftool = lambda paths: [
                {'SourceFile': str(p)} for p in paths if p.name != 'family_reunion.jpg']
            args = type('Args', (), {'root': str(archive), 'full': False})()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                code = photoindex._cmd_scan(args)
            self.assertEqual(code, EXIT_WARNINGS)
            text = stderr.getvalue()
            self.assertIn('stays marked out of date', text)
            self.assertIn('fha photoindex', text)


def _walk_with_unreadable_dir(unreadable: Path):
    """An os.walk stand-in whose listing of `unreadable` fails, like a real one.

    chmod cannot produce this condition in a test run as root (CI is), and it
    does nothing at all on Windows, so the failure is injected at the seam
    os.walk itself reports it from. CPython's os.walk calls scandir(top), hands
    the OSError to `onerror`, and returns - the directory is never yielded and
    its subtree is never descended. This reproduces all three, so a walker with
    no `onerror` sees exactly what it sees in the field: an empty folder.
    """
    real_walk = os.walk
    target = unreadable.resolve()

    def walk(top, topdown=True, onerror=None, followlinks=False):
        for dirpath, dirnames, filenames in real_walk(top, topdown, onerror, followlinks):
            if Path(dirpath).resolve() == target:
                err = PermissionError(13, 'Permission denied')
                err.filename = str(dirpath)
                if onerror is not None:
                    onerror(err)
                dirnames[:] = []
                continue
            yield dirpath, dirnames, filenames

    return walk


class UnreadableDirectoryTests(unittest.TestCase):
    """A folder the walk cannot list is unverified, never empty.

    os.walk swallows the error from a directory it cannot list unless an
    `onerror` callback is supplied, so a permissions change or an unmounted
    network folder used to reach the scan as "these photos are gone". The scan
    then deleted their photos/keyword/face-region/person rows and reported a
    clean run. These tests pin the three halves of the fix: the rows survive,
    the catalog does not certify itself fresh, and a file that really is gone
    is still swept."""

    def setUp(self) -> None:
        self._orig_run_exiftool = photoindex._run_exiftool
        photoindex._run_exiftool = lambda paths: [
            {
                'SourceFile': str(p),
                'Keywords': ['P-de957bcda1', 'SOURCE: S-1111111111'],
                'RegionInfo': {'RegionList': [
                    {'Name': 'Margaret Cole', 'Type': 'Face',
                     'Area': {'X': 0.5, 'Y': 0.5, 'W': 0.1, 'H': 0.1}},
                ]},
            }
            for p in paths
        ]

    def tearDown(self) -> None:
        photoindex._run_exiftool = self._orig_run_exiftool

    @staticmethod
    def _with_attic(archive: Path) -> Path:
        attic = archive / 'photos' / 'Attic'
        attic.mkdir()
        (attic / 'shelf_one.jpg').write_bytes(b'x')
        (attic / 'shelf_two.jpg').write_bytes(b'x')
        return attic

    @staticmethod
    def _cached_paths(archive: Path, table: str = 'photos') -> set:
        conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
        try:
            return {row[0] for row in conn.execute(f'SELECT path FROM {table}')}
        finally:
            conn.close()

    def test_iter_photo_files_reports_a_directory_it_could_not_read(self) -> None:
        # The walker's own contract: without an onerror callback os.walk drops
        # the error, and every caller that deletes rows for files it did not
        # see is then working from a listing it has no way to distrust.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'Keep').mkdir()
            (root / 'Keep' / 'y.jpg').write_bytes(b'x')
            attic = root / 'Attic'
            attic.mkdir()
            (attic / 'x.jpg').write_bytes(b'x')

            errors: list = []
            with mock.patch('os.walk', new=_walk_with_unreadable_dir(attic)):
                found = sorted(p.name for p in photoindex._iter_photo_files(
                    root, None, on_error=errors.append))
            self.assertEqual(found, ['y.jpg'])
            self.assertEqual([Path(e.filename).name for e in errors], ['Attic'])

    def test_scan_keeps_rows_under_a_directory_it_could_not_read(self) -> None:
        # The deletion half. Every cached row below an unreadable folder -
        # photos, keywords, face regions, person matches - must survive: a
        # phantom row is swept by the next scan that can see in there, a
        # deleted confirmed person tag costs the human real work to put back.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            attic = self._with_attic(archive)
            photoindex.run_scan(archive, cfg)

            attic_rows = {p for p in self._cached_paths(archive)
                          if p.startswith('photos/Attic/')}
            self.assertEqual(len(attic_rows), 2)

            with mock.patch('os.walk', new=_walk_with_unreadable_dir(attic)):
                summary = photoindex.run_scan(archive, cfg)

            self.assertEqual(summary['removed'], 0)
            self.assertEqual(summary['held_unreadable'], 2)
            self.assertEqual(summary['unreadable_dirs'], ['photos/Attic'])
            for table in ('photos', 'photo_keywords', 'photo_face_regions', 'photo_people'):
                self.assertEqual(
                    {p for p in self._cached_paths(archive, table)
                     if p.startswith('photos/Attic/')},
                    attic_rows,
                    f'{table} rows under an unreadable folder were not preserved',
                )

    def test_scan_does_not_report_a_clean_run_for_an_unreadable_directory(self) -> None:
        # The false-fresh half. Preserving the rows is not enough on its own:
        # a scan that exits clean certifies the catalog, and every later reader
        # (find --text, packet, site) trusts that certificate.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            attic = self._with_attic(archive)
            photoindex.run_scan(archive, cfg)

            with mock.patch('os.walk', new=_walk_with_unreadable_dir(attic)):
                summary = photoindex.run_scan(archive, cfg)

            self.assertEqual(summary.exit_code, EXIT_WARNINGS)
            self.assertFalse(summary['stale_hold_failed'])
            self.assertEqual(photoindex_status(archive, cfg)[0], 'stale')

    def test_cmd_scan_names_the_unreadable_folder_and_the_next_step(self) -> None:
        # AGENTS.md "Who you serve": the owner must be able to act on this
        # without knowing what os.walk is - which folder, what was NOT done to
        # his catalog, and the one command to run once it opens again.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            attic = self._with_attic(archive)
            photoindex.run_scan(archive, cfg)

            args = type('Args', (), {'root': str(archive), 'full': False})()
            stderr = io.StringIO()
            with mock.patch('os.walk', new=_walk_with_unreadable_dir(attic)):
                with contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(stderr):
                    code = photoindex._cmd_scan(args)

            self.assertEqual(code, EXIT_WARNINGS)
            text = stderr.getvalue()
            self.assertIn('photos/Attic', text)
            self.assertIn('could not be opened', text)
            self.assertIn('left in place', text)
            self.assertIn('fha photoindex', text)

    def test_scan_still_sweeps_a_file_that_really_is_gone(self) -> None:
        # The control: the fix must not disable removal. A photo deleted from a
        # folder the walk COULD read is still swept in the same run that holds
        # the unreadable folder's rows back - the guard is scoped to the
        # subtree nobody could see, not to the sweep as a whole.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            attic = self._with_attic(archive)
            photoindex.run_scan(archive, cfg)

            (archive / 'photos' / 'wedding_1902.jpg').unlink()
            with mock.patch('os.walk', new=_walk_with_unreadable_dir(attic)):
                summary = photoindex.run_scan(archive, cfg)

            self.assertEqual(summary['removed'], 1)
            self.assertEqual(summary['held_unreadable'], 2)
            paths = self._cached_paths(archive)
            self.assertNotIn('photos/wedding_1902.jpg', paths)
            self.assertIn('photos/Attic/shelf_one.jpg', paths)

    def test_scan_still_sweeps_an_ignored_row_under_an_unreadable_directory(self) -> None:
        # photos_ignore is a decision about what the catalog holds, and it does
        # not depend on the walk seeing anything - so an excluded subtree is
        # still swept even when it is the unreadable one. Without that the
        # ignore promise ("its rows are removed") would quietly lapse.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            attic = self._with_attic(archive)
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            cfg = {'roots': {'photos': 'photos'}, 'photos_ignore': ['Attic']}
            with mock.patch('os.walk', new=_walk_with_unreadable_dir(attic)):
                summary = photoindex.run_scan(archive, cfg)

            self.assertEqual(summary['removed'], 2)
            self.assertEqual(summary['held_unreadable'], 0)
            self.assertEqual(
                {p for p in self._cached_paths(archive) if p.startswith('photos/Attic/')},
                set(),
            )

    def test_reconcile_holds_rows_under_a_directory_it_could_not_read(self) -> None:
        # Reconcile decides "missing" by subtracting the on-disk listing from
        # the catalog, so an unlistable folder reads as a shelf of lost photos:
        # every row renamed MISSING:, its groups' dates recomputed without it,
        # and the loss reported to the human as fact.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            attic = self._with_attic(archive)
            photoindex.run_scan(archive, cfg)

            with mock.patch('os.walk', new=_walk_with_unreadable_dir(attic)):
                result = photoindex.run_reconcile(archive, cfg)

            self.assertEqual(result['missing'], [])
            self.assertEqual(result['held_unreadable'], 2)
            self.assertEqual(result['unreadable_dirs'], ['photos/Attic'])
            self.assertEqual(result.exit_code, EXIT_WARNINGS)
            self.assertIn('photos/Attic/shelf_one.jpg', self._cached_paths(archive))

    def test_reconcile_still_flags_a_photo_that_really_is_gone(self) -> None:
        # The reconcile-side control, matching the scan's: a photo deleted from
        # a readable folder is still flagged MISSING: in the same run that
        # holds the unreadable folder's rows back.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            attic = self._with_attic(archive)
            photoindex.run_scan(archive, cfg)

            (archive / 'photos' / 'wedding_1902.jpg').unlink()
            with mock.patch('os.walk', new=_walk_with_unreadable_dir(attic)):
                result = photoindex.run_reconcile(archive, cfg)

            self.assertEqual(result['missing'], ['MISSING:photos/wedding_1902.jpg'])
            self.assertEqual(result['held_unreadable'], 2)


class GroupDateSymmetryTests(unittest.TestCase):
    """The full rebuild and reconcile's narrower patch resolve one group date.

    `_group_photos` orders a group's dated variants primary-first so an equal-
    confidence tie lands on the primary's date; reconcile's
    `_recompute_group_fields` used to order by filename alone. Marking an
    unrelated third variant missing therefore changed `edtf_resolved`, and the
    next full scan changed it back - date filters and gallery headings
    depending on which maintenance command ran last (AGENTS_TOOLING
    "Symmetry gaps": full rebuild vs incremental recompute)."""

    def setUp(self) -> None:
        self._orig_run_exiftool = photoindex._run_exiftool

    def tearDown(self) -> None:
        photoindex._run_exiftool = self._orig_run_exiftool

    @staticmethod
    def _group_date(archive: Path) -> tuple:
        conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
        try:
            return conn.execute(
                'SELECT edtf_resolved, date_conflict FROM photo_groups '
                "WHERE group_id LIKE 'STEM:%portrait_1880%'"
            ).fetchone()
        finally:
            conn.close()

    def _tied_variants_archive(self, tmp: Path) -> Path:
        """Three variants of one photo: the primary and the back scan carry
        equally confident but different years, the negative carries no date.

        The tie is what makes the ordering visible. 'photos/portrait_1880-back.jpg'
        sorts before 'photos/portrait_1880.jpg' (a dash sorts before a dot), so
        filename order and primary-first order disagree about which year wins.
        """
        archive = _copy_fixture(tmp)
        shutil.copy(
            archive / 'photos' / 'portrait_1880.jpg',
            archive / 'photos' / 'portrait_1880-negative.jpg',
        )

        def fake_exiftool(paths: list) -> list:
            rows = {
                'portrait_1880.jpg': {
                    'Keywords': ['DATE: Y!'],
                    'DateTimeOriginal': '1950:01:01 00:00:00',
                },
                'portrait_1880-back.jpg': {
                    'Keywords': ['DATE: Y!'],
                    'DateTimeOriginal': '1940:01:01 00:00:00',
                },
            }
            return [{'SourceFile': str(p), **rows.get(p.name, {})} for p in paths]

        photoindex._run_exiftool = fake_exiftool
        return archive

    def test_reconcile_keeps_the_primary_first_date_tie_break(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = self._tied_variants_archive(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            photoindex.run_scan(archive, cfg)
            self.assertEqual(self._group_date(archive), ('1950', 1))

            # An unrelated third variant vanishes. It carries no date at all,
            # so it cannot change which year the group resolves to - only the
            # ordering rule can.
            (archive / 'photos' / 'portrait_1880-negative.jpg').unlink()
            result = photoindex.run_reconcile(archive, cfg)
            self.assertEqual(result['missing'], ['MISSING:photos/portrait_1880-negative.jpg'])

            self.assertEqual(self._group_date(archive), ('1950', 1))

    def test_full_scan_after_reconcile_leaves_the_group_date_alone(self) -> None:
        # The other half of the same rule: whichever command ran last, the
        # group's date is the same. Before the shared ordering this flipped
        # 1940 -> 1950 on every scan and back on every reconcile.
        with tempfile.TemporaryDirectory() as d:
            archive = self._tied_variants_archive(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            photoindex.run_scan(archive, cfg)

            (archive / 'photos' / 'portrait_1880-negative.jpg').unlink()
            photoindex.run_reconcile(archive, cfg)
            after_reconcile = self._group_date(archive)

            photoindex.run_scan(archive, cfg, full=True)
            self.assertEqual(self._group_date(archive), after_reconcile)

    def test_reconcile_repoints_the_group_primary_at_a_live_file(self) -> None:
        # The other half of the same shared rule. _move_cached_path rewrites
        # photo_groups.primary_path to 'MISSING:photos/...' as ordinary text
        # maintenance; until a full scan re-picked one, `fha photoindex find`
        # showed the whole group as missing while a readable variant of it sat
        # on disk. Reconcile now re-picks by the rebuild's own rule.
        with tempfile.TemporaryDirectory() as d:
            archive = self._tied_variants_archive(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            photoindex.run_scan(archive, cfg)

            (archive / 'photos' / 'portrait_1880.jpg').unlink()
            photoindex.run_reconcile(archive, cfg)

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                after_reconcile = conn.execute(
                    'SELECT primary_path FROM photo_groups '
                    "WHERE group_id LIKE 'STEM:%portrait_1880%'").fetchone()[0]
                primary_rows = {
                    row[0] for row in conn.execute(
                        'SELECT path FROM photos WHERE is_primary=1')
                }
            finally:
                conn.close()
            self.assertEqual(after_reconcile, 'photos/portrait_1880-back.jpg')
            self.assertIn('photos/portrait_1880-back.jpg', primary_rows)

            # And the full rebuild lands on the same file, so neither command
            # can undo the other.
            photoindex.run_scan(archive, cfg, full=True)
            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                after_scan = conn.execute(
                    'SELECT primary_path FROM photo_groups '
                    "WHERE group_id LIKE 'STEM:%portrait_1880%'").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(after_scan, after_reconcile)

    def test_reconcile_uses_a_live_primary_when_the_primary_itself_vanished(self) -> None:
        # The primary is chosen from the live members (_select_group_primary),
        # so when the plain scan itself goes missing the tie-break falls to the
        # best remaining file - and the full rebuild agrees.
        with tempfile.TemporaryDirectory() as d:
            archive = self._tied_variants_archive(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            photoindex.run_scan(archive, cfg)

            (archive / 'photos' / 'portrait_1880.jpg').unlink()
            photoindex.run_reconcile(archive, cfg)
            after_reconcile = self._group_date(archive)
            self.assertEqual(after_reconcile, ('1940', None))

            photoindex.run_scan(archive, cfg, full=True)
            self.assertEqual(self._group_date(archive), after_reconcile)


class GalleryTests(unittest.TestCase):
    """`fha photoindex gallery` (plan 08) - the single-file HTML photo page.

    Staged like the find tests: a fake exiftool payload feeds run_scan, then the
    gallery is built and the HTML the tool actually wrote is inspected. The page
    is a disposable private artifact under generated/gallery/; nothing here reads
    a template in isolation - every assertion reads the produced file."""

    def setUp(self) -> None:
        self._orig_run_exiftool = photoindex._run_exiftool

    def tearDown(self) -> None:
        photoindex._run_exiftool = self._orig_run_exiftool

    def _stage(self, archive: Path, rows: dict, cfg: dict | None = None,
               extra_files: tuple = ()) -> None:
        """Write any extra photo files, then scan with a fake exiftool payload.

        `rows` maps a bare filename to its exiftool metadata dict; a discovered
        file with no entry is scanned with empty metadata (it can still group with
        a sibling). `extra_files` are created under the resolved photos root before
        the scan so run_scan discovers them (the scan lists real on-disk files;
        only the metadata is faked)."""
        cfg = cfg or {'roots': {'photos': 'photos'}}
        photos_dir = resolve_path('photos', cfg, archive)
        photos_dir.mkdir(parents=True, exist_ok=True)
        for name in extra_files:
            (photos_dir / name).write_bytes(b'\xff\xd8\xff\xd9')

        def fake_exiftool(paths: list[Path]) -> list[dict]:
            return [{'SourceFile': str(p), **rows.get(p.name, {})} for p in paths]

        photoindex._run_exiftool = fake_exiftool
        photoindex.run_scan(archive, cfg)

    def _make_index(self, archive: Path, persons, face_tags=(), variants=()) -> Path:
        """Write a schema-valid .cache/index.sqlite so the weaker person tiers and
        the display-name lookup have something to read. Person ids are stored
        lowercase (the form photo_people carries), matching the real index."""
        cache = archive / '.cache'
        cache.mkdir(exist_ok=True)
        index_db = cache / 'index.sqlite'
        conn = sqlite3.connect(index_db)
        try:
            conn.executescript(
                f"""
                PRAGMA user_version={INDEX_SCHEMA_VERSION};
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO meta(key, value) VALUES ('schema_version', '{INDEX_SCHEMA_VERSION}');
                CREATE TABLE persons(id TEXT, name TEXT);
                CREATE TABLE person_face_tags(person_id TEXT, tag TEXT);
                CREATE TABLE person_variants(person_id TEXT, variant TEXT);
                """
            )
            conn.executemany('INSERT INTO persons(id, name) VALUES (?,?)', persons)
            conn.executemany(
                'INSERT INTO person_face_tags(person_id, tag) VALUES (?,?)', face_tags)
            conn.executemany(
                'INSERT INTO person_variants(person_id, variant) VALUES (?,?)', variants)
            conn.commit()
        finally:
            conn.close()
        return index_db

    @staticmethod
    def _read(path) -> str:
        return Path(path).read_text(encoding='utf-8')

    @staticmethod
    def _args(archive: Path, **over):
        base = {'root': str(archive), 'person': None, 'keyword': None,
                'edtf': None, 'text': None, 'out': None}
        base.update(over)
        return type('Args', (), base)()

    def test_gallery_one_row_per_group_with_variant_chips(self) -> None:
        # A front + back + copy of one physical photo collapse to a single row;
        # the siblings become chips ("back", "copy b"), never their own rows.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            self._stage(
                archive,
                {'portrait_1880.jpg': {'Keywords': ['DATE: Y!'],
                                   'DateTimeOriginal': '1880:01:01 00:00:00'}},
                cfg,
                extra_files=('portrait_1880b.jpg',),
            )

            result = photoindex.run_gallery(archive, cfg, edtf='188X')
            html = self._read(result['written'])

            self.assertEqual(result['matched'], 1)
            self.assertEqual(html.count('<article class="photo-row">'), 1)
            # The siblings are chips inside the <details>, not standalone rows.
            self.assertIn('back', html)
            self.assertIn('copy b', html)
            self.assertIn('portrait_1880-back.jpg', html)

    def test_gallery_filters_and_at_group_level(self) -> None:
        # --edtf hits the front scan's date and --text hits the back scan's
        # caption: no single raw file satisfies both, yet the group does. Gallery
        # and find share the matching helper, so they must agree on the result.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            self._stage(archive, {
                'portrait_1880.jpg': {'Keywords': ['DATE: Y!'],
                                      'DateTimeOriginal': '1880:01:01 00:00:00'},
                'portrait_1880-back.jpg': {'Caption-Abstract': 'cemetery visit'},
                'wedding_1902.jpg': {'Keywords': ['DATE: Y!'],
                                     'DateTimeOriginal': '1902:01:01 00:00:00',
                                     'Caption-Abstract': 'Wedding party'},
            }, cfg)

            find = photoindex.run_find(archive, cfg, edtf='188X', text='cemetery')
            find_paths = sorted(r['path'] for r in find['rows'])
            self.assertEqual(find_paths, ['photos/portrait_1880.jpg'])

            gallery = photoindex.run_gallery(archive, cfg, edtf='188X', text='cemetery')
            self.assertEqual(gallery['matched'], len(find_paths))
            html = self._read(gallery['written'])
            for path in find_paths:
                self.assertIn(Path(path).name, html)
            self.assertNotIn('wedding_1902.jpg', html)

            # A filter set that matches nothing agrees too: both come back empty.
            neg_find = photoindex.run_find(archive, cfg, edtf='1902', text='cemetery')
            self.assertEqual(neg_find['rows'], [])
            neg_gallery = photoindex.run_gallery(archive, cfg, edtf='1902', text='cemetery')
            self.assertEqual(neg_gallery['matched'], 0)

    def test_gallery_count_strip_humanizes_the_edtf_filter(self) -> None:
        # Codex P2 (symmetry): the count strip's "matching dated ..." clause
        # must read the same humanized form as each row's own date_label,
        # not the raw EDTF filter syntax the per-row label no longer shows.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            self._stage(archive, {'portrait_1880.jpg': {
                'Keywords': ['DATE: Y!'],
                'DateTimeOriginal': '1880:01:01 00:00:00'}}, cfg)

            result = photoindex.run_gallery(archive, cfg, edtf='188X')
            html = self._read(result['written'])

            self.assertIn('matching dated 1880s', html)
            self.assertNotIn('188X', html)

    def test_gallery_decade_sections_and_undated_tail(self) -> None:
        # Dated groups land in decade sections newest-first; the undated group
        # falls to the "Undated" tail section, which sorts last.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            self._stage(archive, {
                'portrait_1880.jpg': {'Keywords': ['DATE: Y!', 'gallery-test'],
                                      'DateTimeOriginal': '1920:01:01 00:00:00'},
                'portrait_1880-back.jpg': {'Keywords': ['gallery-test']},
                'wedding_1902.jpg': {'Keywords': ['DATE: Y!', 'gallery-test'],
                                     'DateTimeOriginal': '1955:01:01 00:00:00'},
                'family_reunion.jpg': {'Keywords': ['gallery-test']},
            }, cfg)

            result = photoindex.run_gallery(archive, cfg, keyword='gallery-test')
            html = self._read(result['written'])

            self.assertEqual(result['matched'], 3)
            i_1950s = html.index('<h2>1950s</h2>')
            i_1920s = html.index('<h2>1920s</h2>')
            i_undated = html.index('<h2>Undated</h2>')
            self.assertLess(i_1950s, i_1920s)   # newest decade first
            self.assertLess(i_1920s, i_undated)  # undated tail is last

    def test_gallery_confidence_split(self) -> None:
        # A pid-keyword photo renders in the main flow; a name-match photo drops
        # into the "Verify these" tail with its via label. Both resolve to the
        # same person, so a --person build must separate them by confidence.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            self._make_index(
                archive,
                persons=[('p-de957bcda1', 'Margaret Hartley')],
                variants=[('p-de957bcda1', 'Maggie')],
            )
            self._stage(archive, {
                'family_reunion.jpg': {'Keywords': ['P-de957bcda1']},
                'portrait_1880.jpg': {
                    'RegionInfo': {'RegionList': [{'Name': 'Maggie', 'Type': 'Face'}]},
                },
            }, cfg)

            result = photoindex.run_gallery(archive, cfg, person='P-de957bcda1')
            html = self._read(result['written'])

            self.assertIn('Verify these', html)
            self.assertIn('Margaret Hartley', html)      # display name in the strip
            self.assertIn('name match', html)            # the via label
            self.assertIn('tag-person P-de957bcda1', html)
            verify_at = html.index('gallery-verify')
            self.assertLess(html.index('family_reunion.jpg'), verify_at)   # main flow
            self.assertGreater(html.index('portrait_1880.jpg'), verify_at)  # verify tail

    def test_gallery_renderable_vs_placeholder_tiles(self) -> None:
        # A browser-renderable .jpg gets a lazy <img>; a .tif gets a placeholder
        # tile (no <img>) but is still wrapped in an <a href="file://..."> so a
        # click opens the real file.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            self._stage(archive, {
                'portrait_1880.jpg': {'Keywords': ['tiletest']},
                'oldscan.tif': {'Keywords': ['tiletest']},
            }, cfg, extra_files=('oldscan.tif',))

            result = photoindex.run_gallery(archive, cfg, keyword='tiletest')
            html = self._read(result['written'])

            self.assertEqual(result['matched'], 2)
            self.assertEqual(html.count('<img loading="lazy"'), 1)   # only the jpg
            self.assertIn('ext-badge">TIF<', html)                   # placeholder badge
            self.assertIn('href="file://', html)
            self.assertIn('oldscan.tif', html)
            # The tif is never emitted as an <img> (its alt would name the file).
            self.assertNotIn('oldscan.tif" alt=', html)

    def test_gallery_file_urls_resolve_through_roots(self) -> None:
        # With an external photos root in fha.yaml, hrefs must point at the
        # resolved absolute location, not archive_root/photos.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            external = Path(d) / 'external-photos'
            external.mkdir()
            cfg = {'roots': {'photos': str(external)}}
            self._stage(archive, {'photo1.jpg': {'Keywords': ['exttest']}},
                        cfg, extra_files=('photo1.jpg',))

            result = photoindex.run_gallery(archive, cfg, keyword='exttest')
            html = self._read(result['written'])

            self.assertIn((external / 'photo1.jpg').as_uri(), html)
            # The internal archive/photos location must not appear.
            self.assertNotIn((archive / 'photos' / 'photo1.jpg').as_uri(), html)

    def test_gallery_overwrites_own_marker_refuses_foreign_file(self) -> None:
        # The one write guard: a marker-owned file is silently regenerated, a
        # marker-less (human) file at the target is never clobbered.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            self._stage(archive, {'portrait_1880.jpg': {'Keywords': ['marktest']}}, cfg)

            first = photoindex.run_gallery(archive, cfg, keyword='marktest')
            self.assertEqual(first.exit_code, EXIT_CLEAN)
            out_path = Path(first['written'])
            self.assertTrue(out_path.exists())

            # A second run overwrites its own marker-owned output in place.
            second = photoindex.run_gallery(archive, cfg, keyword='marktest')
            self.assertEqual(second.exit_code, EXIT_CLEAN)
            self.assertEqual(Path(second['written']), out_path)

            # A hand-made file at the --out target is refused and left untouched.
            foreign = archive / 'generated' / 'gallery' / 'foreign.html'
            foreign.parent.mkdir(parents=True, exist_ok=True)
            foreign.write_text('<html>hand made, keep me</html>', encoding='utf-8')
            refused = photoindex.run_gallery(
                archive, cfg, keyword='marktest', out='generated/gallery/foreign.html')
            self.assertEqual(refused.exit_code, EXIT_FAILURE)
            self.assertEqual(Path(refused.data['refused']), foreign)
            self.assertEqual(foreign.read_text(encoding='utf-8'),
                             '<html>hand made, keep me</html>')

    def test_gallery_default_landing_and_out_override(self) -> None:
        # Person galleries default to generated/gallery/{slug}_{P-id}.html; --out
        # overrides; the site's own generated/site/ output is never disturbed.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            self._make_index(archive, persons=[('p-de957bcda1', 'Margaret Hartley')])
            self._stage(archive, {'family_reunion.jpg': {'Keywords': ['P-de957bcda1']}}, cfg)

            site_dir = archive / 'generated' / 'site'
            site_dir.mkdir(parents=True, exist_ok=True)
            sentinel = site_dir / 'index.html'
            sentinel.write_text('SITE OUTPUT', encoding='utf-8')

            default = photoindex.run_gallery(archive, cfg, person='P-de957bcda1')
            expected = archive / 'generated' / 'gallery' / 'margaret-hartley_P-de957bcda1.html'
            self.assertEqual(Path(default['written']), expected)
            self.assertTrue(expected.exists())

            override = photoindex.run_gallery(
                archive, cfg, person='P-de957bcda1', out='generated/gallery/custom.html')
            self.assertEqual(Path(override['written']),
                             archive / 'generated' / 'gallery' / 'custom.html')
            self.assertTrue((archive / 'generated' / 'gallery' / 'custom.html').exists())

            # generated/site/ is a sibling the gallery never reads or clears.
            self.assertTrue(sentinel.exists())
            self.assertEqual(sentinel.read_text(encoding='utf-8'), 'SITE OUTPUT')

    def test_gallery_out_with_missing_parent_folder_is_a_clear_error(self) -> None:
        # Codex P2: only the default landing spot (generated/gallery/) may
        # auto-create its folder - an explicit --out is human-typed input, so
        # a typo'd/nonexistent parent must be a clear error, never a silently
        # fabricated directory tree anywhere under the archive root.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            self._stage(archive, {'portrait_1880.jpg': {
                'Keywords': ['DATE: Y!'],
                'DateTimeOriginal': '1880:01:01 00:00:00'}}, cfg)

            missing_dir = archive / 'reports' / 'nested'
            with self.assertRaises(RuntimeError) as ctx:
                photoindex.run_gallery(
                    archive, cfg, edtf='188X', out='reports/nested/farm.html')
            self.assertIn('--out', str(ctx.exception))
            self.assertFalse(missing_dir.exists())

    def test_gallery_zero_matches_writes_nothing_exits_clean(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            self._stage(archive, {}, cfg)

            result = photoindex.run_gallery(archive, cfg, keyword='no-such-keyword')

            self.assertEqual(result.exit_code, EXIT_CLEAN)
            self.assertEqual(result['matched'], 0)
            self.assertIsNone(result['written'])
            self.assertFalse((archive / 'generated' / 'gallery').exists())

    def test_gallery_requires_a_filter_and_validates_inputs(self) -> None:
        # No filter / bad P-id / invalid EDTF are refusals (exit 3), not silent
        # empties. Driven through _cmd_gallery so the exit codes are exercised.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._stage(archive, {'family_reunion.jpg': {
                'Keywords': ['DATE: Y!'],
                'DateTimeOriginal': '1880:01:01 00:00:00'}})

            self.assertEqual(
                photoindex._cmd_gallery(self._args(archive)), EXIT_FAILURE)
            self.assertEqual(
                photoindex._cmd_gallery(self._args(archive, person='not-an-id')),
                EXIT_FAILURE)
            self.assertEqual(
                photoindex._cmd_gallery(self._args(archive, edtf='banana')),
                EXIT_FAILURE)

    def test_gallery_stale_warns_and_builds_absent_fails(self) -> None:
        # Mirrors find's cache posture: a stale cache still builds (warn), an
        # absent cache fails with the rebuild message and writes nothing.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            self._stage(archive, {'family_reunion.jpg': {'Keywords': ['marktest']}}, cfg)

            # Force stale: an index newer than photos.sqlite (the find test's trick).
            cache = archive / '.cache'
            index_db = cache / 'index.sqlite'
            sqlite3.connect(index_db).close()
            photos_mtime = (cache / 'photos.sqlite').stat().st_mtime
            os.utime(index_db, (photos_mtime + 10, photos_mtime + 10))
            self.assertEqual(photoindex_status(archive, cfg)[0], 'stale')

            stale = photoindex.run_gallery(archive, cfg, keyword='marktest')
            self.assertEqual(stale.exit_code, EXIT_CLEAN)
            self.assertEqual(stale['status'], 'stale')
            self.assertIsNotNone(stale['written'])

        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            absent = photoindex.run_gallery(archive, cfg, keyword='marktest')
            self.assertEqual(absent.exit_code, EXIT_FAILURE)
            self.assertEqual(absent['status'], 'absent')
            self.assertIsNone(absent.data.get('written'))
            self.assertFalse((archive / 'generated' / 'gallery').exists())

    def test_gallery_html_is_self_contained(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            self._stage(archive, {'portrait_1880.jpg': {'Keywords': ['marktest']}}, cfg)

            result = photoindex.run_gallery(archive, cfg, keyword='marktest')
            html = self._read(result['written'])

            self.assertNotIn('http://', html)
            self.assertNotIn('https://', html)
            self.assertNotIn('<script', html)

    def test_gallery_banner_and_marker_present(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            self._stage(archive, {'portrait_1880.jpg': {'Keywords': ['marktest']}}, cfg)

            result = photoindex.run_gallery(archive, cfg, keyword='marktest')
            html = self._read(result['written'])

            first_line = html.splitlines()[0]
            self.assertTrue(first_line.startswith('<!-- GENERATED by fha photoindex gallery'))
            self.assertEqual(html.count('Private research companion'), 1)

    # ── Review-fix regression tests (fix pass on the gallery feature) ──────────

    def test_decade_of_reads_edtf_literal_not_bounds_midpoint(self) -> None:
        # F1: the decade comes from the EDTF literal, not edtf_bounds' widened
        # midpoint. The old midpoint code returned 1910 for '1920-01~' and 950
        # for '[..1900]'; open ranges/sets now route to Undated (None).
        self.assertEqual(photoindex._decade_of('1920-01~'), 1920)
        self.assertEqual(photoindex._decade_of('1920-~01'), 1920)
        self.assertEqual(photoindex._decade_of('192X'), 1920)
        self.assertEqual(photoindex._decade_of('1955'), 1950)
        self.assertIsNone(photoindex._decade_of('[..1900]'))
        self.assertIsNone(photoindex._decade_of('[1900..]'))
        self.assertIsNone(photoindex._decade_of(None))

    def test_decade_of_routes_open_ended_slash_intervals_to_undated(self) -> None:
        # Codex P2: an open-ended slash interval ('1870/..', '../1875') names a
        # boundary, not one confident year - it must land in Undated exactly
        # like the bracket open forms, never bucketed by its known side alone
        # (an unbounded "after 1870" would otherwise look as precise as a real
        # 1870s photo).
        self.assertIsNone(photoindex._decade_of('1870/..'))
        self.assertIsNone(photoindex._decade_of('../1875'))
        # A closed range still buckets by its start year (unaffected).
        self.assertEqual(photoindex._decade_of('1912/1915'), 1910)

    def test_decade_of_routes_bare_comma_sets_to_undated(self) -> None:
        # Codex P2: a bare comma-separated EDTF set ('1912,1913' - _edtf_slug's
        # own docstring calls this a legitimate "set/choice" form) named no
        # single confident year, but fell through the bracket-only set guard
        # and got bucketed from its first choice's digits alone.
        self.assertIsNone(photoindex._decade_of('1912,1913'))

    def test_humanize_edtf_preserves_ranges_and_bracket_qualifiers(self) -> None:
        # Codex P2: a slash range or a bracket-qualified bound used to render as
        # only its first/boundary year, turning an uncertain span into a
        # specific-looking date in the gallery label ('1912/1915' -> '1912';
        # '[..1900]' -> '1900'). Both must keep their range/qualifier in the
        # plain-language label instead of silently narrowing to one year.
        h = photoindex._humanize_edtf
        self.assertEqual(h('1912/1915'), '1912 to 1915')
        self.assertEqual(h('1912~/1915'), 'about 1912 to 1915')
        self.assertEqual(h('[..1900]'), 'before 1900')
        self.assertEqual(h('[1900..]'), 'after 1900')
        # Unchanged behavior for a plain (non-interval, non-bracket) date.
        self.assertEqual(h('1920-01~'), 'about January 1920')
        self.assertEqual(h('192X'), '1920s')
        self.assertEqual(h(None), 'Undated')

    def test_gallery_decade_from_edtf_literal_and_undated_photo_in_tail(self) -> None:
        # End to end: a month-approximate 1920 date lands in the 1920s section
        # (never the 1910s the old midpoint gave), and a photo that resolves to
        # no date at all falls to the Undated tail rather than being bucketed
        # from anything it happens to carry.
        #
        # This used to feed the tail a "[..1900]" open range through a keyword.
        # Since 2026-08-16 only SPEC §20's letter grammar resolves, and that
        # grammar cannot express an open range, so no scan can produce one;
        # `_decade_of`'s open-range handling is covered directly in
        # test_decade_of_reads_edtf_literal_not_bounds_midpoint. Here the
        # undated photo is a keyword that affirms no component ('DATE: Y'),
        # which is what an unresolvable date looks like in practice.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            self._stage(archive, {
                'portrait_1880.jpg': {'Keywords': ['DATE: Y!M~', 'dtest'],
                                      'DateTimeOriginal': '1920:01:15 00:00:00'},
                'wedding_1902.jpg': {'Keywords': ['DATE: Y', 'dtest'],
                                     'DateTimeOriginal': '1902:06:14 00:00:00'},
            }, cfg)

            result = photoindex.run_gallery(archive, cfg, keyword='dtest')
            html = self._read(result['written'])

            self.assertIn('<h2>1920s</h2>', html)
            self.assertNotIn('<h2>1910s</h2>', html)
            self.assertIn('<h2>Undated</h2>', html)
            i_undated = html.index('<h2>Undated</h2>')
            # The undated photo is under Undated, not in any numeric decade.
            self.assertIn('wedding_1902.jpg', html[i_undated:])
            self.assertLess(html.index('<h2>1920s</h2>'), i_undated)

    def test_gallery_renders_group_with_torn_photo_groups_row(self) -> None:
        # F2: a matched group whose photo_groups row is missing (torn cache)
        # still renders from a member path instead of being silently dropped -
        # mirroring run_find's `or path` fallback.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            self._stage(
                archive,
                {'portrait_1880.jpg': {'Keywords': ['DATE: Y!', 'torn'],
                                       'DateTimeOriginal': '1880:01:01 00:00:00'}},
                cfg,
                extra_files=('portrait_1880b.jpg',),
            )

            cache_db = archive / '.cache' / 'photos.sqlite'
            conn = sqlite3.connect(cache_db)
            try:
                conn.execute('DELETE FROM photo_groups')
                conn.commit()
            finally:
                conn.close()

            result = photoindex.run_gallery(archive, cfg, keyword='torn')

            self.assertEqual(result.exit_code, EXIT_CLEAN)
            self.assertEqual(result['matched'], 1)
            html = self._read(result['written'])
            self.assertEqual(html.count('<article class="photo-row">'), 1)
            self.assertIn('portrait_1880.jpg', html)

    def test_gallery_excludes_reconciled_missing_rows(self) -> None:
        # Codex P2: reconcile keeps a vanished file's row queryable (caption/
        # keyword history) but prefixes its path 'MISSING:' - a synthetic key
        # that was never a real file. A gallery page exists to be clicked, so
        # a group whose every variant has vanished must drop out of the page
        # entirely, while a still-present sibling match still renders.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            self._stage(archive, {
                'family_reunion.jpg': {'Keywords': ['gone']},
                'wedding_1902.jpg': {'Keywords': ['gone']},
            }, cfg)

            (archive / 'photos' / 'family_reunion.jpg').unlink()
            reconcile_result = photoindex.run_reconcile(archive, cfg, with_exif=False)
            self.assertEqual(reconcile_result['missing'], ['MISSING:photos/family_reunion.jpg'])

            result = photoindex.run_gallery(archive, cfg, keyword='gone')
            html = self._read(result['written'])

            self.assertEqual(result['matched'], 1)
            self.assertIn('wedding_1902.jpg', html)
            self.assertNotIn('family_reunion.jpg', html)
            self.assertNotIn('MISSING:', html)

    def test_gallery_group_with_some_missing_variants_renders_from_present_ones(self) -> None:
        # A logical photo whose front scan vanished but whose back scan is
        # still on disk (family_reunion above has no back; portrait_1880's
        # fixture pair does) must still render - from the still-present
        # variant - rather than exposing the vanished front as a tile/chip or
        # dropping the whole group just because one variant is gone.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            self._stage(archive, {'portrait_1880.jpg': {'Keywords': ['gone']}}, cfg)

            (archive / 'photos' / 'portrait_1880.jpg').unlink()
            photoindex.run_reconcile(archive, cfg, with_exif=False)

            result = photoindex.run_gallery(archive, cfg, keyword='gone')
            html = self._read(result['written'])

            self.assertEqual(result['matched'], 1)
            self.assertIn('portrait_1880-back.jpg', html)
            self.assertNotIn('MISSING:', html)

    def test_gallery_all_matches_missing_writes_nothing_exits_clean(self) -> None:
        # Codex P2: a keyword/text/person filter can still match at the SQL
        # level when every matching row is MISSING: (reconcile keeps the
        # metadata queryable), so the earlier `if not matched_groups` guard
        # does not fire - but if _build_gallery_rows then drops every one of
        # those groups, the zero-match contract (no write, clean exit) must
        # still hold, not an empty clickable page reporting matched: 0.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            self._stage(archive, {'family_reunion.jpg': {'Keywords': ['onlygone']}}, cfg)

            (archive / 'photos' / 'family_reunion.jpg').unlink()
            photoindex.run_reconcile(archive, cfg, with_exif=False)

            result = photoindex.run_gallery(archive, cfg, keyword='onlygone')

            self.assertEqual(result.exit_code, EXIT_CLEAN)
            self.assertEqual(result['matched'], 0)
            self.assertIsNone(result['written'])
            self.assertFalse((archive / 'generated' / 'gallery').exists())

    def test_gallery_verify_tag_command_quotes_paths_with_spaces(self) -> None:
        # Codex P2: the generated tag-person command must stay copy-pasteable
        # even when the weak match's catalog path has a space (or other shell
        # punctuation) - an unquoted --paths value would split into two shell
        # arguments when pasted into a terminal.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            self._make_index(
                archive,
                persons=[('p-de957bcda1', 'Margaret Hartley')],
                face_tags=[('p-de957bcda1', 'Maggie')],
            )
            self._stage(archive, {
                'family reunion.jpg': {
                    'RegionInfo': {'RegionList': [{'Name': 'Maggie', 'Type': 'Face'}]},
                },
            }, cfg, extra_files=('family reunion.jpg',))

            result = photoindex.run_gallery(archive, cfg, person='P-de957bcda1')
            html = self._read(result['written'])

            self.assertIn('Verify these', html)
            # Autoescape renders the shell-quoting single-quotes as &#39;.
            self.assertIn('--paths &#39;photos/family reunion.jpg&#39;', html)

    def test_gallery_verify_tag_command_matches_first_rendered_row(self) -> None:
        # Codex P2: the printed tag-person example must be the SAME row a
        # reader actually sees first in the Verify section - it used to be
        # drawn from the unsorted group-key set while the rows themselves
        # render from a separately sorted list, so the two could disagree
        # (and differ between runs of the identical command against
        # unchanged data).
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            self._make_index(
                archive,
                persons=[('p-de957bcda1', 'Margaret Hartley')],
                face_tags=[('p-de957bcda1', 'Maggie')],
            )
            self._stage(archive, {
                'portrait_1880.jpg': {
                    'Keywords': ['DATE: Y!'],
                    'DateTimeOriginal': '1880:01:01 00:00:00',
                    'RegionInfo': {'RegionList': [{'Name': 'Maggie', 'Type': 'Face'}]},
                },
                'wedding_1902.jpg': {
                    'Keywords': ['DATE: Y!'],
                    'DateTimeOriginal': '1902:01:01 00:00:00',
                    'RegionInfo': {'RegionList': [{'Name': 'Maggie', 'Type': 'Face'}]},
                },
            }, cfg)

            result = photoindex.run_gallery(archive, cfg, person='P-de957bcda1')
            html = self._read(result['written'])

            i_verify = html.index('Verify these')
            i_first_portrait = html.index('portrait_1880.jpg', i_verify)
            i_first_wedding = html.index('wedding_1902.jpg', i_verify)
            first_is_portrait = i_first_portrait < i_first_wedding
            expected_example = 'photos/portrait_1880.jpg' if first_is_portrait else 'photos/wedding_1902.jpg'
            self.assertIn(f'--paths {expected_example}', html)

    def test_gallery_verify_label_excludes_unrelated_face_region(self) -> None:
        # F3: a face-tag Verify row labels only the region name that maps to the
        # galleried person, never an unrelated person's face tagged on the same
        # photo.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            self._make_index(
                archive,
                persons=[('p-de957bcda1', 'Margaret Hartley'),
                         ('p-aaaaaaaaaa', 'A Stranger')],
                face_tags=[('p-de957bcda1', 'Maggie')],
            )
            self._stage(archive, {
                'portrait_1880.jpg': {
                    'RegionInfo': {'RegionList': [
                        {'Name': 'Maggie', 'Type': 'Face'},
                        {'Name': 'Stranger', 'Type': 'Face'},
                    ]},
                },
            }, cfg)

            result = photoindex.run_gallery(archive, cfg, person='P-de957bcda1')
            html = self._read(result['written'])

            self.assertIn('Verify these', html)
            # The apostrophes are HTML-escaped by the autoescaping template.
            self.assertIn('face region &#39;Maggie&#39;', html)   # the person's own tag
            self.assertNotIn('Stranger', html)                   # never the unrelated tag

    def test_gallery_verify_label_degrades_when_index_absent(self) -> None:
        # F3: with no fresh index (no tag->person mapping) a preserved face-tag
        # row degrades to a generic 'face region match' label rather than naming
        # faces it cannot attribute to the person.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            # Build a fresh index, scan (records the face-tag row), then drop the
            # index so the gallery build has no mapping available.
            self._make_index(
                archive,
                persons=[('p-de957bcda1', 'Margaret Hartley')],
                face_tags=[('p-de957bcda1', 'Maggie')],
            )
            self._stage(archive, {
                'portrait_1880.jpg': {
                    'RegionInfo': {'RegionList': [{'Name': 'Maggie', 'Type': 'Face'}]},
                },
            }, cfg)
            (archive / '.cache' / 'index.sqlite').unlink()

            result = photoindex.run_gallery(archive, cfg, person='P-de957bcda1')
            html = self._read(result['written'])

            self.assertIn('Verify these', html)
            self.assertIn('face region match', html)      # generic degrade
            self.assertNotIn('Maggie', html)              # no per-name attribution

    def test_gallery_person_plus_filter_filenames_are_distinct(self) -> None:
        # F4: a person gallery with extra filters appends the filter tokens, so
        # `--person X --keyword farm` and `--person X --keyword school` no longer
        # overwrite the same {slug}_{P-id}.html file.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            self._make_index(archive, persons=[('p-de957bcda1', 'Margaret Hartley')])
            self._stage(archive, {
                'family_reunion.jpg': {'Keywords': ['P-de957bcda1', 'farm', 'school']},
            }, cfg)

            farm = photoindex.run_gallery(
                archive, cfg, person='P-de957bcda1', keyword='farm')
            school = photoindex.run_gallery(
                archive, cfg, person='P-de957bcda1', keyword='school')

            self.assertNotEqual(farm['written'], school['written'])
            self.assertTrue(Path(farm['written']).name
                            .endswith('_P-de957bcda1_keyword-farm.html'))
            self.assertTrue(Path(school['written']).name
                            .endswith('_P-de957bcda1_keyword-school.html'))
            # Bare --person keeps the plain {slug}_{P-id}.html shape.
            bare = photoindex.run_gallery(archive, cfg, person='P-de957bcda1')
            self.assertEqual(Path(bare['written']).name,
                             'margaret-hartley_P-de957bcda1.html')

    def test_gallery_edtf_filename_keeps_qualifiers_distinct(self) -> None:
        # F6: distinct EDTF forms map to distinct file stems - the qualifier is
        # encoded as a readable token before slugifying, so 1912 and 1912~ do
        # not collide.
        with tempfile.TemporaryDirectory() as d:
            archive = Path(d)
            exact = photoindex._gallery_out_path(
                archive, None, None, None, '1912', None, None)
            approx = photoindex._gallery_out_path(
                archive, None, None, None, '1912~', None, None)
            uncertain = photoindex._gallery_out_path(
                archive, None, None, None, '1912?', None, None)
            interval = photoindex._gallery_out_path(
                archive, None, None, None, '1912/1915', None, None)
            before = photoindex._gallery_out_path(
                archive, None, None, None, '[..1900]', None, None)

            names = {p.name for p in (exact, approx, uncertain, interval, before)}
            self.assertEqual(len(names), 5)   # all five stay distinct
            self.assertEqual(exact.name, 'gallery_edtf-1912.html')
            self.assertEqual(approx.name, 'gallery_edtf-1912-approx.html')
            self.assertEqual(uncertain.name, 'gallery_edtf-1912-uncertain.html')
            self.assertEqual(interval.name, 'gallery_edtf-1912-to-1915.html')
            self.assertEqual(before.name, 'gallery_edtf-before-1900.html')

    def test_gallery_subtree_filters_that_slug_alike_stay_distinct(self) -> None:
        # Round 5: two real subtrees can differ only by punctuation _slugify
        # collapses - 'A/B' and 'A-B' both slug to 'a-b'. The first gallery
        # carries the generated marker, so the second run is allowed to
        # overwrite it and the human silently loses a page (#35 promised the
        # subtree participates in the landing name). Distinct --under values
        # must land on distinct files.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            photos = archive / 'photos'
            (photos / 'A' / 'B').mkdir(parents=True)
            (photos / 'A-B').mkdir(parents=True)
            (photos / 'A' / 'B' / 'nested.jpg').write_bytes(b'\xff\xd8\xff\xd9')
            (photos / 'A-B' / 'flat.jpg').write_bytes(b'\xff\xd8\xff\xd9')
            self._stage(archive, {
                'nested.jpg': {'Keywords': ['farm']},
                'flat.jpg': {'Keywords': ['farm']},
            }, cfg)

            nested = photoindex.run_gallery(archive, cfg, under='A/B')
            flat = photoindex.run_gallery(archive, cfg, under='A-B')

            self.assertNotEqual(nested['written'], flat['written'])
            # Both pages survive - the second run did not clobber the first.
            self.assertTrue(Path(nested['written']).is_file())
            self.assertTrue(Path(flat['written']).is_file())
            # Each page still shows only its own subtree's photo.
            self.assertIn('nested.jpg', self._read(nested['written']))
            self.assertNotIn('flat.jpg', self._read(nested['written']))
            # The readable stem survives: a human scanning the folder can still
            # tell which filter each file came from.
            self.assertIn('under-a-b', Path(nested['written']).name)
            self.assertIn('under-a-b', Path(flat['written']).name)

    def test_gallery_keyword_and_text_filters_that_slug_alike_stay_distinct(self) -> None:
        # The same lossy-slug collision, on the other filter values: a keyword
        # with a space and a keyword with a hyphen are different queries with
        # different results, so they cannot share one landing spot either.
        with tempfile.TemporaryDirectory() as d:
            archive = Path(d)
            spaced = photoindex._gallery_out_path(
                archive, None, None, 'farm work', None, None, None)
            hyphened = photoindex._gallery_out_path(
                archive, None, None, 'farm-work', None, None, None)
            self.assertNotEqual(spaced.name, hyphened.name)

            spaced_text = photoindex._gallery_out_path(
                archive, None, None, None, None, 'ferry road', None)
            hyphened_text = photoindex._gallery_out_path(
                archive, None, None, None, None, 'ferry-road', None)
            self.assertNotEqual(spaced_text.name, hyphened_text.name)

    def test_gallery_filter_filenames_are_stable_and_case_insensitive(self) -> None:
        # The disambiguating suffix must be stable across runs (a gallery is
        # regenerated in place, not accumulated), and must NOT split filters
        # that select exactly the same photos: --keyword and --under both match
        # case-insensitively, so 'Farm Work' and 'farm work' are one query.
        with tempfile.TemporaryDirectory() as d:
            archive = Path(d)
            first = photoindex._gallery_out_path(
                archive, None, None, 'farm work', None, None, None)
            second = photoindex._gallery_out_path(
                archive, None, None, 'farm work', None, None, None)
            upper = photoindex._gallery_out_path(
                archive, None, None, 'Farm Work', None, None, None)
            self.assertEqual(first.name, second.name)
            self.assertEqual(first.name, upper.name)

    def test_gallery_edtf_interval_with_a_bracket_half_stays_distinct(self) -> None:
        # '[..1900]/1910' and '1900/1910' are both valid EDTF and mean
        # different things, but the open-start bracket was only recognised on a
        # whole value - inside an interval it was dropped, landing both on
        # gallery_edtf-1900-to-1910.html.
        with tempfile.TemporaryDirectory() as d:
            archive = Path(d)
            names = {
                photoindex._gallery_out_path(
                    archive, None, None, None, e, None, None).name
                for e in ('[..1900]/1910', '1900/1910', '1900/[..1910]')
            }
            self.assertEqual(len(names), 3)

    def test_gallery_count_strip_excludes_verify_tail(self) -> None:
        # F5: the headline counts only the confirmed main-flow photos; the weak
        # name-only matches get a separate clause, not folded into "N photos of".
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            self._make_index(
                archive,
                persons=[('p-de957bcda1', 'Margaret Hartley')],
                variants=[('p-de957bcda1', 'Maggie')],
            )
            self._stage(archive, {
                'family_reunion.jpg': {'Keywords': ['P-de957bcda1']},   # confirmed
                'portrait_1880.jpg': {'RegionInfo': {'RegionList': [
                    {'Name': 'Maggie', 'Type': 'Face'}]}},              # name-match
            }, cfg)

            result = photoindex.run_gallery(archive, cfg, person='P-de957bcda1')
            html = self._read(result['written'])

            self.assertIn('1 photo of Margaret Hartley', html)
            self.assertIn('plus 1 matched by name only', html)
            self.assertEqual(result['counts']['total'], 1)
            self.assertEqual(result['counts']['verify'], 1)
            self.assertEqual(result['matched'], 2)   # whole page = confirmed + verify

    def test_gallery_stale_index_uses_bare_pid_for_title_and_filename(self) -> None:
        # F7: a stale index carries the person's OLD name, so the gallery falls
        # back to the bare P-id for both the title and the filename rather than a
        # possibly-outdated display name.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            self._make_index(archive, persons=[('p-de957bcda1', 'Margaret Hartley')])
            self._stage(archive, {'family_reunion.jpg': {'Keywords': ['P-de957bcda1']}}, cfg)

            # Stale the index: a person record newer than index.sqlite.
            people_dir = archive / 'people'
            people_dir.mkdir(exist_ok=True)
            (people_dir / 'hartley__margaret_P-de957bcda1.md').write_text(
                '---\nid: P-de957bcda1\nname: Margaret Hartley\n---\n', encoding='utf-8')
            os.utime(archive / '.cache' / 'index.sqlite', (1, 1))
            self.assertFalse(photoindex._index_is_fresh(archive))

            result = photoindex.run_gallery(archive, cfg, person='P-de957bcda1')

            self.assertEqual(Path(result['written']).name, 'P-de957bcda1.html')
            html = self._read(result['written'])
            self.assertIn('Photos of P-de957bcda1', html)
            self.assertNotIn('Margaret Hartley', html)

    def test_gallery_missing_css_warning_travels_through_result_and_cmd(self) -> None:
        # F9: a missing design/view.css is warned from the interface layer - the
        # engine returns the warning in the Result (never prints it), and
        # _cmd_gallery prints it to stderr while still exiting clean.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            self._stage(archive, {'portrait_1880.jpg': {'Keywords': ['csstest']}}, cfg)

            orig = photoindex.load_view_css
            photoindex.load_view_css = lambda label: (
                '', f'WARNING: design/view.css is missing - {label} will be unstyled')
            try:
                result = photoindex.run_gallery(archive, cfg, keyword='csstest')
                self.assertIn('design/view.css is missing', result.data['css_warning'])
                self.assertIn('the gallery', result.data['css_warning'])

                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = photoindex._cmd_gallery(self._args(archive, keyword='csstest'))
                self.assertEqual(code, EXIT_CLEAN)
                self.assertIn('design/view.css is missing', err.getvalue())
            finally:
                photoindex.load_view_css = orig

    def test_gallery_write_oserror_reports_plain_error_not_traceback(self) -> None:
        # Codex P2: a filesystem failure during the marker-guarded write
        # (permission denied, disk full, a bad --out target) must not leak a
        # raw OSError - run_gallery translates it to a RuntimeError _cmd_gallery
        # already knows how to report in plain language.
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            self._stage(archive, {'portrait_1880.jpg': {
                'Keywords': ['DATE: Y!'],
                'DateTimeOriginal': '1880:01:01 00:00:00'}}, cfg)

            orig = photoindex.write_generated_file

            def boom(*args, **kwargs):
                raise OSError(28, 'No space left on device')

            photoindex.write_generated_file = boom
            try:
                with self.assertRaises(RuntimeError):
                    photoindex.run_gallery(archive, cfg, edtf='188X')

                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = photoindex._cmd_gallery(self._args(archive, edtf='188X'))
                self.assertEqual(code, EXIT_FAILURE)
                self.assertIn('ERROR:', err.getvalue())
            finally:
                photoindex.write_generated_file = orig


class SetSummaryTests(unittest.TestCase):
    """`fha photoindex set-summary` (BUILD.md M3.5, plan 07).

    The exiftool seams (`_run_exiftool`, `_run_exiftool_read_comments`,
    `_run_exiftool_write_comment`) are monkeypatched by assignment like the
    rest of this file; every test runs against a temp copy of the photo
    fixture, never the real archive."""

    TEXT = 'Margaret Hartley and her father outside the Harlan farm, about 1912'

    def setUp(self) -> None:
        self._orig_run_exiftool = photoindex._run_exiftool
        self._orig_read = photoindex._run_exiftool_read_comments
        self._orig_write = photoindex._run_exiftool_write_comment

    def tearDown(self) -> None:
        photoindex._run_exiftool = self._orig_run_exiftool
        photoindex._run_exiftool_read_comments = self._orig_read
        photoindex._run_exiftool_write_comment = self._orig_write

    def _scan(self, archive: Path, user_comments: dict[str, str] | None = None) -> None:
        """Scan the fixture with optional canned UserComment values per filename."""
        comments = user_comments or {}

        def fake_exiftool(paths: list[Path]) -> list[dict]:
            rows = []
            for p in paths:
                row: dict = {'SourceFile': str(p)}
                if p.name in comments:
                    row['UserComment'] = comments[p.name]
                rows.append(row)
            return rows

        photoindex._run_exiftool = fake_exiftool
        photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

    def _fake_reads(self, comments_by_name: dict[str, str]):
        """A read-seam fake serving canned current comments by filename."""
        def fake(paths: list[Path]) -> dict:
            return {p: (comments_by_name.get(p.name), None) for p in paths}
        return fake

    # ── compose rule (pure function) ──────────────────────────────────────

    def test_compose_replaces_existing_ai_comment(self) -> None:
        composed, preserved = photoindex._compose_user_comment('AI: old summary', 'new text', False)
        self.assertEqual(composed, 'AI: new text')
        self.assertFalse(preserved)
        # The Model: prefix is the same AI convention (_AI_COMMENT_RE).
        composed, preserved = photoindex._compose_user_comment('Model: old', 'new text', False)
        self.assertEqual(composed, 'AI: new text')
        self.assertFalse(preserved)
        # No existing comment at all -> just the AI block.
        composed, preserved = photoindex._compose_user_comment(None, 'new text', False)
        self.assertEqual(composed, 'AI: new text')
        self.assertFalse(preserved)

    def test_compose_append_keeps_prior_ai_block(self) -> None:
        composed, preserved = photoindex._compose_user_comment('AI: old summary', 'new text', True)
        self.assertEqual(composed, 'AI: old summary\n\nAI: new text')
        self.assertFalse(preserved)

    def test_compose_preserves_human_text_verbatim_and_appends(self) -> None:
        human = 'Written on the back:\n  "Margaret, June 1912" '
        for append in (False, True):
            with self.subTest(append=append):
                composed, preserved = photoindex._compose_user_comment(human, 'new text', append)
                # Byte-for-byte: the human text survives untouched, AI block below.
                self.assertEqual(composed, human + '\n\nAI: new text')
                self.assertTrue(preserved)

    def test_compose_mixed_comment_replaces_only_trailing_ai_block(self) -> None:
        """A rerun on the mixed comment this command itself produces
        ('human caption\\n\\nAI: v1') must replace the trailing AI block,
        not stack 'AI: v2', 'AI: v3', ... forever - the human prefix is
        kept byte-for-byte."""
        mixed = 'Grandma wrote: June wedding.\n\nAI: v1'
        composed, preserved = photoindex._compose_user_comment(mixed, 'v2', False)
        self.assertEqual(composed, 'Grandma wrote: June wedding.\n\nAI: v2')
        self.assertTrue(preserved)
        # Rerunning on the result stays bounded: still exactly one AI block.
        composed, preserved = photoindex._compose_user_comment(composed, 'v3', False)
        self.assertEqual(composed, 'Grandma wrote: June wedding.\n\nAI: v3')
        self.assertTrue(preserved)
        # The Model: prefix is the same AI convention (_AI_COMMENT_RE).
        composed, preserved = photoindex._compose_user_comment(
            'human note\n\nModel: old summary', 'v2', False)
        self.assertEqual(composed, 'human note\n\nAI: v2')
        self.assertTrue(preserved)

    def test_compose_mixed_comment_append_keeps_old_ai_block(self) -> None:
        """--append on a mixed comment keeps the human prefix AND the old
        AI block, adding the new block below both."""
        mixed = 'Grandma wrote: June wedding.\n\nAI: v1'
        composed, preserved = photoindex._compose_user_comment(mixed, 'v2', True)
        self.assertEqual(composed, 'Grandma wrote: June wedding.\n\nAI: v1\n\nAI: v2')
        self.assertTrue(preserved)
        # A default (replace) run after that --append swaps only the final
        # block: the deliberately-kept v1 is now part of the retained text.
        composed, preserved = photoindex._compose_user_comment(composed, 'v3', False)
        self.assertEqual(composed, 'Grandma wrote: June wedding.\n\nAI: v1\n\nAI: v3')
        self.assertTrue(preserved)

    def test_compose_ambiguous_ai_markers_are_treated_as_human(self) -> None:
        """Ambiguity preserves: an AI marker that is not a blank-line-delimited
        FINAL paragraph is not clearly the tool's own trailing block - it may
        be, or may shield, human text, so the whole comment is kept and the
        new block appended."""
        cases = [
            'She wrote AI: on the back herself',                # mid-line marker
            'human line\nAI: one newline is not a blank line',  # no paragraph boundary
            'caption\n\nAI: v1\n\nMom added this note later',   # human note below old block
        ]
        for existing in cases:
            with self.subTest(existing=existing):
                composed, preserved = photoindex._compose_user_comment(existing, 'new', False)
                self.assertEqual(composed, existing + '\n\nAI: new')
                self.assertTrue(preserved)

    # ── engine: plan + apply ──────────────────────────────────────────────

    def test_set_summary_plan_requires_exactly_one_target_and_real_text(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan(archive)
            cfg = {'roots': {'photos': 'photos'}}
            with self.assertRaises(ValueError):   # neither target
                photoindex.run_set_summary_plan(archive, cfg, self.TEXT)
            with self.assertRaises(ValueError):   # both targets
                photoindex.run_set_summary_plan(
                    archive, cfg, self.TEXT,
                    path='photos/wedding_1902.jpg', group='SOURCE:s-123456789a',
                )
            with self.assertRaises(ValueError):   # empty text
                photoindex.run_set_summary_plan(
                    archive, cfg, '   ', path='photos/wedding_1902.jpg',
                )
            with self.assertRaises(ValueError):   # unknown group
                photoindex.run_set_summary_plan(
                    archive, cfg, self.TEXT, group='SOURCE:s-zzzzzzzzzz',
                )

    def test_group_addressing_writes_every_variant(self) -> None:
        """--group targets every member of a variation group; a path targets one file."""
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan(archive)
            cfg = {'roots': {'photos': 'photos'}}
            photoindex._run_exiftool_read_comments = self._fake_reads({})

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                group_id = conn.execute(
                    "SELECT group_id FROM photos WHERE path='photos/portrait_1880.jpg'"
                ).fetchone()[0]
            finally:
                conn.close()

            plan = photoindex.run_set_summary_plan(archive, cfg, self.TEXT, group=group_id)
            self.assertEqual(
                [row['path'] for row in plan['plan']],
                ['photos/portrait_1880-back.jpg', 'photos/portrait_1880.jpg'],
            )

            written_files: list[Path] = []

            def fake_write(items: list) -> dict:
                written_files.extend(p for p, _text in items)
                return {p: None for p, _text in items}

            photoindex._run_exiftool_write_comment = fake_write
            result = photoindex.run_set_summary(
                archive, cfg, self.TEXT, [row['path'] for row in plan['plan']],
            )
            self.assertEqual(len(written_files), 2)
            self.assertEqual(
                result['written'],
                ['photos/portrait_1880-back.jpg', 'photos/portrait_1880.jpg'],
            )

            # Path addressing plans exactly one file.
            plan_one = photoindex.run_set_summary_plan(
                archive, cfg, self.TEXT, path='photos/wedding_1902.jpg',
            )
            self.assertEqual([row['path'] for row in plan_one['plan']],
                             ['photos/wedding_1902.jpg'])

    def test_group_lookup_is_forgiving_about_source_id_case(self) -> None:
        """A human types the S-id as the source record shows it; the stored
        group key is the normalized lowercase form - both must resolve."""
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                rows = []
                for p in paths:
                    row: dict = {'SourceFile': str(p)}
                    if p.name == 'wedding_1902.jpg':
                        row['Keywords'] = ['SOURCE: S-123456789a']
                    rows.append(row)
                return rows

            photoindex._run_exiftool = fake_exiftool
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            cfg = {'roots': {'photos': 'photos'}}
            photoindex._run_exiftool_read_comments = self._fake_reads({})

            for spelling in ('SOURCE:S-123456789a', 'S-123456789A', 'source:s-123456789a'):
                with self.subTest(spelling=spelling):
                    plan = photoindex.run_set_summary_plan(
                        archive, cfg, self.TEXT, group=spelling,
                    )
                    self.assertEqual([row['path'] for row in plan['plan']],
                                     ['photos/wedding_1902.jpg'])

    def test_set_summary_never_touches_caption_abstract(self) -> None:
        """The real exiftool command lines must name only UserComment - the
        human-caption fields (Caption-Abstract / XMP-dc:Description) are
        contract-protected (SPEC §20 rule 5)."""
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan(archive)
            cfg = {'roots': {'photos': 'photos'}}

            commands: list[list[str]] = []

            class _FakeProc:
                returncode = 0
                stderr = ''
                stdout = '[{"UserComment": "AI: old"}]'

            orig_subprocess_run = photoindex.subprocess.run

            def fake_run(cmd, **kwargs):
                commands.append(list(cmd))
                return _FakeProc()

            photoindex.subprocess.run = fake_run
            try:
                result = photoindex.run_set_summary(
                    archive, cfg, self.TEXT, ['photos/wedding_1902.jpg'],
                )
            finally:
                photoindex.subprocess.run = orig_subprocess_run

            self.assertEqual(result['written'], ['photos/wedding_1902.jpg'])
            self.assertTrue(commands)
            write_cmds = [c for c in commands if any(a.startswith('-UserComment=') for a in c)]
            self.assertTrue(write_cmds, 'no -UserComment= write call was issued')
            for cmd in commands:
                joined = ' '.join(cmd)
                self.assertNotIn('Caption-Abstract', joined)
                self.assertNotIn('XMP-dc:Description', joined)
                self.assertNotIn('Description=', joined)

    def test_cache_and_fts_mirror_updated(self) -> None:
        """After a live write, photos.user_comment and a photo_fts match both
        see the new text with no rescan (the check-4 symmetry trap)."""
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan(archive)
            cfg = {'roots': {'photos': 'photos'}}
            photoindex._run_exiftool_read_comments = self._fake_reads({})
            photoindex._run_exiftool_write_comment = (
                lambda items: {p: None for p, _t in items}
            )

            result = photoindex.run_set_summary(
                archive, cfg, self.TEXT, ['photos/wedding_1902.jpg'],
            )
            self.assertEqual(result.exit_code, EXIT_CLEAN)
            self.assertEqual(result.changed, ['photos/wedding_1902.jpg'])

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                stored = conn.execute(
                    "SELECT user_comment FROM photos WHERE path='photos/wedding_1902.jpg'"
                ).fetchone()[0]
                self.assertEqual(stored, f'AI: {self.TEXT}')
                fts_hit = conn.execute(
                    "SELECT path FROM photo_fts WHERE photo_fts MATCH 'Harlan'"
                ).fetchall()
                self.assertEqual([row[0] for row in fts_hit], ['photos/wedding_1902.jpg'])
            finally:
                conn.close()

            # The full read path agrees: find --text sees it without a rescan.
            found = photoindex.run_find(archive, cfg, text='Harlan')
            self.assertEqual([r['path'] for r in found['rows']], ['photos/wedding_1902.jpg'])

    def test_stale_index_blocks_set_summary(self) -> None:
        """A stale catalog can address the wrong file for a mutating write -
        the CLI must hard-block (EXIT_FAILURE), not warn-and-continue, and
        must not read or write anything."""
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            people_dir = archive / 'people'
            people_dir.mkdir(exist_ok=True)
            person_file = people_dir / 'grandma_P-de957bcda1.md'
            person_file.write_text('---\nid: P-de957bcda1\n---\n', encoding='utf-8')
            self._scan(archive)
            db_mtime = (archive / '.cache' / 'photos.sqlite').stat().st_mtime
            future = db_mtime + 5
            os.utime(person_file, (future, future))

            def _fail(*a, **k):
                raise AssertionError('no exiftool call may happen on a stale index')

            photoindex._run_exiftool_read_comments = _fail
            photoindex._run_exiftool_write_comment = _fail

            args = type('Args', (), {
                'root': str(archive), 'path': 'photos/wedding_1902.jpg',
                'group': None, 'text': self.TEXT, 'append': False, 'dry_run': True,
            })()
            code = photoindex._cmd_set_summary(args)
            self.assertEqual(code, photoindex.EXIT_FAILURE)

    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan(archive, {'wedding_1902.jpg': 'AI: old summary'})
            db_path = archive / '.cache' / 'photos.sqlite'
            before = db_path.read_bytes()

            photoindex._run_exiftool_read_comments = self._fake_reads(
                {'wedding_1902.jpg': 'AI: old summary'})
            photoindex._run_exiftool_write_comment = lambda items: (
                _ for _ in ()).throw(AssertionError('dry-run must not write'))

            args = type('Args', (), {
                'root': str(archive), 'path': 'photos/wedding_1902.jpg',
                'group': None, 'text': self.TEXT, 'append': False, 'dry_run': True,
            })()
            code = photoindex._cmd_set_summary(args)

            self.assertEqual(code, photoindex.EXIT_CLEAN)
            self.assertEqual(db_path.read_bytes(), before)

    def test_partial_failure_reports_both_lists_and_exits_3(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan(archive, {'portrait_1880-back.jpg': 'AI: back caption'})
            cfg = {'roots': {'photos': 'photos'}}
            photoindex._run_exiftool_read_comments = self._fake_reads(
                {'portrait_1880-back.jpg': 'AI: back caption'})

            def fake_write(items: list) -> dict:
                return {
                    p: ('locked file' if p.name == 'portrait_1880-back.jpg' else None)
                    for p, _t in items
                }

            photoindex._run_exiftool_write_comment = fake_write
            result = photoindex.run_set_summary(
                archive, cfg, self.TEXT,
                ['photos/portrait_1880.jpg', 'photos/portrait_1880-back.jpg'],
            )

            self.assertEqual(result['written'], ['photos/portrait_1880.jpg'])
            self.assertEqual(result['failed'],
                             [('photos/portrait_1880-back.jpg', 'locked file')])
            self.assertIs(result.ok, False)
            self.assertEqual(result.exit_code, EXIT_FAILURE)

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                front = conn.execute(
                    "SELECT user_comment FROM photos WHERE path='photos/portrait_1880.jpg'"
                ).fetchone()[0]
                back = conn.execute(
                    "SELECT user_comment FROM photos WHERE path='photos/portrait_1880-back.jpg'"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(front, f'AI: {self.TEXT}')   # written path mirrored
            self.assertEqual(back, 'AI: back caption')    # failed path untouched

    def test_non_ascii_summary_round_trip(self) -> None:
        """EXIF UserComment has UTF-8/Latin-1 encoding quirks - a summary with
        ę/ü/ł must survive compose -> write args -> cache mirror unchanged."""
        text = 'Zdjęcie Małgorzaty w Suwałkach, ürodziny'
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan(archive)
            cfg = {'roots': {'photos': 'photos'}}
            photoindex._run_exiftool_read_comments = self._fake_reads({})

            written_args: list[str] = []

            def fake_write(items: list) -> dict:
                written_args.extend(t for _p, t in items)
                return {p: None for p, _t in items}

            photoindex._run_exiftool_write_comment = fake_write
            result = photoindex.run_set_summary(
                archive, cfg, text, ['photos/wedding_1902.jpg'],
            )
            self.assertEqual(result['written'], ['photos/wedding_1902.jpg'])
            self.assertEqual(written_args, [f'AI: {text}'])

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                stored = conn.execute(
                    "SELECT user_comment FROM photos WHERE path='photos/wedding_1902.jpg'"
                ).fetchone()[0]
                self.assertEqual(stored, f'AI: {text}')
                fts_hit = conn.execute(
                    "SELECT path FROM photo_fts WHERE photo_fts MATCH 'Suwałkach'"
                ).fetchall()
                self.assertEqual([row[0] for row in fts_hit], ['photos/wedding_1902.jpg'])
            finally:
                conn.close()

    def test_set_summary_rerun_on_mixed_comment_does_not_accumulate(self) -> None:
        """End-to-end regression for the accumulation bug: two engine runs on
        a photo with a human caption leave exactly one AI block in the write
        args and in the photos/photo_fts mirror, human prefix intact."""
        human = 'Grandma wrote: June wedding.'
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan(archive, {'wedding_1902.jpg': human})
            cfg = {'roots': {'photos': 'photos'}}

            written_args: list[str] = []

            def fake_write(items: list) -> dict:
                written_args.extend(t for _p, t in items)
                return {p: None for p, _t in items}

            photoindex._run_exiftool_write_comment = fake_write

            # First run: appends the AI block below the human caption.
            photoindex._run_exiftool_read_comments = self._fake_reads(
                {'wedding_1902.jpg': human})
            result = photoindex.run_set_summary(
                archive, cfg, 'v1', ['photos/wedding_1902.jpg'])
            self.assertEqual(result['written'], ['photos/wedding_1902.jpg'])
            self.assertEqual(written_args, [f'{human}\n\nAI: v1'])

            # Second run reads what the first wrote: the trailing AI block is
            # replaced, not stacked ('AI: v1' must not survive beside 'AI: v2').
            photoindex._run_exiftool_read_comments = self._fake_reads(
                {'wedding_1902.jpg': f'{human}\n\nAI: v1'})
            result = photoindex.run_set_summary(
                archive, cfg, 'v2', ['photos/wedding_1902.jpg'])
            self.assertEqual(result['written'], ['photos/wedding_1902.jpg'])
            self.assertEqual(result['preserved_human'], ['photos/wedding_1902.jpg'])
            self.assertEqual(written_args[-1], f'{human}\n\nAI: v2')

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                stored = conn.execute(
                    "SELECT user_comment FROM photos WHERE path='photos/wedding_1902.jpg'"
                ).fetchone()[0]
                fts = conn.execute(
                    "SELECT user_comment FROM photo_fts WHERE path='photos/wedding_1902.jpg'"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(stored, f'{human}\n\nAI: v2')
            self.assertEqual(fts, f'{human}\n\nAI: v2')

    def test_numeric_user_comment_is_read_as_text_not_discarded(self) -> None:
        """exiftool -j emits a numeric-looking caption ('1912') as a JSON
        number; it must read back as the string '1912', not as no-comment -
        the (None, None) misread previewed 'now: (none)' and let the write
        destroy a human caption."""
        p = Path('x.jpg')

        def fake_run_factory(stdout: str):
            class _FakeProc:
                returncode = 0
                stderr = ''

            proc = _FakeProc()
            proc.stdout = stdout
            return lambda cmd, **kwargs: proc

        orig_subprocess_run = photoindex.subprocess.run
        try:
            photoindex.subprocess.run = fake_run_factory('[{"UserComment": 1912}]')
            reads = photoindex._run_exiftool_read_comments([p])
            self.assertEqual(reads[p], ('1912', None))

            # A genuinely absent UserComment still reads as no-comment.
            photoindex.subprocess.run = fake_run_factory('[{"SourceFile": "x.jpg"}]')
            reads = photoindex._run_exiftool_read_comments([p])
            self.assertEqual(reads[p], (None, None))
        finally:
            photoindex.subprocess.run = orig_subprocess_run

        # And the compose rule treats the coerced text as a human caption.
        composed, preserved = photoindex._compose_user_comment('1912', 'new', False)
        self.assertEqual(composed, '1912\n\nAI: new')
        self.assertTrue(preserved)

    def test_run_set_summary_engine_refuses_empty_text(self) -> None:
        """The engine must refuse empty/whitespace text like the plan does -
        a headless caller skipping the plan would otherwise compose a bare
        'AI: ' block over an existing summary in file, cache, and FTS."""
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}

            def _fail(*a, **k):
                raise AssertionError('empty text must be refused before any exiftool call')

            photoindex._run_exiftool_read_comments = _fail
            photoindex._run_exiftool_write_comment = _fail
            for empty in ('', '   ', None):
                with self.subTest(text=empty):
                    with self.assertRaises(ValueError):
                        photoindex.run_set_summary(
                            archive, cfg, empty, ['photos/wedding_1902.jpg'])

    def test_decline_prompt_writes_nothing(self) -> None:
        """'n' and EOF (closed stdin) both decline; nothing is written."""
        for answer in ('n', EOFError):
            with self.subTest(answer=answer), tempfile.TemporaryDirectory() as d:
                archive = _copy_fixture(Path(d))
                self._scan(archive)
                db_path = archive / '.cache' / 'photos.sqlite'
                before = db_path.read_bytes()

                photoindex._run_exiftool_read_comments = self._fake_reads({})
                photoindex._run_exiftool_write_comment = lambda items: (
                    _ for _ in ()).throw(AssertionError('declined prompt must not write'))

                if answer is EOFError:
                    def fake_input(prompt=''):
                        raise EOFError
                else:
                    def fake_input(prompt=''):
                        return answer

                args = type('Args', (), {
                    'root': str(archive), 'path': 'photos/wedding_1902.jpg',
                    'group': None, 'text': self.TEXT, 'append': False, 'dry_run': False,
                })()
                orig_input = builtins.input
                builtins.input = fake_input
                try:
                    code = photoindex._cmd_set_summary(args)
                finally:
                    builtins.input = orig_input

                self.assertEqual(code, photoindex.EXIT_CLEAN)
                self.assertEqual(db_path.read_bytes(), before)

    def test_cmd_set_summary_confirms_and_writes(self) -> None:
        """The full CLI arm: preview -> y -> write -> cache mirrored, exit 0."""
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self._scan(archive, {'wedding_1902.jpg': 'Grandma wrote: June wedding.'})

            photoindex._run_exiftool_read_comments = self._fake_reads(
                {'wedding_1902.jpg': 'Grandma wrote: June wedding.'})
            photoindex._run_exiftool_write_comment = (
                lambda items: {p: None for p, _t in items}
            )

            args = type('Args', (), {
                'root': str(archive), 'path': 'photos/wedding_1902.jpg',
                'group': None, 'text': self.TEXT, 'append': False, 'dry_run': False,
            })()
            orig_input = builtins.input
            builtins.input = lambda prompt='': 'y'
            try:
                code = photoindex._cmd_set_summary(args)
            finally:
                builtins.input = orig_input

            self.assertEqual(code, photoindex.EXIT_CLEAN)
            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                stored = conn.execute(
                    "SELECT user_comment FROM photos WHERE path='photos/wedding_1902.jpg'"
                ).fetchone()[0]
            finally:
                conn.close()
            # The human text survives verbatim with the AI block below it.
            self.assertEqual(stored, f'Grandma wrote: June wedding.\n\nAI: {self.TEXT}')


if __name__ == '__main__':
    unittest.main()
