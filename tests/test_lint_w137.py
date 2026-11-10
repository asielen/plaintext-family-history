"""W137 (#249): evidence pointed at by folder path, not by its source ID.

What the owner would notice: during review of an interview, the photo whose
inscription the interviewer read aloud turns up in the library, scanned but
never processed, and its path gets pasted into the claim's notes. Nothing
said that was a problem. Now `fha lint` does - and names the fix
(`fha process <path> --type photo`, then cite it as [[S-…]]) - while staying
quiet for paths some source already owns, for [[S-…]] links and for web
addresses.

Run: py -3.14 -m unittest tests.test_lint_w137 -v
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import lint  # noqa: E402
from _lib import load_fha_yaml  # noqa: E402

INTERVIEW = 'S-a1b2c3d4e5'
PHOTO_SRC = 'S-b2c3d4e5f6'
PID = 'P-de957bcda1'


def _source(sid: str, *, files: list[str], claims_notes: str = '', value: str = 'Ada was 16',
            prose: str = '', extra_files: str = '') -> str:
    files_block = ''.join(f'  - file: {f}\n    role: primary\n' for f in files) + extra_files
    return (
        f'---\nid: {sid}\naliases:\n  - {sid}\ntitle: A source\nsource_type: interview\n'
        f'files:\n{files_block}---\n\n'
        '## Claims\n\n```yaml\n'
        f'- value: "{value}"\n  id: C-{sid[2:-1]}9\n  type: note\n  persons: [{PID}]\n'
        f'  status: suggested\n  notes: |\n    {claims_notes}\n```\n\n'
        f'## Notes\n\n{prose}\n')


class W137Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / 'archive'
        for sub in ('sources/interviews', 'sources/photos', 'people', 'documents/interviews',
                    'documents/letters', 'photos/1911'):
            (self.root / sub).mkdir(parents=True)
        self._config('roots:\n  photos: photos\n  documents: documents\n')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _config(self, text: str) -> None:
        (self.root / 'fha.yaml').write_text(text, encoding='utf-8')

    def _write(self, sid: str, kind: str = 'interviews', **kw) -> Path:
        path = self.root / 'sources' / kind / f'a-source_{sid}.md'
        path.write_text(_source(sid, **kw), encoding='utf-8')
        return path

    def _w137(self) -> list:
        findings, _registry = lint._run_lint_core(self.root, load_fha_yaml(self.root))
        return [f for f in findings if f.code == 'W137']

    def _interview(self, **kw) -> Path:
        return self._write(INTERVIEW, files=[f'documents/interviews/talk_{INTERVIEW}.mp3'], **kw)

    # -- it fires ---------------------------------------------------------------

    def test_a_photos_root_path_in_claim_notes_fires_and_names_the_fix(self) -> None:
        self._interview(claims_notes='Inscription on photos/1911/ada-1911-back.jpg reads it.')
        found = self._w137()
        self.assertEqual(len(found), 1)
        msg = found[0].message
        self.assertIn(f'Claim C-{INTERVIEW[2:-1]}9 (its notes:)', msg)
        self.assertIn('photos/1911/ada-1911-back.jpg', msg)
        self.assertIn('fha process "', msg)
        self.assertIn('--type photo', msg)
        self.assertIn('[[S-', msg)
        self.assertTrue(found[0].path.endswith(f'a-source_{INTERVIEW}.md'))

    def test_windows_backslashes_fire(self) -> None:
        self._interview(claims_notes=r'See photos\1911\ada-1911-back.jpg for the back.')
        self.assertEqual(len(self._w137()), 1)

    def test_the_absolute_folder_from_fha_yaml_fires_either_slash(self) -> None:
        library = self.tmp / 'Family Photos'
        (library / '1911').mkdir(parents=True)
        self._config(f'roots:\n  photos: {library.as_posix()}\n  documents: documents\n')
        windows = str(library).replace('/', '\\') + r'\1911\Ada Hartley 16 - 1911-back.jpg'
        posix = library.as_posix() + '/1911/Ada Hartley 16 - 1911.jpg'
        self._interview(claims_notes=f'Back: {windows} and front: {posix} (found in review).')
        found = self._w137()
        self.assertEqual(len(found), 2, [f.message for f in found])
        self.assertTrue(any('Ada Hartley 16 - 1911-back.jpg' in f.message for f in found))

    def test_a_documents_root_path_in_the_value_fires_without_type_photo(self) -> None:
        self._interview(value='Letter documents/letters/ada-letter.pdf names her sister')
        found = self._w137()
        self.assertEqual(len(found), 1)
        self.assertIn('Claim C-', found[0].message)
        self.assertIn('(its value:)', found[0].message)
        self.assertNotIn('--type photo', found[0].message)

    def test_source_prose_fires(self) -> None:
        self._interview(prose='The photo shown is photos/1911/ada-1911.jpg in the library.')
        found = self._w137()
        self.assertEqual(len(found), 1)
        self.assertIn("source record's prose", found[0].message)

    def test_person_prose_fires(self) -> None:
        person = self.root / 'people' / f'hartley__ada_{PID}.md'
        person.write_text(
            f'---\nid: {PID}\nname: Ada Hartley\nsex: F\nliving: false\n---\n\n'
            '## Biography\n\nHer portrait (photos/1911/ada-1911.jpg) was taken at 16.\n',
            encoding='utf-8')
        found = self._w137()
        self.assertEqual(len(found), 1)
        self.assertIn("person record's prose", found[0].message)

    # -- it stays quiet ----------------------------------------------------------

    def test_quiet_when_a_source_owns_the_file_any_case_either_slash(self) -> None:
        self._write(PHOTO_SRC, kind='photos', files=['photos/1911/Ada-1911-back.jpg'])
        self._interview(claims_notes=r'Read from PHOTOS\1911\ada-1911-back.jpg aloud.')
        self.assertEqual(self._w137(), [])

    def test_quiet_for_links_urls_and_bare_folder_words(self) -> None:
        self._interview(
            claims_notes=f'See [[{PHOTO_SRC}]], https://example.org/photos/1911/ada.jpg '
                         'and the photos/ folder in general.')
        self.assertEqual(self._w137(), [])

    def test_quiet_for_a_documents_file_named_with_its_source_id(self) -> None:
        self._interview(claims_notes=f'Compare documents/letters/ada-letter_{PHOTO_SRC}.pdf.')
        self.assertEqual(self._w137(), [])

    def test_quiet_for_the_pre_rename_name_of_a_processed_document(self) -> None:
        self._write(PHOTO_SRC, kind='photos', files=[f'documents/letters/ada-letter_{PHOTO_SRC}.pdf'],
                    extra_files='')
        rec = self.root / 'sources' / 'photos' / f'a-source_{PHOTO_SRC}.md'
        rec.write_text(rec.read_text(encoding='utf-8').replace(
            '    role: primary\n', '    role: primary\n    original_filename: letter.pdf\n', 1),
            encoding='utf-8')
        self._interview(claims_notes='Written before filing: documents/letters/letter.pdf')
        self.assertEqual(self._w137(), [])

    def test_quiet_for_a_photo_the_photo_index_says_is_keyworded(self) -> None:
        (self.root / '.cache').mkdir()
        conn = sqlite3.connect(self.root / '.cache' / 'photos.sqlite')
        conn.execute('CREATE TABLE photos(path TEXT PRIMARY KEY, source_id TEXT)')
        conn.execute("INSERT INTO photos VALUES ('photos/1911/ada-1911.jpg', ?)", (PHOTO_SRC,))
        conn.commit()
        conn.close()
        self._interview(claims_notes='Front is photos/1911/ada-1911.jpg.')
        self.assertEqual(self._w137(), [])

    def test_a_broken_photo_index_does_not_break_lint(self) -> None:
        (self.root / '.cache').mkdir()
        (self.root / '.cache' / 'photos.sqlite').write_bytes(b'not a database')
        self._interview(claims_notes='Front is photos/1911/ada-1911.jpg.')
        self.assertEqual(len(self._w137()), 1)

    def test_one_finding_per_path_per_place(self) -> None:
        self._interview(claims_notes='photos/1911/ada.jpg, and again photos/1911/ada.jpg.')
        self.assertEqual(len(self._w137()), 1)


@unittest.skipUnless((ROOT / 'example-archive' / 'fha.yaml').is_file(), 'no example archive')
class ExampleArchiveTests(unittest.TestCase):
    def test_the_example_archive_raises_no_w137(self) -> None:
        archive = ROOT / 'example-archive'
        findings, _registry = lint._run_lint_core(archive, load_fha_yaml(archive))
        self.assertEqual([f.message for f in findings if f.code == 'W137'], [])


if __name__ == '__main__':
    unittest.main()
