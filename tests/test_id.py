"""Tests for `fha id` - mint bounds.

`-n` was previously only floored at 1; a fat-fingered `-n 500000` would grind
minting hundreds of thousands of verified IDs. It is now capped at 100.
"""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import fha
from _lib import EXIT_CLEAN, EXIT_FAILURE


class IdMintBoundsTests(unittest.TestCase):
    def _archive(self, tmp: str) -> Path:
        root = Path(tmp)
        (root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        return root

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fha.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_n_over_cap_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._archive(tmp)
            rc, out, err = self._run(['id', 'mint', 'P', '-n', '500000', '--root', str(root)])
            self.assertEqual(rc, EXIT_FAILURE)
            self.assertIn('100', err)
            self.assertEqual(out.strip(), '')

    def test_n_below_one_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._archive(tmp)
            rc, _, err = self._run(['id', 'mint', 'P', '-n', '0', '--root', str(root)])
            self.assertEqual(rc, EXIT_FAILURE)
            self.assertIn('at least 1', err)

    def test_n_within_cap_mints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._archive(tmp)
            rc, out, _ = self._run(['id', 'mint', 'P', '-n', '3', '--root', str(root)])
            self.assertEqual(rc, EXIT_CLEAN)
            ids = [ln for ln in out.splitlines() if ln.strip()]
            self.assertEqual(len(ids), 3)
            self.assertTrue(all(i.startswith('p-') for i in ids))

    def test_n_at_cap_mints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._archive(tmp)
            rc, out, _ = self._run(['id', 'mint', 'P', '-n', '100', '--root', str(root)])
            self.assertEqual(rc, EXIT_CLEAN)
            ids = [ln for ln in out.splitlines() if ln.strip()]
            self.assertEqual(len(ids), 100)


if __name__ == '__main__':
    unittest.main()


class LowercaseIdsTests(unittest.TestCase):
    """SPEC §10: every tool writes an ID all lowercase, prefix included
    (`p-3kq9v8x2m1`), and reads any casing. Before this, minting wrote
    `P-…` and every message, filename and keyword the tools wrote showed the
    capital prefix, so the owner's records carried two spellings of one ID."""

    def test_minted_ids_are_all_lowercase(self) -> None:
        import _lib
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
            for prefix in ('P', 's', 'C', 'l', 'H'):
                with self.subTest(prefix=prefix):
                    for new_id in _lib.mint_ids(prefix, 5, root):
                        self.assertEqual(new_id, new_id.lower())
                        self.assertTrue(new_id.startswith(prefix.lower() + '-'))
                        self.assertTrue(_lib.is_valid_id(new_id))

    def test_fha_id_mint_prints_lowercase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                rc = fha.main(['id', 'mint', 'S', '-n', '2', '--root', str(root)])
            self.assertEqual(rc, EXIT_CLEAN)
            ids = [ln.strip() for ln in out.getvalue().splitlines() if ln.strip()]
            self.assertEqual(len(ids), 2)
            self.assertTrue(all(i.startswith('s-') and i == i.lower() for i in ids))

    def test_displayed_ids_are_all_lowercase(self) -> None:
        from _lib import fmt_id_display
        self.assertEqual(fmt_id_display('p-3kq9v8x2m1'), 'p-3kq9v8x2m1')
        # An ID typed with capitals (an older tool's filename, a human) is
        # shown in the one canonical form.
        self.assertEqual(fmt_id_display('P-3KQ9V8X2M1'), 'p-3kq9v8x2m1')
        self.assertEqual(fmt_id_display('S-7n4hp0wztb'), 's-7n4hp0wztb')
        self.assertEqual(fmt_id_display(''), '')
