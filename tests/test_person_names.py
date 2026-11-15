"""
test_person_names.py - how a new person is named and minted.

Two issues, one surface (the minting verbs `fha stubs --from-names` and
`fha person new`, plus the workbench mint button that calls `person.run_new`):

  #279 - an accented letter in a name was DELETED from the filename slug
         instead of folded to its base letter ("Wiśniewska" filed as
         `winiewska`), and `fha index` read that slug back as the surname, so
         `fha gedcom` exported the misspelling. The fold now lives once in
         `_lib.fold_accents` (lifted from xref's town-name fold) and every
         person-filename slugger shares it.
  #282 - minting a name the archive already had made a second record without
         a word. Both verbs now refuse (exit 3, nothing written) and name the
         existing record, checking the index AND the files on disk (a stub
         minted since the last `fha index` is only on disk), with `--force`
         for two people who really share a name.

Expected values come from the issues' own regression lists, not from the
implementation's output.
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
import stubs
import xref
from _lib import (
    fold_accents,
    stub_filename,
)


# The issue #279 table: name -> the `{surname}__{given}` slots it must file as.
_FOLDED = {
    'Halina Wiśniewska': ('wisniewska', 'halina'),
    'Anna Gołębiowska': ('golebiowska', 'anna'),
    'Jan Ślęzak': ('slezak', 'jan'),
    'Józef Żółć': ('zolc', 'jozef'),
    'Hans Müller': ('muller', 'hans'),
}


def _new_archive() -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / 'people' / 'stubs').mkdir(parents=True)
    (root / 'sources').mkdir()
    (root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
    return tmp, root


def _stub_files(root: Path) -> list[str]:
    return sorted(p.name for p in (root / 'people' / 'stubs').glob('*.md'))


def _mint_from_names(root: Path, names: list[str], **kw) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = stubs.mint_named_stubs(root, names, **kw)
    return code, out.getvalue(), err.getvalue()


def _reindex(root: Path) -> None:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        index.build_index(root, {})
    # The index's freshness test is mtime-based: make sure a later mint is
    # strictly newer than the database it is compared with.
    time.sleep(0.02)


# ── #279: accented letters fold, they are not dropped ────────────────────────

class AccentFoldTests(unittest.TestCase):

    def test_fold_accents_examples(self) -> None:
        self.assertEqual(fold_accents('Wiśniewska'), 'Wisniewska')
        self.assertEqual(fold_accents('Gołębiowska'), 'Golebiowska')
        self.assertEqual(fold_accents('Straße Ærø Đorđe'), 'Strasse AEro Dorde')
        # Letters with no base form pass through for the caller's filter.
        self.assertEqual(fold_accents('Иван'), 'Иван')

    def test_stub_filename_folds_each_issue_name(self) -> None:
        for name, (surname, given) in _FOLDED.items():
            with self.subTest(name=name):
                self.assertEqual(stub_filename(name, 'p-0000000001'),
                                 f'{surname}__{given}_p-0000000001.md')

    def test_surname_override_folds_too(self) -> None:
        self.assertEqual(
            stub_filename('Halina Wiśniewska', 'p-0000000001', surname='Wiśniewska'),
            'wisniewska__halina_p-0000000001.md')

    def test_lint_fix_ids_slots_match(self) -> None:
        for name, slots in _FOLDED.items():
            with self.subTest(name=name):
                self.assertEqual(lint._person_filename_parts(name, 'fallback'), slots)

    def test_gedcom_import_slots_match(self) -> None:
        for name, (surname, given) in _FOLDED.items():
            with self.subTest(name=name):
                parts = name.split()
                self.assertEqual(gedcom_import._name_slot(parts[-1]), surname)
                self.assertEqual(gedcom_import._name_slot(' '.join(parts[:-1])), given)

    def test_xref_town_key_unchanged(self) -> None:
        # xref now calls the shared fold; its town keys must not move.
        self.assertEqual(xref._town_key('Suwałki, Poland'), 'suwalki')
        self.assertEqual(xref._town_key('München'), 'munchen')
        self.assertEqual(xref._town_key('Muenchen'), 'munchen')

    def test_stubs_from_names_writes_folded_file(self) -> None:
        tmp, root = _new_archive()
        self.addCleanup(tmp.cleanup)
        _mint_from_names(root, ['Halina Wiśniewska'])
        [filename] = _stub_files(root)
        self.assertTrue(filename.startswith('wisniewska__halina_p-'), filename)

    def test_indexed_stub_exports_its_own_spelling(self) -> None:
        tmp, root = _new_archive()
        self.addCleanup(tmp.cleanup)
        _mint_from_names(root, ['Halina Wiśniewska'])
        _reindex(root)
        text = gedcom.run_gedcom(root, None, all_persons=True, include_living=True)['text']
        self.assertIn('1 NAME Halina /Wiśniewska/', text)

    def test_record_filed_with_the_old_broken_slug_exports_its_own_spelling(self) -> None:
        # A record minted before the fold is still filed `winiewska__halina_…`
        # and indexed with the surname "Winiewska"; the export must still
        # carry the name's own spelling, not the broken slug.
        self.assertEqual(gedcom._gedcom_name('Halina Wiśniewska', 'Winiewska'),
                         ('Halina /Wiśniewska/', None))
        self.assertEqual(gedcom._gedcom_name('Halina Wiśniewska', 'Wisniewska'),
                         ('Halina /Wiśniewska/', None))
        # A surname override the name does not end with still lands whole.
        self.assertEqual(gedcom._gedcom_name('Halina Wiśniewska', 'Kowalska'),
                         ('Halina Wiśniewska /Kowalska/', None))


if __name__ == '__main__':
    unittest.main()
