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


if __name__ == '__main__':
    unittest.main()
