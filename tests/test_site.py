"""
test_site.py — fha site (M8.1 source page + M8.2 person page).

Builds a synthetic .cache/index.sqlite (and, where needed, .cache/photos.sqlite)
directly from index.py's / photoindex.py's DDL — the same pattern as
tests/test_packet.py — so the publication generator can be exercised without a
full archive fixture, exiftool, or a network. The prose/citation that `fha site`
reads from the record .md files is written to disk alongside the index rows.

`site.py`'s module stem collides with Python's stdlib `site`, so it is loaded by
path under the private name `fha_site` (the same trick fha.py uses).
"""

import importlib.util
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

    # — seeding —

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
        self.assertIn('../sources/s-1111111111.html', html)      # [S-id] in prose -> source link
        self.assertIn('Margaret Cole', html)                     # [P-id] -> name (stub, no link)
        self.assertIn('<mark>[S-9999999999]</mark>', html)       # unresolved token -> mark

    def test_timeline_excludes_suggested_includes_needs_review(self):
        self._setup_thomas()
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Lived in Fairview', html)                 # needs-review present
        self.assertNotIn('Bookkeeper (unreviewed)', html)        # suggested excluded from timeline
        self.assertIn('1840s', html)                             # decade grouping
        self.assertIn('1880s', html)

    def test_family_and_sources_grouped(self):
        self._setup_thomas()
        self._run(linked=True)
        html = self._read('persons/p-aaaaaaaaaa.html')
        self.assertIn('Spouses', html)
        self.assertIn('Margaret Cole', html)
        self.assertIn('<h3>census</h3>', html)                   # sources grouped by type
        self.assertIn('<h3>vital-record</h3>', html)


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

    def test_no_index_status(self):
        # Remove the index file entirely.
        self.conn.close()
        (self.archive_root / '.cache' / 'index.sqlite').unlink()
        res = site.run_site(self.archive_root, self.out_dir, linked=True)
        self.assertEqual(res['status'], 'no-index')
        # reopen so tearDown's close() doesn't error
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
