"""
test_views_index_sync.py - generated companions stay searchable and current.

Companion views (timeline, sources-index, draft-queue) are excluded from the
freshness watermark (#37) so a batch of view writes never forces a rebuild.
But `index._index_person` still puts every companion body into `notes_fts`,
so the exclusion on its own left the search text frozen: after `fha index`
then `fha views refresh`, `fha find --text` kept returning the PREVIOUS
timeline's words while the index reported itself fresh, and `fha views clean`
left rows for files that no longer existed.

The view write and clean paths now maintain those rows themselves, through the
shared `_lib.sync_generated_view_rows` (views cannot import index - tools never
import tools). These tests pin both halves: the rows follow the files, no
duplicates accumulate, the result matches a full rebuild, and #37 stays closed.
Fixtures only.
"""

import contextlib
import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import index as index_mod
import views
from _lib import (
    EXIT_CLEAN,
    load_fha_yaml,
    newest_record_mtime,
    open_index_db,
    sync_generated_view_rows,
)

PID = 'P-9aaaaaaaaa'
SID1 = 'S-9aaaaaaaaa'
SID2 = 'S-9bbbbbbbbb'
FOLDER = '040 Cur Hartley'


def _person(pid: str, name: str) -> str:
    return (f'---\nid: {pid}\nname: {name}\nliving: false\n'
            f'tier: curated\n---\n\n# {name}\n\n## Biography\n\nx\n')


def _source(sid: str, title: str, value: str, cid: str) -> str:
    return (
        f'---\nid: {sid}\ntitle: {title}\nsource_type: other\n'
        f'date: 1899\n---\n\n## Claims\n```yaml\n'
        f'- value: "{value}"\n  id: {cid}\n  type: residence\n'
        f'  persons: [{PID}]\n  date: 1899\n  status: accepted\n'
        f'  reviewed: 2026-01-01\n  confidence: high\n'
        f'  information: primary\n  evidence: direct\n  notes: x.\n```\n'
    )


