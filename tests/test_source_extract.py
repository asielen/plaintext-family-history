"""
test_source_extract.py - fha source extract (BUILD M11.5, TOOLING §3d).

The fixture PDFs are hand-rolled minimal documents (catalog/pages/font +
one content stream per page) rather than binary blobs, so the text layer
each test expects is visible right here in the source. pypdf reads them the
same way it reads an archive.org scan's embedded layer. Tests skip cleanly
when pypdf is not installed (it is an optional dependency by design - the
one behavior tested WITHOUT it is the plain install-message refusal).
"""

import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock
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


def _raw(stream: str) -> dict:
    """A _minimal_pdf page given as its raw content stream, operators and all."""
    return {'stream': stream}


def _minimal_pdf(page_texts: list, base_font: str = 'Helvetica') -> bytes:
    """Build a tiny valid PDF: one page per entry, None = no text layer.

    An entry is a string (one line of text), None (a page with no text
    layer), or `_raw(stream)` - a page whose content stream is written out
    exactly as given, for text layers built the odd ways real OCR does.
    """
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
        3: f'<< /Type /Font /Subtype /Type1 /BaseFont /{base_font} >>',
    }
    for i, text in enumerate(page_texts):
        pn, cn = page_nums[i], content_nums[i]
        objs[pn] = ('<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
                    f'/Resources << /Font << /F1 3 0 R >> >> /Contents {cn} 0 R >>')
        if isinstance(text, dict):
            stream = text['stream']
        else:
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


# #281: an OCR-style text layer, the way some scanned books carry one. Each
# word is placed on its own with Tm and squeezed to its word box with
# horizontal scaling (60 Tz), in invisible render mode (3 Tr), and there is no
# space character anywhere. Each x is the previous word's Times-Roman width
# x 0.6, plus a 4 pt gap. pypdf's default mode reads this back as
# "ThomasHartley,sonofEdward,...". The line is the issue's; it is set on three
# baselines so the page is long enough to judge (one line is a "short page"
# the check leaves alone).
_SQUEEZED_WORDS = [
    (72.00, 'Thomas'), (97.27, 'Hartley,'), (122.71, 'son'), (135.88, 'of'),
    (145.37, 'Edward,'), (171.55, 'b.'), (180.50, 'May'), (196.60, '9,'),
    (205.55, '1854,'), (224.40, 'came'), (242.32, 'also'), (256.96, 'from'),
    (273.79, 'Marsh'), (294.65, 'Creek'),
]
SQUEEZED_STREAM = (
    'BT /F1 11 Tf 3 Tr 60 Tz\n'
    + ''.join(f'1 0 0 1 {x:.2f} {y} Tm ({word}) Tj\n'
              for y in (720, 706, 692) for x, word in _SQUEEZED_WORDS)
    + 'ET')
SQUASHED_LINE = 'ThomasHartley,sonofEdward,b.May9,1854,camealsofromMarshCreek'

# Times-Roman advance widths (Adobe's standard metrics, per 1000 em) for the
# characters the fixtures below set, so each word can be placed where a real
# OCR layer would put it.
_TIMES_WIDTHS = {
    **dict(zip('abcdefghijklmnopqrstuvwxyz',
               [444, 500, 444, 500, 444, 333, 500, 500, 278, 278, 500, 278, 778,
                500, 500, 500, 500, 333, 389, 278, 500, 500, 722, 500, 500, 444])),
    **dict(zip('ABCDEFGHIJKLMNOPQRSTUVWXYZ',
               [722, 667, 667, 722, 611, 556, 722, 722, 333, 389, 722, 611, 889,
                722, 722, 556, 722, 667, 556, 611, 722, 722, 944, 722, 722, 611])),
    **{d: 500 for d in '0123456789'},
    ' ': 250, ',': 250, '.': 250, ';': 278, ':': 278, "'": 333,
}


def _times_width(word: str, size: float) -> float:
    return sum(_TIMES_WIDTHS.get(ch, 500) for ch in word) * size / 1000


def _squeezed_block(words: list, left: float, width: float, tz: int, gap: float,
                    size: int = 11, top: float = 720) -> str:
    """Content-stream lines placing each word on its own, squeezed to `tz` percent.

    The OCR-layer shape #281 is about: one Tm per word, horizontal scaling,
    invisible text, no space characters. Words wrap inside `left`..`left+width`.
    """
    out, x, y = [], left, top
    for word in words:
        w = _times_width(word, size) * tz / 100
        if x + w > left + width:
            x, y = left, y - (size + 3)
        out.append(f'1 0 0 1 {x:.2f} {y:.2f} Tm ({word}) Tj\n')
        x += w + gap
    return ''.join(out)


def _lines_stream(lines: list, size: int = 11) -> str:
    """An ordinary text page: one Tj per line, stepped down the page."""
    body = ''.join(f'({line}) Tj 0 -{size + 4} Td\n' for line in lines)
    return f'BT /F1 {size} Tf 72 720 Td\n{body}ET'


SPRING_WORDS = ('In the spring of eighteen hundred fifty four Thomas Hartley removed '
                'with his family to Washington township where he purchased one '
                'hundred sixty acres of unimproved land').split()
LEFT_WORDS = ('Thomas Hartley, son of Edward, b. May 9, 1854, came also from Marsh '
              'Creek with his brothers in the spring and settled on section twelve '
              'near the old mill').split()
MC_TEXT = ("Angus McDonald married Mary MacArthur at St. Andrew's in 1842. Their son "
           "John McDonald, b. 1845, and daughter Flora McDonald, b. 1847, emigrated with "
           "the DeWitt and VanBuren families. Rev. McLeod officiated; witnesses were Ewan "
           "MacKenzie and Catriona McKay. The DeLancey and LaFontaine households were "
           "neighbours. McIntyre, McNab, MacGregor, McPherson and O'Neill also appear in "
           "the parish.")
ENGLISH_WORDS = (('Thomas Hartley, son of Edward Hartley and Mary Ann Day, was baptized '
                  'at Halifax in 1812 and emigrated with his parents to Michigan in 1836, '
                  'where he purchased land in Lyon Township and married Sarah Bell, '
                  'daughter of Thomas Bell, a blacksmith of Novi. ') * 2).split()


def _ascii(text: str) -> str:
    """Fold accents away (the test font is plain Times-Roman), drop parentheses."""
    import unicodedata
    for a, b in (('\u00f8', 'o'), ('\u00e6', 'ae'), ('\u0142', 'l'), ('\u0141', 'L')):
        text = text.replace(a, b)
    folded = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode()
    return folded.replace('(', '').replace(')', '')


