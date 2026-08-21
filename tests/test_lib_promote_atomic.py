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
     half-promoted." With atomic writes the flag-after-write is now safe.

Since #76, `promote_person_record`'s default flow writes the profile ONCE
(tier flip + body-section backfill together) then moves it - there is no
auto-scaffolded research companion any more to fail a THIRD write on, so the
rollback scenarios here fail the MOVE instead (and, for the move-back-also-
fails case, add a hand-written companion so a later step still exists to
fail against) - see PromotionRollbackTests for the adapted windows.
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

    @unittest.skipIf(sys.platform == 'win32',
                     'POSIX file-mode bits: Windows reports 0o666 for any '
                     'writable file, so umask/mode preservation cannot be '
                     'asserted here (this is validated on Linux/CI).')
    def test_existing_target_mode_is_preserved(self) -> None:
        # A group-readable record must not silently drop to owner-only when the
        # atomic write installs mkstemp's 0600 temp inode as the record.
        d = Path(tempfile.mkdtemp())
        target = d / 'rec.md'
        target.write_text('ORIGINAL', encoding='utf-8')
        os.chmod(target, 0o644)
        write_text_exact_atomic(target, 'NEW CONTENT')
        mode = os.stat(target).st_mode & 0o777
        self.assertEqual(mode, 0o644,
                         f'expected 0o644 preserved, got {oct(mode)}')
        self.assertEqual(target.read_text(encoding='utf-8'), 'NEW CONTENT')

    @unittest.skipIf(sys.platform == 'win32',
                     'POSIX umask semantics: Windows has no umask/mode bits, '
                     'so this cannot be asserted here (validated on Linux/CI).')
    def test_new_file_uses_umask_default_not_0600(self) -> None:
        # A brand-new record (a scaffolded research companion) must match what a
        # plain open(..., 'w') would leave under the umask, never the 0600
        # mkstemp hands us.
        d = Path(tempfile.mkdtemp())
        target = d / 'fresh.md'
        old_umask = os.umask(0o022)
        try:
            write_text_exact_atomic(target, 'NEW')
        finally:
            os.umask(old_umask)
        mode = os.stat(target).st_mode & 0o777
        self.assertEqual(mode, 0o644,
                         f'expected 0o666 & ~022 = 0o644, got {oct(mode)}')
        self.assertNotEqual(mode, 0o600, 'must not inherit mkstemp 0600')


class PromotionRollbackTests(unittest.TestCase):
    def test_failed_move_restores_flipped_profile_and_leaves_no_partial(self) -> None:
        # #76: promotion no longer auto-scaffolds a research companion, so the
        # profile's own write (tier flip + body-section backfill) and the
        # record MOVE are the only two steps in the default flow. Fail the
        # move, AFTER the profile write has already landed - the exact window
        # the old flag-after-write bug mishandled (a write that died mid-
        # stream skipped its own rollback, leaving a truncated SOLE record).
        root, record_path, dest = _archive()
        real_move = _lib.shutil.move

        def failing_move(src, dst):
            raise OSError('simulated failure moving the record')

        _lib.shutil.move = failing_move
        try:
            with self.assertRaises(PromotionError):
                promote_person_record(root, KID, record_path, dest)
        finally:
            _lib.shutil.move = real_move

        # The record is back at its original path, restored to its original
        # stub bytes byte-for-byte - the tier flip AND the body backfill were
        # both rolled back, not left half-applied or truncated.
        self.assertTrue(record_path.exists(), 'record must stay/return at its own path')
        self.assertEqual(record_path.read_text(encoding='utf-8'), STUB_RECORD)

        # No record was left in the destination folder.
        self.assertEqual(
            [p.name for p in dest.iterdir()], [],
            'destination folder must be empty after rollback')

    def test_failed_moveback_never_creates_duplicate_record(self) -> None:
        # The flip+backfill and the record move succeed; a LATER step (the
        # hand-written companion's move) fails, and during rollback the
        # record's move-BACK itself ALSO fails (a re-locked destination). The
        # tier-flip undo must then target where the profile actually IS - not
        # blindly recreate the old path, which (because the atomic writer
        # creates a missing target) would leave the curated profile in the
        # destination AND a second stub with the same P-id at the vacated old
        # path.
        root, record_path, dest = _archive()
        companion = record_path.parent / f'kid__ann_research_{KID}.md'
        companion.write_text('KEEP ME HERE', encoding='utf-8')
        real_move = _lib.shutil.move
        calls = {'n': 0}

        def failing_move(src, dst):
            calls['n'] += 1
            if calls['n'] == 2:
                # The companion move (the record's own move is call 1, and it
                # must succeed for this scenario) - fails to trigger rollback.
                raise OSError('simulated failure moving companion')
            if Path(src).parent == dest:
                # The rollback's own move-BACK of the record.
                raise OSError('simulated locked destination during move-back')
            return real_move(src, dst)

        _lib.shutil.move = failing_move
        try:
            with self.assertRaises(PromotionError):
                promote_person_record(root, KID, record_path, dest)
        finally:
            _lib.shutil.move = real_move

        # Exactly ONE *profile* for this P-id survives - never a duplicate.
        # (The stranded companion, checked separately below, also carries the
        # P-id in its name, so the research-named file is excluded here.)
        records = [p for p in (root / 'people').rglob(f'*{KID}*.md')
                  if '_research_' not in p.name]
        self.assertEqual(len(records), 1, f'expected one record, got {records}')
        # It is the profile still stranded in the destination (move-back failed),
        # with the tier flip undone in place (old stub bytes), and the old path
        # was NOT recreated.
        self.assertEqual(records[0].parent, dest)
        self.assertIn('tier: stub', records[0].read_text(encoding='utf-8'))
        self.assertFalse(record_path.exists(), 'no duplicate stub at the old path')
        # The companion never left - its own move failed before it could.
        self.assertTrue(companion.exists())
        self.assertEqual(companion.read_text(encoding='utf-8'), 'KEEP ME HERE')

    def test_clean_promotion_still_succeeds(self) -> None:
        # Guardrail: the atomic rewrite did not break the happy path.
        root, record_path, dest = _archive()
        plan = promote_person_record(root, KID, record_path, dest)
        self.assertEqual(plan['status'], 'ok')
        moved = dest / record_path.name
        self.assertTrue(moved.exists())
        self.assertIn('tier: curated', moved.read_text(encoding='utf-8'))
        self.assertFalse(record_path.exists(), 'original stub path is vacated')
        # #76: no research companion auto-scaffolded; the body's own missing
        # sections are backfilled in the same write instead.
        companions = list(dest.glob('*_research_*.md'))
        self.assertEqual(companions, [], 'no companion auto-created')
        self.assertIn('## Sources', moved.read_text(encoding='utf-8'))


