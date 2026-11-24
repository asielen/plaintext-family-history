"""
A child's birth stays on the shared site when her godmother is still alive,
and a census stays on a dead grandfather's page when his grandson in it is
alive.

Owner decisions 2026-11-22 and 2026-11-23 (SPEC §21): a claim that names a
living or unknown person is kept unless it is that person's own event (his
birth, his death, his marriage), a `relationship` claim naming him, or a
fact about nobody but living people. Kept, the living person is left out of
it (not named, linked or labelled) and so is the claim's own wording, which
usually names everyone on the record. Its source keeps its page and its
citation: the page may show the record's own words, which can print the
living person's name, but never lists, links or labels him. A claim naming
a restricted person (by-request included) is withheld whole, since
by-request is honored by every export path (SPEC §19).

The rule lives once, in `_lib.claim_people_verdict`; the site, GEDCOM and
WikiTree read it. Expected answers below come from those SPEC rules.

Site tests build a synthetic .cache/index.sqlite from index.py's DDL, as
tests/test_site_rings_calendar.py does. GEDCOM and WikiTree tests index real
record files. Temp fixtures only; example-archive style names.
"""

import importlib.util
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import gedcom  # noqa: E402
import index  # noqa: E402
import wikitree  # noqa: E402
from index import _DDL as INDEX_DDL  # noqa: E402
from _lib import (  # noqa: E402
    CLAIM_SHOWN, CLAIM_UNWORDED, CLAIM_WITHHELD, claim_people_verdict,
    claim_subjects, index_manifest_path, record_path_manifest, write_path_manifest,
)

_spec = importlib.util.spec_from_file_location('fha_site_mixed_living', ROOT / 'tools' / 'site.py')
site = importlib.util.module_from_spec(_spec)
sys.modules['fha_site_mixed_living'] = site
_spec.loader.exec_module(site)

THOMAS = 'p-aaaaaaaaaa'    # deceased: born 1885, baptism names a living godparent
WREN = 'p-bbbbbbbbbb'      # living: the godparent and marriage witness
CALVIN = 'p-cccccccccc'    # deceased
ETHEL = 'p-dddddddddd'     # deceased, Calvin's wife
HESTER = 'p-eeeeeeeeee'    # deceased, restricted: by-request
PIP = 'p-ffffffffff'       # living: Thomas's son, born 1931

RIVERSIDE = 'l-aaaaaaaaaa'

# Everything about Wren that must never reach the shared site.
WREN_MARKS = ('Wren', 'Ashcombe', WREN, '1960')
# Pip's own birth (date and place) rests on a record that names his dead father.
PIP_MARKS = ('Pip', PIP, '1931', 'Millbrook')


