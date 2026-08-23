"""
test_source_clear_keyword.py - fha source clear-keyword (#112, TOOLING §3d).

The tool-mediated fix for a stray embedded keyword lint's W131 (tools/lint.py,
tests/test_lint.py's StrayPersonKeywordW131Tests) finds on a documents-root
asset. exiftool is never invoked for real here - `source_mod._run_exiftool_
read_keyword_fields`/`_run_exiftool_edit_keyword_fields` are monkeypatched
against a small in-memory "file" (a dict of {'keywords': [...], 'subject':
[...]}), the same seam-substitution test_process.py already uses for its own
exiftool wrappers. The fake write function actually mutates that shared
state, so a test that reads again after writing is a genuine round-trip
check, not just an assertion on the call arguments.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import source as source_mod
from _lib import EXIT_CLEAN, EXIT_FAILURE, EXIT_WARNINGS, load_fha_yaml

SID = 'S-2b3c4d5e6f'


class _FakeExiftoolStore:
    """In-memory Keywords/Subject fields for one or more files, keyed by
    resolved path - read and written the same way the real exiftool wrappers
    would. The fake write function applies '-=' / '+=' semantics against
    this dict, so a read after a write proves the change actually landed on
    the RIGHT file (a genuine round trip), not merely that a plausible call
    was made somewhere."""

    def __init__(self) -> None:
        self.files: dict[str, dict[str, list[str]]] = {}
        self.write_calls: list[tuple[Path, list, list]] = []

    def seed(self, path: Path, *, keywords=None, subject=None) -> None:
        self.files[str(Path(path).resolve())] = {
            'keywords': list(keywords or []), 'subject': list(subject or [])}

    def _fields(self, path: Path) -> dict:
        return self.files.setdefault(
            str(Path(path).resolve()), {'keywords': [], 'subject': []})

    def read(self, path: Path) -> dict:
        f = self._fields(path)
        return {'keywords': list(f['keywords']), 'subject': list(f['subject'])}

    def write(self, path: Path, *, remove, add, backup):
        self.write_calls.append((Path(path).resolve(), list(remove), list(add)))
        backup.ensure(path)   # exercise the real safety-copy policy, like the live wrapper
        f = self._fields(path)
        for tag, value in remove:
            f[tag] = [v for v in f[tag] if v != value]
        for tag, value in add:
            f[tag].append(value)
        return None


class SourceClearKeywordTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8')
        (self.root / 'documents' / 'deeds').mkdir(parents=True)
        (self.root / 'sources' / 'deeds').mkdir(parents=True)
        self.asset = self.root / 'documents' / 'deeds' / f'deed_{SID.lower()}.tif'
        self.asset.write_bytes(b'x')
        self.record = self.root / 'sources' / 'deeds' / f'deed_{SID.lower()}.md'
        self._write_record()
        self.config = load_fha_yaml(self.root)

        self.store = _FakeExiftoolStore()
        self.store.seed(self.asset, subject=['Margaret Hartley', 'SOURCE: ' + SID])
        self._orig_read = source_mod._run_exiftool_read_keyword_fields
        self._orig_write = source_mod._run_exiftool_edit_keyword_fields
        source_mod._run_exiftool_read_keyword_fields = self.store.read
        source_mod._run_exiftool_edit_keyword_fields = self.store.write

    def tearDown(self) -> None:
        source_mod._run_exiftool_read_keyword_fields = self._orig_read
        source_mod._run_exiftool_edit_keyword_fields = self._orig_write
        self._tmp.cleanup()

    def _write_record(self, files_block: str | None = None, people: str = '') -> None:
        if files_block is None:
            files_block = (f'files:\n  - file: documents/deeds/deed_{SID.lower()}.tif\n'
                           '    role: primary\n')
        self.record.write_text(
            f'---\nid: {SID}\ntitle: Deed\nsource_type: deed\n'
            f'{files_block}{people}---\n\n## Notes\nA county deed.\n',
            encoding='utf-8')

    def _run(self, **kw):
        return source_mod.run_source_clear_keyword(self.root, self.config, SID, **kw)

    # -- the round trip -----------------------------------------------------

    def test_clear_removes_the_exact_keyword_and_round_trips(self) -> None:
        result = self._run(keyword='Margaret Hartley')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['removed_from'], ['subject'])
        self.assertTrue(result.changed, 'a live write must record the changed asset')

        # Round-trip: read the (fake) file again and confirm the keyword is
        # actually gone, not just that a plausible call was made.
        after = source_mod._run_exiftool_read_keyword_fields(self.asset)
        self.assertNotIn('Margaret Hartley', after['subject'])
        self.assertIn('SOURCE: ' + SID, after['subject'])   # untouched

    def test_replace_with_lands_in_the_same_field_and_round_trips(self) -> None:
        result = self._run(keyword='Margaret Hartley', replace_with='Margaret Cole')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['removed_from'], ['subject'])
        self.assertEqual(result['added_to'], ['subject'])

        after = source_mod._run_exiftool_read_keyword_fields(self.asset)
        self.assertNotIn('Margaret Hartley', after['subject'])
        self.assertIn('Margaret Cole', after['subject'])
        self.assertNotIn('Margaret Cole', after['keywords'])   # never grows a NEW field

    def test_match_is_case_insensitive_but_removes_the_exact_on_file_spelling(self) -> None:
        # exiftool's -= is an exact-value match; the command line spelling may
        # differ in case from what is actually on the file.
        result = self._run(keyword='margaret hartley')
        self.assertEqual(result['status'], 'ok')
        _path, removed, _added = self.store.write_calls[0]
        tag, value = removed[0]
        self.assertEqual(value, 'Margaret Hartley')   # the ON-FILE spelling, not the arg

    # -- refusals -------------------------------------------------------------

    def test_keyword_not_present_refuses_without_writing(self) -> None:
        result = self._run(keyword='Nobody Here')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(self.store.write_calls, [])
        self.assertIn('does not currently carry', result.messages[-1].text)

    def test_dry_run_writes_nothing(self) -> None:
        result = self._run(keyword='Margaret Hartley', dry_run=True)
        self.assertEqual(result['status'], 'dry-run')
        self.assertEqual(self.store.write_calls, [])
        after = source_mod._run_exiftool_read_keyword_fields(self.asset)
        self.assertIn('Margaret Hartley', after['subject'])   # untouched

    def test_bad_source_id_refuses(self) -> None:
        result = source_mod.run_source_clear_keyword(
            self.root, self.config, 'not-an-id', keyword='x')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result['status'], 'refused')

    def test_blank_keyword_refuses(self) -> None:
        result = self._run(keyword='   ')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result['status'], 'refused')

    def test_unknown_source_is_not_found(self) -> None:
        result = source_mod.run_source_clear_keyword(
            self.root, self.config, 'S-9999999999', keyword='x')
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result['status'], 'not-found')

    def test_asset_missing_on_disk_is_not_found(self) -> None:
        self.asset.unlink()
        result = self._run(keyword='Margaret Hartley')
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result['status'], 'not-found')
        self.assertIn('fha reconcile', result.messages[-1].text)

    def test_asset_missing_on_disk_reports_ok_false(self) -> None:
        # #147 review (P2): the missing-record branch a few lines above this
        # one already routes through result_fail (ok=False); this branch used
        # to leave Result.ok at its True default even though nothing was
        # cleared - a headless caller reading Result.as_dict() would see a
        # 'not-found' status sitting next to ok: true.
        self.asset.unlink()
        result = self._run(keyword='Margaret Hartley')
        self.assertFalse(result.ok,
                         'a missing asset must report ok=False, not the True default')

    def test_no_documents_root_file_refuses(self) -> None:
        self._write_record(files_block='files:\n  - file: photos/1900/deed.jpg\n    role: primary\n')
        result = self._run(keyword='Margaret Hartley')
        self.assertEqual(result['status'], 'refused')
        self.assertIn('documents-root', result.messages[-1].text)

    def test_alias_escaping_the_documents_root_is_refused(self) -> None:
        # #147 review (P1): the doc_entries filter only checks that the alias
        # STARTS WITH 'documents' as text, so a hand-edited '..' segment
        # passes it - but resolve_path would then land outside the
        # configured documents root entirely. Must refuse before exiftool
        # ever touches the resolved (wrong) target.
        self._write_record(files_block=(
            'files:\n  - file: documents/../../outside.tif\n    role: primary\n'))
        result = self._run(keyword='Margaret Hartley')
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(self.store.write_calls, [])
        self.assertIn('outside the configured documents folder', result.messages[-1].text)

    def test_doubled_slash_alias_escaping_the_root_is_refused(self) -> None:
        self._write_record(files_block=(
            'files:\n  - file: documents//../../outside.tif\n    role: primary\n'))
        result = self._run(keyword='Margaret Hartley')
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(self.store.write_calls, [])
        self.assertIn('outside the configured documents folder', result.messages[-1].text)

    def test_directory_target_is_refused_before_exiftool(self) -> None:
        # #147 review (P1): a hand-edited files: entry can name a FOLDER
        # ('documents/deeds' already exists in this fixture) rather than one
        # file - exiftool accepts a directory operand and applies the write
        # to every file inside it, so this must refuse before any exiftool
        # call at all (originals_backup is unset here too - no safety copy).
        self._write_record(files_block='files:\n  - file: documents/deeds\n    role: primary\n')
        result = self._run(keyword='Margaret Hartley')
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(self.store.write_calls, [])
        self.assertIn('folder', result.messages[-1].text)

    def test_files_entry_pointing_at_a_different_sources_file_is_refused(self) -> None:
        # #147 review (P1): inventory drift - this source's own record still
        # lists a file whose ACTUAL on-disk filename carries a DIFFERENT
        # source's S-id. Following the record's files: entry blindly would
        # edit the wrong document; the filename's own embedded id has to be
        # checked against the source clear-keyword was actually asked to fix.
        other_sid = 'S-9999999999'
        drifted = self.root / 'documents' / 'deeds' / f'deed_{other_sid.lower()}.tif'
        drifted.write_bytes(b'z')
        self.store.seed(drifted, subject=['Margaret Hartley'])
        self._write_record(files_block=(
            f'files:\n  - file: documents/deeds/deed_{other_sid.lower()}.tif\n'
            '    role: primary\n'))
        result = self._run(keyword='Margaret Hartley')
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(self.store.write_calls, [])
        self.assertIn('fha reconcile', result.messages[-1].text)
        # The drifted file itself must be untouched too.
        self.assertEqual(self.store.read(drifted)['subject'], ['Margaret Hartley'])

    def test_write_success_is_verified_by_rereading_not_trusted_from_exit_code(self) -> None:
        # #147 review (P2): exiftool exiting 0 says the CALL succeeded, not
        # that the field ended up in the requested state - simulate a race
        # (something else touched the file between the pre-read and this
        # write) where the wrapper reports success but nothing changed.
        def _fake_write_no_op(path, *, remove, add, backup):
            backup.ensure(path)
            return None   # reports success without touching self.store at all
        source_mod._run_exiftool_edit_keyword_fields = _fake_write_no_op
        result = self._run(keyword='Margaret Hartley')
        self.assertEqual(result['status'], 'refused')
        self.assertFalse(result.ok)
        self.assertIn('re-reading', result.messages[-1].text)
        # Something DID write to the file (backup.ensure ran) even though the
        # requested change did not land - the caller should still know it.
        self.assertTrue(result.changed)

    def test_addition_that_did_not_land_is_also_caught(self) -> None:
        def _fake_write_only_removes(path, *, remove, add, backup):
            backup.ensure(path)
            f = self.store._fields(path)
            for tag, value in remove:
                f[tag] = [v for v in f[tag] if v != value]
            return None   # 'add' is silently dropped, as if the write raced
        source_mod._run_exiftool_edit_keyword_fields = _fake_write_only_removes
        result = self._run(keyword='Margaret Hartley', replace_with='Margaret Cole')
        self.assertEqual(result['status'], 'refused')
        self.assertFalse(result.ok)
        self.assertIn('is missing', result.messages[-1].text)

    def test_multiple_documents_files_require_dash_dash_file(self) -> None:
        second = self.root / 'documents' / 'deeds' / f'deed-back_{SID.lower()}.tif'
        second.write_bytes(b'y')
        self.store.seed(second, subject=[])   # the back scan carries no stray keyword
        self._write_record(files_block=(
            f'files:\n  - file: documents/deeds/deed_{SID.lower()}.tif\n    role: primary\n'
            f'  - file: documents/deeds/deed-back_{SID.lower()}.tif\n    role: back\n'))
        result = self._run(keyword='Margaret Hartley')
        self.assertEqual(result['status'], 'refused')
        self.assertIn('--file', result.messages[-1].text)

        # Picking the WRONG file (--file matches the back scan, which never
        # carried the keyword) must refuse rather than silently write to it.
        wrong = self._run(keyword='Margaret Hartley', file='deed-back_' + SID.lower() + '.tif')
        self.assertEqual(wrong['status'], 'refused')

        # --file disambiguates to the RIGHT file and the correction round-trips.
        result2 = self._run(keyword='Margaret Hartley', file='deed_' + SID.lower() + '.tif')
        self.assertEqual(result2['status'], 'ok')
        self.assertNotIn('Margaret Hartley', self.store.read(self.asset)['subject'])
        self.assertEqual(self.store.read(second)['subject'], [])   # untouched

    # -- safety-copy discipline (TOOLING §13f) --------------------------------

    def test_no_safety_copy_configured_warns_like_every_other_embedded_write(self) -> None:
        # No originals_backup: in fha.yaml (self.config) - the same "no safety
        # copies are being kept" warning tag-person/set-summary/process give.
        result = self._run(keyword='Margaret Hartley')
        self.assertEqual(result['status'], 'ok')
        warnings = [m.text for m in result.messages if m.level == 'warning']
        self.assertTrue(any('No safety copies' in w for w in warnings), result.messages)

    def test_configured_backup_is_actually_used(self) -> None:
        backup_dir = self.root.parent / 'backups'
        (self.root / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n  documents: documents\n'
            f'originals_backup: {backup_dir.as_posix()}\n', encoding='utf-8')
        cfg = load_fha_yaml(self.root)
        result = source_mod.run_source_clear_keyword(
            self.root, cfg, SID, keyword='Margaret Hartley')
        self.assertEqual(result['status'], 'ok')
        copy = backup_dir / 'documents' / 'deeds' / f'deed_{SID.lower()}.tif'
        self.assertTrue(copy.exists(), 'a pristine copy must be made before the write')


if __name__ == '__main__':
    unittest.main()
