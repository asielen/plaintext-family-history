"""
test_site.py - fha site (M8.1 source page + M8.2 person page).

Builds a synthetic .cache/index.sqlite (and, where needed, .cache/photos.sqlite)
directly from index.py's / photoindex.py's DDL - the same pattern as
tests/test_packet.py - so the publication generator can be exercised without a
full archive fixture, exiftool, or a network. The prose/citation that `fha site`
reads from the record .md files is written to disk alongside the index rows.

`site.py`'s module stem collides with Python's stdlib `site`, so it is loaded by
path under the private name `fha_site` (the same trick fha.py uses).
"""

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

from index import _DDL as INDEX_DDL
from photoindex import _DDL as PHOTOS_DDL

_spec = importlib.util.spec_from_file_location('fha_site', ROOT / 'tools' / 'site.py')
site = importlib.util.module_from_spec(_spec)
sys.modules['fha_site'] = site
_spec.loader.exec_module(site)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        cache = self.archive_root / '.cache'
        cache.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(cache / 'index.sqlite'))
        self.conn.executescript(INDEX_DDL)
        self.conn.row_factory = sqlite3.Row
        self.out_dir = self.archive_root / '.cache' / 'site'

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    # - seeding -

    def _seed_person(self, pid, name='Test Person', *, living='false', tier='curated',
                     surname='Person', body='# Test Person\n'):
        rel = f'people/{surname.lower()}__test_{pid}.md'
        path = self.archive_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'---\nid: {pid}\nname: {name}\n---\n{body}', encoding='utf-8')
        self.conn.execute(
            'INSERT INTO persons(id, name, surname, sex, living, tier, status, path) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (pid, name, surname, 'M', living, tier, 'active', rel),
        )

    def _seed_source(self, sid, title='A Source', *, source_type='census', restricted=0,
                     publication_ok=None, citation='A citation.', people=(), frontmatter=None):
        rel = f'sources/{source_type}/src_{sid}.md'
        path = self.archive_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if frontmatter is None:
            frontmatter = f'---\nid: {sid}\ntitle: {title}\nsource_type: {source_type}\ncitation: "{citation}"\n---\n\n## Claims\n'
        path.write_text(frontmatter, encoding='utf-8')
        self.conn.execute(
            'INSERT INTO sources(id, title, source_type, restricted, publication_ok, status, path) '
            'VALUES (?,?,?,?,?,?,?)',
            (sid, title, source_type, restricted, publication_ok, 'active', rel),
        )
        for pid in people:
            self.conn.execute(
                'INSERT INTO source_people(source_id, person_id) VALUES (?,?)', (sid, pid))

    def _seed_claim(self, cid, sid, ctype, value, *, status='accepted', date_edtf=None,
                    place_text=None, persons=()):
        self.conn.execute(
            'INSERT INTO claims(id, source_id, type, value, status, date_edtf, date_min, place_text) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (cid, sid, ctype, value, status, date_edtf, (date_edtf or '')[:4] + '-01-01' if date_edtf else None, place_text),
        )
        for pos, pid in enumerate(persons):
            self.conn.execute(
                'INSERT INTO claim_persons(claim_id, person_id, position) VALUES (?,?,?)', (cid, pid, pos))

    def _seed_rel(self, pid, rel, other):
        self.conn.execute(
            'INSERT INTO relationships(person_id, rel, other_id, claim_id) VALUES (?,?,?,?)',
            (pid, rel, other, 'c-rrrrrrrrrr'))

    def _run(self, *, linked=False, dry_run=False):
        self.conn.commit()
        future = time.time() + 5
        os.utime(self.archive_root / '.cache' / 'index.sqlite', (future, future))
        return site.run_site(self.archive_root, self.out_dir, linked=linked, dry_run=dry_run)

    def _read(self, relpath):
        return (self.out_dir / relpath).read_text(encoding='utf-8')


class SourcePageTests(_Base):
    def test_source_page_has_citation_claims_and_status(self):
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe')
        self._seed_source('s-1111111111', '1880 Census', citation='1880 U.S. Census, Kansas.',
                          people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'residence', 'Lived in Kansas',
                         status='accepted', date_edtf='1880', place_text='Kansas',
                         persons=('p-aaaaaaaaaa',))
        self._seed_claim('c-2222222222', 's-1111111111', 'occupation', 'Bookkeeper',
                         status='suggested', persons=('p-aaaaaaaaaa',))
        res = self._run(linked=True)
        self.assertEqual(res['status'], 'ok')
        html = self._read('sources/s-1111111111.html')
        self.assertIn('1880 U.S. Census, Kansas.', html)         # citation from .md
        self.assertIn('Lived in Kansas', html)
        self.assertIn('status-accepted', html)
        self.assertIn('status-suggested', html)                  # all statuses shown w/ badge
        self.assertIn('../persons/p-aaaaaaaaaa.html', html)      # person link in People column

    def test_standalone_excludes_unreviewed_and_rejected_claims(self):
        # P2-2: a public snapshot shows only accepted + needs-review; --linked
        # (developer preview) shows every status with its badge.
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe')
        self._seed_source('s-1111111111', 'Mixed Source', people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'residence', 'Accepted fact',
                         status='accepted', persons=('p-aaaaaaaaaa',))
        self._seed_claim('c-2222222222', 's-1111111111', 'occupation', 'Under review',
                         status='needs-review', persons=('p-aaaaaaaaaa',))
        self._seed_claim('c-3333333333', 's-1111111111', 'occupation', 'AI draft guess',
                         status='suggested', persons=('p-aaaaaaaaaa',))
        self._seed_claim('c-4444444444', 's-1111111111', 'occupation', 'Known wrong',
                         status='rejected', persons=('p-aaaaaaaaaa',))
        self._run(linked=False)
        public = self._read('sources/s-1111111111.html')
        self.assertIn('Accepted fact', public)
        self.assertIn('Under review', public)
        self.assertNotIn('AI draft guess', public)        # suggested withheld
        self.assertNotIn('Known wrong', public)            # rejected withheld
        self._run(linked=True)
        dev = self._read('sources/s-1111111111.html')
        self.assertIn('AI draft guess', dev)               # linked shows everything
        self.assertIn('Known wrong', dev)

    def test_missing_asset_listed_not_linked(self):
        self._seed_source('s-1111111111', 'Has Asset')
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role) VALUES (?,?,?)',
            ('s-1111111111', 'documents/census/ghost.txt', 'page-1'))
        self._run(linked=True)
        html = self._read('sources/s-1111111111.html')
        self.assertIn('file not available', html)


