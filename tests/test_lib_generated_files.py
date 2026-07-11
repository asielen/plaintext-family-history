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
        self.assertIn('tools/templates', message)

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


if __name__ == '__main__':
    unittest.main()
