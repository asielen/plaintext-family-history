import builtins
import contextlib
import importlib.util
import io
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import fha
import packet
import photoindex
import process
import working_copy
from _lib import EXIT_CLEAN, EXIT_FAILURE

# site.py's module stem collides with Python's stdlib `site`, so it is loaded by
# path under a private name (the same trick test_site.py / fha.py use).
_spec = importlib.util.spec_from_file_location('fha_site', ROOT / 'tools' / 'site.py')
site = importlib.util.module_from_spec(_spec)
sys.modules['fha_site'] = site
_spec.loader.exec_module(site)


def _copy_fixture(tmp: Path) -> Path:
    """Copy the working-copy fixture so tests can mutate marker/cache files."""
    src = ROOT / 'tests' / 'fixtures' / 'working-copy'
    dst = tmp / 'working-copy'
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns('.cache'))
    return dst


def _copy_photo_fixture(tmp: Path) -> Path:
    src = ROOT / 'tests' / 'fixtures' / 'photo-fixture'
    dst = tmp / 'photo-fixture'
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns('.cache'))
    return dst


class WorkingCopyTests(unittest.TestCase):
    def test_status_accepts_parent_and_child_root(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            for argv in (
                ['working-copy', '--root', str(archive), 'status'],
                ['working-copy', 'status', '--root', str(archive)],
            ):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(fha.main(argv), EXIT_CLEAN)
                self.assertIn('Working-copy mode: ON', out.getvalue())

    def test_bare_working_copy_reports_status(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(fha.main(['working-copy', '--root', str(archive)]), EXIT_CLEAN)
            self.assertIn('Working-copy mode: ON', out.getvalue())

    def test_bad_root_exits_failure_with_single_error_line(self) -> None:
        # A --root that is not an archive must exit 3 (EXIT_FAILURE), matching
        # every other tool, and print exactly one plain error line - not the
        # old exit-2 + duplicate second message.
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / 'not-an-archive'
            bad.mkdir()
            for sub in ('on', 'off', 'status'):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = fha.main(['working-copy', sub, '--root', str(bad)])
                self.assertEqual(code, EXIT_FAILURE)
                lines = [ln for ln in err.getvalue().splitlines() if ln.strip()]
                self.assertEqual(len(lines), 1, err.getvalue())
                self.assertTrue(lines[0].startswith('ERROR:'), lines[0])

    def test_off_prompt_names_unreachable_asset_roots_and_decline_keeps_marker(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            prompts: list[str] = []
            orig_input = builtins.input
            builtins.input = lambda prompt='': (prompts.append(prompt) or 'n')
            try:
                code = working_copy._cmd_off(type('Args', (), {'root': str(archive), 'yes': False})())
            finally:
                builtins.input = orig_input

            self.assertEqual(code, EXIT_CLEAN)
            self.assertTrue((archive / 'WORKING_COPY').exists())
            prompt = ''.join(prompts)
            self.assertIn('photos root', prompt)
            self.assertIn('documents root', prompt)
            self.assertIn('not reachable', prompt)

    def test_result_messages_are_json_serializable(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            (archive / 'WORKING_COPY').unlink()

            orig = working_copy._ensure_gitignore_entry
            working_copy._ensure_gitignore_entry = lambda root: (_ for _ in ()).throw(
                OSError('locked')
            )
            try:
                result = working_copy.run_working_copy_on(archive)
            finally:
                working_copy._ensure_gitignore_entry = orig

            payload = result.as_dict()
            self.assertEqual(payload['messages'][0]['level'], 'warning')
            self.assertTrue((archive / 'WORKING_COPY').is_file())

    def test_reconcile_refuses_without_mutating_cache(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_photo_fixture(Path(d))
            cfg = {'roots': {'photos': 'photos'}}
            orig_run_exiftool = photoindex._run_exiftool
            photoindex._run_exiftool = lambda paths: [
                {'SourceFile': str(p), 'Caption-Abstract': p.name} for p in paths
            ]
            try:
                photoindex.run_scan(archive, cfg)
            finally:
                photoindex._run_exiftool = orig_run_exiftool

            (archive / 'WORKING_COPY').write_text('working copy\n', encoding='utf-8')
            shutil.rmtree(archive / 'photos')
            (archive / 'photos').mkdir()

            before = self._photo_paths(archive)
            result = photoindex.run_reconcile(archive, cfg)
            after = self._photo_paths(archive)

            self.assertEqual(result.exit_code, EXIT_CLEAN)
            self.assertTrue(result['working_copy'])
            self.assertEqual(before, after)
            self.assertFalse(any(path.startswith('MISSING:') for path in after))

    def test_asset_refusals_exit_clean(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            self.assertEqual(
                process._run_process(type('Args', (), {'root': str(archive), 'file': 'x.jpg'})()),
                EXIT_CLEAN,
            )
            self.assertEqual(
                packet._cmd_packet(type('Args', (), {'root': str(archive), 'person_id': 'P-wc00000001'})()),
                EXIT_CLEAN,
            )
            self.assertEqual(
                photoindex._cmd_tag_person(type('Args', (), {
                    'root': str(archive), 'person_id': 'P-wc00000001',
                    'from_face_tag': None, 'paths': ['photos/x.jpg'], 'dry_run': False,
                })()),
                EXIT_CLEAN,
            )
            self.assertEqual(
                photoindex._cmd_set_summary(type('Args', (), {
                    'root': str(archive), 'path': 'photos/x.jpg', 'group': None,
                    'text': 'a summary', 'append': False, 'dry_run': False,
                })()),
                EXIT_CLEAN,
            )
            self.assertEqual(
                site._cmd_site(type('Args', (), {
                    'root': str(archive), 'out': None, 'linked': False, 'dry_run': False,
                })()),
                EXIT_CLEAN,
            )

    def test_asset_refusals_report_ok_true_with_status(self) -> None:
        # The asset-mutating commands, refused in working-copy mode, are a
        # warning-level Result: ok is True, exit is clean, and the machine
        # discriminator is data.status == 'working-copy' (Flag 7 / TOOLING §13d).
        with tempfile.TemporaryDirectory() as d:
            archive = _copy_fixture(Path(d))
            out_dir = Path(d) / 'out'

            r_process = process.run_process(
                type('Args', (), {'root': str(archive), 'file': 'x.jpg'})())
            r_packet = packet.run_packet(archive, 'P-wc00000001', out_dir)
            r_site = site.run_site(archive, out_dir)
            r_photo = photoindex.apply_tag_person(
                archive, {'roots': {'photos': 'photos'}}, 'P-wc00000001', ['photos/x.jpg'])
            r_summary = photoindex.run_set_summary(
                archive, {'roots': {'photos': 'photos'}}, 'a summary', ['photos/x.jpg'])

            for name, result in (
                ('process', r_process), ('packet', r_packet),
                ('site', r_site), ('tag-person', r_photo),
                ('set-summary', r_summary),
            ):
                self.assertIs(result.ok, True, name)
                self.assertEqual(result.exit_code, EXIT_CLEAN, name)
                self.assertEqual(result.data.get('status'), 'working-copy', name)

    @staticmethod
    def _photo_paths(archive: Path) -> list[str]:
        conn = sqlite3.connect(archive / '.cache' / 'photos.sqlite')
        try:
            return [row[0] for row in conn.execute('SELECT path FROM photos ORDER BY path')]
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
