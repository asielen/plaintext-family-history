"""`fha process` writes the ID into scanned records too (#252 item 4).

What the owner would notice: a scanned record (TIFF, JPEG, PNG or PDF) filed
in the documents root now carries its `SOURCE: S-…` keyword inside the file,
as a photo does - so if a sync tool or an export renames it, the file still
says which source it is. A format that cannot hold one (a recording, a text
transcript) is filed exactly as before, without a word about it.

Run: py -3.14 -m unittest tests.test_process_document_keyword -v
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
sys.path.insert(0, str(ROOT / 'tests'))

import process  # noqa: E402
from _lib import EXIT_CLEAN, EXIT_ERRORS, EXIT_FAILURE  # noqa: E402
from keyword_fixture_files import EXIFTOOL, read_fields, write_fixture  # noqa: E402


def _make_archive(tmp: Path) -> Path:
    archive = tmp / 'archive'
    for sub in ('documents/census', 'photos/1911', 'sources', 'people', 'notes'):
        (archive / sub).mkdir(parents=True)
    (archive / 'fha.yaml').write_text(
        'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8')
    return archive


class _Store:
    """In-memory keywords keyed by path, patched over process's exiftool seams."""

    def __init__(self) -> None:
        self.keywords: dict[str, list[str]] = {}
        self.embed_calls: list[Path] = []
        self.unavailable = False

    def read(self, path: Path) -> list[str]:
        return list(self.keywords.get(str(path), []))

    def embed(self, path, sid, extra_keywords=None, *, backup=None):
        if self.unavailable:
            raise process.ExiftoolUnavailableError(
                'fha process needs exiftool for the keywords inside photos and scans.')
        self.embed_calls.append(Path(path))
        self.keywords.setdefault(str(path), []).extend([f'SOURCE: {sid}'] + list(extra_keywords or []))
        with open(path, 'ab') as fh:   # a real write changes the bytes, as exiftool does
            fh.write(b'-KEYWORD')
        return None

    def remove(self, path, sid, extra_keywords=None, *, backup=None):
        drop = {f'SOURCE: {sid}'} | set(extra_keywords or [])
        self.keywords[str(path)] = [k for k in self.keywords.get(str(path), []) if k not in drop]
        return None


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.archive = _make_archive(self.tmp)
        self._saved = {name: getattr(process, name) for name in (
            '_run_exiftool_read_keywords', '_run_exiftool_embed_source',
            '_run_exiftool_remove_source', '_prompt')}

    def tearDown(self) -> None:
        for name, fn in self._saved.items():
            setattr(process, name, fn)
        self._tmp.cleanup()

    def _install_store(self) -> _Store:
        store = _Store()
        process._run_exiftool_read_keywords = store.read
        process._run_exiftool_embed_source = store.embed
        process._run_exiftool_remove_source = store.remove
        return store

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = process._standalone_main(argv + ['--root', str(self.archive)])
        return rc, out.getvalue(), err.getvalue()

    def _records(self, kind: str = 'census') -> list[Path]:
        return sorted((self.archive / 'sources' / kind).glob('*_S-*.md'))


