"""
test_lib_duplicates.py - the possible-duplicate-people engine behind
`fha report` §4b (TOOLING §15a).

After an Ancestry or FamilySearch import the same grandmother often lands in
the archive twice. These tests pin which pairs the report names as "probably
one person entered twice" and, just as much, which it must never name: a
father and son who share a name, a Junior and a Senior, two children of one
family given the same name a few years apart.

Each test builds a tiny temp archive (people under `people/stubs/`, claims in
one `sources/notes/` record), indexes it, and asks the engine. Expected
answers come from the rules in TOOLING §15a's 4b row, never from the code's
current output. Temp fixtures throughout; never the real archive.
"""

import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import _lib
import index as index_mod


class _Archive:
    """A throwaway archive with people and one source of claims."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._n = 0
        self._claims: list[str] = []
        (root / 'people' / 'stubs').mkdir(parents=True)
        (root / 'sources' / 'notes').mkdir(parents=True)
        (root / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')

    def _id(self, kind: str) -> str:
        self._n += 1
        return f'{kind}-{self._n:010d}'

    def person(self, name: str, *, sex: str = 'U', extra: str = '') -> str:
        pid = self._id('P')
        slug = f'p{self._n}'
        (self.root / 'people' / 'stubs' / f'{slug}__x_{pid}.md').write_text(
            f'---\nid: {pid}\nname: {name}\nsex: {sex}\nliving: false\n'
            f'tier: stub\n{extra}---\n\n# {name}\n', encoding='utf-8')
        return pid.lower()

    def claim(self, ctype: str, persons: list[str], *, date: str = '',
              status: str = 'accepted', roles: str = '', place_text: str = '') -> str:
        cid = self._id('C')
        lines = [f'- value: "{ctype} claim"', f'  id: {cid}', f'  type: {ctype}',
                 f'  persons: [{", ".join(persons)}]', f'  status: {status}']
        if date:
            lines.append(f'  date: "{date}"')
        if place_text:
            lines.append(f'  place_text: "{place_text}"')
        if roles:
            lines.append(roles)
        if status == 'accepted':
            lines += ['  reviewed: 2026-01-01', '  confidence: high',
                      '  information: primary', '  evidence: direct', '  notes: x.']
        self._claims.append('\n'.join(lines) + '\n')
        return cid

    def parent(self, child: str, parent: str) -> str:
        roles = f'  subtype: biological\n  roles:\n    child: {child}\n    parent: [{parent}]'
        return self.claim('relationship', [child, parent], roles=roles)

    def scan(self, not_same: set | None = None) -> dict:
        (self.root / 'sources' / 'notes' / 'claims_s-0000000000.md').write_text(
            '---\nid: S-0000000000\ntitle: Claims\nsource_type: other\n---\n\n'
            '## Claims\n```yaml\n' + ''.join(self._claims) + '```\n', encoding='utf-8')
        cfg = _lib.load_fha_yaml(self.root)
        index_mod.build_index(self.root, cfg)
        conn = _lib.open_index_db(self.root, ('persons', 'claims'))
        try:
            ctx = _lib.completeness_context(conn)
            return _lib.possible_duplicate_people(conn, ctx, not_same or set())
        finally:
            conn.close()


class PossibleDuplicatePeopleTests(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.a = _Archive(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _born(self, pid: str, date: str, **kw) -> None:
        self.a.claim('birth', [pid], date=date, **kw)

    def _pairs(self, result: dict) -> list[frozenset]:
        return [frozenset((p['person_a'], p['person_b'])) for p in result['pairs']]

    def test_same_name_birth_and_father_is_strong(self) -> None:
        caleb = self.a.person('Caleb Cole', sex='M')
        m1 = self.a.person('Margaret Cole', sex='F')
        m2 = self.a.person('Margaret Cole', sex='F', extra='birth: "1849"\n')
        self._born(m1, '1849')
        self.a.parent(m1, caleb)
        self.a.parent(m2, caleb)
        result = self.a.scan()
        self.assertEqual(self._pairs(result), [frozenset((m1, m2))])
        pair = result['pairs'][0]
        self.assertEqual(pair['strength'], 'strong')
        self.assertIn('same name', pair['reasons'])
        self.assertIn('both born 1849', pair['reasons'])
        self.assertIn('same father, Caleb Cole', pair['reasons'])
        self.assertEqual(pair['person_a'], min(m1, m2))

    def test_two_exact_different_birth_dates_are_two_people(self) -> None:
        # A child who died young often had the name given again to the next.
        j1 = self.a.person('John Smith', sex='M')
        j2 = self.a.person('John Smith', sex='M')
        self._born(j1, '1850-03-04')
        self._born(j2, '1852')
        self.assertEqual(self.a.scan()['pairs'], [])

    def test_approximate_birth_within_two_years_fits(self) -> None:
        j1 = self.a.person('John Smith', sex='M')
        j2 = self.a.person('John Smith', sex='M')
        self._born(j1, '1850~')
        self._born(j2, '1852')
        result = self.a.scan()
        self.assertEqual(self._pairs(result), [frozenset((j1, j2))])
        pair = result['pairs'][0]
        self.assertEqual(pair['strength'], 'worth a look')
        self.assertTrue(any(r.startswith('birth years fit') for r in pair['reasons']),
                        pair['reasons'])

    def test_different_recorded_sex_rules_out(self) -> None:
        a = self.a.person('Jean Hartley', sex='M')
        b = self.a.person('Jean Hartley', sex='F')
        self._born(a, '1880')
        self._born(b, '1880')
        self.assertEqual(self.a.scan()['pairs'], [])

    def test_unknown_sex_does_not_rule_out(self) -> None:
        a = self.a.person('Jean Hartley', sex='U')
        b = self.a.person('Jean Hartley', sex='F')
        self._born(a, '1880')
        self._born(b, '1880')
        self.assertEqual(self._pairs(self.a.scan()), [frozenset((a, b))])

    def test_father_and_son_of_one_name_are_not_paired(self) -> None:
        father = self.a.person('Thomas Hartley', sex='M')
        son = self.a.person('Thomas Hartley', sex='M')
        self.a.parent(son, father)
        # Same-name, undated: 40 points alone is below the bar anyway, so give
        # them a shared spouse to lift it past 50 and prove the tie excludes.
        wife = self.a.person('Ann Cole', sex='F')
        self.a.claim('marriage', [father, wife], date='1870',
                     roles=f'  roles:\n    spouse: [{father}, {wife}]')
        self.a.claim('marriage', [son, wife], date='1895',
                     roles=f'  roles:\n    spouse: [{son}, {wife}]')
        self.assertEqual(self.a.scan()['pairs'], [])

    def test_grandfather_and_grandson_of_one_name_are_not_paired(self) -> None:
        grandpa = self.a.person('Thomas Hartley', sex='M')
        middle = self.a.person('James Hartley', sex='M')
        grandson = self.a.person('Thomas Hartley', sex='M')
        self.a.parent(middle, grandpa)
        self.a.parent(grandson, middle)
        self._born(grandpa, '1850~')
        self._born(grandson, '1851')
        self.assertEqual(self.a.scan()['pairs'], [])

    def test_junior_and_senior_are_two_people(self) -> None:
        jr = self.a.person('John Smith Jr', sex='M')
        sr = self.a.person('John Smith Sr', sex='M')
        self._born(jr, '1850')
        self._born(sr, '1850')
        self.assertEqual(self.a.scan()['pairs'], [])

    def test_pair_he_said_are_different_is_hidden_and_counted(self) -> None:
        a = self.a.person('Ellen Hartley', sex='F')
        b = self.a.person('Ellen Hartley', sex='F')
        self._born(a, '1870')
        self._born(b, '1870')
        result = self.a.scan({frozenset((a, b))})
        self.assertEqual(result['pairs'], [])
        self.assertEqual(result['dismissed_hidden'], 1)

    def test_accents_fold(self) -> None:
        a = self.a.person('Halina Wiśniewska', sex='F')
        b = self.a.person('Halina Wisniewska', sex='F')
        self._born(a, '1901')
        self._born(b, '1901')
        result = self.a.scan()
        self.assertEqual(self._pairs(result), [frozenset((a, b))])
        self.assertIn('same name', result['pairs'][0]['reasons'])

    def test_close_spelling_counts_and_says_so(self) -> None:
        a = self.a.person('William Hartley', sex='M')
        b = self.a.person('William Hartly', sex='M')
        self._born(a, '1860')
        self._born(b, '1860')
        result = self.a.scan()
        self.assertEqual(self._pairs(result), [frozenset((a, b))])
        self.assertIn('similar names (Hartley, Hartly)', result['pairs'][0]['reasons'])

    def test_merged_tombstone_is_left_out(self) -> None:
        a = self.a.person('Rose Hartley', sex='F')
        b = self.a.person('Rose Hartley', sex='F',
                          extra=f'status: merged\nmerged_into: {a}\n')
        self._born(a, '1890')
        self.a.claim('birth', [b], date='1890')
        self.assertEqual(self.a.scan()['pairs'], [])

    def test_placeholder_names_never_pair(self) -> None:
        a = self.a.person('unknown')
        b = self.a.person('unknown')
        self._born(a, '1890')
        self._born(b, '1890')
        self.assertEqual(self.a.scan()['pairs'], [])

    def test_suggested_births_from_an_import_still_count(self) -> None:
        a = self.a.person('Clara Hartley', sex='F')
        b = self.a.person('Clara Hartley', sex='F')
        self._born(a, '1849-03-04', status='suggested')
        self._born(b, '1849-03-04', status='suggested')
        result = self.a.scan()
        self.assertEqual(self._pairs(result), [frozenset((a, b))])
        self.assertIn('both born 4 March 1849', result['pairs'][0]['reasons'])

    def test_suggested_exact_births_that_differ_still_rule_out(self) -> None:
        a = self.a.person('Clara Hartley', sex='F')
        b = self.a.person('Clara Hartley', sex='F')
        self._born(a, '1849', status='suggested')
        self._born(b, '1853', status='suggested')
        self.assertEqual(self.a.scan()['pairs'], [])

    def test_match_through_a_recorded_other_name(self) -> None:
        a = self.a.person('Margaret Hartley', sex='F',
                          extra='name_at_birth: Margaret Cole\n')
        b = self.a.person('Margaret Cole', sex='F')
        self._born(a, '1849')
        self._born(b, '1849')
        self.assertEqual(self._pairs(self.a.scan()), [frozenset((a, b))])

    def test_baptism_stands_in_for_a_missing_birth(self) -> None:
        a = self.a.person('Agnes Cole', sex='F')
        b = self.a.person('Agnes Cole', sex='F')
        self.a.claim('baptism', [a], date='1876')
        self._born(b, '1903')
        self.assertEqual(self.a.scan()['pairs'], [])

    def test_same_birthplace_adds_a_reason(self) -> None:
        a = self.a.person('Edith Hartley', sex='F')
        b = self.a.person('Edith Hartley', sex='F')
        self._born(a, '1880~', place_text='Fairview')
        self._born(b, '1881', place_text='fairview')
        result = self.a.scan()
        self.assertEqual(self._pairs(result), [frozenset((a, b))])
        self.assertIn('both born in Fairview', result['pairs'][0]['reasons'])

    def test_same_name_alone_is_not_listed(self) -> None:
        self.a.person('Henry Cole', sex='M')
        self.a.person('Henry Cole', sex='M')
        self.assertEqual(self.a.scan()['pairs'], [])


class PossibleDuplicatePeopleTimingTests(unittest.TestCase):
    """Guards against an accidental all-pairs loop on a big imported tree,
    so the morning briefing never stalls. Not a benchmark: the bound is
    generous on purpose."""

    def test_five_thousand_people_finish_quickly(self) -> None:
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        conn.executescript(
            'CREATE TABLE persons(id TEXT, name TEXT, surname TEXT, sex TEXT, status TEXT);'
            'CREATE TABLE person_variants(person_id TEXT, variant TEXT);'
            'CREATE TABLE aliases(alias TEXT, canonical_id TEXT, kind TEXT);'
            'CREATE TABLE relationships(person_id TEXT, rel TEXT, other_id TEXT, claim_id TEXT);'
            'CREATE TABLE places(id TEXT, name TEXT);')
        givens = ['John', 'Mary', 'William', 'Elizabeth', 'James', 'Sarah', 'Thomas',
                  'Ann', 'George', 'Margaret', 'Henry', 'Jane', 'Charles', 'Ellen']
        # 300 surnames over 52 two-letter openings: blocks of about a hundred
        # people, the crowded end of what a real imported tree holds.
        letters = 'abcdefghijklmnopqrstuvwxyz'
        surnames = [f'{letters[k % 26].upper()}{"ae"[(k // 26) % 2]}rton{k}'
                    for k in range(300)]
        persons = {}
        for n in range(5000):
            pid = f'P-{n:010d}'
            surname = surnames[n % 300]
            name = f'{givens[n % len(givens)]} {surname}'
            persons[pid] = {'id': pid, 'name': name, 'sex': 'U', 'birth': None,
                            'death': None, 'status': 'active'}
            conn.execute('INSERT INTO persons VALUES (?,?,?,?,?)',
                         (pid, name, surname, 'U', 'active'))
        ctx = {'persons': persons, 'claims': {}, 'claim_people': {}, 'person_claims': {},
               'places': {}}
        start = time.monotonic()
        result = _lib.possible_duplicate_people(conn, ctx, set())
        self.assertLess(time.monotonic() - start, 10.0)
        self.assertEqual(result['pairs'], [])


if __name__ == '__main__':
    unittest.main()
