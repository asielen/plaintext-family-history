"""
test_lib_generated_files.py - _lib's shared generated-file writer and Jinja2
template loader (views companions, the photoindex gallery, and the HTML view
twins all go through these two functions).

Covers two codex review findings on PR #29 (both P2, tools/_lib.py):

  - write_generated_file's `create_parents` flag: a views companion's parent
    is the person's own folder and must already exist - a stale index
    pointing at a folder that moved or was deleted must raise
    GeneratedFileParentMissing rather than silently `mkdir`-ing it back into
    existence from cache state. A caller whose target lives under a
    disposable top-level folder (generated/gallery/) opts in with
    create_parents=True and gets the old auto-create behavior.
  - render_template translates a missing/broken tools/templates/ file (or a
    render-time Jinja error) into a plain RuntimeError instead of leaking a
    raw jinja2.TemplateError; ImportError (Jinja2 itself not installed) is
    left to propagate unchanged, matching load_template's own contract.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import _lib
from _lib import (
    GeneratedFileParentMissing,
    GeneratedFileRefused,
    render_template,
    write_generated_file,
)


class WriteGeneratedFileParentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_parent_raises_by_default(self) -> None:
        out_path = self.root / 'people' / '040 Cur Hartley' / 'companion.md'
        with self.assertRaises(GeneratedFileParentMissing):
            write_generated_file(out_path, 'content', '<!-- GENERATED')
        self.assertFalse(out_path.parent.exists())

    def test_create_parents_true_still_creates_the_folder(self) -> None:
        out_path = self.root / 'generated' / 'gallery' / 'page.html'
        result = write_generated_file(
            out_path, '<!-- GENERATED body -->', '<!-- GENERATED', create_parents=True,
        )
        self.assertEqual(result, out_path)
        self.assertEqual(out_path.read_text(encoding='utf-8'), '<!-- GENERATED body -->')

    def test_existing_parent_needs_no_flag(self) -> None:
        out_path = self.root / 'people' / '040 Cur Hartley' / 'companion.md'
        out_path.parent.mkdir(parents=True)
        write_generated_file(out_path, '<!-- GENERATED body -->', '<!-- GENERATED')
        self.assertEqual(out_path.read_text(encoding='utf-8'), '<!-- GENERATED body -->')

    def test_refusal_still_fires_before_the_parent_check(self) -> None:
        # A hand-written file at the target is still refused, unaffected by
        # the create_parents plumbing (parent obviously exists here already).
        out_path = self.root / 'existing.html'
        out_path.write_text('<p>hand-made</p>', encoding='utf-8')
        with self.assertRaises(GeneratedFileRefused):
            write_generated_file(out_path, 'new content', '<!-- GENERATED')
        self.assertEqual(out_path.read_text(encoding='utf-8'), '<p>hand-made</p>')


class RenderTemplateErrorTranslationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_load_template = _lib.load_template

    def tearDown(self) -> None:
        _lib.load_template = self._orig_load_template

    def test_missing_template_becomes_plain_runtime_error(self) -> None:
        import jinja2

        def boom(name: str):
            raise jinja2.TemplateNotFound(name)

        _lib.load_template = boom
        with self.assertRaises(RuntimeError) as ctx:
            render_template('gallery.html')
        message = str(ctx.exception)
        self.assertIn('gallery.html', message)
        # The message names the real on-disk path, which carries the
        # platform's separator - compare in posix form so Windows passes too.
        self.assertIn('tools/templates', message.replace('\\', '/'))

    def test_render_time_error_becomes_plain_runtime_error(self) -> None:
        import jinja2

        class _BoomTemplate:
            def render(self, **context):
                raise jinja2.exceptions.UndefinedError('bad expression')

        _lib.load_template = lambda name: _BoomTemplate()
        with self.assertRaises(RuntimeError) as ctx:
            render_template('view.html', title='x')
        self.assertIn('view.html', str(ctx.exception))

    def test_import_error_propagates_unchanged(self) -> None:
        def boom(name: str):
            raise ImportError('no module named jinja2')

        _lib.load_template = boom
        with self.assertRaises(ImportError):
            render_template('gallery.html')


class DateOnlyChangeIsNotAWriteTests(unittest.TestCase):
    """A regenerated view that differs from the file only in the date in its
    GENERATED marker is left alone, so the first refresh of a new day does not
    rewrite every view, and the date says when the content last changed."""

    MARKER = '<!-- GENERATED by fha views timeline'

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name) / 'view.md'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _view(self, date: str, line: str = 'Born 1840 in Easton.') -> str:
        return f'{self.MARKER} on {date} - do not edit -->\n\n# Timeline\n\n{line}\n'

    def _write(self, content: str) -> bool:
        """Write through the shared writer; True when it touched the disk."""
        real = _lib.write_text_exact_atomic
        calls = []
        def spy(*a, **kw):
            calls.append(a)
            return real(*a, **kw)
        with mock.patch.object(_lib, 'write_text_exact_atomic', spy):
            write_generated_file(self.out, content, self.MARKER, create_parents=True)
        return bool(calls)

    def test_a_new_date_alone_writes_nothing(self) -> None:
        self.assertTrue(self._write(self._view('2026-11-19')))
        self.assertFalse(self._write(self._view('2026-11-20')))
        self.assertIn('on 2026-11-19', self.out.read_text(encoding='utf-8'))

    def test_a_content_change_writes_with_the_new_date(self) -> None:
        self._write(self._view('2026-11-19'))
        self.assertTrue(self._write(self._view('2026-11-20', 'Born 1841 in Easton.')))
        text = self.out.read_text(encoding='utf-8')
        self.assertIn('on 2026-11-20', text)
        self.assertIn('1841', text)

    def test_a_date_in_record_text_still_counts(self) -> None:
        self._write(self._view('2026-11-19', 'Generated on 1840-03-03 by hand.'))
        self.assertTrue(self._write(self._view('2026-11-19', 'Generated on 1841-03-03 by hand.')))

    def test_region_rewrite_keeps_the_file_when_only_the_date_moved(self) -> None:
        profile = '---\nid: P-9aaaaaaaaa\n---\n\n# Cur Hartley\n\n## Biography\n\nx\n'
        with mock.patch.object(_lib, 'generated_marker_date', lambda: '2026-11-19'):
            first, _created = _lib.write_generated_region(
                profile, heading='Sources', region_name='sources-index',
                subcommand='sources-index', body='- [[S-9aaaaaaaaa]]')
        with mock.patch.object(_lib, 'generated_marker_date', lambda: '2026-11-20'):
            same, created = _lib.write_generated_region(
                first, heading='Sources', region_name='sources-index',
                subcommand='sources-index', body='- [[S-9aaaaaaaaa]]')
            changed, _c = _lib.write_generated_region(
                first, heading='Sources', region_name='sources-index',
                subcommand='sources-index', body='- [[S-9bbbbbbbbb]]')
        self.assertIs(same, first)
        self.assertFalse(created)
        self.assertIn('on 2026-11-20', changed)
        self.assertIn('S-9bbbbbbbbb', changed)


class NotesFtsDeleteByRowidTests(unittest.TestCase):
    """Deleting one file's search rows by rowid removes exactly that file's
    rows, never a neighbour whose path merely starts the same way."""

    def test_only_the_exact_path_goes(self) -> None:
        import sqlite3
        conn = sqlite3.connect(':memory:')
        conn.execute('CREATE VIRTUAL TABLE notes_fts USING fts5(path, content)')
        rows = [('people/a/hartley.md', 'one'), ('people/a/hartley.md', 'dup'),
                ('people/a/hartley.md.bak', 'two'), ('people/a/hartley', 'three'),
                ('people/a/Hartley.md', 'four')]
        conn.executemany('INSERT INTO notes_fts(path, content) VALUES (?,?)', rows)
        ids = _lib.notes_fts_rowids_by_path(conn)
        _lib.delete_notes_fts_path(conn, ids, 'people/a/hartley.md')
        _lib.delete_notes_fts_path(conn, ids, 'people/a/hartley.md')   # second call is a no-op
        left = sorted(r[0] for r in conn.execute('SELECT content FROM notes_fts'))
        self.assertEqual(left, ['four', 'three', 'two'])
        conn.close()


if __name__ == '__main__':
    unittest.main()
