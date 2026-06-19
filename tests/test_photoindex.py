import argparse
import builtins
import os
import subprocess
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import photoindex
from _lib import newest_person_record_mtime, parse_media_filename, photoindex_status


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

    def test_scan_groups_variants_flags_date_conflict_and_indexes_pid_keyword(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))

            def fake_exiftool(paths: list[Path]) -> list[dict]:
                rows = {
                    'portrait_1880.jpg': {
                        'Keywords': ['DATE: 1880!'],
                        'Title': 'Portrait front',
                    },
                    'portrait_1880-back.jpg': {
                        'Keywords': ['DATE: 1881!'],
                        'Title': 'Portrait back',
                    },
                    'wedding_1902.jpg': {
                        'Keywords': ['SOURCE: S-123456789a', 'DATE: 1902!'],
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
            conn, _needs_face_backfill = photoindex._get_db(cache_dir)
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
        # digit ('034b') as copy-variant grammar — 'scan_a'/'scan_b' must stay
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
                    'portrait_1880.jpg': {'Keywords': ['DATE: 1880!']},
                    'portrait_1880-back.jpg': {'Keywords': ['DATE: 1881!']},
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

    def test_stale_index_is_not_used_for_weak_face_or_name_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            cache = archive / '.cache'
            cache.mkdir(exist_ok=True)
            index_db = cache / 'index.sqlite'
            conn = sqlite3.connect(index_db)
            try:
                conn.executescript(
                    """
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
                    """
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
                    """
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

    def test_run_exiftool_fails_on_documented_error_exit_status(self) -> None:
        class FakeProc:
            returncode = 1
            stdout = '[]'
            stderr = 'Error: File not found - missing.jpg'

        orig_run = subprocess.run
        subprocess.run = lambda *a, **k: FakeProc()
        try:
            with self.assertRaisesRegex(RuntimeError, 'exiftool failed'):
                photoindex._run_exiftool([Path('missing.jpg')])
        finally:
            subprocess.run = orig_run

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

    def test_missing_exiftool_row_fails_without_refreshing_stale_metadata(self) -> None:
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

            changed = archive / 'photos' / 'family_reunion.jpg'
            os.utime(changed, None)

            def missing_one_exiftool(paths: list[Path]) -> list[dict]:
                return [
                    {'SourceFile': str(p), 'Title': f'second {p.name}'}
                    for p in paths
                    if p.name != 'family_reunion.jpg'
                ]

            photoindex._run_exiftool = missing_one_exiftool
            with self.assertRaisesRegex(RuntimeError, 'did not return metadata'):
                photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                title = conn.execute(
                    "SELECT title FROM photos WHERE path LIKE '%family_reunion.jpg'"
                ).fetchone()[0]
                self.assertEqual(title, 'first family_reunion.jpg')
            finally:
                conn.close()

    def test_photoindex_subcommands_are_registered_in_the_cli(self) -> None:
        """`fha photoindex <subcommand> --help` should resolve for every M3.1-M3.4 subcommand."""
        for name in ('find', 'triage', 'report', 'reconcile', 'tag-person'):
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

    def _scan_with_find_fixture(self, archive: Path) -> None:
        """Scan with a fixed exiftool payload exercising person/keyword/edtf/text filters."""
        def fake_exiftool(paths: list[Path]) -> list[dict]:
            rows = {
                'portrait_1880.jpg': {
                    'Keywords': ['DATE: 1880!'], 'Title': 'Portrait front',
                },
                'portrait_1880-back.jpg': {
                    'Keywords': ['DATE: 1880!'], 'Caption-Abstract': 'cemetery visit',
                },
                'wedding_1902.jpg': {
                    'Keywords': ['SOURCE: S-123456789a', 'DATE: 1902!'],
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
                    'portrait_1880.jpg': {'Keywords': ['DATE: 1880!']},
                    'portrait_1880-back.jpg': {'Caption-Abstract': 'cemetery visit'},
                    'wedding_1902.jpg': {'Keywords': ['DATE: 1902!']},
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
                    'portrait_1880.jpg': {'Keywords': ['DATE: 1880!']},
                    'portrait_1880-back.jpg': {'Caption-Abstract': 'untagged back'},
                    'wedding_1902.jpg': {'Keywords': ['DATE: 1902!']},
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
            # wedding_1902.jpg already carries a SOURCE: keyword (processed) — excluded.
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
                    'portrait_1880.jpg': {'Keywords': ['DATE: 1880!']},
                    'portrait_1880-back.jpg': {
                        'Keywords': ['DATE: 1881!'], 'Caption-Abstract': 'written 1881',
                    },
                    'wedding_1902.jpg': {
                        'Keywords': ['SOURCE: S-123456789a', 'DATE: 1902!'],
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
                    'portrait_1880.jpg': {'Keywords': ['DATE: 1880!']},
                    'portrait_1880-back.jpg': {'Keywords': ['DATE: 1881!']},
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

    def test_reconcile_already_missing_row_is_not_reprocessed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            photoindex._run_exiftool = lambda paths: [{'SourceFile': str(p)} for p in paths]
            photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

            (archive / 'photos' / 'portrait_1880.jpg').unlink()
            fha_config = {'roots': {'photos': 'photos'}}
            first = photoindex.run_reconcile(archive, fha_config)
            self.assertEqual(first['missing'], ['MISSING:photos/portrait_1880.jpg'])

            second = photoindex.run_reconcile(archive, fha_config)
            self.assertEqual(second['missing'], [])

            conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM photos WHERE path LIKE 'MISSING:MISSING:%'"
                ).fetchone()[0]
                self.assertEqual(count, 0)
            finally:
                conn.close()

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
                "INSERT INTO photo_people(path, person_ref, via) "
                "VALUES ('photos/family_reunion.jpg', 'p-de957bcda1', 'pid-keyword')"
            )
            conn.commit()
            conn.close()

            plan = photoindex.run_tag_person_plan(
                archive, {'roots': {'photos': 'photos'}}, 'P-de957bcda1',
                from_face_tag='Grandma',
            )

            self.assertEqual(plan['candidates'], [])
            self.assertEqual(plan['already_tagged'], ['photos/family_reunion.jpg'])

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


if __name__ == '__main__':
    unittest.main()
