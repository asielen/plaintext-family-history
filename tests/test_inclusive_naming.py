"""
test_inclusive_naming.py - surname-less / non-Western person filenames (gap 3)
and translation/language source fields (gap 5), SPEC §13/§14, chunk 03.

A surname-less filename (leading `__`) is valid grammar; a person filename with
no `__` sort separator is gentle W117 guidance, never E002; a normal Western
filename sees neither. A source carrying `original_language:` and a
`role: translation` derivative lints clean - the role says a file's language
(SPEC §14), and `translation` parses as a freeform role like `transcript`.
Built as tiny temp archives, driven through lint's tool logic directly.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import lint
from _lib import load_fha_yaml


def _person(name: str, pid: str) -> str:
    return (f'---\nid: {pid}\nname: {name}\nliving: false\ntier: stub\n---\n\n'
            f'# {name}\n\n## Biography\n\nx\n')


def _archive(person_files: dict[str, str]) -> Path:
    """person_files: {filename_stem: text}, written under people/stubs/."""
    root = Path(tempfile.mkdtemp())
    (root / 'people' / 'stubs').mkdir(parents=True)
    (root / 'sources' / 'notes').mkdir(parents=True)
    (root / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
    for stem, text in person_files.items():
        (root / 'people' / 'stubs' / f'{stem}.md').write_text(text, encoding='utf-8')
    return root


def _codes(findings, code):
    return [f for f in findings if f.code == code]


class SurnamelessFilenameTests(unittest.TestCase):
    def test_mononym_leading_underscore_is_valid(self) -> None:
        pid = 'P-3kq9v8x2m1'
        root = _archive({f'__caesar_{pid}': _person('Caesar', pid)})
        findings, _ = lint._run_lint_core(root, load_fha_yaml(root))
        self.assertEqual(_codes(findings, 'E002'), [])
        self.assertEqual(_codes(findings, 'W117'), [])

    def test_patronymic_with_underscored_given_is_valid(self) -> None:
        pid = 'P-7n4hp0wztb'
        root = _archive({f'__jon_thorsson_{pid}': _person('Jon Thorsson', pid)})
        findings, _ = lint._run_lint_core(root, load_fha_yaml(root))
        self.assertEqual(_codes(findings, 'E002'), [])
        self.assertEqual(_codes(findings, 'W117'), [])

    def test_no_separator_is_w117_guidance_not_e002(self) -> None:
        pid = 'P-3kq9v8x2m1'
        root = _archive({f'caesar_{pid}': _person('Caesar', pid)})
        findings, _ = lint._run_lint_core(root, load_fha_yaml(root))
        self.assertEqual(_codes(findings, 'E002'), [])      # never an error
        self.assertTrue(_codes(findings, 'W117'))            # gentle guidance

    def test_normal_western_surname_is_clean(self) -> None:
        pid = 'P-de957bcda1'
        root = _archive({f'hartley__thomas_{pid}': _person('Thomas Hartley', pid)})
        findings, _ = lint._run_lint_core(root, load_fha_yaml(root))
        self.assertEqual(_codes(findings, 'E002'), [])
        self.assertEqual(_codes(findings, 'W117'), [])       # English-only archive unchanged

    def test_malformed_with_separator_is_still_e002(self) -> None:
        # Has `__` but the sort slot starts with a digit: genuinely malformed.
        pid = 'P-3kq9v8x2m1'
        root = _archive({f'1bad__name_{pid}': _person('Bad Name', pid)})
        findings, _ = lint._run_lint_core(root, load_fha_yaml(root))
        self.assertTrue(_codes(findings, 'E002'))
        self.assertEqual(_codes(findings, 'W117'), [])


class TranslationLanguageTests(unittest.TestCase):
    SID = 'S-7n4hp0wztb'

    def _archive_with_translation(self) -> Path:
        root = Path(tempfile.mkdtemp())
        (root / 'sources' / 'vital-records').mkdir(parents=True)
        docs = root / 'documents' / 'vital-records'
        docs.mkdir(parents=True)
        (root / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
        # Create the actual inventory files on disk so E011 is satisfied.
        (docs / f'taufbuch-1873_{self.SID}.jpg').write_bytes(b'\xff\xd8\xff')
        (docs / f'taufbuch-1873-transcript_{self.SID}.md').write_text('Taufe...', encoding='utf-8')
        (docs / f'taufbuch-1873-translation_{self.SID}.md').write_text('Baptism...', encoding='utf-8')
        src = (
            f'---\nid: {self.SID}\n'
            'title: Taufbuch 1873 (German baptism)\n'
            'source_type: vital-record\n'
            'original_language: de\n'
            'files:\n'
            f'  - file: documents/vital-records/taufbuch-1873_{self.SID}.jpg\n'
            '    role: front\n'
            f'  - file: documents/vital-records/taufbuch-1873-transcript_{self.SID}.md\n'
            '    role: transcript\n'
            '    derived: true\n'
            f'  - file: documents/vital-records/taufbuch-1873-translation_{self.SID}.md\n'
            '    role: translation\n'
            '    derived: true\n'
            '---\n\n## Notes\nA German baptism record with an English translation.\n'
        )
        (root / 'sources' / 'vital-records' / f'taufbuch-1873_{self.SID}.md').write_text(
            src, encoding='utf-8')
        return root

    def test_translation_role_and_language_fields_lint_clean(self) -> None:
        root = self._archive_with_translation()
        findings, _ = lint._run_lint_core(root, load_fha_yaml(root))
        errors = [f for f in findings if f.severity == 'E']
        self.assertEqual(errors, [], f'unexpected errors: {[(f.code, f.message) for f in errors]}')
        # No inventory-mismatch and no unknown-vocabulary warning for the new role.
        self.assertEqual(_codes(findings, 'E011'), [])
        self.assertEqual(_codes(findings, 'W109'), [])
        # No per-file `language:` is needed or asked for.
        self.assertEqual(_codes(findings, 'W143'), [])


if __name__ == '__main__':
    unittest.main()
