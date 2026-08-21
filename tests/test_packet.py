"""
test_packet.py - fha packet: the gather/copy/redact/zip contract.

Fixtures only (AGENTS_TOOLING §5): every test builds a synthetic
`.cache/index.sqlite` (and `.cache/photos.sqlite` where photos are involved)
from index.py's and photoindex.py's own DDL inside a temp tree.

One thread runs through the photo tests and is worth stating once: a packet
ships **logical photos**, not scans. Group expansion puts the back and the crop
of a matched front in the bundle, so every README caution about a photo is a
statement about the group and is counted over the files actually copied - a
name-match whose matched variant has gone off disk still warns while a sibling
travels, and a group that put nothing in the bundle warns about nothing
(PR #42 round 2).
"""

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
from _lib import (
    index_manifest_path, path_to_alias, photoindex_manifest_path,
    photoindex_record_manifest, record_path_manifest, write_path_manifest,
)


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
                  place_text=None, status='accepted', persons=(),
                  confidence=None, reviewed=None):
    conn.execute(
        '''INSERT INTO claims(id, source_id, type, date_edtf, place_text, value, status,
                              confidence, reviewed)
           VALUES (?,?,?,?,?,?,?,?,?)''',
        (cid, source_id, ctype, date_edtf, place_text, value, status, confidence, reviewed),
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
        # #48: this synthetic index.sqlite is hand-built via raw DDL/INSERTs,
        # bypassing build_index/upsert_source - the only two places that
        # write the #48 path manifest - so without this, open_index_db's
        # additive manifest check finds no manifest at all and (correctly,
        # per the bootstrapping rule) reads every real file _seed_person/
        # _seed_source already wrote as newly "added", i.e. stale. Called
        # here, after every seed call in a test has run (every test that
        # wants a fresh index calls this last), so the manifest always
        # matches whatever real files that test actually created.
        write_path_manifest(
            index_manifest_path(self.archive_root), record_path_manifest(self.archive_root))

    def _commit_photos_fresh(self, pconn) -> None:
        """The photos.sqlite counterpart to `_commit_fresh` - same #48 reason:
        a synthetic photos.sqlite, hand-built and committed after
        `_seed_person`/`_seed_source` (and sometimes a real photo file
        written straight to `photos/` before this call) already put real
        files on disk, needs a matching `.cache/photos_manifest.json` or
        `photoindex_status`'s additive manifest check reads those real files
        as newly "added" (no manifest = bootstrap-stale) and reports
        `no-photoindex` regardless of what the synthetic photo rows say.
        Covers both halves `photoindex_status` checks: the photos-root walk
        (only the real files a test actually wrote under `photos/`, if any -
        the synthetic rows above reference paths that mostly do not exist on
        disk and must not be manifested as if they did) and
        `photoindex_record_manifest` (people/sources-photos).
        """
        pconn.commit()
        photos_dir = self.archive_root / 'photos'
        manifest = {
            path_to_alias(p, 'photos', {}, self.archive_root): p.stat().st_mtime
            for p in (photos_dir.rglob('*') if photos_dir.is_dir() else []) if p.is_file()
        }
        manifest.update(photoindex_record_manifest(self.archive_root))
        write_path_manifest(photoindex_manifest_path(self.archive_root), manifest)

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

    # ── issue #78: a generational suffix must not reach the packet filename ──
    # as the surname. `_seed_person(surname='')` simulates a person with no
    # indexed surname - the `person['surname'] or person_name.split()[-1]`
    # fallback is exactly where the bug lived (the indexed-surname branch
    # short-circuits before the fallback runs, so it was never broken - the
    # "with an indexed surname" cases below are regression coverage, not
    # guard cases).

    _SUFFIXES = ['Jr', 'Sr', 'II', 'III', 'IV', 'V']

    def _packet_zip_name(self, pid):
        result = packet.run_packet(self.archive_root, pid, self.out_dir, no_photos=True)
        self.assertEqual(result['status'], 'ok')
        return result['zip_path'].name

    def test_suffix_without_indexed_surname_is_not_taken_as_surname(self):
        # GUARD (issue #78 case 2): no indexed surname, the last-token
        # fallback used to name the deliverable `packet_jr_...zip`. A
        # distinct pid per suffix (same archive) keeps each packet's
        # output path from colliding with the last.
        for i, suffix in enumerate(self._SUFFIXES):
            with self.subTest(suffix=suffix):
                pid = f'p-suffix{i:03d}aa'
                self._seed_person(pid=pid, name=f'Roy Eugene Dodson {suffix}', surname='')
                self._commit_fresh()
                zip_name = self._packet_zip_name(pid)
                self.assertTrue(zip_name.startswith('packet_dodson_'), zip_name)

    def test_suffix_with_indexed_surname_unaffected(self):
        # Indexed surname present: `person['surname'] or ...` short-circuits
        # before the fallback, so this was never broken - regression
        # coverage per the issue's own suggested test list.
        for i, suffix in enumerate(self._SUFFIXES):
            with self.subTest(suffix=suffix):
                pid = f'p-suffix{i:03d}bb'
                self._seed_person(pid=pid, name=f'Roy Eugene Dodson {suffix}', surname='Dodson')
                self._commit_fresh()
                zip_name = self._packet_zip_name(pid)
                self.assertTrue(zip_name.startswith('packet_dodson_'), zip_name)

    def test_mononym_unchanged(self):
        self._seed_person(name='Cher', surname='')
        self._commit_fresh()
        zip_name = self._packet_zip_name('p-aaaaaaaaaa')
        self.assertTrue(zip_name.startswith('packet_cher_'), zip_name)

    def test_surname_genuinely_at_the_end_unchanged(self):
        self._seed_person(name='Roy Eugene Dodson', surname='')
        self._commit_fresh()
        zip_name = self._packet_zip_name('p-aaaaaaaaaa')
        self.assertTrue(zip_name.startswith('packet_dodson_'), zip_name)

    def test_a_person_with_no_name_left_to_split_still_gets_a_packet(self):
        # GUARD: the suffix-stripping fallback has to be able to REACH the
        # `or 'person'` default below it. A record whose `name:` is
        # whitespace, filed under a stem with no `{surname}__` slot for the
        # indexer to read, leaves both the indexed surname and the name
        # tokens empty - and the fallback raised IndexError on the way
        # past, so the packet came out as "something went wrong" instead
        # of a deliverable named for nobody in particular.
        self._seed_person(name='   ', surname='')
        self._commit_fresh()
        zip_name = self._packet_zip_name('p-aaaaaaaaaa')
        self.assertTrue(zip_name.startswith('packet_person_'), zip_name)

    def test_a_placeholder_surname_never_names_the_deliverable(self):
        # GUARD: `unknown__unknown_P-….md` indexes with the title-cased
        # surname "Unknown", and that slug outlives the placeholder - a
        # human types a real name into the record and `fha lint --fix-ids`
        # has not renamed the file yet. Roy Dodson's packet came out as
        # `packet_unknown_….zip`; a packet filename is a naming surface,
        # so it asks `_lib.is_placeholder_name` like every other one.
        self._seed_person(name='Roy Dodson', surname='Unknown')
        self._commit_fresh()
        zip_name = self._packet_zip_name('p-aaaaaaaaaa')
        self.assertTrue(zip_name.startswith('packet_dodson_'), zip_name)

    def test_a_placeholder_on_both_sides_falls_through_to_the_default(self):
        # ... and with nothing but placeholders, the `or 'person'` default
        # is still what answers - never `packet_none_….zip`.
        self._seed_person(name='None', surname='unknown')
        self._commit_fresh()
        zip_name = self._packet_zip_name('p-aaaaaaaaaa')
        self.assertTrue(zip_name.startswith('packet_person_'), zip_name)

    def test_timeline_tags_parked_and_low_confidence_claims(self):
        # Owner decision 2026-07-22: a packet is family research material, so
        # needs-review claims stay in its timeline - tagged, same words as
        # fha views timeline - and an accepted-low claim carries its flag.
        self._seed_person()
        self._seed_source('s-1111111111', 'Source One', asset_rel='documents/other/file1.txt')
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'birth', 'born 1900',
                      date_edtf='1900', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-1111111111', 'residence', 'maybe Topeka',
                      date_edtf='1905', status='needs-review', reviewed='2026-03-01',
                      persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-cccccccccc', 's-1111111111', 'occupation', 'maybe a miller',
                      date_edtf='1910', confidence='low', persons=['p-aaaaaaaaaa'])
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)
        self.assertEqual(result['status'], 'ok')
        text = (result['packet_dir'] / 'timeline.md').read_text(encoding='utf-8')
        self.assertIn('maybe Topeka', text)
        self.assertIn('[unconfirmed - parked 2026-03-01]', text)
        self.assertIn('maybe a miller', text)
        self.assertIn('[low confidence]', text)
        for line in text.splitlines():
            if 'born 1900' in line:
                self.assertNotIn('[unconfirmed', line)
                self.assertNotIn('[low confidence]', line)

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
        self._commit_photos_fresh(pconn)
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
        self._commit_photos_fresh(pconn)
        pconn.close()
        future = time.time() + 5
        os.utime(self.archive_root / '.cache' / 'photos.sqlite', (future, future))

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir)
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(any('photo missing on disk' in m for m in result['messages']))
        readme = (result['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertIn('Missing files', readme)
        self.assertIn('ghost.jpg', readme)

    def test_reconcile_missing_photo_is_skipped_and_explained(self):
        # `fha photoindex reconcile` keeps a vanished photo as the synthetic
        # key 'MISSING:photos/…' so its caption survives. A packet copies real
        # bytes, so the live back scan ships, the vanished front does not, and
        # the README says so under its real path (no MISSING: jargon, and no
        # 'photos/1' style half-key).
        self._seed_person()
        self._commit_fresh()

        photos_dir = self.archive_root / 'photos'
        photos_dir.mkdir(parents=True, exist_ok=True)
        (photos_dir / 'portrait-back.jpg').write_bytes(b'back')

        pconn = _make_photos_db(self.archive_root)
        pconn.execute(
            "INSERT INTO photos(path, group_id) VALUES ('MISSING:photos/portrait.jpg', 'g1')"
        )
        pconn.execute("INSERT INTO photos(path, group_id) VALUES ('photos/portrait-back.jpg', 'g1')")
        pconn.execute(
            "INSERT INTO photo_people(path, person_ref, via) VALUES "
            "('MISSING:photos/portrait.jpg', 'p-aaaaaaaaaa', 'pid-keyword')"
        )
        self._commit_photos_fresh(pconn)
        pconn.close()
        future = time.time() + 5
        os.utime(self.archive_root / '.cache' / 'photos.sqlite', (future, future))

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir)
        self.assertEqual(result['status'], 'ok')

        photos_out = result['packet_dir'] / 'photos'
        self.assertTrue((photos_out / 'portrait-back.jpg').exists())
        self.assertFalse(any(p.name.startswith('MISSING') for p in photos_out.iterdir()))

        readme = (result['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertIn('photo not on disk, so not copied: photos/portrait.jpg', readme)
        self.assertNotIn('MISSING:', readme)
        self.assertTrue(any(
            'fha photoindex reconcile' in m and 'photos/portrait.jpg' in m
            for m in result['messages']
        ))

    def test_missing_photo_row_not_counted_as_unverified(self):
        # The README's "matched by name only" count describes files the
        # recipient can actually open in photos/, so a name-matched photo whose
        # whole group is off disk - nothing of it copied - must not inflate it.
        self._seed_person()
        self._commit_fresh()

        pconn = _make_photos_db(self.archive_root)
        pconn.execute(
            "INSERT INTO photos(path, group_id) VALUES ('MISSING:photos/guess.jpg', 'g1')"
        )
        pconn.execute(
            "INSERT INTO photo_people(path, person_ref, via) VALUES "
            "('MISSING:photos/guess.jpg', 'p-aaaaaaaaaa', 'name-match')"
        )
        self._commit_photos_fresh(pconn)
        pconn.close()
        future = time.time() + 5
        os.utime(self.archive_root / '.cache' / 'photos.sqlite', (future, future))

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir)
        self.assertEqual(result['status'], 'ok')
        readme = (result['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertNotIn('matched by name only', readme)
        self.assertIn('photo not on disk, so not copied: photos/guess.jpg', readme)

    def test_name_match_caution_survives_when_only_matched_variant_is_missing(self):
        # The name match is a fact about the physical photo, not about one scan
        # of it: the vanished front is what carried the unverified match, but
        # the live back of the same group is what ships - so the recipient must
        # still be told the photo in photos/ was picked by name alone.
        self._seed_person()
        self._commit_fresh()

        photos_dir = self.archive_root / 'photos'
        photos_dir.mkdir(parents=True, exist_ok=True)
        (photos_dir / 'guess-back.jpg').write_bytes(b'back')

        pconn = _make_photos_db(self.archive_root)
        pconn.execute(
            "INSERT INTO photos(path, group_id) VALUES ('MISSING:photos/guess.jpg', 'g1')"
        )
        pconn.execute("INSERT INTO photos(path, group_id) VALUES ('photos/guess-back.jpg', 'g1')")
        pconn.execute(
            "INSERT INTO photo_people(path, person_ref, via) VALUES "
            "('MISSING:photos/guess.jpg', 'p-aaaaaaaaaa', 'name-match')"
        )
        self._commit_photos_fresh(pconn)
        pconn.close()
        future = time.time() + 5
        os.utime(self.archive_root / '.cache' / 'photos.sqlite', (future, future))

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir)
        self.assertEqual(result['status'], 'ok')
        self.assertTrue((result['packet_dir'] / 'photos' / 'guess-back.jpg').exists())
        readme = (result['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertIn('1 photo(s) in photos/ are matched by name only', readme)

    def test_verified_tag_anywhere_in_group_lifts_the_name_match_caution(self):
        # A group the person is tagged into by P-id keyword is verified as a
        # whole, so a weaker name-match row left on a sibling variant must not
        # make the packet call the copied photos unverified.
        self._seed_person()
        self._commit_fresh()

        photos_dir = self.archive_root / 'photos'
        photos_dir.mkdir(parents=True, exist_ok=True)
        (photos_dir / 'sure-front.jpg').write_bytes(b'front')
        (photos_dir / 'sure-back.jpg').write_bytes(b'back')

        pconn = _make_photos_db(self.archive_root)
        pconn.execute("INSERT INTO photos(path, group_id) VALUES ('photos/sure-front.jpg', 'g1')")
        pconn.execute("INSERT INTO photos(path, group_id) VALUES ('photos/sure-back.jpg', 'g1')")
        pconn.execute(
            "INSERT INTO photo_people(path, person_ref, via) VALUES "
            "('photos/sure-front.jpg', 'p-aaaaaaaaaa', 'pid-keyword'), "
            "('photos/sure-back.jpg', 'p-aaaaaaaaaa', 'name-match')"
        )
        self._commit_photos_fresh(pconn)
        pconn.close()
        future = time.time() + 5
        os.utime(self.archive_root / '.cache' / 'photos.sqlite', (future, future))

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir)
        self.assertEqual(result['status'], 'ok')
        readme = (result['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertNotIn('matched by name only', readme)

    def test_living_person_tagged_only_on_missing_variant_still_cautioned(self):
        # The vanished front scan keeps its tags through reconcile, and the
        # live back scan of the same physical photo does ship - so the caution
        # about a living person must not disappear with the file.
        self._seed_person()
        self._seed_person(pid='p-bbbbbbbbbb', name='Living Cousin', living='unknown',
                          surname='Cousin')
        self._commit_fresh()

        photos_dir = self.archive_root / 'photos'
        photos_dir.mkdir(parents=True, exist_ok=True)
        (photos_dir / 'group-back.jpg').write_bytes(b'back')

        pconn = _make_photos_db(self.archive_root)
        pconn.execute(
            "INSERT INTO photos(path, group_id) VALUES ('MISSING:photos/group.jpg', 'g1')"
        )
        pconn.execute("INSERT INTO photos(path, group_id) VALUES ('photos/group-back.jpg', 'g1')")
        pconn.execute(
            "INSERT INTO photo_people(path, person_ref, via) VALUES "
            "('photos/group-back.jpg', 'p-aaaaaaaaaa', 'pid-keyword'), "
            "('MISSING:photos/group.jpg', 'p-bbbbbbbbbb', 'pid-keyword')"
        )
        self._commit_photos_fresh(pconn)
        pconn.close()
        future = time.time() + 5
        os.utime(self.archive_root / '.cache' / 'photos.sqlite', (future, future))

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir)
        readme = (result['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertIn('CAUTION', readme)
        self.assertIn('Living Cousin', readme)

    def test_living_person_from_a_wholly_missing_group_is_not_cautioned(self):
        # The mirror of the test above: when every variant of that photo is off
        # disk, nothing of it reaches the bundle - so naming the living person
        # in the README would put a name in the packet that the packet does not
        # otherwise contain.
        self._seed_person()
        self._seed_person(pid='p-bbbbbbbbbb', name='Living Cousin', living='unknown',
                          surname='Cousin')
        self._commit_fresh()

        pconn = _make_photos_db(self.archive_root)
        pconn.execute(
            "INSERT INTO photos(path, group_id) VALUES ('MISSING:photos/gone.jpg', 'g1')"
        )
        pconn.execute(
            "INSERT INTO photo_people(path, person_ref, via) VALUES "
            "('MISSING:photos/gone.jpg', 'p-aaaaaaaaaa', 'pid-keyword'), "
            "('MISSING:photos/gone.jpg', 'p-bbbbbbbbbb', 'pid-keyword')"
        )
        self._commit_photos_fresh(pconn)
        pconn.close()
        future = time.time() + 5
        os.utime(self.archive_root / '.cache' / 'photos.sqlite', (future, future))

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir)
        self.assertEqual(result['status'], 'ok')
        readme = (result['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertNotIn('Living Cousin', readme)
        self.assertIn('photo not on disk, so not copied: photos/gone.jpg', readme)

    def test_missing_profile_file_is_refused_on_the_privacy_arm(self):
        """A profile that is not on disk is refused as unreadable, not as a
        missing file.

        This case used to land on `write-failed`, several hundred lines later,
        because the profile copy checks `exists()`. That was an accident of
        ordering, not a safeguard: the subject's `restricted:` marker had
        already been read as "no marker" and the export had already been
        allowed. The refusal now happens at the privacy gate that owns the
        question, before any output directory is created, so it holds however
        the rest of the build is reordered.
        """
        profile_path = self._seed_person()
        profile_path.unlink()
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)

        self.assertEqual(result['status'], 'restricted-subject')
        self.assertTrue(any('left out of exports' in m for m in result['messages']))
        self.assertFalse(self.out_dir.exists())

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
        self._commit_photos_fresh(pconn)
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
        self._commit_photos_fresh(pconn)
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
        self._commit_photos_fresh(pconn)
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

    def test_purpose_block_and_sources_region_stripped_from_profile_copy(self):
        # #75/#76: the visible purpose block, and the `## Sources`
        # GENERATED-BEGIN/END region, are scaffolding for the working
        # archive - instructions for whoever edits the record in place, not
        # content for a relative reading a static export. Neither belongs
        # in a shipped copy, and the Sources region is not just stripped of
        # its markers but dropped whole (it names sources by archive-
        # relative path with none of the packet's own privacy filtering).
        profile_path = self._seed_person()
        profile_path.write_text(
            '---\nid: p-aaaaaaaaaa\nname: Test Person\n---\n'
            '# Test Person\n\n'
            "> **This person's record - yours to write.** The main page for "
            "this person: summary,\n"
            '> biography, relationships.\n\n'
            '## Sources\n'
            '<!-- GENERATED-BEGIN sources-index by sources-index on 2026-08-01 -->\n\n'
            '*(Generated by `sources-index` - do not edit; regenerate instead.)*\n\n'
            '**Census:** [[S-1111111111]]\n\n'
            '<!-- GENERATED-END sources-index -->\n\n'
            '## Biography\n\nAccepted paragraph about the farm.\n',
            encoding='utf-8',
        )
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)
        self.assertEqual(result['status'], 'ok')
        copied = next((result['packet_dir'] / 'profile').glob('*.md')).read_text(encoding='utf-8')
        self.assertNotIn('yours to write', copied)
        self.assertNotIn('GENERATED-BEGIN', copied)
        self.assertNotIn('GENERATED-END', copied)
        self.assertNotIn('S-1111111111', copied)
        self.assertNotIn('## Sources', copied)
        self.assertIn('# Test Person', copied)
        self.assertIn('## Biography', copied)
        self.assertIn('Accepted paragraph about the farm.', copied)

    def test_pre_75_profile_with_no_purpose_block_ships_unchanged(self):
        # No backfill/migration tooling (#75/#76 done-when): an
        # older-shaped record that never had a purpose block or a Sources
        # region to begin with must ship exactly as it always did - the
        # stripper is a no-op on text that carries nothing to strip.
        profile_path = self._seed_person()
        original = (
            '---\nid: p-aaaaaaaaaa\nname: Test Person\n---\n'
            '# Test Person\n\n## Biography\n\nAn old-shape record.\n'
        )
        profile_path.write_text(original, encoding='utf-8')
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)
        self.assertEqual(result['status'], 'ok')
        copied = next((result['packet_dir'] / 'profile').glob('*.md')).read_text(encoding='utf-8')
        self.assertEqual(copied, original)

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
        # ...and the always-on privacy caution names the unredacted risk too.
        self.assertIn('not redacted', readme)

    def test_research_copy_always_warns_it_is_unredacted(self):
        # Research ships byte-for-byte (not run through the restricted/deadname
        # redaction the profile and sources get), so ANY --include-research must
        # warn the recipient - even with no AI-DRAFT marker - that the notes may
        # name living/restricted people. The draft-specific line only appears
        # when a marker is actually present.
        self._seed_person()
        self._seed_research('# Research\n\nClean notes naming a cousin.\n')
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir,
                                   no_photos=True, include_research=True)
        self.assertEqual(result['status'], 'ok')
        readme = (result['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertIn('not redacted', readme)             # the privacy caution
        self.assertIn('living or restricted', readme)
        self.assertNotIn('unreviewed draft text', readme)  # no draft marker present

    def test_research_copy_strips_the_purpose_block(self):
        # #75/§16a: a research file carries its own visible purpose block
        # (_lib.RESEARCH_PURPOSE_BLOCK) exactly like a profile or a source
        # record - scaffolding for the working archive, not content for the
        # family - so --include-research must not ship it verbatim, the same
        # way the profile and source copies already strip theirs. Everything
        # else about the research copy (byte-for-byte, unredacted) is
        # unchanged - only the purpose block goes.
        self._seed_person()
        self._seed_research(
            '# Research\n\n'
            "> **This person's research workspace - yours to write.** Open "
            'questions,\n> hunches, and searches performed live here.\n\n'
            '## Research Notes\nClean notes naming a cousin.\n')
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir,
                                   no_photos=True, include_research=True)
        self.assertEqual(result['status'], 'ok')
        copied = (result['packet_dir'] / 'profile' / 'research_p-aaaaaaaaaa.md').read_text(
            encoding='utf-8')
        self.assertNotIn('research workspace - yours to write', copied)
        self.assertIn('Clean notes naming a cousin.', copied)   # the real content survives

    def test_a_packet_folder_it_cannot_read_fails_instead_of_shipping_short(self):
        """A packet zip that could not read part of itself is not handed over.

        The bundle goes to a relative who cannot check it against the archive,
        so a zip silently missing the source a claim rests on is a false
        success that travels. The walk fails closed onto the existing
        write-failed arm, which also clears the half-built folder.
        """
        self._seed_person()
        self._seed_source('s-1111111111', 'A letter',
                          asset_rel='documents/letters/letter.pdf')
        self._commit_fresh()
        with unittest.mock.patch('os.scandir', new=_scandir_denying('/files')):
            result = packet.run_packet(
                self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)
        self.assertEqual(result['status'], 'write-failed')
        text = '\n'.join(result['messages'])
        self.assertIn('could not be read', text)
        self.assertIn('fha packet', text)
        # Nothing half-built is left behind to block or mislead a retry.
        self.assertFalse(any(self.out_dir.glob('*.zip')))
        self.assertFalse(any(q.is_dir() for q in self.out_dir.glob('*')))

    # ── A privacy marker that could not be read is not a missing marker ────────
    #
    # `restricted:` lives in the record file and nowhere else for a person, so
    # the export decision is made by READING a file. Every one of these tests
    # is the same shape: the marker cannot be read, and the question is which
    # way the gate falls. The pre-fix answer was "include" in all of them, and
    # the packet is the person's entire material.
    #
    # `_lib.read_record` is why the shape is easy to miss: it does not raise
    # for the ordinary failures. A gone file, a permission error and malformed
    # YAML all come back as an E010 entry in `parse_errors` with `meta` empty,
    # so an `except` arm alone guards almost nothing - and a record with no
    # frontmatter block at all comes back with `meta` empty and NO parse error,
    # which is the shape that shipped a written packet.

    def _profile_text(self, text: str, pid='p-aaaaaaaaaa'):
        """Seed the standard curated subject, then replace their profile text.

        The index row keeps `tier: curated` / `living: false` while the file on
        disk no longer says either. That divergence is the whole scenario: it
        is what a restore-from-backup (unzip preserves the old mtime, so the
        index still looks fresh), a truncated write, or a half-finished
        hand-edit leaves behind. `_commit_fresh` after the rewrite reproduces
        the fresh-index half.
        """
        profile_path = self._seed_person(pid=pid)
        profile_path.write_text(text, encoding='utf-8')
        self._commit_fresh()
        return profile_path

    def test_subject_with_no_frontmatter_is_not_exported(self):
        """The reachable shape: frontmatter gone, index still says curated.

        Nothing errors. `FRONT_RE` does not match, so `read_record` reports no
        parse error and hands back an empty `meta`; `_redact_profile_text`
        returns `(text, 0)` for the same reason and the profile is byte-copied.
        Pre-fix this wrote a complete packet - profile, timeline, sources, zip -
        for a person whose file may have said `restricted: by-request` an hour
        ago. There is nowhere left in the file for the marker to live, so the
        only honest reading is "unknown", and unknown is withheld.
        """
        self._profile_text('# Test Person\n\nBorn in Kansas.\n')

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)

        self.assertEqual(result['status'], 'restricted-subject')
        self.assertFalse(self.out_dir.exists())
        text = '\n'.join(result['messages'])
        self.assertIn('left out of exports', text)
        self.assertIn('fha lint', text)

    def test_subject_whose_frontmatter_will_not_parse_is_not_exported(self):
        """The `parse_errors` route: malformed frontmatter YAML.

        `read_record` returns E010 and an empty `meta` rather than raising, so
        the pre-fix `except Exception` arm never fired and the marker read as
        absent. The frontmatter fence is intact here, so `_redact_profile_text`
        finds a block to work on, finds no `name_variants`, and reports nothing
        to strip - the profile shipped verbatim, malformed YAML and all.
        """
        self._profile_text(
            '---\nid: p-aaaaaaaaaa\nname: Test Person\nrestricted: [unclosed\n---\n# Test Person\n'
        )

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)

        self.assertEqual(result['status'], 'restricted-subject')
        self.assertFalse(self.out_dir.exists())

    def test_subject_whose_frontmatter_is_not_a_block_of_fields_is_not_exported(self):
        """Frontmatter that parses to a scalar, not a mapping.

        `meta` is then a string, and the pre-fix `meta.get('restricted')` raised
        `AttributeError` into the `except` arm - which set the marker to None
        and carried on. An exception on the read is the same failure as a parse
        error: the marker was not read.
        """
        self._profile_text('---\njust a sentence, not fields\n---\n# Test Person\n')

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)

        self.assertEqual(result['status'], 'restricted-subject')
        self.assertFalse(self.out_dir.exists())

    def test_subject_with_an_empty_frontmatter_block_still_exports(self):
        """The boundary, pinned: a block that is present and states nothing.

        This is the line the fix draws, and it is drawn deliberately. A
        frontmatter block that parses is the record SAYING it carries no
        restriction - the same reading every other consumer makes of an absent
        key. No block at all is a record that cannot say anything. Only the
        second is treated as unknown, so the guard cannot creep into refusing
        ordinary records with nothing to declare.

        (A bare `---\\n---\\n` is not this case: `_lib.FRONT_RE` wants a content
        line between the fences, so that file has no frontmatter block as far as
        every tool here is concerned, and the test below holds it to the
        withhold arm.)
        """
        self._profile_text('---\n\n---\n# Test Person\n')

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)

        self.assertEqual(result['status'], 'ok')

    def test_subject_with_only_a_doubled_fence_is_not_exported(self):
        """`---\\n---\\n` reads as no frontmatter, and is withheld on that reading.

        Worth pinning because it looks like the empty block above and is not
        one. The rule the fix applies is "does this file have a frontmatter
        block the shared reader can see", and the answer here is no - so it
        must be withheld, consistently with the reader every other tool uses,
        rather than by some second opinion about what the fences meant.
        """
        self._profile_text('---\n---\n# Test Person\n')

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)

        self.assertEqual(result['status'], 'restricted-subject')

    def test_unreadable_source_is_left_out_with_its_files(self):
        """A source whose own record cannot be read takes its assets with it.

        `_source_copy_plan` already refuses to COPY such a record, so the
        pre-fix leak was not the .md: it was everything hanging off the
        source-level decision, which fell back to the index's 0/1 and read
        `not restricted`. The source was counted as included, so its scan was
        copied into files/ and its title was printed in the README - for a
        record that may have carried `restricted: by-request`.
        """
        self._seed_person()
        src_path = self._seed_source(
            's-1111111111', 'Private Title',
            asset_rel='documents/other/scan.jpg',
        )
        src_path.unlink()
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)

        self.assertEqual(result['status'], 'ok')
        self.assertFalse((result['packet_dir'] / 'files').exists())
        readme = (result['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertNotIn('Private Title', readme)
        self.assertIn('Excluded sources', readme)
        self.assertIn('(could not be read)', readme)
        self.assertTrue(any('could not be read' in m for m in result['messages']))

    def test_unreadable_source_is_not_opened_by_include_restricted(self):
        """The no-override half, and the more serious one.

        The index column is a boolean, so an unreadable record that falls back
        to it cannot express `restricted: by-request` - the one type AGENTS.md
        contract item 6 says is honored everywhere with no override. Falling
        back silently turned a no-override restriction into a plain one that
        `--include-restricted` opens. An unreadable record therefore opens
        under no flag at all, because `by-request` is exactly what cannot be
        ruled out.
        """
        self._seed_person()
        src_path = self._seed_source('s-1111111111', 'Private Title')
        src_path.unlink()
        self._commit_fresh()

        result = packet.run_packet(
            self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True,
            include_restricted=True, include_dna=True,
        )

        self.assertEqual(result['status'], 'ok')
        self.assertFalse((result['packet_dir'] / 'sources').exists())
        readme = (result['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertNotIn('Private Title', readme)
        self.assertIn('(could not be read)', readme)

    def test_unreadable_source_claims_stay_off_the_timeline(self):
        """GUARD - this one passes pre-fix, and says so on purpose.

        The timeline is generated from the index, not from the record, so a
        source dropped at the gather step can still leak its facts there if the
        filter set is not updated with it. Pre-fix that was already held, by a
        different mechanism: `_source_copy_plan` marked the record `unsafe` and
        `run_packet` subtracted `unsafe_source_ids` from the timeline's source
        set. Moving the withhold up to classification must not lose that, so
        this pins the outcome rather than the mechanism - the proof of the fix
        lives in its two neighbours above.
        """
        self._seed_person()
        src_path = self._seed_source('s-1111111111', 'Private Title')
        _insert_claim(
            self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'death',
            'cause of death was suicide', persons=['p-aaaaaaaaaa'],
        )
        src_path.unlink()
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)

        self.assertEqual(result['status'], 'ok')
        timeline = (result['packet_dir'] / 'timeline.md').read_text(encoding='utf-8')
        self.assertNotIn('suicide', timeline)

    def test_source_with_unparseable_frontmatter_is_left_out(self):
        """Route 2 for a source: the frontmatter itself will not parse.

        Distinct from the gone-file case above because the file opens fine -
        `read_record` reports E010 and hands back an empty `meta` rather than
        raising, which is what made the marker read as absent. The `restricted:`
        key could be sitting in the very text that would not parse.
        """
        self._seed_person()
        src_path = self._seed_source('s-1111111111', 'Private Title')
        src_path.write_text(
            '---\nid: s-1111111111\nrestricted: [unclosed\n---\n## Claims\n',
            encoding='utf-8',
        )
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)

        self.assertEqual(result['status'], 'ok')
        readme = (result['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertNotIn('Private Title', readme)
        self.assertIn('(could not be read)', readme)

    def test_source_with_unparseable_claims_keeps_its_source_level_verdict(self):
        """The narrowing, pinned: a broken CLAIMS block is not a broken marker.

        The first draft of this fix asked `read_record` for `parse_errors`,
        which also carries claims-block failures - so a source whose claims YAML
        would not parse was excluded at the source level and its scan was
        dropped with it. That is over-reach: the frontmatter opened fine and
        said what it says, and the claim-level guard already handles the claims
        (`_source_copy_plan` marks the record unsafe, so the record and its
        claims are withheld while the assets, which carry no claim YAML, still
        ship). The marker read and the claims read are separate questions with
        separately correct answers; this holds them apart.
        """
        self._seed_person()
        src_path = self._seed_source(
            's-1111111111', 'Ordinary Title', asset_rel='documents/other/scan.jpg',
        )
        src_path.write_text(
            '---\nid: s-1111111111\ntitle: Ordinary Title\n---\n'
            '## Claims\n```yaml\n- {broken: [\n```\n',
            encoding='utf-8',
        )
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)

        self.assertEqual(result['status'], 'ok')
        readme = (result['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertIn('Included sources', readme)
        self.assertNotIn('(could not be read)', readme)
        # The record itself is still withheld by the claim-level guard...
        self.assertEqual(list((result['packet_dir'] / 'sources').glob('*')), [])
        self.assertTrue(any('left out of sources/' in m for m in result['messages']))
        # ...while the asset, which carries no claim YAML, still travels.
        self.assertTrue((result['packet_dir'] / 'files' / 'scan.jpg').exists())

    def test_purpose_block_stripped_from_an_ordinary_source_copy(self):
        # The common case: a source with no restricted claim never enters
        # `_source_copy_plan`'s 'redact' bucket and used to go straight to a
        # byte copy - but #75's purpose block still must not ship. This is
        # the regression the redact-only source test above cannot catch,
        # since a source with something to redact takes a different code
        # path than the ordinary one nearly every source actually uses.
        self._seed_person()
        src_path = self._seed_source('s-1111111111', 'Ordinary Title')
        src_path.write_text(
            '---\nid: s-1111111111\ntitle: Ordinary Title\nsource_type: letter\n---\n'
            "> **This source's record - yours to write.** The citation and "
            "claims for one piece\n"
            "> of evidence. `fha process` scaffolded this file; everything "
            "below is yours to\n"
            '> correct and add to.\n\n'
            '## Claims\n```yaml\n```\n\n'
            '## Notes\nA note about the letter.\n',
            encoding='utf-8',
        )
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)
        self.assertEqual(result['status'], 'ok')
        copied = next((result['packet_dir'] / 'sources').glob('*.md')).read_text(encoding='utf-8')
        self.assertNotIn('yours to write', copied)
        self.assertIn('## Claims', copied)
        self.assertIn('## Notes', copied)
        self.assertIn('A note about the letter.', copied)

    def test_purpose_block_stripped_from_a_redacted_source_copy(self):
        # The other branch: a source WITH a restricted claim being withheld
        # goes through `_copy_redacted_source` instead - the purpose block
        # must not survive that path either.
        self._seed_person()
        src_path = self._seed_source('s-1111111111', 'Restricted Source', restricted=0)
        src_path.write_text(
            '---\nid: s-1111111111\ntitle: Restricted Source\nsource_type: letter\n---\n'
            "> **This source's record - yours to write.** The citation and "
            "claims for one piece\n"
            "> of evidence. `fha process` scaffolded this file; everything "
            "below is yours to\n"
            '> correct and add to.\n\n'
            '## Claims\n```yaml\n'
            '- value: "Cause of death: suicide"\n'
            '  type: death\n'
            '  persons: [p-aaaaaaaaaa]\n'
            '  id: c-1111111111\n'
            '  status: accepted\n'
            '  restricted: true\n'
            '```\n',
            encoding='utf-8',
        )
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)
        self.assertEqual(result['status'], 'ok')
        copied = next((result['packet_dir'] / 'sources').glob('*.md')).read_text(encoding='utf-8')
        self.assertNotIn('yours to write', copied)
        self.assertNotIn('suicide', copied)   # the restricted claim itself is still cut

    def test_pre_75_source_with_no_purpose_block_ships_unchanged(self):
        # No backfill/migration tooling: an older-shaped source record that
        # never had a purpose block ships exactly as it always did.
        self._seed_person()
        src_path = self._seed_source('s-1111111111', 'Ordinary Title')
        original = (
            '---\nid: s-1111111111\ntitle: Ordinary Title\n---\n## Claims\n'
        )
        src_path.write_text(original, encoding='utf-8')
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)
        self.assertEqual(result['status'], 'ok')
        copied = next((result['packet_dir'] / 'sources').glob('*.md')).read_text(encoding='utf-8')
        self.assertEqual(copied, original)

    def test_readable_source_with_no_marker_is_still_included(self):
        """The guard against over-reach: an ordinary source still ships.

        The three tests above all withhold; this one proves the withhold is
        keyed on the read failing, not on the marker being absent. Without it a
        fix that excluded every source would pass the whole group.
        """
        self._seed_person()
        self._seed_source('s-1111111111', 'Ordinary Title',
                          asset_rel='documents/other/scan.jpg')
        self._commit_fresh()

        result = packet.run_packet(self.archive_root, 'p-aaaaaaaaaa', self.out_dir, no_photos=True)

        self.assertEqual(result['status'], 'ok')
        readme = (result['packet_dir'] / 'README.txt').read_text(encoding='utf-8')
        self.assertIn('Included sources', readme)
        self.assertIn('Ordinary Title', readme)
        self.assertTrue((result['packet_dir'] / 'files' / 'scan.jpg').exists())


def _scandir_denying(match: str):
    """An os.scandir stand-in that refuses to list any path ending in `match`.

    The fault goes in at `os.scandir` because `os.walk` resolves it at call
    time on every supported Python - that is what makes the `onerror` seam
    observable here. chmod cannot produce this: CI runs as root, which ignores
    mode bits, and Windows has no equivalent.

    What this deliberately does NOT rely on: that pathlib's `rglob` reaches the
    disk the same way. It does on 3.11/3.12/3.14, but NOT on the 3.10 floor
    (pathlib routes through an accessor object that bound `os.scandir` at
    import time, so a later patch is invisible) and not on 3.13. So the
    injection does not reproduce the pre-fix `rglob` behaviour on every version
    we support - a regression back to `rglob` is still caught everywhere, but
    on the floor it is caught by the warning going missing rather than by the
    folder reading as empty.
    """
    real_scandir = os.scandir

    def scandir(path='.'):
        if str(path).endswith(match):
            err = PermissionError(13, 'Permission denied')
            err.filename = str(path)
            raise err
        return real_scandir(path)

    return scandir


if __name__ == '__main__':
    unittest.main()