class DocumentKeywordTests(_Base):
    """#252 item 4: a documents-root file that can hold a keyword gets one."""

    def test_each_metadata_format_gets_the_keyword_and_others_do_not(self) -> None:
        store = self._install_store()
        for name, expect in (('scan.tif', True), ('scan2.jpg', True), ('scan3.png', True),
                             ('will.pdf', True), ('talk.mp3', False), ('notes.md', False)):
            with self.subTest(name=name):
                store.embed_calls.clear()
                (self.archive / 'documents' / 'census' / name).write_bytes(b'x')
                rc, out, err = self._run([str(self.archive / 'documents' / 'census' / name),
                                          '--type', 'census'])
                self.assertEqual(rc, EXIT_CLEAN, err)
                renamed = list((self.archive / 'documents' / 'census').glob(
                    f'{Path(name).stem}_S-*{Path(name).suffix}'))
                self.assertEqual(len(renamed), 1)
                if expect:
                    self.assertEqual(store.embed_calls, renamed)
                    self.assertIn(f'Embedded SOURCE: ', out)
                else:
                    self.assertEqual(store.embed_calls, [])
                    self.assertNotIn('Embedded SOURCE', out)
                self.assertNotIn('WARNING', err)

    def test_dry_run_names_the_embed_and_writes_nothing(self) -> None:
        store = self._install_store()
        scan = self.archive / 'documents' / 'census' / 'scan.tif'
        scan.write_bytes(b'x')
        rc, out, _err = self._run([str(scan), '--type', 'census', '--dry-run'])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertRegex(out, r'Would embed SOURCE: S-\w+ in scan_S-\w+\.tif')
        self.assertEqual(store.embed_calls, [])
        self.assertTrue(scan.exists())
        self.assertEqual(self._records(), [])
        # The live run does what the preview said, to the same-named file.
        rc, live, _err = self._run([str(scan), '--type', 'census'])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertRegex(live, r'Embedded SOURCE: S-\w+ in scan_S-\w+\.tif')

    def test_without_exiftool_the_document_is_still_filed_with_a_plain_warning(self) -> None:
        store = self._install_store()
        store.unavailable = True
        scan = self.archive / 'documents' / 'census' / 'scan.pdf'
        scan.write_bytes(b'x')
        rc, out, err = self._run([str(scan), '--type', 'census'])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(len(self._records()), 1)
        self.assertIn('was filed without its SOURCE:', err)
        self.assertIn('Nothing is lost', err)
        self.assertNotIn('Traceback', err)

    def test_a_failed_record_write_takes_the_keyword_back_out(self) -> None:
        store = self._install_store()
        scan = self.archive / 'documents' / 'census' / 'scan.tif'
        scan.write_bytes(b'x')
        (self.archive / 'sources' / 'census').write_text('blocker', encoding='utf-8')
        rc, _out, err = self._run([str(scan), '--type', 'census'])
        self.assertEqual(rc, EXIT_FAILURE)
        self.assertTrue(scan.exists(), 'the rename is undone')
        self.assertEqual(len(store.embed_calls), 1, 'the keyword was written first')
        self.assertEqual(scan.read_bytes(), b'x', 'and the original is byte-for-byte back')
        self.assertIn('rolled back', err)

    def test_a_back_scan_beside_it_gets_the_keyword_too(self) -> None:
        store = self._install_store()
        folder = self.archive / 'documents' / 'census'
        (folder / 'sheet.jpg').write_bytes(b'x')
        (folder / 'sheet-back.jpg').write_bytes(b'x')
        rc, _out, err = self._run([str(folder / 'sheet.jpg'), '--type', 'census'])
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertEqual(sorted(p.name.split('_S-')[0] for p in store.embed_calls),
                         ['sheet', 'sheet-back'])

    def test_a_bundle_document_gets_the_keyword(self) -> None:
        store = self._install_store()
        bundle = self.archive / 'inbox' / 'hartley-letter'
        bundle.mkdir(parents=True)
        (bundle / 'notes.md').write_text('---\nsource_type: letter\n---\nA letter.\n',
                                         encoding='utf-8')
        (bundle / 'letter.pdf').write_bytes(b'x')
        (bundle / 'letter-transcript.md').write_text('Dear Ada', encoding='utf-8')
        rc, out, err = self._run([str(bundle)])
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertEqual([p.suffix for p in store.embed_calls], ['.pdf'])
        self.assertIn('SOURCE: ', out)

    def test_a_renamed_processed_scan_is_not_given_a_second_id(self) -> None:
        # The day the keyword earns its keep: a sync tool renamed the scan, so
        # its filename no longer says which source it is - the keyword does.
        store = self._install_store()
        scan = self.archive / 'documents' / 'census' / 'renamed-by-dropbox.tif'
        scan.write_bytes(b'x')
        store.keywords[str(scan)] = ['SOURCE: S-a1b2c3d4e5']
        rc, _out, err = self._run([str(scan), '--type', 'census'])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('already carries SOURCE: S-a1b2c3d4e5', err)
        self.assertIn('fha find S-a1b2c3d4e5', err)
        self.assertTrue(scan.exists())
        self.assertEqual(self._records(), [])

    def test_a_renamed_photo_from_the_library_is_pointed_at_refile(self) -> None:
        # #253 review: a processed photo moved by hand to be filed as a record
        # used to be told only to "rename it back".
        store = self._install_store()
        sid = 'S-a1b2c3d4e5'
        (self.archive / 'sources' / 'photos').mkdir(parents=True, exist_ok=True)
        (self.archive / 'sources' / 'photos' / f'postcard_{sid}.md').write_text(
            f'---\nid: {sid}\ntitle: Postcard\nsource_type: photo\nfiles:\n'
            '  - file: photos/1911/postcard.jpg\n    role: primary\n---\n', encoding='utf-8')
        scan = self.archive / 'documents' / 'census' / 'postcard.jpg'
        scan.write_bytes(b'x')
        store.keywords[str(scan)] = [f'SOURCE: {sid}']
        rc, _out, err = self._run([str(scan), '--type', 'letter'])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn(f'fha process refile {sid} --to documents --type letter', err)

    def test_more_refuses_a_scan_that_belongs_to_another_source(self) -> None:
        # #253 review / Codex: the main path refused this renamed scan, but
        # --more stamped a second SOURCE: into it and listed it under a second
        # source.
        store = self._install_store()
        folder = self.archive / 'documents' / 'census'
        (folder / 'will.pdf').write_bytes(b'x')
        rc, _out, err = self._run([str(folder / 'will.pdf'), '--type', 'census'])
        self.assertEqual(rc, EXIT_CLEAN, err)
        primary = next(folder.glob('will_S-*.pdf'))
        stray = folder / 'census 1910 (synced).tif'
        stray.write_bytes(b'x')
        store.keywords[str(stray)] = ['SOURCE: S-a1b2c3d4e5']
        store.embed_calls.clear()
        rc, _out, err = self._run([str(primary), '--more', str(stray), 'page'])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('already carries SOURCE: S-a1b2c3d4e5', err)
        self.assertIn('two sources', err)
        self.assertTrue(stray.exists(), 'not renamed')
        self.assertEqual(store.embed_calls, [])

    def test_a_bundle_refuses_a_document_that_belongs_to_another_source(self) -> None:
        store = self._install_store()
        bundle = self.archive / 'inbox' / 'aunt-ruth'
        bundle.mkdir(parents=True)
        (bundle / 'notes.md').write_text('---\nsource_type: letter\n---\nLetters.\n',
                                         encoding='utf-8')
        (bundle / 'letter.pdf').write_bytes(b'x')
        store.keywords[str(bundle / 'letter.pdf')] = ['SOURCE: S-a1b2c3d4e5']
        rc, _out, err = self._run([str(bundle)])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('already carries SOURCE: S-a1b2c3d4e5', err)
        self.assertTrue((bundle / 'letter.pdf').exists())
        self.assertEqual(store.embed_calls, [])

    def test_the_back_scan_beside_it_is_checked_too(self) -> None:
        # Codex 4521910862: record.jpg + record-back.jpg - the back is pulled in
        # automatically, and a back already belonging to another source used to
        # get a second SOURCE: keyword (exit 0).
        store = self._install_store()
        folder = self.archive / 'documents' / 'census'
        (folder / 'record.jpg').write_bytes(b'x')
        (folder / 'record-back.jpg').write_bytes(b'x')
        store.keywords[str(folder / 'record-back.jpg')] = ['SOURCE: S-a1b2c3d4e5']
        for extra in ([], ['--dry-run']):
            with self.subTest(dry_run=bool(extra)):
                rc, _out, err = self._run([str(folder / 'record.jpg'), '--type', 'census'] + extra)
                self.assertEqual(rc, EXIT_ERRORS)
                self.assertIn('record-back.jpg (the back scan beside it) already carries '
                              'SOURCE: S-a1b2c3d4e5', err)
        self.assertTrue((folder / 'record.jpg').exists())
        self.assertTrue((folder / 'record-back.jpg').exists())
        self.assertEqual(store.embed_calls, [])
        self.assertEqual(self._records(), [])

    def test_the_back_scan_in_the_inbox_is_checked_before_anything_moves(self) -> None:
        store = self._install_store()
        inbox = self.archive / 'inbox'
        inbox.mkdir()
        (inbox / 'record.jpg').write_bytes(b'x')
        (inbox / 'record-back.jpg').write_bytes(b'x')
        store.keywords[str(inbox / 'record-back.jpg')] = ['SOURCE: S-a1b2c3d4e5']
        rc, _out, err = self._run([str(inbox / 'record.jpg'), '--type', 'census'])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('already carries SOURCE: S-a1b2c3d4e5', err)
        self.assertTrue((inbox / 'record.jpg').exists(), 'the primary went back to the inbox')
        self.assertTrue((inbox / 'record-back.jpg').exists())
        self.assertEqual(store.embed_calls, [])

    def test_ctrl_c_after_the_keyword_leaves_no_undo_copy_behind(self) -> None:
        import tempfile as _tempfile
        store = self._install_store()
        scan = self.archive / 'documents' / 'census' / 'will.pdf'
        scan.write_bytes(b'x')
        before = set(Path(_tempfile.gettempdir()).glob('fha-undo-*'))
        real_mkdir = Path.mkdir

        def _interrupt(self_path, *a, **k):
            if self_path.name == 'census' and 'sources' in self_path.parts:
                raise KeyboardInterrupt
            return real_mkdir(self_path, *a, **k)
        Path.mkdir = _interrupt
        try:
            with self.assertRaises(KeyboardInterrupt):
                self._run([str(scan), '--type', 'census'])
        finally:
            Path.mkdir = real_mkdir
        self.assertEqual(len(store.embed_calls), 1, 'the keyword had been written')
        self.assertEqual(set(Path(_tempfile.gettempdir()).glob('fha-undo-*')) - before, set())

    def test_refile_into_the_photo_library_checks_the_files_own_keyword(self) -> None:
        # The sweep: refile ends with a SOURCE: write into the moved file, so a
        # different ID already inside it must refuse before anything moves.
        store = self._install_store()
        sid = 'S-b2c3d4e5f6'
        folder = self.archive / 'documents' / 'census'
        scan = folder / f'portrait_{sid}.jpg'
        scan.write_bytes(b'x')
        (self.archive / 'sources' / 'census').mkdir(parents=True, exist_ok=True)
        (self.archive / 'sources' / 'census' / f'portrait_{sid}.md').write_text(
            f'---\nid: {sid}\ntitle: Portrait\nsource_type: census\nfiles:\n'
            f'  - file: documents/census/portrait_{sid}.jpg\n    role: primary\n---\n',
            encoding='utf-8')
        store.keywords[str(scan)] = ['SOURCE: S-a1b2c3d4e5']
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = process._standalone_main(['refile', sid, '--to', 'photos', '--dest', '1911',
                                           '--type', 'photo', '--root', str(self.archive)])
        self.assertNotEqual(rc, EXIT_CLEAN)
        self.assertIn('already carries SOURCE: S-a1b2c3d4e5', err.getvalue())
        self.assertTrue(scan.exists(), 'nothing moved')
        self.assertEqual(store.embed_calls, [])

    def test_an_attached_document_scan_gets_the_keyword(self) -> None:
        store = self._install_store()
        folder = self.archive / 'documents' / 'census'
        (folder / 'will.pdf').write_bytes(b'x')
        rc, _out, err = self._run([str(folder / 'will.pdf'), '--type', 'census'])
        self.assertEqual(rc, EXIT_CLEAN, err)
        primary = next(folder.glob('will_S-*.pdf'))
        (folder / 'codicil.tif').write_bytes(b'x')
        store.embed_calls.clear()
        rc, out, err = self._run([str(primary), '--more', str(folder / 'codicil.tif'), 'codicil'])
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertEqual(len(store.embed_calls), 1)
        self.assertTrue(store.embed_calls[0].name.startswith('codicil-codicil_S-'))