class SourceRedactionTests(_Base):
    def _setup_redactable(self):
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe',
                           body='# Jane\n## Biography\nSee [S-2222222222].\n')
        self._seed_source('s-1111111111', 'Public Source', people=('p-aaaaaaaaaa',))
        self._seed_source('s-2222222222', 'Restricted Source', restricted=1, people=('p-aaaaaaaaaa',))

    def test_restricted_source_no_page_standalone(self):
        self._setup_redactable()
        self._run(linked=False)
        self.assertFalse((self.out_dir / 'sources' / 's-2222222222.html').exists())
        self.assertTrue((self.out_dir / 'sources' / 's-1111111111.html').exists())
        bio = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn(site._RESTRICTED_LABEL, bio)               # reference redacted, not linked
        self.assertNotIn('sources/s-2222222222.html', bio)

    def test_restricted_source_page_in_linked(self):
        self._setup_redactable()
        self._run(linked=True)
        self.assertTrue((self.out_dir / 'sources' / 's-2222222222.html').exists())
        bio = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('sources/s-2222222222.html', bio)          # linked, not redacted

    def test_publication_ok_false_redacted_standalone(self):
        self._seed_source('s-3333333333', 'No-Pub Source', publication_ok=0)
        self._run(linked=False)
        self.assertFalse((self.out_dir / 'sources' / 's-3333333333.html').exists())

    def test_dna_source_redacted_standalone(self):
        self._seed_source('s-4444444444', 'DNA Source', source_type='dna')
        self._run(linked=False)
        self.assertFalse((self.out_dir / 'sources' / 's-4444444444.html').exists())


class PersonPageTests(_Base):
    def _setup_thomas(self):
        bio = ('# Thomas\n## Biography\n'
               'Thomas worked as a bookkeeper [S-1111111111] and married [P-bbbbbbbbbb].\n'
               'See also [S-9999999999].\n\n'
               '## Stories\nA tale worth keeping.\n')
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley', body=bio)
        self._seed_person('p-bbbbbbbbbb', 'Margaret Cole', tier='stub')
        self._seed_source('s-1111111111', 'Census', people=('p-aaaaaaaaaa',))
        self._seed_source('s-2222222222', 'Marriage Record', source_type='vital-record',
                          people=('p-aaaaaaaaaa',))
        self._seed_claim('c-1111111111', 's-1111111111', 'birth', 'Born about 1840',
                         status='accepted', date_edtf='1840', place_text='New York',
                         persons=('p-aaaaaaaaaa',))
        self._seed_claim('c-2222222222', 's-2222222222', 'marriage', 'Married Margaret',
                         status='accepted', date_edtf='1871', persons=('p-aaaaaaaaaa',))
        self._seed_claim('c-3333333333', 's-1111111111', 'residence', 'Lived in Fairview',
                         status='needs-review', date_edtf='1880', persons=('p-aaaaaaaaaa',))
        self._seed_claim('c-4444444444', 's-1111111111', 'occupation', 'Bookkeeper (unreviewed)',
                         status='suggested', date_edtf='1880', persons=('p-aaaaaaaaaa',))
        self._seed_rel('p-aaaaaaaaaa', 'spouse', 'p-bbbbbbbbbb')

    def test_person_page_all_sections(self):
        self._setup_thomas()
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('<h2>Biography</h2>', html)
        self.assertIn('<h2>Timeline</h2>', html)
        self.assertIn('Friends', html)            # Friends & Family
        self.assertIn('<h2>Sources</h2>', html)
        # Summary block from accepted vitals
        self.assertIn('Born', html)
        self.assertIn('Married', html)
        # Stories section rendered
        self.assertIn('A tale worth keeping.', html)

    def test_biography_token_swap(self):
        self._setup_thomas()
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('../sources/s-1111111111.html', html)      # cited source appears (footnote list)
        self.assertIn('class="fn-ref"', html)                    # [S-id] in prose -> superscript footnote
        self.assertIn('Margaret Cole', html)                     # [P-id] -> name (stub, no link)
        self.assertNotIn('9999999999', html)                     # unresolved source id hidden, never shown raw

    def test_timeline_excludes_suggested_includes_needs_review(self):
        self._setup_thomas()
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Lived in Fairview', html)                 # needs-review present
        self.assertNotIn('Bookkeeper (unreviewed)', html)        # suggested excluded from timeline
        self.assertIn('1840s', html)                             # decade grouping
        self.assertIn('1880s', html)

    def test_family_and_source_footnotes(self):
        self._setup_thomas()
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Spouses', html)
        self.assertIn('Margaret Cole', html)
        # Sources are a numbered footnote list of human names, not raw [S-id] chips,
        # and inline citations are superscript refs into it.
        self.assertIn('<ol class="footnotes">', html)
        self.assertIn('id="fn-1"', html)
        self.assertIn('>Census</a>', html)                       # source shown by name
        self.assertIn('class="fn-ref"', html)                    # inline superscript ref
        self.assertNotIn('[S-1111111111]', html)                 # backend id never shown inline
        self.assertNotIn('<h3>census</h3>', html)                # no longer grouped by type
        self.assertNotIn('class="ids"', html)                    # person id line removed


class PersonRedactionTests(_Base):
    def test_living_person_no_page_standalone(self):
        self._seed_person('p-aaaaaaaaaa', 'Living Larry', living='true')
        self._seed_person('p-bbbbbbbbbb', 'Dead Dan', living='false',
                          body='# Dan\n## Biography\nKnew [P-aaaaaaaaaa] well.\n')
        self._run(linked=False)
        self.assertFalse((self.out_dir / 'persons' / 'p-aaaaaaaaaa.html').exists())
        self.assertTrue((self.out_dir / 'persons' / 'p-bbbbbbbbbb.html').exists())
        dan = self._read('persons/p-bbbbbbbbbb.html')
        self.assertIn(site._LIVING_LABEL, dan)
        self.assertNotIn('persons/p-aaaaaaaaaa.html', dan)

    def test_unknown_living_treated_as_living(self):
        self._seed_person('p-aaaaaaaaaa', 'Unknown Ursula', living='unknown')
        self._run(linked=False)
        self.assertFalse((self.out_dir / 'persons' / 'p-aaaaaaaaaa.html').exists())

    def test_living_person_has_page_in_linked(self):
        self._seed_person('p-aaaaaaaaaa', 'Living Larry', living='true')
        self._run(linked=True)
        self.assertTrue((self.out_dir / 'persons' / 'p-aaaaaaaaaa.html').exists())

    def test_stub_person_never_gets_page(self):
        self._seed_person('p-aaaaaaaaaa', 'Stubby', tier='stub')
        self._run(linked=True)
        self.assertFalse((self.out_dir / 'persons' / 'p-aaaaaaaaaa.html').exists())

    def test_linked_shows_living_person_data(self):
        # Inverse of redaction: in --linked, a living person keeps their real
        # name and a working link (developer preview is unredacted).
        self._seed_person('p-aaaaaaaaaa', 'Living Larry', living='true')
        self._seed_person('p-bbbbbbbbbb', 'Dead Dan', living='false',
                          body='# Dan\n## Biography\nKnew [P-aaaaaaaaaa] well.\n')
        self._run(linked=True)
        dan = self._read('persons/p-bbbbbbbbbb.html')
        self.assertIn('Living Larry', dan)                       # real name, not redacted
        self.assertIn('href="p-aaaaaaaaaa.html"', dan)           # real link (sibling page)
        self.assertNotIn(site._LIVING_LABEL, dan)


