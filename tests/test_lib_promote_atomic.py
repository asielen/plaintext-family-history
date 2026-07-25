"""
test_lib_promote_atomic.py - crash safety of the promotion write path.

Two guarantees are exercised here, both of which a truncating write breaks:

  1. `write_text_exact_atomic` leaves the target as the OLD bytes or the NEW
     bytes, never a torn half. When the final `os.replace` (or the write that
     precedes it) fails, the original file stands untouched and no stray temp
     file is left behind.

  2. `promote_person_record` restores the original stub record when a later
     step fails partway. The old code set each `wrote_*` flag only AFTER the
     write returned, so a write that died mid-stream skipped its own rollback -
     leaving a truncated SOLE person record while reporting "nothing was left
     half-promoted." With atomic writes the flag-after-write is now safe, and
     the companion write leaves no untracked partial file.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import _lib
from _lib import (
    PromotionError,
    promote_person_record,
    read_text_exact,
    write_text_exact_atomic,
)

KID = 'P-aaaaaaaaaa'

STUB_RECORD = (
    f'---\nid: {KID}\nname: Ann Kid\nsex: F\nliving: false\n'
    f'tier: stub\n---\n\n# Ann Kid\n\n## Biography\n\nx\n'
)


def _archive() -> tuple[Path, Path, Path]:
    """A minimal archive: one stub record under people/stubs/ plus an existing
    (empty) destination couple folder. Returns (root, record_path, dest)."""
    root = Path(tempfile.mkdtemp())
    (root / 'fha.yaml').write_text(
        'roots:\n  documents: documents\n', encoding='utf-8')
    stubs = root / 'people' / 'stubs'
    stubs.mkdir(parents=True)
    dest = root / 'people' / '040 Pa + Ma'
    dest.mkdir(parents=True)
    record_path = stubs / f'kid__ann_{KID}.md'
    record_path.write_text(STUB_RECORD, encoding='utf-8')
    return root, record_path, dest


class AtomicWriteTests(unittest.TestCase):
    def test_replace_failure_keeps_original_and_no_temp(self) -> None:
        d = Path(tempfile.mkdtemp())
        target = d / 'rec.md'
        target.write_text('ORIGINAL', encoding='utf-8')

        def boom(src, dst):
            raise OSError('simulated replace failure')

        real_replace = os.replace
        os.replace = boom
        try:
            with self.assertRaises(OSError):
                write_text_exact_atomic(target, 'NEW CONTENT')
        finally:
            os.replace = real_replace

        # Original bytes are intact - the write never became visible.
        self.assertEqual(target.read_text(encoding='utf-8'), 'ORIGINAL')
        # No half-written temp file left behind in the directory.
        leftovers = [p.name for p in d.iterdir() if p.name != 'rec.md']
        self.assertEqual(leftovers, [], f'stray temp files: {leftovers}')

    def test_new_file_failure_creates_nothing(self) -> None:
        d = Path(tempfile.mkdtemp())
        target = d / 'fresh.md'

        def boom(src, dst):
            raise OSError('simulated replace failure')

        real_replace = os.replace
        os.replace = boom
        try:
            with self.assertRaises(OSError):
                write_text_exact_atomic(target, 'NEW')
        finally:
            os.replace = real_replace

        self.assertFalse(target.exists(), 'no partial file should appear')
        self.assertEqual(list(d.iterdir()), [], 'no temp file should linger')

    def test_success_writes_exact_bytes_without_newline_translation(self) -> None:
        d = Path(tempfile.mkdtemp())
        target = d / 'crlf.md'
        payload = 'line one\r\nline two\r\n'
        write_text_exact_atomic(target, payload)
        # Read with translation disabled: CRLF must survive byte-for-byte.
        self.assertEqual(read_text_exact(target), payload)


class PromotionRollbackTests(unittest.TestCase):
    def test_failed_companion_write_restores_stub_and_leaves_no_partial(self) -> None:
        root, record_path, dest = _archive()

        # Fail only the research-companion write, and only after the tier flip
        # and the move have already succeeded - the exact window the old
        # flag-after-write bug mishandled.
        real_atomic = _lib.write_text_exact_atomic

        def failing_atomic(path, text):
            if '_research' in Path(path).name:
                raise OSError('simulated disk full during companion write')
            return real_atomic(path, text)

        _lib.write_text_exact_atomic = failing_atomic
        try:
            with self.assertRaises(PromotionError):
                promote_person_record(root, KID, record_path, dest)
        finally:
            _lib.write_text_exact_atomic = real_atomic

        # The record is back in stubs/, restored to its original stub bytes -
        # the tier flip was rolled back, not left curated or truncated.
        self.assertTrue(record_path.exists(), 'record must be moved back')
        self.assertEqual(record_path.read_text(encoding='utf-8'), STUB_RECORD)

        # No record was left in the destination folder.
        self.assertEqual(
            [p.name for p in dest.iterdir()], [],
            'destination folder must be empty after rollback')

        # No partial research companion anywhere under people/.
        companions = list((root / 'people').rglob('*_research_*.md'))
        self.assertEqual(companions, [], f'stray companion: {companions}')

    def test_clean_promotion_still_succeeds(self) -> None:
        # Guardrail: the atomic rewrite did not break the happy path.
        root, record_path, dest = _archive()
        plan = promote_person_record(root, KID, record_path, dest)
        self.assertEqual(plan['status'], 'ok')
        moved = dest / record_path.name
        self.assertTrue(moved.exists())
        self.assertIn('tier: curated', moved.read_text(encoding='utf-8'))
        self.assertFalse(record_path.exists(), 'original stub path is vacated')
        companions = list(dest.glob('*_research_*.md'))
        self.assertEqual(len(companions), 1, 'companion scaffolded once')


if __name__ == '__main__':
    unittest.main()
