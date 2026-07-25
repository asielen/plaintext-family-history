"""
test_reconcile.py - fha reconcile, the documents-side path healer (TOOLING §9).

The engine reads record files and the documents tree directly (no index
needed - the .md files are the truth it heals), so the fixture here is a tiny
on-disk archive: one source record with a files: inventory, plus document
files placed and then moved to exercise each plan bucket (healed, ambiguous,
missing, unlisted). The photos side is photoindex's own machinery, gated on a
photos.sqlite that these fixtures deliberately do not create.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import argparse
import contextlib
import io

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

    def test_unreachable_documents_root_warns_without_mass_flagging(self) -> None:
        (self.root / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n  documents: Q:/no/such/drive\n', encoding='utf-8')
        config = load_fha_yaml(self.root)
        result = reconcile.run_reconcile(self.root, config)
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result['missing'], 0)
        self.assertTrue(any('not reachable' in m.text for m in result.messages))


if __name__ == '__main__':
    unittest.main()
