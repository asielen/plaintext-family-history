"""A source id retired by a merge still names the survivor (#300).

When two source records are merged by hand, the survivor keeps the retired
S-id in its `aliases:`. Two places treated that id as a dead end:

  * lint reported a `[[S-old]]` link as an E004 orphan; it is now W107, the
    gradual-cleanup warning a link to a merged person already gets.
  * `fha process --more` refused a file named or keyworded with the retired
    id as "filed for another source"; it now attaches it to the survivor,
    renamed into its grammar and re-keyworded, with the old name kept as
    `original_filename:`.

Run: python -m pytest tests/test_source_aliases.py -q   (from the repo root)
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))

import index
import lint
import process
from _lib import EXIT_CLEAN, EXIT_ERRORS, load_fha_yaml
from test_process_inbox_filing import JPEG, PDF, InboxFilingTestCase

EXAMPLE = ROOT / 'example-archive'
CENSUS = 'sources/census/1880-census-fairview-hartley-household_S-4f5f215e60.md'
NOTICE = 'sources/newspapers/1871-marriage-notice-fairview_S-ea61339378.md'
OLD = 'S-zz00000001'


class RetiredSourceLinkLintTests(unittest.TestCase):
    """A `[[S-old]]` held in a survivor's aliases is W107, not E004."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / 'arc'
        shutil.copytree(EXAMPLE, self.root)
        notice = self.root / NOTICE
        notice.write_text(notice.read_text(encoding='utf-8')
                          + f'\nSee also [[{OLD}]].\n', encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _findings(self) -> list:
        config = load_fha_yaml(self.root)
        index.build_index(self.root, config)
        return [m for m in lint.run_lint(self.root, config).messages
                if OLD.lower() in str(m).lower()]

    def _add_alias(self) -> None:
        census = self.root / CENSUS
        text = census.read_text(encoding='utf-8')
        old = 'aliases: [S-4f5f215e60, C-d22ce0ac77]'
        self.assertIn(old, text)
        census.write_text(text.replace(old, f'aliases: [S-4f5f215e60, C-d22ce0ac77, {OLD}]'),
                          encoding='utf-8')

    def test_link_to_an_alias_is_a_cleanup_warning(self) -> None:
        self._add_alias()
        codes = [m.code for m in self._findings()]
        self.assertEqual(codes, ['W107'])

    def test_link_to_an_unknown_id_is_still_an_orphan(self) -> None:
        codes = [m.code for m in self._findings()]
        self.assertEqual(codes, ['E004'])


def _embed_with_removal(store):
    """The fake store's embed, also taking `remove_keywords` the way the real
    exiftool seam does."""
    def embed(file_path, s_id, extra_keywords=None, *, backup=None, remove_keywords=None):
        key = str(file_path)
        kept = [kw for kw in store.keywords.get(key, []) if kw not in (remove_keywords or [])]
        store.keywords[key] = kept
        return store.embed(file_path, s_id, extra_keywords, backup=backup)
    return embed


class MoreAcceptsARetiredIdTests(InboxFilingTestCase):
    """`--more` onto the record that holds the alias attaches and re-keywords."""

    def setUp(self) -> None:
        super().setUp()
        process._run_exiftool_embed_source = _embed_with_removal(self.store)

    def _letter_with_alias(self) -> tuple[Path, Path, str]:
        primary, record, sid = self._file_letter()
        text = record.read_text(encoding='utf-8')
        record.write_text(text.replace(f'aliases: [{sid}, ', f'aliases: [{sid}, {OLD}, ', 1),
                          encoding='utf-8')
        self.assertIn(OLD, record.read_text(encoding='utf-8'))
        return primary, record, sid

    def test_file_named_for_the_alias_joins_the_survivor(self) -> None:
        primary, record, sid = self._letter_with_alias()
        page = self._put(self.inbox / 'Letters' / f'scan_{OLD}.jpg', JPEG)

        rc, out, err = self._run(str(primary), '--more', str(page), 'page-2')

        self.assertEqual(rc, EXIT_CLEAN, err)
        filed = self.docs / 'letter' / f'postcard-from-aunt-1908-page-2_{sid}.jpg'
        self.assertTrue(filed.is_file(), self._files(self.docs))
        entry = self._files_entries(record)[1]
        self.assertEqual(entry['file'], f'documents/letter/{filed.name}')
        self.assertEqual(entry['original_filename'], f'scan_{OLD}.jpg')
        self.assertEqual(self.store.keywords[str(filed)], [f'SOURCE: {sid}'])

    def test_file_keyworded_for_the_alias_is_re_keyworded(self) -> None:
        primary, record, sid = self._letter_with_alias()
        page = self._put(self.inbox / 'Letters' / 'renamed-by-another-app.jpg', JPEG)
        self.store.keywords[str(page)] = [f'SOURCE: {OLD}']
        # The copy filed out of the inbox carries the same embedded keyword.
        original_read = self.store.read
        self.store.read = lambda p: (original_read(p) if str(p) in self.store.keywords
                                     else [f'SOURCE: {OLD}'])
        process._run_exiftool_read_keywords = self.store.read

        rc, out, err = self._run(str(primary), '--more', str(page), 'page-2')

        self.assertEqual(rc, EXIT_CLEAN, err)
        filed = self.docs / 'letter' / f'postcard-from-aunt-1908-page-2_{sid}.jpg'
        self.assertTrue(filed.is_file(), self._files(self.docs))
        self.assertEqual(self.store.keywords[str(filed)], [f'SOURCE: {sid}'])

    def test_an_alias_whose_record_still_exists_is_refused(self) -> None:
        # Part-way through a hand merge: the other letter still has its record,
        # so its file is still its own.
        primary, record, sid = self._file_letter()
        other = self._put(self.inbox / 'Letters' / 'tax-receipt.pdf', b'%PDF-1.4 other')
        rc, _out, err = self._run(str(other), '--type', 'letter', '--slug', 'tax-receipt')
        self.assertEqual(rc, EXIT_CLEAN, err)
        other_filed = next((self.docs / 'letter').glob('tax-receipt_S-*.pdf'))
        other_sid = other_filed.stem.split('_')[-1]
        text = record.read_text(encoding='utf-8')
        record.write_text(text.replace(f'aliases: [{sid}, ', f'aliases: [{sid}, {other_sid}, ', 1),
                          encoding='utf-8')

        rc, _out, err = self._run(str(primary), '--more', str(other_filed), 'page-2')

        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('Nothing was changed', err)
        self.assertTrue(other_filed.is_file())

    def test_a_file_already_listed_under_its_old_name_is_left_alone(self) -> None:
        primary, record, sid = self._letter_with_alias()
        old_page = self._put(self.docs / 'letter' / f'old-page_{OLD}.pdf', PDF)
        text = record.read_text(encoding='utf-8')
        record.write_text(text.replace(
            '    original_filename: scan-0412.pdf\n',
            '    original_filename: scan-0412.pdf\n'
            f'  - file: documents/letter/{old_page.name}\n    role: page-2\n', 1),
            encoding='utf-8')
        before = record.read_text(encoding='utf-8')

        rc, _out, err = self._run(str(primary), '--more', str(old_page), 'page-2')

        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('already listed', err)
        self.assertTrue(old_page.is_file())
        self.assertEqual(record.read_text(encoding='utf-8'), before)

    def test_an_id_that_is_not_an_alias_is_still_refused(self) -> None:
        primary, _record, _sid = self._file_letter()
        page = self._put(self.inbox / 'Letters' / f'scan_{OLD}.jpg', JPEG)

        rc, _out, err = self._run(str(primary), '--more', str(page), 'page-2')

        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('different S-id', err)


if __name__ == '__main__':
    unittest.main()
