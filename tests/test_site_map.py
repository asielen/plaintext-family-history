"""
test_site_map.py: the family map page of the family site (TOOLING 12 "Pages",
"Family map").

What a relative sees: one page, map.html, with an outline map of the world
fitted to the family's places and a pin at every registered place that has
coordinates, each listing the people the records put there. It works from a
USB stick with no internet: the outline, D3 and topojson-client are vendored
files, and the pins are inline JSON in the page. Places with no coordinates
are listed under the map, never guessed onto it.

On the shared site the map shows nothing a place page would not: no pin,
count or name for a living, unknown, restricted or by-request person, no pin
whose only people are hidden, and no count that changes because a hidden
person is on a record. A claim places only the people it is about (its only
person, or those under `subject`, `head` or `household_member`), never a
child merely named on his father's census line.

Journeys: a line for each person who has died, from the place of one of
their records to the next in date order, a vague date sitting at its middle
("about 1840" is 1840). Undated records and records at places with no map
position are counted, never drawn. No living, unknown, restricted or
by-request person has a line on the shared site, and a line never passes
through a record the shared site withholds.

Builds a synthetic .cache/index.sqlite from index.py's DDL, the way
tests/test_site_search.py does. Temp fixtures only. The JavaScript half is
checked with node where node is installed (CI has none, so those tests skip
there).
"""

import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

from index import _DDL as INDEX_DDL
from _lib import edtf_bounds, index_manifest_path, record_path_manifest, write_path_manifest

# site.py's stem collides with the stdlib `site` module, so it is loaded by
# path under a private name (as tests/test_site.py and fha.py do).
_spec = importlib.util.spec_from_file_location('fha_site_map', ROOT / 'tools' / 'site.py')
site = importlib.util.module_from_spec(_spec)
sys.modules['fha_site_map'] = site
_spec.loader.exec_module(site)

VENDOR = ROOT / 'tools' / 'templates' / 'vendor'
MAP_JS = VENDOR / 'fha-map.js'

THOMAS = 'p-aaaaaaaaaa'     # deceased
MARGARET = 'p-bbbbbbbbbb'   # deceased
ETHEL = 'p-cccccccccc'      # deceased, their daughter
LIVE = 'p-dddddddddd'       # living: true
UNK = 'p-eeeeeeeeee'        # living: unknown
SECRET = 'p-ffffffffff'     # deceased, restricted: true
QUIET = 'p-gggggggggg'      # deceased, restricted: by-request
STUB = 'p-hhhhhhhhhh'       # deceased stub (no page on a shared site)

FAIRVIEW = 'l-aaaaaaaaaa'   # coords
CEMETERY = 'l-bbbbbbbbbb'   # within Fairview, no coords of its own
ELM_CREEK = 'l-cccccccccc'  # coords
TOWNSHIP = 'l-dddddddddd'   # no coords anywhere on its chain

SRC = 's-aaaaaaaaaa'

WITHHELD_NAMES = {LIVE: 'Walter Livesey', UNK: 'Ursula Unwin',
                  SECRET: 'Rupert Sequester', QUIET: 'Prudence Quayle'}


