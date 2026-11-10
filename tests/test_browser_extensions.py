"""
Tests for tools/browser_extensions.py - `fha install` / `fha update-tools`
keeping the owner's installed Chrome add-ons (Seen It) current from the tools,
in the folder fha.yaml's `browser_extensions:` names (TOOLING §13c).

What is at stake for the owner: Chrome keeps an add-on's data by its ID, so the
copy he loaded must be refreshed in place - found even under a folder name he
chose himself, backed up first, with his helper's launcher and the add-on's
data folder left exactly as they were. A wrong move here deletes years of
"already looked at" history.

Every fixture lives in a throwaway folder whose name has a space in it, the
way `D:/Genealogy/Tools/Chrome Extensions` does.

Run: python -m unittest tests.test_browser_extensions -v
"""

import argparse
import contextlib
import datetime
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import scaffold
try:
    import browser_extensions as bx
except ImportError:     # before this feature: let each test fail on its own
    bx = None
from _lib import EXIT_CLEAN, EXIT_WARNINGS

KEY = 'MIIBfakeSeenItKeyForTests'
TODAY = datetime.date.today().isoformat()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode('utf-8'))   # exact bytes, no newline translation


def _seen_it_manifest(version: str, key: str = KEY) -> str:
    return json.dumps({'manifest_version': 3, 'name': 'Seen It: Test Sites',
                       'version': version, 'key': key}, indent=2)


def _make_repo(repo: Path, version: str = '0.2.0') -> Path:
    """A tiny tools clone: one tool, a template fha.yaml, Seen It, the capture add-on."""
    _write(repo / 'SPEC.md', '# SPEC\n\n**Version 9.9 - 2026-01-01**\n')
    _write(repo / 'tools' / 'atool.py', 'print("a tool")\n')
    _write(repo / 'archive-template' / 'fha.yaml',
           '# my archive settings - keep this comment\nroots:\n  photos: photos\n')
    _write(repo / 'archive-template' / 'sources' / '.gitkeep', '')
    _write(repo / 'browser-companion' / 'manifest.json',
           json.dumps({'manifest_version': 3, 'name': 'Capture (Test)', 'version': '1.0'}))
    _write(repo / 'browser-companion' / 'src' / 'panel.js', '// v1\n')
    seen = repo / 'seen-it'
    _write(seen / 'manifest.json', _seen_it_manifest(version))
    _write(seen / 'background.js', f'// background {version}\n')
    _write(seen / 'README.md', '# Seen It\n')
    _write(seen / 'host' / 'seenit_host.py', f'# helper {version}\n')
    _write(seen / 'host' / 'install.cmd', '@echo off\r\n')
    # Dev furniture that must never reach an installed copy.
    _write(seen / 'tests' / 'test-background.js', '// test\n')
    _write(seen / 'package.json', '{}\n')
    _write(seen / '.gitignore', 'data/\n')
    _write(seen / 'host' / '__pycache__' / 'seenit_host.cpython-314.pyc', 'x')
    scaffold._write_manifest(repo)
    return repo


def _tree(folder: Path) -> dict:
    """Every file under `folder` -> its bytes (a before/after snapshot)."""
    if not folder.exists():
        return {}
    return {p.relative_to(folder).as_posix(): p.read_bytes()
            for p in sorted(folder.rglob('*')) if p.is_file()}


def _quiet(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*args, **kwargs)
    return result, buf.getvalue()


def _never_ask(prompt):
    raise AssertionError(f'asked when it should not have: {prompt}')


class _Base(unittest.TestCase):
    """A repo, an installed archive, and the owner's extensions folder layout."""

    def setUp(self):
        tmp_base = os.environ.get('TEMP') or None
        self._tmp = tempfile.TemporaryDirectory(dir=tmp_base)
        self.tmp = Path(self._tmp.name)
        self.repo = _make_repo(self.tmp / 'tools clone')
        self.archive = self.tmp / 'My Family Archive'
        _quiet(scaffold.run_install, self.archive, self.repo)
        self.ext = self.tmp / 'Chrome Extensions'

    def tearDown(self):
        self._tmp.cleanup()

    def owner_layout(self, version: str = '0.1.0') -> Path:
        """Seen It installed under an older folder name, as the owner has it."""
        installed = self.ext / 'newspapers-seen-it'
        _write(installed / 'manifest.json', _seen_it_manifest(version))
        _write(installed / 'background.js', f'// background {version}\n')
        _write(installed / 'host' / 'seenit_host.py', f'# helper {version}\n')
        _write(installed / 'host' / 'install.cmd', '@echo off\r\n')
        # Written by Seen It's helper installer; the repo does not ship it.
        _write(installed / 'host' / 'seenit_host.cmd', '@echo off\r\npython -u x\r\n')
        _write(self.ext / 'newspapers-seen-it-data' / 'seen-it-pc.json', '{"looks": 812}\n')
        _write(self.ext / '_backups' / 'newspapers-seen-it-0.0.9-2026-10-10' / 'manifest.json',
               _seen_it_manifest('0.0.9'))
        return installed

    def set_folder(self, value: str) -> None:
        with open(self.archive / 'fha.yaml', 'a', encoding='utf-8') as f:
            f.write(f'browser_extensions: {value}\n')

    def update(self, **kwargs):
        return _quiet(scaffold.run_update_tools, self.archive, self.repo, **kwargs)


