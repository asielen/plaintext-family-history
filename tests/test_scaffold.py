"""
Tests for tools/scaffold.py — `fha install` and `fha update-tools` (BUILD.md M9).

Two layers:
  1. A manifest-sync guard that recomputes the operating-layer manifest from the
     real repo and asserts the committed manifest.json still matches it — so a PR
     that changes a tool/doc/skeleton file but forgets to regenerate fails here.
  2. Behavior tests against a small, hand-built FAKE repo (no .git/, proving the
     git-free / zip install path) and throwaway archives, exercising install,
     re-install refusal, the four update outcomes (add/stock/customized/retired),
     the critical skeleton-is-never-touched safety property, dry-run no-ops, and
     the friendly error paths.

Run: python -m unittest tests.test_scaffold -v
"""

import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import scaffold
from _lib import EXIT_CLEAN, EXIT_FAILURE, EXIT_WARNINGS


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def _make_fake_repo(repo: Path) -> Path:
    """Build a minimal public-repo clone: 3 operating files + 3 skeleton seeds.

    Deliberately tiny so the tests are fast and every file's role is obvious. No
    .git/ is created anywhere — install/update working against this directory is
    itself the proof of the zip-based, git-free path (BUILD.md M9.1).
    """
    _write(repo / 'SPEC.md', '# SPEC\n\n**Version 9.9 - 2026-01-01**\n\nbody\n')
    _write(repo / 'tools' / 'atool.py', 'print("a tool")\n')
    _write(repo / 'docs' / 'guide.md', '# Guide\n')
    # bytecode that must NOT enter the manifest
    _write(repo / 'tools' / '__pycache__' / 'atool.cpython-312.pyc', 'junk\n')
    # skeleton (remapped from archive-template/ → archive root)
    _write(repo / 'archive-template' / 'fha.yaml', 'roots:\n  photos: photos\n')
    _write(repo / 'archive-template' / 'places' / 'places.yaml', '# places\n')
    _write(repo / 'archive-template' / 'sources' / '.gitkeep', '')
    _write(repo / 'archive-template' / 'README.md', 'template readme — excluded\n')
    scaffold._write_manifest(repo)
    return repo


class ManifestSyncTest(unittest.TestCase):
    """The committed manifest.json must match what the repo currently contains."""

    def test_committed_manifest_is_current(self):
        committed = json.loads((ROOT / 'manifest.json').read_text(encoding='utf-8'))
        regenerated = scaffold.generate_manifest(ROOT)
        # `generated` is a date stamp that legitimately changes day to day; the
        # contract is the file set + checksums + versions.
        self.assertEqual(committed['manifest_version'], regenerated['manifest_version'])
        self.assertEqual(committed['spec_version'], regenerated['spec_version'])
        self.assertEqual(
            committed['files'], regenerated['files'],
            'manifest.json is out of date — run '
            '`python tools/scaffold.py write-manifest --repo .` and commit the result.',
        )

    def test_manifest_excludes_repo_furniture(self):
        paths = {e['path'] for e in scaffold.generate_manifest(ROOT)['files']}
        # Public-repo furniture that must never enter an archive: PRIVACY.md is the
        # "no real data" policy (contradictory inside a real archive), and the
        # release checklist / packing list / template's own readme are spec-repo
        # maintenance.
        for furniture in ('PRIVACY.md', 'RELEASE_CHECKLIST.md',
                          'manifest.json', 'archive-template/README.md'):
            self.assertNotIn(furniture, paths)
        # No bytecode, no example/test furniture.
        self.assertFalse(any('__pycache__' in p or p.endswith('.pyc') for p in paths))
        self.assertFalse(any(p.startswith(('example-archive/', 'tests/', 'archive-template/'))
                             for p in paths))

    def test_manifest_includes_operating_extras(self):
        paths = {e['path'] for e in scaffold.generate_manifest(ROOT)['files']}
        # Project orientation + the agent's workflow procedures ship into archives.
        self.assertIn('README.md', paths)
        self.assertIn('.claude/skills/README.md', paths)
        # but the spec-repo's own agent config does not.
        self.assertNotIn('.claude/settings.json', paths)

    def test_skeleton_and_operating_categories(self):
        files = scaffold.generate_manifest(ROOT)['files']
        by_path = {e['path']: e for e in files}
        # Skeleton seeds present, remapped, and carry a src that differs.
        self.assertEqual(by_path['fha.yaml']['category'], 'skeleton')
        self.assertEqual(by_path['fha.yaml']['src'], 'archive-template/fha.yaml')
        self.assertEqual(by_path['places/places.yaml']['category'], 'skeleton')
        # The five BUILD-mandated docs are present as operating.
        for doc in ('docs/GETTING_STARTED.md', 'docs/SETUP_FROM_ZIP.md',
                    'docs/CHEATSHEET.md', 'docs/TROUBLESHOOTING.md',
                    'docs/FILING_CABINET.md'):
            self.assertIn(doc, by_path, doc)
            self.assertEqual(by_path[doc]['category'], 'operating')
        # Operating entries carry no src remap.
        self.assertNotIn('src', by_path['tools/scaffold.py'])


class InstallTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _make_fake_repo(self.tmp / 'repo')
        self.archive = self.tmp / 'archive'

    def tearDown(self):
        self._tmp.cleanup()

    def test_install_copies_files_and_stamps(self):
        rc = scaffold.run_install(self.archive, self.repo)
        self.assertEqual(rc, EXIT_CLEAN)
        # operating + skeleton landed, remapped correctly
        self.assertTrue((self.archive / 'SPEC.md').is_file())
        self.assertTrue((self.archive / 'tools' / 'atool.py').is_file())
        self.assertTrue((self.archive / 'docs' / 'guide.md').is_file())
        self.assertTrue((self.archive / 'fha.yaml').is_file())
        self.assertTrue((self.archive / 'places' / 'places.yaml').is_file())
        self.assertTrue((self.archive / 'sources' / '.gitkeep').is_file())
        # archive-template/ folder itself is never created in the archive
        self.assertFalse((self.archive / 'archive-template').exists())
        # stamp records every copied file's checksum
        stamp = json.loads((self.archive / '.plainfile-version').read_text(encoding='utf-8'))
        self.assertIn('SPEC.md', stamp['files'])
        self.assertIn('fha.yaml', stamp['files'])
        self.assertEqual(stamp['manifest_version'], scaffold.MANIFEST_VERSION)

    def test_install_dry_run_writes_nothing(self):
        rc = scaffold.run_install(self.archive, self.repo, dry_run=True)
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(self.archive.exists())

    def test_reinstall_refused(self):
        scaffold.run_install(self.archive, self.repo)
        with self.assertRaises(scaffold.ScaffoldError) as ctx:
            scaffold.run_install(self.archive, self.repo)
        self.assertIn('already', str(ctx.exception).lower())

    def test_missing_source_refused_before_writing(self):
        (self.repo / 'tools' / 'atool.py').unlink()  # manifest still lists it
        with self.assertRaises(scaffold.ScaffoldError) as ctx:
            scaffold.run_install(self.archive, self.repo)
        self.assertIn('missing', str(ctx.exception).lower())
        # nothing half-written
        self.assertFalse(self.archive.exists())

    def test_missing_manifest_is_friendly(self):
        (self.repo / 'manifest.json').unlink()
        with self.assertRaises(scaffold.ScaffoldError) as ctx:
            scaffold.run_install(self.archive, self.repo)
        self.assertIn('manifest.json', str(ctx.exception))

    def test_python_too_old_is_a_hard_stop(self):
        with mock.patch.object(scaffold.sys, 'version_info', (3, 9, 0)):
            rc = scaffold._cmd_install(argparse.Namespace(
                archive_path=str(self.archive), repo=str(self.repo), dry_run=False))
        self.assertEqual(rc, EXIT_FAILURE)
        self.assertFalse(self.archive.exists())

    def test_exiftool_missing_is_only_advisory(self):
        with mock.patch('scaffold.shutil.which', return_value=None):
            rc = scaffold.run_install(self.archive, self.repo)
        self.assertEqual(rc, EXIT_CLEAN)  # install still succeeds
        self.assertTrue((self.archive / 'SPEC.md').is_file())


class UpdateToolsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _make_fake_repo(self.tmp / 'repo')
        self.archive = self.tmp / 'archive'
        scaffold.run_install(self.archive, self.repo)

    def tearDown(self):
        self._tmp.cleanup()

    def _stamp(self):
        return json.loads((self.archive / '.plainfile-version').read_text(encoding='utf-8'))

    def test_noop_when_current(self):
        rc = scaffold.run_update_tools(self.archive, self.repo)
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse((self.archive / '.plainfile-backup').exists())

    def test_stock_change_overwrites_silently(self):
        # Upstream improved atool.py; the archive's copy is still pristine.
        _write(self.repo / 'tools' / 'atool.py', 'print("a tool v2")\n')
        scaffold._write_manifest(self.repo)
        rc = scaffold.run_update_tools(self.archive, self.repo)
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual((self.archive / 'tools' / 'atool.py').read_text(encoding='utf-8'),
                         'print("a tool v2")\n')
        self.assertFalse((self.archive / '.plainfile-backup').exists())
        # stamp now records the new checksum
        self.assertEqual(self._stamp()['files']['tools/atool.py'],
                         scaffold._sha256_file(self.repo / 'tools' / 'atool.py'))

    def test_customized_file_is_backed_up_then_updated(self):
        # Archive owner edited their atool.py; upstream also moved on.
        _write(self.archive / 'tools' / 'atool.py', 'print("MY EDIT")\n')
        _write(self.repo / 'tools' / 'atool.py', 'print("a tool v2")\n')
        scaffold._write_manifest(self.repo)
        rc = scaffold.run_update_tools(self.archive, self.repo)
        self.assertEqual(rc, EXIT_CLEAN)
        # live file is the new stock
        self.assertEqual((self.archive / 'tools' / 'atool.py').read_text(encoding='utf-8'),
                         'print("a tool v2")\n')
        # the edit survives in the backup
        backups = list((self.archive / '.plainfile-backup').rglob('atool.py'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding='utf-8'), 'print("MY EDIT")\n')

    def test_skeleton_files_are_never_touched(self):
        # The critical safety property: a user's fha.yaml/places.yaml is data, not
        # operating layer — update-tools must leave it exactly as-is, even if
        # upstream's template changed.
        _write(self.archive / 'fha.yaml', 'roots:\n  photos: D:/MyPhotos\n')
        _write(self.archive / 'places' / 'places.yaml', '- id: L-abc\n  name: MyTown\n')
        _write(self.repo / 'archive-template' / 'fha.yaml', 'roots:\n  photos: changed\n')
        scaffold._write_manifest(self.repo)
        scaffold.run_update_tools(self.archive, self.repo)
        self.assertIn('MyPhotos', (self.archive / 'fha.yaml').read_text(encoding='utf-8'))
        self.assertIn('MyTown', (self.archive / 'places' / 'places.yaml').read_text(encoding='utf-8'))
        # never even backed up
        self.assertFalse((self.archive / '.plainfile-backup').exists())

    def test_retired_file_moved_to_backup(self):
        # Inject a recorded tool that the manifest no longer lists.
        retired = self.archive / 'tools' / 'oldtool.py'
        _write(retired, 'print("old")\n')
        stamp = self._stamp()
        stamp['files']['tools/oldtool.py'] = 'deadbeef'
        _write(self.archive / '.plainfile-version', json.dumps(stamp))
        rc = scaffold.run_update_tools(self.archive, self.repo)
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(retired.exists())
        moved = list((self.archive / '.plainfile-backup').rglob('oldtool.py'))
        self.assertEqual(len(moved), 1)
        # and it's dropped from the refreshed stamp
        self.assertNotIn('tools/oldtool.py', self._stamp()['files'])

    def test_added_file_copied_in(self):
        _write(self.repo / 'tools' / 'newtool.py', 'print("new")\n')
        scaffold._write_manifest(self.repo)
        scaffold.run_update_tools(self.archive, self.repo)
        self.assertTrue((self.archive / 'tools' / 'newtool.py').is_file())
        self.assertIn('tools/newtool.py', self._stamp()['files'])

    def test_dry_run_writes_nothing(self):
        _write(self.archive / 'tools' / 'atool.py', 'print("MY EDIT")\n')
        _write(self.repo / 'tools' / 'atool.py', 'print("v2")\n')
        scaffold._write_manifest(self.repo)
        rc = scaffold.run_update_tools(self.archive, self.repo, dry_run=True)
        self.assertEqual(rc, EXIT_CLEAN)
        # the customized file is left exactly as the user had it; no backup made
        self.assertEqual((self.archive / 'tools' / 'atool.py').read_text(encoding='utf-8'),
                         'print("MY EDIT")\n')
        self.assertFalse((self.archive / '.plainfile-backup').exists())

    def test_broken_clone_refused_before_any_mutation(self):
        # A file the manifest lists but the clone no longer ships must abort the
        # update before anything is copied or backed up.
        _write(self.archive / 'tools' / 'atool.py', 'print("MY EDIT")\n')  # would be customized
        _write(self.repo / 'tools' / 'newtool.py', 'print("new")\n')
        scaffold._write_manifest(self.repo)            # manifest now lists newtool.py
        (self.repo / 'tools' / 'newtool.py').unlink()  # ...then the source vanishes
        with self.assertRaises(scaffold.ScaffoldError) as ctx:
            scaffold.run_update_tools(self.archive, self.repo)
        self.assertIn('missing', str(ctx.exception).lower())
        # the customized file was NOT moved to backup
        self.assertFalse((self.archive / '.plainfile-backup').exists())
        self.assertEqual((self.archive / 'tools' / 'atool.py').read_text(encoding='utf-8'),
                         'print("MY EDIT")\n')

    def test_partial_failure_does_not_claim_success(self):
        # A per-file failure must not produce a success message or inflate the
        # summary counts (the output stays honest), and must surface the failure.
        _write(self.archive / 'tools' / 'atool.py', 'print("MY EDIT")\n')  # customized
        _write(self.repo / 'tools' / 'atool.py', 'print("v2")\n')
        scaffold._write_manifest(self.repo)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch('scaffold.shutil.move', side_effect=OSError('locked')):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = scaffold.run_update_tools(self.archive, self.repo)
        self.assertEqual(rc, EXIT_WARNINGS)
        self.assertNotIn('has been backed up', out.getvalue())          # no false success
        self.assertIn('Done: 0 added, 0 updated, 0 backed up', out.getvalue())  # honest counts
        self.assertIn('could not be updated', err.getvalue())           # failure surfaced
        # the edit is intact (the move failed before touching it); nothing backed up
        self.assertEqual((self.archive / 'tools' / 'atool.py').read_text(encoding='utf-8'),
                         'print("MY EDIT")\n')
        self.assertEqual(list((self.archive / '.plainfile-backup').rglob('atool.py')), [])

    def test_failed_update_keeps_edit_safe_on_retry(self):
        # Regression: a failed customized-file update must NOT record the edited
        # bytes as the installed baseline. If it did, the retry would see
        # disk == recorded, classify the file as pristine stock, and silently
        # overwrite the human's edit with no backup (data loss).
        _write(self.archive / 'tools' / 'atool.py', 'print("MY EDIT")\n')
        _write(self.repo / 'tools' / 'atool.py', 'print("v2")\n')
        scaffold._write_manifest(self.repo)
        # Run 1: move fails.
        with mock.patch('scaffold.shutil.move', side_effect=OSError('locked')):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(scaffold.run_update_tools(self.archive, self.repo), EXIT_WARNINGS)
        self.assertEqual((self.archive / 'tools' / 'atool.py').read_text(encoding='utf-8'),
                         'print("MY EDIT")\n')
        # Run 2: move works. The edit must be BACKED UP, not silently overwritten.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(scaffold.run_update_tools(self.archive, self.repo), EXIT_CLEAN)
        self.assertEqual((self.archive / 'tools' / 'atool.py').read_text(encoding='utf-8'),
                         'print("v2")\n')
        backups = list((self.archive / '.plainfile-backup').rglob('atool.py'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding='utf-8'), 'print("MY EDIT")\n')

    def test_failed_retired_move_is_retried_next_run(self):
        # A retired file whose move fails must stay recorded so the next run
        # re-detects and retries it (a successful move drops it from the stamp).
        retired = self.archive / 'tools' / 'oldtool.py'
        _write(retired, 'print("old")\n')
        stamp = json.loads((self.archive / '.plainfile-version').read_text(encoding='utf-8'))
        stamp['files']['tools/oldtool.py'] = 'deadbeef'
        _write(self.archive / '.plainfile-version', json.dumps(stamp))
        with mock.patch('scaffold.shutil.move', side_effect=OSError('locked')):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(scaffold.run_update_tools(self.archive, self.repo), EXIT_WARNINGS)
        self.assertTrue(retired.exists())  # move failed, file still there
        # still recorded → still detectable as retired
        new_stamp = json.loads((self.archive / '.plainfile-version').read_text(encoding='utf-8'))
        self.assertIn('tools/oldtool.py', new_stamp['files'])
        # retry succeeds and moves it to backup
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(scaffold.run_update_tools(self.archive, self.repo), EXIT_CLEAN)
        self.assertFalse(retired.exists())
        self.assertEqual(len(list((self.archive / '.plainfile-backup').rglob('oldtool.py'))), 1)

    def test_no_version_stamp_treats_existing_as_customized(self):
        # An archive whose tools were hand-copied (no install) still must not lose
        # a hand-edit on update.
        (self.archive / '.plainfile-version').unlink()
        _write(self.archive / 'tools' / 'atool.py', 'print("HAND EDIT")\n')
        scaffold.run_update_tools(self.archive, self.repo)
        backups = list((self.archive / '.plainfile-backup').rglob('atool.py'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding='utf-8'), 'print("HAND EDIT")\n')


