"""
test_lib_plausibility.py - the "dates that cannot all be true" engine in _lib.

A warning against a correct record teaches the owner to stop reading lint, so
the engine fires only when a conflict holds for every date the claims allow.
These tests call the `_lib` functions directly with hand-built inputs: how a
date becomes a range (every EDTF precision honoured, open sides unknown), how
several claims of one fact merge into one range, the three one-person
rules (W163, W164, W165) that `fha lint` and later the GEDCOM import plan
both ask through `one_person_problems`, and the family rules (W166-W170:
couples, parents and children, siblings, loops) asked through
`family_problems`.

Archive-level behaviour (which claims are read, the message wording, the file
a finding lands on) is in tests/test_lint_plausibility.py.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import _lib
from _lib import (
    DatedClaim, ParentEdge, PlausibilityInput, claim_date_range, family_problems,
    iso_plus_years, merge_fact_range, one_person_problems,
    plausibility_input_from_claims,
)


def _dc(cid: str, lo, hi, ctype: str = 'birth', date: str = '') -> DatedClaim:
    return DatedClaim(claim_id=cid, ctype=ctype, lo=lo, hi=hi, date=date, subtype='')


class DateRangeTests(unittest.TestCase):
    def test_year_only_dates_span_their_year(self) -> None:
        self.assertEqual(claim_date_range({'date': '1871'}), ('1871-01-01', '1871-12-31'))

    def test_an_unquoted_year_reads_the_same(self) -> None:
        # YAML hands back `date: 1871` as a number.
        self.assertEqual(claim_date_range({'date': 1871}), ('1871-01-01', '1871-12-31'))

    def test_approximate_and_decade_dates_widen(self) -> None:
        self.assertEqual(claim_date_range({'date': '1880?'}), ('1879-01-01', '1881-12-31'))
        self.assertEqual(claim_date_range({'date': '188X'}), ('1880-01-01', '1889-12-31'))
        self.assertEqual(claim_date_range({'date': '1870/1875'}), ('1870-01-01', '1875-12-31'))

    def test_open_sides_are_unknown(self) -> None:
        self.assertEqual(claim_date_range({'date': '[..1920]'}), (None, '1920-12-31'))
        self.assertEqual(claim_date_range({'date': '[1920..]'}), ('1920-01-01', None))
        self.assertEqual(claim_date_range({'date': '1870/..'}), ('1870-01-01', None))
        self.assertIsNone(claim_date_range({}))
        self.assertIsNone(claim_date_range({'date': None}))
        self.assertIsNone(claim_date_range({'date': 'not a date'}))

    def test_impossible_day_is_skipped_not_raised(self) -> None:
        self.assertIsNone(claim_date_range({'date': '1900-02-31'}))

    def test_an_approximate_day_or_month_widens_a_year_each_way(self) -> None:
        # "About 20 May 1850" is not exactly 20 May: a death on 19 May is
        # possible, so the range must hold more than the one day. Any `~`,
        # `?` or `%` widens a date at least a year each way, like `1850~`.
        for date in ('1850-05-~20', '1850-05-20~', '1850-05-20?', '1850-~05-20',
                     'about 20 May 1850', '1850-05~', '1850-~05'):
            with self.subTest(date=date):
                self.assertEqual(claim_date_range({'date': date}),
                                 ('1849-01-01', '1851-12-31'))
        # An exact day stays one day.
        self.assertEqual(claim_date_range({'date': '1850-05-20'}),
                         ('1850-05-20', '1850-05-20'))

    def test_a_mark_on_one_end_of_a_range_widens_only_that_end(self) -> None:
        # "About 1880 to 1885": the 1885 end is exact, so a birth in 1886
        # still cannot come before a death in that range.
        self.assertEqual(claim_date_range({'date': '1880~/1885'}),
                         ('1879-01-01', '1885-12-31'))
        self.assertEqual(claim_date_range({'date': '1880/1885~'}),
                         ('1880-01-01', '1886-12-31'))
        self.assertEqual(claim_date_range({'date': '1850-05-~20/1860'}),
                         ('1849-01-01', '1860-12-31'))
        self.assertEqual(claim_date_range({'date': '../1890~'}), (None, '1891-12-31'))

    def test_a_backwards_interval_is_not_read(self) -> None:
        # `1890/1850` runs backwards (E014 says so); its intended range is
        # unknown, so no rule may read it.
        self.assertIsNone(claim_date_range({'date': '1890/1850'}))

    def test_plus_years_keeps_leap_day_safe(self) -> None:
        self.assertEqual(iso_plus_years('1896-02-29', 1), '1897-02-28')
        self.assertEqual(iso_plus_years('1896-02-29', 4), '1900-02-28')
        self.assertEqual(iso_plus_years('1800-12-31', 120), '1920-12-31')
        self.assertEqual(iso_plus_years('9999-12-31', 5), '9999-12-31')
        self.assertEqual(iso_plus_years('9990-06-01', 120), '9999-12-31')

    def test_merge_widens_and_any_open_side_opens_it(self) -> None:
        a = _dc('c-1', '1850-01-01', '1850-12-31')
        b = _dc('c-2', '1870-01-01', '1870-12-31')
        merged = merge_fact_range([a, b])
        self.assertEqual((merged.lo, merged.hi), ('1850-01-01', '1870-12-31'))
        self.assertEqual([c.claim_id for c in merged.claims], ['c-1', 'c-2'])
        open_start = _dc('c-3', None, '1860-12-31')
        merged = merge_fact_range([a, b, open_start])
        self.assertEqual((merged.lo, merged.hi), (None, '1870-12-31'))
        open_end = _dc('c-4', '1855-01-01', None)
        merged = merge_fact_range([a, open_end])
        self.assertEqual((merged.lo, merged.hi), ('1850-01-01', None))


class OnePersonEngineTests(unittest.TestCase):
    """The rules over raw claim dicts, the way any caller hands them in."""

    ADA, BEN = 'p-aaaaaaaaaa', 'p-bbbbbbbbbb'

    @staticmethod
    def _claim(cid, ctype, persons, date, *, roles=None, status='accepted', **extra):
        c = {'id': cid, 'type': ctype, 'persons': list(persons), 'date': date,
             'status': status}
        if roles is not None:
            c['roles'] = roles
        c.update(extra)
        return c

    def _problems(self, claims, survivors=None, statuses=('accepted',)):
        persons = {self.ADA: {'sex': 'F'}, self.BEN: {'sex': 'M'}}
        data = plausibility_input_from_claims(claims, persons, {}, survivors,
                                              statuses=statuses)
        return one_person_problems(data)

    def test_death_before_birth_is_one_problem_with_both_sides(self) -> None:
        found = self._problems([self._claim('c-1', 'birth', [self.ADA], '1871'),
                                self._claim('c-2', 'death', [self.ADA], '1850')])
        self.assertEqual([(p.code, p.kind, p.pids) for p in found],
                         [('W163', 'death-before-birth', (self.ADA,))])
        death_side, birth_side = found[0].claims
        self.assertEqual([c.claim_id for c in death_side], ['c-2'])
        self.assertEqual([c.claim_id for c in birth_side], ['c-1'])

    def test_overlapping_readings_never_fire(self) -> None:
        cases = [('1871', '1871'), ('1850~', '1849'), ('185X', '1850'),
                 ('[..1890]', '1850'), ('1850', '[..1890]'), ('[1840..]', '1850'),
                 ('1880?', '1879'), ('1850/1860', '1855')]
        for born, died in cases:
            with self.subTest(born=born, died=died):
                self.assertEqual(self._problems([
                    self._claim('c-1', 'birth', [self.ADA], born),
                    self._claim('c-2', 'death', [self.ADA], died)]), [])

    def test_a_merge_candidate_is_asked_as_one_person(self) -> None:
        # How a duplicate finder asks "could Ada and Ben be one person?":
        # read Ben as Ada through `survivors`.
        claims = [self._claim('c-1', 'birth', [self.ADA], '1871'),
                  self._claim('c-2', 'death', [self.BEN], '1850')]
        self.assertEqual(self._problems(claims), [])
        found = self._problems(claims, survivors={self.BEN: self.ADA})
        self.assertEqual([p.kind for p in found], ['death-before-birth'])

    def test_statuses_pick_which_claims_count(self) -> None:
        claims = [self._claim('c-1', 'birth', [self.ADA], '1871', status='suggested'),
                  self._claim('c-2', 'death', [self.ADA], '1850', status='suggested')]
        self.assertEqual(self._problems(claims), [])
        self.assertEqual(len(self._problems(claims, statuses=('suggested',))), 1)

    def test_long_life_counts_the_shortest_lifespan(self) -> None:
        found = self._problems([self._claim('c-1', 'birth', [self.ADA], '1800-06-10'),
                                self._claim('c-2', 'death', [self.ADA], '1925-06-09')])
        self.assertEqual([(p.kind, p.numbers) for p in found],
                         [('long-life', {'years': 124})])

    def test_long_life_from_a_baptism_when_no_birth_is_recorded(self) -> None:
        found = self._problems([self._claim('c-1', 'baptism', [self.ADA], '1800'),
                                self._claim('c-2', 'death', [self.ADA], '1925')])
        self.assertEqual([p.kind for p in found], ['long-life'])

    def test_life_after_death_one_problem_per_claim(self) -> None:
        found = self._problems([
            self._claim('c-1', 'death', [self.ADA], '1880'),
            self._claim('c-2', 'residence', [self.ADA], '1885'),
            self._claim('c-3', 'occupation', [self.ADA], '1890'),
            self._claim('c-4', 'residence', [self.ADA], '1880')])
        self.assertEqual([(p.kind, p.claims[1][0].claim_id) for p in found],
                         [('life-after-death', 'c-2'), ('life-after-death', 'c-3')])

    def test_a_burial_stands_in_for_a_missing_death(self) -> None:
        found = self._problems([self._claim('c-1', 'burial', [self.ADA], '1880'),
                                self._claim('c-2', 'census', [self.ADA], '1900')])
        self.assertEqual([p.kind for p in found], ['life-after-death'])


class FamilyEngineTests(unittest.TestCase):
    """W166-W170 over raw claim dicts: couples, parents and children,
    siblings and loops. Each boundary test sits on both sides of its line,
    because a warning against a correct family teaches him to ignore lint."""

    MUM, DAD, KID, KID2, ANON = ('p-mmmmmmmmmm', 'p-ffffffffff', 'p-kkkkkkkkkk',
                                 'p-jjjjjjjjjj', 'p-nnnnnnnnnn')

    def setUp(self) -> None:
        self.claims: list[dict] = []
        self.persons = {self.MUM: {'sex': 'F'}, self.DAD: {'sex': 'M'},
                        self.KID: {'sex': 'F'}, self.KID2: {'sex': 'M'},
                        self.ANON: {}}
        self._n = 0

    def _add(self, ctype, persons, date=None, *, roles=None, subtype=None,
             status='accepted') -> str:
        self._n += 1
        cid = f'c-{self._n:010d}'
        c = {'id': cid, 'type': ctype, 'persons': list(persons), 'status': status}
        if date is not None:
            c['date'] = date
        if roles is not None:
            c['roles'] = roles
        if subtype is not None:
            c['subtype'] = subtype
        self.claims.append(c)
        return cid

    def _tie(self, parent, child, subtype=None) -> str:
        return self._add('relationship', [parent, child],
                         roles={'parent': [parent], 'child': [child]}, subtype=subtype)

    def _problems(self):
        data = plausibility_input_from_claims(self.claims, self.persons, {})
        return family_problems(data)

    def _kinds(self):
        return [p.kind for p in self._problems()]

    # W167: born after a parent died
    def test_born_after_mother_died(self) -> None:
        tie = self._tie(self.MUM, self.KID)
        self._add('death', [self.MUM], '1869')
        self._add('birth', [self.KID], '1871')
        (problem,) = self._problems()
        self.assertEqual((problem.code, problem.kind, problem.pids),
                         ('W167', 'born-after-mother-died', (self.KID, self.MUM)))
        self.assertEqual(problem.numbers['ties'], (tie,))

    def test_born_the_year_the_mother_died_is_silent(self) -> None:
        self._tie(self.MUM, self.KID)
        self._add('death', [self.MUM], '1871')
        self._add('birth', [self.KID], '1871')
        self.assertEqual(self._kinds(), [])

    def test_posthumous_birth_within_a_year_is_silent(self) -> None:
        self._tie(self.DAD, self.KID)
        self._add('death', [self.DAD], '1850-03')
        self._add('birth', [self.KID], '1851-02')
        self.assertEqual(self._kinds(), [])
        self.claims[-1]['date'] = '1852'
        self.assertEqual(self._kinds(), ['born-after-father-died'])

    def test_unknown_sex_parent_gets_the_fathers_allowance(self) -> None:
        self._tie(self.ANON, self.KID)
        self._add('death', [self.ANON], '1850-03')
        self._add('birth', [self.KID], '1851-02')
        self.assertEqual(self._kinds(), [])
        self.claims[-1]['date'] = '1852'
        self.assertEqual(self._kinds(), ['born-after-father-died'])

    def test_a_burial_stands_in_for_the_parents_death(self) -> None:
        self._tie(self.MUM, self.KID)
        self._add('burial', [self.MUM], '1869')
        self._add('birth', [self.KID], '1871')
        self.assertEqual(self._kinds(), ['born-after-mother-died'])

    def test_a_baptism_alone_says_nothing_about_how_early_the_birth_was(self) -> None:
        # A baptism in 1871 allows a birth years earlier, before she died.
        self._tie(self.MUM, self.KID)
        self._add('death', [self.MUM], '1869')
        self._add('baptism', [self.KID], '1871')
        self.assertEqual(self._kinds(), [])

    def test_social_ties_are_not_read_for_age_or_death(self) -> None:
        for subtype in ('adoptive', 'step', 'foster', 'guardian'):
            with self.subTest(subtype=subtype):
                self.setUp()
                self._tie(self.MUM, self.KID, subtype=subtype)
                self._add('death', [self.MUM], '1869')
                self._add('birth', [self.MUM], '1865')
                self._add('birth', [self.KID], '1871')
                self.assertEqual(self._kinds(), [])

    # W168: a parent's age
    def test_parent_born_after_child(self) -> None:
        self._tie(self.MUM, self.KID)
        self._add('birth', [self.MUM], '1880')
        self._add('birth', [self.KID], '1871')
        self.assertEqual(self._kinds(), ['parent-born-after-child'])

    def test_parent_too_young_uses_the_oldest_reading(self) -> None:
        # Mother born 1860, child 1871: she was 11 at most.
        self._tie(self.MUM, self.KID)
        self._add('birth', [self.MUM], '1860')
        self._add('birth', [self.KID], '1871')
        (problem,) = self._problems()
        self.assertEqual((problem.kind, problem.numbers['age']), ('parent-too-young', 11))
        # Child 1872: she could have been 12 by then.
        self.claims[-1]['date'] = '1872'
        self.assertEqual(self._kinds(), [])

    def test_mother_at_55_is_silent_at_56_fires(self) -> None:
        self._tie(self.MUM, self.KID)
        self._add('birth', [self.MUM], '1800-06-10')
        self._add('birth', [self.KID], '1856-06-09')
        self.assertEqual(self._kinds(), [])
        self.claims[-1]['date'] = '1856-06-10'
        (problem,) = self._problems()
        self.assertEqual((problem.kind, problem.numbers['age']), ('mother-too-old', 56))

    def test_year_only_ages_need_the_youngest_reading(self) -> None:
        self._tie(self.MUM, self.KID)
        self._add('birth', [self.MUM], '1800')
        self._add('birth', [self.KID], '1856')
        self.assertEqual(self._kinds(), [])
        self.claims[-1]['date'] = '1857'
        self.assertEqual(self._kinds(), ['mother-too-old'])

    def test_father_too_old_and_unknown_sex_gets_the_fathers_ceiling(self) -> None:
        for parent in (self.DAD, self.ANON):
            with self.subTest(parent=parent):
                self.setUp()
                self._tie(parent, self.KID)
                self._add('birth', [parent], '1800')
                self._add('birth', [self.KID], '1870')
                self.assertEqual(self._kinds(), [])
                self.claims[-1]['date'] = '1882'
                self.assertEqual(self._kinds(), ['father-too-old'])

    def test_a_parent_tie_from_a_birth_claims_roles_counts(self) -> None:
        self._add('birth', [self.KID, self.MUM], '1871',
                  roles={'child': [self.KID], 'parent': [self.MUM]})
        self._add('death', [self.MUM], '1869')
        self.assertEqual(self._kinds(), ['born-after-mother-died'])

    def test_undated_ties_count_and_suggested_claims_do_not(self) -> None:
        self._tie(self.MUM, self.KID)
        self._add('death', [self.MUM], '1869', status='suggested')
        self._add('birth', [self.KID], '1871')
        self.assertEqual(self._kinds(), [])

    # W166: couples
    def test_married_before_born(self) -> None:
        self._add('birth', [self.KID], '1871')
        self._add('marriage', [self.KID, self.KID2], '1865')
        found = [(p.code, p.kind, p.pids) for p in self._problems()]
        self.assertEqual(found, [('W166', 'married-before-born', (self.KID, self.KID2))])

    def test_married_after_death(self) -> None:
        self._add('death', [self.KID2], '1860')
        self._add('marriage', [self.KID, self.KID2], '1865')
        self.assertEqual(self._kinds(), ['married-after-death'])

    def test_married_young_and_at_fourteen_is_silent(self) -> None:
        self._add('birth', [self.KID], '1860')
        self._add('marriage', [self.KID, self.KID2], '1873')
        (problem,) = self._problems()
        self.assertEqual((problem.kind, problem.numbers['age']), ('married-young', 13))
        self.claims[-1]['date'] = '1874'
        self.assertEqual(self._kinds(), [])

    def test_a_divorce_is_never_called_young(self) -> None:
        self._add('birth', [self.KID], '1860')
        self._add('divorce', [self.KID, self.KID2], '1873')
        self.assertEqual(self._kinds(), [])

    def test_parents_named_on_a_marriage_are_not_spouses(self) -> None:
        self._add('death', [self.DAD], '1850')
        self._add('marriage', [self.KID, self.KID2, self.DAD], '1865',
                  roles={'spouse': [self.KID, self.KID2], 'parent': [self.DAD]})
        self.assertEqual(self._kinds(), [])

    # W169: siblings
    def _two_children(self, first, second, parents=None) -> None:
        for parent in (parents or [self.MUM]):
            self._tie(parent, self.KID)
            self._tie(parent, self.KID2)
        self._add('birth', [self.KID], first)
        self._add('birth', [self.KID2], second)

    def test_siblings_five_months_apart_fire(self) -> None:
        self._two_children('1850-01', '1850-06')
        (problem,) = self._problems()
        self.assertEqual((problem.code, problem.kind, problem.pids[:2]),
                         ('W169', 'siblings-too-close', (self.KID2, self.KID)))
        self.assertEqual((problem.numbers['min_gap'], problem.numbers['max_gap']), (121, 180))

    def test_twins_are_silent(self) -> None:
        self._two_children('1850-01-04', '1850-01-05')
        self.assertEqual(self._kinds(), [])

    def test_siblings_eight_months_apart_are_silent(self) -> None:
        self._two_children('1850-01-01', '1850-09-01')
        self.assertEqual(self._kinds(), [])

    def test_a_year_only_sibling_between_them_does_not_hide_the_pair(self) -> None:
        # A third child born "1850" sorts between the two exact births; the
        # 141-day pair must still be compared, not only birth-order neighbours.
        self._two_children('1849-12-20', '1850-05-10')
        third = 'p-tttttttttt'
        self.persons[third] = {}
        self._tie(self.MUM, third)
        self._add('birth', [third], '1850')
        found = [(p.kind, p.pids[:2]) for p in self._problems()]
        self.assertEqual(found, [('siblings-too-close', (self.KID2, self.KID))])

    def test_year_only_siblings_never_fire(self) -> None:
        self._two_children('1850', '1851')
        self.assertEqual(self._kinds(), [])

    def test_half_siblings_by_two_mothers_are_not_compared(self) -> None:
        other = 'p-qqqqqqqqqq'
        self.persons[other] = {'sex': 'F'}
        self._tie(self.DAD, self.KID)
        self._tie(self.DAD, self.KID2)
        self._tie(self.MUM, self.KID)
        self._tie(other, self.KID2)
        self._add('birth', [self.KID], '1850-01')
        self._add('birth', [self.KID2], '1850-06')
        self.assertEqual(self._kinds(), [])

    def test_same_two_parents_neither_recorded_female_are_compared(self) -> None:
        self._two_children('1850-01', '1850-06', parents=[self.DAD, self.ANON])
        self.assertEqual(self._kinds(), ['siblings-too-close'])

    def test_a_pair_seen_through_two_groups_is_reported_once(self) -> None:
        self._two_children('1850-01', '1850-06', parents=[self.MUM, self.DAD])
        self.assertEqual(self._kinds(), ['siblings-too-close'])

    # W170: loops
    def test_loop_of_three_is_one_problem(self) -> None:
        a, b, c = self.DAD, self.KID2, self.ANON
        ties = [self._tie(a, b), self._tie(b, c), self._tie(c, a, subtype='adoptive')]
        (problem,) = self._problems()
        self.assertEqual((problem.code, problem.kind), ('W170', 'own-ancestor'))
        # Walk order, starting from the smallest id.
        self.assertEqual(problem.pids, (a, b, c))
        self.assertEqual(sorted(problem.numbers['ties']), sorted(ties))

    def test_no_recursion_on_a_deep_line(self) -> None:
        data = PlausibilityInput()
        pids = [f'p-{i:010d}' for i in range(5001)]
        for parent, child in zip(pids, pids[1:]):
            data.parent_edges[(parent, child)] = ParentEdge(natures={''}, claims=[])
        self.assertEqual(family_problems(data), [])
        data.parent_edges[(pids[-1], pids[0])] = ParentEdge(natures={''}, claims=[])
        (problem,) = family_problems(data)
        self.assertEqual(len(problem.pids), 5001)


if __name__ == '__main__':
    unittest.main()
