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
        # The couple-folder sources-index carries no P-id in its name and is
        # what `fha views refresh` writes for every couple folder.
        p = self.folder / 'sources-index.md'
        p.write_text('<!-- GENERATED -->\n', encoding='utf-8')
        os.utime(p, (future, future))
        self.assertEqual(newest_record_mtime(self.root), before)

    def test_refresh_twice_needs_no_reindex(self) -> None:
        # `fha views refresh` writes per-person AND couple-folder companions;
        # a second refresh must not fail on the strict gate.
        first = views.run_refresh(self.root)
        self.assertEqual(first.exit_code, EXIT_CLEAN, first.messages)
        self.assertTrue(self._index_is_fresh())
        second = views.run_refresh(self.root)
        self.assertEqual(second.exit_code, EXIT_CLEAN, second.messages)

    def test_a_note_named_sources_index_still_counts(self) -> None:
        # notes/ is a place a human writes freely, and `_index_notes` indexes
        # every .md under it - including one he happened to name
        # `sources-index.md`. Excluding it by basename alone meant editing that
        # note left the index reading 'fresh' while `fha find --text` served
        # its old notes_fts row indefinitely, with nothing ever asking for a
        # reindex.
        notes = self.root / 'notes'
        notes.mkdir(exist_ok=True)
        p = notes / 'sources-index.md'
        p.write_text('# Where my sources live\n\nThe Cole letters are in the attic.\n',
                     encoding='utf-8')
        before = newest_record_mtime(self.root)
        future = time.time() + 3600
        os.utime(p, (future, future))
        self.assertGreater(newest_record_mtime(self.root), before)

    def test_a_sources_index_outside_a_couple_folder_still_counts(self) -> None:
        # The unscoped `sources-index.md` name is only a generated view at the
        # root of a couple folder, which is where `fha views` writes it.
        # people/stubs/ is not one, and neither is a subfolder of a couple
        # folder.
        for parent in (self.root / 'people' / 'stubs',
                       self.folder / 'scans'):
            parent.mkdir(parents=True, exist_ok=True)
            p = parent / 'sources-index.md'
            p.write_text('my own list of sources\n', encoding='utf-8')
            before = newest_record_mtime(self.root)
            future = time.time() + 3600
            os.utime(p, (future, future))
            self.assertGreater(newest_record_mtime(self.root), before, str(p))
            p.unlink()

    def test_a_human_file_with_a_generated_name_still_counts(self) -> None:
        # The name is a convention; the GENERATED header is the ownership
        # contract. A human file that borrows a generated name - which
        # `write_generated_file` then refuses to overwrite, so it can sit there
        # for years - is a record, and editing it must stale the index.
        future = time.time() + 3600
        for name in (f'hartley__cur_timeline_{CUR}.md', 'sources-index.md'):
            p = self.folder / name
            p.write_text('# My own notes\n\nNot generated by anything.\n',
                         encoding='utf-8')
            before = newest_record_mtime(self.root)
            os.utime(p, (future, future))
            self.assertGreater(newest_record_mtime(self.root), before, name)
            p.unlink()

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
