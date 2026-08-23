"""
test_views_stub_guard.py - per-person companion views skip stub persons (SPEC §16).

Companion views (timeline / draft-queue) are curated-person files; the
per-person generator paths must skip a stub with a plain note and exit 1
(warning), never writing a GENERATED file into people/stubs/. The
--all-curated forms already filter by tier; this guards the direct P-id forms
so the curated-only rule lives in the tool, not in every caller's memory.

Per-person sources-index is the ONE exception (#76): it rewrites the `##
Sources` region INSIDE the profile wherever the profile already lives, so
neither the stub-tier guard nor the reserved-folder guard applies to it - a
stub's Sources section is meant to be populated BEFORE promotion, not
regenerated from scratch afterward. `SourcesIndexWorksOnStubsTests` below
is the positive proof; it is deliberately excluded from the skip-loops here.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import views
import index as index_mod
from _lib import load_fha_yaml, EXIT_CLEAN, EXIT_WARNINGS

CUR = 'P-aaaaaaaaaa'
STUB = 'P-bbbbbbbbbb'
# A record flipped to tier: curated but still physically parked in people/stubs/.
PROMOTED = 'P-cccccccccc'


def _person(pid: str, name: str, tier: str) -> str:
    return (f'---\nid: {pid}\nname: {name}\nliving: false\n'
            f'tier: {tier}\n---\n\n# {name}\n\n## Biography\n\nx\n')


class StubGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        (self.root / 'people' / 'stubs').mkdir(parents=True)
        (self.root / 'people' / '040 Test Couple').mkdir(parents=True)
        (self.root / 'sources' / 'notes').mkdir(parents=True)
        (self.root / 'fha.yaml').write_text(
            'roots:\n  documents: documents\n', encoding='utf-8')
        (self.root / 'people' / '040 Test Couple' / f'hartley__cur_{CUR}.md').write_text(
            _person(CUR, 'Cur Hartley', 'curated'), encoding='utf-8')
        (self.root / 'people' / 'stubs' / f'hartley__stub_{STUB}.md').write_text(
            _person(STUB, 'Stub Hartley', 'stub'), encoding='utf-8')
        # tier: curated but never moved out of people/stubs/ - the guard must still
        # refuse it, or a GENERATED companion lands in stubs/ (the wrong home).
        (self.root / 'people' / 'stubs' / f'hartley__promoted_{PROMOTED}.md').write_text(
            _person(PROMOTED, 'Promoted Hartley', 'curated'), encoding='utf-8')
        index_mod.build_index(self.root, load_fha_yaml(self.root))

    def _stub_dir_names(self) -> list[str]:
        return sorted(p.name for p in (self.root / 'people' / 'stubs').iterdir())

    def test_per_person_views_skip_stub(self) -> None:
        for runner in (views.run_timeline, views.run_draft_queue):
            res = runner(self.root, person_id=STUB)
            self.assertEqual(res.exit_code, EXIT_WARNINGS, runner.__name__)
            self.assertEqual(res.data.get('count'), 0, runner.__name__)
            self.assertFalse(res.changed, runner.__name__)
        # Nothing was written into stubs/ by those two - only the two source
        # records remain (sources-index DOES write here - see
        # SourcesIndexWorksOnStubsTests below - so it is not part of this loop).
        self.assertEqual(
            self._stub_dir_names(),
            [f'hartley__promoted_{PROMOTED}.md', f'hartley__stub_{STUB}.md'])

    def test_per_person_views_skip_curated_record_left_in_stubs(self) -> None:
        # A curated-tier record still in people/stubs/ must be refused by location,
        # so no GENERATED companion file is written beside it.
        for runner in (views.run_timeline, views.run_draft_queue):
            res = runner(self.root, person_id=PROMOTED)
            self.assertEqual(res.exit_code, EXIT_WARNINGS, runner.__name__)
            self.assertEqual(res.data.get('count'), 0, runner.__name__)
            self.assertFalse(res.changed, runner.__name__)
        # Names on disk are unchanged by those two (sources-index's own edit
        # rewrites the promoted record IN PLACE, not a new filename - checked
        # separately below).
        self.assertEqual(
            self._stub_dir_names(),
            [f'hartley__promoted_{PROMOTED}.md', f'hartley__stub_{STUB}.md'])

    def test_curated_person_still_generates(self) -> None:
        res = views.run_timeline(self.root, person_id=CUR)
        self.assertEqual(res.data.get('count'), 1)
        self.assertTrue(res.changed)

    def _reindex(self) -> None:
        # A prior view write stales the index; rebuild so the next view call's
        # freshness gate (strict open) passes and we test the write path itself.
        index_mod.build_index(self.root, load_fha_yaml(self.root))

    def test_successful_writes_exit_clean(self) -> None:
        # A successful companion write is not a warning: it exits 0 (the reindex
        # nudge stays as printed advice), while the skip paths above stay at 1.
        # Single-person forms:
        for runner in (views.run_timeline, views.run_sources_index,
                       views.run_draft_queue):
            self._reindex()
            res = runner(self.root, person_id=CUR)
            self.assertEqual(res.exit_code, EXIT_CLEAN, runner.__name__)
            self.assertTrue(res.changed, runner.__name__)
        # Bulk --all-curated forms and refresh:
        for runner in (
            lambda: views.run_timeline(self.root, all_curated=True),
            lambda: views.run_sources_index(self.root, all_curated=True),
            lambda: views.run_draft_queue(self.root, all_curated=True),
            lambda: views.run_refresh(self.root),
        ):
            self._reindex()
            res = runner()
            self.assertEqual(res.exit_code, EXIT_CLEAN)
            self.assertTrue(res.changed)

    def test_bulk_refresh_skips_curated_record_in_stubs(self) -> None:
        # The bulk paths (refresh / --all-curated) must apply the same location
        # filter as the per-person guard, or a curated record parked in stubs/
        # gets a GENERATED companion written beside it.
        res = views.run_refresh(self.root)
        self.assertTrue(res.changed)  # CUR (in a couple folder) still generated
        self.assertEqual(
            self._stub_dir_names(),
            [f'hartley__promoted_{PROMOTED}.md', f'hartley__stub_{STUB}.md'])

    def test_all_curated_skips_curated_record_in_stubs(self) -> None:
        res = views.run_timeline(self.root, all_curated=True)
        self.assertTrue(res.changed)
        self.assertEqual(
            self._stub_dir_names(),
            [f'hartley__promoted_{PROMOTED}.md', f'hartley__stub_{STUB}.md'])

    def test_bulk_warns_when_every_curated_is_in_stubs(self) -> None:
        # A fresh archive whose only curated record is parked in people/stubs/:
        # bulk generation must exit 1 (warning), not report a clean "nothing to
        # do" - otherwise automation treats the views as current.
        root = Path(tempfile.mkdtemp())
        (root / 'people' / 'stubs').mkdir(parents=True)
        (root / 'sources' / 'notes').mkdir(parents=True)
        (root / 'fha.yaml').write_text(
            'roots:\n  documents: documents\n', encoding='utf-8')
        (root / 'people' / 'stubs' / f'hartley__promoted_{PROMOTED}.md').write_text(
            _person(PROMOTED, 'Promoted Hartley', 'curated'), encoding='utf-8')
        index_mod.build_index(root, load_fha_yaml(root))
        for res in (views.run_refresh(root),
                    views.run_timeline(root, all_curated=True),
                    views.run_sources_index(root, all_curated=True),
                    views.run_draft_queue(root, all_curated=True)):
            self.assertEqual(res.exit_code, EXIT_WARNINGS)
            self.assertFalse(res.changed)

    def test_curated_record_in_connections_is_eligible(self) -> None:
        # #80: connections/ is no longer a reserved non-couple folder like
        # stubs/ - a curated record filed there (via `fha person promote
        # --into connections/`, SPEC §12.3) gets its per-person companion
        # views exactly like one in a couple folder.
        root = Path(tempfile.mkdtemp())
        (root / 'people' / 'connections').mkdir(parents=True)
        (root / 'people' / '060 Real Couple').mkdir(parents=True)
        (root / 'sources' / 'notes').mkdir(parents=True)
        (root / 'fha.yaml').write_text(
            'roots:\n  documents: documents\n', encoding='utf-8')
        conn_pid, couple_pid = 'P-dddddddddd', 'P-eeeeeeeeee'
        (root / 'people' / '060 Real Couple' / f'hartley__cur_{couple_pid}.md').write_text(
            _person(couple_pid, 'Couple Cur', 'curated'), encoding='utf-8')
        (root / 'people' / 'connections' / f'hartley__conn_{conn_pid}.md').write_text(
            _person(conn_pid, 'Conn Cur', 'curated'), encoding='utf-8')
        index_mod.build_index(root, load_fha_yaml(root))
        res = views.run_timeline(root, person_id=conn_pid)
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        self.assertTrue(res.changed)
        views.run_refresh(root)
        conn_dir = sorted(p.name for p in (root / 'people' / 'connections').iterdir())
        self.assertIn(f'hartley__conn_{conn_pid}.md', conn_dir)
        self.assertTrue(
            any('_timeline_' in n for n in conn_dir),
            f'expected a GENERATED timeline companion in connections/, got {conn_dir}')
        # The couple-LEVEL sources-index has no home in connections/ - there
        # is no couple, no bracket list to hang one on (SPEC §12.3).
        self.assertNotIn('sources-index.md', conn_dir)


class SourcesIndexWorksOnStubsTests(unittest.TestCase):
    """#76: per-person `fha views sources-index <P-id>` is the ONE content
    view exempt from the stub/reserved-folder guard the other two share -
    it rewrites the profile's own `## Sources` region wherever the profile
    already lives, so a stub can have its sources listed before promotion."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        (self.root / 'people' / 'stubs').mkdir(parents=True)
        (self.root / 'sources' / 'notes').mkdir(parents=True)
        (self.root / 'fha.yaml').write_text(
            'roots:\n  documents: documents\n', encoding='utf-8')
        (self.root / 'people' / 'stubs' / f'hartley__stub_{STUB}.md').write_text(
            _person(STUB, 'Stub Hartley', 'stub'), encoding='utf-8')
        (self.root / 'people' / 'stubs' / f'hartley__promoted_{PROMOTED}.md').write_text(
            _person(PROMOTED, 'Promoted Hartley', 'curated'), encoding='utf-8')
        index_mod.build_index(self.root, load_fha_yaml(self.root))

    def test_stub_gets_its_sources_section_refreshed_in_place(self) -> None:
        stub_path = self.root / 'people' / 'stubs' / f'hartley__stub_{STUB}.md'
        res = views.run_sources_index(self.root, person_id=STUB)
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        self.assertTrue(res.changed)
        # Rewritten IN PLACE - same filename, no new file created in stubs/.
        self.assertEqual(
            sorted(p.name for p in (self.root / 'people' / 'stubs').iterdir()),
            [f'hartley__promoted_{PROMOTED}.md', f'hartley__stub_{STUB}.md'])
        text = stub_path.read_text(encoding='utf-8')
        self.assertIn('## Sources', text)
        # SPEC §16 places `## Sources` FIRST among the profile's `##`
        # headings, right after the H1 - not wherever there happened to be
        # room. This fixture's `## Biography` already existed on disk with
        # no `## Sources` heading yet (the ordinary pre-#76-touched shape),
        # so a positional regression would bolt the new region onto the very
        # end of the file instead of inserting it at the top.
        self.assertLess(text.index('## Sources'), text.index('## Biography'))

    def test_stub_sources_section_lands_before_biography_not_at_eof(self) -> None:
        # The direct regression test for the positioning bug: a pre-#76-
        # shaped stub with a Biography already on disk, no `## Sources`
        # heading, must get the new region inserted BEFORE the existing
        # section, never appended after it.
        stub_path = self.root / 'people' / 'stubs' / f'hartley__stub_{STUB}.md'
        res = views.run_sources_index(self.root, person_id=STUB)
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        text = stub_path.read_text(encoding='utf-8')
        idx_title = text.index(f'# Stub Hartley')
        idx_sources = text.index('## Sources')
        idx_biography = text.index('## Biography')
        idx_x = text.index('\nx\n')
        self.assertLess(idx_title, idx_sources)
        self.assertLess(idx_sources, idx_biography)
        self.assertGreater(idx_x, idx_biography)   # the human's prose survives untouched

    def test_curated_record_left_in_stubs_also_gets_its_section_refreshed(self) -> None:
        promoted_path = self.root / 'people' / 'stubs' / f'hartley__promoted_{PROMOTED}.md'
        res = views.run_sources_index(self.root, person_id=PROMOTED)
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        self.assertTrue(res.changed)
        self.assertIn('## Sources', promoted_path.read_text(encoding='utf-8'))

    def test_all_curated_batch_form_still_excludes_a_stub(self) -> None:
        # The bulk --all-curated form stays curated-tier-only by its own name
        # (matching timeline/draft-queue's batch scope) - only the single
        # explicit <P-id> form works on a stub.
        res = views.run_sources_index(self.root, all_curated=True)
        stub_path = self.root / 'people' / 'stubs' / f'hartley__stub_{STUB}.md'
        self.assertNotIn('## Sources', stub_path.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