class ExistingCompanionMoveTests(unittest.TestCase):
    """A hand-written companion beside the stub must travel WITH the record,
    not be left behind while a blank one is scaffolded at the destination."""

    def _companion_name(self) -> str:
        return f'kid__ann_research_{KID}.md'

    def test_existing_companion_is_moved_not_duplicated(self) -> None:
        root, record_path, dest = _archive()
        companion = record_path.parent / self._companion_name()
        companion.write_text('MY HAND-WRITTEN NOTES', encoding='utf-8')

        plan = promote_person_record(root, KID, record_path, dest)
        self.assertTrue(plan['research_move'], 'plan must mark a move')
        self.assertFalse(plan['research_create'], 'must not scaffold a new one')

        # The populated companion now sits at the destination with its notes,
        # and nothing is left behind in stubs/.
        moved_companion = dest / self._companion_name()
        self.assertTrue(moved_companion.exists())
        self.assertEqual(moved_companion.read_text(encoding='utf-8'),
                         'MY HAND-WRITTEN NOTES')
        self.assertFalse(companion.exists(), 'stub companion must be vacated')
        # Exactly one companion exists across the archive - not duplicated.
        all_companions = list((root / 'people').rglob('*_research_*.md'))
        self.assertEqual(len(all_companions), 1, f'duplicated: {all_companions}')

    def test_moved_companion_rolls_back_to_source_on_later_failure(self) -> None:
        # Force the record MOVE to fail AFTER the companion move would run? No -
        # the companion move is the last write, so fail it and confirm the
        # record move and tier flip roll back and the companion never left.
        # Instead we fail the record move here to exercise the move-companion
        # inverse: make shutil.move fail on the RECORD move (second call).
        root, record_path, dest = _archive()
        companion = record_path.parent / self._companion_name()
        companion.write_text('KEEP ME HERE', encoding='utf-8')

        real_move = _lib.shutil.move
        calls = {'n': 0}

        def failing_move(src, dst):
            calls['n'] += 1
            # First move is the record; let it through. The companion move is
            # the second - fail it to trigger rollback of the record move.
            if calls['n'] == 2:
                raise OSError('simulated failure moving companion')
            return real_move(src, dst)

        _lib.shutil.move = failing_move
        try:
            with self.assertRaises(PromotionError):
                promote_person_record(root, KID, record_path, dest)
        finally:
            _lib.shutil.move = real_move

        # Record restored to stubs/ and companion still beside it with its notes.
        self.assertTrue(record_path.exists(), 'record must be moved back')
        self.assertTrue(companion.exists(), 'companion must stay in stubs/')
        self.assertEqual(companion.read_text(encoding='utf-8'), 'KEEP ME HERE')
        self.assertEqual([p.name for p in dest.iterdir()], [],
                         'destination folder must be empty after rollback')


