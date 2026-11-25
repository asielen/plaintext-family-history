"""A person's timeline says how old they were, and when their parents died
and their children were born: `_lib.age_between`, `_lib.age_phrase` and
`_lib.family_life_events` (TOOLING §12, family rows)."""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import _lib  # noqa: E402
from index import _DDL as INDEX_DDL  # noqa: E402

M = 'p-m000000001'   # Margaret, born 1871, died 1923
F = 'p-f000000001'   # her father Caleb, died 1877
MO = 'p-mo00000001'  # her mother
E = 'p-e000000001'   # her daughter Ethel, born 1902
B = 'p-b000000001'   # a brother


class AgeBetweenTests(unittest.TestCase):
    def test_worked_values(self):
        cases = [
            (('1871', '1877'), (5, 6)),
            (('1871-03-02', '1877-06-01'), (6, 6)),
            (('1871-03-02', '1877-01-10'), (5, 5)),
            (('1840~', '1850'), None),          # 1839-1841 gives 8..11: too loose
            (('1871', '1871'), (0, 0)),
            (('1871', '1870'), None),           # before she was born
            (('[..1871]', '1880'), None),       # open-ended birth
            (('187X', '1890'), None),
            ((None, '1890'), None),
            (('1871', ''), None),
            (('1871', 'not a date'), None),
            # "About 1871": her father's death in 1870 or 1871 may have come
            # before she was born, so no "under a year old" / "not yet 2".
            (('1871~', '1870'), None),
            (('1871~', '1871'), None),
        ]
        for (birth, event), expected in cases:
            with self.subTest(birth=birth, event=event):
                self.assertEqual(_lib.age_between(birth, event), expected)

    def test_age_phrase(self):
        self.assertEqual(_lib.age_phrase((0, 0)), 'under a year old')
        self.assertEqual(_lib.age_phrase((0, 1)), 'not yet 2')
        self.assertEqual(_lib.age_phrase((6, 6)), '6')
        self.assertEqual(_lib.age_phrase((5, 6)), '5 or 6')


class FamilyLifeEventsTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.executescript(INDEX_DDL)
        self.conn.row_factory = sqlite3.Row
        self.person(M, 'Margaret Cole', 'F')
        self.person(F, 'Caleb Hartley', 'M')
        self.person(E, 'Ethel Cole', 'F')
        self.claim('c-mb', 's-1', 'birth', '1871', {M: 'child'})
        self.claim('c-md', 's-1', 'death', '1923', {M: 'deceased'})
        self.claim('c-fd', 's-2', 'death', '1877', {F: 'deceased'})
        self.parent(M, F, 'c-rel1')
        self.claim('c-eb', 's-3', 'birth', '1902', {E: 'child', M: 'parent'})
        self.parent(E, M, 'c-eb')

    def person(self, pid, name, sex):
        self.conn.execute(
            "INSERT INTO persons(id, name, surname, sex, living, tier, status, path) "
            "VALUES (?,?,?,?,?,?,?,?)", (pid, name, name.split()[-1], sex, 'false',
                                         'curated', 'active', f'people/{pid}.md'))

    def claim(self, cid, sid, ctype, date, roles, *, status='accepted', reviewed=None,
              subtype=None):
        lo, hi = _lib.edtf_bounds(date) if date else (None, None)
        self.conn.execute(
            'INSERT INTO claims(id, source_id, type, subtype, value, status, date_edtf, '
            'date_min, date_max, reviewed) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (cid, sid, ctype, subtype, f'{ctype} record', status, date, lo, hi, reviewed))
        for pos, (pid, role) in enumerate(roles.items()):
            self.conn.execute(
                'INSERT INTO claim_persons(claim_id, person_id, position, role) VALUES (?,?,?,?)',
                (cid, pid, pos, role))

    def parent(self, child, parent, cid, subtype=None):
        if not self.conn.execute('SELECT 1 FROM claims WHERE id=?', (cid,)).fetchone():
            self.claim(cid, 's-9', 'relationship', None, {child: 'child', parent: 'parent'},
                       subtype=subtype)
        self.conn.execute('INSERT INTO relationships(person_id, rel, other_id, claim_id) '
                          'VALUES (?,?,?,?)', (child, 'parent', parent, cid))
        self.conn.execute('INSERT INTO relationships(person_id, rel, other_id, claim_id) '
                          'VALUES (?,?,?,?)', (parent, 'child', child, cid))

    def events(self, **kw):
        return _lib.family_life_events(self.conn, M, **kw)

    def test_window_and_events(self):
        out = self.events()
        self.assertEqual(out['window'], ('1871-01-01', '1923-12-31'))
        got = [(e['relation'], e['type'], e['age']) for e in out['events']]
        self.assertEqual(got, [('father', 'death', (5, 6)), ('daughter', 'birth', (30, 31))])

    def test_no_death_stops_at_last_record(self):
        self.conn.execute("DELETE FROM claims WHERE id='c-md'")
        self.conn.execute("DELETE FROM claim_persons WHERE claim_id='c-md'")
        self.claim('c-ed', 's-4', 'death', '1950', {E: 'deceased'})
        out = self.events()
        self.assertEqual(out['window'], ('1871-01-01', '1902-12-31'))
        self.assertNotIn('c-ed', [e['claim_id'] for e in out['events']])

    def test_no_dated_records_no_window(self):
        conn = self.conn
        conn.execute('DELETE FROM claims')
        conn.execute('DELETE FROM claim_persons')
        out = self.events()
        self.assertIsNone(out['window'])
        self.assertEqual(out['events'], [])

    def test_sibling_full_and_half(self):
        self.person(B, 'Ben Hartley', 'M')
        self.claim('c-bb', 's-5', 'birth', '1875', {B: 'child'})
        self.parent(B, F, 'c-relb')
        words = {e['relative_id']: e['relation'] for e in self.events()['events']}
        self.assertEqual(words[B], 'brother')      # her mother unrecorded: plain
        self.person(MO, 'Ann Cole', 'F')
        self.person('p-mo00000002', 'Sarah Lane', 'F')
        self.parent(M, MO, 'c-relm')
        self.parent(B, 'p-mo00000002', 'c-relb2')
        words = {e['relative_id']: e['relation'] for e in self.events()['events']}
        self.assertEqual(words[B], 'half-brother')

    def test_sibling_through_a_refused_parent_but_not_a_refused_tie(self):
        # A father the caller will not name (living or unknown) is still the
        # way to her brother; a tie the caller refuses (restricted) is not.
        self.person(B, 'Ben Hartley', 'M')
        self.claim('c-bb', 's-5', 'birth', '1875', {B: 'child'})
        self.parent(B, F, 'c-relb')
        out = self.events(person_ok=lambda p: p != F)
        self.assertEqual([(e['relative_id'], e['relation']) for e in out['events']
                          if e['relative_id'] in (B, F)], [(B, 'brother')])
        out = self.events(edge_ok=lambda p, rel, o, cid: F not in (p, o))
        self.assertNotIn(B, [e['relative_id'] for e in out['events']])

    def test_half_word_from_a_source_that_says_so(self):
        # A record calling them half-siblings is enough, as in fha relate.
        self.person(B, 'Ben Hartley', 'M')
        self.claim('c-bb', 's-5', 'birth', '1875', {B: 'child'})
        self.parent(B, F, 'c-relb')
        self.claim('c-half', 's-5', 'relationship', None, {M: 'sibling', B: 'sibling'},
                   subtype='half')
        words = {e['relative_id']: e['relation'] for e in self.events()['events']}
        self.assertEqual(words[B], 'half-brother')

    def test_step_child_word(self):
        self.person('p-s000000001', 'Tom Cole', 'M')
        self.claim('c-sb', 's-6', 'birth', '1900', {'p-s000000001': 'child'})
        self.parent('p-s000000001', M, 'c-rels', subtype='step')
        words = {e['relative_id']: e['relation'] for e in self.events()['events']}
        self.assertEqual(words['p-s000000001'], 'stepson')

    def test_callbacks_honoured(self):
        out = self.events(person_ok=lambda p: p != F)
        self.assertNotIn(F, [e['relative_id'] for e in out['events']])
        out = self.events(claim_ok=lambda r: r['id'] != 'c-fd')
        self.assertNotIn('c-fd', [e['claim_id'] for e in out['events']])
        out = self.events(edge_ok=lambda p, rel, o, cid: cid != 'c-rel1')
        self.assertNotIn(F, [e['relative_id'] for e in out['events']])

    def test_parked_only_when_asked(self):
        self.conn.execute("UPDATE claims SET status='suggested', reviewed='2026-01-01' "
                          "WHERE id='c-fd'")
        self.assertNotIn('c-fd', [e['claim_id'] for e in self.events()['events']])
        out = self.events(include_parked=True)
        self.assertIn('c-fd', [e['claim_id'] for e in out['events']])

    def test_double_death_and_parents_marriage_once(self):
        # One record of two deaths gives one row per relative; her parents'
        # marriage after her birth gives one row, not one per parent.
        self.person(MO, 'Ann Cole', 'F')
        self.parent(M, MO, 'c-relm')
        self.claim('c-pm', 's-7', 'marriage', '1872', {F: 'spouse', MO: 'spouse'})
        rows = [(e['claim_id'], e['relative_id']) for e in self.events()['events']]
        self.assertEqual(sum(1 for cid, _ in rows if cid == 'c-pm'), 1)
        self.assertEqual(len(rows), len(set(rows)))

    def test_own_marriage_not_a_family_row(self):
        self.person('p-h000000001', 'John Cole', 'M')
        self.claim('c-mm', 's-8', 'marriage', '1893', {M: 'spouse', 'p-h000000001': 'spouse'})
        self.conn.execute("INSERT INTO relationships(person_id, rel, other_id, claim_id) "
                          "VALUES (?,?,?,?)", (M, 'spouse', 'p-h000000001', 'c-mm'))
        self.assertNotIn('c-mm', [e['claim_id'] for e in self.events()['events']])


if __name__ == '__main__':
    unittest.main()
