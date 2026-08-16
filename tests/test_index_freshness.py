"""
test_index_freshness.py - view writes must not stale the index (#37).

The review-claims close-out runs three companion views per curated person:
`fha views timeline`, `fha views sources-index`, `fha views draft-queue`. Each
writes a GENERATED companion under people/, and `newest_record_mtime` counted
those files - so the SECOND call in the sequence failed on the strict
freshness gate ("index is stale; run 'fha index'"), and ten people meant
thirty view writes and thirty full rebuilds. Generated companions are written
FROM the index; nothing the index needs to re-read changed. They are now
excluded from the watermark; the human-written `research` companion is not.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import index as index_mod
import views
from _lib import (
    EXIT_CLEAN, load_fha_yaml, newest_record_mtime, open_index_db,
)

CUR = 'P-aaaaaaaaaa'


def _person(pid: str, name: str, tier: str) -> str:
    return (f'---\nid: {pid}\nname: {name}\nliving: false\n'
            f'tier: {tier}\n---\n\n# {name}\n\n## Biography\n\nx\n')


class ViewWritesKeepIndexFreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.folder = self.root / 'people' / '040 Test Couple'
        self.folder.mkdir(parents=True)
        (self.root / 'sources' / 'notes').mkdir(parents=True)
        (self.root / 'fha.yaml').write_text(
            'roots:\n  documents: documents\n', encoding='utf-8')
        (self.folder / f'hartley__cur_{CUR}.md').write_text(
            _person(CUR, 'Cur Hartley', 'curated'), encoding='utf-8')
        index_mod.build_index(self.root, load_fha_yaml(self.root))

    def _index_is_fresh(self) -> bool:
        db = self.root / '.cache' / 'index.sqlite'
        return db.stat().st_mtime >= newest_record_mtime(self.root)

    def test_three_consecutive_view_writes_need_no_reindex(self) -> None:
        # The documented close-out, run as written, with no rebuild between.
        for runner in (views.run_timeline, views.run_sources_index,
                       views.run_draft_queue):
            res = runner(self.root, person_id=CUR)
            self.assertEqual(res.exit_code, EXIT_CLEAN, runner.__name__)
            self.assertTrue(res.changed, runner.__name__)
            self.assertTrue(self._index_is_fresh(), runner.__name__)
        # And a strict reader (what the next view call is) still opens it.
        conn = open_index_db(self.root, ('persons',), strict=True)
        self.assertIsNotNone(conn)
        conn.close()

    def test_generated_companions_are_excluded_from_the_watermark(self) -> None:
        before = newest_record_mtime(self.root)
        future = time.time() + 3600
        for kind in ('timeline', 'sources-index', 'draft-queue'):
            p = self.folder / f'hartley__cur_{kind}_{CUR}.md'
            p.write_text('<!-- GENERATED -->\n', encoding='utf-8')
            os.utime(p, (future, future))
        self.assertEqual(newest_record_mtime(self.root), before)

    def test_research_companion_still_counts(self) -> None:
        # Human-written: an edit there is an edit to a record and must stale.
        before = newest_record_mtime(self.root)
        future = time.time() + 3600
        p = self.folder / f'hartley__cur_research_{CUR}.md'
        p.write_text('---\nid: P-aaaaaaaaaa\n---\n## Research Notes\n', encoding='utf-8')
        os.utime(p, (future, future))
        self.assertGreater(newest_record_mtime(self.root), before)

    def test_profile_edit_still_stales(self) -> None:
        before = newest_record_mtime(self.root)
        future = time.time() + 3600
        p = self.folder / f'hartley__cur_{CUR}.md'
        os.utime(p, (future, future))
        self.assertGreater(newest_record_mtime(self.root), before)


if __name__ == '__main__':
    unittest.main()
