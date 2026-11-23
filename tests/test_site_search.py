"""
test_site_search.py: the search box on every page of the family site
(TOOLING 12 "Pages", "Search").

What a relative sees: one box at the top of every page. Typing "Kowalsky"
finds Kowalski, typing "Hartley" also finds Margaret Cole because she married
Thomas Hartley, and it works from a USB stick with no internet. The words it
searches ship as one file every visitor downloads (`data/search-data.js`), so
that file must hold nothing a page on the shared site does not already show:
no living, unknown, restricted or by-request person, no private name, no
married surname through a hidden spouse or a private marriage record, no
private record, and no count of who was left out.

Builds a synthetic .cache/index.sqlite from index.py's DDL, the way
tests/test_site_rings_calendar.py does. Temp fixtures only. The JavaScript
half is checked with node where node is installed (CI has none, so those
tests skip there).
"""

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
from _lib import index_manifest_path, record_path_manifest, write_path_manifest

# site.py's stem collides with the stdlib `site` module, so it is loaded by
# path under a private name (as tests/test_site.py and fha.py do).
_spec = importlib.util.spec_from_file_location('fha_site_search', ROOT / 'tools' / 'site.py')
site = importlib.util.module_from_spec(_spec)
sys.modules['fha_site_search'] = site
_spec.loader.exec_module(site)

SEARCH_JS = ROOT / 'tools' / 'templates' / 'vendor' / 'fha-search.js'

MARGARET = 'p-aaaaaaaaaa'   # deceased, married Thomas
THOMAS = 'p-bbbbbbbbbb'     # deceased
JOSEFA = 'p-cccccccccc'     # deceased, accented name
LIVE = 'p-dddddddddd'       # living: true
UNK = 'p-eeeeeeeeee'        # living: unknown
SECRET = 'p-ffffffffff'     # deceased, restricted: true
QUIET = 'p-gggggggggg'      # deceased, restricted: by-request
EDITH = 'p-hhhhhhhhhh'      # deceased, married to the living man
STUB = 'p-jjjjjjjjjj'       # deceased stub (no page on a shared site)

ELM_CREEK = 'l-aaaaaaaaaa'
KANSAS = 'l-bbbbbbbbbb'

# Every withheld person, with the words that would give them away: their
# name, each part of it, their id and the year they were born. None of these
# may appear anywhere in the shared search file.
WITHHELD = {
    LIVE: ('Walter Livesey', '1951'),
    UNK: ('Ursula Unwin', '1932'),
    SECRET: ('Rupert Sequester', '1843'),
    QUIET: ('Prudence Quayle', '1847'),
}


