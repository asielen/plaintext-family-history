"""`fha process --variants one|separate` (#249 side note).

What the owner would notice (or rather, his assistant): processing a photo
with its back scan beside it asks "Process as ONE source ... [one / separate
/ skip]", and an agent with no keyboard got Python's "EOF when reading a
line". `--variants one|separate` answers the question up front; with no one
at the keyboard and no flag, the run now stops with a sentence naming the
flag and writes nothing. Asked interactively, nothing changed.

Run: py -3.14 -m unittest tests.test_process_variants -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))

import process  # noqa: E402
from _lib import EXIT_CLEAN, EXIT_ERRORS, read_record  # noqa: E402
from test_process_document_keyword import _Base  # noqa: E402


class VariantsFlagTests(_Base):
    """#249 side note: `--variants one|separate` for a front/back pair."""

    def setUp(self) -> None:
        super().setUp()
        self.store = self._install_store()
        self.front = self.archive / 'photos' / '1911' / 'ada-1911.jpg'
        self.back = self.archive / 'photos' / '1911' / 'ada-1911-back.jpg'
        self.front.write_bytes(b'\xff\xd8\xff')
        self.back.write_bytes(b'\xff\xd8\xff')

        def _no_one_here(_msg):
            raise EOFError('EOF when reading a line')
        process._prompt = _no_one_here

    def test_one_files_the_pair_as_one_source_without_asking(self) -> None:
        rc, out, err = self._run([str(self.front), '--variants', 'one'])
        self.assertEqual(rc, EXIT_CLEAN, err)
        records = self._records('photos')
        self.assertEqual(len(records), 1)
        self.assertEqual(len(read_record(records[0])['meta']['files']), 2)
        self.assertEqual(self.store.read(self.front), self.store.read(self.back))
        self.assertIn('--variants one', out)

    def test_separate_files_each_as_its_own_source(self) -> None:
        rc, _out, err = self._run([str(self.front), '--variants', 'separate'])
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertEqual(len(self._records('photos')), 2)
        self.assertNotEqual(self.store.read(self.front), self.store.read(self.back))

    def test_no_flag_and_no_one_to_ask_names_the_flag_and_writes_nothing(self) -> None:
        rc, _out, err = self._run([str(self.front)])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('--variants one', err)
        self.assertIn('--variants separate', err)
        self.assertNotIn('EOF when reading a line', err)
        self.assertNotIn('Traceback', err)
        self.assertEqual(self._records('photos'), [])
        self.assertEqual(self.store.keywords, {})

    def test_the_question_is_still_asked_when_someone_is_there(self) -> None:
        asked = []
        process._prompt = lambda msg: asked.append(msg) or 'one'
        rc, _out, err = self._run([str(self.front)])
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertEqual(len(asked), 1)
        self.assertIn('[one / separate / skip]', asked[0])

    def test_dry_run_with_the_flag_previews_the_same_group(self) -> None:
        rc, out, err = self._run([str(self.front), '--variants', 'one', '--dry-run'])
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertIn('for a 2-file variation set', out)
        self.assertEqual(self._records('photos'), [])

    def test_with_more_it_is_refused(self) -> None:
        rc, _out, err = self._run([str(self.front), '--more', str(self.back), 'back',
                                   '--variants', 'one'])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('drop --variants', err)

    def test_on_a_document_it_is_refused_rather_than_ignored(self) -> None:
        doc = self.archive / 'documents' / 'census' / 'sheet.txt'
        doc.write_text('x', encoding='utf-8')
        rc, _out, err = self._run([str(doc), '--variants', 'one'])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('drop --variants', err)
        self.assertTrue(doc.exists())

    def test_a_lone_photo_says_the_flag_had_nothing_to_decide(self) -> None:
        lone = self.archive / 'photos' / '1911' / 'farm.jpg'
        lone.write_bytes(b'\xff\xd8\xff')
        rc, out, err = self._run([str(lone), '--variants', 'one'])
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertIn('nothing to decide', out)

    def test_folder_triage_with_no_one_to_ask_explains_itself(self) -> None:
        saved = process._run_exiftool_read_meta
        process._run_exiftool_read_meta = lambda _p: {
            'caption': None, 'user_comment': None, 'edtf': None, 'has_pid_keyword': False}
        try:
            rc, _out, err = self._run([str(self.archive / 'photos' / '1911')])
        finally:
            process._run_exiftool_read_meta = saved
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('no one here to answer', err)
        self.assertNotIn('Traceback', err)


if __name__ == '__main__':
    unittest.main()
