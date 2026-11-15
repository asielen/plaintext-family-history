"""
test_reconcile.py - fha reconcile, the documents-side path healer (TOOLING §9).

The engine reads record files and the documents tree directly (no index
needed - the .md files are the truth it heals), so the fixture here is a tiny
on-disk archive: one source record with a files: inventory, plus document
files placed and then moved to exercise each plan bucket (healed, ambiguous,
missing, unlisted). The photos side is photoindex's own machinery, gated on a
photos.sqlite that these fixtures deliberately do not create.
"""

import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import argparse
import contextlib
import io

import photoindex
import reconcile
from _lib import (
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    load_fha_yaml,
    read_record,
)

SID = 'S-1a2b3c4d5e'
DOC = f'letter_{SID.lower()}.pdf'


def _scandir_denying(unreadable: Path):
    """An os.scandir stand-in that refuses to list `unreadable`.

    The fault goes in at `os.scandir` because `os.walk` resolves it at call
    time on every supported Python - that is what makes the `onerror` seam
    observable here. chmod cannot produce this: CI runs as root, which ignores
    mode bits, and Windows has no equivalent.

    What this deliberately does NOT rely on: that pathlib's `rglob` reaches the
    disk the same way. It does on 3.11/3.12/3.14, but NOT on the 3.10 floor
    (pathlib routes through an accessor object that bound `os.scandir` at
    import time, so a later patch is invisible) and not on 3.13. So the
    injection does not reproduce the pre-fix `rglob` behaviour on every version
    we support - a regression back to `rglob` is still caught everywhere, but
    on the floor it is caught by the warning going missing rather than by the
    folder reading as empty.
    """
    real_scandir = os.scandir
    target = unreadable.resolve()

    def scandir(path='.'):
        try:
            denied = Path(path).resolve() == target
        except (TypeError, ValueError, OSError):
            denied = False
        if denied:
            err = PermissionError(13, 'Permission denied')
            err.filename = str(path)
            raise err
        return real_scandir(path)

    return scandir


class ReconcileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8')
        (self.root / 'documents').mkdir()
        (self.root / 'sources' / 'letter').mkdir(parents=True)
        self.record = self.root / 'sources' / 'letter' / f'letter_{SID.lower()}.md'
        self._write_record(f'documents/{DOC}')
        self.config = load_fha_yaml(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_record(self, alias: str, extra_entry: str = '') -> None:
        self.record.write_text(
            f'---\nid: {SID}\ntitle: A letter\nsource_type: letter\n'
            f'files:\n  - file: {alias}\n    role: primary\n{extra_entry}---\n\n## Notes\nx.\n',
            encoding='utf-8')

    def _run(self, **kw):
        return reconcile.run_reconcile(self.root, self.config, **kw)

    def test_a_documents_folder_that_will_not_open_heals_nothing(self) -> None:
        """Under-seeing here re-ties a record to the WRONG original.

        The heal picks a file by basename and only when exactly one unclaimed
        copy exists; a folder that will not list hides the other copy, so a
        genuinely ambiguous name becomes a confident rewrite of the record -
        reported as a successful heal - and documents that are merely out of
        sight are reported gone. Same posture as an unreachable documents
        root: name it, heal nothing, still run the photo pass.
        """
        self._write_record(f'documents/{DOC}')          # stale: file has moved
        seen = self.root / 'documents' / 'letters' / DOC
        seen.parent.mkdir(parents=True)
        seen.write_text('the wrong one', encoding='utf-8')
        hidden = self.root / 'documents' / 'archive-box' / 'scans' / DOC
        hidden.parent.mkdir(parents=True)
        hidden.write_text('the right one', encoding='utf-8')
        before = self.record.read_text(encoding='utf-8')

        shut = self.root / 'documents' / 'archive-box'
        with unittest.mock.patch('os.scandir', new=_scandir_denying(shut)):
            result = self._run()

        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result.data['healed'], 0)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('could not be opened', text)
        self.assertIn('archive-box', text)
        self.assertIn('fha reconcile', text)
        self.assertNotIn('Re-tied', text)

    def test_clean_archive_reports_nothing_to_heal(self) -> None:
        (self.root / 'documents' / DOC).write_text('x', encoding='utf-8')
        result = self._run()
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertIn('nothing to heal', ' '.join(m.text for m in result.messages))

    def test_moved_document_is_healed_and_dry_run_writes_nothing(self) -> None:
        moved = self.root / 'documents' / 'letters' / '1920s' / DOC
        moved.parent.mkdir(parents=True)
        moved.write_text('x', encoding='utf-8')
        before = self.record.read_text(encoding='utf-8')

        preview = self._run(dry_run=True)
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertTrue(any('[dry-run] Would re-tie' in m.text for m in preview.messages))

        applied = self._run()
        # The P1 regression this pins: a clean heal is EXIT 0 with zero
        # warnings - the just-healed file must never re-report as an
        # "unlisted" S-id file (the reverse pass judges the POST-heal state).
        self.assertEqual(applied.exit_code, EXIT_CLEAN)
        self.assertEqual(applied['healed'], 1)
        self.assertEqual(applied['unlisted'], 0)
        self.assertFalse([m.text for m in applied.messages if m.level == 'warning'])
        self.assertIn(str(self.record), applied.changed)
        meta = read_record(self.record)['meta']
        self.assertEqual(meta['files'][0]['file'], f'documents/letters/1920s/{DOC}')
        # role and the rest of the entry survive the surgical rewrite
        self.assertEqual(meta['files'][0]['role'], 'primary')
        # a second run has nothing left to do
        self.assertEqual(self._run().exit_code, EXIT_CLEAN)

    def test_prefix_alias_never_grabs_a_longer_siblings_line(self) -> None:
        # The substring-corruption regression: the record also lists a
        # hand-authored sidecar whose name EXTENDS the primary's
        # ('letter_….pdf.txt'), still on disk and listed FIRST. Healing the
        # moved primary must rewrite the primary's own line, never the
        # sidecar's (a substring match would corrupt the valid entry and
        # leave the stale one, reporting success).
        sidecar_name = f'{DOC}.txt'
        self._write_record(
            f'documents/{sidecar_name}',
            extra_entry=f'  - file: documents/{DOC}\n    role: primary\n')
        (self.root / 'documents' / sidecar_name).write_text('t', encoding='utf-8')
        moved = self.root / 'documents' / 'letters' / DOC
        moved.parent.mkdir(parents=True)
        moved.write_text('x', encoding='utf-8')

        applied = self._run()
        self.assertEqual(applied.exit_code, EXIT_CLEAN)
        meta = read_record(self.record)['meta']
        aliases = [f['file'] for f in meta['files']]
        self.assertIn(f'documents/{sidecar_name}', aliases)          # untouched
        self.assertIn(f'documents/letters/{DOC}', aliases)           # healed
        self.assertNotIn(f'documents/letters/{sidecar_name}', aliases)

    def test_moved_and_renamed_file_heals_by_sid_fallback(self) -> None:
        # TOOLING §9's embedded-ID contract: renaming a filed original is
        # forbidden, but when it happens anyway the S-id in the new name is
        # still the identity - a unique unlisted carrier heals the entry.
        renamed = self.root / 'documents' / 'letters' / f'renamed_{SID.lower()}.pdf'
        renamed.parent.mkdir(parents=True)
        renamed.write_text('x', encoding='utf-8')
        applied = self._run()
        self.assertEqual(applied.exit_code, EXIT_CLEAN)
        self.assertEqual(applied['healed'], 1)
        meta = read_record(self.record)['meta']
        self.assertEqual(meta['files'][0]['file'],
                         f'documents/letters/renamed_{SID.lower()}.pdf')

    def test_backslash_alias_entry_is_healed_not_missing_or_unlisted(self) -> None:
        # A record may carry a valid Windows-style alias with backslash
        # separators. On POSIX, Path('documents\\letters\\x.pdf').name keeps the
        # WHOLE backslash string, so the basename lookup misses the moved file.
        # The separators must be normalized to forward slashes before matching,
        # and the heal must rewrite to the canonical forward-slash alias.
        self._write_record(f'documents\\letters\\{DOC}')
        moved = self.root / 'documents' / 'moved' / DOC
        moved.parent.mkdir(parents=True)
        moved.write_text('x', encoding='utf-8')

        applied = self._run()
        self.assertEqual(applied.exit_code, EXIT_CLEAN)
        self.assertEqual(applied['healed'], 1)
        self.assertEqual(applied['missing'], 0)
        self.assertEqual(applied['unlisted'], 0)
        self.assertFalse([m.text for m in applied.messages if m.level == 'warning'])
        meta = read_record(self.record)['meta']
        self.assertEqual(meta['files'][0]['file'], f'documents/moved/{DOC}')
        self.assertEqual(meta['files'][0]['role'], 'primary')

    def test_backslash_front_and_back_heal_by_basename_not_ambiguous(self) -> None:
        # Two files of ONE source share its S-id (a front and a back). With
        # backslash aliases, an un-normalized basename lookup misses BOTH, so
        # each entry falls through to the S-id fallback - which sees two
        # carriers for the shared S-id and falsely reports each as ambiguous,
        # healing nothing. Normalizing the separators first lets each entry
        # match its own unique basename and heal cleanly.
        back = f'letter-back_{SID.lower()}.pdf'
        self.record.write_text(
            f'---\nid: {SID}\ntitle: A letter\nsource_type: letter\n'
            f'files:\n'
            f'  - file: documents\\old\\{DOC}\n    role: primary\n'
            f'  - file: documents\\old\\{back}\n    role: back\n'
            f'---\n\n## Notes\nx.\n', encoding='utf-8')
        dest = self.root / 'documents' / '1920s'
        dest.mkdir()
        (dest / DOC).write_text('x', encoding='utf-8')
        (dest / back).write_text('x', encoding='utf-8')

        applied = self._run()
        self.assertEqual(applied.exit_code, EXIT_CLEAN)
        self.assertEqual(applied['healed'], 2)
        self.assertEqual(applied['ambiguous'], 0)
        self.assertEqual(applied['unlisted'], 0)
        aliases = {f['file'] for f in read_record(self.record)['meta']['files']}
        self.assertEqual(aliases,
                         {f'documents/1920s/{DOC}', f'documents/1920s/{back}'})

    def test_corrupt_photo_catalog_is_an_error_not_a_clean_report(self) -> None:
        (self.root / 'documents' / DOC).write_text('x', encoding='utf-8')
        cache = self.root / '.cache'
        cache.mkdir()
        (cache / 'photos.sqlite').write_text('this is not a database', encoding='utf-8')
        result = self._run()
        self.assertEqual(result.exit_code, 3)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('fha photoindex', text)
        self.assertNotIn('nothing to heal.', ' '.join(
            m.text for m in result.messages if m.text.startswith('photos:')))

    def test_duplicate_names_are_ambiguous_and_untouched(self) -> None:
        for sub in ('a', 'b'):
            p = self.root / 'documents' / sub / DOC
            p.parent.mkdir(parents=True)
            p.write_text('x', encoding='utf-8')
        before = self.record.read_text(encoding='utf-8')
        result = self._run()
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result['ambiguous'], 1)
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertTrue(any('more than one place' in m.text for m in result.messages))

    def test_vanished_document_reported_missing(self) -> None:
        result = self._run()
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result['missing'], 1)
        self.assertTrue(any('is gone' in m.text for m in result.messages))

    def test_unlisted_sid_file_reported_with_attach_path(self) -> None:
        (self.root / 'documents' / DOC).write_text('x', encoding='utf-8')
        stray = self.root / 'documents' / f'letter-back_{SID.lower()}.pdf'
        stray.write_text('x', encoding='utf-8')
        result = self._run()
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result['unlisted'], 1)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('--more', text)

    def test_malformed_records_document_is_not_reported_unlisted(self) -> None:
        # P2 codex finding (round 8): a malformed source record is skipped, so
        # its aliases never enter listed_aliases - but its on-disk document
        # (carrying the same S-id in its name) must NOT then be advertised as an
        # unlisted "attach me" orphan, because the unreadable record already
        # lists it. The malformed record's filename S-id is reserved and the
        # reverse pass suppresses unlisted conclusions for it until the YAML is fixed.
        (self.root / 'documents' / DOC).write_text('x', encoding='utf-8')   # self.record clean
        SID_B = 'S-9z8y7x6w5v'
        # A malformed record (unterminated quote in the frontmatter) whose
        # filename still carries SID_B, plus its on-disk document.
        (self.root / 'sources' / 'letter' / f'other_{SID_B.lower()}.md').write_text(
            f'---\nid: {SID_B}\ntitle: "unterminated\nsource_type: letter\n'
            f'files:\n  - file: documents/other_{SID_B.lower()}.pdf\n    role: primary\n'
            '---\n\n## Notes\ny.\n', encoding='utf-8')
        (self.root / 'documents' / f'other_{SID_B.lower()}.pdf').write_text(
            'y', encoding='utf-8')

        result = self._run()

        # The malformed record is warned about, but its document is NOT flagged
        # unlisted (nor is an attach path advised for it).
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result['unlisted'], 0)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('malformed YAML', text)
        self.assertNotIn('--more', text)

    def test_missing_fixture_entry_is_left_alone(self) -> None:
        (self.root / 'documents' / DOC).write_text('x', encoding='utf-8')
        self._write_record(
            f'documents/{DOC}',
            extra_entry='  - file: documents/ghost.tif\n    role: page-2\n'
                        '    status: missing-fixture\n')
        result = self._run()
        self.assertEqual(result['healed'], 0)
        self.assertEqual(result['missing'], 0)

    def test_move_into_folder_with_hash_is_quoted_and_round_trips(self) -> None:
        # P1: a valid destination folder containing ' #' (e.g. 'Box #3') must
        # be re-emitted as a QUOTED YAML scalar. The old raw-string splice
        # wrote it bare, and the next parse read only 'documents/Box' - YAML
        # treats ' #3/...' as a comment - silently detaching the source from
        # its document while reporting exit 0 and counting the entry healed.
        dest = self.root / 'documents' / 'Box #3'
        dest.mkdir()
        (dest / DOC).write_text('x', encoding='utf-8')

        applied = self._run()
        self.assertEqual(applied.exit_code, EXIT_CLEAN)
        self.assertEqual(applied['healed'], 1)
        self.assertEqual(applied['unlisted'], 0)
        self.assertFalse([m.text for m in applied.messages if m.level == 'warning'])
        # The record must read back the WHOLE path, not a '#'-truncated stub.
        meta = read_record(self.record)['meta']
        self.assertEqual(meta['files'][0]['file'], f'documents/Box #3/{DOC}')
        self.assertEqual(meta['files'][0]['role'], 'primary')
        # And a second run finds nothing to heal - the source stayed attached.
        self.assertEqual(self._run().exit_code, EXIT_CLEAN)

    @unittest.skipIf(sys.platform == 'win32',
                     "a ':' in a directory name is illegal on NTFS (reserved "
                     'for alternate data streams), so the fixture cannot be '
                     'created here (the reconcile logic is exercised on Linux/CI).')
    def test_move_into_folder_with_colon_round_trips(self) -> None:
        # Same P1 class via a different YAML-significant character: a ': ' in a
        # path would make YAML read it as a nested mapping unless quoted.
        dest = self.root / 'documents' / 'Notes: 1920'
        dest.mkdir()
        (dest / DOC).write_text('x', encoding='utf-8')
        applied = self._run()
        self.assertEqual(applied.exit_code, EXIT_CLEAN)
        self.assertEqual(applied['healed'], 1)
        meta = read_record(self.record)['meta']
        self.assertEqual(meta['files'][0]['file'], f'documents/Notes: 1920/{DOC}')

    def test_comment_on_file_line_survives_a_heal(self) -> None:
        # The rewrite promises to preserve a trailing comment on the file:
        # line. The comment splitter is now quote-aware, so this must hold.
        self.record.write_text(
            f'---\nid: {SID}\ntitle: A letter\nsource_type: letter\n'
            f'files:\n  - file: documents/{DOC}  # scanned original\n'
            f'    role: primary\n---\n\n## Notes\nx.\n', encoding='utf-8')
        moved = self.root / 'documents' / 'letters' / DOC
        moved.parent.mkdir(parents=True)
        moved.write_text('x', encoding='utf-8')
        applied = self._run()
        self.assertEqual(applied.exit_code, EXIT_CLEAN)
        self.assertEqual(applied['healed'], 1)
        raw = self.record.read_text(encoding='utf-8')
        self.assertIn('# scanned original', raw)
        meta = read_record(self.record)['meta']
        self.assertEqual(meta['files'][0]['file'], f'documents/letters/{DOC}')

    def test_interrupted_heal_write_leaves_record_intact(self) -> None:
        # P1 (atomic heal write): a write that dies mid-stream - disk full, the
        # process killed - must never leave the source record half-written. The
        # heal goes through write_text_exact_atomic, which writes a sibling temp
        # and os.replace()s it, so an OSError raised from the write means the
        # target was never touched. We simulate that failure and assert the
        # record's ORIGINAL bytes survive whole (not truncated), the record is
        # reported by name, and the run ends in a warning, not a clean exit.
        moved = self.root / 'documents' / 'letters' / DOC
        moved.parent.mkdir(parents=True)
        moved.write_text('x', encoding='utf-8')
        before = self.record.read_text(encoding='utf-8')

        original = reconcile.write_text_exact_atomic

        def boom(path, text):
            # Mimic a truncation-point failure: an atomic writer must NOT have
            # touched the target, so we raise before writing anything.
            raise OSError(28, 'No space left on device')

        reconcile.write_text_exact_atomic = boom
        try:
            result = self._run()
        finally:
            reconcile.write_text_exact_atomic = original

        # The record is byte-for-byte what it was - never truncated or partial.
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        # Nothing counted as healed, and the run warns rather than reporting clean.
        self.assertEqual(result['healed'], 0)
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        warnings = [m.text for m in result.messages if m.level == 'warning']
        self.assertTrue(any(self.record.name in w and 'could not be written' in w
                            for w in warnings),
                        f'expected a named write-failure warning, got: {warnings}')

    def test_malformed_record_is_skipped_with_a_named_warning(self) -> None:
        # P2 (parse_errors): read_record reports malformed YAML through its
        # parse_errors field rather than raising. The old code read that as
        # empty meta and yielded it silently - dropping the record's files:
        # inventory. The record must instead be skipped WITH a warning that
        # names it and points at the fix.
        (self.root / 'documents' / DOC).write_text('x', encoding='utf-8')
        other_sid = 'S-9z8y7x6w5v'
        other_doc = f'deed_{other_sid.lower()}.pdf'
        (self.root / 'documents' / other_doc).write_text('x', encoding='utf-8')
        bad = self.root / 'sources' / 'deed' / f'deed_{other_sid.lower()}.md'
        bad.parent.mkdir(parents=True)
        bad.write_text(
            f'---\nid: {other_sid}\ntitle: "unterminated\n'
            f'files:\n  - file: documents/{other_doc}\n---\n\n## Notes\ny.\n',
            encoding='utf-8')
        result = self._run()
        warnings = [m.text for m in result.messages if m.level == 'warning']
        self.assertTrue(any('malformed YAML' in w and bad.name in w
                            for w in warnings),
                        f'expected a named skip warning, got: {warnings}')

    def test_malformed_config_stops_before_planning(self) -> None:
        # P2 (strict config): a malformed fha.yaml must halt reconcile with a
        # plain cause + next command, never fall back to {} and scan the empty
        # internal skeleton (which would report every real file missing).
        (self.root / 'fha.yaml').write_text('roots: : : bad\n', encoding='utf-8')
        args = argparse.Namespace(root=str(self.root), dry_run=False,
                                  with_exif=False)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = reconcile._cmd_reconcile(args)
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn('fha.yaml', err.getvalue())

    def test_working_copy_is_a_clean_no_op(self) -> None:
        (self.root / 'WORKING_COPY').write_text('', encoding='utf-8')
        result = self._run()
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result['status'], 'working-copy')
        self.assertTrue(any('main machine' in m.text for m in result.messages))

    def test_basename_already_listed_by_another_record_is_not_healed(self) -> None:
        # A unique on-disk basename that ANOTHER record already lists must not be
        # grabbed as a heal - that would rewrite this record onto the other's
        # original and point two records at the same asset. The conflict is
        # reported; the stale entry is left for the human to fix.
        SID_B = 'S-9z8y7x6w5v'
        moved = self.root / 'documents' / 'moved' / DOC
        moved.parent.mkdir(parents=True)
        moved.write_text('x', encoding='utf-8')
        # Record B correctly lists the file at its real location.
        record_b = self.root / 'sources' / 'letter' / f'other_{SID_B.lower()}.md'
        record_b.write_text(
            f'---\nid: {SID_B}\ntitle: Other\nsource_type: letter\n'
            f'files:\n  - file: documents/moved/{DOC}\n    role: primary\n'
            '---\n\n## Notes\ny.\n', encoding='utf-8')
        # Record A (self.record) still lists the stale documents/{DOC}, which is
        # not on disk - its basename matches the file record B already lists.
        before_a = self.record.read_text(encoding='utf-8')
        before_b = record_b.read_text(encoding='utf-8')

        result = self._run()

        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result['healed'], 0)
        self.assertTrue(
            any('already listed by another record' in m.text for m in result.messages),
            [m.text for m in result.messages])
        # Neither record was rewritten.
        self.assertEqual(self.record.read_text(encoding='utf-8'), before_a)
        self.assertEqual(record_b.read_text(encoding='utf-8'), before_b)

    def test_unreachable_documents_root_warns_without_mass_flagging(self) -> None:
        (self.root / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n  documents: Q:/no/such/drive\n', encoding='utf-8')
        config = load_fha_yaml(self.root)
        result = reconcile.run_reconcile(self.root, config)
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result['missing'], 0)
        self.assertTrue(any('not reachable' in m.text for m in result.messages))

    def test_unreachable_documents_still_runs_the_photo_pass(self) -> None:
        # An offline documents drive must NOT short-circuit the independent photo
        # pass (TOOLING §9's one-command contract). With no photo catalog present,
        # the photo pass reaches its own "No photo catalog" notice - proof it ran
        # rather than being skipped by the documents early-return.
        (self.root / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n  documents: Q:/no/such/drive\n', encoding='utf-8')
        config = load_fha_yaml(self.root)
        result = reconcile.run_reconcile(self.root, config)
        msgs = [m.text for m in result.messages]
        self.assertTrue(any('not reachable' in t for t in msgs), msgs)
        self.assertTrue(any('No photo catalog' in t for t in msgs), msgs)


PHOTO_SID = 'S-6gqarv176s'
PHOTO_FIXTURES = ROOT / 'tests' / 'fixtures' / 'photo-fixture' / 'photos'


class PhotoRecordReconcileTests(unittest.TestCase):
    """#289: a photo filed as a source and then moved within the photos root.

    The record's `files:` entry is the pointer that has to follow the photo;
    the photo's identity is the `SOURCE:` keyword `fha process` embedded in it
    (photos are never renamed, SPEC §12.1/§13). These tests build a small
    archive from the shared photo-fixture JPEGs and stand in for exiftool at
    photoindex's `_run_exiftool` seam (the same seam test_photoindex uses), so
    each file's keywords are whatever the test says they are. The keyword map
    is keyed by the file's current location and follows a move, the way an
    embedded keyword travels with the file.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8')
        (self.root / 'documents').mkdir()
        (self.root / 'photos').mkdir()
        (self.root / 'sources' / 'photos').mkdir(parents=True)
        self.config = load_fha_yaml(self.root)
        self.keywords: dict[Path, list[str]] = {}
        patcher = unittest.mock.patch.object(
            photoindex, '_run_exiftool', side_effect=self._fake_exiftool)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.record = self._write_photo_record(
            PHOTO_SID, ['photos/portrait_1880.jpg'])
        self._place('portrait_1880.jpg', 'photos/portrait_1880.jpg', PHOTO_SID)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- helpers ------------------------------------------------------------

    def _fake_exiftool(self, paths, *_args, **_kwargs):
        return [{'SourceFile': str(p), 'Keywords': self.keywords.get(Path(p).resolve(), [])}
                for p in paths]

    def _write_record_text(self, filename: str, text: str) -> Path:
        rec = self.root / 'sources' / 'photos' / filename
        rec.write_text(text, encoding='utf-8')
        return rec

    def _warnings(self, result) -> str:
        return ' '.join(m.text for m in result.messages if m.level == 'warning')

    def _place(self, fixture_name: str, alias: str, sid: 'str | None') -> Path:
        dest = self.root / alias
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((PHOTO_FIXTURES / fixture_name).read_bytes())
        self.keywords[dest.resolve()] = [f'SOURCE: {sid}'] if sid else []
        return dest

    def _move(self, old_alias: str, new_alias: str) -> None:
        old, new = self.root / old_alias, self.root / new_alias
        new.parent.mkdir(parents=True, exist_ok=True)
        kws = self.keywords.pop(old.resolve(), [])
        old.rename(new)
        self.keywords[new.resolve()] = kws

    def _write_photo_record(self, sid: str, aliases: list, status: str = '') -> Path:
        rec = self.root / 'sources' / 'photos' / f'portrait_{sid}.md'
        lines = [f'---\nid: {sid}\ntitle: A portrait\nsource_type: photo\nfiles:\n']
        for i, alias in enumerate(aliases):
            lines.append(f'  - file: {alias}\n    role: {"front" if i == 0 else "back"}\n')
            if status:
                lines.append(f'    status: {status}\n')
        lines.append('---\n\n## Notes\nx.\n')
        rec.write_text(''.join(lines), encoding='utf-8')
        return rec

    def _scan(self) -> None:
        photoindex.run_scan(self.root, self.config)

    def _run(self, **kw):
        return reconcile.run_reconcile(self.root, self.config, **kw)

    def _files(self, rec=None) -> list:
        return [f['file'] for f in read_record(rec or self.record)['meta']['files']]

    def _e011_for(self, rec_path: Path) -> list:
        import lint
        result = lint.run_lint(self.root, self.config)
        return [m.text for m in result.messages
                if m.code == 'E011' and Path(str(m.path)).name == rec_path.name]

    # -- the issue's three regression tests ----------------------------------

    def test_moved_photo_is_re_tied_without_a_rescan(self) -> None:
        """Case A: catalog built, photo moved, reconcile (no rescan between)."""
        self._scan()
        self._move('photos/portrait_1880.jpg', 'photos/portraits/portrait_1880.jpg')
        before = self.record.read_text(encoding='utf-8')

        preview = self._run(dry_run=True, with_exif=True)
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertIn(
            '[dry-run] Would re-tie photos/portrait_1880.jpg -> '
            f'photos/portraits/portrait_1880.jpg ({self.record.name})',
            [m.text for m in preview.messages])

        applied = self._run(with_exif=True)
        self.assertEqual(self._files(), ['photos/portraits/portrait_1880.jpg'])
        self.assertEqual(applied['photos_healed'], 1)
        self.assertIn(str(self.record), applied.changed)
        self.assertEqual(applied.exit_code, EXIT_CLEAN,
                         [m.text for m in applied.messages])
        self.assertEqual(self._e011_for(self.record), [])

    def test_moved_photo_is_re_tied_after_a_rescan(self) -> None:
        """Case B: `fha photoindex` ran after the move and dropped the old row.

        The catalog then knows the photo only at its new path, with its
        source_id; that is enough to heal, without --with-exif.
        """
        self._scan()
        self._move('photos/portrait_1880.jpg', 'photos/portraits/portrait_1880.jpg')
        self._scan()

        preview = self._run(dry_run=True)
        self.assertTrue(any('[dry-run] Would re-tie photos/portrait_1880.jpg' in m.text
                            for m in preview.messages), [m.text for m in preview.messages])
        self.assertEqual(self._files(), ['photos/portrait_1880.jpg'])

        applied = self._run()
        self.assertEqual(self._files(), ['photos/portraits/portrait_1880.jpg'])
        self.assertEqual(applied.exit_code, EXIT_CLEAN,
                         [m.text for m in applied.messages])
        self.assertEqual(self._e011_for(self.record), [])

    def test_two_files_carrying_the_source_are_ambiguous_and_untouched(self) -> None:
        self._move('photos/portrait_1880.jpg', 'photos/a/portrait_1880.jpg')
        self._place('portrait_1880.jpg', 'photos/b/portrait_1880.jpg', PHOTO_SID)
        before = self.record.read_text(encoding='utf-8')

        result = self._run(with_exif=True)
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result['photos_ambiguous'], 1)
        text = ' '.join(m.text for m in result.messages if m.level == 'warning')
        self.assertIn('photos/a/portrait_1880.jpg', text)
        self.assertIn('photos/b/portrait_1880.jpg', text)
        self.assertIn('file: photos/portrait_1880.jpg', text)

    # -- the guards around them ----------------------------------------------

    def test_two_differently_named_carriers_are_proposed_not_applied(self) -> None:
        # Neither carrier keeps the listed name, so only the keyword links
        # them - a proposal naming both, never a heal.
        self._move('photos/portrait_1880.jpg', 'photos/a/renamed-one.jpg')
        self._place('wedding_1902.jpg', 'photos/b/renamed-two.jpg', PHOTO_SID)
        before = self.record.read_text(encoding='utf-8')
        result = self._run(with_exif=True)
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertEqual(result['photos_proposed'], 1)
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertIn('photos/a/renamed-one.jpg', self._warnings(result))
        self.assertIn('photos/b/renamed-two.jpg', self._warnings(result))

    def test_moved_and_renamed_photo_is_proposed_not_applied(self) -> None:
        # Another program (a Lightroom rename, SPEC §13) renamed it on the way.
        # The keyword alone cannot tell this photo from any other file a photo
        # manager stamped with it, so the owner's rule is: propose, never apply.
        self._move('photos/portrait_1880.jpg', 'photos/1880s/hartley-portrait.jpg')
        before = self.record.read_text(encoding='utf-8')
        result = self._run(with_exif=True)
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result['photos_proposed'], 1)
        text = self._warnings(result)
        self.assertIn('photos/1880s/hartley-portrait.jpg', text)
        self.assertIn(f'`file: photos/portrait_1880.jpg` line in {self.record.name}', text)

    # -- review round 2 (P1/P2 findings) -------------------------------------

    def test_lone_entry_never_heals_onto_unlisted_back_carrying_keyword(self) -> None:
        # The front is gone; its back scan, never listed, carries the same
        # SOURCE: keyword. Re-tying the front's entry to it would swap the
        # picture the record shows for a different one.
        (self.root / 'photos' / 'portrait_1880.jpg').unlink()
        self.keywords.clear()
        self._place('portrait_1880-back.jpg', 'photos/portrait_1880-back.jpg', PHOTO_SID)
        before = self.record.read_text(encoding='utf-8')
        result = self._run(with_exif=True)
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertNotIn(str(self.record), result.changed)
        self.assertIn('photos/portrait_1880-back.jpg', self._warnings(result))

    def test_lone_entry_never_heals_onto_edit_copy_when_original_moved_into_ignored_folder(
            self) -> None:
        (self.root / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n  documents: documents\n'
            "photos_ignore:\n  - 'Bulk'\n", encoding='utf-8')
        config = load_fha_yaml(self.root)
        self._move('photos/portrait_1880.jpg', 'photos/Bulk/portrait_1880.jpg')
        self._place('portrait_1880.jpg', 'photos/portrait_1880-Edit.jpg', PHOTO_SID)
        before = self.record.read_text(encoding='utf-8')
        result = reconcile.run_reconcile(self.root, config, with_exif=True)
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertIn('photos/portrait_1880-Edit.jpg', self._warnings(result))

    def test_same_name_heal_refused_when_part_kind_disagrees_with_role(self) -> None:
        # The record says the entry is the front, but the only same-named
        # candidate's name says it is a back scan. Something is inconsistent;
        # a heal would carry the inconsistency onto a new path silently.
        self.record = self._write_photo_record(PHOTO_SID, ['photos/portrait_1880-back.jpg'])
        self._place('portrait_1880-back.jpg', 'photos/a/portrait_1880-back.jpg', PHOTO_SID)
        before = self.record.read_text(encoding='utf-8')
        result = self._run(with_exif=True)
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertIn('back', self._warnings(result))

    def test_same_name_in_two_folders_prefers_the_longest_matching_tail(self) -> None:
        self.record = self._write_photo_record(PHOTO_SID, ['photos/front/0001.jpg'])
        (self.root / 'photos' / 'portrait_1880.jpg').unlink()
        self.keywords.clear()
        self._place('portrait_1880.jpg', 'photos/2020/front/0001.jpg', PHOTO_SID)
        self._place('portrait_1880-back.jpg', 'photos/2020/back/0001.jpg', PHOTO_SID)
        result = self._run(with_exif=True)
        self.assertEqual(self._files(), ['photos/2020/front/0001.jpg'])
        self.assertEqual(result.exit_code, EXIT_CLEAN, [m.text for m in result.messages])

    def test_one_candidate_wanted_by_two_entries_is_refused_for_both(self) -> None:
        # Two prints of one photo, both role: variant, in folders x/ and y/.
        self.record = self._write_record_text(
            f'portrait_{PHOTO_SID}.md',
            f'---\nid: {PHOTO_SID}\ntitle: A portrait\nsource_type: photo\nfiles:\n'
            '  - file: photos/x/0001.jpg\n    role: variant\n'
            '  - file: photos/y/0001.jpg\n    role: variant\n---\n')
        (self.root / 'photos' / 'portrait_1880.jpg').unlink()
        self.keywords.clear()
        self._place('portrait_1880.jpg', 'photos/z/0001.jpg', PHOTO_SID)
        before = self.record.read_text(encoding='utf-8')
        result = self._run(with_exif=True)
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertEqual(result['photos_ambiguous'], 2)

    def test_listed_photo_spelled_differently_is_still_treated_as_listed(self) -> None:
        # Another record lists the moved file through an odd but valid
        # spelling. It is still that record's photo, so it is never taken.
        self._move('photos/portrait_1880.jpg', 'photos/a/portrait_1880.jpg')
        self._write_record_text(
            'other_S-0000000001.md',
            '---\nid: S-0000000001\ntitle: Other\nsource_type: photo\nfiles:\n'
            '  - file: photos/a/./portrait_1880.jpg\n    role: front\n---\n')
        before = self.record.read_text(encoding='utf-8')
        result = self._run(with_exif=True)
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertEqual(result.exit_code, EXIT_WARNINGS)

    def test_heal_never_duplicates_an_entry_in_the_same_record(self) -> None:
        # The record already lists the moved photo itself (spelled oddly):
        # pointing its stale entry there too would list one photo twice.
        self._move('photos/portrait_1880.jpg', 'photos/a/portrait_1880.jpg')
        self.record = self._write_photo_record(
            PHOTO_SID, ['photos/portrait_1880.jpg', 'photos/a/./portrait_1880.jpg'])
        before = self.record.read_text(encoding='utf-8')
        result = self._run(with_exif=True)
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        # And the writer itself refuses such a heal, whoever planned it.
        direct = reconcile.Result()
        applied = reconcile._apply(
            self.root, {self.record: [('photos/portrait_1880.jpg', 'photos/a/portrait_1880.jpg')]},
            direct, self.config)
        self.assertEqual(applied, 0)
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertTrue(any(m.level == 'warning' for m in direct.messages))

    def _two_source_photo(self) -> None:
        self._move('photos/portrait_1880.jpg', 'photos/a/portrait_1880.jpg')
        moved = (self.root / 'photos' / 'a' / 'portrait_1880.jpg').resolve()
        self.keywords[moved] = ['SOURCE: S-0000000001', f'SOURCE: {PHOTO_SID}']

    def test_photo_with_two_source_keywords_is_ambiguous_not_gone(self) -> None:
        for route in ('exif', 'catalog'):
            with self.subTest(route=route):
                self.setUp_fresh()
                self._two_source_photo()
                if route == 'catalog':
                    self._scan()
                before = self.record.read_text(encoding='utf-8')
                result = self._run(with_exif=(route == 'exif'))
                self.assertEqual(self.record.read_text(encoding='utf-8'), before)
                self.assertEqual(result['photos_missing'], 0)
                self.assertEqual(result['photos_ambiguous'], 1)
                self.assertNotIn('is gone', self._warnings(result))

    def setUp_fresh(self) -> None:
        """Reset to the setUp state, for a test that loops over routes."""
        self.tearDown()
        self.setUp()

    def test_alias_sid_owned_by_another_record_never_steals_its_photo(self) -> None:
        other_sid = 'S-0000000001'
        (self.root / 'photos' / 'portrait_1880.jpg').unlink()
        self.record.unlink()
        self.keywords.clear()
        # Record A (sorted first) wrongly lists B's live id among its aliases.
        rec_a = self._write_record_text(
            f'a_{PHOTO_SID}.md',
            f'---\nid: {PHOTO_SID}\naliases: [{other_sid}]\ntitle: A\nsource_type: photo\n'
            'files:\n  - file: photos/portrait_1880.jpg\n    role: front\n---\n')
        rec_b = self._write_record_text(
            f'b_{other_sid}.md',
            f'---\nid: {other_sid}\ntitle: B\nsource_type: photo\n'
            'files:\n  - file: photos/portrait_1880.jpg\n    role: front\n---\n')
        self._place('portrait_1880.jpg', 'photos/b/portrait_1880.jpg', other_sid)
        result = self._run(with_exif=True)
        self.assertEqual(self._files(rec_a), ['photos/portrait_1880.jpg'])
        self.assertEqual(self._files(rec_b), ['photos/b/portrait_1880.jpg'])
        self.assertIn(other_sid.upper(), self._warnings(result))

    def test_retired_alias_sid_still_heals(self) -> None:
        retired = 'S-0000000009'
        self.record = self._write_record_text(
            f'portrait_{PHOTO_SID}.md',
            f'---\nid: {PHOTO_SID}\naliases: [{retired}]\ntitle: A\nsource_type: photo\n'
            'files:\n  - file: photos/portrait_1880.jpg\n    role: front\n---\n')
        self._move('photos/portrait_1880.jpg', 'photos/a/portrait_1880.jpg')
        self.keywords[(self.root / 'photos/a/portrait_1880.jpg').resolve()] = [
            f'SOURCE: {retired}']
        result = self._run(with_exif=True)
        self.assertEqual(self._files(), ['photos/a/portrait_1880.jpg'])
        self.assertEqual(result.exit_code, EXIT_CLEAN, [m.text for m in result.messages])

    def test_old_path_reused_by_different_photo_is_reported_not_called_tied(self) -> None:
        self._move('photos/portrait_1880.jpg', 'photos/a/portrait_1880.jpg')
        self._place('wedding_1902.jpg', 'photos/portrait_1880.jpg', None)
        before = self.record.read_text(encoding='utf-8')
        result = self._run(with_exif=True)
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        texts = [m.text for m in result.messages]
        self.assertFalse(any('Filed photos all tied' in t or 'No filed photo is missing' in t
                             for t in texts), texts)
        warnings = self._warnings(result)
        self.assertIn('photos/a/portrait_1880.jpg', warnings)
        self.assertIn(f'`file: photos/portrait_1880.jpg` line in {self.record.name}', warnings)

    def test_missing_message_does_not_deny_the_records_own_carrier(self) -> None:
        # Front gone; the back the record lists still carries the keyword.
        self.record = self._write_photo_record(
            PHOTO_SID, ['photos/portrait_1880.jpg', 'photos/portrait_1880-back.jpg'])
        (self.root / 'photos' / 'portrait_1880.jpg').unlink()
        self.keywords.clear()
        self._place('portrait_1880-back.jpg', 'photos/portrait_1880-back.jpg', PHOTO_SID)
        result = self._run(with_exif=True)
        self.assertEqual(result['photos_missing'], 1)
        text = self._warnings(result)
        self.assertNotIn(f'no photo in the photos folder carries {PHOTO_SID.upper()}', text)
        self.assertIn('no other photo', text)

    @unittest.skipIf(os.name == 'nt', 'symlinks need privileges on Windows')
    def test_photo_heal_into_symlinked_folder_not_reported_truly_gone(self) -> None:
        outside = Path(self._tmp.name).parent / f'{Path(self._tmp.name).name}-linked'
        outside.mkdir()
        self.addCleanup(lambda: __import__('shutil').rmtree(outside, ignore_errors=True))
        (self.root / 'photos' / 'linked').symlink_to(outside, target_is_directory=True)
        self._move('photos/portrait_1880.jpg', 'photos/linked/portrait_1880.jpg')
        before = self.record.read_text(encoding='utf-8')
        result = self._run(with_exif=True)
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertEqual(result['photos_missing'], 0)
        text = self._warnings(result)
        self.assertNotIn('is gone', text)
        self.assertIn('linked', text)

    def test_photo_dry_run_makes_zero_mutations(self) -> None:
        self._scan()
        self._move('photos/portrait_1880.jpg', 'photos/portraits/portrait_1880.jpg')

        def snapshot():
            out = {}
            for base in ('sources', '.cache'):
                for p in sorted((self.root / base).rglob('*')):
                    if p.is_file():
                        out[p.relative_to(self.root).as_posix()] = p.read_bytes()
            return out

        before = snapshot()
        result = self._run(dry_run=True, with_exif=True)
        self.assertTrue(any('[dry-run] Would re-tie' in m.text for m in result.messages))
        self.assertEqual(snapshot(), before)
        self.assertEqual(result.changed, [])

    def test_front_and_back_both_moved_heal_by_name(self) -> None:
        # Front and back share one SOURCE: keyword. Two carriers, but each
        # keeps its own name, so each entry has exactly one same-named match.
        self.record = self._write_photo_record(
            PHOTO_SID, ['photos/portrait_1880.jpg', 'photos/portrait_1880-back.jpg'])
        self._place('portrait_1880-back.jpg', 'photos/portrait_1880-back.jpg', PHOTO_SID)
        self._move('photos/portrait_1880.jpg', 'photos/1880s/portrait_1880.jpg')
        self._move('photos/portrait_1880-back.jpg', 'photos/1880s/portrait_1880-back.jpg')
        result = self._run(with_exif=True)
        self.assertEqual(self._files(), ['photos/1880s/portrait_1880.jpg',
                                         'photos/1880s/portrait_1880-back.jpg'])
        self.assertEqual(result.exit_code, EXIT_CLEAN, [m.text for m in result.messages])

    def test_front_and_back_both_renamed_are_proposed_not_applied(self) -> None:
        # Two stale entries, two carriers, and neither carrier keeps a listed
        # name: which file is the front is not something the keyword says.
        self.record = self._write_photo_record(
            PHOTO_SID, ['photos/portrait_1880.jpg', 'photos/portrait_1880-back.jpg'])
        self._place('portrait_1880-back.jpg', 'photos/portrait_1880-back.jpg', PHOTO_SID)
        self._move('photos/portrait_1880.jpg', 'photos/1880s/one.jpg')
        self._move('photos/portrait_1880-back.jpg', 'photos/1880s/two.jpg')
        before = self.record.read_text(encoding='utf-8')
        result = self._run(with_exif=True)
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertEqual(result['photos_proposed'], 2)

    def test_one_renamed_carrier_for_two_lost_entries_is_proposed_not_applied(self) -> None:
        # Front renamed on its way to a new folder, back gone altogether: one
        # carrier, two entries. Whether it is the front or the back is not
        # something the keyword can say, so neither entry takes it.
        self.record = self._write_photo_record(
            PHOTO_SID, ['photos/portrait_1880.jpg', 'photos/portrait_1880-back.jpg'])
        self._move('photos/portrait_1880.jpg', 'photos/1880s/one.jpg')
        before = self.record.read_text(encoding='utf-8')
        result = self._run(with_exif=True)
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertEqual(result['photos_proposed'], 2)

    def test_an_unread_same_named_copy_blocks_a_name_match(self) -> None:
        # The catalog knows one copy carries the keyword; a second copy with
        # the same name arrived after the scan and was never read. It might
        # carry the keyword too, so the match is not certain.
        self._move('photos/portrait_1880.jpg', 'photos/a/portrait_1880.jpg')
        self._scan()
        self._place('portrait_1880.jpg', 'photos/b/portrait_1880.jpg', PHOTO_SID)
        before = self.record.read_text(encoding='utf-8')
        result = self._run()
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertEqual(result['photos_unverified'], 1)
        warnings = ' '.join(m.text for m in result.messages if m.level == 'warning')
        self.assertIn('photos/b/portrait_1880.jpg', warnings)
        self.assertIn('fha reconcile --with-exif', warnings)

    def test_an_unread_photo_blocks_a_keyword_only_match(self) -> None:
        # Renamed on the way, so only the keyword can match it - and a photo
        # nobody has read yet could be a second carrier.
        self._move('photos/portrait_1880.jpg', 'photos/a/renamed.jpg')
        self._scan()
        self._place('wedding_1902.jpg', 'photos/new/unscanned.jpg', None)
        before = self.record.read_text(encoding='utf-8')
        result = self._run()
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertEqual(result['photos_proposed'], 1)
        self.assertIn('fha reconcile --with-exif', self._warnings(result))

    def test_a_catalog_row_for_a_changed_file_is_not_trusted(self) -> None:
        # The catalog says the file carries the keyword, but the file changed
        # after the scan (its keyword was taken off). Its row no longer
        # describes it, so it is not taken as a carrier without a fresh read.
        self._move('photos/portrait_1880.jpg', 'photos/a/portrait_1880.jpg')
        self._scan()
        changed = self.root / 'photos' / 'a' / 'portrait_1880.jpg'
        with changed.open('ab') as fh:
            fh.write(b'\0')
        self.keywords[changed.resolve()] = []
        before = self.record.read_text(encoding='utf-8')
        result = self._run()
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertEqual(result['photos_unverified'], 1)
        # And a fresh read settles it: the file no longer carries the keyword.
        result = self._run(with_exif=True)
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertEqual(result['photos_missing'], 1)

    def test_front_renamed_back_still_listed_is_proposed_for_the_front_only(self) -> None:
        self.record = self._write_photo_record(
            PHOTO_SID, ['photos/portrait_1880.jpg', 'photos/portrait_1880-back.jpg'])
        self._place('portrait_1880-back.jpg', 'photos/portrait_1880-back.jpg', PHOTO_SID)
        self._move('photos/portrait_1880.jpg', 'photos/1880s/renamed-front.jpg')
        before = self.record.read_text(encoding='utf-8')
        result = self._run(with_exif=True)
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertEqual(result['photos_proposed'], 1)
        text = self._warnings(result)
        self.assertIn('photos/1880s/renamed-front.jpg', text)
        self.assertNotIn('`file: photos/portrait_1880-back.jpg`', text)

    def test_a_carrier_another_record_lists_is_never_taken(self) -> None:
        # The only file carrying the keyword is already another record's
        # entry: re-tying onto it would point two records at one photo.
        self._move('photos/portrait_1880.jpg', 'photos/shared/portrait_1880.jpg')
        other = self.root / 'sources' / 'photos' / 'other_S-0000000001.md'
        other.write_text(
            '---\nid: S-0000000001\ntitle: Other\nsource_type: photo\nfiles:\n'
            '  - file: photos/shared/portrait_1880.jpg\n    role: front\n---\n',
            encoding='utf-8')
        before = self.record.read_text(encoding='utf-8')
        result = self._run(with_exif=True)
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertNotIn(str(self.record), result.changed)

    def test_without_keywords_to_read_nothing_is_guessed(self) -> None:
        # No catalog and no --with-exif: a same-named file is sitting in a new
        # folder, but nothing has read its keyword, so nothing is rewritten
        # and the message names the flag that would let reconcile look.
        self._move('photos/portrait_1880.jpg', 'photos/portraits/portrait_1880.jpg')
        before = self.record.read_text(encoding='utf-8')
        result = self._run()
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        warnings = [m.text for m in result.messages if m.level == 'warning']
        self.assertTrue(any('fha reconcile --with-exif' in t for t in warnings), warnings)

    def test_stale_catalog_without_exif_points_at_with_exif(self) -> None:
        # Case A without the flag: the catalog still holds the photo under its
        # old path, and the same-named file at the new path was never read.
        self._scan()
        self._move('photos/portrait_1880.jpg', 'photos/portraits/portrait_1880.jpg')
        before = self.record.read_text(encoding='utf-8')
        result = self._run()
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        warnings = [m.text for m in result.messages if m.level == 'warning']
        self.assertTrue(any('fha reconcile --with-exif' in t
                            and 'photos/portraits/portrait_1880.jpg' in t
                            for t in warnings), warnings)

    def test_no_exiftool_names_the_line_to_edit(self) -> None:
        from _lib import format_exiftool_error
        self._move('photos/portrait_1880.jpg', 'photos/portraits/portrait_1880.jpg')

        def no_exiftool(_paths, *_args):
            raise RuntimeError(format_exiftool_error('fha reconcile'))

        before = self.record.read_text(encoding='utf-8')
        with unittest.mock.patch.object(photoindex, '_run_exiftool', side_effect=no_exiftool):
            result = self._run(with_exif=True)
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        text = ' '.join(m.text for m in result.messages if m.level == 'warning')
        self.assertIn('exiftool', text)
        self.assertIn(f'`file: photos/portrait_1880.jpg` line in {self.record.name}', text)

    def test_photo_moved_into_an_ignored_folder_is_not_re_tied(self) -> None:
        (self.root / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n  documents: documents\n'
            "photos_ignore:\n  - 'Bulk Export'\n", encoding='utf-8')
        config = load_fha_yaml(self.root)
        self._move('photos/portrait_1880.jpg', 'photos/Bulk Export/portrait_1880.jpg')
        before = self.record.read_text(encoding='utf-8')
        result = reconcile.run_reconcile(self.root, config, with_exif=True)
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertEqual(result['photos_missing'], 1)
        text = ' '.join(m.text for m in result.messages if m.level == 'warning')
        self.assertIn('photos_ignore', text)

    def test_a_malformed_photos_ignore_is_one_plain_error(self) -> None:
        (self.root / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n  documents: documents\n'
            'photos_ignore:\n  bad: shape\n', encoding='utf-8')
        config = load_fha_yaml(self.root)
        self._move('photos/portrait_1880.jpg', 'photos/a/portrait_1880.jpg')
        before = self.record.read_text(encoding='utf-8')
        result = reconcile.run_reconcile(self.root, config, with_exif=True)
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        errors = [m.text for m in result.messages if m.level == 'error']
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('photos_ignore', errors[0])

    def test_missing_fixture_photo_entry_is_left_alone(self) -> None:
        self.record = self._write_photo_record(
            PHOTO_SID, ['photos/gone.jpg'], status='missing-fixture')
        before = self.record.read_text(encoding='utf-8')
        result = self._run(with_exif=True)
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertEqual(result.exit_code, EXIT_CLEAN, [m.text for m in result.messages])

    def test_a_photos_folder_that_will_not_open_heals_nothing(self) -> None:
        # A second copy hiding in a folder that will not list would make the
        # visible one look unique; re-tying from that picture could be wrong.
        self._move('photos/portrait_1880.jpg', 'photos/seen/portrait_1880.jpg')
        self._place('portrait_1880.jpg', 'photos/shut/inner/portrait_1880.jpg', PHOTO_SID)
        before = self.record.read_text(encoding='utf-8')
        with unittest.mock.patch(
                'os.scandir', new=_scandir_denying(self.root / 'photos' / 'shut')):
            result = self._run(with_exif=True)
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('shut', text)
        self.assertNotIn('Re-tied', text)

    def test_unmoved_photo_records_report_nothing_to_heal(self) -> None:
        result = self._run(with_exif=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN, [m.text for m in result.messages])
        self.assertEqual(result['photos_healed'], 0)


@unittest.skipUnless(__import__('shutil').which('exiftool'), 'needs exiftool on PATH')
class PhotoReconcileEndToEndTests(unittest.TestCase):
    """#289 with the real tools: `fha process` embeds the keyword, the photo
    moves, `fha reconcile --with-exif` re-ties the record, lint is clean.
    Case A has no rescan between the move and the reconcile; case B runs
    `fha photoindex` after the move, which drops the old catalog row."""

    FHA = ROOT / 'tools' / 'fha.py'

    def _fha(self, root: Path, *args: str) -> 'subprocess.CompletedProcess':
        import subprocess
        return subprocess.run(
            [sys.executable, str(self.FHA), *args, '--root', str(root)],
            capture_output=True, text=True, encoding='utf-8')

    def _case(self, rescan_after_move: bool) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'fha.yaml').write_text(
                'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8')
            for sub in ('photos', 'documents', 'sources'):
                (root / sub).mkdir()
            photo = root / 'photos' / 'portrait_1880.jpg'
            photo.write_bytes((PHOTO_FIXTURES / 'portrait_1880.jpg').read_bytes())
            done = self._fha(root, 'process', str(photo))
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertEqual(self._fha(root, 'photoindex').returncode, 0)
            (root / 'photos' / 'portraits').mkdir()
            photo.rename(root / 'photos' / 'portraits' / 'portrait_1880.jpg')
            if rescan_after_move:
                self.assertEqual(self._fha(root, 'photoindex').returncode, 0)
            healed = self._fha(root, 'reconcile', '--with-exif')
            self.assertEqual(healed.returncode, 0, healed.stdout + healed.stderr)
            self.assertIn('Re-tied photos/portrait_1880.jpg -> '
                          'photos/portraits/portrait_1880.jpg', healed.stdout)
            lint_out = self._fha(root, 'lint')
            self.assertNotIn('E011', lint_out.stdout + lint_out.stderr)

    def test_case_a_move_then_reconcile(self) -> None:
        self._case(rescan_after_move=False)

    def test_case_b_move_rescan_then_reconcile(self) -> None:
        self._case(rescan_after_move=True)


class PhotoE011MessageTests(unittest.TestCase):
    """#289: lint E011 for a photos-root entry names a fix that can work.

    Before, it promised `fha reconcile` re-ties the photo, which no command
    did. Now reconcile can, but only when it can read the photo's keyword:
    from a current catalog (plain `fha reconcile`), or through exiftool
    (`--with-exif`). With neither, the honest next step is the hand edit.
    """

    def setUp(self) -> None:
        import lint
        self.lint = lint
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8')
        (self.root / 'documents').mkdir()
        (self.root / 'photos').mkdir()
        (self.root / 'sources' / 'photos').mkdir(parents=True)
        self.record = self.root / 'sources' / 'photos' / f'portrait_{PHOTO_SID}.md'
        self.record.write_text(
            f'---\nid: {PHOTO_SID}\ntitle: A portrait\nsource_type: photo\nfiles:\n'
            '  - file: photos/portrait_1880.jpg\n    role: front\n---\n\n## Notes\nx.\n',
            encoding='utf-8')
        self.config = load_fha_yaml(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _e011(self, *, catalog: str, exiftool: bool) -> str:
        import _lib
        with unittest.mock.patch.object(_lib, 'photoindex_status',
                                        return_value=(catalog, 0.0)), \
             unittest.mock.patch.object(_lib.shutil, 'which',
                                        return_value='/usr/bin/exiftool' if exiftool else None):
            result = self.lint.run_lint(self.root, self.config)
        found = [m.text for m in result.messages if m.code == 'E011']
        self.assertEqual(len(found), 1, found)
        return found[0]

    def test_fresh_catalog_points_at_plain_reconcile(self) -> None:
        text = self._e011(catalog='fresh', exiftool=False)
        self.assertIn('`fha reconcile`', text)
        self.assertNotIn('--with-exif', text)

    def test_exiftool_points_at_reconcile_with_exif(self) -> None:
        text = self._e011(catalog='stale', exiftool=True)
        self.assertIn('`fha reconcile --with-exif`', text)

    def test_neither_names_the_line_to_edit(self) -> None:
        text = self._e011(catalog='absent', exiftool=False)
        self.assertNotIn('`fha reconcile`', text)
        self.assertIn('`file: photos/portrait_1880.jpg`', text)
        self.assertIn('exiftool', text)

    def test_shared_route_used_by_doctor_and_refile(self) -> None:
        # doctor's orphaned-back-scan step and refile's not-on-disk message
        # route through the same helper E011 uses, so none of them names
        # --with-exif where exiftool is absent.
        import _lib
        cases = {('fresh', True): 'catalog', ('stale', True): 'exif',
                 ('absent', False): 'none', ('fresh', False): 'catalog'}
        for (catalog, exiftool), want in cases.items():
            with unittest.mock.patch.object(_lib, 'photoindex_status',
                                            return_value=(catalog, 0.0)), \
                 unittest.mock.patch.object(_lib.shutil, 'which',
                                            return_value='/x/exiftool' if exiftool else None):
                self.assertEqual(_lib.photo_reconcile_route(self.root, self.config), want)

    def test_documents_entry_keeps_its_own_message(self) -> None:
        self.record.write_text(
            f'---\nid: {PHOTO_SID}\ntitle: A letter\nsource_type: letter\nfiles:\n'
            f'  - file: documents/letter_{PHOTO_SID}.pdf\n    role: primary\n---\n',
            encoding='utf-8')
        text = self._e011(catalog='absent', exiftool=False)
        self.assertIn('`fha reconcile` re-ties it', text)


if __name__ == '__main__':
    unittest.main()
