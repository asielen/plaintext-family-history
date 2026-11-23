"""
test_lib_plausibility.py - the "dates that cannot all be true" engine in _lib.

A warning against a correct record teaches the owner to stop reading lint, so
the engine fires only when a conflict holds for every date the claims allow.
These tests call the `_lib` functions directly with hand-built inputs: how a
date becomes a range (every EDTF precision honoured, open sides unknown), how
several claims of one fact merge into one range, and the three one-person
rules (W163, W164, W165) that `fha lint` and later the GEDCOM import plan
both ask through `one_person_problems`.

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
    DatedClaim, claim_date_range, iso_plus_years, merge_fact_range,
    one_person_problems, plausibility_input_from_claims,
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


if __name__ == '__main__':
    unittest.main()
