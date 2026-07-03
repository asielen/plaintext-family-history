"""
test_views_stub_guard.py - per-person companion views skip stub persons (SPEC §16).

Companion views (timeline / sources-index / draft-queue) are curated-person
files; the per-person generator paths must skip a stub with a plain note and
exit 1 (warning), never writing a GENERATED file into people/stubs/. The
--all-curated forms already filter by tier; this guards the direct P-id forms
so the curated-only rule lives in the tool, not in every caller's memory.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import views
import index as index_mod
from _lib import load_fha_yaml, EXIT_WARNINGS

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
        for runner in (views.run_timeline, views.run_sources_index,
                       views.run_draft_queue):
            res = runner(self.root, person_id=STUB)
            self.assertEqual(res.exit_code, EXIT_WARNINGS, runner.__name__)
            self.assertEqual(res.data.get('count'), 0, runner.__name__)
            self.assertFalse(res.changed, runner.__name__)
        # Nothing was written into stubs/ - only the two source records remain.
        self.assertEqual(
            self._stub_dir_names(),
            [f'hartley__promoted_{PROMOTED}.md', f'hartley__stub_{STUB}.md'])

    def test_per_person_views_skip_curated_record_left_in_stubs(self) -> None:
        # A curated-tier record still in people/stubs/ must be refused by location,
        # so no GENERATED companion file is written beside it.
        for runner in (views.run_timeline, views.run_sources_index,
                       views.run_draft_queue):
            res = runner(self.root, person_id=PROMOTED)
            self.assertEqual(res.exit_code, EXIT_WARNINGS, runner.__name__)
            self.assertEqual(res.data.get('count'), 0, runner.__name__)
            self.assertFalse(res.changed, runner.__name__)
        self.assertEqual(
            self._stub_dir_names(),
            [f'hartley__promoted_{PROMOTED}.md', f'hartley__stub_{STUB}.md'])

    def test_curated_person_still_generates(self) -> None:
        res = views.run_timeline(self.root, person_id=CUR)
        self.assertEqual(res.data.get('count'), 1)
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

    def test_curated_record_in_connections_is_ineligible(self) -> None:
        # connections/ is a reserved non-couple folder like stubs/; a curated
        # record parked there must be refused by both the per-person and bulk
        # paths (nothing GENERATED written into connections/).
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
        self.assertEqual(res.exit_code, EXIT_WARNINGS)
        self.assertFalse(res.changed)
        views.run_refresh(root)
        conn_dir = sorted(p.name for p in (root / 'people' / 'connections').iterdir())
        self.assertEqual(conn_dir, [f'hartley__conn_{conn_pid}.md'])


if __name__ == '__main__':
    unittest.main()