class FindAndUpdateInPlace(_Base):

    def test_installed_copy_is_found_by_key_under_another_folder_name(self):
        installed = self.owner_layout()
        self.set_folder(self.ext.as_posix())
        rc, out = self.update()
        self.assertEqual(rc, EXIT_CLEAN, out)
        self.assertEqual((installed / 'background.js').read_text(encoding='utf-8'),
                         '// background 0.2.0\n')
        self.assertFalse((self.ext / 'seen-it').exists())   # no second copy
        self.assertIn('0.1.0 -> 0.2.0', out)
        self.assertIn('click the reload arrow on "Seen It: Test Sites"', out)
        self.assertIn('do not remove it', out)

    def test_backup_is_made_first_and_never_overwritten(self):
        installed = self.owner_layout()
        self.set_folder(self.ext.as_posix())
        taken = self.ext / '_backups' / f'newspapers-seen-it-0.1.0-{TODAY}'
        _write(taken / 'note.txt', 'an earlier backup from this morning\n')
        before = _tree(installed)
        rc, out = self.update()
        self.assertEqual(rc, EXIT_CLEAN, out)
        self.assertEqual(_tree(taken), {'note.txt': b'an earlier backup from this morning\n'})
        backup = taken.with_name(taken.name + '-2')
        self.assertEqual(_tree(backup), before)
        self.assertIn(str(backup), out)
        # The older backup the owner already had is untouched too.
        self.assertEqual(
            len(_tree(self.ext / '_backups' / 'newspapers-seen-it-0.0.9-2026-10-10')), 1)

    def test_files_the_repo_does_not_ship_are_kept_and_dev_files_not_copied(self):
        installed = self.owner_layout()
        self.set_folder(self.ext.as_posix())
        self.update()
        self.assertEqual((installed / 'host' / 'seenit_host.cmd').read_bytes(),
                         b'@echo off\r\npython -u x\r\n')
        for dev in ('tests', 'package.json', '.gitignore'):
            self.assertFalse((installed / dev).exists(), dev)
        self.assertFalse((installed / 'host' / '__pycache__').exists())
        self.assertTrue((installed / 'README.md').is_file())

    def test_sibling_data_folder_is_never_touched(self):
        installed = self.owner_layout()
        self.set_folder(self.ext.as_posix())
        data = self.ext / 'newspapers-seen-it-data'
        before = _tree(data)
        mtime = (data / 'seen-it-pc.json').stat().st_mtime_ns
        self.update()
        # Its neighbour WAS updated, and the data folder beside it was not.
        self.assertEqual((installed / 'background.js').read_text(encoding='utf-8'),
                         '// background 0.2.0\n')
        self.assertEqual(_tree(data), before)
        self.assertEqual((data / 'seen-it-pc.json').stat().st_mtime_ns, mtime)

    def test_dry_run_writes_nothing_and_plans_what_the_real_run_does(self):
        self.owner_layout()
        self.set_folder(self.ext.as_posix())
        ext_before = _tree(self.ext)
        yaml_before = (self.archive / 'fha.yaml').read_bytes()
        dry, dry_out = _quiet(bx.run_extensions, self.archive, self.repo, dry_run=True)
        self.assertEqual(_tree(self.ext), ext_before)
        self.assertEqual((self.archive / 'fha.yaml').read_bytes(), yaml_before)
        self.assertIn('[dry-run] Would update Seen It', dry_out)
        real, _out = _quiet(bx.run_extensions, self.archive, self.repo)

        def shape(items):
            return [(i['action'], i['target'], i['backup'],
                     [rel for rel, _p in i['copy']]) for i in items]
        self.assertEqual(shape(dry['items']), shape(real['items']))

    def test_dry_run_through_update_tools_writes_nothing(self):
        self.owner_layout()
        self.set_folder(self.ext.as_posix())
        before = _tree(self.ext)
        rc, out = self.update(dry_run=True)
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(_tree(self.ext), before)
        self.assertIn('[dry-run] Would update Seen It 0.1.0 -> 0.2.0', out)

    def test_second_run_says_up_to_date_and_writes_nothing(self):
        self.owner_layout()
        self.set_folder(self.ext.as_posix())
        self.update()
        before = _tree(self.ext)
        rc, out = self.update()
        self.assertEqual(rc, EXIT_CLEAN, out)
        self.assertIn('Seen It is up to date (version 0.2.0)', out)
        self.assertEqual(_tree(self.ext), before)

    def test_newer_installed_copy_is_never_downgraded(self):
        installed = self.owner_layout(version='0.3.0')
        self.set_folder(self.ext.as_posix())
        before = _tree(self.ext)
        rc, out = self.update()
        self.assertEqual(rc, EXIT_CLEAN, out)
        self.assertEqual(_tree(self.ext), before)
        self.assertIn('newer than the copy these tools carry', out)
        self.assertTrue((installed / 'manifest.json').is_file())

    def test_two_copies_with_the_same_key_are_both_left_alone(self):
        self.owner_layout()
        shutil.copytree(self.ext / 'newspapers-seen-it', self.ext / 'seen-it copy')
        self.set_folder(self.ext.as_posix())
        before = _tree(self.ext)
        rc, out = self.update()
        self.assertEqual(rc, EXIT_WARNINGS)
        self.assertEqual(_tree(self.ext), before)
        self.assertIn('more than one folder', out)

    def test_helper_change_is_explained_without_running_any_installer(self):
        self.owner_layout()
        self.set_folder(self.ext.as_posix())
        _rc, out = self.update()
        self.assertIn('helper (host folder) changed too', out)
        self.assertIn('Nothing to re-run', out)


