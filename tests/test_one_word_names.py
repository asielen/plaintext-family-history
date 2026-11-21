"""
test_one_word_names.py: "surname not known" and "no surname" stay different
(SPEC §13).

A one-word name ("Cher", "Caesar") says nothing about a surname, so it files
as `unknown__cher`, the same from every tool that names a person: `fha person
new`, `fha person rename`, `fha stubs --from-names`, `fha gedcom import` and
`fha lint --fix-ids`. A person who has no surname at all files as `__cher`,
and only when that is said: `--surname ""`, a `surname: ""` line, or a GEDCOM
NAME that marks the surname empty (`Cher //`).

W138 reads both shapes for what they mean, and the files earlier versions
wrote (`__cher` from `person new`, `cher__unknown` from `--fix-ids`) draw no
warning, so `--fix-surnames` never gives anyone the surname "Cher".
"""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import gedcom_import
import lint
import person
from _lib import load_fha_yaml, read_record, stub_filename


class _Archive(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('roots:\n  documents: documents\n',
                                            encoding='utf-8')
        (self.root / 'people' / 'stubs').mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _names(self):
        return sorted(p.name.rsplit('_p-', 1)[0] for p in (self.root / 'people').rglob('*.md'))

    def _w138(self, **kw):
        res = lint.run_lint(self.root, {}, **kw)
        return res, [m for m in res.messages if m.code == 'W138']


class EveryToolAgreesTests(_Archive):

    def test_person_new_files_one_word_as_surname_unknown(self):
        person.run_new(self.root, 'Cher')
        person.run_new(self.root, 'Caesar', surname='')
        self.assertEqual(self._names(), ['__caesar', 'unknown__cher'])
        self.assertEqual(self._w138()[1], [])

    def test_stub_filename_used_by_stubs_from_names(self):
        self.assertEqual(stub_filename('Cher', 'p-aaaaaaaaaa'), 'unknown__cher_p-aaaaaaaaaa.md')
        self.assertEqual(stub_filename('Cher', 'p-aaaaaaaaaa', surname=''),
                         '__cher_p-aaaaaaaaaa.md')

    def test_fix_ids_files_one_word_as_person_new_does(self):
        people = self.root / 'people'
        (people / 'Cher.md').write_text('---\nname: Cher\nliving: false\n---\n\n# Cher\n',
                                        encoding='utf-8')
        (people / 'Caesar.md').write_text(
            '---\nname: Caesar\nsurname: ""\nliving: false\n---\n\n# Caesar\n',
            encoding='utf-8')
        lint.run_lint(self.root, {}, fix_ids=True)
        self.assertEqual(self._names(), ['__caesar', 'unknown__cher'])
        res, found = self._w138(fix_surnames=True)
        self.assertEqual(found, [])
        for path in people.rglob('*.md'):
            self.assertNotIn('surname: Cher', path.read_text(encoding='utf-8'))

    def test_rename_to_one_word_files_as_surname_unknown(self):
        res = person.run_new(self.root, 'Cher Bradford')
        pid = res.data['person_id']
        res = person.run_rename(self.root, pid, 'Cher')
        self.assertEqual(res.exit_code, 0, res.messages)
        self.assertEqual(self._names(), ['unknown__cher'])

    def _import(self, name_line):
        ged = self.root / 'one.ged'
        ged.write_text('0 HEAD\n1 CHAR UTF-8\n0 @I1@ INDI\n' f'1 NAME {name_line}\n'
                       '1 SEX F\n0 TRLR\n', encoding='utf-8')
        config = load_fha_yaml(self.root, strict=True)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            gedcom_import.run_import(self.root, config, ged, apply=True)
        return next((self.root / 'people' / 'stubs').glob('*.md'))

    def test_gedcom_name_without_a_surname_files_as_surname_unknown(self):
        path = self._import('Cher')
        self.assertTrue(path.name.startswith('unknown__cher_'), path.name)
        self.assertNotIn('surname', read_record(path)['meta'])

    def test_gedcom_name_marking_no_surname_files_with_the_empty_slot(self):
        path = self._import('Cher //')
        self.assertTrue(path.name.startswith('__cher_'), path.name)
        self.assertEqual(read_record(path)['meta'].get('surname'), '')


class OlderFilesStayQuietTests(_Archive):

    def test_files_earlier_versions_wrote_draw_no_w138(self):
        stubs = self.root / 'people' / 'stubs'
        for stem, pid in (('__cher', 'p-aaaaaaaaaa'), ('cher__unknown', 'p-bbbbbbbbbb'),
                          ('unknown__cher', 'p-cccccccccc'), ('__roy_jr', 'p-dddddddddd')):
            name = 'Roy Jr' if 'roy' in stem else 'Cher'
            (stubs / f'{stem}_{pid}.md').write_text(
                f'---\nid: {pid}\nname: {name}\nliving: false\ntier: stub\n---\n\n# {name}\n',
                encoding='utf-8')
        res, found = self._w138(fix_surnames=True)
        self.assertEqual([m.text for m in found], [])
        self.assertFalse(any('surname' in p for p in res.data.get('progress', [])))

    def test_a_real_surname_mismatch_is_still_reported(self):
        stubs = self.root / 'people' / 'stubs'
        (stubs / 'garcia_lopez__maria_jose_p-aaaaaaaaaa.md').write_text(
            '---\nid: p-aaaaaaaaaa\nname: Maria Jose Garcia Lopez\nliving: false\n'
            'tier: stub\n---\n', encoding='utf-8')
        self.assertEqual(len(self._w138()[1]), 1)


if __name__ == '__main__':
    unittest.main()