OTHER_TEXTS = {k: _ascii(v) for k, v in {
    'pl': ('Jan Kowalski, syn J\u00f3zefa i Marii z d. Nowak, ur. w Suwa\u0142kach dnia 3 maja '
           '1868 r., zm. w Krasnopolu w 1921 r. Ochrzczony w ko\u015bciele parafialnym w '
           'Suwa\u0142kach przez ks. Piotra Zawadzkiego. Rodzice chrzestni: Adam Wi\u015bniewski '
           'z Suwa\u0142k i Anna Lewandowska ze wsi P\u0142ociczno. O\u017ceni\u0142 si\u0119 w r. '
           '1892 z Katarzyn\u0105 Zieli\u0144sk\u0105, c\u00f3rk\u0105 Stanis\u0142awa, we wsi Huta.'),
    'cz': ('Jan Nov\u00e1k, syn Josefa Nov\u00e1ka, roln\u00edka v Lipov\u00e9 \u010d. 12, a jeho '
           'man\u017eelky Marie roz. Dvo\u0159\u00e1kov\u00e9 z Kun\u010dic, narozen dne 4. b\u0159ezna '
           '1871 a pok\u0159t\u011bn v kostele sv. Jakuba v Lipov\u00e9. Kmot\u0159i: V\u00e1clav Svoboda '
           'z Lipov\u00e9 a Anna \u010cern\u00e1 ze Skalice.'),
    'fr': ('Le vingt-trois mai mil huit cent quarante-deux, \u00e0 dix heures du matin, '
           'par-devant nous, maire de la commune de Saint-Jean-de-Luz, est comparu '
           'Jean-Baptiste Dupont, \u00e2g\u00e9 de trente ans, cultivateur, domicili\u00e9 en cette '
           'commune, lequel nous a pr\u00e9sent\u00e9 un enfant du sexe masculin, n\u00e9 hier \u00e0 six '
           'heures du soir.'),
    'nl': ('Heden den twaalfden Maart achttien honderd zeven en vijftig, compareerde voor '
           'ons Ambtenaar van den Burgerlijken Stand der gemeente Zaltbommel, Jan van der '
           'Berg, oud dertig jaren, van beroep schipper, wonende te Zaltbommel, dewelke ons '
           'heeft verklaard dat op den tienden dezer maand is geboren een kind.'),
    'no': ('Ole Olsen Haugen, f. 3. mai 1834 p\u00e5 Haugen i Sogndal, s\u00f8nn av Ola Pedersen '
           'og Kari Andersdatter. Gift 1858 i Sogndal kirke med Brita Larsdatter Nes, f. 1836 '
           'p\u00e5 Nes, datter av Lars Johannesen og Ingeborg Olsdatter. De bodde p\u00e5 plassen '
           'under Haugen til 1866, da de utvandret til Amerika.'),
    'regrom': ('1. THOMAS HARTLEY, b. 3 May 1812, Halifax, Yorks.; m. 1835 MARY ANN DAY. '
               'Children: i. JOHN, b. 1836; m. Sarah Bell. ii. EDWARD, b. 1838; d. young. '
               'iii. MARY, b. 1840; m. Wm. Cox. iv. ANN, b. 1842. v. ELI, b. 1845; m. Jane '
               'Ives. vi. SARAH, b. 1847; d. 1849. vii. THOMAS, b. 1849. viii. HANNAH, b. 1851. '
               'ix. GEORGE, b. 1853. x. JAMES, b. 1855.'),
    'cite': ('Ancestry.com, 1880 U.S. Fed. Census, Twp. of Lyon, Oakland Co., Mich., ED 12, '
             'sht. 4B, dwl. 34, fam. 36, NARA micro. T9, roll 597, p. 112. Cf. FHL film '
             '1,254,597, it. 3; ibid., p. 113. Death Cert. no. 4521, gives b. abt. 1841 Eng.; '
             'inf. dau. Mrs. Wm. Hartley. Op. cit. n. 4; viz. lib. 12, fol. 34v; wit. Jno. '
             'and Thos. Hartley; bk. 3, pg. 7.'),
    'hyph': ('Thomas Hart- ley, son of Edward Hart- ley and Mary Ann Day, was bap- tized at '
             'Halifax in 1812 and emi- grated with his par- ents to Michi- gan in 1836, where '
             'he pur- chased land in Lyon Town- ship and mar- ried Sarah Bell, daugh- ter of '
             'Thomas Bell, a black- smith of Novi.'),
}.items()}
SPANISH_TEXT = ('En la ciudad de Sevilla, a los doce dias del mes de mayo de mil ochocientos '
                'uno, yo el cura de la parroquia de San Juan bautice a una nina hija legitima '
                'de Jose Garcia y de Maria de la Cruz, su mujer. Fueron sus padrinos Juan de '
                'la Vega y Ana de los Rios, a quienes adverti el parentesco espiritual y la '
                'obligacion. Abuelos paternos Pedro Garcia y Rosa Lopez; maternos Luis de la '
                'Cruz y Teresa de la O. Y lo firme en el dia de la fecha.')