class FirstCopyAndSetting(_Base):

    def test_no_installed_copy_is_copied_in_with_load_unpacked_steps(self):
        self.ext.mkdir()
        self.set_folder(self.ext.as_posix())
        rc, out = self.update()
        self.assertEqual(rc, EXIT_CLEAN, out)
        fresh = self.ext / 'seen-it'
        self.assertEqual(json.loads((fresh / 'manifest.json').read_text(encoding='utf-8'))['key'], KEY)
        self.assertFalse((fresh / 'tests').exists())
        self.assertFalse((self.ext / 'seen-it.part').exists())
        self.assertIn('Load unpacked', out)
        self.assertIn(str(fresh), out)
        _rc, again = self.update()
        self.assertNotIn('Load unpacked', again)
        self.assertIn('up to date', again)

    def test_a_different_folder_already_named_seen_it_is_not_overwritten(self):
        _write(self.ext / 'seen-it' / 'mine.txt', 'not an add-on\n')
        self.set_folder(self.ext.as_posix())
        rc, out = self.update()
        self.assertEqual(rc, EXIT_WARNINGS)
        self.assertEqual(_tree(self.ext / 'seen-it'), {'mine.txt': b'not an add-on\n'})

    def test_no_setting_and_no_terminal_leaves_extensions_alone_with_a_hint(self):
        installed = self.owner_layout()
        before = _tree(self.ext)
        rc, out = self.update()            # no `ask`: nobody at the keyboard
        self.assertEqual(rc, EXIT_CLEAN, out)
        self.assertEqual(_tree(self.ext), before)
        self.assertTrue((installed / 'manifest.json').is_file())
        self.assertIn('browser_extensions: extensions', out)
        self.assertNotIn('browser_extensions', (self.archive / 'fha.yaml').read_text(encoding='utf-8'))

    def test_non_terminal_command_line_run_never_prompts(self):
        args = argparse.Namespace(repo=str(self.repo), root=str(self.archive),
                                  dry_run=False, verbose=False)
        with mock.patch.object(scaffold.sys, 'stdin') as stdin, \
                mock.patch('builtins.input', side_effect=_never_ask):
            stdin.isatty.return_value = False
            rc, out = _quiet(scaffold._cmd_update_tools, args)
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('Browser add-ons: not set up', out)

    def test_prompt_yes_with_a_typed_folder_saves_it_and_installs(self):
        answers = iter(['y', f'"{self.ext}"'])       # pasted with quotes
        rc, out = self.update(ask=lambda _p: next(answers))
        self.assertEqual(rc, EXIT_CLEAN, out)
        text = (self.archive / 'fha.yaml').read_text(encoding='utf-8')
        self.assertIn('# my archive settings - keep this comment\nroots:\n  photos: photos\n', text)
        import yaml
        self.assertEqual(Path(yaml.safe_load(text)['browser_extensions']), self.ext)
        self.assertTrue((self.ext / 'seen-it' / 'manifest.json').is_file())
        # Asked once: the next run reads the saved line.
        rc, _out = self.update(ask=_never_ask)
        self.assertEqual(rc, EXIT_CLEAN)

    def test_prompt_enter_takes_the_default_folder_inside_the_archive(self):
        answers = iter(['yes', ''])
        rc, out = self.update(ask=lambda _p: next(answers))
        self.assertEqual(rc, EXIT_CLEAN, out)
        self.assertIn('browser_extensions: extensions\n',
                      (self.archive / 'fha.yaml').read_text(encoding='utf-8'))
        self.assertTrue((self.archive / 'extensions' / 'seen-it' / 'manifest.json').is_file())

    def test_prompt_no_is_saved_and_never_asked_again(self):
        rc, out = self.update(ask=lambda _p: 'n')
        self.assertEqual(rc, EXIT_CLEAN, out)
        self.assertIn('browser_extensions: none\n',
                      (self.archive / 'fha.yaml').read_text(encoding='utf-8'))
        self.assertIn('you will not be asked again', out)
        rc, again = self.update(ask=_never_ask)
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertNotIn('Browser add-ons', again)

    def test_existing_setting_means_no_prompt(self):
        self.ext.mkdir()
        self.set_folder(f'"{self.ext.as_posix()}"')
        rc, _out = self.update(ask=_never_ask)
        self.assertEqual(rc, EXIT_CLEAN)

    def test_install_asks_once_and_copies_the_add_on(self):
        archive = self.tmp / 'Second Archive'
        answers = iter(['y', ''])
        rc, out = _quiet(scaffold.run_install, archive, self.repo,
                         ask=lambda _p: next(answers))
        self.assertEqual(rc, EXIT_CLEAN, out)
        self.assertTrue((archive / 'extensions' / 'seen-it' / 'manifest.json').is_file())
        self.assertIn('Load unpacked', out)

    def test_ctrl_c_at_the_question_saves_nothing(self):
        def interrupted(_p):
            raise KeyboardInterrupt
        before = (self.archive / 'fha.yaml').read_bytes()
        rc, _out = self.update(ask=interrupted)
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual((self.archive / 'fha.yaml').read_bytes(), before)

    def test_a_folder_inside_the_archive_records_is_refused(self):
        self.set_folder('sources/addons')
        rc, out = self.update()
        self.assertEqual(rc, EXIT_WARNINGS)
        self.assertFalse((self.archive / 'sources' / 'addons').exists())
        self.assertIn('browser_extensions', out)

    def test_windows_style_path_with_spaces_and_backslashes(self):
        installed = self.owner_layout()
        self.set_folder(f"'{self.ext}'")           # native separators, quoted
        rc, out = self.update()
        self.assertEqual(rc, EXIT_CLEAN, out)
        self.assertEqual((installed / 'background.js').read_text(encoding='utf-8'),
                         '// background 0.2.0\n')


