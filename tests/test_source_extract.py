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
from _lib import EXIT_CLEAN, EXIT_WARNINGS, load_fha_yaml, read_record

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
        # write_text_exact truncates before writing, so a mid-write failure
        # can destroy the record - the rollback must restore the pristine
        # text, and only then claim everything was rolled back.
        self.pdf.write_bytes(_minimal_pdf(['Some text']))
        before = self.record.read_bytes()
        real_write = source_mod.write_text_exact
        calls = {'n': 0}

        def failing_write(path, text):
            calls['n'] += 1
            if calls['n'] == 1:
                # Simulate a truncating write that dies partway.
                Path(path).write_text(text[:20], encoding='utf-8')
                raise OSError('simulated disk full')
            return real_write(path, text)

        source_mod.write_text_exact = failing_write
        try:
            result = self._run()
        finally:
            source_mod.write_text_exact = real_write
        self.assertNotEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(self.record.read_bytes(), before)      # restored
        self.assertFalse(
            (self.pdf.parent / f'county-history-extracted-text_{SID}.md').exists())
        self.assertTrue(any('rolled back' in m.text for m in result.messages))

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


if __name__ == '__main__':
    unittest.main()