@unittest.skipUnless(EXIFTOOL, 'exiftool is not installed - these run on a machine that has it')
class RealDocumentKeywordTests(_Base):
    """The same, with the real exiftool writing real files."""

    def test_tiff_jpeg_png_and_pdf_carry_the_keyword_after_processing(self) -> None:
        for name, fields in (('scan.tif', 3), ('photo-of-will.jpg', 3),
                             ('chart.png', 2), ('will.pdf', 2)):
            with self.subTest(name=name):
                write_fixture(self.archive / 'documents' / 'census' / name)
                rc, out, err = self._run([str(self.archive / 'documents' / 'census' / name),
                                          '--type', 'census'])
                self.assertEqual(rc, EXIT_CLEAN, err)
                self.assertNotIn('WARNING', err)
                renamed = next((self.archive / 'documents' / 'census').glob(
                    f'{Path(name).stem}_S-*{Path(name).suffix}'))
                sid = renamed.stem.rsplit('_', 1)[1]
                got = read_fields(renamed)
                holding = [tag for tag, vals in got.items() if f'SOURCE: {sid}' in vals]
                self.assertEqual(len(holding), fields, got)
                # E012's reader agrees: the identity carrier is readable back.
                self.assertEqual(process._read_all_source_keywords(renamed), [sid.lower()])

    def test_a_scan_renamed_after_processing_is_recognised(self) -> None:
        scan = write_fixture(self.archive / 'documents' / 'census' / 'sheet.tif')
        rc, _out, err = self._run([str(scan), '--type', 'census'])
        self.assertEqual(rc, EXIT_CLEAN, err)
        processed = next((self.archive / 'documents' / 'census').glob('sheet_S-*.tif'))
        renamed = processed.with_name('sheet (1).tif')
        processed.rename(renamed)
        rc, _out, err = self._run([str(renamed), '--type', 'census'])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('already carries SOURCE:', err)
        self.assertEqual(len(self._records()), 1)

    def test_a_rolled_back_run_leaves_every_format_byte_for_byte_as_it_was(self) -> None:
        # #253 review: removing the keyword again does not restore a PDF -
        # exiftool appends an update to it - so a rollback restores the bytes.
        (self.archive / 'sources' / 'letter').write_text('blocker', encoding='utf-8')
        for name in ('will.pdf', 'will.tif', 'will.jpg', 'will.png'):
            with self.subTest(name=name):
                scan = write_fixture(self.archive / 'documents' / 'census' / name)
                before = scan.read_bytes()
                rc, _out, err = self._run([str(scan), '--type', 'letter'])
                self.assertEqual(rc, EXIT_FAILURE, err)
                self.assertEqual(scan.read_bytes(), before)

    def test_more_rollback_keeps_a_matching_keyword_the_file_already_had(self) -> None:
        # Codex: a scan that already carries THIS source's keyword keeps it
        # when a failed --more is undone.
        folder = self.archive / 'documents' / 'census'
        primary = write_fixture(folder / 'will.tif')
        rc, _out, err = self._run([str(primary), '--type', 'census'])
        self.assertEqual(rc, EXIT_CLEAN, err)
        primary = next(folder.glob('will_S-*.tif'))
        sid = primary.stem.rsplit('_', 1)[1]
        page = write_fixture(folder / 'will page 2.tif')
        import subprocess
        subprocess.run(['exiftool', '-q', '-overwrite_original',
                        f'-XMP-dc:Subject=SOURCE: {sid}', str(page)], check=True)
        before = page.read_bytes()
        real_write = process.write_text_exact_atomic

        def _fail(*_a, **_k):
            raise OSError('disk full')
        process.write_text_exact_atomic = _fail
        try:
            rc, _out, err = self._run([str(primary), '--more', str(page), 'page'])
        finally:
            process.write_text_exact_atomic = real_write
        self.assertEqual(rc, EXIT_FAILURE, err)
        self.assertEqual(page.read_bytes(), before)
        self.assertEqual(process._read_all_source_keywords(page), [sid.lower()])

    def test_this_sources_keyword_in_other_capitals_is_not_doubled(self) -> None:
        folder = self.archive / 'documents' / 'census'
        primary = write_fixture(folder / 'will.tif')
        rc, _out, err = self._run([str(primary), '--type', 'census'])
        self.assertEqual(rc, EXIT_CLEAN, err)
        primary = next(folder.glob('will_S-*.tif'))
        sid = primary.stem.rsplit('_', 1)[1]
        page = write_fixture(folder / 'page3.tif')
        import subprocess
        subprocess.run(['exiftool', '-q', '-overwrite_original',
                        f'-XMP-dc:Subject=SOURCE: {sid.upper()}', str(page)], check=True)
        rc, _out, err = self._run([str(primary), '--more', str(page), 'page'])
        self.assertEqual(rc, EXIT_CLEAN, err)
        attached = next(folder.glob('page3-page_S-*.tif'))
        for tag, values in read_fields(attached).items():
            self.assertEqual(values, [f'SOURCE: {sid}'], tag)

    def test_a_document_exiftool_cannot_update_is_still_filed(self) -> None:
        bad = self.archive / 'documents' / 'census' / 'locked.pdf'
        bad.write_bytes(b'%PDF-1.4 not really a pdf')
        rc, _out, err = self._run([str(bad), '--type', 'census'])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(len(self._records()), 1)
        self.assertIn('was filed without its SOURCE:', err)


if __name__ == '__main__':
    unittest.main()