class VerdictTests(unittest.TestCase):
    """`_lib.claim_people_verdict`, the one rule every shared copy reads."""

    def verdict(self, ctype, pairs, living=(), restricted=()):
        return claim_people_verdict(ctype, pairs, withheld=lambda p: p in living,
                                    restricted=lambda p: p in restricted)

    def test_a_birth_naming_a_living_godparent_is_kept_without_its_wording(self):
        pairs = [(THOMAS, 'child'), (WREN, 'godparent')]
        self.assertEqual(self.verdict('birth', pairs, living={WREN}), CLAIM_UNWORDED)

    def test_a_marriage_of_two_dead_people_with_a_living_witness_is_kept(self):
        pairs = [(CALVIN, 'spouse'), (ETHEL, 'spouse'), (WREN, 'witness')]
        self.assertEqual(self.verdict('marriage', pairs, living={WREN}), CLAIM_UNWORDED)

    def test_a_claim_naming_only_a_living_person_is_withheld(self):
        self.assertEqual(self.verdict('birth', [(WREN, None)], living={WREN}), CLAIM_WITHHELD)

    def test_a_living_sons_birth_naming_his_dead_father_is_withheld(self):
        # Its date and place are the son's own birthday.
        pairs = [(PIP, 'child'), (THOMAS, 'parent')]
        self.assertEqual(self.verdict('birth', pairs, living={PIP}), CLAIM_WITHHELD)

    def test_a_census_of_a_dead_head_with_a_living_household_member_is_kept(self):
        # The census may be the heart of his story (owner, 2026-11-23).
        pairs = [(CALVIN, 'head'), (WREN, 'household_member')]
        self.assertEqual(self.verdict('census', pairs, living={WREN}), CLAIM_UNWORDED)
        pairs = [(CALVIN, 'head'), (WREN, 'child')]
        self.assertEqual(self.verdict('census', pairs, living={WREN}), CLAIM_UNWORDED)

    def test_a_fact_about_nobody_but_living_people_is_withheld(self):
        pairs = [(PIP, 'head'), (WREN, 'household_member'), (THOMAS, 'parent')]
        self.assertEqual(self.verdict('census', pairs, living={PIP, WREN}), CLAIM_WITHHELD)
        pairs = [(PIP, 'subject'), (THOMAS, 'parent')]
        self.assertEqual(self.verdict('residence', pairs, living={PIP}), CLAIM_WITHHELD)

    def test_a_residence_shared_by_a_dead_and_a_living_subject_is_kept(self):
        pairs = [(CALVIN, 'subject'), (PIP, 'subject')]
        self.assertEqual(self.verdict('residence', pairs, living={PIP}), CLAIM_UNWORDED)

    def test_a_marriage_with_a_living_spouse_is_withheld(self):
        # The date is the living spouse's own wedding day.
        pairs = [(THOMAS, 'spouse'), (WREN, 'spouse')]
        self.assertEqual(self.verdict('marriage', pairs, living={WREN}), CLAIM_WITHHELD)

    def test_a_living_twin_with_no_role_or_a_misspelt_one_keeps_the_birth_home(self):
        # Unroled or `chlid:`, he may be the one born that day: fail closed.
        for role in (None, 'chlid', 'children'):
            pairs = [(THOMAS, 'child'), (PIP, role)]
            self.assertEqual(self.verdict('birth', pairs, living={PIP}), CLAIM_WITHHELD, role)
        pairs = [(CALVIN, 'deceased'), (PIP, None)]
        self.assertEqual(self.verdict('death', pairs, living={PIP}), CLAIM_WITHHELD)

    def test_a_claim_that_does_not_say_whose_it_is_counts_everyone(self):
        pairs = [(THOMAS, None), (WREN, None)]
        self.assertEqual(self.verdict('birth', pairs, living={WREN}), CLAIM_WITHHELD)
        self.assertEqual(claim_subjects('residence', pairs), [THOMAS, WREN])

    def test_a_relationship_claim_is_about_every_party(self):
        pairs = [(PIP, 'child'), (THOMAS, 'parent')]
        self.assertEqual(claim_subjects('relationship', pairs), [PIP, THOMAS])
        self.assertEqual(self.verdict('relationship', pairs, living={PIP}), CLAIM_WITHHELD)

    def test_a_dead_and_a_by_request_person_is_withheld(self):
        pairs = [(CALVIN, 'deceased'), (HESTER, 'informant')]
        self.assertEqual(self.verdict('death', pairs, restricted={HESTER}), CLAIM_WITHHELD)

    def test_nobody_withheld_is_shown(self):
        pairs = [(CALVIN, 'spouse'), (ETHEL, 'spouse')]
        self.assertEqual(self.verdict('marriage', pairs), CLAIM_SHOWN)
        self.assertEqual(self.verdict('note', []), CLAIM_SHOWN)