class ResilienceTests(_Base):
    def test_malformed_source_yaml_warns_and_continues(self):
        # Broken frontmatter YAML in one source; another source is fine.
        self._seed_source('s-1111111111', 'Broken Source',
                          frontmatter='---\nid: s-1111111111\ntitle: "unterminated\n  : : :\n---\n\n## Claims\n')
        self._seed_source('s-2222222222', 'Good Source')
        res = self._run(linked=True)
        self.assertEqual(res['status'], 'ok')
        self.assertTrue(any('formatting problem' in m or 'could not read' in m for m in res['messages']))
        # Both pages still built; broken one falls back to its index title.
        self.assertTrue((self.out_dir / 'sources' / 's-1111111111.html').exists())
        self.assertIn('Broken Source', self._read('sources/s-1111111111.html'))
        self.assertTrue((self.out_dir / 'sources' / 's-2222222222.html').exists())

    def test_dry_run_writes_nothing(self):
        self._seed_source('s-1111111111', 'A Source')
        res = self._run(dry_run=True)
        self.assertEqual(res['status'], 'dry-run')
        self.assertFalse((self.out_dir / 'sources').exists())
        self.assertFalse((self.out_dir / 'index.html').exists())

    def test_rebuild_drops_now_redacted_page(self):
        self._seed_person('p-aaaaaaaaaa', 'Was Dead', living='false')
        self._run(linked=False)
        self.assertTrue((self.out_dir / 'persons' / 'p-aaaaaaaaaa.html').exists())
        # Person becomes living; a rebuild must remove the stale page.
        self.conn.execute("UPDATE persons SET living='true' WHERE id='p-aaaaaaaaaa'")
        self._run(linked=False)
        self.assertFalse((self.out_dir / 'persons' / 'p-aaaaaaaaaa.html').exists())

    def test_refuses_archive_root_as_output(self):
        # The site clears its sources/ subtree on rebuild; the archive's own
        # sources/ must never be the target.
        self._seed_source('s-1111111111', 'A Source')
        self.conn.commit()
        future = time.time() + 5
        os.utime(self.archive_root / '.cache' / 'index.sqlite', (future, future))
        (self.archive_root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        res = site.run_site(self.archive_root, self.archive_root, linked=True)
        self.assertEqual(res['status'], 'bad-output')
        self.assertTrue((self.archive_root / 'sources').exists())   # records untouched

    def test_refuses_output_inside_record_tree(self):
        # Building into the archive's own sources/ would scatter pages among the
        # record .md files; refuse before any write.
        self._seed_source('s-1111111111', 'A Source')
        self.conn.commit()
        future = time.time() + 5
        os.utime(self.archive_root / '.cache' / 'index.sqlite', (future, future))
        res = site.run_site(self.archive_root, self.archive_root / 'sources', linked=True)
        self.assertEqual(res['status'], 'bad-output')

    def test_no_index_status(self):
        # Remove the index file entirely.
        self.conn.close()
        (self.archive_root / '.cache' / 'index.sqlite').unlink()
        res = site.run_site(self.archive_root, self.out_dir, linked=True)
        self.assertEqual(res['status'], 'no-index')
        # reopen so tearDown's close() doesn't error
        self.conn = sqlite3.connect(':memory:')

    def test_old_schema_index_rejected(self):
        # P2-4: an index built before the publication_ok three-state fix (older
        # schema version) must be refused, not trusted, so a rebuild applies the
        # corrected redaction. Overwrite with a v1-shaped index.
        self.conn.close()
        db = self.archive_root / '.cache' / 'index.sqlite'
        db.unlink()
        conn = sqlite3.connect(str(db))
        conn.executescript(
            "PRAGMA user_version=1;"
            "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "INSERT INTO meta(key, value) VALUES ('schema_version', '1');"
            "CREATE TABLE persons(id TEXT, name TEXT, surname TEXT, sex TEXT, living TEXT,"
            " tier TEXT, status TEXT, merged_into TEXT, path TEXT);"
            "CREATE TABLE sources(id TEXT, title TEXT, source_type TEXT, date_edtf TEXT,"
            " repository TEXT, source_class TEXT, restricted INTEGER, publication_ok INTEGER,"
            " status TEXT, path TEXT);"
        )
        conn.commit()
        conn.close()
        future = time.time() + 5
        os.utime(db, (future, future))
        res = site.run_site(self.archive_root, self.out_dir, linked=True)
        self.assertEqual(res['status'], 'no-index')   # old schema → refused, prompt to rebuild
        self.conn = sqlite3.connect(':memory:')


class AssetTests(_Base):
    def _make_photos_db(self):
        conn = sqlite3.connect(str(self.archive_root / '.cache' / 'photos.sqlite'))
        conn.executescript(PHOTOS_DDL)
        conn.row_factory = sqlite3.Row
        return conn

    def _make_photos_fresh(self):
        far_future = time.time() + 10_000
        os.utime(self.archive_root / '.cache' / 'photos.sqlite', (far_future, far_future))

    def test_linked_photo_strip(self):
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe')
        # A real (dummy) file on disk; linked mode only checks existence + links it.
        img = self.archive_root / 'photos' / '1880' / 'jane.jpg'
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(b'not-a-real-image-but-exists')
        pconn = self._make_photos_db()
        pconn.execute(
            'INSERT INTO photos(path, group_id, is_primary, caption) VALUES (?,?,?,?)',
            ('photos/1880/jane.jpg', 'g1', 1, 'Jane in 1880'))
        pconn.execute(
            'INSERT INTO photo_people(path, person_ref, via) VALUES (?,?,?)',
            ('photos/1880/jane.jpg', 'p-aaaaaaaaaa', 'pid-keyword'))
        pconn.commit()
        pconn.close()
        self._make_photos_fresh()
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Photographs', html)
        self.assertIn('Jane in 1880', html)
        self.assertIn('jane.jpg', html)

    @unittest.skipUnless(site._PIL_AVAILABLE, 'Pillow not installed')
    def test_standalone_image_derivative(self):
        from PIL import Image
        self._seed_source('s-1111111111', 'Photo Source', source_type='photo')
        img = self.archive_root / 'photos' / '1880' / 'pic.png'
        img.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (2000, 1500), (120, 90, 60)).save(img)
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role) VALUES (?,?,?)',
            ('s-1111111111', 'photos/1880/pic.png', 'front'))
        self._run(linked=False)
        # An EXIF-stripped, resized derivative is created under media/ and linked.
        derivs = list((self.out_dir / 'media').rglob('*.jpg'))
        self.assertTrue(derivs, 'expected a media derivative to be written')
        with Image.open(derivs[0]) as im:
            self.assertLessEqual(max(im.size), site._DERIVATIVE_MAX_PX)
            self.assertEqual(im.info.get('exif'), None)
        self.assertIn('media/', self._read('sources/s-1111111111.html'))

    @unittest.skipUnless(site._PIL_AVAILABLE, 'Pillow not installed')
    def test_same_stem_photos_get_distinct_derivatives(self):
        # Two photos sharing a filename stem in different folders must not
        # overwrite each other's derivative (P2-1).
        from PIL import Image
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe')
        pconn = self._make_photos_db()
        for i, (group, sub) in enumerate(((1, '1880'), (2, '1890'))):
            img = self.archive_root / 'photos' / sub / 'scan.jpg'
            img.parent.mkdir(parents=True, exist_ok=True)
            Image.new('RGB', (300, 200), (10 * i, 20, 30)).save(img)
            pconn.execute('INSERT INTO photos(path, group_id, is_primary, caption) VALUES (?,?,?,?)',
                          (f'photos/{sub}/scan.jpg', f'g{group}', 1, f'Scan {sub}'))
            pconn.execute('INSERT INTO photo_people(path, person_ref, via) VALUES (?,?,?)',
                          (f'photos/{sub}/scan.jpg', 'p-aaaaaaaaaa', 'pid-keyword'))
        pconn.commit()
        pconn.close()
        self._make_photos_fresh()
        self._run(linked=False)
        derivs = list((self.out_dir / 'media' / 'people').glob('scan_*.jpg'))
        self.assertEqual(len(derivs), 2, 'both same-stem photos should get distinct derivatives')

    def test_standalone_non_image_kept_in_archive(self):
        self._seed_source('s-1111111111', 'Doc Source', source_type='letter')
        doc = self.archive_root / 'documents' / 'letters' / 'note_s-1111111111.txt'
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text('a letter', encoding='utf-8')
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role) VALUES (?,?,?)',
            ('s-1111111111', 'documents/letters/note_s-1111111111.txt', 'transcript'))
        self._run(linked=False)
        html = self._read('sources/s-1111111111.html')
        self.assertIn('original kept in the archive', html)      # not copied out of the archive


