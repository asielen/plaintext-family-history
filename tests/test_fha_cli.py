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


if __name__ == '__main__':
    unittest.main()