class _SiteBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        cache = self.root / '.cache'
        cache.mkdir(parents=True)
        self.conn = sqlite3.connect(str(cache / 'index.sqlite'))
        self.conn.executescript(INDEX_DDL)
        self.conn.row_factory = sqlite3.Row
        self.out = self.root / '.cache' / 'site'
        (self.root / 'fha.yaml').write_text(
            'roots: {}\nsite:\n  completeness_rings: true\n  calendar: true\n',
            encoding='utf-8')

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def person(self, pid, name, *, living='false', extra=''):
        rel = f'people/{name.split()[-1].lower()}_{pid}.md'
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'---\nid: {pid}\nname: {name}\n{extra}---\n# {name}\n',
                        encoding='utf-8')
        self.conn.execute(
            'INSERT INTO persons(id, name, surname, sex, living, tier, status, path) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (pid, name, name.split()[-1], 'F', living, 'curated', 'active', rel))

    def source(self, sid, title):
        rel = f'sources/vital-record/src_{sid}.md'
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'---\nid: {sid}\ntitle: {title}\nsource_type: vital-record\n'
                        'citation: "A citation."\n---\n\n## Claims\n```yaml\n```\n',
                        encoding='utf-8')
        self.conn.execute(
            'INSERT INTO sources(id, title, source_type, restricted, status, path) '
            'VALUES (?,?,?,?,?,?)', (sid, title, 'vital-record', 0, 'active', rel))

    def claim(self, cid, sid, ctype, date, roles, value, *, place_id=None):
        lo = (date + '-01-01')[:10] if len(date) == 4 else date[:10]
        self.conn.execute(
            'INSERT INTO claims(id, source_id, type, value, status, date_edtf, date_min, '
            'date_max, place_id, place_text, negated) VALUES (?,?,?,?,?,?,?,?,?,?,0)',
            (cid, sid, ctype, value, 'accepted', date, lo, lo, place_id, None))
        for pos, (pid, role) in enumerate(roles):
            self.conn.execute(
                'INSERT INTO claim_persons(claim_id, person_id, position, role) '
                'VALUES (?,?,?,?)', (cid, pid, pos, role))

    def build(self, *, linked=False):
        self.conn.commit()
        future = time.time() + 5
        os.utime(self.root / '.cache' / 'index.sqlite', (future, future))
        write_path_manifest(index_manifest_path(self.root), record_path_manifest(self.root))
        with unittest.mock.patch.object(site, '_today', return_value='2026-04-15'):
            return site.run_site(self.root, self.out, linked=linked)

    def page(self, rel):
        return (self.out / rel).read_text(encoding='utf-8')

    def every_page_text(self):
        """Every page and data file the build wrote, the vendored libraries
        aside (minified third-party code is full of stray digits)."""
        out = {}
        for path in self.out.rglob('*'):
            if path.is_file() and path.suffix in ('.html', '.json', '.js') \
                    and 'vendor' not in path.parts:
                out[path.relative_to(self.out).as_posix()] = path.read_text(encoding='utf-8')
        return out

    def assert_nowhere(self, marks):
        for rel, text in self.every_page_text().items():
            for mark in marks:
                self.assertNotIn(mark.lower(), text.lower(), f'{mark!r} found in {rel}')