class PlacePageTests(_Base):
    def _seed_place(self, lid, name, *, hierarchy=None, within=None, lat=None, lon=None,
                    alt_names=(), history=()):
        self.conn.execute(
            'INSERT INTO places(id, name, hierarchy, within, lat, lon) VALUES (?,?,?,?,?,?)',
            (lid, name, hierarchy, within, lat, lon))
        for a in alt_names:
            self.conn.execute('INSERT INTO place_names(place_id, alt_name) VALUES (?,?)', (lid, a))
        for period, hier in history:
            self.conn.execute(
                'INSERT INTO place_history(place_id, period_edtf, date_min, hierarchy) VALUES (?,?,?,?)',
                (lid, period, (period or '')[:4], hier))

    def _seed_claim_at_place(self, cid, sid, lid, value, persons):
        self.conn.execute(
            'INSERT INTO claims(id, source_id, type, value, status, place_id, date_edtf, date_min) '
            "VALUES (?,?,?,?,?,?,?,?)",
            (cid, sid, 'residence', value, 'accepted', lid, '1880', '1880'))
        for pos, pid in enumerate(persons):
            self.conn.execute(
                'INSERT INTO claim_persons(claim_id, person_id, position) VALUES (?,?,?)', (cid, pid, pos))

    def test_place_page_sections(self):
        self._seed_person('p-aaaaaaaaaa', 'Jane Doe')
        self._seed_source('s-1111111111', 'Census')
        self._seed_place('l-1111111111', 'Fairview', hierarchy='Fairview, Kansas',
                         lat=39.8, lon=-95.6, alt_names=('Fairview City',),
                         history=(('1858/1861', 'Fairview, Kansas Territory'),))
        self._seed_place('l-2222222222', 'Fairview Cemetery', within='l-1111111111')
        self._seed_claim_at_place('c-1111111111', 's-1111111111', 'l-1111111111',
                                  'Lived in Fairview', ('p-aaaaaaaaaa',))
        self._run(linked=True)
        html = self._read('places/l-1111111111.html')
        self.assertIn('Fairview', html)
        self.assertIn('openstreetmap.org', html)                 # coords → map URL, no embed
        self.assertIn('Fairview City', html)                     # alt name
        self.assertIn('Kansas Territory', html)                  # dated history
        self.assertIn('Lived in Fairview', html)                 # claim naming the place
        self.assertIn('../persons/p-aaaaaaaaaa.html', html)      # associated person linked
        self.assertIn('"l-2222222222.html"', html)               # micro-place (within:) linked (same places/ dir)

    def test_l_token_links_to_place_page(self):
        self._seed_place('l-1111111111', 'Fairview')
        self._seed_person('p-aaaaaaaaaa', 'Jane',
                          body='# Jane\n## Biography\nBorn in [L-1111111111].\n')
        self._run(linked=True)
        self.assertIn('../places/l-1111111111.html', self._read('persons/p-aaaaaaaaaa.html'))

    def test_claim_place_column_links_to_place_page(self):
        # Symmetry fix: a claim's place cell links to the place page when the
        # claim carries a registered place_id (not just prose [L-id] tokens).
        self._seed_person('p-aaaaaaaaaa', 'Jane')
        self._seed_source('s-1111111111', 'Census', people=('p-aaaaaaaaaa',))
        self._seed_place('l-1111111111', 'Fairview')
        self._seed_claim_at_place('c-1111111111', 's-1111111111', 'l-1111111111',
                                  'Lived in Fairview', ('p-aaaaaaaaaa',))
        self._run(linked=True)
        # Source page claims table and the person timeline both link the place.
        self.assertIn('../places/l-1111111111.html', self._read('sources/s-1111111111.html'))
        self.assertIn('../places/l-1111111111.html', self._read('persons/p-aaaaaaaaaa.html'))

    def test_freetext_place_without_id_is_not_linked(self):
        self._seed_person('p-aaaaaaaaaa', 'Jane')
        self._seed_source('s-1111111111', 'Census', people=('p-aaaaaaaaaa',))
        # place_text but no place_id → plain text, no link, no crash.
        self.conn.execute(
            "INSERT INTO claims(id, source_id, type, value, status, place_text) VALUES (?,?,?,?,?,?)",
            ('c-1111111111', 's-1111111111', 'residence', 'Somewhere', 'accepted', 'Old Country'))
        self.conn.execute('INSERT INTO claim_persons(claim_id, person_id, position) VALUES (?,?,?)',
                          ('c-1111111111', 'p-aaaaaaaaaa', 0))
        self._run(linked=True)
        html = self._read('sources/s-1111111111.html')
        self.assertIn('Old Country', html)
        self.assertNotIn('places/', html.split('Old Country')[0][-200:])  # no place link around it