class TwoCompanionConflictTests(unittest.TestCase):
    """A populated companion beside the stub AND another already at the
    destination is a split the engine must NOT resolve silently: moving the
    record would keep the destination file and strand the stub one under
    people/stubs/. Promotion must REFUSE and hand the reconcile to the human.

    The ordinary single-companion cases (MOVE / SKIP / CREATE) must be
    untouched, so each is re-asserted here alongside the refusal."""

    def _companion_name(self) -> str:
        return f'kid__ann_research_{KID}.md'

    def test_two_companions_refuse_and_write_nothing(self) -> None:
        root, record_path, dest = _archive()
        cname = self._companion_name()
        source_companion = record_path.parent / cname
        dest_companion = dest / cname
        source_companion.write_text('STUB-SIDE NOTES', encoding='utf-8')
        dest_companion.write_text('DEST-SIDE NOTES', encoding='utf-8')

        with self.assertRaises(PromotionError) as ctx:
            promote_person_record(root, KID, record_path, dest)
        msg = str(ctx.exception)
        # The message names BOTH companion paths so the human can reconcile.
        self.assertIn(str(source_companion), msg)
        self.assertIn(str(dest_companion), msg)

        # Nothing moved or written: record still in stubs/ as a stub, both
        # companions intact with their original notes.
        self.assertTrue(record_path.exists(), 'record must stay in stubs/')
        self.assertEqual(record_path.read_text(encoding='utf-8'), STUB_RECORD)
        self.assertFalse((dest / record_path.name).exists(),
                         'no record written to destination')
        self.assertEqual(source_companion.read_text(encoding='utf-8'),
                         'STUB-SIDE NOTES')
        self.assertEqual(dest_companion.read_text(encoding='utf-8'),
                         'DEST-SIDE NOTES')

    def test_two_companions_refuse_in_dry_run_too(self) -> None:
        # The check lives in the PLAN phase, so a preview surfaces it and still
        # writes nothing - the human learns of the conflict before committing.
        root, record_path, dest = _archive()
        cname = self._companion_name()
        source_companion = record_path.parent / cname
        dest_companion = dest / cname
        source_companion.write_text('STUB-SIDE NOTES', encoding='utf-8')
        dest_companion.write_text('DEST-SIDE NOTES', encoding='utf-8')

        with self.assertRaises(PromotionError):
            promote_person_record(root, KID, record_path, dest, dry_run=True)
        self.assertEqual(source_companion.read_text(encoding='utf-8'),
                         'STUB-SIDE NOTES')
        self.assertEqual(dest_companion.read_text(encoding='utf-8'),
                         'DEST-SIDE NOTES')

    def test_only_destination_companion_skips_and_still_promotes(self) -> None:
        # SKIP: a companion at the destination but NONE beside the stub. No
        # source file is stranded, so this stays the ordinary skip - the record
        # still promotes and the destination companion is left exactly as is.
        root, record_path, dest = _archive()
        cname = self._companion_name()
        dest_companion = dest / cname
        dest_companion.write_text('DEST NOTES', encoding='utf-8')

        plan = promote_person_record(root, KID, record_path, dest)
        self.assertEqual(plan['status'], 'ok')
        self.assertFalse(plan['research_move'], 'no companion to move')
        self.assertFalse(plan['research_create'], 'destination one is kept')
        self.assertTrue((dest / record_path.name).exists(), 'record promoted')
        self.assertFalse(record_path.exists(), 'stub path vacated')
        self.assertEqual(dest_companion.read_text(encoding='utf-8'),
                         'DEST NOTES', 'destination companion untouched')

    def test_only_source_companion_moves(self) -> None:
        # MOVE: a companion beside the stub, none at the destination -> it
        # travels with the record. (Mirror of ExistingCompanionMoveTests, kept
        # here so the three-fate matrix reads in one place.)
        root, record_path, dest = _archive()
        cname = self._companion_name()
        source_companion = record_path.parent / cname
        source_companion.write_text('TRAVELING NOTES', encoding='utf-8')

        plan = promote_person_record(root, KID, record_path, dest)
        self.assertTrue(plan['research_move'])
        self.assertFalse(plan['research_create'])
        self.assertFalse(source_companion.exists(), 'source vacated')
        self.assertEqual((dest / cname).read_text(encoding='utf-8'),
                         'TRAVELING NOTES')

    def test_no_companion_creates(self) -> None:
        # #76: none anywhere -> none is scaffolded. The separate research
        # companion is an opt-in escape valve now, never a promotion default -
        # the profile's own body gets the missing sections instead.
        root, record_path, dest = _archive()
        plan = promote_person_record(root, KID, record_path, dest)
        self.assertFalse(plan['research_move'])
        self.assertFalse(plan['research_create'])
        self.assertEqual(len(list(dest.glob('*_research_*.md'))), 0)
        moved = dest / record_path.name
        self.assertIn('## Sources', moved.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
