"""
test_source_extract.py - fha source extract (BUILD M11.5, TOOLING §3d).

The fixture PDFs are hand-rolled minimal documents (catalog/pages/font +
one content stream per page) rather than binary blobs, so the text layer
each test expects is visible right here in the source. pypdf reads them the
same way it reads an archive.org scan's embedded layer. Tests skip cleanly
when pypdf is not installed (it is an optional dependency by design - the
one behavior tested WITHOUT it is the plain install-message refusal).
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import source as source_mod
from _lib import EXIT_CLEAN, EXIT_FAILURE, EXIT_WARNINGS, load_fha_yaml, read_record

try:
    import pypdf  # noqa: F401 - availability probe only
    HAVE_PYPDF = True
except ImportError:
    HAVE_PYPDF = False

SID = 'S-2b3c4d5e6f'


def _minimal_pdf(page_texts: list) -> bytes:
    """Build a tiny valid PDF: one page per entry, None = no text layer."""
    n_pages = len(page_texts)
    page_nums, content_nums = [], []
    next_num = 4
    for _ in page_texts:
        page_nums.append(next_num)
        content_nums.append(next_num + 1)
        next_num += 2
    kids = ' '.join(f'{n} 0 R' for n in page_nums)
    objs = {
        1: '<< /Type /Catalog /Pages 2 0 R >>',
        2: f'<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>',
        3: '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    }
    for i, text in enumerate(page_texts):
        pn, cn = page_nums[i], content_nums[i]
        objs[pn] = ('<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
                    f'/Resources << /Font << /F1 3 0 R >> >> /Contents {cn} 0 R >>')
        stream = f'BT /F1 12 Tf 72 720 Td ({text}) Tj ET' if text else ''
        objs[cn] = f'<< /Length {len(stream)} >>\nstream\n{stream}\nendstream'
    out = b'%PDF-1.4\n'
    offsets = {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += f'{num} 0 obj\n{objs[num]}\nendobj\n'.encode('latin-1')
    xref_pos = len(out)
    count = max(objs) + 1
    out += f'xref\n0 {count}\n'.encode()
    out += b'0000000000 65535 f \n'
    for num in range(1, count):
        out += f'{offsets[num]:010d} 00000 n \n'.encode()
    out += (f'trailer\n<< /Size {count} /Root 1 0 R >>\n'
            f'startxref\n{xref_pos}\n%%EOF\n').encode()
    return out


class SourceExtractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8')
        (self.root / 'documents' / 'book').mkdir(parents=True)
        (self.root / 'sources' / 'book').mkdir(parents=True)
        self.pdf = self.root / 'documents' / 'book' / f'county-history_{SID.lower()}.pdf'
        self.record = self.root / 'sources' / 'book' / f'county-history_{SID.lower()}.md'
        self._write_record()
        self.config = load_fha_yaml(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_record(self, files_block: str | None = None) -> None:
        if files_block is None:
            files_block = (f'files:\n  - file: documents/book/county-history_{SID.lower()}.pdf\n'
                           '    role: primary\n')
        self.record.write_text(
            f'---\nid: {SID}\ntitle: County History\nsource_type: book\n'
            f'{files_block}---\n\n## Notes\nA fat county history.\n',
            encoding='utf-8')

    def _run(self, **kw):
        return source_mod.run_source_extract(self.root, self.config, SID, **kw)

    def test_malformed_config_with_external_root_refuses_not_degrades(self) -> None:
        # P2 codex finding (round 8): `_cmd_source_extract` loaded fha.yaml
        # permissively, so a malformed config silently degraded to {} and
        # discarded external document-root mappings - then resolved the PDF alias
        # against the internal documents/ skeleton and could extract an unrelated
        # same-named PDF's text against this source. With strict=True it refuses.
        import argparse
        import contextlib
        import io
        # Maps documents to an EXTERNAL root, but the YAML is malformed
        # (inconsistent indentation) - strict load must raise, not degrade to {}.
        (self.root / 'fha.yaml').write_text(
            'roots:\n  documents: /mnt/external/docs\n   photos: photos\n',
            encoding='utf-8')
        args = argparse.Namespace(root=str(self.root), source_id=SID,
                                  pages=None, dry_run=False)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = source_mod._cmd_source_extract(args)
        self.assertEqual(code, EXIT_FAILURE)
        self.assertTrue(err.getvalue().strip(), 'the config problem must be named')

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_extracts_text_pages_and_appends_derived_entry(self) -> None:
        self.pdf.write_bytes(_minimal_pdf(['Hartley arrived 1854', 'He farmed Marsh Creek']))
        result = self._run()
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result['status'], 'ok')
        out = self.pdf.parent / f'county-history-extracted-text_{SID}.md'
        self.assertTrue(out.exists())
        text = out.read_text(encoding='utf-8')
        self.assertIn('[Page 1]', text)
        self.assertIn('Hartley arrived 1854', text)
        self.assertIn('[Page 2]', text)
        self.assertIn('He farmed Marsh Creek', text)
        entry = read_record(self.record)['meta']['files'][-1]
        self.assertEqual(entry['role'], 'extracted-text')
        self.assertIn(entry['derived'], (True, 'true'))
        self.assertEqual(entry['file'],
                         f'documents/book/county-history-extracted-text_{SID}.md')
        # the original is byte-untouched
        self.assertEqual(self.pdf.read_bytes(), _minimal_pdf(
            ['Hartley arrived 1854', 'He farmed Marsh Creek']))

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_textless_page_gets_placeholder_never_silence(self) -> None:
        self.pdf.write_bytes(_minimal_pdf(['Text here', None, 'More text']))
        result = self._run()
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result['pages_with_text'], 2)
        out = self.pdf.parent / f'county-history-extracted-text_{SID}.md'
        text = out.read_text(encoding='utf-8')
        self.assertIn('[Page 2]\n(no text layer on this page - read it with vision)', text)
        self.assertTrue(any('no text layer on: 2' in m.text for m in result.messages))

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_all_image_pdf_refuses_writing_nothing(self) -> None:
        self.pdf.write_bytes(_minimal_pdf([None, None]))
        result = self._run()
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result['status'], 'no-text')
        self.assertFalse(
            (self.pdf.parent / f'county-history-extracted-text_{SID}.md').exists())
        self.assertTrue(any('scanned-image' in m.text for m in result.messages))
        # the record's inventory gained nothing
        self.assertEqual(len(read_record(self.record)['meta']['files']), 1)

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_page_extraction_error_marked_failed_not_empty(self) -> None:
        # A pypdf error on ONE page must not be recorded as "no text layer":
        # that would make a parser failure look like confirmed absence. The good
        # page's text is still written, the failed page is marked distinctly, and
        # the run exits nonzero naming the page.
        import pypdf
        self.pdf.write_bytes(_minimal_pdf(['Good page one', 'Good page two']))
        real_cls = pypdf.PdfReader

        class _Page:
            def __init__(self, real, fail):
                self._real, self._fail = real, fail

            def extract_text(self, *a, **k):
                if self._fail:
                    raise ValueError('pypdf boom on this page')
                return self._real.extract_text(*a, **k)

        class _Reader:
            def __init__(self, *a, **k):
                pages = list(real_cls(*a, **k).pages)
                self.pages = [_Page(pages[0], False), _Page(pages[1], True)]

        pypdf.PdfReader = _Reader
        try:
            result = self._run()
        finally:
            pypdf.PdfReader = real_cls

        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result.data.get('error_pages'), [2])
        out = self.pdf.parent / f'county-history-extracted-text_{SID}.md'
        self.assertTrue(out.exists())                    # the good page is kept
        text = out.read_text(encoding='utf-8')
        self.assertIn('Good page one', text)
        self.assertIn('extraction FAILED', text)         # distinct error marker
        self.assertNotIn('[Page 2]\n(no text layer', text)   # NOT mislabeled empty
        self.assertTrue(any('FAILED on page' in m.text for m in result.messages))

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_all_pages_error_refuses_not_confirmed_absence(self) -> None:
        # Every page errors: this is a parser failure, NOT a scanned-image PDF.
        # Refuse writing anything rather than emit a dump that reads as "no text
        # on any page" (which would send the human away from real evidence).
        import pypdf
        self.pdf.write_bytes(_minimal_pdf(['x', 'y']))
        real_cls = pypdf.PdfReader

        class _Reader:
            def __init__(self, *a, **k):
                n = len(list(real_cls(*a, **k).pages))

                class _P:
                    def extract_text(self, *a, **k):
                        raise ValueError('pypdf boom')
                self.pages = [_P() for _ in range(n)]

        pypdf.PdfReader = _Reader
        try:
            result = self._run()
        finally:
            pypdf.PdfReader = real_cls

        self.assertEqual(result['status'], 'extract-error')
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertFalse(
            (self.pdf.parent / f'county-history-extracted-text_{SID}.md').exists())
        self.assertTrue(any('FAILED on every selected' in m.text for m in result.messages))
        self.assertFalse(any('scanned-image' in m.text for m in result.messages))
        self.assertEqual(len(read_record(self.record)['meta']['files']), 1)

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_pages_subset_and_out_of_range(self) -> None:
        self.pdf.write_bytes(_minimal_pdf(['One', 'Two', 'Three']))
        # Out-of-range first (nothing extracted yet): names the real count.
        bad = self._run(pages='2-9')
        self.assertNotEqual(bad.exit_code, EXIT_CLEAN)
        self.assertTrue(any('only 3 page(s)' in m.text for m in bad.messages))
        # Then a valid subset extracts just those pages.
        result = self._run(pages='2-3')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        out = self.pdf.parent / f'county-history-extracted-text_{SID}.md'
        text = out.read_text(encoding='utf-8')
        self.assertNotIn('[Page 1]', text)
        self.assertIn('[Page 2]', text)
        self.assertIn('[Page 3]', text)

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_bad_page_spec_is_a_plain_refusal(self) -> None:
        self.pdf.write_bytes(_minimal_pdf(['One']))
        result = self._run(pages='chapter two')
        self.assertNotEqual(result.exit_code, EXIT_CLEAN)
        self.assertTrue(any('1-60' in m.text for m in result.messages))

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_dry_run_writes_nothing(self) -> None:
        self.pdf.write_bytes(_minimal_pdf(['Some text']))
        before = self.record.read_text(encoding='utf-8')
        result = self._run(dry_run=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result['status'], 'dry-run')
        self.assertFalse(
            (self.pdf.parent / f'county-history-extracted-text_{SID}.md').exists())
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertTrue(any('[dry-run]' in m.text for m in result.messages))

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_second_run_is_a_clean_already_no_op(self) -> None:
        self.pdf.write_bytes(_minimal_pdf(['Some text']))
        self.assertEqual(self._run().exit_code, EXIT_CLEAN)
        again = self._run()
        self.assertEqual(again.exit_code, EXIT_CLEAN)
        self.assertEqual(again['status'], 'already')
        self.assertTrue(any('never overwrites' in m.text for m in again.messages))

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_no_pdf_in_inventory_refuses_plainly(self) -> None:
        self._write_record(
            f'files:\n  - file: documents/book/scan_{SID.lower()}.jpg\n    role: primary\n')
        result = self._run()
        self.assertNotEqual(result.exit_code, EXIT_CLEAN)
        self.assertTrue(any('no PDF' in m.text for m in result.messages))

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_two_pdfs_without_primary_refuse_never_guess(self) -> None:
        self._write_record(
            f'files:\n  - file: documents/book/a_{SID.lower()}.pdf\n    role: page-1\n'
            f'  - file: documents/book/b_{SID.lower()}.pdf\n    role: page-2\n')
        result = self._run()
        self.assertNotEqual(result.exit_code, EXIT_CLEAN)
        self.assertTrue(any('more than one PDF' in m.text for m in result.messages))

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_two_primary_pdfs_refuse_never_guess(self) -> None:
        # Two non-derived PDFs both marked role: primary is a hand-edit mistake.
        # Taking the first would risk attaching text from the WRONG PDF, so the
        # tool must refuse and name both files rather than guess.
        self._write_record(
            f'files:\n  - file: documents/book/a_{SID.lower()}.pdf\n    role: primary\n'
            f'  - file: documents/book/b_{SID.lower()}.pdf\n    role: primary\n')
        (self.root / 'documents' / 'book' / f'a_{SID.lower()}.pdf').write_bytes(
            _minimal_pdf(['A text']))
        (self.root / 'documents' / 'book' / f'b_{SID.lower()}.pdf').write_bytes(
            _minimal_pdf(['B text']))
        result = self._run()
        self.assertNotEqual(result.exit_code, EXIT_CLEAN)
        self.assertTrue(any('more than one PDF' in m.text
                            and 'role: primary' in m.text for m in result.messages))
        self.assertTrue(any(f'a_{SID.lower()}.pdf' in m.text for m in result.messages))
        self.assertTrue(any(f'b_{SID.lower()}.pdf' in m.text for m in result.messages))
        # Nothing was written: no dump, no inventory growth.
        self.assertEqual(len(read_record(self.record)['meta']['files']), 2)

    def test_working_copy_refuses_before_pypdf_check(self) -> None:
        # On a working copy the PDF is not present, so "install pypdf" is a dead
        # end - the actionable answer is "run on the main archive." That guard
        # must win even when pypdf is absent, so simulate both here.
        (self.root / 'WORKING_COPY').write_text('', encoding='utf-8')
        saved = sys.modules.get('pypdf')
        sys.modules['pypdf'] = None
        try:
            result = self._run()
        finally:
            if saved is not None:
                sys.modules['pypdf'] = saved
            else:
                sys.modules.pop('pypdf', None)
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertTrue(any('working copy' in m.text and 'main archive' in m.text
                            for m in result.messages))
        self.assertFalse(any('pip install pypdf' in m.text for m in result.messages))

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_crlf_record_gains_no_mixed_line_endings(self) -> None:
        # The inventory append must carry the record's OWN line ending - a
        # CRLF-authored record previously came back with a bare-LF island
        # exactly where the entry landed.
        crlf_text = self.record.read_text(encoding='utf-8').replace('\n', '\r\n')
        self.record.write_bytes(crlf_text.encode('utf-8'))
        self.pdf.write_bytes(_minimal_pdf(['Some text']))
        result = self._run()
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        raw = self.record.read_bytes().decode('utf-8')
        bare_lf = [ln for ln in raw.split('\r\n') if '\n' in ln]
        self.assertEqual(bare_lf, [], f'mixed endings: {bare_lf!r}')
        entry = read_record(self.record)['meta']['files'][-1]
        self.assertEqual(entry['role'], 'extracted-text')

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_record_write_failure_restores_the_record(self) -> None:
        # The record write is atomic (write_text_exact_atomic), so a failed
        # forward write leaves the record untouched; the rollback still restores
        # the pristine text and deletes the dump, then claims a clean rollback.
        # The failure is scoped to the RECORD path (the dump write, also atomic,
        # must succeed) and to its FIRST write (the restore must land).
        self.pdf.write_bytes(_minimal_pdf(['Some text']))
        before = self.record.read_bytes()
        real_write = source_mod.write_text_exact_atomic
        record_writes = {'n': 0}

        def failing_write(path, text):
            if Path(path).name == self.record.name:
                record_writes['n'] += 1
                if record_writes['n'] == 1:
                    raise OSError('simulated disk full')     # forward record write
            return real_write(path, text)

        source_mod.write_text_exact_atomic = failing_write
        try:
            result = self._run()
        finally:
            source_mod.write_text_exact_atomic = real_write
        self.assertNotEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(self.record.read_bytes(), before)      # restored/untouched
        self.assertFalse(
            (self.pdf.parent / f'county-history-extracted-text_{SID}.md').exists())
        self.assertTrue(any('rolled back' in m.text for m in result.messages))

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_dump_write_failure_leaves_no_partial_to_block_retry(self) -> None:
        # The initial dump write goes through write_text_exact_atomic, so a
        # mid-write failure (the disk fills as os.replace lands the temp) must
        # leave NO extract_path behind. Otherwise the existence guard above the
        # write would refuse the retry the failure message tells the user to
        # make, stranding them on an incomplete, uninventoried file they would
        # have to hunt down and delete by hand. Fail the REAL helper's os.replace
        # so its own cleanup runs exactly as run_source_extract invokes it.
        import _lib
        self.pdf.write_bytes(_minimal_pdf(['Some text']))
        record_before = self.record.read_bytes()
        out = self.pdf.parent / f'county-history-extracted-text_{SID}.md'
        real_replace = _lib.os.replace

        def failing_replace(src, dst, *a, **k):
            if Path(dst) == out:
                raise OSError('simulated disk full')
            return real_replace(src, dst, *a, **k)

        _lib.os.replace = failing_replace
        try:
            result = self._run()
        finally:
            _lib.os.replace = real_replace
        # A plain failure, not a traceback.
        self.assertNotEqual(result.exit_code, EXIT_CLEAN)
        self.assertTrue(any('could not write' in m.text for m in result.messages))
        # No partial dump survives, so the existence guard cannot block a retry.
        self.assertFalse(out.exists())
        # No stray temp file left in the folder either.
        strays = list(out.parent.glob('.*.tmp'))
        self.assertEqual(strays, [], f'stray temp files left behind: {strays}')
        # The dump write failed before the record edit, so the record is intact
        # and its inventory gained nothing.
        self.assertEqual(self.record.read_bytes(), record_before)
        self.assertEqual(len(read_record(self.record)['meta']['files']), 1)

    def test_missing_pypdf_refuses_with_install_command(self) -> None:
        self.pdf.write_bytes(b'%PDF-1.4\n%%EOF\n')
        saved = sys.modules.get('pypdf')
        sys.modules['pypdf'] = None
        try:
            result = self._run()
        finally:
            if saved is not None:
                sys.modules['pypdf'] = saved
            else:
                sys.modules.pop('pypdf', None)
        self.assertNotEqual(result.exit_code, EXIT_CLEAN)
        self.assertTrue(any('pip install pypdf' in m.text for m in result.messages))

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_windows_alias_entry_normalizes_to_forward_slash_documents(self) -> None:
        # A stored alias written with Windows separators must still produce a
        # 'documents/.../<name>' entry, never a bare filename. On POSIX,
        # Path('documents\\book\\x.pdf').parent is '.', which would collapse the
        # dump's entry to just the filename and send indexing to the wrong place.
        self._write_record(
            f'files:\n  - file: documents\\book\\county-history_{SID.lower()}.pdf\n'
            '    role: primary\n')
        self.pdf.write_bytes(_minimal_pdf(['Hartley arrived 1854']))
        result = self._run()
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        entry = read_record(self.record)['meta']['files'][-1]
        self.assertEqual(entry['role'], 'extracted-text')
        self.assertEqual(entry['file'],
                         f'documents/book/county-history-extracted-text_{SID}.md')

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_malformed_yaml_record_refuses_naming_lint_not_no_pdf(self) -> None:
        # read_record reports malformed frontmatter through parse_errors and
        # hands back partial meta; the tool must refuse on that, naming the
        # record and `fha lint`, rather than falling through to the misleading
        # "lists no PDF" refusal built from the half-read inventory.
        self.pdf.write_bytes(_minimal_pdf(['Hartley arrived 1854']))
        self.record.write_text(
            f'---\nid: {SID}\ntitle: County History\nsource_type: book\n'
            'files: [broken\n---\n\n## Notes\nDamaged frontmatter.\n',
            encoding='utf-8')
        result = self._run()
        self.assertNotEqual(result.exit_code, EXIT_CLEAN)
        joined = ' '.join(m.text for m in result.messages)
        self.assertIn('malformed YAML', joined)
        self.assertIn('fha lint', joined)
        self.assertNotIn('lists no PDF', joined)


if __name__ == '__main__':
    unittest.main()