def _map_json(page: str) -> dict:
    m = re.search(r'<script type="application/json" id="fha-map-data">(.*?)</script>', page, re.S)
    assert m, 'map.html carries no inline map data'
    return json.loads(m.group(1))


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        cache = self.root / '.cache'
        cache.mkdir(parents=True)
        self.conn = sqlite3.connect(str(cache / 'index.sqlite'))
        self.conn.executescript(INDEX_DDL)
        self.conn.row_factory = sqlite3.Row
        self.out = self.root / '.cache' / 'site'
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        self.source(SRC, 'Breton County register')

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    # Seeding

    def person(self, pid, name, *, living='false', tier='curated', extra=''):
        surname = name.split()[-1]
        rel = f'people/{surname.lower()}_{pid}.md'
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'---\nid: {pid}\nname: {name}\nliving: {living}\n{extra}---\n# {name}\n',
                        encoding='utf-8')
        self.conn.execute(
            'INSERT INTO persons(id, name, surname, sex, living, tier, status, path) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (pid, name, surname, 'F', living, tier, 'active', rel))

    def source(self, sid, title, *, restricted=0, restricted_claims=()):
        rel = f'sources/vital-record/src_{sid}.md'
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = ['---', f'id: {sid}', f'title: "{title}"', 'source_type: vital-record',
                 'citation: "A citation."', '---', '', '## Claims', '```yaml']
        for cid in restricted_claims:
            lines += [f'- id: {cid}', '  type: residence', '  value: "private"',
                      '  status: accepted', '  restricted: true']
        lines.append('```')
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        self.conn.execute(
            'INSERT INTO sources(id, title, source_type, restricted, status, path) '
            'VALUES (?,?,?,?,?,?)', (sid, title, 'vital-record', restricted, 'active', rel))

    def place(self, lid, name, *, hierarchy=None, within=None, lat=None, lon=None):
        self.conn.execute(
            'INSERT INTO places(id, name, hierarchy, within, lat, lon) VALUES (?,?,?,?,?,?)',
            (lid, name, hierarchy, within, lat, lon))

    def claim(self, cid, ctype, persons, place, *, roles=None, status='accepted',
              date='1880', source=SRC, negated=0):
        # The bounds the index gives the date (index.py: none for no date).
        lo, hi = edtf_bounds(date) if date else ('', '')
        self.conn.execute(
            'INSERT INTO claims(id, source_id, type, value, status, date_edtf, date_min, '
            'date_max, place_id, negated) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (cid, source, ctype, f'{ctype} record', status, date, lo, hi, place, negated))
        for pos, pid in enumerate(persons):
            self.conn.execute(
                'INSERT INTO claim_persons(claim_id, person_id, position, role) '
                'VALUES (?,?,?,?)', (cid, pid, pos, (roles or {}).get(pid)))

    def seed_places(self):
        self.place(FAIRVIEW, 'Fairview', hierarchy='Fairview, Breton County, Kansas, USA',
                   lat=39.8, lon=-95.6)
        self.place(CEMETERY, 'Fairview Cemetery', within=FAIRVIEW)
        self.place(ELM_CREEK, 'Elm Creek', hierarchy='Elm Creek, Kansas, USA',
                   lat=39.5, lon=-96.25)
        self.place(TOWNSHIP, 'Breton Township', hierarchy='Breton Township, Kansas, USA')

    def seed_family(self):
        self.seed_places()
        self.person(THOMAS, 'Thomas Edward Hartley')
        self.person(MARGARET, 'Margaret Cole')
        self.person(ETHEL, 'Ethel Hartley')

    def build(self, *, linked=False, out=None, dry_run=False):
        self.conn.commit()
        future = time.time() + 5
        os.utime(self.root / '.cache' / 'index.sqlite', (future, future))
        write_path_manifest(index_manifest_path(self.root), record_path_manifest(self.root))
        with unittest.mock.patch.object(site, '_today', return_value='2026-06-15'):
            return site.run_site(self.root, out or self.out, linked=linked, dry_run=dry_run)

    def page(self, out=None) -> str:
        return ((out or self.out) / 'map.html').read_text(encoding='utf-8')

    def data(self, out=None) -> dict:
        return _map_json(self.page(out))

    def pin(self, lid, out=None):
        for p in self.data(out)['places']:
            if p['id'] == lid:
                return p
        return None

    def names(self, pin) -> list[str]:
        return [p['name'] for p in pin['people']]


class PinTests(_Base):
    def test_a_pin_for_each_place_with_coordinates_and_a_list_of_the_rest(self):
        self.seed_family()
        self.claim('c-aaaaaaaaaa', 'birth', [ETHEL], FAIRVIEW, roles={ETHEL: 'child'})
        self.claim('c-bbbbbbbbbb', 'residence', [THOMAS], ELM_CREEK, date='1870')
        self.claim('c-cccccccccc', 'residence', [MARGARET], TOWNSHIP, date='1890')
        self.claim('c-dddddddddd', 'death', [MARGARET], TOWNSHIP, date='1921')
        result = self.build()
        self.assertEqual(result['status'], 'ok', result['messages'])
        data = self.data()
        self.assertEqual(data['version'], 1)
        self.assertEqual([p['id'] for p in data['places']], [ELM_CREEK, FAIRVIEW],
                         'pins sort by name')
        fairview = self.pin(FAIRVIEW)
        self.assertEqual((fairview['lat'], fairview['lon']), (39.8, -95.6))
        self.assertEqual(fairview['events'], 1)
        self.assertEqual(fairview['href'], f'places/{FAIRVIEW}.html')
        self.assertEqual(fairview['label'], 'Fairview, Breton County, Kansas, USA')
        self.assertEqual(fairview['people'],
                         [{'name': 'Ethel Hartley', 'href': f'persons/{ETHEL}.html'}])
        # The township has no coordinates: listed under the map with its two
        # events, never drawn at a guessed spot.
        self.assertNotIn(TOWNSHIP, [p['id'] for p in data['places']])
        page = self.page()
        self.assertRegex(page, r'(?s)no map position yet.*?'
                               rf'<a href="places/{TOWNSHIP}\.html">Breton Township</a>.*?2 events')

    def test_a_cemetery_without_coordinates_counts_on_its_town(self):
        self.seed_family()
        self.claim('c-aaaaaaaaaa', 'burial', [THOMAS], CEMETERY, roles={THOMAS: 'deceased'})
        self.claim('c-bbbbbbbbbb', 'residence', [THOMAS], FAIRVIEW)
        self.build()
        fairview = self.pin(FAIRVIEW)
        self.assertEqual(fairview['events'], 2)
        self.assertEqual(self.names(fairview), ['Thomas Edward Hartley'])
        self.assertIsNone(self.pin(CEMETERY))

    def test_a_claim_places_only_the_people_it_is_about(self):
        # A child named on his father's residence, and a father named on his
        # daughter's birth, are not placed there by those records.
        self.seed_family()
        self.claim('c-aaaaaaaaaa', 'residence', [THOMAS, ETHEL], ELM_CREEK,
                   roles={THOMAS: 'subject', ETHEL: 'mentioned'})
        self.claim('c-bbbbbbbbbb', 'birth', [ETHEL, THOMAS], FAIRVIEW,
                   roles={ETHEL: 'child', THOMAS: 'father'})
        self.claim('c-cccccccccc', 'census', [THOMAS, MARGARET], FAIRVIEW,
                   roles={THOMAS: 'head', MARGARET: 'household_member'})
        self.build()
        self.assertEqual(self.names(self.pin(ELM_CREEK)), ['Thomas Edward Hartley'])
        fairview = self.pin(FAIRVIEW)
        self.assertEqual(fairview['events'], 2)
        self.assertEqual(sorted(self.names(fairview)),
                         ['Ethel Hartley', 'Margaret Cole', 'Thomas Edward Hartley'])

    def test_a_claim_about_nobody_places_nobody(self):
        self.seed_family()
        self.claim('c-aaaaaaaaaa', 'residence', [THOMAS], ELM_CREEK)
        self.claim('c-bbbbbbbbbb', 'event', [], FAIRVIEW)
        self.build()
        self.assertIsNone(self.pin(FAIRVIEW), 'a town event naming nobody is not a family pin')

    def test_only_accepted_claims_in_every_mode(self):
        self.seed_family()
        self.claim('c-aaaaaaaaaa', 'residence', [THOMAS], ELM_CREEK)
        self.claim('c-bbbbbbbbbb', 'residence', [THOMAS], FAIRVIEW, status='suggested')
        self.claim('c-cccccccccc', 'residence', [MARGARET], FAIRVIEW, status='disputed')
        self.claim('c-dddddddddd', 'residence', [ETHEL], FAIRVIEW, negated=1)
        for linked in (False, True):
            with self.subTest(linked=linked):
                self.build(linked=linked)
                self.assertIsNone(self.pin(FAIRVIEW))
                self.assertEqual(self.pin(ELM_CREEK)['events'], 1)

    def test_people_are_listed_most_placed_first_and_capped_at_twelve(self):
        self.seed_places()
        pids = [f'p-{c * 10}' for c in 'abcdefghjkmnpqrs']   # 16 people
        for n, pid in enumerate(pids):
            self.person(pid, f'Person {chr(65 + n)} Hartley')
            self.claim(f'c-{"abcdefghjkmnpqrs"[n] * 10}', 'residence', [pid], FAIRVIEW)
        self.claim('c-0000000000', 'residence', [pids[-1]], FAIRVIEW, date='1890')
        self.build()
        fairview = self.pin(FAIRVIEW)
        self.assertEqual(fairview['events'], 17)
        self.assertEqual(len(fairview['people']), 12)
        self.assertEqual(fairview['more_people'], 4)
        self.assertEqual(fairview['people'][0]['name'], 'Person P Hartley')   # two records
        self.assertEqual(fairview['people'][1]['name'], 'Person A Hartley')

    def test_a_stub_counts_but_is_not_listed(self):
        self.seed_family()
        self.person(STUB, 'Samuel Stub', tier='stub')
        self.claim('c-aaaaaaaaaa', 'residence', [STUB], FAIRVIEW)
        self.build()
        fairview = self.pin(FAIRVIEW)
        self.assertEqual(fairview['events'], 1)
        self.assertEqual(fairview['people'], [])
        self.assertEqual(fairview['more_people'], 0)


class PageTests(_Base):
    def test_no_coordinates_means_no_map_page_and_no_link(self):
        self.seed_family()
        self.claim('c-aaaaaaaaaa', 'residence', [THOMAS], TOWNSHIP)
        result = self.build()
        self.assertFalse((self.out / 'map.html').exists())
        for page in self.out.rglob('*.html'):
            with self.subTest(str(page.relative_to(self.out))):
                self.assertNotIn('map.html', page.read_text(encoding='utf-8'))
        pages = sum(1 for _ in self.out.rglob('*.html'))
        self.assertEqual(result['pages'], pages)

    def test_the_map_is_linked_from_every_page_and_counted(self):
        self.seed_family()
        self.claim('c-aaaaaaaaaa', 'residence', [THOMAS], FAIRVIEW)
        result = self.build()
        self.assertTrue((self.out / 'map.html').is_file())
        for page in self.out.rglob('*.html'):
            rel = page.relative_to(self.out)
            prefix = '..' if len(rel.parts) > 1 else '.'
            with self.subTest(str(rel)):
                self.assertIn(f'href="{prefix}/map.html"', page.read_text(encoding='utf-8'))
        self.assertEqual(result['pages'], sum(1 for _ in self.out.rglob('*.html')))
        preview = self.build(dry_run=True)
        self.assertEqual(preview['pages'], result['pages'])

    def test_a_rebuild_with_no_coordinates_left_removes_the_old_map(self):
        self.seed_family()
        self.claim('c-aaaaaaaaaa', 'residence', [THOMAS], FAIRVIEW)
        self.build()
        self.assertTrue((self.out / 'map.html').is_file())
        self.conn.execute('UPDATE places SET lat = NULL, lon = NULL')
        self.build()
        self.assertFalse((self.out / 'map.html').exists())

    def test_the_page_loads_only_vendored_scripts(self):
        self.seed_family()
        self.claim('c-aaaaaaaaaa', 'residence', [THOMAS], FAIRVIEW)
        self.build()
        page = self.page()
        srcs = re.findall(r'<script[^>]*\bsrc="([^"]+)"', page)
        wanted = ['./vendor/d3.min.js', './vendor/topojson-client.min.js',
                  './vendor/fha-world.js', './vendor/fha-map.js']
        self.assertEqual([s for s in srcs if s in wanted], wanted, 'loaded in this order')
        for src in srcs:
            with self.subTest(src):
                self.assertTrue((self.out / src).is_file())

    def test_a_place_name_cannot_break_out_of_the_data(self):
        self.seed_family()
        self.place('l-eeeeeeeeee', '</script><b>Elm & Co', lat=40.0, lon=-96.0)
        self.claim('c-aaaaaaaaaa', 'residence', [THOMAS], 'l-eeeeeeeeee')
        self.build()
        page = self.page()
        self.assertNotIn('</script><b>', page)
        self.assertEqual(self.pin('l-eeeeeeeeee')['name'], '</script><b>Elm & Co')

    def test_two_builds_give_the_same_bytes(self):
        self.seed_family()
        self.claim('c-aaaaaaaaaa', 'residence', [THOMAS], FAIRVIEW)
        self.claim('c-bbbbbbbbbb', 'residence', [MARGARET], ELM_CREEK)
        self.claim('c-cccccccccc', 'residence', [ETHEL], TOWNSHIP)
        other = self.root / 'other-site'
        self.build()
        self.build(out=other)
        self.assertEqual(self.page(), self.page(other))

    def test_the_no_script_list_groups_places_by_country(self):
        self.seed_family()
        self.place('l-eeeeeeeeee', 'Suwałki', hierarchy='Suwałki, Poland', lat=54.1, lon=22.93)
        self.claim('c-aaaaaaaaaa', 'residence', [THOMAS], FAIRVIEW)
        self.claim('c-bbbbbbbbbb', 'birth', [MARGARET], 'l-eeeeeeeeee')
        self.build()
        page = self.page()
        self.assertRegex(page, r'(?s)<h3>Poland</h3>.*Suwałki.*<h3>USA</h3>.*Fairview')


class PrivacyTests(_Base):
    """A shared map shows no more than the shared place pages, and no count
    a hidden person changes. The linked preview shows everyone."""

    def seed_everyone(self):
        self.seed_family()
        self.person(LIVE, WITHHELD_NAMES[LIVE], living='true')
        self.person(UNK, WITHHELD_NAMES[UNK], living='unknown')
        self.person(SECRET, WITHHELD_NAMES[SECRET], extra='restricted: true\n')
        self.person(QUIET, WITHHELD_NAMES[QUIET], extra='restricted: by-request\n')
        self.claim('c-aaaaaaaaaa', 'residence', [THOMAS], FAIRVIEW)
        # Each hidden person's own birth at Elm Creek, and nothing else there.
        # Their own registers: a register naming a by-request person is
        # private as a whole, and would take Thomas's record with it.
        for n, pid in enumerate((LIVE, UNK, SECRET, QUIET)):
            sid = f's-{"bcde"[n] * 10}'
            self.source(sid, f'Register volume {"ABCD"[n]}')
            self.claim(f'c-{"bcde"[n] * 10}', 'birth', [pid], ELM_CREEK,
                       roles={pid: 'child'}, date='1950', source=sid)
            # And at the township, which has no map position.
            self.claim(f'c-{"fghj"[n] * 10}', 'residence', [pid], TOWNSHIP, date='1960',
                       source=sid)

    def test_a_hidden_person_gets_no_pin_count_or_name(self):
        self.seed_everyone()
        self.build()
        data = self.data()
        self.assertEqual([p['id'] for p in data['places']], [FAIRVIEW],
                         'a pin whose only people are hidden does not appear')
        self.assertEqual(self.pin(FAIRVIEW)['events'], 1)
        page = self.page()
        for pid, name in WITHHELD_NAMES.items():
            with self.subTest(name):
                self.assertNotIn(name, page)
                self.assertNotIn(pid, page)
        self.assertNotIn('Breton Township', page, 'nor a no-position entry with only hidden people')
        self.assertNotIn('Elm Creek', page)

    def test_the_linked_preview_shows_everyone(self):
        self.seed_everyone()
        self.build(linked=True)
        elm = self.pin(ELM_CREEK)
        self.assertEqual(elm['events'], 4)
        self.assertEqual(sorted(self.names(elm)), sorted(WITHHELD_NAMES.values()))
        self.assertIn('Breton Township', self.page())

    def test_a_hidden_person_on_a_shown_record_changes_no_count(self):
        # Thomas's 1880 household names his living grandson; the record stays
        # on Thomas's page without its wording, and the map counts it the same
        # as if the grandson were not on it.
        def counts(with_hidden: bool):
            self.tearDown()
            self.setUp()
            self.seed_family()
            self.person(LIVE, WITHHELD_NAMES[LIVE], living='true')
            people = [THOMAS, MARGARET] + ([LIVE] if with_hidden else [])
            roles = {THOMAS: 'head', MARGARET: 'household_member', LIVE: 'household_member'}
            self.claim('c-aaaaaaaaaa', 'census', people, FAIRVIEW, roles=roles)
            self.build()
            pin = self.pin(FAIRVIEW)
            return pin['events'], self.names(pin), pin['more_people']

        self.assertEqual(counts(True), counts(False))
        self.assertEqual(counts(True), (1, ['Margaret Cole', 'Thomas Edward Hartley'], 0))

    def test_a_wedding_with_a_living_spouse_puts_no_pin(self):
        # Margaret is dead, but the wedding is also her living husband's own
        # event: where it happened stays home, with the record.
        self.seed_family()
        self.person(LIVE, WITHHELD_NAMES[LIVE], living='true')
        self.source('s-bbbbbbbbbb', 'Marriage register')
        self.claim('c-aaaaaaaaaa', 'residence', [THOMAS], FAIRVIEW)
        self.claim('c-bbbbbbbbbb', 'marriage', [MARGARET, LIVE], ELM_CREEK,
                   roles={MARGARET: 'spouse', LIVE: 'spouse'}, source='s-bbbbbbbbbb')
        self.build()
        self.assertIsNone(self.pin(ELM_CREEK))
        self.build(linked=True)
        self.assertEqual(self.pin(ELM_CREEK)['events'], 1)

    def test_a_private_record_is_not_counted(self):
        self.seed_family()
        self.source('s-bbbbbbbbbb', 'Sealed register', restricted=1)
        self.source('s-cccccccccc', 'Open register', restricted_claims=('c-cccccccccc',))
        self.claim('c-aaaaaaaaaa', 'residence', [THOMAS], FAIRVIEW)
        self.claim('c-bbbbbbbbbb', 'residence', [MARGARET], FAIRVIEW, source='s-bbbbbbbbbb')
        self.claim('c-cccccccccc', 'residence', [ETHEL], FAIRVIEW, source='s-cccccccccc')
        self.build()
        fairview = self.pin(FAIRVIEW)
        self.assertEqual(fairview['events'], 1)
        self.assertEqual(self.names(fairview), ['Thomas Edward Hartley'])
        self.build(linked=True)
        self.assertEqual(self.pin(FAIRVIEW)['events'], 3)


HAYS = 'l-ffffffffff'       # coords, a third town for journeys


class _JourneyBase(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.seed_family()
        self.place(HAYS, 'Hays', hierarchy='Hays, Ellis County, Kansas, USA',
                   lat=38.88, lon=-99.33)

    def journey(self, name, out=None):
        for j in self.data(out)['journeys']:
            if j['name'] == name:
                return j
        return None

    def stops(self, name, out=None):
        j = self.journey(name, out)
        if j is None:
            return None
        return [(s['place'], s['year_from'], s['year_to'], s['what']) for s in j['stops']]

    def journey_names(self, out=None):
        return [j['name'] for j in self.data(out)['journeys']]


class JourneyTests(_JourneyBase):
    """Lines on the map for each person who has died: their own dated,
    placed, accepted records in date order, one stop per town in a row."""

    def test_a_life_in_three_towns(self):
        self.claim('c-aaaaaaaaaa', 'birth', [THOMAS], ELM_CREEK, roles={THOMAS: 'child'},
                   date='1840-03-02')
        self.claim('c-bbbbbbbbbb', 'census', [THOMAS], FAIRVIEW, roles={THOMAS: 'head'},
                   date='1870')
        self.claim('c-cccccccccc', 'residence', [THOMAS], FAIRVIEW, date='1880')
        self.claim('c-dddddddddd', 'death', [THOMAS], HAYS, roles={THOMAS: 'deceased'},
                   date='1941-01-19')
        result = self.build()
        self.assertEqual(result['status'], 'ok', result['messages'])
        j = self.journey('Thomas Edward Hartley')
        self.assertEqual(j['href'], f'persons/{THOMAS}.html')
        self.assertEqual(j['life'], '1840-1941')
        # The two Fairview records in a row are one stop, 1870 to 1880.
        self.assertEqual(self.stops('Thomas Edward Hartley'), [
            (ELM_CREEK, 1840, 1840, ['birth']),
            (FAIRVIEW, 1870, 1880, ['census', 'residence']),
            (HAYS, 1941, 1941, ['death']),
        ])
        self.assertEqual((j['undated'], j['unplaced']), (0, 0))
        pins = {p['id'] for p in self.data()['places']}
        self.assertTrue({s['place'] for s in j['stops']} <= pins, 'every stop is a pin')

    def test_a_stop_sits_at_the_middle_of_its_date(self):
        # "About 1840" spans 1839 to 1841 and reads 1840, not the 1839 its
        # range starts in; a decade or a range reads its middle; "by 1895"
        # and "from 1883 on" read the year they name.
        dates = [('1840~', 1840), ('186X', 1865), ('1870/1880', 1875), ('[1883..]', 1883),
                 ('[..1895]', 1895)]
        towns = [ELM_CREEK, FAIRVIEW]
        for n, (date, _year) in enumerate(dates):
            self.claim(f'c-{"abcde"[n] * 10}', 'residence', [THOMAS], towns[n % 2], date=date)
        self.build()
        self.assertEqual([(s[0], s[1]) for s in self.stops('Thomas Edward Hartley')],
                         [(towns[n % 2], year) for n, (_d, year) in enumerate(dates)])

    def test_a_vague_date_does_not_jump_ahead_of_an_exact_one(self):
        # Died 30 December 1900 at Hays, buried "about 1901" at Fairview's
        # cemetery: the burial's range starts first (1900), but its middle is
        # after the death, so the line goes Hays, then Fairview.
        self.claim('c-aaaaaaaaaa', 'birth', [THOMAS], ELM_CREEK, roles={THOMAS: 'child'},
                   date='1840')
        self.claim('c-bbbbbbbbbb', 'burial', [THOMAS], CEMETERY, roles={THOMAS: 'deceased'},
                   date='1901~')
        self.claim('c-cccccccccc', 'death', [THOMAS], HAYS, roles={THOMAS: 'deceased'},
                   date='1900-12-30')
        self.build()
        self.assertEqual(self.stops('Thomas Edward Hartley'), [
            (ELM_CREEK, 1840, 1840, ['birth']),
            (HAYS, 1900, 1900, ['death']),
            (FAIRVIEW, 1901, 1901, ['burial']),
        ])

    def test_records_on_the_same_date_keep_the_timeline_order(self):
        # Same year, two sources: the source id decides, as on his timeline,
        # whatever order the index holds them in.
        self.source('s-bbbbbbbbbb', 'Kansas state census')
        self.claim('c-aaaaaaaaaa', 'residence', [THOMAS], FAIRVIEW, date='1880',
                   source='s-bbbbbbbbbb')
        self.claim('c-bbbbbbbbbb', 'residence', [THOMAS], ELM_CREEK, date='1880')
        self.build()
        self.assertEqual([s[0] for s in self.stops('Thomas Edward Hartley')],
                         [ELM_CREEK, FAIRVIEW])

    def test_undated_and_unmapped_records_are_counted_not_drawn(self):
        self.claim('c-aaaaaaaaaa', 'residence', [THOMAS], ELM_CREEK, date='1860')
        self.claim('c-bbbbbbbbbb', 'residence', [THOMAS], FAIRVIEW, date='1870')
        self.claim('c-cccccccccc', 'residence', [THOMAS], HAYS, date=None)
        self.claim('c-dddddddddd', 'occupation', [THOMAS], HAYS, date='sometime')
        self.claim('c-eeeeeeeeee', 'residence', [THOMAS], TOWNSHIP, date='1875')
        self.build()
        j = self.journey('Thomas Edward Hartley')
        self.assertEqual([s[0] for s in self.stops('Thomas Edward Hartley')],
                         [ELM_CREEK, FAIRVIEW])
        self.assertEqual((j['undated'], j['unplaced']), (2, 1))
        data = self.data()
        self.assertEqual((data['undated_total'], data['unplaced_total']), (2, 1))
        self.assertIn('Not on the lines: 2 events with no date and 1 at a place with no '
                      'map position yet.', self.page())

    def test_the_counts_line_reads_right_for_none_one_and_many(self):
        self.claim('c-aaaaaaaaaa', 'residence', [THOMAS], ELM_CREEK, date='1860')
        self.claim('c-bbbbbbbbbb', 'residence', [THOMAS], FAIRVIEW, date='1870')
        self.build()
        self.assertNotIn('Not on the lines', self.page())
        self.claim('c-cccccccccc', 'residence', [THOMAS], HAYS, date=None)
        self.build()
        self.assertIn('Not on the lines: 1 event with no date.', self.page())
        self.claim('c-dddddddddd', 'residence', [THOMAS], TOWNSHIP, date='1875')
        self.claim('c-eeeeeeeeee', 'residence', [THOMAS], TOWNSHIP, date='1876')
        self.build()
        self.assertIn('Not on the lines: 1 event with no date and 2 at a place with no '
                      'map position yet.', self.page())

    def test_a_record_places_only_the_people_it_is_about(self):
        # Ethel's birth at Hays names her mother, and Margaret's 1880
        # residence mentions Thomas: neither gives the other person a stop.
        self.claim('c-aaaaaaaaaa', 'birth', [ETHEL, MARGARET], HAYS,
                   roles={ETHEL: 'child', MARGARET: 'mother'}, date='1885')
        self.claim('c-bbbbbbbbbb', 'residence', [MARGARET, THOMAS], ELM_CREEK,
                   roles={MARGARET: 'subject', THOMAS: 'mentioned'}, date='1880')
        self.claim('c-cccccccccc', 'residence', [MARGARET], FAIRVIEW, date='1890')
        self.claim('c-dddddddddd', 'residence', [THOMAS], FAIRVIEW, date='1870')
        self.claim('c-eeeeeeeeee', 'residence', [THOMAS], HAYS, date='1900')
        self.build()
        self.assertEqual([s[0] for s in self.stops('Margaret Cole')], [ELM_CREEK, FAIRVIEW])
        self.assertEqual([s[0] for s in self.stops('Thomas Edward Hartley')], [FAIRVIEW, HAYS])
        self.assertIsNone(self.journey('Ethel Hartley'), 'one stop draws no line')

    def test_a_cemetery_without_coordinates_is_a_stop_at_its_town(self):
        self.claim('c-aaaaaaaaaa', 'residence', [THOMAS], ELM_CREEK, date='1900')
        self.claim('c-bbbbbbbbbb', 'burial', [THOMAS], CEMETERY, roles={THOMAS: 'deceased'},
                   date='1941')
        self.build()
        self.assertEqual([s[0] for s in self.stops('Thomas Edward Hartley')],
                         [ELM_CREEK, FAIRVIEW])

    def test_journeys_are_in_name_order(self):
        for n, pid in enumerate((THOMAS, MARGARET, ETHEL)):
            self.claim(f'c-{"abc"[n] * 10}', 'residence', [pid], ELM_CREEK, date='1900')
            self.claim(f'c-{"def"[n] * 10}', 'residence', [pid], FAIRVIEW, date='1910')
        self.build()
        self.assertEqual(self.journey_names(),
                         ['Ethel Hartley', 'Margaret Cole', 'Thomas Edward Hartley'])

    def test_a_stub_has_no_journey(self):
        self.person(STUB, 'Samuel Stub', tier='stub')
        self.claim('c-aaaaaaaaaa', 'residence', [STUB], ELM_CREEK, date='1900')
        self.claim('c-bbbbbbbbbb', 'residence', [STUB], FAIRVIEW, date='1910')
        self.build()
        self.assertEqual(self.data()['journeys'], [])

    def test_two_builds_give_the_same_bytes(self):
        self.claim('c-aaaaaaaaaa', 'residence', [THOMAS], ELM_CREEK, date='1860')
        self.claim('c-bbbbbbbbbb', 'residence', [THOMAS], FAIRVIEW, date='1870')
        self.claim('c-cccccccccc', 'residence', [MARGARET], FAIRVIEW, date='1860')
        self.claim('c-dddddddddd', 'residence', [MARGARET], HAYS, date='1870~')
        other = self.root / 'other-site'
        self.build()
        self.build(out=other)
        self.assertEqual(self.page(), self.page(other))


class JourneyPrivacyTests(_JourneyBase):
    """No journey, stop, line or count for a living, unknown, restricted or
    by-request person on the shared site, and a line never passes through a
    record the shared site withholds."""

    def test_a_hidden_person_has_no_journey_and_counts_nothing(self):
        self.person(LIVE, WITHHELD_NAMES[LIVE], living='true')
        self.person(UNK, WITHHELD_NAMES[UNK], living='unknown')
        self.person(SECRET, WITHHELD_NAMES[SECRET], extra='restricted: true\n')
        self.person(QUIET, WITHHELD_NAMES[QUIET], extra='restricted: by-request\n')
        self.claim('c-aaaaaaaaaa', 'residence', [THOMAS], ELM_CREEK, date='1860')
        self.claim('c-bbbbbbbbbb', 'residence', [THOMAS], FAIRVIEW, date='1870')
        for n, pid in enumerate((LIVE, UNK, SECRET, QUIET)):
            sid = f's-{"bcde"[n] * 10}'
            self.source(sid, f'Register volume {"ABCD"[n]}')
            self.claim(f'c-{"cdef"[n] * 10}', 'residence', [pid], ELM_CREEK, date='1950',
                       source=sid)
            self.claim(f'c-{"ghjk"[n] * 10}', 'residence', [pid], HAYS, date='1960',
                       source=sid)
            self.claim(f'c-{"mnpq"[n] * 10}', 'residence', [pid], TOWNSHIP, date='1970',
                       source=sid)
            self.claim(f'c-{"rstv"[n] * 10}', 'residence', [pid], FAIRVIEW, date=None,
                       source=sid)
        self.build()
        self.assertEqual(self.journey_names(), ['Thomas Edward Hartley'])
        data = self.data()
        self.assertEqual((data['undated_total'], data['unplaced_total']), (0, 0))
        page = self.page()
        self.assertNotIn('Not on the lines', page)
        self.assertNotIn('Hays', page)
        for pid, name in WITHHELD_NAMES.items():
            with self.subTest(name):
                self.assertNotIn(name, page)
                self.assertNotIn(pid, page)
        # The private preview: a living or unknown person still draws no
        # line; the restricted are the owner's own to see.
        self.build(linked=True)
        self.assertEqual(self.journey_names(),
                         ['Prudence Quayle', 'Rupert Sequester', 'Thomas Edward Hartley'])

    def test_a_line_skips_what_the_shared_site_withholds(self):
        self.person(LIVE, WITHHELD_NAMES[LIVE], living='true')
        self.source('s-bbbbbbbbbb', 'Open register', restricted_claims=('c-cccccccccc',))
        self.source('s-cccccccccc', 'Sealed register', restricted=1)
        self.source('s-dddddddddd', 'Marriage register')
        self.claim('c-aaaaaaaaaa', 'residence', [THOMAS], ELM_CREEK, date='1860')
        self.claim('c-bbbbbbbbbb', 'residence', [THOMAS], FAIRVIEW, date='1870')
        # A restricted record, and an undated one in a sealed register, at Hays.
        self.claim('c-cccccccccc', 'residence', [THOMAS], HAYS, date='1865',
                   source='s-bbbbbbbbbb')
        self.claim('c-dddddddddd', 'residence', [THOMAS], HAYS, date=None,
                   source='s-cccccccccc')
        # Margaret's second wedding, to a living man, at Hays.
        self.claim('c-eeeeeeeeee', 'residence', [MARGARET], ELM_CREEK, date='1860')
        self.claim('c-ffffffffff', 'marriage', [MARGARET, LIVE], HAYS,
                   roles={MARGARET: 'spouse', LIVE: 'spouse'}, date='1868',
                   source='s-dddddddddd')
        self.claim('c-gggggggggg', 'residence', [MARGARET], FAIRVIEW, date='1870')
        self.build()
        self.assertEqual([s[0] for s in self.stops('Thomas Edward Hartley')],
                         [ELM_CREEK, FAIRVIEW])
        self.assertEqual([s[0] for s in self.stops('Margaret Cole')], [ELM_CREEK, FAIRVIEW])
        self.assertEqual(self.journey('Thomas Edward Hartley')['undated'], 0)
        self.assertNotIn('Hays', self.page())
        self.build(linked=True)
        self.assertEqual([s[0] for s in self.stops('Thomas Edward Hartley')],
                         [ELM_CREEK, HAYS, FAIRVIEW])
        self.assertEqual([s[0] for s in self.stops('Margaret Cole')],
                         [ELM_CREEK, HAYS, FAIRVIEW])
        self.assertEqual(self.journey('Thomas Edward Hartley')['undated'], 1)

    def test_a_merged_record_moves_with_the_person_it_was_merged_into(self):
        # An old duplicate's residence is the survivor's record now.
        self.person('p-jjjjjjjjjj', 'Tom Hartley', extra=f'merged_into: {THOMAS}\n')
        self.conn.execute("UPDATE persons SET merged_into = ?, status = 'merged' WHERE id = ?",
                          (THOMAS, 'p-jjjjjjjjjj'))
        self.claim('c-aaaaaaaaaa', 'residence', [THOMAS], ELM_CREEK, date='1860')
        self.claim('c-bbbbbbbbbb', 'residence', ['p-jjjjjjjjjj'], FAIRVIEW, date='1870')
        self.build()
        self.assertEqual(self.journey_names(), ['Thomas Edward Hartley'])
        self.assertEqual([s[0] for s in self.stops('Thomas Edward Hartley')],
                         [ELM_CREEK, FAIRVIEW])


class YearSliderTests(_JourneyBase):
    """A slider under the map from the first year on any line to the last:
    moving it back shows where the family had got to by then. Its years come
    only from the lines, so nobody the shared site hides moves its ends."""

    def test_the_years_run_from_the_first_stop_to_the_last(self):
        self.claim('c-aaaaaaaaaa', 'birth', [THOMAS], ELM_CREEK, roles={THOMAS: 'child'},
                   date='1840~')
        self.claim('c-bbbbbbbbbb', 'census', [THOMAS], FAIRVIEW, roles={THOMAS: 'head'},
                   date='1870')
        self.claim('c-cccccccccc', 'residence', [THOMAS], FAIRVIEW, date='1880')
        self.claim('c-dddddddddd', 'residence', [MARGARET], HAYS, date='1855')
        self.claim('c-eeeeeeeeee', 'residence', [MARGARET], FAIRVIEW, date='[..1895]')
        self.build()
        data = self.data()
        self.assertEqual(data['years'], [1840, 1895])
        # Last key, in a fixed place, so two builds give the same bytes.
        self.assertEqual(list(data)[-1], 'years')
        page = self.page()
        self.assertIn('type="range"', page)
        self.assertIn('min="1840" max="1895" step="1" value="1895"', page)
        self.assertIn('aria-valuetext="Every year, up to 1895"', page)

    def test_a_stop_over_several_records_counts_to_its_last_year(self):
        self.claim('c-aaaaaaaaaa', 'residence', [THOMAS], ELM_CREEK, date='1860')
        self.claim('c-bbbbbbbbbb', 'residence', [THOMAS], FAIRVIEW, date='1870')
        self.claim('c-cccccccccc', 'residence', [THOMAS], FAIRVIEW, date='1910')
        self.build()
        self.assertEqual(self.data()['years'], [1860, 1910])

    def test_no_lines_means_no_years_and_no_slider(self):
        # One stop draws no line, so there is nothing to step through.
        self.claim('c-aaaaaaaaaa', 'residence', [THOMAS], ELM_CREEK, date='1860')
        self.build()
        data = self.data()
        self.assertEqual(data['journeys'], [])
        self.assertNotIn('years', data)
        self.assertNotIn('type="range"', self.page())

    def test_a_hidden_person_does_not_move_the_ends(self):
        self.person(LIVE, WITHHELD_NAMES[LIVE], living='true')
        self.person(UNK, WITHHELD_NAMES[UNK], living='unknown')
        self.person(SECRET, WITHHELD_NAMES[SECRET], extra='restricted: true\n')
        self.person(QUIET, WITHHELD_NAMES[QUIET], extra='restricted: by-request\n')
        self.claim('c-aaaaaaaaaa', 'residence', [THOMAS], ELM_CREEK, date='1860')
        self.claim('c-bbbbbbbbbb', 'residence', [THOMAS], FAIRVIEW, date='1870')
        for n, pid in enumerate((LIVE, UNK, SECRET, QUIET)):
            sid = f's-{"bcde"[n] * 10}'
            self.source(sid, f'Register volume {"ABCD"[n]}')
            self.claim(f'c-{"cdef"[n] * 10}', 'residence', [pid], ELM_CREEK, date='1801',
                       source=sid)
            self.claim(f'c-{"ghjk"[n] * 10}', 'residence', [pid], HAYS, date='1990',
                       source=sid)
        self.build()
        self.assertEqual(self.data()['years'], [1860, 1870])
        page = self.page()
        for year in ('1801', '1990'):
            self.assertNotIn(year, page)
        # The private preview: the restricted are the owner's own to see,
        # but a living or unknown person still has no line, so no years.
        self.conn.execute("UPDATE persons SET living = 'true' WHERE id IN (?, ?)",
                          (SECRET, QUIET))
        self.build(linked=True)
        self.assertEqual(self.data()['years'], [1860, 1870])
        self.conn.execute("UPDATE persons SET living = 'false' WHERE id = ?", (SECRET,))
        self.build(linked=True)
        self.assertEqual(self.data()['years'], [1801, 1990])


class VendoredMapFilesTests(unittest.TestCase):
    """The outline the map draws is Natural Earth's 1:110m countries, from
    world-atlas 2.0.2, wrapped as a script so a page opened from disk can
    load it (browsers refuse to fetch a local JSON file)."""

    PREFIX = 'window.FHA_WORLD = '

    def _world_json_bytes(self) -> bytes:
        text = (VENDOR / 'fha-world.js').read_bytes()
        # The statement starts a line; the header comment above quotes it too.
        start = text.index(b'\n' + self.PREFIX.encode()) + 1 + len(self.PREFIX)
        body = text[start:]
        self.assertTrue(body.endswith(b';\n'), 'fha-world.js must end with ";" and a newline')
        return body[:-2] + b'\n'

    def test_the_outline_is_the_pinned_world_atlas_file_unchanged(self):
        # The licence notice records the downloaded file's checksum; the JSON
        # inside the wrapper, with the newline the original ends with, is it.
        notice = (VENDOR / 'licenses' / 'world-atlas-ISC.txt').read_text(encoding='utf-8')
        m = re.search(r'countries-110m\.json.*?sha256 ([0-9a-f]{64})', notice, re.S)
        self.assertIsNotNone(m, 'world-atlas-ISC.txt must record the sha256 of countries-110m.json')
        self.assertEqual(hashlib.sha256(self._world_json_bytes()).hexdigest(), m.group(1))

    def test_the_outline_holds_land_and_countries(self):
        world = json.loads(self._world_json_bytes())
        self.assertEqual(world['type'], 'Topology')
        self.assertIn('land', world['objects'])
        self.assertIn('countries', world['objects'])

    def test_topojson_client_is_the_pinned_version(self):
        first = (VENDOR / 'topojson-client.min.js').read_text(encoding='utf-8').splitlines()[0]
        self.assertIn('topojson-client v3.1.0', first)

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_map_script_parses(self):
        r = subprocess.run(['node', '--check', str(MAP_JS)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class MapLayoutJsTests(unittest.TestCase):
    """The drawing maths, run in node with the real vendored D3,
    topojson-client and outline."""

    def run_js(self, body: str, payload) -> object:
        script = (
            'const d3 = require(process.argv[1]);'
            'const topojson = require(process.argv[2]);'
            'global.window = {};'
            'require(process.argv[3]);'
            'const world = window.FHA_WORLD;'
            'const m = require(process.argv[4]);'
            'const input = JSON.parse(require("fs").readFileSync(0, "utf8"));'
            + body)
        r = subprocess.run(
            ['node', '-e', script, str(VENDOR / 'd3.min.js'), str(VENDOR / 'topojson-client.min.js'),
             str(VENDOR / 'fha-world.js'), str(MAP_JS)],
            input=json.dumps(payload), capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_regional_pins_use_mercator_and_wide_ones_natural_earth(self):
        kansas = [{'lat': 39.8, 'lon': -95.6}, {'lat': 39.5, 'lon': -96.25}]
        ocean = kansas + [{'lat': 54.1, 'lon': 22.93}]
        out = self.run_js('process.stdout.write(JSON.stringify(input.map(m.projectionKind)));',
                          [kansas, ocean, []])
        self.assertEqual(out, ['mercator', 'naturalEarth', 'naturalEarth'])

    def test_every_pin_lands_inside_the_frame(self):
        cases = {
            'one pin': [{'lat': 39.8, 'lon': -95.6}],
            'one town': [{'lat': 39.8, 'lon': -95.6}, {'lat': 39.81, 'lon': -95.61}],
            'two continents': [{'lat': 39.8, 'lon': -95.6}, {'lat': 54.1, 'lon': 22.93}],
        }
        body = (
            'const out = {};'
            'for (const [k, places] of Object.entries(input)) {'
            '  const p = m.makeProjection(d3, places, 960, 540);'
            '  out[k] = places.map(pl => p([pl.lon, pl.lat]));'
            '}'
            'process.stdout.write(JSON.stringify(out));')
        out = self.run_js(body, cases)
        for name, points in out.items():
            for x, y in points:
                with self.subTest(name):
                    self.assertTrue(40 - 1e-6 <= x <= 920 + 1e-6, (name, x))
                    self.assertTrue(40 - 1e-6 <= y <= 500 + 1e-6, (name, y))
        # One pin does not zoom in without end: two pins a degree apart in
        # the same frame still sit well apart.
        out = self.run_js(
            'const pl = input; const p = m.makeProjection(d3, [pl[0]], 960, 540);'
            'process.stdout.write(JSON.stringify([p([pl[0].lon, pl[0].lat]), p([pl[1].lon, pl[1].lat])]));',
            [{'lat': 39.8, 'lon': -95.6}, {'lat': 39.8, 'lon': -94.6}])
        self.assertGreater(abs(out[1][0] - out[0][0]), 20)
        self.assertLess(abs(out[1][0] - out[0][0]), 600)

    def test_a_journey_turns_into_moves_numbers_and_text(self):
        payload = {
            'places': {'l-a': {'id': 'l-a', 'name': 'Elm Creek', 'lat': 39.5, 'lon': -96.25},
                       'l-b': {'id': 'l-b', 'name': 'Fairview', 'lat': 39.8, 'lon': -95.6}},
            'journey': {'stops': [
                {'place': 'l-a', 'year_from': 1840, 'year_to': 1840, 'what': ['birth']},
                {'place': 'l-b', 'year_from': 1870, 'year_to': 1900,
                 'what': ['census', 'residence']},
                {'place': 'l-a', 'year_from': 1941, 'year_to': 1941, 'what': ['death']}],
                'undated': 2, 'unplaced': 1},
        }
        out = self.run_js(
            'const j = input.journey, by = input.places;'
            'process.stdout.write(JSON.stringify({'
            ' segs: m.journeySegments(j, by),'
            ' text: j.stops.map(s => m.stopText(s, by[s.place])),'
            ' numbers: m.stopNumbers(j),'
            ' left: [m.leftOutText(j), m.leftOutText({undated: 0, unplaced: 1}),'
            '        m.leftOutText({undated: 1, unplaced: 0}), m.leftOutText({})],'
            ' missing: m.journeySegments({stops: [j.stops[0], {place: "l-x"}]}, by)}));',
            payload)
        self.assertEqual(out['segs'], [
            {'from': [-96.25, 39.5], 'to': [-95.6, 39.8], 'year': 1870},
            {'from': [-95.6, 39.8], 'to': [-96.25, 39.5], 'year': 1941}])
        self.assertEqual(out['text'], ['Elm Creek, 1840, birth',
                                       'Fairview, 1870 to 1900, census, residence',
                                       'Elm Creek, 1941, death'])
        self.assertEqual(out['numbers'], [{'place': 'l-a', 'label': '1, 3'},
                                          {'place': 'l-b', 'label': '2'}])
        self.assertEqual(out['left'], [
            'Not on this line: 2 events with no date and 1 at a place with no map position yet.',
            'Not on this line: 1 event at a place with no map position yet.',
            'Not on this line: 1 event with no date.', ''])
        self.assertEqual(out['missing'], [], 'a stop with no pin draws no move')

    def test_the_year_slider_shows_what_had_happened_by_then(self):
        payload = {'journeys': [
            {'stops': [{'place': 'l-a', 'year_from': 1840, 'year_to': 1840},
                       {'place': 'l-b', 'year_from': 1870, 'year_to': 1900}]},
            {'stops': [{'place': 'l-b', 'year_from': 1855, 'year_to': 1855},
                       {'place': 'l-c', 'year_from': 1895, 'year_to': 1895}]}],
            'years': [1840, 1895]}
        out = self.run_js(
            'const ys = input.years;'
            'let y = m.playStart(ys[1], ys); const play = [y];'
            'for (let i = 0; i < 20; i++) { y = m.nextPlayYear(y, ys); if (y === null) break;'
            '  play.push(y); }'
            'play.push(m.playStart(1870, ys));'
            'process.stdout.write(JSON.stringify({'
            ' pins: m.pinYears(input.journeys),'
            ' shown: [m.shownBy(1870, 1870), m.shownBy(1871, 1870), m.shownBy(undefined, 1870),'
            '         m.shownBy(undefined, null), m.shownBy(1999, null)],'
            ' text: [m.yearText(1870, ys), m.yearText(null, ys), m.yearText(1895, ys)],'
            ' play: play}));', payload)
        # A pin shows from the first year a line reaches it.
        self.assertEqual(out['pins'], {'l-a': 1840, 'l-b': 1855, 'l-c': 1895})
        # Up to the year, inclusive; a pin no line reaches waits for "Show all".
        self.assertEqual(out['shown'], [True, False, False, True, True])
        self.assertEqual(out['text'], ['Up to 1870', 'Every year, up to 1895',
                                       'Every year, up to 1895'])
        # Play from the last year starts again at the first, steps a decade
        # at a time and ends on the last year; from a year partway, it goes
        # on from there.
        self.assertEqual(out['play'], [1840, 1850, 1860, 1870, 1880, 1890, 1895, 1870])

    def test_playing_the_years_is_left_out_for_someone_who_wants_less_motion(self):
        out = self.run_js(
            'const win = r => ({matchMedia: q => ({matches: r && q.includes("reduce")})});'
            'process.stdout.write(JSON.stringify([m.motionReduced(win(true)),'
            ' m.motionReduced(win(false)), m.motionReduced(undefined), m.motionReduced({})]));',
            {})
        self.assertEqual(out, [True, False, False, False])

    def test_the_outline_turns_into_shapes(self):
        out = self.run_js(
            'const land = topojson.feature(world, world.objects.land);'
            'const borders = topojson.mesh(world, world.objects.countries, (a, b) => a !== b);'
            'process.stdout.write(JSON.stringify([land.type, borders.type, m.pinRadius(1),'
            ' m.pinRadius(100), m.pinRadius(100000)]));', None)
        self.assertEqual(out[:2], ['FeatureCollection', 'MultiLineString'])
        self.assertLess(out[2], out[3])
        self.assertEqual(out[4], 12)


if __name__ == '__main__':
    unittest.main()
