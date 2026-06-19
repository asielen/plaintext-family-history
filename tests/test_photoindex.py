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
from _lib import parse_media_filename


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

    def test_deferred_photoindex_subcommands_accept_documented_arguments(self) -> None:
        commands = [
            ['find', '--person', 'P-de957bcda1', '--root', 'tests/fixtures/photo-fixture'],
            ['find', '--text', 'cemetery', '--root', 'tests/fixtures/photo-fixture'],
            ['triage', '--top', '10', '--root', 'tests/fixtures/photo-fixture'],
            ['tag-person', 'P-de957bcda1', '--root', 'tests/fixtures/photo-fixture'],
            [
                'tag-person', 'P-de957bcda1', '--from-face-tag', 'Grandma',
                '--root', 'tests/fixtures/photo-fixture',
            ],
            [
                'tag-person', 'P-de957bcda1', '--paths', 'photos/a.jpg', 'photos/b.jpg',
                '--root', 'tests/fixtures/photo-fixture',
            ],
            ['reconcile', '--root', 'tests/fixtures/photo-fixture'],
            ['report', '--root', 'tests/fixtures/photo-fixture'],
        ]

        for args in commands:
            with self.subTest(args=args):
                proc = subprocess.run(
                    [sys.executable, 'tools/fha.py', 'photoindex'] + args,
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn('deferred to a follow-up photoindex PR', proc.stdout)

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


if __name__ == '__main__':
    unittest.main()