class SharedSiteTests(_SiteBase):
    def seed(self):
        self.conn.execute('INSERT INTO places(id, name, hierarchy) VALUES (?,?,?)',
                          (RIVERSIDE, 'Riverside', 'Riverside, Kansas'))
        self.person(THOMAS, 'Thomas Hartley')
        self.person(WREN, 'Wren Ashcombe', living='true')
        self.person(CALVIN, 'Calvin Hartley')
        self.person(ETHEL, 'Ethel Moore')
        self.person(PIP, 'Pip Hartley', living='true')
        self.person(HESTER, 'Hester Quill', extra='restricted: by-request\n')
        self.source('s-1111111111', 'Riverside baptism register')
        self.claim('c-1111111111', 's-1111111111', 'birth', '1885-04-02',
                   [(THOMAS, 'child'), (WREN, 'godparent')],
                   'Thomas Hartley born, godparent Wren Ashcombe', place_id=RIVERSIDE)
        self.source('s-2222222222', 'Riverside marriage register')
        self.claim('c-2222222222', 's-2222222222', 'marriage', '1910-06-01',
                   [(CALVIN, 'spouse'), (ETHEL, 'spouse'), (WREN, 'witness')],
                   'Calvin Hartley married Ethel Moore, witness Wren Ashcombe',
                   place_id=RIVERSIDE)
        self.source('s-3333333333', 'Wren Ashcombe birth certificate')
        self.claim('c-3333333333', 's-3333333333', 'birth', '1960-03-03', [(WREN, None)],
                   'Wren Ashcombe born')
        self.source('s-4444444444', 'Pip Hartley birth certificate')
        self.claim('c-4444444444', 's-4444444444', 'birth', '1931-08-08',
                   [(PIP, 'child'), (THOMAS, 'parent')], 'Pip Hartley born in Millbrook')
        self.source('s-5555555555', 'Calvin Hartley death certificate')
        self.claim('c-5555555555', 's-5555555555', 'death', '1950-01-09',
                   [(CALVIN, 'deceased'), (HESTER, 'informant')],
                   'Calvin Hartley died, informant Hester Quill')

    def timeline(self, pid):
        html = self.page(f'persons/{pid}.html')
        return html.split('<h2>Timeline</h2>', 1)[1] if '<h2>Timeline</h2>' in html else html

    def test_a_birth_naming_a_living_godparent_is_shown_without_him(self):
        self.seed()
        self.build()
        html = self.page(f'persons/{THOMAS}.html')
        self.assertIn('1885-04-02', html)
        self.assertIn('Riverside', html)
        self.assertIn(f'<a href="../places/{RIVERSIDE}.html">', html)
        # The record's own wording names him, so it stays home too.
        self.assertNotIn('godparent', html)
        # Its source keeps its page and its citation (owner, 2026-11-23).
        self.assertTrue((self.out / 'sources' / 's-1111111111.html').exists())
        self.assertIn('Riverside baptism register', html)
        self.assertIn('s-1111111111.html', html)

    def test_a_marriage_of_two_dead_people_with_a_living_witness_is_shown(self):
        self.seed()
        self.build()
        for pid in (CALVIN, ETHEL):
            html = self.timeline(pid)
            self.assertIn('1910-06-01', html)
            self.assertIn('Calvin Hartley', html)
            self.assertIn('Ethel Moore', html)
            self.assertNotIn('witness', html)

    def test_the_living_godparent_appears_nowhere_on_the_shared_site(self):
        self.seed()
        self.build()
        self.assert_nowhere(WREN_MARKS)
        self.assertNotIn(site._LIVING_LABEL, self.page(f'places/{RIVERSIDE}.html'))

    def test_a_living_sons_birth_stays_off_his_dead_fathers_page(self):
        self.seed()
        self.build()
        self.assert_nowhere(PIP_MARKS)

    def test_a_birth_shared_with_a_living_twin_appears_nowhere(self):
        # The record is his birth as much as hers: not on her page, and not
        # as the life dates on her card in any family chart.
        self.seed()
        self.person('p-gggggggggg', 'Iris Hartley')
        self.person('p-hhhhhhhhhh', 'Ivo Hartley', living='true')
        for kid in ('p-gggggggggg', 'p-hhhhhhhhhh'):
            self.conn.execute(
                "INSERT INTO relationships(person_id, rel, other_id, claim_id) "
                "VALUES (?, 'parent', ?, NULL)", (kid, THOMAS))
        self.source('s-6666666666', 'Twins birth register')
        self.claim('c-6666666666', 's-6666666666', 'birth', '1921-02-14',
                   [('p-gggggggggg', 'child'), ('p-hhhhhhhhhh', 'child')], 'Twins born')
        self.build()
        self.assert_nowhere(('1921', 'Ivo'))

    def test_a_marriage_with_a_living_spouse_appears_nowhere(self):
        self.seed()
        self.source('s-7777777777', 'Riverside marriage register, 1912')
        self.claim('c-7777777777', 's-7777777777', 'marriage', '1912-05-05',
                   [(THOMAS, 'spouse'), (WREN, 'spouse')], 'Thomas married Wren')
        self.build()
        self.assert_nowhere(('1912', 'Wren'))

    def test_a_confirmed_absence_naming_a_living_witness_is_not_shown_as_an_event(self):
        # "Thomas did not live in Riverside" is told only by its wording;
        # shown without it, his page would say he lived there (review, #465).
        self.seed()
        self.source('s-7171717171', 'Riverside directory, 1902')
        self.claim('c-7171717171', 's-7171717171', 'residence', '1902',
                   [(THOMAS, 'subject'), (WREN, 'witness')],
                   'Thomas did not live in Riverside', place_id=RIVERSIDE)
        self.conn.execute("UPDATE claims SET negated = 1 WHERE id = 'c-7171717171'")
        self.build()
        self.assertNotIn('1902', self.timeline(THOMAS))
        self.assertNotIn('1902', self.page(f'places/{RIVERSIDE}.html'))

    def test_a_living_twin_left_unroled_keeps_the_birth_home(self):
        self.seed()
        self.person('p-gggggggggg', 'Iris Hartley')
        self.person('p-hhhhhhhhhh', 'Ivo Hartley', living='true')
        self.source('s-6666666666', 'Twins birth register')
        self.claim('c-6666666666', 's-6666666666', 'birth', '1921-02-14',
                   [('p-gggggggggg', 'child'), ('p-hhhhhhhhhh', None)], 'Twins born')
        self.build()
        self.assert_nowhere(('1921', 'Ivo'))

    def test_a_record_naming_a_by_request_person_stays_withheld(self):
        self.seed()
        self.build()
        self.assertNotIn('1950', self.page(f'persons/{CALVIN}.html'))
        self.assert_nowhere(('Hester', 'Quill', HESTER, '1950'))

    def test_the_place_page_lists_the_kept_events_without_the_living(self):
        self.seed()
        self.build()
        html = self.page(f'places/{RIVERSIDE}.html')
        events = html.split('<h2>Events here</h2>', 1)[1]
        self.assertIn('1885-04-02', events)
        self.assertIn('1910-06-01', events)
        self.assertIn('Thomas Hartley', events)

    def test_the_calendar_and_ring_count_the_kept_birth(self):
        self.seed()
        self.build()
        self.assertIn('2 April 1885', self.page('index.html'))
        self.assertIn('<g class="ring-met" data-check="birth"',
                      self.page(f'persons/{THOMAS}.html'))

    def test_the_family_tree_gains_no_tie_to_the_godparent(self):
        # A godparent is a descriptive role: keeping the birth adds no edge,
        # so no chart, card or tree file places him beside Thomas.
        self.seed()
        self.build()
        html = self.page(f'persons/{THOMAS}.html')
        self.assertNotIn(site._LIVING_LABEL, html)
        self.assert_nowhere(WREN_MARKS)

    def census(self):
        """The 1940 census of Calvin's household, which lists his living
        grandson Pip; its citation, the record's own words, names Pip."""
        self.seed()
        rel = 'sources/census/src_s-8888888888.md'
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('---\nid: s-8888888888\ntitle: 1940 census, Riverside\n'
                        'source_type: census\ncitation: "1940 census, Riverside, '
                        'household of Calvin Hartley with grandson Pip Hartley."\n---\n\n'
                        '## Claims\n```yaml\n```\n', encoding='utf-8')
        self.conn.execute(
            'INSERT INTO sources(id, title, source_type, restricted, status, path) '
            'VALUES (?,?,?,?,?,?)',
            ('s-8888888888', '1940 census, Riverside', 'census', 0, 'active', rel))
        self.claim('c-8888888888', 's-8888888888', 'census', '1940-04-01',
                   [(CALVIN, 'head'), (PIP, 'household_member')],
                   'Calvin Hartley, head; Pip Hartley, grandson, age 9', place_id=RIVERSIDE)
        self.build()

    def test_a_census_naming_a_living_grandson_stays_on_the_dead_heads_page(self):
        self.census()
        self.assertIn('1940-04-01', self.timeline(CALVIN))
        # Its source is cited by name and linked, not withheld.
        self.assertIn('s-8888888888.html', self.page(f'persons/{CALVIN}.html'))

    def test_the_census_source_page_shows_its_words_but_never_lists_the_living(self):
        # "Showing a living person's name is fine, just don't connect the
        # dots" (owner, 2026-11-23): the citation, the record's own words,
        # may print his name; no claim row, link or label names him.
        self.census()
        page = self.page('sources/s-8888888888.html')
        self.assertIn('grandson Pip Hartley', page)          # the record's own words
        self.assertNotIn(PIP, page)
        self.assertNotIn(site._LIVING_LABEL, page)
        self.assertNotIn('age 9', page)                      # the claim's wording
        self.assertNotIn('household_member', page)
        self.assertIn(f'{CALVIN}.html', page)

    def test_the_living_grandson_is_on_no_page_but_the_census_source(self):
        self.census()
        for rel, text in self.every_page_text().items():
            self.assertNotIn(PIP, text, rel)
            self.assertNotIn('age 9', text, rel)
            if rel != 'sources/s-8888888888.html':
                self.assertNotIn('Pip', text, rel)

    def test_search_never_finds_the_living_grandson_through_the_census_title(self):
        # The census page exists and its title is shown as written (owner,
        # 2026-11-23), but the search box must not find a living person.
        self.census()
        self.conn.execute("UPDATE sources SET title = '1940 census, Calvin and Pip Hartley' "
                          "WHERE id = 's-8888888888'")
        self.build()
        search = self.page('data/search-data.js')
        self.assertNotIn('Pip', search)
        self.assertNotIn('s-8888888888', search)
        self.assertIn('Calvin Hartley', search)
        self.assertTrue((self.out / 'sources' / 's-8888888888.html').exists())

    def test_a_photograph_naming_a_living_grandson_stays_home(self):
        # A snapshot of Grandpa with his living grandson is a picture of the
        # grandson, not a record printing his name: SPEC §21 withholds the
        # photos naming him, kept claim or not, catalog or not. A framed
        # print filed as an `artifact`, or a type lint does not know
        # (`photograph`), is held back the same way.
        for stype in ('photo', 'Photo', 'artifact', 'photograph'):
            with self.subTest(stype=stype):
                self.tearDown()
                self.setUp()
                self._photo_stays_home(stype)

    def _photo_stays_home(self, stype):
        self.seed()
        self.source('s-9999999999', 'On the porch')
        self.conn.execute("UPDATE sources SET source_type = ? WHERE id = 's-9999999999'",
                          (stype,))
        self.conn.execute('INSERT INTO source_files(source_id, path, role, is_primary) '
                          "VALUES ('s-9999999999', 'photos/porch.jpg', 'front', 1)")
        self.claim('c-9999999999', 's-9999999999', 'residence', '1958',
                   [(CALVIN, 'subject'), (PIP, 'grandchild')], 'Calvin and Pip on the porch')
        self.build()
        self.assertFalse((self.out / 'sources' / 's-9999999999.html').exists())
        self.assertNotIn('s-9999999999.html', self.page(f'persons/{CALVIN}.html'))
        self.assertFalse((self.out / 'media' / 's-9999999999').exists())

    def test_a_living_sons_own_birth_certificate_still_gets_no_page(self):
        self.census()
        self.assertFalse((self.out / 'sources' / 's-4444444444.html').exists())

    def test_the_linked_preview_still_shows_everything(self):
        self.seed()
        self.build(linked=True)
        self.assertIn('godparent Wren Ashcombe', self.page(f'persons/{THOMAS}.html'))