RIGHT_WORDS = ('Mary Ann Brown, daughter of Josiah, married Thomas in 1876 at the '
               'Methodist church in Marsh Creek and had nine children of whom seven '
               'survived to adulthood').split()


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
        # pdftotext is an optional third reader (#281). Tests run as if it is
        # absent - the CI image has none - unless one asks for the real one.
        patcher = mock.patch.object(source_mod, '_pdftotext_path',
                                    return_value=None, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_record(self, files_block: str | None = None) -> None:
        if files_block is None:
            files_block = (f'files:\n  - file: documents/book/county-history_{SID.lower()}.pdf\n'
                           '    is_primary: true\n')
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
        out = self.pdf.parent / f'county-history-extracted-text_{SID.lower()}.md'
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
                         f'documents/book/county-history-extracted-text_{SID.lower()}.md')
        # the original is byte-untouched
        self.assertEqual(self.pdf.read_bytes(), _minimal_pdf(
            ['Hartley arrived 1854', 'He farmed Marsh Creek']))

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_textless_page_gets_placeholder_never_silence(self) -> None:
        self.pdf.write_bytes(_minimal_pdf(['Text here', None, 'More text']))
        result = self._run()
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result['pages_with_text'], 2)
        out = self.pdf.parent / f'county-history-extracted-text_{SID.lower()}.md'
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
            (self.pdf.parent / f'county-history-extracted-text_{SID.lower()}.md').exists())
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
        out = self.pdf.parent / f'county-history-extracted-text_{SID.lower()}.md'
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
            (self.pdf.parent / f'county-history-extracted-text_{SID.lower()}.md').exists())
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
        out = self.pdf.parent / f'county-history-extracted-text_{SID.lower()}.md'
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
            (self.pdf.parent / f'county-history-extracted-text_{SID.lower()}.md').exists())
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
            f'files:\n  - file: documents/book/scan_{SID.lower()}.jpg\n    is_primary: true\n')
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
        # Two non-derived PDFs both marked role: primary and NEITHER labelled
        # with copy: - nothing says which is the main copy. Taking the first
        # would risk attaching text from the WRONG PDF, so the tool refuses,
        # names both files, and says how to label the second (#280: without
        # calling the record a hand-edit mistake).
        self._write_record(
            f'files:\n  - file: documents/book/a_{SID.lower()}.pdf\n    is_primary: true\n'
            f'  - file: documents/book/b_{SID.lower()}.pdf\n    is_primary: true\n')
        (self.root / 'documents' / 'book' / f'a_{SID.lower()}.pdf').write_bytes(
            _minimal_pdf(['A text']))
        (self.root / 'documents' / 'book' / f'b_{SID.lower()}.pdf').write_bytes(
            _minimal_pdf(['B text']))
        result = self._run()
        self.assertNotEqual(result.exit_code, EXIT_CLEAN)
        joined = ' '.join(m.text for m in result.messages)
        self.assertIn('main file', joined)
        self.assertIn(f'a_{SID.lower()}.pdf', joined)
        self.assertIn(f'b_{SID.lower()}.pdf', joined)
        self.assertIn('copy:', joined)
        self.assertNotIn('hand-edit', joined)
        # Nothing was written: no dump, no inventory growth.
        self.assertFalse(list(self.pdf.parent.glob('*extracted-text*')))
        self.assertEqual(len(read_record(self.record)['meta']['files']), 2)

    # -- #280: a second digitization attached as primary:COPY -----------------

    def _two_copies(self, second_copy: str = 'familysearch') -> None:
        """The shape `fha process --more FILE primary:familysearch` writes."""
        second = f'hartley-familysearch-primary_{SID.lower()}.pdf'
        self._write_record(
            f'files:\n  - file: documents/book/county-history_{SID.lower()}.pdf\n'
            '    is_primary: true\n'
            f'  - file: documents/book/{second}\n'
            f'    is_primary: true\n    copy: {second_copy}\n')
        self.pdf.write_bytes(_minimal_pdf(['Own scan text']))
        (self.pdf.parent / second).write_bytes(_minimal_pdf(['FamilySearch copy text']))

    def _two_copies_new_form(self, second: str) -> None:
        """SPEC §14: `is_primary: true` on the main PDF alone; the second
        digitization listed by its copy label, with no role line."""
        self._write_record(
            f'files:\n  - file: documents/book/county-history_{SID.lower()}.pdf\n'
            '    is_primary: true\n'
            f'  - file: documents/book/{second}\n'
            '    copy: familysearch\n')
        self.pdf.write_bytes(_minimal_pdf(['Own scan text']))
        (self.pdf.parent / second).write_bytes(_minimal_pdf(['FamilySearch copy text']))

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_is_primary_marks_the_pdf_to_read(self) -> None:
        self._two_copies_new_form(f'hartley-familysearch_{SID.lower()}.pdf')
        result = self._run()
        self.assertEqual(result.exit_code, EXIT_CLEAN, [m.text for m in result.messages])
        out = self.pdf.parent / f'county-history-extracted-text_{SID.lower()}.md'
        self.assertIn('Own scan text', out.read_text(encoding='utf-8'))

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_an_older_primary_named_copy_keeps_working_without_its_role(self) -> None:
        # A copy filed as `-primary` by an older `--more FILE primary:COPY`
        # keeps its name once its record drops `role: primary`; the dump
        # still leaves the word out.
        self._two_copies_new_form(f'hartley-familysearch-primary_{SID.lower()}.pdf')
        result = self._run(copy_label='familysearch')
        self.assertEqual(result.exit_code, EXIT_CLEAN, [m.text for m in result.messages])
        out = self.pdf.parent / f'hartley-familysearch-extracted-text_{SID.lower()}.md'
        self.assertIn('FamilySearch copy text', out.read_text(encoding='utf-8'))

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_labelled_second_primary_extracts_the_unlabelled_one(self) -> None:
        self._two_copies()
        result = self._run()
        self.assertEqual(result.exit_code, EXIT_CLEAN, [m.text for m in result.messages])
        out = self.pdf.parent / f'county-history-extracted-text_{SID.lower()}.md'
        text = out.read_text(encoding='utf-8')
        self.assertIn('Own scan text', text)
        self.assertNotIn('FamilySearch copy text', text)

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_copy_flag_extracts_the_labelled_copy(self) -> None:
        self._two_copies()
        result = self._run(copy_label='FamilySearch')     # capitals do not matter
        self.assertEqual(result.exit_code, EXIT_CLEAN, [m.text for m in result.messages])
        out = self.pdf.parent / f'hartley-familysearch-extracted-text_{SID.lower()}.md'
        text = out.read_text(encoding='utf-8')
        self.assertIn('FamilySearch copy text', text)
        self.assertNotIn('Own scan text', text)
        self.assertEqual(read_record(self.record)['meta']['files'][-1]['file'],
                         f'documents/book/{out.name}')
        # L4: the PDF's `-primary` role gives way to `-extracted-text`
        # (SPEC §13 `{slug}[-{copy}][-{role}]`); the copy part stays.
        self.assertNotIn('-primary-', out.name)
        # L3: the header names the copy, and does not promise page numbers
        # that line up in every copy of the book.
        head = text.partition('[Page 1]')[0]
        self.assertIn('(copy: familysearch)', head)
        self.assertNotIn('line up in any copy', head)

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_copy_flag_on_an_extracted_source_names_the_pdf_it_came_from(self) -> None:
        # L2: the one dump a source keeps came from the main copy, so asking
        # for the familysearch copy must say so, not just "already".
        self._two_copies()
        self.assertEqual(self._run().exit_code, EXIT_CLEAN)
        again = self._run(copy_label='familysearch')
        self.assertEqual(again['status'], 'already')
        joined = ' '.join(m.text for m in again.messages)
        self.assertIn(f'made from county-history_{SID.lower()}.pdf', joined)
        self.assertIn('copy: familysearch', joined)
        # Asking for the copy it was made from says nothing extra.
        self.tearDown()
        self.setUp()
        self._two_copies()
        self.assertEqual(self._run(copy_label='familysearch').exit_code, EXIT_CLEAN)
        again = self._run(copy_label='familysearch')
        self.assertNotIn('made from', ' '.join(m.text for m in again.messages))

    def test_layout_retry_restores_the_pypdf_log_level_across_threads(self) -> None:
        # L5: two extractions at once (the workbench can run them) must not
        # leave pypdf's logger quieted for good.
        import logging
        import threading
        import time
        logger = logging.getLogger('pypdf')
        before = logger.level
        logger.setLevel(logging.WARNING)
        self.addCleanup(logger.setLevel, before)

        class _Page:
            def extract_text(self, *a, **k):
                time.sleep(0.05)
                return 'x'

        threads = [threading.Thread(target=source_mod._layout_reading, args=(_Page(),))
                   for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(logger.level, logging.WARNING)

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_copy_flag_unknown_label_refuses_listing_labels(self) -> None:
        self._two_copies()
        before = self.record.read_text(encoding='utf-8')
        result = self._run(copy_label='ancestry')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        joined = ' '.join(m.text for m in result.messages)
        self.assertIn('ancestry', joined)
        self.assertIn(f'fha source extract {SID.lower()} --copy familysearch', joined)
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        self.assertFalse(list(self.pdf.parent.glob('*extracted-text*')))

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_copy_flag_unknown_label_is_named_even_after_an_extraction(self) -> None:
        # A mistyped label must not hide behind the clean "already" exit.
        self._two_copies()
        self.assertEqual(self._run().exit_code, EXIT_CLEAN)
        result = self._run(copy_label='ancestry')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertTrue(any('ancestry' in m.text for m in result.messages))

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_copy_flag_on_a_source_with_no_labels_refuses_plainly(self) -> None:
        self.pdf.write_bytes(_minimal_pdf(['Own scan text']))
        result = self._run(copy_label='b')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        joined = ' '.join(m.text for m in result.messages)
        self.assertIn('none of its PDFs carries a copy: label', joined)
        self.assertIn(f'`fha source extract {SID.lower()}`', joined)

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_every_primary_labelled_refuses_naming_copy(self) -> None:
        self._write_record(
            f'files:\n  - file: documents/book/a_{SID.lower()}.pdf\n'
            '    is_primary: true\n    copy: b\n'
            f'  - file: documents/book/c_{SID.lower()}.pdf\n'
            '    is_primary: true\n    copy: c\n')
        result = self._run()
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        joined = ' '.join(m.text for m in result.messages)
        self.assertIn(f'fha source extract {SID.lower()} --copy b', joined)
        self.assertIn('(b, c)', joined)

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_cli_passes_copy_through(self) -> None:
        import argparse
        import contextlib
        import io
        self._two_copies()
        parser = argparse.ArgumentParser()
        source_mod.register(parser.add_subparsers())
        args = parser.parse_args(['source', 'extract', SID, '--copy', 'familysearch',
                                  '--root', str(self.root)])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = args.func(args)
        self.assertEqual(code, EXIT_CLEAN, out.getvalue())
        self.assertTrue((self.pdf.parent /
                         f'hartley-familysearch-extracted-text_{SID.lower()}.md').exists())

    # -- #281: a text layer whose words run together ---------------------------

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_squeezed_ocr_layer_keeps_its_word_spaces(self) -> None:
        self.pdf.write_bytes(_minimal_pdf([_raw(SQUEEZED_STREAM)], base_font='Times-Roman'))
        result = self._run()
        out = self.pdf.parent / f'county-history-extracted-text_{SID.lower()}.md'
        text = out.read_text(encoding='utf-8')
        if 'Thomas Hartley, son of Edward' in text:
            self.assertEqual(result.exit_code, EXIT_CLEAN,
                             [m.text for m in result.messages])
            self.assertEqual(result.data['squashed_pages'], [])
            self.assertNotIn('ThomasHartley,sonofEdward', text)
            self.assertNotIn('Words run together', text)
        else:
            # This pypdf cannot recover the spaces: the run must say so.
            self.assertEqual(result.exit_code, EXIT_WARNINGS)
            self.assertEqual(result.data['squashed_pages'], [1])
            self.assertTrue(any('page(s) 1' in m.text and 'fha find --text' in m.text
                                for m in result.messages))

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_rotated_body_is_never_dropped_for_a_spaced_heading(self) -> None:
        # PR #283 review: pypdf's layout mode leaves out rotated text by
        # default. A short upright heading over a rotated, squeezed,
        # run-together body then read back as just the heading - spaced
        # normally, so it passed the check and replaced the whole page with no
        # warning. The body must survive, and the page must be flagged.
        body = ''.join(f'0 1 -1 0 {x0} {y - 72 + 100:.2f} Tm ({word}) Tj\n'
                       for x0 in (300, 314, 328) for y, word in _SQUEEZED_WORDS)
        stream = ('BT /F1 11 Tf 100 Tz 1 0 0 1 72 740 Tm '
                  '(County History of Marsh Creek) Tj\n3 Tr 60 Tz\n' + body + 'ET')
        self.pdf.write_bytes(_minimal_pdf([_raw(stream)], base_font='Times-Roman'))
        result = self._run()
        text = (self.pdf.parent / f'county-history-extracted-text_{SID.lower()}.md').read_text(
            encoding='utf-8')
        self.assertIn('County History of Marsh Creek', text)
        self.assertIn(SQUASHED_LINE, text)            # the rotated body is kept
        self.assertEqual(result.data['layout_pages'], [])
        self.assertEqual(result.data['squashed_pages'], [1])
        self.assertEqual(result.exit_code, EXIT_WARNINGS)

    def test_same_reading_refuses_a_layout_read_that_loses_or_scrambles(self) -> None:
        same = source_mod._same_reading
        default = 'Heading line\n' + (SQUASHED_LINE + '\n') * 3
        spaced = ('Heading line\n'
                  + 'Thomas Hartley, son of Edward, b. May 9, 1854, came also '
                    'from Marsh Creek\n' * 3)
        self.assertTrue(same(default, spaced))
        self.assertFalse(same(default, 'Heading line'))                 # lost
        self.assertFalse(same(default, spaced.rsplit('\n', 2)[0]))      # a line lost
        scrambled = 'Heading line\n' + '\n'.join(
            'ThomasThomasThomas Hartley,Hartley,Hartley, sonsonson '
            'ofofof Edward,Edward,Edward, b.b.b. MayMayMay 9,9,9, '
            '1854,1854,1854, camecamecame alsoalsoalso fromfromfrom '
            'MarshMarshMarsh CreekCreekCreek'.split(' '))
        self.assertFalse(same(default, scrambled))

    def _fake_reader(self, default_text: str, layout_text):
        """A PdfReader stand-in whose one page reads back as given, per mode."""
        calls: list = []

        class _Page:
            def extract_text(self, *a, **k):
                calls.append(k.get('extraction_mode', 'plain'))
                if k.get('extraction_mode') == 'layout':
                    if isinstance(layout_text, Exception):
                        raise layout_text
                    return layout_text
                return default_text

        class _Reader:
            def __init__(self, *a, **k):
                self.pages = [_Page()]

        return _Reader, calls

    def _run_with_reader(self, reader_cls, **kw):
        import pypdf
        self.pdf.write_bytes(_minimal_pdf(['placeholder']))
        real_cls = pypdf.PdfReader
        pypdf.PdfReader = reader_cls
        try:
            return self._run(**kw)
        finally:
            pypdf.PdfReader = real_cls

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_unrecoverable_run_together_page_is_written_with_a_warning(self) -> None:
        squashed = (SQUASHED_LINE + '\n') * 3
        # The layout read finds one more break per line but still runs the
        # words together: no reader recovered the spaces.
        still_squashed = (SQUASHED_LINE.replace('1854,', '1854, ') + '\n') * 3
        reader, calls = self._fake_reader(squashed, still_squashed)
        result = self._run_with_reader(reader)
        self.assertIn('layout', calls)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result.data['squashed_pages'], [1])
        self.assertEqual(result.data['reordered_pages'], [])
        warnings = [m.text for m in result.messages if m.level == 'warning']
        self.assertTrue(any('page(s) 1' in w and 'fha find --text' in w
                            and 'could not recover the spaces' in w for w in warnings),
                        warnings)
        text = (self.pdf.parent / f'county-history-extracted-text_{SID.lower()}.md').read_text(
            encoding='utf-8')
        head, _, body = text.partition('[Page 1]')
        self.assertIn('Words run together on page(s) 1', head)
        self.assertIn('fha find --text', head)
        self.assertIn(SQUASHED_LINE, body)          # the text is still kept

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_dry_run_gives_the_same_page_warnings_and_exit(self) -> None:
        # L1: a preview that hid what the real run will warn about is no preview.
        squashed = (SQUASHED_LINE + '\n') * 3
        reader, _ = self._fake_reader(squashed, ValueError('layout boom'))
        dry = self._run_with_reader(reader, dry_run=True)
        self.assertEqual(dry['status'], 'dry-run')
        self.assertEqual(dry.exit_code, EXIT_WARNINGS)
        self.assertTrue(any(m.level == 'warning' and 'Words run together on page(s) 1'
                            in m.text and 'fha find --text' in m.text
                            for m in dry.messages), [m.text for m in dry.messages])
        self.assertFalse(list(self.pdf.parent.glob('*extracted-text*')))

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_dry_run_warns_about_failed_pages_like_the_real_run(self) -> None:
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
            dry = self._run(dry_run=True)
        finally:
            pypdf.PdfReader = real_cls
        self.assertEqual(dry.exit_code, EXIT_WARNINGS)
        self.assertTrue(any(m.level == 'warning' and 'FAILED on page(s) 2' in m.text
                            for m in dry.messages), [m.text for m in dry.messages])

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_layout_mode_failure_keeps_the_default_text_and_warns(self) -> None:
        squashed = (SQUASHED_LINE + '\n') * 3
        reader, _ = self._fake_reader(squashed, ValueError('layout boom'))
        result = self._run_with_reader(reader)
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result.data['squashed_pages'], [1])
        self.assertEqual(result.data['error_pages'], [])

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_layout_text_is_collapsed_and_used_when_it_reads_as_words(self) -> None:
        squashed = (SQUASHED_LINE + '\n') * 3
        layout = ('      Thomas   Hartley,   son  of   Edward,  b. May 9, 1854,  came  '
                  'also from  Marsh  Creek   \n\n\n\n') * 3
        reader, _ = self._fake_reader(squashed, layout)
        result = self._run_with_reader(reader)
        self.assertEqual(result.exit_code, EXIT_CLEAN, [m.text for m in result.messages])
        self.assertEqual(result.data['layout_pages'], [1])
        text = (self.pdf.parent / f'county-history-extracted-text_{SID.lower()}.md').read_text(
            encoding='utf-8')
        self.assertIn('Thomas Hartley, son of Edward, b. May 9, 1854, came also '
                      'from Marsh Creek\n', text)
        self.assertNotIn('  ', text)
        self.assertNotIn('\n\n\n', text)

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_normal_page_never_takes_the_layout_retry(self) -> None:
        prose = ('Thomas Hartley, son of Edward, was born May 9, 1854, and came '
                 'also from Marsh Creek with his brothers in the spring. ') * 4
        reader, calls = self._fake_reader(prose, 'should not be read')
        result = self._run_with_reader(reader)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(calls, ['plain'])

    # -- #281 review: pages that only look run together -------------------------

    def _assert_plain_dump(self, result) -> None:
        self.assertEqual(result.exit_code, EXIT_CLEAN, [m.text for m in result.messages])
        self.assertEqual(result.data['squashed_pages'], [])
        text = (self.pdf.parent / f'county-history-extracted-text_{SID.lower()}.md').read_text(
            encoding='utf-8')
        self.assertNotIn('Words run together', text)

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_contents_page_with_dot_leaders_is_plain(self) -> None:
        entries = [('Preface', 'iii'), ('Introduction', '1'), ('Early Settlers', '23'),
                   ('Churches', '57'), ('Schools', '88'),
                   ('Biographical Sketches', '120'), ('Index', '301')]
        lines = [f'{title} {"." * (70 - len(title))} {page}' for title, page in entries]
        self.pdf.write_bytes(_minimal_pdf([_raw(_lines_stream(lines))],
                                          base_font='Times-Roman'))
        self._assert_plain_dump(self._run())

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_url_list_page_is_plain(self) -> None:
        lines = [f'https://archive.org/details/historyofmarshcreek00hart/page/{i}/mode/2up'
                 for i in range(12)]
        self.pdf.write_bytes(_minimal_pdf([_raw(_lines_stream(lines, size=9))]))
        self._assert_plain_dump(self._run())

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_long_words_both_readers_agree_on_are_plain(self) -> None:
        # Long compounds trip the trigger, but layout mode breaks the page at
        # the same places, so the words really are that long: no warning.
        line = ('Donaudampfschifffahrtsgesellschaft Grundbuchamt '
                'Kirchenbuchduplikatsammlung Standesamtsregister')
        self.pdf.write_bytes(_minimal_pdf([_raw(_lines_stream([line] * 4, size=9))]))
        self.assertTrue(source_mod._words_run_together('\n'.join([line] * 4)))
        self._assert_plain_dump(self._run())

    # -- #281 review: pages squeezed only in part --------------------------------

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_lightly_squeezed_page_is_retried_and_spaced(self) -> None:
        # 85% scaling with 6 pt gaps: pypdf's default mode keeps some spaces and
        # loses others ("eighteenhundredfifty"), so the page used to slip past.
        body = _squeezed_block(SPRING_WORDS * 2, left=72, width=468, tz=85, gap=6)
        self.pdf.write_bytes(_minimal_pdf(
            [_raw('BT /F1 11 Tf 3 Tr 85 Tz\n' + body + 'ET')], base_font='Times-Roman'))
        result = self._run()
        text = (self.pdf.parent / f'county-history-extracted-text_{SID.lower()}.md').read_text(
            encoding='utf-8')
        self.assertIn('eighteen hundred fifty', text)
        self.assertEqual(result.exit_code, EXIT_CLEAN, [m.text for m in result.messages])

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_kerning_split_words_are_retried_and_spaced(self) -> None:
        # Each longer word set as two halves with a kerning step between them:
        # the default reading puts a space inside the word and loses the real
        # ones ("Tho masHart ley,sonofEdw ard") - short pieces, but glued.
        body = ''
        for y in (720, 706, 692, 678):
            for x, word in _SQUEEZED_WORDS:
                if len(word) >= 4:
                    h = len(word) // 2
                    body += f'1 0 0 1 {x:.2f} {y} Tm [({word[:h]}) -120 ({word[h:]})] TJ\n'
                else:
                    body += f'1 0 0 1 {x:.2f} {y} Tm ({word}) Tj\n'
        self.pdf.write_bytes(_minimal_pdf(
            [_raw('BT /F1 11 Tf 3 Tr 60 Tz\n' + body + 'ET')], base_font='Times-Roman'))
        result = self._run()
        self.assertEqual(result.exit_code, EXIT_CLEAN, [m.text for m in result.messages])
        text = (self.pdf.parent / f'county-history-extracted-text_{SID.lower()}.md').read_text(
            encoding='utf-8')
        self.assertIn('Thomas Hartley, son of Edward', text)

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_pdftotext_is_the_one_second_reading_when_installed(self) -> None:
        # The best reader there is reads the page once: pdftotext, when it is
        # installed and its reading can be used - layout mode is not asked.
        squashed = 'Tho masHart ley,sonofEdw ard,b.May9,18 54,ca meal sofr omMa rshCr eek\n' * 4
        broken = ('Tho mas Hart ley, son of Edw ard, b. May 9, 18 54, ca me al so fr om '
                  'Ma rsh Cr eek\n') * 4
        whole = 'Thomas Hartley, son of Edward, b. May 9, 1854, came also from Marsh Creek\n' * 4
        reader, calls = self._fake_reader(squashed, broken)
        with mock.patch.object(source_mod, '_pdftotext_path', return_value='pdftotext'), \
                mock.patch.object(source_mod._PdftotextPages, 'page', return_value=whole):
            result = self._run_with_reader(reader)
        self.assertEqual(result.data['pdftotext_pages'], [1])
        self.assertNotIn('layout', calls)
        self.assertEqual(result.exit_code, EXIT_CLEAN, [m.text for m in result.messages])
        text = (self.pdf.parent / f'county-history-extracted-text_{SID.lower()}.md').read_text(
            encoding='utf-8')
        self.assertIn('Thomas Hartley, son of Edward', text)

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_unusable_pdftotext_reading_falls_back_to_layout(self) -> None:
        # pdftotext gave nothing, or a reading that changed the text: the
        # page is read by layout mode instead.
        squashed = (SQUASHED_LINE + '\n') * 3
        spaced = 'Thomas Hartley, son of Edward, b. May 9, 1854, came also from Marsh Creek\n' * 3
        for pt_reading in (None, 'Thomas Hartley, son of Edward\n'):
            with self.subTest(pt_reading=pt_reading):
                self.tearDown()
                self.setUp()
                reader, calls = self._fake_reader(squashed, spaced)
                with mock.patch.object(source_mod, '_pdftotext_path', return_value='pdftotext'), \
                        mock.patch.object(source_mod._PdftotextPages, 'page',
                                          return_value=pt_reading):
                    result = self._run_with_reader(reader)
                self.assertIn('layout', calls)
                self.assertEqual(result.data['layout_pages'], [1])
                self.assertEqual(result.exit_code, EXIT_CLEAN)

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_kern170_style_pdf_without_pdftotext_takes_layout(self) -> None:
        # Every longer word set as two halves with a wide kerning step (-170).
        # Without pdftotext, layout's reading spaces the page but still breaks
        # some words ("Hart ley"). The owner's call (#281): that is accepted -
        # far better for search than "ThomasHartley" - with no warning.
        body = ''
        for y in (720, 706, 692, 678):
            for x, word in _SQUEEZED_WORDS:
                if len(word) >= 4:
                    h = len(word) // 2
                    body += f'1 0 0 1 {x:.2f} {y} Tm [({word[:h]}) -170 ({word[h:]})] TJ\n'
                else:
                    body += f'1 0 0 1 {x:.2f} {y} Tm ({word}) Tj\n'
        self.pdf.write_bytes(_minimal_pdf(
            [_raw('BT /F1 11 Tf 3 Tr 60 Tz\n' + body + 'ET')], base_font='Times-Roman'))
        result = self._run()
        self.assertEqual(result.data['layout_pages'], [1])
        self.assertEqual(result.exit_code, EXIT_CLEAN, [m.text for m in result.messages])
        text = (self.pdf.parent / f'county-history-extracted-text_{SID.lower()}.md').read_text(
            encoding='utf-8')
        self.assertIn('son of', text)
        self.assertNotIn('sonofEdw', text)

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_spanish_squeezed_page_with_wide_gaps_is_clean_layout(self) -> None:
        # Tz 60 with 6 pt gaps; the default reading keeps some real gaps
        # next to short words ("mesde mayode"). Layout spaces it: no warning.
        body = _squeezed_block(SPANISH_TEXT.split(), left=72, width=468, tz=60, gap=6)
        self.pdf.write_bytes(_minimal_pdf(
            [_raw('BT /F1 11 Tf 3 Tr 60 Tz\n' + body + 'ET')], base_font='Times-Roman'))
        result = self._run()
        self.assertEqual(result.data['layout_pages'], [1])
        self.assertEqual(result.data['squashed_pages'], [])
        self.assertEqual(result.exit_code, EXIT_CLEAN, [m.text for m in result.messages])

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_surname_page_number_index_is_reread(self) -> None:
        # P2-3: "Abbott12,340" - an index's name run into its page numbers.
        entries = [f'{name} {12 + i * 7}, {340 + i * 3}' for i, name in enumerate(
            'Abbott Adams Baker Clark Davis Evans Foster Green Hall Irwin Jones King '
            'Lewis Moore Nash Owen Price Quinn Reed Stone'.split())]
        body, y = '', 740
        for entry in entries:
            body += _squeezed_block(entry.split(), left=72, width=468, tz=60, gap=3, top=y)
            y -= 14
        self.pdf.write_bytes(_minimal_pdf(
            [_raw('BT /F1 11 Tf 3 Tr 60 Tz\n' + body + 'ET')], base_font='Times-Roman'))
        result = self._run()
        self.assertEqual(result.data['layout_pages'], [1], [m.text for m in result.messages])
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        text = (self.pdf.parent / f'county-history-extracted-text_{SID.lower()}.md').read_text(
            encoding='utf-8')
        self.assertIn('Abbott 12, 340', text)
        # Ordinary pages of numbers and ordinals do not trigger at all.
        check = source_mod._words_run_together
        self.assertFalse(check('\n'.join(entries)))
        self.assertFalse(check('\n'.join(' '.join(str(x) for x in (1850 + i, 12 + i, 3, 4, 5, 6))
                                         for i in range(20))))
        self.assertFalse(check('Born on the 21st of May 1850 and married on the 3rd of June '
                               '1872 at the church of St. Mary in the town of Lyon. ' * 4))

    def test_pdftotext_crash_not_reported_as_read(self) -> None:
        # L1: only a successful run counts as "read"; a failure is named.
        with mock.patch.object(source_mod, '_pdftotext_path', return_value='pdftotext'), \
                mock.patch.object(source_mod.subprocess, 'run',
                                  return_value=mock.Mock(returncode=-11, stdout=b'')):
            pages = source_mod._PdftotextPages(self.pdf, [1, 2])
            self.assertIsNone(pages.page(1))
        self.assertEqual(pages.calls, 0)
        self.assertEqual(pages.failures[0][:2], (1, 2))
        for stdout, why in ((b'', 'no text'), (b'one\f', 'told apart')):
            with mock.patch.object(source_mod, '_pdftotext_path', return_value='pdftotext'), \
                    mock.patch.object(source_mod.subprocess, 'run',
                                      return_value=mock.Mock(returncode=0, stdout=stdout)):
                pages = source_mod._PdftotextPages(self.pdf, [1, 2])
                pages.page(1)
            self.assertEqual(pages.calls, 0)
            self.assertIn(why, pages.failures[0][2])

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_pdftotext_failure_is_one_plain_line_in_the_run(self) -> None:
        squashed = (SQUASHED_LINE + '\n') * 3
        reader, _ = self._fake_reader(squashed, ValueError('layout boom'))
        with mock.patch.object(source_mod, '_pdftotext_path', return_value='pdftotext'), \
                mock.patch.object(source_mod.subprocess, 'run',
                                  return_value=mock.Mock(returncode=-11, stdout=b'')):
            result = self._run_with_reader(reader)
        infos = [m.text for m in result.messages if m.level == 'info']
        self.assertFalse(any('pdftotext read' in t for t in infos), infos)
        self.assertTrue(any("pdftotext failed on page 1" in t and "pypdf read those" in t
                            for t in infos), infos)

    def test_second_reader_exception_does_not_mark_page_failed(self) -> None:
        # L2: the optional reader or the progress printer failing is not an
        # extraction failure of the page.
        squashed = (SQUASHED_LINE + '\n') * 3

        class _Page:
            def extract_text(self, *a, **k):
                if k.get('extraction_mode') == 'layout':
                    raise ValueError('layout boom')
                return squashed

        def boom():
            raise RuntimeError('pdftotext wrapper broke')

        text, how = source_mod._extract_page_text(_Page(), boom)
        self.assertEqual((text, how), (squashed.strip(), 'squashed'))

        def bad_progress(text):
            raise OSError('stdout closed')

        with mock.patch.object(source_mod, '_pdftotext_path', return_value='pdftotext'), \
                mock.patch.object(source_mod.subprocess, 'run',
                                  return_value=mock.Mock(returncode=0, stdout=b'x\f' * 12)):
            pages = source_mod._PdftotextPages(self.pdf, list(range(1, 13)),
                                               progress=bad_progress)
            self.assertEqual(pages.page(1), 'x')

    def test_progress_is_announced_before_a_long_pdftotext_run(self) -> None:
        # L-1: the engine never prints, but a caller can hear about a long
        # pdftotext run BEFORE it starts.
        events: list = []

        def fake_run(args, **kw):
            events.append('run')
            return mock.Mock(returncode=0, stdout=b'\f' * 12)

        with mock.patch.object(source_mod, '_pdftotext_path', return_value='pdftotext'), \
                mock.patch.object(source_mod.subprocess, 'run', side_effect=fake_run):
            pages = source_mod._PdftotextPages(self.pdf, list(range(1, 13)),
                                               progress=lambda text: events.append(text))
            pages.page(3)
            short = source_mod._PdftotextPages(self.pdf, [1, 2],
                                               progress=lambda text: events.append(text))
            short.page(1)
        self.assertEqual(len(events), 3)
        self.assertIn('pdftotext', events[0])
        self.assertIn('1-12', events[0])
        self.assertEqual(events[1:], ['run', 'run'])      # a short block is not announced

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_pdftotext_dehyphenated_reading_is_not_reordered(self) -> None:
        # Third review P2-2: pdftotext joins words hyphenated at a line end
        # ("Hart-" / "ley" -> "Hartley"). That is not a change of text.
        lines = ['Thomas Hart-', 'ley, son of Edward Hart-', 'ley and Mary Ann Day, was bap-',
                 'tized at Halifax in 1812 and emi-', 'grated with his par-',
                 'ents to Michigan in 1836, where he pur-', 'chased land in Lyon Town-',
                 'ship and married Sarah Bell, daugh-', 'ter of Thomas Bell, a blacksmith.']
        squashed = '\n'.join(line.replace(' ', '') for line in lines)
        rejoined = re.sub(r'-\n', '', '\n'.join(lines))
        reader, _ = self._fake_reader(squashed, ValueError('layout boom'))
        with mock.patch.object(source_mod, '_pdftotext_path', return_value='pdftotext'), \
                mock.patch.object(source_mod._PdftotextPages, 'page', return_value=rejoined):
            result = self._run_with_reader(reader)
        self.assertEqual(result.data['reordered_pages'], [])
        self.assertEqual(result.data['pdftotext_pages'], [1])
        self.assertEqual(result.exit_code, EXIT_CLEAN, [m.text for m in result.messages])
        self.assertTrue(source_mod._same_reading(squashed, rejoined))

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_narrow_column_no_space_layer_still_warns(self) -> None:
        # Third review P1-1: in a 110 pt column every line of a no-space layer
        # is one piece of 16-40 letters - under the 40-letter guard - and
        # every reader agrees on it. Agreement must not make that plain.
        body = _squeezed_block(ENGLISH_WORDS, left=60, width=110, tz=60, gap=0)
        self.pdf.write_bytes(_minimal_pdf(
            [_raw('BT /F1 11 Tf 3 Tr 60 Tz\n' + body + 'ET')], base_font='Times-Roman'))
        result = self._run()
        self.assertEqual(result.data['squashed_pages'], [1])
        self.assertEqual(result.exit_code, EXIT_WARNINGS)

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_squeezed_pages_in_other_languages_and_formats_take_layout(self) -> None:
        # Third review P2-1: correct layout readings of Polish, Czech, French,
        # Dutch and Norwegian records (ASCII-folded - the test font has no
        # other glyphs), a Roman-numeral register, citation abbreviations and
        # a hyphenated page, all full of short words no word list knows.
        for name, text in OTHER_TEXTS.items():
            with self.subTest(name):
                self.tearDown()
                self.setUp()
                body = _squeezed_block(text.split(), left=60, width=490, tz=60, gap=3)
                self.pdf.write_bytes(_minimal_pdf(
                    [_raw('BT /F1 11 Tf 3 Tr 60 Tz\n' + body + 'ET')],
                    base_font='Times-Roman'))
                result = self._run()
                self.assertEqual(result.exit_code, EXIT_CLEAN,
                                 [m.text for m in result.messages])
                self.assertEqual(result.data['layout_pages'], [1])

    # -- #281 review: squeezed two-column pages ----------------------------------

    def _two_column_pdf(self) -> None:
        body = (_squeezed_block(LEFT_WORDS * 2, left=72, width=220, tz=60, gap=3)
                + _squeezed_block(RIGHT_WORDS * 2, left=320, width=220, tz=60, gap=3))
        self.pdf.write_bytes(_minimal_pdf(
            [_raw('BT /F1 11 Tf 3 Tr 60 Tz\n' + body + 'ET')], base_font='Times-Roman'))

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_two_column_page_without_pdftotext_warns_with_the_real_reason(self) -> None:
        self._two_column_pdf()
        result = self._run()
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result.data['reordered_pages'], [1])
        warnings = ' '.join(m.text for m in result.messages if m.level == 'warning')
        self.assertIn('changed their order', warnings)
        self.assertNotIn('could not recover the spaces', warnings)
        self.assertIn('pdftotext', warnings)            # names the optional reader
        text = (self.pdf.parent / f'county-history-extracted-text_{SID.lower()}.md').read_text(
            encoding='utf-8')
        # the column-by-column default reading is kept, never the interleaved one
        self.assertIn('ThomasHartley,sonofEdward', text)

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    @unittest.skipUnless(shutil.which('pdftotext'), 'pdftotext (poppler) not installed')
    def test_two_column_page_is_recovered_by_pdftotext(self) -> None:
        self._two_column_pdf()
        with mock.patch.object(source_mod, '_pdftotext_path',
                               return_value=shutil.which('pdftotext')):
            result = self._run()
        self.assertEqual(result.exit_code, EXIT_CLEAN, [m.text for m in result.messages])
        self.assertEqual(result.data['pdftotext_pages'], [1])
        text = (self.pdf.parent / f'county-history-extracted-text_{SID.lower()}.md').read_text(
            encoding='utf-8')
        self.assertIn('Thomas Hartley, son of Edward', text)
        self.assertIn('Mary Ann Brown, daughter of Josiah', text)

    def test_pdftotext_absent_or_failing_is_no_reading(self) -> None:
        self.assertIsNone(source_mod._pdftotext_reading(self.pdf, 1))   # absent
        with mock.patch.object(source_mod, '_pdftotext_path',
                               return_value=str(self.root / 'no-such-binary')):
            self.assertIsNone(source_mod._pdftotext_reading(self.pdf, 1))

    # -- #281 re-review: readers compared, not absolute counts ------------------

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_no_space_text_layer_both_readers_agree_still_warns(self) -> None:
        # P2-A: a text layer with no spaces at all reads the same in layout
        # mode. The readers agreeing must not turn that into "plain".
        squashed = (SQUASHED_LINE + '\n') * 3
        reader, _ = self._fake_reader(squashed, squashed)
        result = self._run_with_reader(reader)
        self.assertEqual(result.data['squashed_pages'], [1])
        self.assertEqual(result.exit_code, EXIT_WARNINGS)

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_squeezed_mc_surname_page_takes_layout_reading(self) -> None:
        # P2-B: McDonald and DeWitt look glued in every reading. Layout mode
        # spaces the page correctly and must be accepted for it.
        body = _squeezed_block(MC_TEXT.split(), left=72, width=468, tz=60, gap=3)
        self.pdf.write_bytes(_minimal_pdf(
            [_raw('BT /F1 11 Tf 3 Tr 60 Tz\n' + body + 'ET')], base_font='Times-Roman'))
        result = self._run()
        self.assertEqual(result.exit_code, EXIT_CLEAN, [m.text for m in result.messages])
        self.assertEqual(result.data['layout_pages'], [1])
        text = (self.pdf.parent / f'county-history-extracted-text_{SID.lower()}.md').read_text(
            encoding='utf-8')
        self.assertIn('Angus McDonald married Mary MacArthur', text)
        self.assertIn('DeWitt and VanBuren families', text)

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_clean_mc_page_with_superscripts_is_plain(self) -> None:
        # P2-B: an ordinary page of Mc names with footnote figures set after
        # its lines, and a library stamp printed up the margin, trips the
        # eager trigger; layout mode leaves every name whole (the figures
        # land against the line ends, the stamp in among the lines), so the
        # page is plain - no warning.
        words, lines, line = MC_TEXT.split() * 2, [], ''
        for word in words:
            if line and _times_width(line + ' ' + word, 11) > 300:
                lines.append(line)
                line = word
            else:
                line = f'{line} {word}'.strip()
        lines.append(line)
        body, y = '', 720
        for text in lines:
            x_sup = 72 + _times_width(text, 11)
            body += (f'/F1 11 Tf 1 0 0 1 72 {y} Tm ({text}) Tj\n'
                     f'/F1 6 Tf 1 0 0 1 {x_sup:.2f} {y + 5} Tm (12) Tj\n')
            y -= 15
        body += ('/F1 8 Tf 0 1 -1 0 40 300 Tm '
                 '(Scanned from the library of the University of Michigan) Tj\n')
        self.pdf.write_bytes(_minimal_pdf([_raw('BT\n' + body + 'ET')],
                                          base_font='Times-Roman'))
        self._assert_plain_dump(self._run())

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_squeezed_register_and_index_pages_are_recovered(self) -> None:
        # P2-C: short-pieced pages, spaced correctly by layout mode, must not
        # be refused for their short pieces.
        register = ' '.join(f'{i}. John Hartley, b. {d} May 18{20 + i}; d. 4 Jun 18{60 + i}; '
                            f'm. 3 Oct 18{45 + i} Mary Day; ch.: Ann, b. {d}; Eli, b. 18{50 + i}.'
                            for i, d in zip(range(1, 6), (3, 9, 12, 1, 5)))
        index = ' '.join(f'{n}, {i}., {p}, {p + 3},' for n, i, p in
                         [('Abbott', 'J', 12), ('Ames', 'R', 45), ('Bell', 'T', 67),
                          ('Cox', 'A', 8), ('Day', 'M', 19), ('Fox', 'E', 23),
                          ('Gay', 'W', 31), ('Hart', 'T', 4), ('Hay', 'J', 55),
                          ('Ives', 'S', 61), ('Kay', 'B', 72), ('Lee', 'C', 88)])
        pages = [_raw('BT /F1 11 Tf 3 Tr 60 Tz\n'
                      + _squeezed_block(text.split(), left=72, width=468, tz=60, gap=3)
                      + 'ET') for text in (register, index)]
        self.pdf.write_bytes(_minimal_pdf(pages, base_font='Times-Roman'))
        result = self._run()
        self.assertEqual(result.exit_code, EXIT_CLEAN, [m.text for m in result.messages])
        self.assertEqual(sorted(result.data['layout_pages']
                                + result.data['pdftotext_pages']), [1, 2])
        text = (self.pdf.parent / f'county-history-extracted-text_{SID.lower()}.md').read_text(
            encoding='utf-8')
        self.assertIn('1. John Hartley, b. 3 May 1821; d. 4 Jun 1861;', text)
        self.assertIn('Abbott, J., 12, 15, Ames, R., 45, 48,', text)

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_dry_run_pdftotext_hint_has_no_delete_advice(self) -> None:
        # L-a: a dry run writes no dump, so it must not say to delete one.
        squashed = (SQUASHED_LINE + '\n') * 3
        reader, _ = self._fake_reader(squashed, squashed)
        dry = self._run_with_reader(reader, dry_run=True)
        dry_warnings = ' '.join(m.text for m in dry.messages if m.level == 'warning')
        self.assertIn('pdftotext', dry_warnings)
        self.assertNotIn('delete the dump', dry_warnings)
        live = self._run_with_reader(reader)
        live_warnings = ' '.join(m.text for m in live.messages if m.level == 'warning')
        self.assertIn('delete the dump', live_warnings)

    def test_dump_source_pdf_reads_names_with_spaces_and_dots(self) -> None:
        # L-b: the name is read up to the fixed text that follows it.
        dump = self.root / 'dump.md'
        for name, tail in (('my scan 2.pdf', ''), ('a.pdf.backup.pdf', ' (copy: b)')):
            dump.write_text(f'# Extracted text - X [{SID}]\n\nMachine-extracted from the '
                            f'embedded text layer of {name}{tail} by `fha source extract`. '
                            'A derived working copy.\n', encoding='utf-8')
            self.assertEqual(source_mod._dump_source_pdf(dump), name)
        self.assertIsNone(source_mod._dump_source_pdf(self.root / 'missing.md'))

    def test_pdftotext_runs_once_per_block_of_selected_pages(self) -> None:
        # L-d / third review L-1: one process per contiguous block of the
        # selected pages, split on the form feed, one budget for the run.
        outputs = {('3', '4'): b'page three\fpage four\f', ('9', '9'): b'page nine\f'}

        def fake_run(args, **kw):
            key = (args[args.index('-f') + 1], args[args.index('-l') + 1])
            return mock.Mock(returncode=0, stdout=outputs[key])

        with mock.patch.object(source_mod, '_pdftotext_path', return_value='pdftotext'), \
                mock.patch.object(source_mod.subprocess, 'run', side_effect=fake_run) as run:
            pages = source_mod._PdftotextPages(self.pdf, [3, 4, 9])
            self.assertEqual(pages.page(3), 'page three')
            self.assertEqual(pages.page(4), 'page four')
            self.assertEqual(pages.page(9), 'page nine')
            self.assertIsNone(pages.page(5))           # not selected: never read
        self.assertEqual(run.call_count, 2)            # pages 5-8 were never asked for
        self.assertEqual(pages.budget, 60 + 2 * 3)     # by the pages selected, not the span
        # A page count that does not match cannot be split safely.
        done = mock.Mock(returncode=0, stdout=b'only one page\f')
        with mock.patch.object(source_mod, '_pdftotext_path', return_value='pdftotext'), \
                mock.patch.object(source_mod.subprocess, 'run', return_value=done):
            self.assertIsNone(source_mod._PdftotextPages(self.pdf, [3, 4]).page(3))
        # A spent budget reads nothing more.
        with mock.patch.object(source_mod, '_pdftotext_path', return_value='pdftotext'), \
                mock.patch.object(source_mod.subprocess, 'run') as run:
            spent = source_mod._PdftotextPages(self.pdf, [3])
            spent.seconds = spent.budget
            self.assertIsNone(spent.page(3))
        run.assert_not_called()

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_copy_label_is_added_to_a_dump_name_that_lacks_it(self) -> None:
        # Drift 1: `{slug}[-{copy}]-extracted-text`, even when the PDF's own
        # name does not carry the label.
        second = f'scan2_{SID.lower()}.pdf'
        self._write_record(
            f'files:\n  - file: documents/book/county-history_{SID.lower()}.pdf\n'
            '    is_primary: true\n'
            f'  - file: documents/book/{second}\n    is_primary: true\n    copy: b\n')
        self.pdf.write_bytes(_minimal_pdf(['Own scan text']))
        (self.pdf.parent / second).write_bytes(_minimal_pdf(['Copy b text']))
        self.assertEqual(self._run(copy_label='b').exit_code, EXIT_CLEAN)
        self.assertTrue((self.pdf.parent / f'scan2-b-extracted-text_{SID.lower()}.md').exists())

    # -- #281 review: what a second reading may change ---------------------------

    def test_same_reading_accepts_only_a_change_of_spacing(self) -> None:
        same = source_mod._same_reading
        lines = ['Thomas Hartley, son of Edward, b. May 9, 1854, came from Marsh Creek',
                 'Mary Ann Brown, daughter of Josiah, married Thomas in 1876 there',
                 'Their son William was baptized at the Methodist church in 1877',
                 'Edward Hartley died at Marsh Creek on the first of March, 1881']
        default = '\n'.join(line.replace(' ', '') for line in lines)
        self.assertTrue(same(default, '\n'.join(lines)))
        # A ligature glyph is the same letters.
        self.assertTrue(same('Thebaptizedinfantﬁrst' * 8,
                             'The baptized infant first ' * 8))
        # Dropped digits are lost evidence, however few.
        self.assertFalse(same(default, '\n'.join(lines).replace(', 1881', '')))
        self.assertFalse(same(default, '\n'.join(lines).replace(' 1854', ' 185')))
        # A block of whole lines in another order.
        self.assertFalse(same(default, '\n'.join(lines[2:] + lines[:2])))
        # Whether a reading still breaks a word where the layer kerns is not
        # judged at all (#281, owner's call): "Hart ley" beats "ThomasHartley".

    def test_same_reading_accepts_short_pieced_pages_and_itself(self) -> None:
        # #281 re-review P2-C: correct pages of short pieces must pass.
        register = ' '.join(f'{i}. John Hartley, b. 3 May 18{20 + i}; d. 4 Jun 18{60 + i}; '
                            f'm. 3 Oct 18{45 + i} Mary Day; ch.: Ann, b. 3; Eli, b. 18{50 + i}.'
                            for i in range(1, 6))
        index = ' '.join(f'{n}, {i}., {p}, {p + 3},' for n, i, p in
                         [('Abbott', 'J', 12), ('Ames', 'R', 45), ('Bell', 'T', 67),
                          ('Cox', 'A', 8), ('Day', 'M', 19), ('Fox', 'E', 23)] * 3)
        german = ('Johann an der Weide und Maria von der Au, beide aus dem Ort an der '
                  'Oder, sind am 3. Mai 1801 in der Kirche zu St. Marien getraut. ') * 3
        table = '\n'.join(' '.join(str(x) for x in (1850 + i, 12 + i, 3, 4, i % 10, 5, 6))
                          for i in range(20))
        for text in (register, index, german, table):
            self.assertTrue(source_mod._same_reading(text, text), text[:40])
            self.assertTrue(source_mod._same_reading(text.replace(' ', ''), text), text[:40])

    def test_words_run_together_check(self) -> None:
        check = source_mod._words_run_together
        self.assertTrue(check('\n'.join([SQUASHED_LINE] * 3)))
        # Ordinary text, however it is laid out, never trips it.
        prose = ('In the year 1854 Thomas Hartley, son of Edward, came also from '
                 'Marsh Creek and took up a farm of one hundred and sixty acres '
                 'in what is now Washington township. ') * 5
        self.assertFalse(check(prose))
        index_page = '\n'.join(['Hartley, Thomas 212', 'Hartley, Edward 198',
                                'Wisniewska, Anna 44'] * 10)
        self.assertFalse(check(index_page))
        self.assertFalse(check('\n'.join(['Hartley'] * 40)))   # one name per line
        german = ('Die Familie wanderte im Jahre 1854 aus dem Großherzogtum Baden '
                  'nach Amerika aus und ließ sich in der Gemeinde Marsh Creek nieder. ') * 4
        self.assertFalse(check(german))
        contents = '\n'.join(['Chapter I. Early Settlers ........................ 1',
                              'Chapter II. Marsh Creek ......................... 23',
                              'Introduction........................................1'] * 4)
        self.assertFalse(check(contents))
        urls = '\n'.join(f'https://www.familysearch.org/ark:/61903/1:1:QV{i:04d}-ZZ9'
                         for i in range(10))
        self.assertFalse(check(urls))
        emails = '\n'.join(f'margaret.hartley.genealogy{i}@familyhistorysociety.org.uk'
                           for i in range(10))
        self.assertFalse(check(emails))
        form = '\n'.join(['Name ' + '_' * 40, 'Date of birth ' + '_' * 30,
                          'Place ' + '_' * 40, 'Father ' + '_' * 40] * 2)
        self.assertFalse(check(form))
        # A short page is left alone, even with no spaces at all.
        self.assertFalse(check(SQUASHED_LINE))
        self.assertFalse(check('INDEX'))
        self.assertFalse(check(''))
        # Pages in scripts written without spaces are spaced as they stand.
        self.assertFalse(check('王大明生于一八五' * 20))
        self.assertFalse(check('བོད་ཀྱི་ལོ་རྒྱུས་ཀྱི་དེབ་ཐེར་' * 10))
        # Squeezed only in part: some run-together lines amid good prose.
        self.assertTrue(check(prose + '\n' + (SQUASHED_LINE + '\n') * 15))
        # Kerning read as gaps: short pieces, but glued.
        self.assertTrue(check(('Tho masHart ley,sonofEdw ard,b.May9,18 54,ca meal '
                               'sofr omMa rshCr eek\n') * 4))

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
            (self.pdf.parent / f'county-history-extracted-text_{SID.lower()}.md').exists())
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
        out = self.pdf.parent / f'county-history-extracted-text_{SID.lower()}.md'
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
            '    is_primary: true\n')
        self.pdf.write_bytes(_minimal_pdf(['Hartley arrived 1854']))
        result = self._run()
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        entry = read_record(self.record)['meta']['files'][-1]
        self.assertEqual(entry['role'], 'extracted-text')
        self.assertEqual(entry['file'],
                         f'documents/book/county-history-extracted-text_{SID.lower()}.md')

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