class CaptureAddOnReload(_Base):

    def test_capture_add_on_update_says_to_reload_it(self):
        _write(self.repo / 'browser-companion' / 'src' / 'panel.js', '// v2\n')
        scaffold._write_manifest(self.repo)
        rc, out = self.update()
        self.assertEqual(rc, EXIT_CLEAN, out)
        self.assertIn('click the reload arrow on "Capture (Test)"', out)
        _rc, again = self.update()
        self.assertNotIn('reload arrow on "Capture (Test)"', again)


class ArchiveToolsIgnoreTheFolder(unittest.TestCase):
    """An `extensions/` folder inside the archive changes nothing lint or index report.

    A guard, not a fix: the default folder is inside the archive, and Seen It's
    README.md and manifest.json must not read as records or as citations.
    """

    def test_lint_and_index_report_the_same_with_the_folder_present(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TEMP') or None) as tmp:
            archive = Path(tmp) / 'example archive'
            shutil.copytree(ROOT / 'example-archive', archive)
            fha = [sys.executable, str(ROOT / 'tools' / 'fha.py')]

            def run_both():
                outs = []
                for cmd in (['index'], ['lint', '--format-check']):
                    proc = subprocess.run(fha + cmd, cwd=archive, capture_output=True,
                                          text=True, encoding='utf-8', errors='replace')
                    outs.append((proc.returncode, proc.stdout))
                return outs
            without = run_both()
            shutil.copytree(ROOT / 'seen-it', archive / 'extensions' / 'seen-it')
            self.assertEqual(run_both(), without)


if __name__ == '__main__':
    unittest.main()
