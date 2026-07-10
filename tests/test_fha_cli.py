"""Tests for the top-level `fha` command boundary.

These focus on the user-facing contract from PR 03: no raw tracebacks by
default, close-command suggestions for typos, and a debug path for tool
builders who need the Python stack.
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import fha
from _lib import FhaConfigError, load_fha_yaml


class FhaCliBoundaryTests(unittest.TestCase):
    def test_unknown_command_suggests_close_match(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = fha.main(['dctor'])

        self.assertEqual(rc, 2)
        text = err.getvalue()
        self.assertIn('Did you mean `doctor`?', text)
        self.assertIn('fha doctor --help', text)
        self.assertNotIn('Traceback', text)

    def test_uncaught_exception_is_plain_without_debug(self) -> None:
        original = fha._intercept_doctor

        def boom(argv: list[str]) -> int | None:
            raise RuntimeError('simulated failure')

        fha._intercept_doctor = boom
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = fha.main(['doctor'])
        finally:
            fha._intercept_doctor = original

        self.assertEqual(rc, 3)
        text = err.getvalue()
        self.assertIn('simulated failure', text)
        self.assertIn('fha doctor', text)
        self.assertIn('--debug', text)
        self.assertNotIn('Traceback', text)

    def test_debug_shows_traceback_for_tool_builders(self) -> None:
        original = fha._intercept_doctor

        def boom(argv: list[str]) -> int | None:
            raise RuntimeError('simulated failure')

        fha._intercept_doctor = boom
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = fha.main(['--debug', 'doctor'])
        finally:
            fha._intercept_doctor = original

        self.assertEqual(rc, 3)
        text = err.getvalue()
        self.assertIn('Traceback', text)
        self.assertIn('simulated failure', text)

    def test_malformed_fha_yaml_message_is_plain_and_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'fha.yaml').write_text('roots:\n  documents: [unterminated\n', encoding='utf-8')

            with self.assertRaises(FhaConfigError) as ctx:
                load_fha_yaml(root, strict=True)

        text = str(ctx.exception)
        self.assertIn('fha.yaml has a problem on line', text)
        self.assertIn('roots:', text)
        self.assertIn('documents: documents', text)


class IdCheckRootGuardTests(unittest.TestCase):
    """The `fha id check` alias resolves its root through the shared
    `_lib.resolve_root_arg` chokepoint (round-2 finding 10 deleted the
    hand-copied guard here - it had already drifted to `.exists()` where
    index/find used `.is_file()`). A typo'd --root must refuse, never answer
    a false "not found in archive tree" against an empty folder."""

    def test_non_archive_root_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                rc = fha.main(['id', 'check', 'p-aaaaaaaaaa', '--root', tmp])
            self.assertEqual(rc, 3)
            text = err.getvalue()
            self.assertIn('does not look like an archive', text)
            self.assertIn('fha id check', text)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_fha_yaml_directory_refused_like_index_and_find(self) -> None:
        # The drift the chokepoint erases: `.exists()` would have accepted a
        # FOLDER named fha.yaml; the shared guard requires a real file.
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'fha.yaml').mkdir()
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                rc = fha.main(['id', 'check', 'p-aaaaaaaaaa', '--root', tmp])
            self.assertEqual(rc, 3)
            self.assertIn('does not look like an archive', err.getvalue())

    def test_root_with_fha_yaml_still_answers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                rc = fha.main(['id', 'check', 'p-aaaaaaaaaa', '--root', tmp])
            # A real (empty) archive: an honest not-found, never the refusal 3.
            self.assertNotEqual(rc, 3)
            self.assertIn('not found', out.getvalue().lower())


class SearchCheckAliasTests(unittest.TestCase):
    """`fha search` (= `fha find --text`) and `fha check` (= `fha lint`) are the
    two verbs a human actually types (persona B2). Each must produce output
    identical to its canonical form and appear in `fha --help`."""

    def _archive(self, tmp: str) -> Path:
        root = Path(tmp)
        (root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        notes = root / 'sources' / 'notes'
        notes.mkdir(parents=True)
        (notes / 'n.md').write_text(
            '---\nid: S-aaaaaaaaaa\ntitle: Note\nsource_type: note\n---\n\n'
            '## Notes\n\nRose Hartley lived in Kansas.\n', encoding='utf-8')
        return root

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fha.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_search_matches_find_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._archive(tmp)
            rc_s, out_s, _ = self._run(['search', 'rose', 'hartley', '--root', str(root)])
            rc_f, out_f, _ = self._run(['find', '--text', 'rose hartley', '--root', str(root)])
            self.assertEqual(rc_s, rc_f)
            self.assertEqual(out_s, out_f)

    def test_check_routes_to_lint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._archive(tmp)
            rc_c, out_c, _ = self._run(['check', '--root', str(root)])
            rc_l, out_l, _ = self._run(['lint', '--root', str(root)])
            self.assertEqual(rc_c, rc_l)
            self.assertEqual(out_c, out_l)

    def test_search_and_check_appear_in_help(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit):
                fha.main(['--help'])
        text = out.getvalue()
        self.assertIn('search', text)
        self.assertIn('check', text)


if __name__ == '__main__':
    unittest.main()
