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

import reconcile
from _lib import EXIT_CLEAN, EXIT_WARNINGS, load_fha_yaml, read_record

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