class DiscoveriesTests(_Base):
    def _write_discoveries(self, text):
        path = self.archive_root / 'notes' / 'discoveries.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def test_discoveries_links_and_redacts(self):
        self._seed_person('p-aaaaaaaaaa', 'Dead Dan', living='false')
        self._seed_person('p-bbbbbbbbbb', 'Living Larry', living='true')
        self._seed_source('s-1111111111', 'A Source')
        self._write_discoveries(
            '# Discoveries Log\n\n'
            '## 2026-06-01\nConfirmed [P-aaaaaaaaaa] via [S-1111111111].\n\n'
            '## 2026-06-02\nNew lead on [P-bbbbbbbbbb].\n')
        self._run(linked=False)
        html = self._read('discoveries.html')
        self.assertIn('persons/p-aaaaaaaaaa.html', html)                    # dead person linked
        self.assertIn('sources/s-1111111111.html', html)                    # source linked
        self.assertIn(site._LIVING_LABEL, html)                              # living person redacted
        self.assertNotIn('persons/p-bbbbbbbbbb.html', html)                  # ...and not linked

    def test_discoveries_teaser_on_home(self):
        self._seed_person('p-aaaaaaaaaa', 'Dan')
        self._write_discoveries(
            '# Discoveries Log\n\n## 2026-06-01\nFirst win.\n\n## 2026-06-02\nSecond win.\n')
        self._run(linked=True)
        home = self._read('index.html')
        self.assertIn('Recent discoveries', home)
        self.assertIn('Second win.', home)
        self.assertIn('discoveries.html', home)                  # link to full page

    def test_missing_discoveries_file_is_fine(self):
        self._seed_person('p-aaaaaaaaaa', 'Dan')
        res = self._run(linked=True)
        self.assertEqual(res['status'], 'ok')
        self.assertIn('No discoveries', self._read('discoveries.html'))
        self.assertNotIn('Recent discoveries', self._read('index.html'))

    def test_ambiguous_name_link_to_living_is_redacted(self):
        # Two people share a name; one is living. The clash drops the name from
        # the single-id alias_map, so `[[John Smith]]` fails to resolve - it must
        # fail closed (redact), not publish the living person's name verbatim.
        self._seed_person('p-aaaaaaaaaa', 'John Smith', living='false', surname='Smith')
        self._seed_person('p-bbbbbbbbbb', 'John Smith', living='true', surname='Smith')
        for pid in ('p-aaaaaaaaaa', 'p-bbbbbbbbbb'):
            self.conn.execute("INSERT INTO aliases(alias, canonical_id, kind) VALUES (?,?,?)",
                              ('john smith', pid, 'name'))
        self._write_discoveries('# Discoveries Log\n\n## 2026-06-01\nA lead on [[John Smith]].\n')
        self._run(linked=False)
        html = self._read('discoveries.html')
        self.assertIn(site._LIVING_LABEL, html)          # redacted, not leaked
        self.assertNotIn('John Smith', html)             # the name never appears

    def test_unaccepted_draft_excluded_from_discoveries(self):
        # The standalone site is external output, so an AI-DRAFT block in
        # discoveries.md must be stripped just like person prose is.
        self._seed_person('p-aaaaaaaaaa', 'Dan')
        self._write_discoveries(
            '# Discoveries Log\n\n'
            '## 2026-06-01\nA published finding.\n\n'
            '## 2026-06-02\nAn unreviewed draft lead.\n\n'
            '<!-- AI-DRAFT 2026-07-01 claude-x - drafted -->\n')
        self._run(linked=False)
        html = self._read('discoveries.html')
        self.assertIn('A published finding.', html)
        self.assertNotIn('An unreviewed draft lead.', html)
        self.assertNotIn('AI-DRAFT', html)

    def test_damaged_draft_marker_withholds_discoveries(self):
        # Fail closed: an unterminated marker withholds the whole page rather
        # than leaking half-parsed draft text or a raw marker.
        self._seed_person('p-aaaaaaaaaa', 'Dan')
        self._write_discoveries(
            '# Discoveries Log\n\n## 2026-06-01\nA finding.\n\n<!-- AI-DRAFT missing its close\n')
        self._run(linked=False)
        html = self._read('discoveries.html')
        self.assertNotIn('A finding.', html)
        self.assertNotIn('AI-DRAFT', html)


class HomePageTests(_Base):
    def test_surname_az_index(self):
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley', surname='Hartley')
        self._seed_person('p-bbbbbbbbbb', 'James Bradford', surname='Bradford')
        self._run(linked=True)
        home = self._read('index.html')
        self.assertIn('<h3>B</h3>', home)
        self.assertIn('<h3>H</h3>', home)
        self.assertLess(home.index('<h3>B</h3>'), home.index('<h3>H</h3>'))   # A-Z order
        self.assertIn('James Bradford', home)
        self.assertIn('Thomas Hartley', home)

    def test_home_omits_living_under_standalone(self):
        self._seed_person('p-aaaaaaaaaa', 'Dead Dan', living='false', surname='Dan')
        self._seed_person('p-bbbbbbbbbb', 'Living Larry', living='true', surname='Larry')
        self._run(linked=False)
        home = self._read('index.html')
        self.assertIn('Dead Dan', home)
        self.assertNotIn('Living Larry', home)                   # living person omitted from index
        self.assertNotIn('persons/p-bbbbbbbbbb.html', home)


class StandaloneRedactionAuditTests(_Base):
    """M8.4: no standalone page may link to a person/source page that was not
    generated. Build a mixed archive, then crawl every emitted page for hrefs
    into persons/ and sources/ and assert each target exists on disk."""

    def test_no_dangling_links_to_redacted_pages(self):
        import re as _re
        self._seed_person('p-aaaaaaaaaa', 'Dead Dan', living='false',
                          body='# Dan\n## Biography\nKnew [P-bbbbbbbbbb]; see [S-2222222222] and [L-1111111111].\n')
        self._seed_person('p-bbbbbbbbbb', 'Living Larry', living='true')
        self._seed_source('s-1111111111', 'Public Source', people=('p-aaaaaaaaaa',))
        self._seed_source('s-2222222222', 'Restricted Source', restricted=1, people=('p-aaaaaaaaaa',))
        self.conn.execute(
            'INSERT INTO places(id, name) VALUES (?,?)', ('l-1111111111', 'Fairview'))
        self.conn.execute(
            'INSERT INTO claims(id, source_id, type, value, status, place_id) VALUES (?,?,?,?,?,?)',
            ('c-1111111111', 's-1111111111', 'residence', 'Lived here', 'accepted', 'l-1111111111'))
        self.conn.execute(
            'INSERT INTO claim_persons(claim_id, person_id, position) VALUES (?,?,?)',
            ('c-1111111111', 'p-bbbbbbbbbb', 0))      # a living person on a claim
        self._run(linked=False)
        href_re = _re.compile(r'href="((?:\.\./)?(?:persons|sources)/[a-z0-9-]+\.html)"')
        checked = 0
        for page in self.out_dir.rglob('*.html'):
            text = page.read_text(encoding='utf-8')
            for m in href_re.finditer(text):
                target = (page.parent / m.group(1)).resolve()
                self.assertTrue(target.exists(),
                                f'{page.name} links to missing page {m.group(1)}')
                checked += 1
        self.assertGreater(checked, 0)                # the crawl actually found links
        # The redacted source/person pages must not exist at all.
        self.assertFalse((self.out_dir / 'sources' / 's-2222222222.html').exists())
        self.assertFalse((self.out_dir / 'persons' / 'p-bbbbbbbbbb.html').exists())
        # The tree JSON artifacts are another surface: no node url may point at a
        # person page that wasn't generated, and no living person may be named.
        for data in self.out_dir.glob('data/*.json'):
            tree = json.loads(data.read_text(encoding='utf-8'))
            for n in tree['nodes']:
                if n['url']:
                    self.assertTrue((self.out_dir / 'persons' / Path(n['url']).name).exists(),
                                    f'{data.name} node url -> missing page {n["url"]}')