class _SyncBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.folder = self.root / 'people' / FOLDER
        self.folder.mkdir(parents=True)
        (self.root / 'sources' / 'notes').mkdir(parents=True)
        (self.root / 'fha.yaml').write_text(
            'roots:\n  documents: documents\n', encoding='utf-8')
        (self.folder / f'hartley__cur_{PID}.md').write_text(
            _person(PID, 'Cur Hartley'), encoding='utf-8')
        (self.root / 'sources' / 'notes' / f'first_{SID1.lower()}.md').write_text(
            _source(SID1, 'Kingfisher Directory', 'lived at Kingfisher Lane',
                    'C-9aaaaaaaaa'),
            encoding='utf-8')
        self._reindex()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _reindex(self) -> None:
        index_mod.build_index(self.root, load_fha_yaml(self.root))

    def _quiet(self, fn, *args, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            res = fn(*args, **kwargs)
        return res, out.getvalue(), err.getvalue()

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        conn.row_factory = sqlite3.Row
        return conn

    def _fts_rows(self, needle: str) -> list[str]:
        conn = self._db()
        try:
            return [r['path'] for r in conn.execute(
                'SELECT path FROM notes_fts WHERE content LIKE ?',
                (f'%{needle}%',))]
        finally:
            conn.close()

    def _rows_for(self, table: str, rel: str) -> int:
        conn = self._db()
        try:
            return conn.execute(
                f'SELECT COUNT(*) FROM {table} WHERE path=?', (rel,)).fetchone()[0]
        finally:
            conn.close()

    def _timeline_rel(self) -> str:
        return str(Path('people') / FOLDER / f'hartley__cur_timeline_{PID}.md')


class ViewWriteKeepsSearchCurrentTests(_SyncBase):
    def test_refresh_makes_new_timeline_text_searchable_without_a_rebuild(self) -> None:
        res, _out, _err = self._quiet(views.run_refresh, self.root)
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        # The claim's place text reached notes_fts through the timeline file,
        # with no `fha index` in between.
        self.assertIn(self._timeline_rel(), self._fts_rows('Kingfisher Lane'))

    def test_a_second_source_reaches_search_through_the_refreshed_view(self) -> None:
        self._quiet(views.run_refresh, self.root)
        # A new source is indexed the ordinary way; the person's timeline is
        # regenerated afterwards, exactly the documented review close-out.
        (self.root / 'sources' / 'notes' / f'second_{SID2.lower()}.md').write_text(
            _source(SID2, 'Ferry Road Directory', 'lived at Ferry Road',
                    'C-9bbbbbbbbb'),
            encoding='utf-8')
        self._reindex()
        self.assertEqual(self._fts_rows('Ferry Road'),
                         [str(Path('sources') / 'notes' / f'second_{SID2.lower()}.md')])
        self._quiet(views.run_refresh, self.root)
        self.assertIn(self._timeline_rel(), self._fts_rows('Ferry Road'))
        self.assertEqual(self._rows_for('notes_fts', self._timeline_rel()), 1)

    def test_obsolete_view_text_stops_being_searchable(self) -> None:
        # The reviewer's case: the claim behind a timeline line changes, so the
        # regenerated timeline no longer says what it said. Without the row
        # sync the old wording answered `fha find --text` forever, because a
        # generated companion never moves the freshness watermark.
        self._quiet(views.run_refresh, self.root)
        self.assertIn(self._timeline_rel(), self._fts_rows('Kingfisher Lane'))
        (self.root / 'sources' / 'notes' / f'first_{SID1.lower()}.md').write_text(
            _source(SID1, 'Corrected Directory', 'lived at Ferry Road',
                    'C-9aaaaaaaaa'),
            encoding='utf-8')
        self._reindex()
        self._quiet(views.run_refresh, self.root)
        self.assertEqual(self._fts_rows('Kingfisher Lane'), [])
        self.assertIn(self._timeline_rel(), self._fts_rows('Ferry Road'))

    def test_repeated_refreshes_do_not_stack_duplicate_rows(self) -> None:
        for _ in range(3):
            self._quiet(views.run_refresh, self.root)
        self.assertEqual(self._rows_for('notes_fts', self._timeline_rel()), 1)
        self.assertEqual(self._rows_for('person_files', self._timeline_rel()), 1)

    def test_single_person_view_writes_sync_too(self) -> None:
        for runner in (views.run_timeline, views.run_sources_index,
                       views.run_draft_queue):
            res, _out, _err = self._quiet(runner, self.root, person_id=PID)
            self.assertEqual(res.exit_code, EXIT_CLEAN, runner.__name__)
        self.assertEqual(self._rows_for('notes_fts', self._timeline_rel()), 1)

    def test_rows_match_a_full_rebuild(self) -> None:
        self._quiet(views.run_refresh, self.root)
        conn = self._db()
        try:
            incremental = sorted(
                (r['path'], r['content']) for r in
                conn.execute('SELECT path, content FROM notes_fts')
                if str(r['path']).startswith('people'))
            files_inc = sorted(
                (r['person_id'], r['kind'], r['path'], r['generated'])
                for r in conn.execute('SELECT * FROM person_files'))
        finally:
            conn.close()
        self._reindex()
        conn = self._db()
        try:
            full = sorted(
                (r['path'], r['content']) for r in
                conn.execute('SELECT path, content FROM notes_fts')
                if str(r['path']).startswith('people'))
            files_full = sorted(
                (r['person_id'], r['kind'], r['path'], r['generated'])
                for r in conn.execute('SELECT * FROM person_files'))
        finally:
            conn.close()
        self.assertEqual(incremental, full)
        self.assertEqual(files_inc, files_full)

    def test_issue_37_stays_closed(self) -> None:
        # The whole point of the watermark exclusion: a batch of view writes
        # must not force a rebuild. The row sync writes to index.sqlite, which
        # only moves its mtime FORWARD - never past a record's.
        for _ in range(2):
            res, _out, _err = self._quiet(views.run_refresh, self.root)
            self.assertEqual(res.exit_code, EXIT_CLEAN)
            db = self.root / '.cache' / 'index.sqlite'
            self.assertGreaterEqual(db.stat().st_mtime,
                                    newest_record_mtime(self.root))
        conn = open_index_db(self.root, ('persons',), strict=True)
        self.assertIsNotNone(conn)
        conn.close()

    def test_html_writes_touch_no_rows(self) -> None:
        self._quiet(views.run_refresh, self.root, fmt='html')
        conn = self._db()
        try:
            html_rows = [r['path'] for r in conn.execute(
                "SELECT path FROM notes_fts WHERE path LIKE '%generated%'")]
        finally:
            conn.close()
        self.assertEqual(html_rows, [])


class CleanRemovesRowsTests(_SyncBase):
    def test_clean_deletes_the_companion_rows_and_exits_clean(self) -> None:
        self._quiet(views.run_refresh, self.root)
        self.assertEqual(self._rows_for('notes_fts', self._timeline_rel()), 1)

        res, out, _err = self._quiet(views.run_clean, self.root)
        # Nothing stale is left behind, so the sweep is a clean exit.
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        self.assertNotIn('still appear in .cache/index.sqlite', out)
        self.assertEqual(self._rows_for('notes_fts', self._timeline_rel()), 0)
        self.assertEqual(self._rows_for('person_files', self._timeline_rel()), 0)
        # The profile's own row is untouched - clean never deletes records.
        profile_rel = str(Path('people') / FOLDER / f'hartley__cur_{PID}.md')
        self.assertEqual(self._rows_for('person_files', profile_rel), 1)
        # Searching the deleted view's text finds only the source it came from.
        self.assertNotIn(self._timeline_rel(), self._fts_rows('Kingfisher Lane'))

    def test_a_file_that_will_not_delete_is_named_not_thrown(self) -> None:
        # A locked or read-only companion (open in another program) must not
        # abort the sweep or reach the human as a traceback: it is named, the
        # rest still go, and the run exits non-zero so nothing reads as clean.
        self._quiet(views.run_refresh, self.root)
        stuck = self._timeline_rel()
        real_unlink = Path.unlink

        def fake_unlink(self, *args, **kwargs):
            if self.name.endswith(f'timeline_{PID}.md'):
                raise PermissionError(13, 'Permission denied')
            return real_unlink(self, *args, **kwargs)

        with mock.patch.object(Path, 'unlink', fake_unlink):
            res, out, err = self._quiet(views.run_clean, self.root)
        self.assertEqual(res.exit_code, 1)
        self.assertIn('could not remove', err)
        self.assertNotIn('Traceback', err)
        self.assertTrue((self.root / stuck).exists())
        # The others really went, and their rows with them.
        draft_rel = str(Path('people') / FOLDER / f'hartley__cur_draft-queue_{PID}.md')
        self.assertFalse((self.root / draft_rel).exists())
        self.assertEqual(self._rows_for('notes_fts', draft_rel), 0)
        # The file that survived keeps its row - the index still matches disk.
        self.assertEqual(self._rows_for('notes_fts', stuck), 1)

    def test_clean_dry_run_touches_no_rows(self) -> None:
        self._quiet(views.run_refresh, self.root)
        res, _out, _err = self._quiet(views.run_clean, self.root, dry_run=True)
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        self.assertEqual(self._rows_for('notes_fts', self._timeline_rel()), 1)
        self.assertEqual(self._rows_for('person_files', self._timeline_rel()), 1)


class SyncHelperTests(_SyncBase):
    """The shared _lib engine's own contract."""

    def test_no_index_is_not_an_error(self) -> None:
        (self.root / '.cache' / 'index.sqlite').unlink()
        self.assertEqual(
            sync_generated_view_rows(self.root, written=[
                self.folder / f'hartley__cur_timeline_{PID}.md']),
            'index_absent')

    def test_non_companion_paths_are_ignored(self) -> None:
        # A profile, a couple-folder index (no P-id, never indexed), and an
        # HTML twin under generated/ must all leave the rows alone.
        profile = self.folder / f'hartley__cur_{PID}.md'
        before = self._rows_for('person_files',
                                str(Path('people') / FOLDER / profile.name))
        status = sync_generated_view_rows(self.root, written=[
            profile,
            self.folder / 'sources-index.md',
            self.root / 'generated' / 'views' / f'hartley__cur_timeline_{PID}.html',
        ])
        self.assertEqual(status, 'indexed')
        self.assertEqual(
            self._rows_for('person_files',
                           str(Path('people') / FOLDER / profile.name)),
            before)


if __name__ == '__main__':
    unittest.main()
