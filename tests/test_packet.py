import os
import sqlite3
import sys
import tempfile
import time
import unittest
import unittest.mock
import zipfile
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import packet
from index import _DDL as INDEX_DDL
from photoindex import _DDL as PHOTOS_DDL


def _make_index(archive_root: Path) -> sqlite3.Connection:
    cache = archive_root / '.cache'
    cache.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(cache / 'index.sqlite'))
    conn.executescript(INDEX_DDL)
    conn.row_factory = sqlite3.Row
    return conn


def _make_photos_db(archive_root: Path) -> sqlite3.Connection:
    cache = archive_root / '.cache'
    cache.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(cache / 'photos.sqlite'))
    conn.executescript(PHOTOS_DDL)
    conn.row_factory = sqlite3.Row
    return conn


def _insert_claim(conn, cid, source_id, ctype, value, *, date_edtf=None,
                  place_text=None, status='accepted', persons=()):
    conn.execute(
        '''INSERT INTO claims(id, source_id, type, date_edtf, place_text, value, status)
           VALUES (?,?,?,?,?,?,?)''',
        (cid, source_id, ctype, date_edtf, place_text, value, status),
    )
    for pos, pid in enumerate(persons):
        conn.execute(
            'INSERT INTO claim_persons(claim_id, person_id, position) VALUES (?,?,?)',
            (cid, pid, pos),
        )


class PacketTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        # The resolve_root_arg chokepoint refuses a --root that carries no
        # fha.yaml (round-2 finding 10), so the CLI-path test needs the
        # fixture to look like a real archive, not just a dir with a .cache.
        (self.archive_root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        self.conn = _make_index(self.archive_root)
        self.out_dir = self.archive_root / 'out'

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _commit_fresh(self) -> None:
        self.conn.commit()
        future = time.time() + 5
        os.utime(self.archive_root / '.cache' / 'index.sqlite', (future, future))

    def _seed_person(self, pid='p-aaaaaaaaaa', name='Test Person', living='false',
                     tier='curated', surname='Person', status='active', merged_into=None):
        profile_path = self.archive_root / 'people' / f'{surname.lower()}__test_{pid}.md'
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        profile_path.write_text(f'---\nid: {pid}\nname: {name}\n---\n# {name}\n', encoding='utf-8')
        rel = profile_path.relative_to(self.archive_root).as_posix()
        self.conn.execute(
            'INSERT INTO persons(id, name, surname, living, tier, status, merged_into, path) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (pid, name, surname, living, tier, status, merged_into, rel),
        )
        return profile_path

    def _seed_source(self, sid, title, *, restricted=0, source_type=None, asset_rel=None,
                     create_asset=True, persons=('p-aaaaaaaaaa',)):
        src_path = self.archive_root / 'sources' / 'other' / f'{sid}.md'
        src_path.parent.mkdir(parents=True, exist_ok=True)
        src_path.write_text(f'---\nid: {sid}\ntitle: {title}\n---\n## Claims\n', encoding='utf-8')
        rel = src_path.relative_to(self.archive_root).as_posix()
        self.conn.execute(
            'INSERT INTO sources(id, title, source_type, restricted, path) VALUES (?,?,?,?,?)',
            (sid, title, source_type, restricted, rel),
        )
        for pid in persons:
            self.conn.execute(
                'INSERT INTO source_people(source_id, person_id) VALUES (?,?)', (sid, pid),
            )
        if asset_rel:
            if create_asset:
                asset_path = self.archive_root / asset_rel
                asset_path.parent.mkdir(parents=True, exist_ok=True)
                asset_path.write_bytes(b'fake-bytes')
            self.conn.execute(
                'INSERT INTO source_files(source_id, path) VALUES (?,?)', (sid, asset_rel),
            )
        return src_path

    def test_not_found(self):
        self._commit_fresh()
        result = packet.run_packet(self.archive_root, 'p-zzzzzzzzzz', self.out_dir)
        self.assertEqual(result['status'], 'not-found')

    def test_not_curated_refused(self):
        self._seed_person(tier='stub')
        self._commit_fresh()
        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir)
        self.assertEqual(result['status'], 'not-curated')

    def test_living_subject_refused(self):
        self._seed_person(living='unknown')
        self._commit_fresh()
        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)
        self.assertEqual(result['status'], 'living-subject')
        self.assertFalse(self.out_dir.exists())

    def test_merged_tombstone_redirects_to_survivor(self):
        self._seed_person(pid='p-bbbbbbbbbb', name='Survivor Person', surname='Survivor')
        self._seed_person(
            pid='p-aaaaaaaaaa', name='Old Record', surname='Old',
            status='merged', merged_into='p-bbbbbbbbbb',
        )
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)

        self.assertEqual(result['status'], 'ok')
        self.assertTrue(any('merged into' in m for m in result['messages']))
        readme = (result['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertIn('Survivor Person', readme)

    def test_merge_redirect_to_living_survivor_refuses(self):
        self._seed_person(pid='p-bbbbbbbbbb', name='Survivor Person', surname='Survivor', living='true')
        self._seed_person(
            pid='p-aaaaaaaaaa', name='Old Record', surname='Old',
            status='merged', merged_into='p-bbbbbbbbbb',
        )
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)

        self.assertEqual(result['status'], 'living-subject')

    def test_merge_chain_cycle_does_not_hang(self):
        self._seed_person(
            pid='p-aaaaaaaaaa', name='A', surname='A',
            status='merged', merged_into='p-bbbbbbbbbb',
        )
        self._seed_person(
            pid='p-bbbbbbbbbb', name='B', surname='B',
            status='merged', merged_into='p-aaaaaaaaaa',
        )
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)

        self.assertTrue(any('cycle detected' in m for m in result['messages']))

    def test_stale_index_refuses_before_export(self):
        profile = self._seed_person()
        self._seed_source('s-1111111111', 'Source One')
        self._commit_fresh()
        future = time.time() + 10
        os.utime(profile, (future, future))

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)
        self.assertEqual(result['status'], 'no-index')
        self.assertFalse(self.out_dir.exists())

    def test_basic_packet_zips_profile_and_sources(self):
        self._seed_person()
        self._seed_source('s-1111111111', 'Source One', asset_rel='documents/other/file1.txt')
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'birth', 'born 1900',
                      date_edtf='1900', persons=['p-aaaaaaaaaa'])
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)
        self.assertEqual(result['status'], 'ok')
        packet_dir = result['packet_dir']
        self.assertTrue((packet_dir / 'README.txt').exists())
        self.assertTrue((packet_dir / 'timeline.md').exists())
        self.assertTrue(any((packet_dir / 'profile').iterdir()))
        self.assertTrue(any((packet_dir / 'sources').iterdir()))
        self.assertTrue((packet_dir / 'files' / 'file1.txt').exists())

        timeline_text = (packet_dir / 'timeline.md').read_text(encoding='utf-8')
        self.assertIn('born 1900', timeline_text)

        self.assertTrue(result['zip_path'].exists())
        with zipfile.ZipFile(result['zip_path']) as zf:
            names = zf.namelist()
        self.assertTrue(any(n.endswith('README.txt') for n in names))
        self.assertTrue(any(n.endswith('file1.txt') for n in names))

    def test_missing_source_asset_reported_in_readme_and_messages(self):
        self._seed_person()
        self._seed_source(
            's-1111111111', 'Source One',
            asset_rel='documents/other/missing.txt', create_asset=False,
        )
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(any('missing on disk' in m for m in result['messages']))
        readme = (result['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertIn('Missing files', readme)
        self.assertIn('documents/other/missing.txt', readme)

    def test_existing_output_refused_without_overwrite(self):
        self._seed_person()
        self._commit_fresh()
        first = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)
        sentinel = first['packet_dir'] / 'sentinel.txt'
        sentinel.write_text('keep', encoding='utf-8')

        second = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)
        self.assertEqual(second['status'], 'output-exists')
        self.assertTrue(sentinel.exists())

    def test_overwrite_replaces_existing_output(self):
        self._seed_person()
        self._commit_fresh()
        first = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)
        sentinel = first['packet_dir'] / 'sentinel.txt'
        sentinel.write_text('remove', encoding='utf-8')

        second = packet.run_packet(
            self.archive_root, 'p-aaaaaaaaaa', self.out_dir,
            no_photos=True, overwrite=True,
        )
        self.assertEqual(second['status'], 'ok')
        self.assertFalse(sentinel.exists())

    def test_dry_run_writes_nothing(self):
        self._seed_person()
        self._seed_source('s-1111111111', 'Source One', asset_rel='documents/other/file1.txt')
        self._commit_fresh()

        result = packet.run_packet(
            self.archive_root, 'p-aaaaaaaaaa', self.out_dir,
            no_photos=True, dry_run=True,
        )
        self.assertEqual(result['status'], 'dry-run')
        self.assertFalse(self.out_dir.exists())

    def test_cmd_packet_external_out_prints_absolute_path(self):
        self._seed_person()
        self._commit_fresh()
        external_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(external_tmp.cleanup)
        external = Path(external_tmp.name) / 'packet-out'
        args = Namespace(
            root=str(self.archive_root), spec_root=None, person_id='p-aaaaaaaaaa',
            out=str(external), include_research=False, include_restricted=False,
            include_dna=False, no_photos=True, dry_run=False, overwrite=False,
        )
        stdout = StringIO()
        stderr = StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = packet._cmd_packet(args)

        self.assertEqual(code, 0)
        self.assertIn(str(external), stdout.getvalue())

    def test_restricted_source_excluded_by_default(self):
        self._seed_person()
        self._seed_source('s-1111111111', 'Restricted Source', restricted=1)
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)
        readme = (result['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertIn('Excluded sources', readme)
        self.assertIn('S-1111111111', readme)
        self.assertNotIn('Included sources', readme)
        self.assertFalse((result['packet_dir'] / 'sources').exists())

    def test_restricted_source_included_with_flag(self):
        self._seed_person()
        self._seed_source('s-1111111111', 'Restricted Source', restricted=1)
        self._commit_fresh()

        result = packet.run_packet(
            self.archive_root, 'p-aaaaaaaaaa', self.out_dir,
            no_photos=True, include_restricted=True,
        )
        readme = (result['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertIn('Included sources', readme)
        self.assertTrue(any((result['packet_dir'] / 'sources').iterdir()))

    def test_dna_source_excluded_even_with_include_restricted(self):
        self._seed_person()
        self._seed_source('s-1111111111', 'DNA Source', restricted=1, source_type='dna')
        self._commit_fresh()

        result = packet.run_packet(
            self.archive_root, 'p-aaaaaaaaaa', self.out_dir,
            no_photos=True, include_restricted=True,
        )
        readme = (result['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertIn('Excluded sources', readme)
        self.assertIn('(DNA)', readme)

    def test_dna_source_included_with_include_dna(self):
        self._seed_person()
        self._seed_source('s-1111111111', 'DNA Source', restricted=1, source_type='dna')
        self._commit_fresh()

        result = packet.run_packet(
            self.archive_root, 'p-aaaaaaaaaa', self.out_dir,
            no_photos=True, include_dna=True,
        )
        readme = (result['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertIn('Included sources', readme)

    def test_living_other_person_named_in_caution(self):
        self._seed_person()
        self._seed_person(pid='p-bbbbbbbbbb', name='Living Person', living='true', surname='Other')
        self._seed_source('s-1111111111', 'Joint Source')
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'residence', 'lived together',
                      persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb'])
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)
        readme = (result['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertIn('CAUTION', readme)
        self.assertIn('Living Person', readme)

    def test_living_unknown_other_person_named_in_caution(self):
        self._seed_person()
        self._seed_person(pid='p-bbbbbbbbbb', name='Unknown Living Person', living='unknown', surname='Other')
        self._seed_source('s-1111111111', 'Joint Source')
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'residence', 'lived together',
                      persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb'])
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)
        readme = (result['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertIn('CAUTION', readme)
        self.assertIn('Unknown Living Person', readme)

    def test_excluded_source_claims_omitted_from_timeline(self):
        self._seed_person()
        self._seed_source('s-1111111111', 'Restricted Source', restricted=1)
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'birth', 'secret fact',
                      date_edtf='1900', persons=['p-aaaaaaaaaa'])
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)
        timeline_text = (result['packet_dir'] / 'timeline.md').read_text(encoding='utf-8')
        self.assertNotIn('secret fact', timeline_text)

    def test_no_photos_flag_skips_photoindex_requirement(self):
        self._seed_person()
        self._commit_fresh()
        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)
        self.assertEqual(result['status'], 'ok')
        self.assertFalse((result['packet_dir'] / 'photos').exists())

    def test_missing_photoindex_refuses_unless_no_photos(self):
        self._seed_person()
        self._commit_fresh()
        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir)
        self.assertEqual(result['status'], 'no-photoindex')

    def test_photo_group_expansion_pulls_in_back_variant(self):
        self._seed_person()
        self._commit_fresh()

        photos_dir = self.archive_root / 'photos'
        photos_dir.mkdir(parents=True, exist_ok=True)
        front = photos_dir / 'portrait.jpg'
        back = photos_dir / 'portrait-back.jpg'
        front.write_bytes(b'front')
        back.write_bytes(b'back')

        pconn = _make_photos_db(self.archive_root)
        pconn.execute("INSERT INTO photos(path, group_id) VALUES ('photos/portrait.jpg', 'g1')")
        pconn.execute("INSERT INTO photos(path, group_id) VALUES ('photos/portrait-back.jpg', 'g1')")
        pconn.execute(
            "INSERT INTO photo_people(path, person_ref, via) VALUES "
            "('photos/portrait.jpg', 'p-aaaaaaaaaa', 'pid-keyword')"
        )
        pconn.commit()
        pconn.close()
        future = time.time() + 5
        os.utime(self.archive_root / '.cache' / 'photos.sqlite', (future, future))

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir)
        self.assertEqual(result['status'], 'ok')
        photos_out = result['packet_dir'] / 'photos'
        self.assertTrue((photos_out / 'portrait.jpg').exists())
        self.assertTrue((photos_out / 'portrait-back.jpg').exists())

    def test_copy_failure_on_existing_asset_reported_not_raised(self):
        self._seed_person()
        self._seed_source('s-1111111111', 'Source One', asset_rel='documents/other/file1.txt')
        self._commit_fresh()

        real_copy2 = packet.shutil.copy2

        def flaky_copy2(src, dst):
            if Path(src).name == 'file1.txt':
                raise PermissionError('file1.txt is locked')
            return real_copy2(src, dst)

        with unittest.mock.patch.object(packet.shutil, 'copy2', side_effect=flaky_copy2):
            result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)

        self.assertEqual(result['status'], 'ok')
        self.assertTrue(any('could not copy' in m for m in result['messages']))
        self.assertFalse((result['packet_dir'] / 'files' / 'file1.txt').exists())

    def test_structural_write_failure_is_reported_not_raised(self):
        self._seed_person()
        self._commit_fresh()

        with unittest.mock.patch.object(packet, '_zip_directory', side_effect=OSError('disk full')):
            result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)

        self.assertEqual(result['status'], 'write-failed')
        self.assertTrue(any('disk full' in m for m in result['messages']))
        self.assertFalse(any(self.out_dir.glob('packet_*')))

    def test_missing_photo_reported_in_readme_and_messages(self):
        self._seed_person()
        self._commit_fresh()

        pconn = _make_photos_db(self.archive_root)
        pconn.execute(
            "INSERT INTO photo_people(path, person_ref, via) VALUES "
            "('photos/ghost.jpg', 'p-aaaaaaaaaa', 'pid-keyword')"
        )
        pconn.commit()
        pconn.close()
        future = time.time() + 5
        os.utime(self.archive_root / '.cache' / 'photos.sqlite', (future, future))

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir)
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(any('photo missing on disk' in m for m in result['messages']))
        readme = (result['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertIn('Missing files', readme)
        self.assertIn('ghost.jpg', readme)

    def test_missing_profile_file_is_structural_failure(self):
        profile_path = self._seed_person()
        profile_path.unlink()
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)

        self.assertEqual(result['status'], 'write-failed')
        self.assertTrue(any('profile' in m for m in result['messages']))
        self.assertFalse(any(self.out_dir.glob('packet_*')))

    def test_living_person_named_only_in_prose_gets_caution(self):
        profile_path = self._seed_person()
        self._seed_person(pid='p-bbbbbbbbbb', name='Prose Only Living', living='true', surname='Other')
        # No claim_persons/source_people row for p-bbbbbbbbbb - only a bare
        # [P-id] token in the copied profile prose.
        profile_path.write_text(
            '---\nid: p-aaaaaaaaaa\nname: Test Person\n---\n'
            '# Test Person\n\nRaised alongside [P-bbbbbbbbbb].\n',
            encoding='utf-8',
        )
        self.conn.execute(
            "INSERT INTO citations(token, kind, path, line) VALUES "
            "('p-bbbbbbbbbb', 'P', ?, 4)",
            (profile_path.relative_to(self.archive_root).as_posix(),),
        )
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)
        readme = (result['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertIn('CAUTION', readme)
        self.assertIn('Prose Only Living', readme)

    def test_out_dir_inside_record_tree_refused(self):
        self._seed_person()
        self._commit_fresh()

        for subdir in ('sources', 'people', 'notes'):
            result = packet.run_packet(
                self.archive_root, 'p-aaaaaaaaaa', self.archive_root / subdir / 'packets',
                no_photos=True,
            )
            self.assertEqual(result['status'], 'bad-output-path')

    def test_include_research_warns_when_no_research_file_exists(self):
        self._seed_person()
        self._commit_fresh()

        result = packet.run_packet(
            self.archive_root, 'p-aaaaaaaaaa', self.out_dir,
            no_photos=True, include_research=True,
        )
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(any('--include-research' in m for m in result['messages']))
        readme = (result['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertNotIn('research notes', readme)

    def test_dry_run_with_overwrite_does_not_delete_existing_output(self):
        self._seed_person()
        self._commit_fresh()
        first = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)
        sentinel = first['packet_dir'] / 'sentinel.txt'
        sentinel.write_text('keep', encoding='utf-8')

        second = packet.run_packet(
            self.archive_root, 'p-aaaaaaaaaa', self.out_dir,
            no_photos=True, dry_run=True, overwrite=True,
        )
        self.assertEqual(second['status'], 'dry-run')
        self.assertTrue(sentinel.exists())

    def test_out_dir_inside_arbitrary_record_subdir_refused(self):
        # Broadened rule: anything inside the archive whose top-level
        # component isn't literally 'out' is refused, not just the three
        # named record trees - e.g. a custom internal scratch dir.
        self._seed_person()
        self._commit_fresh()

        result = packet.run_packet(
            self.archive_root, 'p-aaaaaaaaaa', self.archive_root / 'scratch' / 'packets',
            no_photos=True,
        )
        self.assertEqual(result['status'], 'bad-output-path')

    def test_out_dir_nested_under_out_is_allowed(self):
        self._seed_person()
        self._commit_fresh()

        result = packet.run_packet(
            self.archive_root, 'p-aaaaaaaaaa', self.archive_root / 'out' / 'nested',
            no_photos=True,
        )
        self.assertEqual(result['status'], 'ok')

    def test_source_image_expands_to_photo_group_siblings(self):
        self._seed_person()
        self._seed_source(
            's-1111111111', 'Source One', asset_rel='photos/scan-front.jpg',
        )
        self._commit_fresh()

        back = self.archive_root / 'photos' / 'scan-back.jpg'
        back.write_bytes(b'back')

        pconn = _make_photos_db(self.archive_root)
        pconn.execute("INSERT INTO photos(path, group_id) VALUES ('photos/scan-front.jpg', 'g1')")
        pconn.execute("INSERT INTO photos(path, group_id) VALUES ('photos/scan-back.jpg', 'g1')")
        pconn.commit()
        pconn.close()
        future = time.time() + 5
        os.utime(self.archive_root / '.cache' / 'photos.sqlite', (future, future))

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir)
        self.assertEqual(result['status'], 'ok')
        photos_out = result['packet_dir'] / 'photos'
        self.assertTrue((photos_out / 'scan-front.jpg').exists())
        self.assertTrue((photos_out / 'scan-back.jpg').exists())

    def test_photo_only_living_person_gets_caution(self):
        self._seed_person()
        self._seed_person(pid='p-bbbbbbbbbb', name='Photo Only Living', living='true', surname='Other')
        self._commit_fresh()

        photos_dir = self.archive_root / 'photos'
        photos_dir.mkdir(parents=True, exist_ok=True)
        photo = photos_dir / 'group.jpg'
        photo.write_bytes(b'group')

        pconn = _make_photos_db(self.archive_root)
        pconn.execute("INSERT INTO photos(path, group_id) VALUES ('photos/group.jpg', 'g1')")
        pconn.execute(
            "INSERT INTO photo_people(path, person_ref, via) VALUES "
            "('photos/group.jpg', 'p-aaaaaaaaaa', 'pid-keyword')"
        )
        pconn.execute(
            "INSERT INTO photo_people(path, person_ref, via) VALUES "
            "('photos/group.jpg', 'p-bbbbbbbbbb', 'face-tag')"
        )
        pconn.commit()
        pconn.close()
        future = time.time() + 5
        os.utime(self.archive_root / '.cache' / 'photos.sqlite', (future, future))

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir)
        self.assertEqual(result['status'], 'ok')
        readme = (result['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertIn('CAUTION', readme)
        self.assertIn('Photo Only Living', readme)

    def test_merged_alias_sources_still_gathered(self):
        # p-aaaaaaaaaa is merged into the survivor p-bbbbbbbbbb; a source
        # citing the old, merged-away id must still appear in the
        # survivor's packet (SPEC §8.8).
        self._seed_person(pid='p-bbbbbbbbbb', name='Survivor Person', surname='Survivor')
        self._seed_person(
            pid='p-aaaaaaaaaa', name='Old Identity', surname='Old',
            status='merged', merged_into='p-bbbbbbbbbb',
        )
        self._seed_source('s-1111111111', 'Old Alias Source', persons=('p-aaaaaaaaaa',))
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-bbbbbbbbbb', self.out_dir, no_photos=True)
        self.assertEqual(result['status'], 'ok')
        sources_out = result['packet_dir'] / 'sources'
        self.assertTrue((sources_out / 's-1111111111.md').exists())

    def test_stale_photoindex_refuses_unless_no_photos(self):
        self._seed_person()
        self._commit_fresh()
        pconn = _make_photos_db(self.archive_root)
        pconn.commit()
        pconn.close()
        photos_dir = self.archive_root / 'photos'
        photos_dir.mkdir()
        photo = photos_dir / 'newer.jpg'
        photo.write_bytes(b'new')
        future = time.time() + 10
        os.utime(photo, (future, future))

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir)
        self.assertEqual(result['status'], 'no-photoindex')
        self.assertFalse(self.out_dir.exists())

    # ── AI-draft prose in the profile copy (round-2 S1) ───────────────────────

    def _seed_research(self, text):
        research_path = self.archive_root / 'people' / 'research_p-aaaaaaaaaa.md'
        research_path.parent.mkdir(parents=True, exist_ok=True)
        research_path.write_text(text, encoding='utf-8')
        self.conn.execute(
            "INSERT INTO person_files(person_id, kind, path, generated) VALUES "
            "('p-aaaaaaaaaa', 'research', ?, 0)",
            (research_path.relative_to(self.archive_root).as_posix(),),
        )
        return research_path

    def test_unaccepted_draft_prose_withheld_from_profile_copy(self):
        # The AI-pass contract is unqualified: prose still inside
        # <!-- AI-DRAFT --> markers never ships on any export path, and no
        # packet flag opens it (acceptance is `fha confirm draft`, a human
        # gate, not an export switch). Accepted prose ships with its
        # provenance marker removed.
        profile_path = self._seed_person()
        profile_path.write_text(
            '---\nid: p-aaaaaaaaaa\nname: Test Person\n---\n'
            '# Test Person\n\n## Biography\n\n'
            'Accepted paragraph about the farm.\n<!-- AI-ACCEPTED 2026-05-01 -->\n\n'
            'Unreviewed draft paragraph.\n<!-- AI-DRAFT 2026-06-30 claims: [] -->\n\n'
            '## Notes\n\nHuman-written note.\n',
            encoding='utf-8',
        )
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)
        self.assertEqual(result['status'], 'ok')
        copied = next((result['packet_dir'] / 'profile').glob('*.md')).read_text(encoding='utf-8')
        self.assertNotIn('Unreviewed draft paragraph', copied)
        self.assertNotIn('AI-DRAFT', copied)
        self.assertIn('Accepted paragraph about the farm.', copied)
        self.assertNotIn('AI-ACCEPTED', copied)
        self.assertIn('Human-written note.', copied)
        readme = (result['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertIn(
            '1 draft paragraph awaiting your review was left out of '
            f'{profile_path.name}; it stays in your archive.', readme)

    def test_accepted_marker_removed_without_readme_note(self):
        # An AI-ACCEPTED marker is provenance, not withheld content: the
        # prose ships, the comment goes, and the README counts nothing.
        profile_path = self._seed_person()
        profile_path.write_text(
            '---\nid: p-aaaaaaaaaa\nname: Test Person\n---\n'
            '# Test Person\n\n## Biography\n\n'
            'Accepted paragraph.\n<!-- AI-ACCEPTED 2026-05-01 -->\n',
            encoding='utf-8',
        )
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)
        self.assertEqual(result['status'], 'ok')
        copied = next((result['packet_dir'] / 'profile').glob('*.md')).read_text(encoding='utf-8')
        self.assertIn('Accepted paragraph.', copied)
        self.assertNotIn('AI-ACCEPTED', copied)
        readme = (result['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertNotIn('Left out for privacy', readme)

    def test_damaged_draft_marker_fails_packet_build(self):
        # A marker missing its "-->" means draft can no longer be told from
        # accepted prose. The profile is the packet's required centerpiece,
        # so the build fails structurally (write-failed), the same posture as
        # a private name that could not be separated out - never a verbatim
        # profile copy.
        profile_path = self._seed_person()
        profile_path.write_text(
            '---\nid: p-aaaaaaaaaa\nname: Test Person\n---\n'
            '# Test Person\n\n## Biography\n\nDraft text.\n<!-- AI-DRAFT 2026-06-30\n',
            encoding='utf-8',
        )
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)
        self.assertEqual(result['status'], 'write-failed')
        self.assertTrue(any('draft marker' in m and profile_path.name in m
                            for m in result['messages']))
        self.assertTrue(any('-->' in m for m in result['messages']))
        self.assertFalse(any(self.out_dir.glob('packet_*')))

    def test_research_copy_with_draft_marker_gets_readme_caution(self):
        # Research files ship as byte copies (documented round-2 scope
        # decision: working notes, not publication prose) - the draft text
        # travels with them, so the README must say so in one plain line.
        self._seed_person()
        self._seed_research(
            '# Research\n\nA half-drafted lead.\n<!-- AI-DRAFT 2026-06-30 -->\n')
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir,
                                   no_photos=True, include_research=True)
        self.assertEqual(result['status'], 'ok')
        copied = (result['packet_dir'] / 'profile' / 'research_p-aaaaaaaaaa.md').read_text(
            encoding='utf-8')
        self.assertIn('AI-DRAFT', copied)   # byte copy, by scope decision
        readme = (result['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertIn('unreviewed draft text', readme)

    def test_research_copy_without_draft_marker_no_caution(self):
        self._seed_person()
        self._seed_research('# Research\n\nClean notes.\n')
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir,
                                   no_photos=True, include_research=True)
        self.assertEqual(result['status'], 'ok')
        readme = (result['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertNotIn('unreviewed draft text', readme)


if __name__ == '__main__':
    unittest.main()
