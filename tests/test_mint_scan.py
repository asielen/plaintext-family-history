"""
test_mint_scan.py - the ID collision scan, and minting people in a batch
(issue #291).

Every mint (`fha person new`, `fha stubs`, `fha claim new`, `fha process`)
ran `_lib.scan_ids_in_tree`, which walked and read EVERYTHING under the
archive root: `.git/` objects, `.cache/`, the installed `.fha/` tools and a
`generated/` site. None of those holds a record's own ID, so the scan now
skips dot-folders and the top-level `generated/`; these tests count the
files it opens rather than timing it.

`fha person new --from-file people.tsv` (name, sex, surname override) mints
a whole list in one `mint_ids` call - one scan - with the #282 duplicate
check run once over all rows, and refuses the whole file on any bad row
before an ID is drawn.
"""

import builtins
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import _lib
import person
from _lib import EXIT_CLEAN, EXIT_ERRORS, EXIT_FAILURE, read_record, scan_ids_in_tree


def _archive(tmp: str) -> Path:
    root = Path(tmp)
    for d in ('people/stubs', 'sources', 'places', 'notes'):
        (root / d).mkdir(parents=True)
    (root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
    return root


class ScanSkipsNonRecordFoldersTests(unittest.TestCase):

    def test_never_opens_dot_folders_or_generated_and_still_finds_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _archive(tmp)
            placed = {
                'sources/a_S-0000000001.md': 's-0000000001',
                'people/stubs/b__c_p-0000000002.md': 'p-0000000002',
                'places/places.yaml': 'l-0000000003',
                'notes/n.md': 'h-0000000004',
            }
            for rel, rid in placed.items():
                (root / rel).write_text(f'id: {rid}\n', encoding='utf-8')
            for rel in ('.git/objects/x.txt', '.cache/log.md', '.fha/tools/t.md',
                        'generated/site/p.md', 'people/.obsidian/w.md'):
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text('id: p-1111111111\n', encoding='utf-8')

            opened: list[str] = []
            real_open = builtins.open

            def counting_open(file, *a, **k):
                opened.append(Path(file).relative_to(root).as_posix()
                              if Path(file).is_absolute() and root in Path(file).parents
                              else str(file))
                return real_open(file, *a, **k)

            with mock.patch('builtins.open', counting_open), \
                    mock.patch('io.open', counting_open):
                found = scan_ids_in_tree(root)

            self.assertTrue(set(placed.values()) <= found)
            self.assertNotIn('p-1111111111', found)
            for rel in opened:
                self.assertFalse(rel.startswith(('.git', '.cache', '.fha', 'generated'))
                                 or '/.' in rel, rel)
            self.assertIn('sources/a_S-0000000001.md', opened)


class FromFileTests(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _archive(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _tsv(self, text: str) -> Path:
        path = self.root / 'people.tsv'
        path.write_text(text, encoding='utf-8')
        return path

    def _stubs(self) -> list[Path]:
        return sorted((self.root / 'people' / 'stubs').glob('*.md'))

    def _run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = person._standalone_main(['new', *argv, '--root', str(self.root)])
        return code, out.getvalue(), err.getvalue()

    def test_three_rows_three_stubs_one_scan(self) -> None:
        path = self._tsv('name\tsex\tsurname\n'
                         'Henry Smith\tM\t\n'
                         'Ann Smith\tf\t\n'
                         'Maria Garcia Lopez\tF\tGarcia Lopez\n')
        calls = []
        real_scan = _lib.scan_ids_in_tree

        def counting_scan(root):
            calls.append(root)
            return real_scan(root)

        with mock.patch.object(_lib, 'scan_ids_in_tree', counting_scan):
            code, out, err = self._run('--from-file', str(path))
        self.assertEqual(code, EXIT_CLEAN, err)
        self.assertEqual(len(calls), 1)
        stubs = self._stubs()
        self.assertEqual(len(stubs), 3)
        by_name = {read_record(p)['meta']['name']: (p.name, read_record(p)['meta'].get('sex'))
                   for p in stubs}
        self.assertEqual(by_name['Henry Smith'][1], 'M')
        self.assertEqual(by_name['Ann Smith'][1], 'F')
        self.assertEqual(by_name['Maria Garcia Lopez'][1], 'F')
        self.assertTrue(by_name['Maria Garcia Lopez'][0].startswith('garcia_lopez__maria_p-'))
        self.assertEqual(len({p.name.rsplit('_', 1)[1] for p in stubs}), 3)

    def test_bad_sex_refuses_the_whole_file_before_minting(self) -> None:
        path = self._tsv('Henry Smith\tM\nAnn Smith\tWoman\n')
        with mock.patch.object(_lib, 'mint_ids', side_effect=AssertionError('minted')), \
                mock.patch.object(person, 'mint_ids', side_effect=AssertionError('minted')):
            code, _out, err = self._run('--from-file', str(path))
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn('row 2', err)
        self.assertEqual(self._stubs(), [])

    def test_too_many_columns_and_blank_name_refuse(self) -> None:
        for text in ('Henry Smith\tM\t\textra\n', '\tM\n'):
            with self.subTest(text=text):
                code, _out, _err = self._run('--from-file', str(self._tsv(text)))
                self.assertEqual(code, EXIT_FAILURE)
                self.assertEqual(self._stubs(), [])

    def test_duplicate_check_runs_once_over_all_rows(self) -> None:
        person.run_new(self.root, 'Ann Smith')
        path = self._tsv('Henry Smith\tM\nAnn Smith\tF\nHenry Smith\tM\n')
        calls = []
        real_find = person.find_people_by_name

        def counting_find(root, names, **kw):
            calls.append(list(names))
            return real_find(root, names, **kw)

        with mock.patch.object(person, 'find_people_by_name', counting_find):
            code, _out, err = self._run('--from-file', str(path))
        self.assertEqual(code, EXIT_FAILURE)
        self.assertEqual(len(calls), 1)
        self.assertIn('Ann Smith already has a record', err)
        self.assertIn('twice', err)
        self.assertEqual(len(self._stubs()), 1)
        code, _out, err = self._run('--from-file', str(path), '--force')
        self.assertEqual(code, EXIT_CLEAN, err)
        self.assertEqual(len(self._stubs()), 4)

    def test_dry_run_writes_nothing(self) -> None:
        path = self._tsv('Henry Smith\tM\n')
        code, out, _err = self._run('--from-file', str(path), '--dry-run')
        self.assertEqual(code, EXIT_CLEAN)
        self.assertIn('[dry-run] Would create', out)
        self.assertEqual(self._stubs(), [])

    def test_mid_way_write_failure_leaves_nothing(self) -> None:
        path = self._tsv('Henry Smith\tM\nAnn Smith\tF\n')
        real_write = person.write_text_exact_atomic
        calls = []

        def failing_write(p, text):
            calls.append(p)
            if len(calls) == 2:
                raise OSError('disk full')
            return real_write(p, text)

        with mock.patch.object(person, 'write_text_exact_atomic', failing_write):
            code, _out, err = self._run('--from-file', str(path))
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn('disk full', err)
        self.assertEqual(self._stubs(), [])

    def test_name_and_from_file_together_is_refused(self) -> None:
        path = self._tsv('Henry Smith\tM\n')
        code, _out, err = self._run('Ann Smith', '--from-file', str(path))
        self.assertEqual(code, EXIT_ERRORS)
        self.assertIn('--from-file', err)

    def test_missing_file_is_refused(self) -> None:
        code, _out, err = self._run('--from-file', str(self.root / 'nope.tsv'))
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn('nope.tsv', err)


if __name__ == '__main__':
    unittest.main()
