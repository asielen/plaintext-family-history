"""
test_site_statistics.py: the optional "Family statistics" page (TOOLING 12,
"Optional panels").

What a relative sees when the owner turns `site: statistics: true` on: one page
that says how long the family lived, how old they were when they married and
how many children they had, generation by generation, plus the commonest
names. Off unless asked for. The page counts only people the site already
shows, and never a living, unknown, restricted or by-request person, so a
visitor cannot work out who was left out from a count.

Builds a synthetic .cache/index.sqlite from index.py's DDL, the way
tests/test_site_search.py does. Temp fixtures only.
"""

import importlib.util
import os
import re
import sqlite3
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

_spec = importlib.util.spec_from_file_location('fha_site_statistics', ROOT / 'tools' / 'site.py')
site = importlib.util.module_from_spec(_spec)
sys.modules['fha_site_statistics'] = site
_spec.loader.exec_module(site)


def _letters(n: int) -> str:
    """Ten lowercase letters that differ for each n (ids are `p-` plus ten)."""
    out = ''
    for _ in range(10):
        out = chr(ord('a') + n % 26) + out
        n //= 26
    return out


def _text(page: Path) -> str:
    """The page with tags dropped and whitespace squeezed, to read numbers from."""
    raw = page.read_text(encoding='utf-8')
    raw = re.sub(r'<(script|style)\b.*?</\1>', ' ', raw, flags=re.S)
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', raw))


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
        self.config('site:\n  statistics: true\n')
        self.n = 0
        self.source_id = 's-' + 'z' * 10
        self._source_made = False

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def config(self, text: str) -> None:
        (self.root / 'fha.yaml').write_text('roots: {}\n' + text, encoding='utf-8')

    def _next(self) -> int:
        self.n += 1
        return self.n

    def ensure_source(self, sid=None) -> str:
        if sid:
            # A source of its own, so a record that names a withheld person
            # cannot hold back the claims of everyone else.
            rel = f'sources/vital-record/src_{sid}.md'
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                '\n'.join(['---', f'id: {sid}', f'title: "Register {sid}"',
                           'source_type: vital-record', 'citation: "A citation."', '---', '',
                           '## Claims', '```yaml', '```', '']), encoding='utf-8')
            self.conn.execute(
                'INSERT INTO sources(id, title, source_type, restricted, status, path) '
                'VALUES (?,?,?,?,?,?)', (sid, f'Register {sid}', 'vital-record', 0, 'active', rel))
            return sid
        if not self._source_made:
            rel = 'sources/vital-record/src_one.md'
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f'---\nid: {self.source_id}\ntitle: "County register"\nsource_type: vital-record\n'
                'citation: "A citation."\n---\n\n## Claims\n```yaml\n```\n', encoding='utf-8')
            self.conn.execute(
                'INSERT INTO sources(id, title, source_type, restricted, status, path) '
                'VALUES (?,?,?,?,?,?)',
                (self.source_id, 'County register', 'vital-record', 0, 'active', rel))
            self._source_made = True
        return self.source_id

    def person(self, name, sex='F', *, living='false', born=None, died=None,
               married=None, extra='') -> str:
        pid = 'p-' + _letters(self._next())
        surname = name.split()[-1]
        rel = f'people/{surname.lower()}_{pid}.md'
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'---\nid: {pid}\nname: {name}\nliving: {living}\n{extra}---\n# {name}\n',
                        encoding='utf-8')
        self.conn.execute(
            'INSERT INTO persons(id, name, surname, sex, living, tier, status, path) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (pid, name, surname, sex, living, 'curated', 'active', rel))
        sid = None
        if living != 'false' or extra:
            sid = self.ensure_source('s-' + _letters(self._next()))
        if born:
            self.claim('birth', born, [pid], sid=sid)
        if died:
            self.claim('death', died, [pid], sid=sid)
        if married:
            self.claim('marriage', married, [pid], roles={pid: 'spouse'}, sid=sid)
        return pid

    def claim(self, ctype, date, persons, *, roles=None, sid=None) -> str:
        cid = 'c-' + _letters(self._next())
        sid = sid or self.ensure_source()
        lo = (date + '-01-01')[:10]
        self.conn.execute(
            'INSERT INTO claims(id, source_id, type, value, status, date_edtf, date_min, '
            'date_max) VALUES (?,?,?,?,?,?,?,?)',
            (cid, sid, ctype, f'{ctype} record', 'accepted', date, lo, lo))
        for pos, pid in enumerate(persons):
            self.conn.execute(
                'INSERT INTO claim_persons(claim_id, person_id, position, role) VALUES (?,?,?,?)',
                (cid, pid, pos, (roles or {}).get(pid)))
        return cid

    def parent_of(self, child, *parents):
        cid = self.claim('birth', '1900', [child, *parents],
                         roles={child: 'child', **{p: 'parent' for p in parents}})
        # A second birth claim for the child would out-rank nothing: the
        # child's own vital comes from `roles`. Keep the date vague so it is
        # never mistaken for the child's picked birth year.
        self.conn.execute('UPDATE claims SET date_edtf = ?, date_min = ?, date_max = ? WHERE id = ?',
                          ('19XX', '1900-01-01', '1999-12-31', cid))
        for p in parents:
            for x, rel, y in ((child, 'parent', p), (p, 'child', child)):
                self.conn.execute(
                    'INSERT INTO relationships(person_id, rel, other_id, claim_id) VALUES (?,?,?,?)',
                    (x, rel, y, cid))

    def lives(self, century, ages, sex='F', prefix='Mary'):
        """One deceased person per age, born in the given century, died at that age."""
        out = []
        for i, age in enumerate(ages):
            born = century + 10 + i
            out.append(self.person(f'{prefix} Hale', sex,
                                   born=str(born), died=str(born + age)))
        return out

    def build(self, *, linked=False, dry_run=False, today='2026-06-15'):
        self.conn.commit()
        future = time.time() + 5
        os.utime(self.root / '.cache' / 'index.sqlite', (future, future))
        write_path_manifest(index_manifest_path(self.root), record_path_manifest(self.root))
        with unittest.mock.patch.object(site, '_today', return_value=today):
            return site.run_site(self.root, self.out, linked=linked, dry_run=dry_run)

    def page(self) -> str:
        return _text(self.out / 'statistics.html')


class SwitchTests(_Base):
    def test_off_by_default_and_not_linked_from_the_home_page(self):
        self.lives(1800, [60, 64, 70])
        self.config('')
        self.build()
        self.assertFalse((self.out / 'statistics.html').exists())
        self.assertNotIn('statistics.html', (self.out / 'index.html').read_text(encoding='utf-8'))

    def test_on_writes_the_page_and_links_it_under_people(self):
        self.lives(1800, [60, 64, 70])
        self.build()
        self.assertTrue((self.out / 'statistics.html').is_file())
        home = (self.out / 'index.html').read_text(encoding='utf-8')
        self.assertIn('href="statistics.html"', home)
        self.assertIn('Family statistics', home)
        self.assertIn('Worked out from the dates on this site.', self.page())

    def test_switching_off_removes_the_old_page(self):
        self.lives(1800, [60, 64, 70])
        self.build()
        self.config('site:\n  statistics: false\n')
        self.build()
        self.assertFalse((self.out / 'statistics.html').exists())

    def test_page_counts_match_the_pages_written(self):
        self.lives(1800, [60, 64, 70])
        for cfg in ('', 'site:\n  statistics: true\n'):
            with self.subTest(cfg or 'default'):
                self.config(cfg)
                preview = self.build(dry_run=True)['pages']
                real = self.build()['pages']
                self.assertEqual(preview, real)
                self.assertEqual(real, len(list(self.out.rglob('*.html'))))

    def test_two_builds_write_the_same_bytes(self):
        self.lives(1800, [60, 64, 70])
        self.build()
        first = (self.out / 'statistics.html').read_bytes()
        self.build()
        self.assertEqual(first, (self.out / 'statistics.html').read_bytes())


class AgeTests(_Base):
    def test_median_age_at_death_by_century_of_birth(self):
        self.lives(1800, [50, 64, 90])        # median 64
        self.lives(1700, [30, 40, 80, 90])    # median 60
        self.build()
        text = self.page()
        self.assertIn('Born in the 1800s 64 years 3 people', text)
        self.assertIn('Born in the 1700s 60 years 4 people', text)

    def test_a_birth_known_only_to_the_decade_is_not_counted(self):
        self.lives(1800, [50, 64, 90])
        self.person('Vague Hale', 'M', born='184X', died='1900')
        self.build()
        self.assertIn('Born in the 1800s 64 years 3 people', self.page())

    def test_an_about_date_is_counted(self):
        self.lives(1800, [50, 64])
        self.person('About Hale', 'M', born='1850~', died='1914')    # 64 years
        self.build()
        self.assertIn('Born in the 1800s 64 years 3 people', self.page())

    def test_fewer_than_three_people_is_too_few_to_say(self):
        self.lives(1800, [50, 64, 90])
        self.lives(1700, [30, 40])
        self.build()
        self.assertIn('Born in the 1700s Too few to say. 2 people', self.page())

    def test_one_long_life_does_not_move_the_median(self):
        self.lives(1800, [60, 62, 64, 66, 118])
        self.build()
        self.assertIn('Born in the 1800s 64 years 5 people', self.page())

    def test_age_at_first_marriage_by_sex(self):
        for i, age in enumerate([20, 22, 24]):
            born = 1810 + i
            self.person(f'Wife{chr(97 + i)} Hale', 'F', born=str(born), died=str(born + 70),
                        married=str(born + age))
        for i, age in enumerate([26, 28, 30]):
            born = 1810 + i
            self.person(f'Husband{chr(97 + i)} Hale', 'M', born=str(born), died=str(born + 70),
                        married=str(born + age))
        self.person('Nosex Hale', 'U', born='1811', died='1880', married='1830')
        self.build()
        text = self.page()
        self.assertRegex(text, r'Women .*Born in the 1800s 22 years 3 people')
        self.assertRegex(text, r'Men .*Born in the 1800s 28 years 3 people')
        self.assertIn('1 person with no recorded sex is not counted here.', text)


class PrivacyTests(_Base):
    def test_living_unknown_and_restricted_people_are_never_counted(self):
        self.lives(1800, [50, 64, 90])
        self.person('Alive Hale', 'M', living='true', born='1801', died='1921')
        self.person('Maybe Hale', 'M', living='unknown', born='1802', died='1922')
        self.person('Secret Hale', 'M', born='1803', died='1923', extra='restricted: true\n')
        self.person('Quiet Hale', 'M', born='1804', died='1924', extra='restricted: by-request\n')
        for linked in (False, True):
            with self.subTest(linked=linked):
                self.build(linked=linked)
                text = self.page()
                self.assertIn('Born in the 1800s 64 years 3 people', text)
                for gone in ('Alive', 'Maybe', 'Secret', 'Quiet'):
                    self.assertNotIn(gone, text)

    def test_children_per_family_ignores_children_who_are_not_shown(self):
        def couple(kids, *, mum_living=False, living_kids=0):
            dad = self.person('Paul Hale', 'M', born='1800', died='1870')
            mum = self.person('Anna Hale', 'F', living='true' if mum_living else 'false',
                              born='1802', died=None if mum_living else '1880')
            for _ in range(kids):
                self.parent_of(self.person('Kid Hale', 'F', born='1830', died='1890'), dad, mum)
            for _ in range(living_kids):
                self.parent_of(self.person('Livingkid Hale', 'F', living='true', born='1990'),
                               dad, mum)
        couple(2, living_kids=1)      # the living child is not counted
        couple(2)
        couple(1)
        couple(3, mum_living=True)    # one parent is living: not a family here
        self.build()
        text = self.page()
        self.assertIn('2 children 2 families', text)
        self.assertIn('1 child 1 family', text)
        self.assertNotIn('3 children', text)
        self.assertIn('Counts only the children shown on this site.', text)

    def test_fewer_than_three_families_is_too_few_to_say(self):
        dad = self.person('Paul Hale', 'M', born='1800', died='1870')
        mum = self.person('Anna Hale', 'F', born='1802', died='1880')
        self.parent_of(self.person('Kid Hale', 'F', born='1830', died='1890'), dad, mum)
        self.build()
        self.assertRegex(self.page(), r'Children per family Too few to say\.')

    def test_statistics_never_name_a_withheld_person(self):
        self.lives(1800, [50, 64, 90])
        self.person('Walter Livesey', 'M', living='true', born='1951')
        self.build()
        self.assertNotIn('Livesey', self.page())
        self.assertNotIn('Walter', self.page())


class NameAndGenerationTests(_Base):
    def test_common_first_names_and_surnames_count_shown_people_only(self):
        for i in range(3):
            self.person(f'Mary Hale{i if i else ""}', 'F', born=str(1800 + i), died=str(1860 + i))
        self.person('Mary Brown', 'F', born='1810', died='1870')
        self.person('John Hale', 'M', born='1801', died='1870')
        self.person('Mary Livesey', 'F', living='true', born='1990')
        self.build()
        text = self.page()
        self.assertIn('Mary 4', text)
        self.assertNotIn('Livesey', text)

    def test_generation_table_counts_the_known_ancestors(self):
        me = self.person('Root Hale', 'F', born='1900', died='1970')
        dad = self.person('Dad Hale', 'M', born='1870', died='1940')
        mum = self.person('Mum Hale', 'F', born='1872', died='1950')
        gdad = self.person('Grandad Hale', 'M', born='1840', died='1910')
        self.parent_of(me, dad, mum)
        self.parent_of(dad, gdad)
        self.config(f'root_person: {me}\nsite:\n  statistics: true\n')
        self.build()
        text = self.page()
        self.assertIn('Parents known 2 of 2', text)
        self.assertIn('Grandparents known 1 of 4', text)


class ReviewFixTests(_Base):
    """Review of PR #496: the figures a relative reads must be the ones the
    records support, and a merged duplicate must not count twice."""

    def tombstone(self, name, survivor, *, living='false', extra=''):
        pid = 'p-' + _letters(self._next())
        rel = f'people/merged_{pid}.md'
        (self.root / rel).write_text(
            f'---\nid: {pid}\nname: {name}\nliving: {living}\nstatus: merged\n'
            f'merged_into: {survivor}\n{extra}---\n', encoding='utf-8')
        self.conn.execute(
            'INSERT INTO persons(id, name, surname, sex, living, tier, status, merged_into, path) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (pid, name, name.split()[-1], 'F', living, 'curated', 'merged', survivor, rel))
        return pid

    def test_a_loosely_dated_first_marriage_is_not_replaced_by_a_remarriage(self):
        # Wed in the 1840s (decade only), remarried 1860: no age, not age 40.
        for i in range(3):
            w = self.person(f'Ann{chr(97 + i)} Hale', 'F', born='1820', died='1890')
            self.claim('marriage', '184X', [w], roles={w: 'spouse'})
            self.claim('marriage', '1860', [w], roles={w: 'spouse'})
        self.build()
        self.assertNotIn('40 years', self.page())

    def test_the_first_of_two_well_dated_marriages_counts(self):
        for i in range(3):
            w = self.person(f'Ann{chr(97 + i)} Hale', 'F', born='1820', died='1890')
            self.claim('marriage', '1860', [w], roles={w: 'spouse'})
            self.claim('marriage', '1841', [w], roles={w: 'spouse'})
        self.build()
        self.assertRegex(self.page(), r'Women .*Born in the 1800s 21 years 3 people')

    def test_a_cousin_marriage_keeps_every_generation(self):
        # Xavier is the mother's father and also the father's mother's father.
        x = self.person('Xavier Hale', 'M', born='1800', died='1870')
        me = self.person('Root Hale', 'F', born='1880', died='1950')
        f = self.person('Fred Hale', 'M', born='1855', died='1920')
        m = self.person('Mona Hale', 'F', born='1835', died='1910')
        fm = self.person('Fiona Hale', 'F', born='1830', died='1900')
        self.parent_of(me, f, m)
        self.parent_of(m, x)
        self.parent_of(f, fm)
        self.parent_of(fm, x)
        self.config(f'root_person: {me}\nsite:\n  statistics: true\n')
        self.build()
        text = self.page()
        self.assertIn('Grandparents known 2 of 4', text)
        self.assertIn('Great-grandparents known 1 of 8', text)

    def test_a_living_starting_person_still_gets_the_generation_table(self):
        # The owner is usually the root person, and living. His row is left
        # off; his parents' line is counted, as the home page's tree shows it.
        me = self.person('Owner Hale', 'M', living='true', born='1951')
        dad = self.person('Dad Hale', 'M', born='1920', died='1990')
        mum = self.person('Mum Hale', 'F', born='1922', died='1999')
        gdad = self.person('Gramps Hale', 'M', born='1890', died='1960')
        self.parent_of(me, dad, mum)
        self.parent_of(dad, gdad)
        # The fixture's parent link carries a placeholder date; his own birth
        # record is what shows he is an adult (a "Private" card on the tree).
        self.conn.execute(
            "UPDATE claims SET date_edtf = '1951' WHERE id IN (SELECT claim_id FROM "
            "claim_persons WHERE person_id = ? AND role = 'child')", (me,))
        self.config(f'root_person: {me}\nsite:\n  statistics: true\n')
        for linked in (False, True):
            with self.subTest(linked=linked):
                self.build(linked=linked)
                text = self.page()
                self.assertIn('Parents known 2 of 2', text)
                self.assertIn('Grandparents known 1 of 4', text)
                self.assertNotIn('Starting person', text)
                self.assertNotIn('Owner', text)

    def test_a_by_request_starting_person_gets_no_generation_table(self):
        me = self.person('Quiet Hale', 'M', living='true', born='1951',
                         extra='restricted: by-request\n')
        dad = self.person('Dad Hale', 'M', born='1920', died='1990')
        mum = self.person('Mum Hale', 'F', born='1922', died='1999')
        self.parent_of(me, dad, mum)
        self.config(f'root_person: {me}\nsite:\n  statistics: true\n')
        for linked in (False, True):
            with self.subTest(linked=linked):
                self.build(linked=linked)
                self.assertNotIn('Generation by generation', self.page())

    def test_a_merged_duplicate_is_counted_once_in_the_workbench(self):
        a = self.person('Zelda Hale', 'F', born='1800', died='1860')
        self.tombstone('Zelda Hale', a)
        self.tombstone('Zelda Hale', a)
        self.conn.commit()
        future = time.time() + 5
        os.utime(self.root / '.cache' / 'index.sqlite', (future, future))
        write_path_manifest(index_manifest_path(self.root), record_path_manifest(self.root))
        with unittest.mock.patch.object(site, '_today', return_value='2026-06-15'):
            site.run_site(self.root, self.out, linked=True, workbench=True)
        self.assertIn('Zelda 1', self.page())

    def test_the_preview_leaves_out_someone_a_merged_duplicate_withholds(self):
        self.lives(1800, [50, 64, 90])
        # Their own sources, so withholding them holds back nobody else's dates.
        a = self.person('Quinn Hale', 'M', born='1801', died='1901', extra='sex: M\n')
        self.tombstone('Quinn Hale', a, extra='restricted: by-request\n')
        b = self.person('Rufus Hale', 'M', born='1802', died='1902', extra='sex: M\n')
        self.tombstone('Rufus Hale', b, living='true')
        for linked in (False, True):
            with self.subTest(linked=linked):
                self.build(linked=linked)
                text = self.page()
                self.assertIn('Born in the 1800s 64 years 3 people', text)
                self.assertNotIn('Quinn', text)
                self.assertNotIn('Rufus', text)


if __name__ == '__main__':
    unittest.main()
