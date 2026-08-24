"""
test_lib_edtf_humanize.py - _lib.humanize_edtf, the shared EDTF -> plain-
English formatter.

#123 follow-up (Codex review on PR #149): `humanize_edtf` (plus its private
helper `_humanize_edtf_bound`) used to be `photoindex._humanize_edtf`, a
"nominally-private" (leading-underscore) function `tools/site.py` reached by
importing the whole `photoindex` tool - crossing this repo's "tools never
import tools" boundary (`report.py` is the one documented orchestrator
exception; `site.py` is not). Moved into `_lib.py` so both tools import the
same public name instead. This file pins the formatter's own behavior
directly; `tests/test_photoindex.py` and `tests/test_site.py` keep their
existing call-site coverage (the gallery label and the source-page file
note) unchanged, proving the move didn't alter either caller's output.

Also covers the P2 finding from the same review: `?` (uncertain - "not sure
this is the right year") and `~` (approximate - "this is a rough guess")
used to render through the exact same "about {year}" prefix, silently
turning an unconfirmed date into an approximation. The two now read as the
two different things they record, matching base.html's date-notation
legend wording (`~` = "about this year"; `?` = "probably right but not
confirmed").
"""

import sys
from pathlib import Path

import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

from _lib import humanize_edtf


class HumanizeEdtfUncertainVsApproximateTests(unittest.TestCase):
    """The P2 finding itself: `?` and `~` must not collapse to one phrasing."""

    def test_uncertain_year_reads_unconfirmed_not_about(self):
        # Before the fix: humanize_edtf('1916?') == 'about 1916', which
        # misstates "not certain it was 1916" as an approximation.
        self.assertEqual(humanize_edtf('1916?'), '1916 (unconfirmed)')
        self.assertNotIn('about', humanize_edtf('1916?'))

    def test_approximate_year_still_reads_about(self):
        # The `~` case is unchanged - this is the regression guard proving
        # the fix didn't collapse the other direction instead.
        self.assertEqual(humanize_edtf('1916~'), 'about 1916')

    def test_uncertain_month_and_day_also_read_unconfirmed(self):
        self.assertEqual(humanize_edtf('1916-06?'), 'June 1916 (unconfirmed)')
        self.assertEqual(humanize_edtf('1916-06-10?'), '10 June 1916 (unconfirmed)')

    def test_uncertain_bound_inside_a_range_reads_unconfirmed(self):
        # The uncertain/approximate distinction must survive through the
        # range formatter too, not just the plain-date fast path.
        self.assertEqual(humanize_edtf('1916?/1918'), '1916 (unconfirmed) to 1918')


class HumanizeEdtfUnchangedBehaviorTests(unittest.TestCase):
    """Everything humanize_edtf already did before the move/fix, pinned so
    the #123 follow-up refactor (moving this out of photoindex.py) provably
    didn't change any of it."""

    def test_plain_year(self):
        self.assertEqual(humanize_edtf('1916'), '1916')

    def test_year_month(self):
        self.assertEqual(humanize_edtf('1920-01'), 'January 1920')

    def test_year_month_day(self):
        self.assertEqual(humanize_edtf('1916-02-26'), '26 February 1916')

    def test_approximate_month(self):
        self.assertEqual(humanize_edtf('1920-01~'), 'about January 1920')

    def test_decade(self):
        self.assertEqual(humanize_edtf('192X'), '1920s')

    def test_slash_range(self):
        self.assertEqual(humanize_edtf('1912/1915'), '1912 to 1915')

    def test_approximate_start_of_range(self):
        self.assertEqual(humanize_edtf('1912~/1915'), 'about 1912 to 1915')

    def test_open_ended_range(self):
        self.assertEqual(humanize_edtf('1870/..'), 'after 1870')
        self.assertEqual(humanize_edtf('../1875'), 'before 1875')

    def test_bracket_before_and_after(self):
        self.assertEqual(humanize_edtf('[..1900]'), 'before 1900')
        self.assertEqual(humanize_edtf('[1900..]'), 'after 1900')

    def test_none_and_empty_are_undated(self):
        self.assertEqual(humanize_edtf(None), 'Undated')
        self.assertEqual(humanize_edtf(''), 'Undated')


if __name__ == '__main__':
    unittest.main()
