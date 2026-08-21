"""
test_views_companion_view_marker.py - #77: generated companions sort with the
person's own record, not ahead of it.

`_out_path_for` (views.py) builds the filename for the three GENERATED
companion kinds (timeline, sources-index, draft-queue). Before this fix the
kind word alone decided where a companion sorted in its folder, so the
disposable `draft-queue` cache ('d') sorted ahead of the profile itself
('p') - the first thing in every person folder was a cache, not the record.
A `view_` marker inserted immediately before the kind word fixes the order:
`view` ('v') sorts after both the bare profile and `_research_` ('p' < 'r' <
'v' in plain lexicographic order), so a folder listing reads the profile,
then the research file, then the three generated companions together, below
both (SPEC §13, §16).

These tests pin three things directly, rather than assuming them from the
code read:
  1. `_out_path_for`'s literal output shape for all three generated kinds.
  2. The actual sort order of the five companion-family filenames, including
     the issue's own `kelly__mary_*` example.
  3. `fha views clean` still sweeps an old-style (pre-#77) generated
     companion with no parser change - proving the issue's migration claim
     ("clean, then regenerate is a complete migration") rather than assuming
     it, since ownership is decided by the GENERATED header alone, never the
     filename (_owned_by_views).

Run: py -3.14 -m unittest tests.test_views_companion_view_marker -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import views
from _lib import EXIT_CLEAN

PID = 'P-de957bcda1'


class OutPathForInsertsViewMarkerTests(unittest.TestCase):
    """`_out_path_for(profile_path, kind, person_id)` is a pure path
    function - string manipulation only, no archive fixture needed."""

    def setUp(self) -> None:
        self.profile_path = (
            Path('people') / '040 Hartley' / f'hartley__thomas_edward_{PID}.md')

    def test_timeline_gets_the_view_marker(self) -> None:
        out = views._out_path_for(self.profile_path, 'timeline', PID)
        self.assertEqual(out.name, f'hartley__thomas_edward_view_timeline_{PID}.md')

    def test_sources_index_gets_the_view_marker(self) -> None:
        out = views._out_path_for(self.profile_path, 'sources-index', PID)
        self.assertEqual(out.name, f'hartley__thomas_edward_view_sources-index_{PID}.md')

    def test_draft_queue_gets_the_view_marker(self) -> None:
        out = views._out_path_for(self.profile_path, 'draft-queue', PID)
        self.assertEqual(out.name, f'hartley__thomas_edward_view_draft-queue_{PID}.md')

    def test_output_lands_beside_the_profile(self) -> None:
        out = views._out_path_for(self.profile_path, 'timeline', PID)
        self.assertEqual(out.parent, self.profile_path.parent)

    def test_doubled_kind_word_case_gets_the_marker_too(self) -> None:
        # Marie Timeline Hartley's own profile stem already ends in
        # "_timeline" (SPEC §13's kind slot and the last given-name segment
        # are the same slot) - the generated timeline for her comes out with
        # the word doubled, now separated by the marker rather than sitting
        # adjacent (views.py's _out_path_for docstring; TOOLING.md's W122
        # entry names this exact filename).
        marie_profile = (
            Path('people') / '040 Hartley' / f'hartley__marie_timeline_{PID}.md')
        out = views._out_path_for(marie_profile, 'timeline', PID)
        self.assertEqual(out.name, f'hartley__marie_timeline_view_timeline_{PID}.md')


class CompanionFamilySortOrderTests(unittest.TestCase):
    """The issue's actual complaint, checked directly: a plain filename sort
    must put the profile first, the research file second, and the three
    generated companions together after both - never a generated file
    first."""

    def test_profile_and_research_sort_before_every_generated_companion(self) -> None:
        profile_path = (
            Path('people') / '040 Hartley' / f'hartley__thomas_edward_{PID}.md')
        research_path = profile_path.with_name(
            f'hartley__thomas_edward_research_{PID}.md')
        generated = [
            views._out_path_for(profile_path, kind, PID)
            for kind in ('timeline', 'sources-index', 'draft-queue')
        ]
        names = sorted(p.name for p in [profile_path, research_path] + generated)
        self.assertEqual(names, [
            profile_path.name,
            research_path.name,
            f'hartley__thomas_edward_view_draft-queue_{PID}.md',
            f'hartley__thomas_edward_view_sources-index_{PID}.md',
            f'hartley__thomas_edward_view_timeline_{PID}.md',
        ])

    def test_the_issues_own_five_file_example_sorts_as_proposed(self) -> None:
        # kelly__mary_* from the GitHub issue body itself (#77), reproduced
        # exactly - including the issue's own "draft-queue sorts first" bug
        # being fixed and its proposed desired order.
        pid = 'P-ah07m3282y'
        stem = 'kelly__mary'
        profile = f'{stem}_{pid}.md'
        research = f'{stem}_research_{pid}.md'
        profile_path = Path('people') / '004 Kelly' / profile
        generated = [
            views._out_path_for(profile_path, kind, pid).name
            for kind in ('draft-queue', 'sources-index', 'timeline')
        ]
        names = sorted([profile, research] + generated)
        self.assertEqual(names, [
            profile,
            research,
            f'{stem}_view_draft-queue_{pid}.md',
            f'{stem}_view_sources-index_{pid}.md',
            f'{stem}_view_timeline_{pid}.md',
        ])


class CleanSweepsOldStyleCompanionsTests(unittest.TestCase):
    """The issue's migration claim, verified rather than assumed: `fha views
    clean` decides ownership purely from the <!-- GENERATED --> header on a
    file's first non-blank line (`_owned_by_views` in views.py), never from
    the filename - so a leftover pre-#77 companion sweeps exactly like a
    freshly-written new-style one, with no parser or clean-logic change
    needed to migrate an existing archive."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.folder = self.root / 'people' / '040 Hartley'
        self.folder.mkdir(parents=True)
        self.profile = self.folder / f'hartley__thomas_edward_{PID}.md'
        self.profile.write_text(
            f'---\nid: {PID}\nname: Thomas Edward Hartley\nliving: false\n'
            f'tier: curated\n---\n\n# Thomas Edward Hartley\n', encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _generated_text(self, kind: str = 'timeline') -> str:
        return (
            f'<!-- GENERATED by fha views {kind} on 2026-01-01'
            ' - do not edit; regenerate instead -->\n\n# Timeline\n')

    def test_an_old_style_named_generated_file_is_removed(self) -> None:
        old_style = self.folder / f'hartley__thomas_edward_timeline_{PID}.md'
        old_style.write_text(self._generated_text(), encoding='utf-8')

        res = views.run_clean(self.root)

        self.assertEqual(res.exit_code, EXIT_CLEAN)
        self.assertFalse(old_style.exists())
        self.assertTrue(self.profile.exists())   # the record itself is never touched

    def test_a_new_style_named_generated_file_is_removed_too(self) -> None:
        # Symmetric check: the sweep is equally unbothered by either shape,
        # because it never reads the filename's kind word at all - only the
        # GENERATED header decides.
        new_style = self.folder / f'hartley__thomas_edward_view_timeline_{PID}.md'
        new_style.write_text(self._generated_text(), encoding='utf-8')

        res = views.run_clean(self.root)

        self.assertEqual(res.exit_code, EXIT_CLEAN)
        self.assertFalse(new_style.exists())
        self.assertTrue(self.profile.exists())

    def test_an_old_style_hand_written_lookalike_is_left_alone(self) -> None:
        # No GENERATED header - a human file merely named like a companion -
        # must survive the sweep regardless of which shape its name follows.
        lookalike = self.folder / f'hartley__thomas_edward_timeline_{PID}.md'
        lookalike.write_text('# My own hand-written notes\n', encoding='utf-8')

        res = views.run_clean(self.root)

        self.assertEqual(res.exit_code, EXIT_CLEAN)
        self.assertTrue(lookalike.exists())


if __name__ == '__main__':
    unittest.main()