class CmdErrorPathTest(unittest.TestCase):
    """The argparse bridges return friendly exit codes, never tracebacks."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _make_fake_repo(self.tmp / 'repo')

    def tearDown(self):
        self._tmp.cleanup()

    def test_update_missing_repo(self):
        rc = scaffold._cmd_update_tools(argparse.Namespace(
            repo=None, root=str(self.tmp), dry_run=False, verbose=False))
        self.assertEqual(rc, EXIT_FAILURE)

    def test_update_not_an_archive(self):
        # root points at a folder with no fha.yaml; no auto-detect either.
        empty = self.tmp / 'not-an-archive'
        empty.mkdir()
        # find_archive_root walks up from CWD; force the no-root branch by passing
        # a root that exists but isn't an archive — _cmd uses the explicit root, so
        # update runs and fails to find a manifest? No: it runs against that root.
        # Instead drop --root and patch find_archive_root to None.
        with mock.patch('scaffold.find_archive_root', return_value=None):
            rc = scaffold._cmd_update_tools(argparse.Namespace(
                repo=str(self.repo), root=None, dry_run=False, verbose=False))
        self.assertEqual(rc, EXIT_FAILURE)

    def test_update_explicit_root_must_be_an_archive(self):
        # A mistyped --root (a real folder, but not an archive) must be refused
        # before any operating-layer file is written into it.
        not_arch = self.tmp / 'not-an-archive'
        not_arch.mkdir()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = scaffold._cmd_update_tools(argparse.Namespace(
                repo=str(self.repo), root=str(not_arch), dry_run=False, verbose=False))
        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('does not look like an archive', err.getvalue())
        self.assertEqual(list(not_arch.iterdir()), [])  # nothing written

    def test_install_bad_repo_is_friendly_exit(self):
        rc = scaffold._cmd_install(argparse.Namespace(
            archive_path=str(self.tmp / 'arch'),
            repo=str(self.tmp / 'no-such-repo'), dry_run=False))
        self.assertEqual(rc, EXIT_FAILURE)


if __name__ == '__main__':
    unittest.main()
