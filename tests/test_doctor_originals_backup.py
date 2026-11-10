"""`fha doctor` says whether safety copies are set up (#252 item 5).

What the owner would notice: on a real archive 275 originals were written
into before anyone saw the once-per-run "no safety copies" warning. Doctor -
the command every error in the suite points at - now has a line for it: on
(and where), set but unusable (every keyword write will be refused), or not
set up, with a ready-to-paste `originals_backup:` line naming a folder the
safety-copy rule actually accepts.

Run: py -3.14 -m unittest tests.test_doctor_originals_backup -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import doctor  # noqa: E402
from _lib import EXIT_CLEAN, OriginalBackup, load_fha_yaml  # noqa: E402


class DoctorOriginalsBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.archive = self.tmp / 'Family Archive'
        for sub in ('photos', 'documents', 'sources', 'people'):
            (self.archive / sub).mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _doctor(self, extra: str = ''):
        (self.archive / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n  documents: documents\n' + extra, encoding='utf-8')
        cfg = load_fha_yaml(self.archive)
        result = doctor.run_doctor(self.archive, cfg)
        check = next(c for c in result.data['checks'] if c['id'] == 'originals_backup')
        line = next(l for l in result.data['lines'] if l.startswith('safety copies:'))
        return result, check, line, cfg

    def test_unset_suggests_a_folder_the_rule_accepts(self) -> None:
        result, check, line, cfg = self._doctor()
        self.assertEqual(check['status'], 'info')
        self.assertIn('not set up', line)
        self.assertIn('originals_backup: ', line)
        suggested = line.split('originals_backup: ', 1)[1].split('\n', 1)[0].strip()
        self.assertEqual(Path(suggested), (self.tmp / 'Family Archive-originals').resolve())
        # The suggestion is one the real guard would take, not a guess.
        self.assertIsNone(OriginalBackup(self.archive, {**cfg, 'originals_backup': suggested}).refusal)

    def test_unset_does_not_turn_the_report_amber(self) -> None:
        # Opt-in, like the "no backup recorded" line: info, not a warning.
        _result, check, line, _cfg = self._doctor()
        self.assertEqual(check['status'], 'info')
        self.assertNotIn(doctor._WARN, line)

    def test_set_and_usable_says_where(self) -> None:
        dest = self.tmp / 'PhotoOriginals'
        result, check, line, _cfg = self._doctor(f'originals_backup: {dest.as_posix()}\n')
        self.assertEqual(check['status'], 'ok')
        self.assertIn(str(dest.resolve()), line)

    def test_a_file_where_the_folder_should_be_warns(self) -> None:
        # Codex: a FILE at the configured path passed as "on", while every
        # copy - and so every keyword write - would be refused.
        blocker = self.tmp / 'PhotoOriginals'
        blocker.write_text('not a folder', encoding='utf-8')
        for dest in (blocker, blocker / 'inside'):
            with self.subTest(dest=dest.name):
                result, check, line, _cfg = self._doctor(f'originals_backup: {dest.as_posix()}\n')
                self.assertEqual(check['status'], 'warn')
                self.assertIn('is a file', line)
                self.assertGreaterEqual(result.exit_code, 1)

    def test_a_folder_that_does_not_exist_yet_is_fine(self) -> None:
        _result, check, _line, _cfg = self._doctor(
            f'originals_backup: {(self.tmp / "not-yet" / "copies").as_posix()}\n')
        self.assertEqual(check['status'], 'ok')

    def test_set_inside_the_archive_warns_that_writes_will_be_refused(self) -> None:
        result, check, line, _cfg = self._doctor('originals_backup: safety\n')
        self.assertEqual(check['status'], 'warn')
        self.assertIn('refuses to write keywords', line)
        self.assertGreaterEqual(result.exit_code, 1)

    def test_a_malformed_value_warns(self) -> None:
        _result, check, line, _cfg = self._doctor("originals_backup: ''\n")
        self.assertEqual(check['status'], 'warn')
        self.assertIn('originals_backup', line)

    def test_suggestion_skips_a_folder_inside_the_photos_root(self) -> None:
        # Photos root is the archive's parent folder: "beside the archive"
        # would be inside the photo library, so the next candidate is used.
        (self.archive / 'fha.yaml').write_text(
            f'roots:\n  photos: {self.tmp.as_posix()}\n  documents: documents\n',
            encoding='utf-8')
        cfg = load_fha_yaml(self.archive)
        suggestion = doctor._suggest_originals_backup_folder(self.archive, cfg)
        self.assertIsNotNone(suggestion)
        self.assertNotEqual(suggestion, (self.tmp / 'Family Archive-originals').resolve())
        self.assertIsNone(OriginalBackup(
            self.archive, {**cfg, 'originals_backup': str(suggestion)}).refusal)


if __name__ == '__main__':
    unittest.main()