class TreeTests(_Base):
    """M8.5: interactive trees - vendored renderer + adapter, build-time neutral
    tree JSON (descendants from the root person's apex on the home page, ancestor
    pedigree per curated person), redaction baked into the JSON."""

    def _seed_rels_chain(self):
        # Grandparent -> parent -> child (root_person). Edges both directions,
        # matching index.py's derivation (X parent Y = Y is X's parent;
        # X child Y = Y is X's child).
        self._seed_person('p-aaaaaaaaaa', 'Child Carl', surname='Carl')
        self._seed_person('p-bbbbbbbbbb', 'Parent Pat', surname='Pat')
        self._seed_person('p-cccccccccc', 'Grandparent Gus', surname='Gus')
        for child, parent in (('p-aaaaaaaaaa', 'p-bbbbbbbbbb'), ('p-bbbbbbbbbb', 'p-cccccccccc')):
            self.conn.execute(
                'INSERT INTO relationships(person_id, rel, other_id, claim_id) VALUES (?,?,?,?)',
                (child, 'parent', parent, 'c-1111111111'))
            self.conn.execute(
                'INSERT INTO relationships(person_id, rel, other_id, claim_id) VALUES (?,?,?,?)',
                (parent, 'child', child, 'c-1111111111'))
        (self.archive_root / 'fha.yaml').write_text(
            'roots: {}\nroot_person: P-aaaaaaaaaa\n', encoding='utf-8')

    def test_vendor_copied_and_offline(self):
        self._seed_person('p-aaaaaaaaaa', 'Solo')
        self._run(linked=True)
        self.assertTrue((self.out_dir / 'vendor' / 'fha-tree.js').exists())
        self.assertTrue((self.out_dir / 'vendor' / 'tree-adapter.js').exists())
        # No CDN / remote-loading references in the vendored bundle. The SVG
        # namespace URI (http://www.w3.org/2000/svg) is a required constant, not
        # a network fetch, so it is excluded before the check.
        for js in (self.out_dir / 'vendor').glob('*.js'):
            text = js.read_text(encoding='utf-8').replace('http://www.w3.org/2000/svg', '')
            self.assertNotIn('http://', text)
            self.assertNotIn('https://', text)

    def test_home_descendant_tree_from_apex(self):
        self._seed_rels_chain()
        self._run(linked=True)
        # Data artifact written for the apex (grandparent) in descendants mode.
        data = self.out_dir / 'data' / 'tree_p-cccccccccc_descendants.json'
        self.assertTrue(data.exists())
        tree = json.loads(data.read_text(encoding='utf-8'))
        self.assertEqual(tree['seed'], 'P-cccccccccc')
        self.assertEqual(tree['mode'], 'descendants')
        ids = {n['p_id'] for n in tree['nodes']}
        self.assertEqual(ids, {'P-aaaaaaaaaa', 'P-bbbbbbbbbb', 'P-cccccccccc'})  # whole line
        # Home page embeds the tree data + includes both vendor scripts.
        home = self._read('index.html')
        self.assertIn('fha-tree-data', home)
        self.assertIn('vendor/fha-tree.js', home)
        self.assertIn('vendor/tree-adapter.js', home)

    def test_person_ancestor_pedigree(self):
        self._seed_rels_chain()
        self._run(linked=True)
        page = self._read('persons/p-aaaaaaaaaa.html')
        # The person page now carries a static horizontal pedigree SVG (subject +
        # parents + grandparents), not the interactive descendant renderer.
        self.assertIn('class="pedigree"', page)
        for name in ('Child Carl', 'Parent Pat', 'Grandparent Gus'):   # 3 generations
            self.assertIn(name, page)
        self.assertNotIn('fha-tree-data', page)                        # no interactive tree here
        self.assertFalse((self.out_dir / 'data' / 'tree_p-aaaaaaaaaa_ancestors.json').exists())

    def test_tree_redacts_living_and_links_only_existing_pages(self):
        self._seed_rels_chain()
        # Make the grandparent (apex) living → must be "Living Person", no url.
        self.conn.execute("UPDATE persons SET living='true' WHERE id='p-cccccccccc'")
        self._run(linked=False)
        tree = json.loads(
            (self.out_dir / 'data' / 'tree_p-cccccccccc_descendants.json').read_text(encoding='utf-8'))
        by_id = {n['p_id']: n for n in tree['nodes']}
        self.assertEqual(by_id['P-cccccccccc']['name'], site._LIVING_LABEL)   # living apex redacted
        self.assertIsNone(by_id['P-cccccccccc']['url'])
        # Every node url that is set must point to a generated person page.
        for n in tree['nodes']:
            if n['url']:
                self.assertTrue((self.out_dir / 'persons' / Path(n['url']).name).exists())

    def test_no_tree_without_root_person(self):
        self._seed_person('p-aaaaaaaaaa', 'Solo')   # no fha.yaml root_person, no edges
        self._run(linked=True)
        self.assertNotIn('fha-tree-data', self._read('index.html'))

    def test_home_tree_bounds_initial_paint(self):
        # P2-3: the home descendant explorer passes a bounded initialDepth to the
        # renderer. The per-person page now shows a static pedigree (no interactive
        # renderer), so it carries no initialDepth.
        self._seed_rels_chain()
        self._run(linked=True)
        self.assertIn('initialDepth: 4', self._read('index.html'))
        person = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('initialDepth', person)
        self.assertIn('class="pedigree"', person)

    def test_relationship_cycle_terminates(self):
        # A cousin-marriage style cycle must not loop forever; the BFS visited
        # set bounds it and the node set is deduplicated. Exercised now via the
        # home descendant tree (the only interactive tree that remains).
        self._seed_person('p-aaaaaaaaaa', 'A')
        self._seed_person('p-bbbbbbbbbb', 'B')
        for a, b in (('p-aaaaaaaaaa', 'p-bbbbbbbbbb'), ('p-bbbbbbbbbb', 'p-aaaaaaaaaa')):
            self.conn.execute(
                'INSERT INTO relationships(person_id, rel, other_id, claim_id) VALUES (?,?,?,?)',
                (a, 'parent', b, 'c-1111111111'))
            self.conn.execute(
                'INSERT INTO relationships(person_id, rel, other_id, claim_id) VALUES (?,?,?,?)',
                (a, 'child', b, 'c-1111111111'))
        (self.archive_root / 'fha.yaml').write_text(
            'roots: {}\nroot_person: P-aaaaaaaaaa\n', encoding='utf-8')
        res = self._run(linked=True)
        self.assertEqual(res['status'], 'ok')                          # terminates
        artifacts = list((self.out_dir / 'data').glob('tree_*_descendants.json'))
        self.assertTrue(artifacts)
        ids = [n['p_id'] for n in json.loads(artifacts[0].read_text(encoding='utf-8'))['nodes']]
        self.assertEqual(sorted(set(ids)), ['P-aaaaaaaaaa', 'P-bbbbbbbbbb'])
        self.assertEqual(len(ids), len(set(ids)))                      # each node once

    def test_mistyped_root_person_warns(self):
        self._seed_person('p-aaaaaaaaaa', 'Real Person')
        (self.archive_root / 'fha.yaml').write_text(
            'roots: {}\nroot_person: P-zzzzzzzzzz\n', encoding='utf-8')   # not in index
        res = self._run(linked=True)
        self.assertTrue(any('root_person' in m and 'not in the index' in m for m in res['messages']))
        self.assertNotIn('fha-tree-data', self._read('index.html'))


