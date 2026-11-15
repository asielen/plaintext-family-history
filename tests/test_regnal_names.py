"""
test_regnal_names.py - a regnal number is not a surname (issue #292).

"Henry VII" used to file as `vii__henry_P-….md` and export `1 NAME Henry
/VII/`: the shared suffix rule (`_lib.strip_generational_suffix`) knew only
Jr, Sr, II, III, IV and V. VI to XXX now count as a suffix when typed in
capitals in a name that is not itself all capitals, so the real surnames
spelled like numerals ("Wei Li", "Jin Xi", a record typed "WEI LI") keep
their split. `--surname ""` now means "no surname" (SPEC §13's
`__caesar_P-….md` form) instead of being ignored.

Expected values come from the issue's regression list.
"""

import contextlib
import io
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import gedcom
import gedcom_import
import index
import lint
import person
from _lib import (
    bracket_given_name,
    find_people_by_name,
    strip_generational_suffix,
    stub_filename,
)

PID = 'p-0000000001'


def _slots(filename: str) -> str:
    return filename[:-len(f'_{PID}.md')]


class RegnalNumberTests(unittest.TestCase):

    def test_regnal_numbers_ride_in_the_given_slot(self) -> None:
        for name, expected in (('Henry VII', '__henry_vii'), ('Henry VIII', '__henry_viii'),
                               ('Louis XIV', '__louis_xiv'), ('Henry VI', '__henry_vi'),
                               ('John Smith VII', 'smith__john_vii')):
            with self.subTest(name=name):
                self.assertEqual(_slots(stub_filename(name, PID)), expected)

    def test_numeral_shaped_surnames_keep_their_split(self) -> None:
        for name, expected in (('Wei Li', 'li__wei'), ('Jin Xi', 'xi__jin'),
                               ('WEI LI', 'li__wei'), ('JIN XI', 'xi__jin'),
                               ('Ana Vi', 'vi__ana')):
            with self.subTest(name=name):
                self.assertEqual(_slots(stub_filename(name, PID)), expected)

    def test_existing_suffixes_unchanged(self) -> None:
        self.assertEqual(_slots(stub_filename('Henry V', PID)), '__henry_v')
        self.assertEqual(_slots(stub_filename('Roy Eugene Dodson Jr', PID)),
                         'dodson__roy_eugene_jr')

    def test_every_consumer_shares_the_rule(self) -> None:
        self.assertEqual(strip_generational_suffix(['Louis', 'XIV']), (['Louis'], 'xiv'))
        self.assertEqual(lint._person_filename_parts('Henry VII', 'x'), ('', 'henry_vii'))
        self.assertEqual(gedcom_import._parse_gedcom_name('Henry VII')[1:], ('Henry VII', ''))
        self.assertEqual(gedcom._gedcom_name('Henry VII', None), ('Henry // VII', 'VII'))

    def test_bracket_surname_skips_esq_and_regnal_numbers(self) -> None:
        # The second suffix list (_NAME_SUFFIXES behind `_surname_of`) is
        # folded into the shared rule: V and the regnal numbers now count.
        self.assertEqual(bracket_given_name('Mrs. John Smith V'), 'Mrs. Smith')
        self.assertEqual(bracket_given_name('Mrs. John Smith Esq'), 'Mrs. Smith')

    def test_empty_surname_override_means_no_surname(self) -> None:
        self.assertEqual(_slots(stub_filename('Henry Tudor', PID, surname='')),
                         '__henry_tudor')
        self.assertEqual(_slots(stub_filename('Henry Tudor', PID, surname='  ')),
                         '__henry_tudor')

    def test_person_new_cli_empty_surname(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'people' / 'stubs').mkdir(parents=True)
            (root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                code = person._standalone_main(
                    ['new', 'Henry Tudor', '--surname', '', '--root', str(root)])
            self.assertEqual(code, 0)
            [f] = [p.name for p in (root / 'people' / 'stubs').glob('*.md')]
            self.assertTrue(f.startswith('__henry_tudor_p-'), f)

    def test_export_and_duplicate_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'people' / 'stubs').mkdir(parents=True)
            (root / 'sources').mkdir()
            (root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
            first = person.run_new(root, 'Henry VII', sex='M')
            self.assertTrue(first.ok)
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                index.build_index(root, {})
            time.sleep(0.02)
            text = gedcom.run_gedcom(root, None, all_persons=True, include_living=True)['text']
            self.assertIn('1 NAME Henry // VII', text)
            found = find_people_by_name(root, ['Henry VII'])
            self.assertEqual([m['id'] for m in found['Henry VII']], [first.data['person_id']])



class PreSuffixRuleRecordsTests(unittest.TestCase):
    """Records filed before #292 carry the numeral (or Esq) as their filename
    surname. They must still export one suffix, and a same-name rename must
    be able to re-file them (P2-A)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'people' / 'stubs').mkdir(parents=True)
        (self.root / 'sources').mkdir()
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _stub(self, filename: str, name: str, pid: str) -> Path:
        path = self.root / 'people' / 'stubs' / filename
        path.write_text(f'---\nid: {pid}\nname: {name}\nliving: false\ntier: stub\n---\n',
                        encoding='utf-8')
        return path

    def test_gedcom_old_regnal_and_esq_slug_exports_single_suffix(self) -> None:
        self._stub('vii__henry_p-0000000051.md', 'Henry VII', 'p-0000000051')
        self._stub('esq__john_smith_p-0000000052.md', 'John Smith Esq', 'p-0000000052')
        self._stub('xi__jin_p-0000000053.md', 'Jin XI', 'p-0000000053')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            index.build_index(self.root, {})
        time.sleep(0.02)
        text = gedcom.run_gedcom(self.root, None, all_persons=True, include_living=True)['text']
        lines = text.splitlines()
        self.assertIn('1 NAME Henry // VII', lines)
        self.assertIn('1 NAME John /Smith/ Esq', lines)
        self.assertIn('1 NAME Jin // XI', lines)
        self.assertNotIn('/Vii/', text)
        self.assertEqual(gedcom._gedcom_name('Henry VII', 'Vii'), ('Henry // VII', 'VII'))

    def test_rename_same_name_refiles_pre_292_regnal_slug(self) -> None:
        old = self._stub('vii__henry_p-0000000051.md', 'Henry VII', 'p-0000000051')
        out = person.run_rename(self.root, 'P-0000000051', 'Henry VII')
        self.assertEqual(out.data['status'], 'ok', [m.text for m in out.messages])
        self.assertFalse(old.exists())
        self.assertTrue((self.root / 'people' / 'stubs' / '__henry_vii_p-0000000051.md').is_file())
        text = ' '.join(m.text for m in out.messages)
        self.assertIn('re-filed', text)
        self.assertNotIn("-> 'Henry VII'", text)


class SuffixEdgeTests(unittest.TestCase):

    def test_trailing_comma_does_not_hide_the_suffix(self) -> None:
        self.assertEqual(_slots(stub_filename('Henry VII,', PID)), '__henry_vii')
        self.assertEqual(_slots(stub_filename('Roy Dodson Jr.,', PID)), 'dodson__roy_jr')
        self.assertEqual(strip_generational_suffix(['Henry', 'VII,']), (['Henry'], 'vii'))

    def test_help_documents_the_capitalised_surname_escape(self) -> None:
        # "Jinping XI" reads as a regnal number (the Louis XI tradeoff); the
        # documented way out is --surname XI.
        self.assertIn('--surname XI', person._NEW_DESCRIPTION)
        self.assertIn('Esq', person._NEW_DESCRIPTION)
        self.assertEqual(_slots(stub_filename('Jinping XI', PID, surname='XI')), 'xi__jinping')



class ExplicitNumeralSurnameTests(unittest.TestCase):
    """`--surname XI` on "Jinping XI" is the human's decision that XI is the
    surname; the export must keep it, while a pre-#292 `vii__henry` file
    (no recorded decision) still exports one suffix (Codex, PR #285)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'people' / 'stubs').mkdir(parents=True)
        (self.root / 'sources').mkdir()
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _export_lines(self) -> list[str]:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            index.build_index(self.root, {})
        time.sleep(0.02)
        return gedcom.run_gedcom(self.root, None, all_persons=True,
                                 include_living=True)['text'].splitlines()

    def test_explicit_surname_survives_index_and_export(self) -> None:
        made = person.run_new(self.root, 'Jinping XI', surname='XI')
        self.assertTrue(Path(made.data['path']).name.startswith('xi__jinping_p-'))
        (self.root / 'people' / 'stubs' / 'vii__henry_p-0000000051.md').write_text(
            '---\nid: p-0000000051\nname: Henry VII\nliving: false\n---\n', encoding='utf-8')
        lines = self._export_lines()
        self.assertIn('1 NAME Jinping /XI/', lines)
        self.assertIn('1 NAME Henry // VII', lines)

    def test_same_name_rename_keeps_the_recorded_surname(self) -> None:
        made = person.run_new(self.root, 'Jinping XI', surname='XI')
        out = person.run_rename(self.root, made.data['person_id'], 'Jinping XI')
        self.assertEqual(out.data['status'], 'already')
        self.assertTrue(Path(made.data['path']).is_file())

    def test_from_file_records_the_surname_too(self) -> None:
        path = self.root / 'p.tsv'
        path.write_text('Jinping XI\tM\tXI\n', encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(person.run_new_from_file(self.root, path).ok)
        self.assertIn('1 NAME Jinping /XI/', self._export_lines())


    def test_rename_with_surname_records_the_decision_on_an_old_file(self) -> None:
        # An old `xi__jinping` file has no `surname:` line, so it exports as
        # a suffix; `rename --surname XI` with the same name records it.
        path = self.root / 'people' / 'stubs' / 'xi__jinping_p-0000000081.md'
        path.write_text('---\nid: p-0000000081\nname: Jinping XI\nliving: unknown\n---\n',
                        encoding='utf-8')
        out = person.run_rename(self.root, 'P-0000000081', 'Jinping XI', surname='XI')
        self.assertEqual(out.data['status'], 'ok', [m.text for m in out.messages])
        self.assertTrue(path.is_file())
        self.assertIn('surname: XI', path.read_text(encoding='utf-8'))
        self.assertIn('1 NAME Jinping /XI/', self._export_lines())



class NoSurnameOverrideTests(unittest.TestCase):
    """`--surname ""` is a recorded decision too: `surname: ""` in the
    frontmatter, read by the export, and a same-name rename can apply it
    (Codex, PR #285)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'people' / 'stubs').mkdir(parents=True)
        (self.root / 'sources').mkdir()
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _export_lines(self) -> list[str]:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            index.build_index(self.root, {})
        time.sleep(0.02)
        return gedcom.run_gedcom(self.root, None, all_persons=True,
                                 include_living=True)['text'].splitlines()

    def test_person_new_empty_surname_exports_no_surname(self) -> None:
        made = person.run_new(self.root, 'Jon Thorsson', surname='')
        path = Path(made.data['path'])
        self.assertTrue(path.name.startswith('__jon_thorsson_p-'))
        self.assertIn('surname: ""', path.read_text(encoding='utf-8'))
        self.assertIn('1 NAME Jon Thorsson //', self._export_lines())

    def test_from_file_dash_exports_no_surname(self) -> None:
        tsv = self.root / 'p.tsv'
        tsv.write_text('Jon Thorsson\tM\t-\n', encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(person.run_new_from_file(self.root, tsv).ok)
        self.assertIn('1 NAME Jon Thorsson //', self._export_lines())

    def test_same_name_rename_removes_an_inferred_surname(self) -> None:
        made = person.run_new(self.root, 'Henry Tudor')
        old = Path(made.data['path'])
        self.assertTrue(old.name.startswith('tudor__henry_p-'))
        out = person.run_rename(self.root, made.data['person_id'], 'Henry Tudor', surname='')
        self.assertEqual(out.data['status'], 'ok', [m.text for m in out.messages])
        self.assertFalse(old.exists())
        [new] = list((self.root / 'people' / 'stubs').glob('__henry_tudor_p-*.md'))
        text = new.read_text(encoding='utf-8')
        self.assertIn('name: Henry Tudor', text)
        self.assertIn('surname: ""', text)
        self.assertIn('1 NAME Henry Tudor //', self._export_lines())
        # The recorded no-surname decision holds on a later same-name rename.
        again = person.run_rename(self.root, made.data['person_id'], 'Henry Tudor')
        self.assertEqual(again.data['status'], 'already')

    def test_rename_with_a_surname_replaces_the_empty_one(self) -> None:
        made = person.run_new(self.root, 'Henry Tudor', surname='')
        out = person.run_rename(self.root, made.data['person_id'], 'Henry Tudor',
                                surname='Tudor')
        self.assertEqual(out.data['status'], 'ok', [m.text for m in out.messages])
        [new] = list((self.root / 'people' / 'stubs').glob('tudor__henry_p-*.md'))
        self.assertIn('surname: Tudor', new.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