def _js_data(text: str) -> dict:
    """The JSON inside `window.FHA_SEARCH = {...};`."""
    prefix = 'window.FHA_SEARCH = '
    assert text.startswith(prefix), text[:60]
    body = text[len(prefix):].rstrip()
    assert body.endswith(';'), body[-20:]
    return json.loads(body[:-1])


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
        self.config('')

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    # Seeding

    def config(self, text: str) -> None:
        (self.root / 'fha.yaml').write_text('roots: {}\n' + text, encoding='utf-8')

    def person(self, pid, name, *, living='false', tier='curated', sex='F', extra=''):
        surname = name.split()[-1]
        rel = f'people/{surname.lower()}_{pid}.md'
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'---\nid: {pid}\nname: {name}\nliving: {living}\n{extra}---\n# {name}\n',
                        encoding='utf-8')
        self.conn.execute(
            'INSERT INTO persons(id, name, surname, sex, living, tier, status, path) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (pid, name, surname, sex, living, tier, 'active', rel))

    def source(self, sid, title, *, source_type='vital-record', restricted=0, extra='',
               restricted_claims=(), date=None):
        rel = f'sources/{source_type}/src_{sid}.md'
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = ['---', f'id: {sid}', f'title: "{title}"', f'source_type: {source_type}',
                 'citation: "A citation."']
        if extra:
            lines.append(extra.rstrip('\n'))
        lines += ['---', '', '## Claims', '```yaml']
        for cid in restricted_claims:
            lines += [f'- id: {cid}', '  type: marriage', '  value: "private"',
                      '  status: accepted', '  restricted: true']
        lines.append('```')
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        self.conn.execute(
            'INSERT INTO sources(id, title, source_type, date_edtf, restricted, status, path) '
            'VALUES (?,?,?,?,?,?,?)',
            (sid, title, source_type, date, restricted, 'active', rel))

    def claim(self, cid, sid, ctype, date, persons, *, status='accepted', roles=None):
        lo = (date + '-01-01')[:10] if date else None
        self.conn.execute(
            'INSERT INTO claims(id, source_id, type, value, status, date_edtf, date_min, '
            'date_max) VALUES (?,?,?,?,?,?,?,?)',
            (cid, sid, ctype, f'{ctype} record', status, date, lo, lo))
        for pos, pid in enumerate(persons):
            self.conn.execute(
                'INSERT INTO claim_persons(claim_id, person_id, position, role) '
                'VALUES (?,?,?,?)', (cid, pid, pos, (roles or {}).get(pid)))

    def marriage(self, cid, sid, a, b, *, status='accepted'):
        """A marriage claim naming both spouses, and the two spouse edges
        `fha index` derives from it."""
        self.claim(cid, sid, 'marriage', '1866', [a, b], status=status,
                   roles={a: 'spouse', b: 'spouse'})
        for x, y in ((a, b), (b, a)):
            self.conn.execute(
                'INSERT INTO relationships(person_id, rel, other_id, claim_id) VALUES (?,?,?,?)',
                (x, 'spouse', y, cid))

    def birth(self, cid, sid, pid, year):
        self.claim(cid, sid, 'birth', year, [pid])

    def place(self, lid, name, *, within=None, alt=()):
        self.conn.execute('INSERT INTO places(id, name, hierarchy, within) VALUES (?,?,?,?)',
                          (lid, name, name, within))
        for a in alt:
            self.conn.execute('INSERT INTO place_names(place_id, alt_name) VALUES (?,?)', (lid, a))

    def build(self, *, linked=False, out=None, dry_run=False, today='2026-06-15'):
        self.conn.commit()
        future = time.time() + 5
        os.utime(self.root / '.cache' / 'index.sqlite', (future, future))
        write_path_manifest(index_manifest_path(self.root), record_path_manifest(self.root))
        with unittest.mock.patch.object(site, '_today', return_value=today):
            return site.run_site(self.root, out or self.out, linked=linked, dry_run=dry_run)

    def data_text(self, out=None) -> str:
        return ((out or self.out) / 'data' / 'search-data.js').read_text(encoding='utf-8')

    def data(self, out=None) -> dict:
        return _js_data(self.data_text(out))

    def person_entry(self, name, out=None):
        for p in self.data(out)['people']:
            if p['name'] == name:
                return p
        return None

    def seed_couple(self):
        """Thomas Hartley and Margaret Cole, married on an accepted record,
        with birth and death years."""
        self.person(MARGARET, 'Margaret Cole')
        self.person(THOMAS, 'Thomas Edward Hartley', sex='M')
        self.source('s-aaaaaaaaaa', 'Breton County marriages')
        self.marriage('c-aaaaaaaaaa', 's-aaaaaaaaaa', MARGARET, THOMAS)
        self.source('s-bbbbbbbbbb', 'Margaret Cole birth register')
        self.birth('c-bbbbbbbbbb', 's-bbbbbbbbbb', MARGARET, '1845')
        self.claim('c-cccccccccc', 's-bbbbbbbbbb', 'death', '1921', [MARGARET])


class FoldTests(unittest.TestCase):
    """One spelling rule, used to build the search file and to read a query:
    accents fold the way filenames already do, a few old-record spellings
    meet (w/v, y/i, sz/sh, cz/ch, sch/sh, tsch/ch, ph/f), doubled letters
    count once."""

    def test_fold_rules(self):
        cases = {
            'Kowalski': ['kovalski'], 'Kowalsky': ['kovalski'], 'Kovalski': ['kovalski'],
            'Szymański': ['shimanski'], 'Wiśniewska': ['visnievska'], 'Müller': ['muler'],
            'Schmitt': ['shmit'], 'Łódź': ['lodz'], 'Philipp Tscherny': ['filip', 'cherni'],
            'Czech': ['chech'], "O'Brien-Hartley": ['o', 'brien', 'hartlei'],
            '1880 census, Elm Creek': ['1880', 'census', 'elm', 'crek'], '': [],
        }
        for text, tokens in cases.items():
            with self.subTest(text):
                self.assertEqual(site._search_fold(text), tokens)

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_js_fold_matches_python_fold(self):
        names = ['Kowalski', 'Kowalsky', 'Szymański', 'Wiśniewska', 'Müller', 'Schmitt',
                 'Łódź', 'Philipp Tscherny', 'Þórður Ægisson', 'Straße', 'İstanbul',
                 'Đorđe Øster', "O'Brien-Hartley", 'Ann  Price', 'Sinéad Ní Bhriain',
                 'Иван Петров', 'Zoë Ŧest', '1880 census, Elm Creek', '']
        table = site._search_fold_table()
        script = (
            'const s = require(process.argv[1]);'
            'const input = JSON.parse(require("fs").readFileSync(0, "utf8"));'
            'process.stdout.write(JSON.stringify(input.names.map('
            'n => s.fold(n, input.table))));')
        r = subprocess.run(['node', '-e', script, str(SEARCH_JS)],
                           input=json.dumps({'names': names, 'table': table}),
                           capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), [site._search_fold(n) for n in names])

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_search_script_parses(self):
        r = subprocess.run(['node', '--check', str(SEARCH_JS)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class SwitchTests(_Base):
    def seed(self):
        self.seed_couple()
        self.place(ELM_CREEK, 'Elm Creek')

    def html_pages(self):
        return sorted(p for p in self.out.rglob('*.html'))

    def test_search_is_on_by_default_and_off_when_asked(self):
        self.seed()
        self.build()
        self.assertTrue((self.out / 'search.html').is_file())
        self.assertTrue((self.out / 'data' / 'search-data.js').is_file())
        for page in self.html_pages():
            rel = page.relative_to(self.out)
            prefix = '..' if len(rel.parts) > 1 else '.'
            with self.subTest(str(rel)):
                self.assertIn(f'action="{prefix}/search.html"', page.read_text(encoding='utf-8'))

        self.config('site:\n  search: false\n')
        self.build()
        self.assertFalse((self.out / 'search.html').exists(),
                         'turning search off must remove the old page')
        self.assertFalse((self.out / 'data' / 'search-data.js').exists())
        for page in self.html_pages():
            with self.subTest(str(page.relative_to(self.out))):
                self.assertNotIn('site-search', page.read_text(encoding='utf-8'))

    def test_an_unreadable_value_leaves_search_off_with_a_warning(self):
        self.seed()
        self.config('site:\n  search: maybe\n')
        result = self.build()
        self.assertFalse((self.out / 'search.html').exists())
        self.assertTrue(any('site.search' in m for m in result['messages']), result['messages'])

    def test_page_counts_match_with_and_without_search(self):
        self.seed()
        for cfg in ('', 'site:\n  search: false\n'):
            with self.subTest(cfg or 'default'):
                self.config(cfg)
                preview = self.build(dry_run=True)['pages']
                real = self.build()['pages']
                self.assertEqual(preview, real)
                self.assertEqual(real, len(list(self.out.rglob('*.html'))))

    def test_search_page_loads_its_data_and_script_from_the_site(self):
        self.seed()
        self.build()
        page = (self.out / 'search.html').read_text(encoding='utf-8')
        self.assertIn('<script src="data/search-data.js"></script>', page)
        self.assertIn('<script src="vendor/fha-search.js"></script>', page)
        self.assertTrue((self.out / 'vendor' / 'fha-search.js').is_file())
        self.assertNotIn('http', page, 'search works offline: nothing is fetched')


class PrivacyTests(_Base):
    """The shared search file holds nothing a shared page does not show."""

    def seed_everyone(self):
        self.seed_couple()
        self.person(LIVE, WITHHELD[LIVE][0], living='true', sex='M')
        self.person(UNK, WITHHELD[UNK][0], living='unknown')
        self.person(SECRET, WITHHELD[SECRET][0], sex='M', extra='restricted: true\n')
        self.person(QUIET, WITHHELD[QUIET][0], extra='restricted: by-request\n')
        for n, pid in enumerate((LIVE, UNK, SECRET, QUIET)):
            sid = f's-{"kmnpq"[n] * 10}'
            self.source(sid, f'Register volume {"ABCD"[n]}')
            self.birth(f'c-{"kmnpq"[n] * 10}', sid, pid, WITHHELD[pid][1])
        # Edith, deceased, married the living man: his surname must not find her.
        self.person(EDITH, 'Edith Brown')
        self.source('s-rrrrrrrrrr', 'Brown marriage')
        self.marriage('c-rrrrrrrrrr', 's-rrrrrrrrrr', EDITH, LIVE)
        # A record about the unknown-status person and a deceased one: withheld
        # whole on a shared site, because it names her.
        self.source('s-ssssssssss', 'Unwin family letter')
        self.claim('c-ssssssssss', 's-ssssssssss', 'residence', '1960', [UNK, THOMAS])
        # A private record and a by-request record, each about Thomas.
        self.source('s-tttttttttt', 'Sealed adoption file', restricted=1)
        self.claim('c-tttttttttt', 's-tttttttttt', 'residence', '1870', [THOMAS])
        self.source('s-vvvvvvvvvv', 'Pension bundle', extra='restricted: by-request')
        self.claim('c-vvvvvvvvvv', 's-vvvvvvvvvv', 'residence', '1880', [THOMAS])
        self.source('s-wwwwwwwwww', 'Genetic match list', source_type='dna')
        self.claim('c-wwwwwwwwww', 's-wwwwwwwwww', 'residence', '1890', [THOMAS])

    def test_no_withheld_person_record_or_count_is_in_the_shared_file(self):
        self.seed_everyone()
        self.build()
        text = self.data_text()
        folded = ' '.join(t for p in self.data()['people'] for t in p['t'].split())
        for pid, (name, year) in WITHHELD.items():
            with self.subTest(name):
                self.assertNotIn(pid, text)
                self.assertNotIn(year, text)
                for word in name.split():
                    self.assertNotIn(word, text)
                    self.assertNotIn(word.lower(), text.lower())
                    for tok in site._search_fold(word):
                        self.assertNotIn(tok, folded)
        for title in ('Unwin family letter', 'Sealed adoption file', 'Pension bundle',
                      'Genetic match list', 'Register volume'):
            self.assertNotIn(title, text)
        names = sorted(p['name'] for p in self.data()['people'])
        self.assertEqual(names, ['Edith Brown', 'Margaret Cole', 'Thomas Edward Hartley'])
        self.assertEqual(self.person_entry('Edith Brown').get('m', []), [])
        # No count of anyone left out, anywhere in the data or on the page.
        data = self.data()
        self.assertEqual(set(data), {'v', 'fold', 'people', 'places', 'sources'})
        for p in data['people']:
            self.assertLessEqual(set(p), {'name', 'years', 'href', 't', 'm'})
        page = (self.out / 'search.html').read_text(encoding='utf-8')
        self.assertNotRegex(page, r'\b[0-9]+ (more|other|hidden|living)\b')

    def test_a_restricted_name_variant_is_not_a_search_token(self):
        self.person(MARGARET, 'Margaret Cole', extra=(
            'name_variants:\n  - value: Peggy Smith\n    restricted: true\n'
            '  - Maggie Cole\n'
            'name_at_birth:\n  value: Margaret Doyle\n  restricted: true\n'
            'married_name: Margaret Hartley\n'))
        self.build()
        entry = self.person_entry('Margaret Cole')
        tokens = entry['t'].split()
        self.assertIn('magie', tokens)
        self.assertIn('hartlei', tokens)
        for private in ('pegi', 'smith', 'doile'):
            self.assertNotIn(private, tokens)
        text = self.data_text()
        for word in ('Peggy', 'Smith', 'Doyle'):
            self.assertNotIn(word, text)

    def test_a_merged_record_that_says_living_keeps_the_survivor_out(self):
        self.seed_couple()
        self.person('p-kkkkkkkkkk', 'Margaret Coles', living='true')
        self.conn.execute("UPDATE persons SET status = 'merged', merged_into = ? WHERE id = ?",
                          (MARGARET, 'p-kkkkkkkkkk'))
        self.build()
        self.assertIsNone(self.person_entry('Margaret Cole'))
        self.assertNotIn('Margaret', self.data_text())


class MarriedNameTests(_Base):
    def test_married_surname_from_an_accepted_marriage_finds_her(self):
        self.seed_couple()
        self.build()
        entry = self.person_entry('Margaret Cole')
        self.assertEqual(entry['m'], [{'t': 'hartlei', 'label': 'married Thomas Edward Hartley'}])
        self.assertEqual(entry['years'], '1845-1921')
        self.assertEqual(entry['href'], f'persons/{MARGARET}.html')
        # And the other way: his entry carries her surname.
        self.assertEqual(self.person_entry('Thomas Edward Hartley')['m'],
                         [{'t': 'cole', 'label': 'married Margaret Cole'}])

    def test_no_married_surname_through_a_living_spouse(self):
        self.person(EDITH, 'Edith Brown')
        self.person(LIVE, 'Walter Livesey', living='true', sex='M')
        self.source('s-aaaaaaaaaa', 'Brown marriage')
        self.marriage('c-aaaaaaaaaa', 's-aaaaaaaaaa', EDITH, LIVE)
        self.build()
        self.assertEqual(self.person_entry('Edith Brown').get('m', []), [])
        self.assertNotIn('Livesey', self.data_text())

    def test_no_married_surname_from_a_restricted_marriage_claim(self):
        self.person(MARGARET, 'Margaret Cole')
        self.person(THOMAS, 'Thomas Edward Hartley', sex='M')
        self.source('s-aaaaaaaaaa', 'Breton County marriages', restricted_claims=('c-aaaaaaaaaa',))
        self.marriage('c-aaaaaaaaaa', 's-aaaaaaaaaa', MARGARET, THOMAS)
        self.build()
        self.assertEqual(self.person_entry('Margaret Cole').get('m', []), [])

    def test_no_married_surname_from_a_private_marriage_record(self):
        self.person(MARGARET, 'Margaret Cole')
        self.person(THOMAS, 'Thomas Edward Hartley', sex='M')
        self.source('s-aaaaaaaaaa', 'Breton County marriages', extra='restricted: by-request')
        self.marriage('c-aaaaaaaaaa', 's-aaaaaaaaaa', MARGARET, THOMAS)
        self.build()
        self.assertEqual(self.person_entry('Margaret Cole').get('m', []), [])

    def test_no_married_surname_from_an_unaccepted_marriage_in_a_shared_build(self):
        self.person(MARGARET, 'Margaret Cole')
        self.person(THOMAS, 'Thomas Edward Hartley', sex='M')
        self.source('s-aaaaaaaaaa', 'Breton County marriages')
        self.marriage('c-aaaaaaaaaa', 's-aaaaaaaaaa', MARGARET, THOMAS, status='suggested')
        self.build()
        self.assertEqual(self.person_entry('Margaret Cole').get('m', []), [])

    def test_a_shared_surname_adds_no_married_token(self):
        self.person(MARGARET, 'Margaret Hartley')
        self.person(THOMAS, 'Thomas Edward Hartley', sex='M')
        self.source('s-aaaaaaaaaa', 'Breton County marriages')
        self.marriage('c-aaaaaaaaaa', 's-aaaaaaaaaa', MARGARET, THOMAS)
        self.build()
        self.assertEqual(self.person_entry('Margaret Hartley').get('m', []), [])


class ContentTests(_Base):
    def test_places_and_records_are_listed_with_their_other_names(self):
        self.seed_couple()
        self.place(KANSAS, 'Kansas')
        self.place(ELM_CREEK, 'Elm Creek', within=KANSAS, alt=('Ulmenbach',))
        self.source('s-cccccccccc', '1880 census, Elm Creek', date='1880-06')
        self.source('s-dddddddddd', 'Family notes, circa 1940', date='1940~')
        self.build()
        data = self.data()
        elm = next(p for p in data['places'] if p['name'] == 'Elm Creek')
        self.assertEqual(elm['href'], f'places/{ELM_CREEK}.html')
        self.assertEqual(elm['within'], 'Kansas')
        self.assertEqual(elm['t'], 'elm crek ulmenbach')
        census = next(s for s in data['sources'] if s['title'] == '1880 census, Elm Creek')
        self.assertEqual(census['year'], '1880')
        self.assertEqual(census['href'], 'sources/s-cccccccccc.html')
        notes = next(s for s in data['sources'] if s['title'] == 'Family notes, circa 1940')
        self.assertEqual(notes['year'], '1940', 'the year as the record writes it')
        self.assertEqual(data['fold']['pairs'][0], ['tsch', 'ch'])
        self.assertEqual(data['fold']['chars']['ł'], 'l')

    def test_a_stub_has_no_entry_on_the_shared_site(self):
        self.seed_couple()
        self.person(STUB, 'Ann Price', tier='stub')
        self.build()
        self.assertIsNone(self.person_entry('Ann Price'))

    def test_data_is_sorted_and_byte_identical(self):
        self.seed_couple()
        self.person(JOSEFA, 'Józefa Kowalska')
        self.person(EDITH, 'Edith Brown')
        self.build()
        other = self.root / 'site2'
        self.build(out=other)
        for rel in ('data/search-data.js', 'search.html'):
            with self.subTest(rel):
                self.assertEqual((self.out / rel).read_bytes(), (other / rel).read_bytes())
        names = [p['name'] for p in self.data()['people']]
        self.assertEqual(names, sorted(names, key=str.lower))
        self.assertNotIn(b'\r\n', (self.out / 'data' / 'search-data.js').read_bytes())
        self.assertIn('Józefa', self.data_text(), 'names keep their own spelling')

    def test_data_cannot_close_its_script(self):
        self.source('s-cccccccccc', '</script><b>bold</b> ')
        self.build()
        text = self.data_text()
        self.assertNotIn('</script', text.lower())
        self.assertNotIn('<b>', text)
        self.assertNotIn(' ', text)
        titles = [s['title'] for s in self.data()['sources']]
        self.assertIn('</script><b>bold</b> ', titles)

    def test_linked_build_lists_everyone_and_drops_the_living_note(self):
        self.seed_couple()
        self.person(LIVE, 'Walter Livesey', living='true', sex='M')
        self.build()
        self.assertIn('Living relatives are not listed',
                      (self.out / 'search.html').read_text(encoding='utf-8'))
        self.build(linked=True)
        self.assertIsNotNone(self.person_entry('Walter Livesey'))
        self.assertNotIn('Living relatives are not listed',
                         (self.out / 'search.html').read_text(encoding='utf-8'))


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class QueryTests(_Base):
    """The query side, run in node over a real build's search file."""

    def query(self, q):
        script = (
            'const s = require(process.argv[1]);'
            'const input = JSON.parse(require("fs").readFileSync(0, "utf8"));'
            'process.stdout.write(JSON.stringify(s.search(input.data, input.q)));')
        r = subprocess.run(['node', '-e', script, str(SEARCH_JS)],
                           input=json.dumps({'data': self.data(), 'q': q}),
                           capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def seed(self):
        self.seed_couple()
        self.person(JOSEFA, 'Józefa Kowalski')
        self.place(ELM_CREEK, 'Elm Creek')
        self.source('s-cccccccccc', '1880 census, Elm Creek', date='1880-06')

    def test_a_spelling_difference_still_finds_her(self):
        self.seed()
        self.build()
        people = self.query('kowalsky')['people']
        self.assertEqual([p['name'] for p in people], ['Józefa Kowalski'])
        self.assertEqual([p['name'] for p in self.query('Jozefa')['people']], ['Józefa Kowalski'])

    def test_a_married_surname_finds_her_after_his_own_entry(self):
        self.seed()
        self.build()
        people = self.query('hartley')['people']
        self.assertEqual([p['name'] for p in people], ['Thomas Edward Hartley', 'Margaret Cole'])
        self.assertEqual(people[1]['married'], 'married Thomas Edward Hartley')
        self.assertIsNone(people[0].get('married'))

    def test_every_word_must_match_and_groups_are_separate(self):
        self.seed()
        self.build()
        self.assertEqual([p['name'] for p in self.query('margaret hart')['people']],
                         ['Margaret Cole'])
        self.assertEqual(self.query('margaret kansas')['people'], [])
        found = self.query('elm')
        self.assertEqual([p['name'] for p in found['places']], ['Elm Creek'])
        self.assertEqual([s['title'] for s in found['sources']], ['1880 census, Elm Creek'])

    def test_one_letter_finds_nothing(self):
        self.seed()
        self.build()
        found = self.query('m')
        self.assertEqual((found['people'], found['places'], found['sources']), ([], [], []))

    def test_a_name_typed_partway_through_a_folded_spelling_still_finds_him(self):
        # The box searches as she types. "Step" is on its way to "Stephen",
        # whose shipped word is "stefen": stopping after the p must not say
        # "Nothing matches". Same for tsch (Deutsch) and ph (Raphael).
        self.person(MARGARET, 'Stephen Cole', sex='M')
        self.person(THOMAS, 'Hans Deutsch', sex='M')
        self.person(JOSEFA, 'Raphael Hartley', sex='M')
        self.build()
        for typed, name in (('Step', 'Stephen Cole'), ('Deut', 'Hans Deutsch'),
                            ('Deuts', 'Hans Deutsch'), ('Deutsc', 'Hans Deutsch'),
                            ('Rap', 'Raphael Hartley'), ('step cole', 'Stephen Cole')):
            with self.subTest(typed):
                self.assertEqual([p['name'] for p in self.query(typed)['people']], [name])
        # Completing the pair adds only names that carry it: "Cos" is not Cole.
        self.assertEqual(self.query('Cos')['people'], [])

    def test_a_name_joined_or_split_at_an_apostrophe_finds_her(self):
        self.person(MARGARET, "Mary O'Brien")
        self.build()
        for typed in ('OBrien', "O'Brien", 'O Brien', 'brien', 'mary obr'):
            with self.subTest(typed):
                self.assertEqual([p['name'] for p in self.query(typed)['people']],
                                 ["Mary O'Brien"])

    def test_a_two_letter_name_that_folds_to_one_letter_is_found(self):
        self.person(MARGARET, 'Soon Yi')
        self.person(THOMAS, 'Ivan Cole', sex='M')
        self.build()
        self.assertEqual([p['name'] for p in self.query('Yi')['people']][0], 'Soon Yi')

    def test_a_whole_word_ranks_above_a_prefix(self):
        self.person(MARGARET, 'Ann Coleman')
        self.person(THOMAS, 'John Cole', sex='M')
        self.build()
        self.assertEqual([p['name'] for p in self.query('cole')['people']],
                         ['John Cole', 'Ann Coleman'])


if __name__ == '__main__':
    unittest.main()