class DraftExclusionTests(_Base):
    """Unaccepted `<!-- AI-DRAFT ... -->` prose (AGENTS.md: draft prose stays
    inside its markers until the human accepts it via `fha confirm draft`)
    must never publish, and no AI marker may surface as visible page text."""

    def test_draft_block_excluded_human_prose_after_marker_kept(self):
        body = ('# Thomas\n## Biography\n'
                'Drafted census claim [S-1111111111].\n\n'
                'Second drafted paragraph.\n\n'
                '<!-- AI-DRAFT 2026-07-01 claude-x - drafted from census -->\n\n'
                'A human-written paragraph that stays.\n')
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley', body=body)
        self._seed_source('s-1111111111', 'Census', people=('p-aaaaaaaaaa',))
        self._run(linked=False)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('A human-written paragraph that stays.', html)
        self.assertNotIn('Drafted census claim', html)
        self.assertNotIn('Second drafted paragraph', html)
        self.assertNotIn('AI-DRAFT', html)

    def test_accepted_prose_published_marker_invisible(self):
        body = ('# T\n## Biography\n'
                'An accepted paragraph of biography.\n\n'
                '<!-- AI-ACCEPTED 2026-06-01 claude-x - drafted (accepted 2026-06-20) -->\n')
        self._seed_person('p-aaaaaaaaaa', 'Thomas', body=body)
        self._run(linked=False)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('An accepted paragraph of biography.', html)
        self.assertNotIn('AI-ACCEPTED', html)

    def test_extend_flow_accepted_kept_new_draft_excluded(self):
        # The write-biography extend flow: an accepted block, then a fresh
        # draft appended below it. The accepted marker bounds the new block.
        body = ('# T\n## Biography\n'
                'The accepted early-life paragraph.\n\n'
                '<!-- AI-ACCEPTED 2026-06-01 claude-x - v1 (accepted 2026-06-20) -->\n\n'
                'A new unreviewed paragraph.\n\n'
                '<!-- AI-DRAFT 2026-07-01 claude-x - v2 -->\n')
        self._seed_person('p-aaaaaaaaaa', 'Thomas', body=body)
        self._run(linked=False)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('The accepted early-life paragraph.', html)
        self.assertNotIn('A new unreviewed paragraph.', html)
        self.assertNotIn('AI-DRAFT', html)
        self.assertNotIn('AI-ACCEPTED', html)

    def test_all_draft_biography_renders_like_no_biography(self):
        body = ('# T\n## Biography\n'
                'Entirely drafted paragraph.\n\n'
                '<!-- AI-DRAFT 2026-07-01 claude-x - note -->\n\n'
                '## Stories\nA human tale.\n')
        self._seed_person('p-aaaaaaaaaa', 'Thomas', body=body)
        res = self._run(linked=False)
        self.assertEqual(res['status'], 'ok')
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('<h2>Biography</h2>', html)     # no stray heading
        self.assertNotIn('Entirely drafted', html)
        self.assertIn('<h2>Stories</h2>', html)
        self.assertIn('A human tale.', html)

    def test_unmarked_prose_directly_above_draft_withheld_failsafe(self):
        # The block START is not syntactically encoded, so prose sitting
        # directly above a draft run (no marker/heading between) cannot be
        # told apart from the draft. It is withheld too - fail-closed is the
        # only safe direction for a publication path; it returns on accept.
        body = ('# T\n## Biography\n'
                'Older unmarked paragraph.\n\n'
                'Drafted paragraph.\n\n'
                '<!-- AI-DRAFT 2026-07-01 claude-x - note -->\n')
        self._seed_person('p-aaaaaaaaaa', 'Thomas', body=body)
        res = self._run(linked=False)
        self.assertEqual(res['status'], 'ok')
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('Older unmarked paragraph.', html)
        self.assertNotIn('Drafted paragraph.', html)

    def test_stories_draft_excluded(self):
        body = ('# T\n## Biography\nHuman bio.\n\n'
                '## Stories\nA drafted tale.\n\n'
                '<!-- AI-DRAFT 2026-07-01 claude-x - story -->\n')
        self._seed_person('p-aaaaaaaaaa', 'Thomas', body=body)
        self._run(linked=False)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Human bio.', html)
        self.assertNotIn('A drafted tale.', html)
        self.assertNotIn('<h2>Stories</h2>', html)       # emptied section skipped

    def test_linked_mode_also_excludes_drafts(self):
        # The dev preview skips privacy redaction, but a draft is not privacy
        # material - it is not-yet-content, and the marker would render as
        # escaped junk. Both modes exclude it.
        body = ('# T\n## Biography\nDrafted paragraph.\n\n'
                '<!-- AI-DRAFT 2026-07-01 claude-x - note -->\n')
        self._seed_person('p-aaaaaaaaaa', 'Thomas', body=body)
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('Drafted paragraph.', html)
        self.assertNotIn('AI-DRAFT', html)

    # The marker-grammar unit tests moved to tests/test_lib_text.py with the
    # function itself (site consumes _lib.strip_unaccepted_drafts now); the
    # tests below cover site's own half of the contract - what a damaged
    # marker does to the built page.

    def test_damaged_marker_withholds_prose_and_warns(self):
        # X1 fail-closed: an unterminated marker means draft and accepted
        # prose can no longer be told apart. The page still builds, but its
        # whole prose surface is withheld, and one warning names the file
        # and the fix. The old behavior published the draft + the dangling
        # marker into the standalone site.
        body = ('# T\n## Biography\n'
                'Human paragraph.\n\n'
                'Drafted paragraph.\n\n'
                '<!-- AI-DRAFT 2026-07-01 claude-x - note missing its arrow\n\n'
                '## Stories\nA human tale.\n')
        self._seed_person('p-aaaaaaaaaa', 'Thomas Hartley', body=body)
        res = self._run(linked=False)
        self.assertEqual(res['status'], 'ok')            # build completes
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('Drafted paragraph.', html)     # the leak, closed
        self.assertNotIn('AI-DRAFT', html)
        self.assertNotIn('Human paragraph.', html)       # withheld entirely
        self.assertNotIn('A human tale.', html)          # both sections
        warnings = [m for m in res['messages'] if 'damaged' in m]
        self.assertEqual(len(warnings), 1)               # one warning, not two
        self.assertIn('draft marker', warnings[0])
        self.assertIn('people/', warnings[0])            # names the file
        self.assertIn('rebuild', warnings[0])            # names the fix

    def test_wrap_style_marker_withholds_not_leaks(self):
        # Wrap-style authoring (marker above + /AI-DRAFT below) used to cut
        # the HUMAN text above and publish the draft below it. Fail closed.
        body = ('# T\n## Biography\n'
                'Human paragraph above.\n\n'
                '<!-- AI-DRAFT 2026-07-01 claude-x - wrap -->\n'
                'Wrapped draft paragraph.\n'
                '<!-- /AI-DRAFT -->\n')
        self._seed_person('p-aaaaaaaaaa', 'Thomas', body=body)
        res = self._run(linked=False)
        self.assertEqual(res['status'], 'ok')
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertNotIn('Wrapped draft paragraph.', html)
        self.assertNotIn('AI-DRAFT', html)
        self.assertTrue(any('damaged' in m for m in res['messages']))