def _person_md(pid: str, name: str, extra: str = '', living: str = 'false') -> str:
    return (f'---\nid: {pid}\nname: {name}\ntier: curated\n'
            f'living: {living}\n{extra}---\n# {name}\n')


def _source_md(sid: str, title: str, claims: str) -> str:
    return (f'---\nid: {sid}\ntitle: {title}\nsource_type: vital-record\n'
            f'citation: "{title}."\n---\n\n## Claims\n```yaml\n{claims}```\n')


class _ArchiveBase(unittest.TestCase):
    """Real record files indexed by `fha index`, for GEDCOM and WikiTree."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / 'fha.yaml').write_text('archive_name: test\n', encoding='utf-8')
        self.write('people/hartley__thomas_P-AAAAAAAAAA.md',
                   _person_md('P-AAAAAAAAAA', 'Thomas Hartley'))
        self.write('people/ashcombe__wren_P-BBBBBBBBBB.md',
                   _person_md('P-BBBBBBBBBB', 'Wren Ashcombe', living='true'))
        self.write('people/hartley__calvin_P-CCCCCCCCCC.md',
                   _person_md('P-CCCCCCCCCC', 'Calvin Hartley'))
        self.write('people/quill__hester_P-EEEEEEEEEE.md',
                   _person_md('P-EEEEEEEEEE', 'Hester Quill', 'restricted: by-request\n'))

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def index(self):
        index.build_index(self.root, {})


class GedcomTests(_ArchiveBase):
    def test_a_birth_naming_a_living_godparent_keeps_its_date(self):
        self.write('sources/vital-record/src_S-1111111111.md', _source_md(
            'S-1111111111', 'Riverside baptism register',
            '- id: C-1111111111\n  type: birth\n  date: 1885-04-02\n'
            '  persons: [P-AAAAAAAAAA, P-BBBBBBBBBB]\n'
            '  roles: {child: [P-AAAAAAAAAA], godparent: [P-BBBBBBBBBB]}\n'
            '  value: Thomas born, godparent Wren\n  status: accepted\n'))
        self.index()
        text = gedcom.run_gedcom(self.root, None, all_persons=True)['text']
        self.assertIn('2 DATE 2 APR 1885', text)
        self.assertNotIn('Wren', text)
        self.assertNotIn('Ashcombe', text)

    def test_a_death_naming_a_by_request_informant_is_withheld(self):
        self.write('sources/vital-record/src_S-5555555555.md', _source_md(
            'S-5555555555', 'Calvin Hartley death certificate',
            '- id: C-5555555555\n  type: death\n  date: 1950-01-09\n'
            '  persons: [P-CCCCCCCCCC, P-EEEEEEEEEE]\n'
            '  roles: {deceased: [P-CCCCCCCCCC], informant: [P-EEEEEEEEEE]}\n'
            '  value: Calvin died\n  status: accepted\n'))
        self.index()
        text = gedcom.run_gedcom(self.root, None, all_persons=True)['text']
        self.assertNotIn('1950', text)
        self.assertNotIn('Hester', text)


class WikiTreeTests(_ArchiveBase):
    TEMPLATES = {'birth': {'template': 'Baptism', 'fields': {'date': 'date', 'note': 'value'}}}

    def conn(self):
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn

    def test_a_template_from_a_birth_naming_a_living_godparent_leaves_out_its_wording(self):
        self.write('sources/vital-record/src_S-1111111111.md', _source_md(
            'S-1111111111', 'Riverside baptism register',
            '- id: C-1111111111\n  type: birth\n  date: 1885-04-02\n'
            '  persons: [P-AAAAAAAAAA, P-BBBBBBBBBB]\n'
            '  roles: {child: [P-AAAAAAAAAA], godparent: [P-BBBBBBBBBB]}\n'
            '  value: Thomas born, godparent Wren Ashcombe\n  status: accepted\n'))
        self.index()
        out = wikitree._render_templates(self.conn(), self.root, 'p-aaaaaaaaaa', self.TEMPLATES)
        self.assertEqual(out, ['{{Baptism|date=1885-04-02}}'])

    def test_a_template_from_a_living_sons_birth_is_withheld(self):
        self.write('people/hartley__pip_P-FFFFFFFFFF.md',
                   _person_md('P-FFFFFFFFFF', 'Pip Hartley', living='true'))
        self.write('sources/vital-record/src_S-4444444444.md', _source_md(
            'S-4444444444', 'Pip Hartley birth certificate',
            '- id: C-4444444444\n  type: residence\n  date: 1931-08-08\n'
            '  persons: [P-FFFFFFFFFF, P-AAAAAAAAAA]\n'
            '  roles: {subject: [P-FFFFFFFFFF], parent: [P-AAAAAAAAAA]}\n'
            '  value: Pip lived in Millbrook\n  status: accepted\n'))
        self.index()
        templates = {'residence': {'template': 'Residence', 'fields': {'date': 'date'}}}
        self.assertEqual(
            wikitree._render_templates(self.conn(), self.root, 'p-aaaaaaaaaa', templates), [])


if __name__ == '__main__':
    unittest.main()