class LinkSchemeTests(unittest.TestCase):
    """Markdown-link URLs allowlist http/https/mailto; a javascript:/data:
    (or any other scheme-bearing) URL renders its label as plain text - the
    stored-XSS guard for prose published by the site."""

    def _html(self, prose):
        return site._prose_to_html(prose, lambda t, d=None: t)

    def test_javascript_uri_not_linked(self):
        out = self._html('[click](javascript:alert%281%29)')
        self.assertNotIn('<a ', out)
        self.assertNotIn('javascript', out)
        self.assertIn('click', out)

    def test_uppercase_scheme_not_linked(self):
        out = self._html('[x](JAVASCRIPT:alert%281%29)')
        self.assertNotIn('<a ', out)

    def test_data_uri_not_linked(self):
        out = self._html('[x](data:text/html,hello)')
        self.assertNotIn('<a ', out)
        self.assertNotIn('data:', out)

    def test_https_still_links(self):
        out = self._html('[site](https://example.org/page)')
        self.assertIn('<a href="https://example.org/page">site</a>', out)

    def test_mailto_still_links(self):
        out = self._html('[mail](mailto:a@b.example)')
        self.assertIn('<a href="mailto:a@b.example">mail</a>', out)

    def test_relative_url_still_links(self):
        out = self._html('[p](sub/page.html)')
        self.assertIn('<a href="sub/page.html">p</a>', out)

    def test_colon_after_first_slash_is_relative(self):
        out = self._html('[p](files/a:b.html)')
        self.assertIn('<a href="files/a:b.html">p</a>', out)

    def test_helper_scheme_matrix(self):
        self.assertIsNone(site._safe_link_href('javascript:x'))
        self.assertIsNone(site._safe_link_href('data:text/html,x'))
        self.assertIsNone(site._safe_link_href('vbscript:x'))
        self.assertIsNone(site._safe_link_href('file:///etc/passwd'))
        self.assertEqual(site._safe_link_href('HTTPS://X'), 'HTTPS://X')
        self.assertEqual(site._safe_link_href('#top'), '#top')


class OutputGuardTests(_Base):
    """`_reset_output` clears generically named subtrees, so a rebuild must
    first prove the --out folder is fha site's own: the `.fha-site` marker
    (stamped by every successful build), an empty/new folder, or the
    pre-marker legacy shape (index.html + vendor/fha-tree.js)."""

    def _run_to(self, out, *, dry_run=False):
        self.conn.commit()
        future = time.time() + 5
        os.utime(self.archive_root / '.cache' / 'index.sqlite', (future, future))
        return site.run_site(self.archive_root, out, linked=True, dry_run=dry_run)

    def test_refuses_nonempty_unowned_out_dir(self):
        self._seed_person('p-aaaaaaaaaa', 'Jane')
        out = self.archive_root / 'exports'
        (out / 'sources').mkdir(parents=True)            # shares a site subtree name
        (out / 'sources' / 'precious.txt').write_text('mine', encoding='utf-8')
        (out / 'notes.txt').write_text('also mine', encoding='utf-8')
        res = self._run_to(out)
        self.assertEqual(res['status'], 'bad-output')
        self.assertTrue(any("wasn't created by fha site" in m for m in res['messages']))
        # Nothing was deleted and nothing was built.
        self.assertEqual((out / 'sources' / 'precious.txt').read_text(encoding='utf-8'), 'mine')
        self.assertTrue((out / 'notes.txt').exists())
        self.assertFalse((out / 'index.html').exists())

    def test_empty_dir_builds_and_gains_marker(self):
        self._seed_person('p-aaaaaaaaaa', 'Jane')
        out = self.archive_root / 'exports'
        out.mkdir()
        res = self._run_to(out)
        self.assertEqual(res['status'], 'ok')
        self.assertTrue((out / '.fha-site').exists())

    def test_marked_dir_rebuilds(self):
        self._seed_person('p-aaaaaaaaaa', 'Jane')
        self.assertEqual(self._run_to(self.out_dir)['status'], 'ok')
        self.assertTrue((self.out_dir / '.fha-site').exists())
        self.assertEqual(self._run_to(self.out_dir)['status'], 'ok')

    def test_legacy_prior_build_without_marker_rebuilds(self):
        # A site built before the marker shipped has index.html +
        # vendor/fha-tree.js but no .fha-site; it is accepted and gains the
        # marker on the rebuild (documented back-compat).
        self._seed_person('p-aaaaaaaaaa', 'Jane')
        self.assertEqual(self._run_to(self.out_dir)['status'], 'ok')
        (self.out_dir / '.fha-site').unlink()            # simulate the pre-marker build
        res = self._run_to(self.out_dir)
        self.assertEqual(res['status'], 'ok')
        self.assertTrue((self.out_dir / '.fha-site').exists())

    def test_interrupted_first_build_does_not_lock_output_dir(self):
        # X3 (round-2 finding 13): a crash/Ctrl-C after the reset but before
        # index.html used to leave a non-empty folder with no marker and no
        # index.html - the next run refused it as "wasn't created by fha
        # site" with no way out. The marker is now stamped the moment
        # _reset_output succeeds (the tool owns the dir it just cleared), so
        # the rerun simply rebuilds.
        self._seed_person('p-aaaaaaaaaa', 'Jane')
        out = self.archive_root / 'exports'
        original = site._SiteBuilder.build_index_page

        def _boom(builder_self):
            raise RuntimeError('simulated mid-build crash')

        site._SiteBuilder.build_index_page = _boom
        try:
            with self.assertRaises(RuntimeError):
                self._run_to(out)
        finally:
            site._SiteBuilder.build_index_page = original
        # The interrupted build left the poison shape: files present (vendor
        # was copied), but no index.html - and, now, the ownership marker.
        self.assertTrue((out / 'vendor' / 'fha-tree.js').exists())
        self.assertFalse((out / 'index.html').exists())
        self.assertTrue((out / '.fha-site').exists())
        res = self._run_to(out)
        self.assertEqual(res['status'], 'ok')            # rebuilt, not refused
        self.assertTrue((out / 'index.html').exists())

    def test_dry_run_lists_would_remove_and_deletes_nothing(self):
        self._seed_person('p-aaaaaaaaaa', 'Jane')
        self.assertEqual(self._run_to(self.out_dir)['status'], 'ok')
        before = sorted(str(p) for p in self.out_dir.rglob('*'))
        res = self._run_to(self.out_dir, dry_run=True)
        self.assertEqual(res['status'], 'dry-run')
        preview = res['reset_preview']
        self.assertIn('index.html', preview)
        self.assertIn('persons/', preview)
        self.assertEqual(before, sorted(str(p) for p in self.out_dir.rglob('*')))

    def test_dry_run_fresh_dir_has_empty_preview(self):
        self._seed_person('p-aaaaaaaaaa', 'Jane')
        res = self._run_to(self.out_dir, dry_run=True)
        self.assertEqual(res['status'], 'dry-run')
        self.assertEqual(res['reset_preview'], [])
        self.assertFalse(self.out_dir.exists())


class ProseConverterTests(unittest.TestCase):
    def test_headings_bold_lists_links(self):
        ident = lambda t: f'TOK({t})'  # noqa: E731
        out = site._prose_to_html(
            '## A Heading\n\nParagraph with **bold** and a [label](http://x).\n\n- one\n- two\n',
            ident)
        self.assertIn('<h3>A Heading</h3>', out)
        self.assertIn('<strong>bold</strong>', out)
        self.assertIn('<a href="http://x">label</a>', out)
        self.assertIn('<ul><li>one</li><li>two</li></ul>', out)

    def test_html_in_prose_is_escaped(self):
        out = site._prose_to_html('A <script>alert(1)</script> line.', lambda t: t)
        self.assertNotIn('<script>', out)
        self.assertIn('&lt;script&gt;', out)

    def test_token_delegated_to_renderer(self):
        out = site._prose_to_html('Born in [S-1111111111] year.', lambda t: f'<a>{t}</a>')
        self.assertIn('<a>S-1111111111</a>', out)


if __name__ == '__main__':
    unittest.main()
