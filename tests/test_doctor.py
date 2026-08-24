"""
test_doctor.py - fha doctor: counts parity, degraded checks, exit ladder.

Doctor had no dedicated tests. Three contracts locked here:

  - Counts parity: the index path (_counts_from_index, WHERE restricted = 1)
    and the scan path (_counts_from_scan, frontmatter walk) must report
    identical restricted/living counts - including a source restricted by a
    TYPED value (`restricted: by-request`, SPEC §19), which the old narrow
    `in (True, 'true')` idiom dropped on both paths: the index write stored 0
    and the scan test skipped it.

  - Degraded checks: a broken capture module (a partial tools update, say)
    must degrade the staged-captures check to a warning line with a next
    step, never kill the whole health report - doctor is the tool a human
    reaches for when something is already broken.

  - Exit ladder: a fresh archive with no caches built lands on 1 (warnings
    only - design decision D5, TOOLING §3a), never 2/3; an unreachable
    mapped root is an error (2).

Synthetic tmp archives only - the real archive is never a test bed.
"""

import datetime
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import capture
import doctor
import index
from _lib import (
    EXIT_ERRORS, EXIT_WARNINGS, index_manifest_path, record_path_manifest,
    write_path_manifest,
)


_PERSON = '''---
id: {pid}
name: {name}
living: {living}
tier: stub
---

# {name}
'''

_SOURCE = '''---
id: {sid}
title: {title}
source_type: other
{line}---

## Claims
'''


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def _make_archive(root: Path) -> None:
    """A small synthetic archive: three persons (living true/unknown/false)
    and three sources (restricted by-request / true / unrestricted). The
    by-request source is the parity linchpin - a typed value must count as
    restricted on both the index and scan paths."""
    _write(root / 'fha.yaml', 'roots: {}\n')
    _write(root / 'people' / 'smith__alice_P-aaaaaaaaaa.md',
           _PERSON.format(pid='P-aaaaaaaaaa', name='Alice Smith', living='true'))
    _write(root / 'people' / 'smith__bob_P-bbbbbbbbbb.md',
           _PERSON.format(pid='P-bbbbbbbbbb', name='Bob Smith', living='unknown'))
    _write(root / 'people' / 'smith__carol_P-cccccccccc.md',
           _PERSON.format(pid='P-cccccccccc', name='Carol Smith', living='false'))
    _write(root / 'sources' / 'other' / 'letter_S-1111111111.md',
           _SOURCE.format(sid='S-1111111111', title='Private letter',
                          line='restricted: by-request\n'))
    _write(root / 'sources' / 'other' / 'diary_S-2222222222.md',
           _SOURCE.format(sid='S-2222222222', title='Family diary',
                          line='restricted: true\n'))
    _write(root / 'sources' / 'other' / 'census_S-3333333333.md',
           _SOURCE.format(sid='S-3333333333', title='Census page', line=''))


def _scandir_denying(unreadable: Path):
    """An os.scandir stand-in that refuses to list `unreadable`.

    The fault goes in at `os.scandir` because `os.walk` resolves it at call
    time on every supported Python - that is what makes the `onerror` seam
    observable here. chmod cannot produce this: CI runs as root, which ignores
    mode bits, and Windows has no equivalent.

    What this deliberately does NOT rely on: that pathlib's `rglob` reaches the
    disk the same way. It does on 3.11/3.12/3.14, but NOT on the 3.10 floor
    (pathlib routes through an accessor object that bound `os.scandir` at
    import time, so a later patch is invisible) and not on 3.13. So the
    injection does not reproduce the pre-fix `rglob` behaviour on every version
    we support - a regression back to `rglob` is still caught everywhere, but
    on the floor it is caught by the warning going missing rather than by the
    folder reading as empty.
    """
    real_scandir = os.scandir
    target = unreadable.resolve()

    def scandir(path='.'):
        try:
            denied = Path(path).resolve() == target
        except (TypeError, ValueError, OSError):
            denied = False
        if denied:
            err = PermissionError(13, 'Permission denied')
            err.filename = str(path)
            raise err
        return real_scandir(path)

    return scandir


class StaleIndexCauseTests(unittest.TestCase):
    """"Stale, run fha index" is a dead end when fha index cannot fix it.

    A record folder that will not list holds the index at stale by design
    (the watermark reports 'now' rather than a number it cannot stand behind),
    so the human runs `fha index`, it stays stale, and doctor - the tool whose
    whole job is explaining the archive to him - has nothing more to say.
    `fha index` and `fha lint` (W123) both name the folder; doctor must too."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _make_archive(self.root)
        index.build_index(self.root, {})
        # Age the records and the index into the past, index last: without the
        # shut folder this archive reads 'fresh', so the test cannot pass by
        # accident. (Pushing the index into the FUTURE instead would mask the
        # fix - the honest 'now' watermark an unreadable folder produces would
        # still be older than the db.)
        now = time.time()
        for q in self.root.rglob('*'):
            if q.is_file() and '.cache' not in q.parts:
                os.utime(q, (now - 600, now - 600))
        os.utime(self.root / '.cache' / 'index.sqlite', (now - 300, now - 300))
        # #48: the #48 path manifest build_index wrote captured each record's
        # mtime BEFORE this loop rewound it, so every record now reads as
        # "modified" against that stale snapshot - a real staleness signal in
        # general (the file's mtime genuinely no longer matches what the
        # cache last saw), but not the one this test class is about. Resync
        # the manifest to the now-aged mtimes, exactly as an archive that had
        # legitimately settled into this state (aged records, an index built
        # after them) would have on disk - `newest_record_mtime` is a max, so
        # bumping every file down together does not need a real rebuild to
        # stay internally consistent, only the manifest catching up to it.
        write_path_manifest(index_manifest_path(self.root), record_path_manifest(self.root))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _report(self, denied: Path):
        with unittest.mock.patch('os.scandir', new=_scandir_denying(denied)):
            result = doctor.run_doctor(self.root, {})
        return result, '\n'.join(result.data['lines'])

    def test_without_a_shut_folder_the_index_reads_fresh(self) -> None:
        result = doctor.run_doctor(self.root, {})
        check = next(c for c in result.data['checks'] if c['id'] == 'index')
        self.assertEqual(check['status'], 'ok')

    def test_the_stale_line_names_the_folder_and_the_real_fix(self) -> None:
        result, report = self._report(self.root / 'sources' / 'other')
        self.assertIn('index: ', report)
        self.assertIn('stale', report)
        self.assertIn('sources/other', report)
        self.assertIn('could not be opened', report)
        self.assertIn('reconnect it', report)
        check = next(c for c in result.data['checks'] if c['id'] == 'index')
        self.assertEqual(check['unreadable_dirs'], ['sources/other'])

    def test_the_scanned_counts_say_they_are_a_floor(self) -> None:
        # These count privacy-bearing records, and they are counted by
        # walking the same folders that would not open.
        _result, report = self._report(self.root / 'people')
        self.assertIn('counts (scanned', report)
        self.assertIn('counted low', report)

    def test_a_readable_archive_says_none_of_this(self) -> None:
        report = '\n'.join(doctor.run_doctor(self.root, {}).data['lines'])
        self.assertNotIn('could not be opened', report)
        self.assertNotIn('counted low', report)


class CountsParityTests(unittest.TestCase):
    """Index-backed and scan-backed counts must agree exactly (the report
    switches between them on freshness, so a disagreement shows the human
    two different archives depending on when he last ran `fha index`)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _make_archive(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_index_and_scan_counts_agree_including_typed_restricted(self) -> None:
        scan = doctor._counts_from_scan(self.root)
        index.build_index(self.root, {})
        idx = doctor._counts_from_index(self.root)
        self.assertIsNotNone(idx, 'index counts unavailable after a fresh build')
        self.assertEqual(idx, scan)
        # And both agree on the truth: by-request + true = 2 restricted
        # (the typed value counted), one living, one unknown-living.
        self.assertEqual(scan, {'restricted': 2, 'living': 1, 'unknown': 1, 'unread': 0})


class StagedCapturesDegradeTests(unittest.TestCase):
    """A failing `capture.staged_bundles` must not kill the report: the
    staged-captures check degrades to a warning line naming the next step,
    every later section (counts, E018, backup reminder) still renders, and
    the exit lands on the warnings rung at worst-error, never a traceback."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _make_archive(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_report_completes_when_staged_bundles_raises(self) -> None:
        def _boom(fha_config):
            raise RuntimeError('bundles exploded')

        orig = capture.staged_bundles
        capture.staged_bundles = _boom
        try:
            result = doctor.run_doctor(self.root, {})
        finally:
            capture.staged_bundles = orig

        report = '\n'.join(result.data['lines'])
        self.assertIn('staged captures', report)
        self.assertIn('check skipped', report)
        self.assertIn('bundles exploded', report)
        self.assertIn('fha capture --ingest', report)   # the next step is named
        # The report ran to its end: counts and the closing backup reminder
        # both rendered after the failed check.
        self.assertIn('sources restricted:', report)
        self.assertIn('Backup policy', report)
        statuses = {c['id']: c['status'] for c in result.data['checks']}
        self.assertEqual(statuses.get('staged-captures'), 'warn')
        # Warnings at least (the degraded check contributes 1); errors only
        # if some other check independently found one - never clean, never 3.
        self.assertIn(result.exit_code, (EXIT_WARNINGS, EXIT_ERRORS))


class ExitLadderTests(unittest.TestCase):
    """The 0/1/2 ladder: a fresh archive with nothing built yet is warnings
    (D5 - doctor must be safe and useful before any caches exist); a broken
    mapped root is an error."""

    def test_fresh_empty_archive_exits_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / 'fha.yaml', 'roots: {}\n')
            result = doctor.run_doctor(root, {})
            self.assertEqual(result.exit_code, EXIT_WARNINGS)
            report = '\n'.join(result.data['lines'])
            self.assertIn('not yet built', report)   # absent index = warn, not error

    def test_unreachable_mapped_root_exits_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = str(root / 'no-such-folder')
            _write(root / 'fha.yaml', f'roots:\n  photos: {missing}\n')
            result = doctor.run_doctor(root, {'roots': {'photos': missing}})
            self.assertEqual(result.exit_code, EXIT_ERRORS)
            self.assertFalse(result.ok)
            self.assertIn('not reachable', '\n'.join(result.data['lines']))


class MediaOptionalDependencyTests(unittest.TestCase):
    """`fha media probe`'s two backends (#44), reported the way exiftool
    (binary, warn-if-missing) and Pillow/pypdf (optional package,
    info-if-missing) already are - ffprobe is the primary so its absence
    warns, PyAV is only the fallback so its absence alone is informational."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _make_archive(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _check(self, result, check_id: str) -> dict:
        return next(c for c in result.data['checks'] if c['id'] == check_id)

    def test_ffprobe_present_is_ok_and_never_warns(self) -> None:
        with unittest.mock.patch('doctor.shutil.which',
                                 side_effect=lambda name: '/usr/bin/%s' % name):
            result = doctor.run_doctor(self.root, {})
        check = self._check(result, 'ffprobe')
        self.assertEqual(check['status'], 'ok')

    def test_ffprobe_missing_warns_and_names_the_fix(self) -> None:
        real_which = shutil.which

        def fake_which(name):
            if name == 'ffprobe':
                return None
            return real_which(name)

        with unittest.mock.patch('doctor.shutil.which', side_effect=fake_which):
            result = doctor.run_doctor(self.root, {})
        check = self._check(result, 'ffprobe')
        self.assertEqual(check['status'], 'warn')
        self.assertGreaterEqual(result.exit_code, EXIT_WARNINGS)
        report = '\n'.join(result.data['lines'])
        self.assertIn('ffprobe', report)
        self.assertIn('media probe', report)

    def test_pyav_missing_is_informational_only_never_a_warning(self) -> None:
        """PyAV is only the fallback (ffprobe is primary) - its own absence
        must not, by itself, move the exit code the way ffprobe's does.

        `run_doctor` does `import importlib.util as _ilu` LOCALLY (matching
        the existing Jinja2/Pillow/pypdf checks right above it), so the alias
        is bound to the one real `importlib.util` module object each call -
        patching `find_spec` there, not a nonexistent `doctor._ilu`, is what
        actually reaches it.
        """
        import importlib.util as real_ilu
        real_find_spec = real_ilu.find_spec

        def fake_find_spec(name, *a, **kw):
            if name == 'av':
                return None
            return real_find_spec(name, *a, **kw)

        with unittest.mock.patch('doctor.shutil.which',
                                 side_effect=lambda name: '/usr/bin/%s' % name), \
             unittest.mock.patch('importlib.util.find_spec', side_effect=fake_find_spec):
            result = doctor.run_doctor(self.root, {})
        check = self._check(result, 'pyav')
        self.assertEqual(check['status'], 'info')

    def test_pyav_present_is_ok(self) -> None:
        with unittest.mock.patch('doctor.shutil.which',
                                 side_effect=lambda name: '/usr/bin/%s' % name):
            result = doctor.run_doctor(self.root, {})
        check = self._check(result, 'pyav')
        self.assertIn(check['status'], ('ok', 'info'))   # 'ok' when PyAV is actually installed here


class BackupStampTests(unittest.TestCase):
    """The backup reminder reads real state from `.cache/last_backup.json`
    (written by `fha backup`) - the actual date and zip when a stamp exists,
    an honest "none recorded" when it doesn't - and stays info-level either
    way: the reminder's job is to name the command and the date, never to
    turn a fresh archive's health check red (plan 04 / TOOLING §13e)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _make_archive(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _backup_check(self, result) -> dict:
        return next(c for c in result.data['checks'] if c['id'] == 'backup')

    def test_stamp_present_reports_date_and_zip(self) -> None:
        stamp = {
            'date': (datetime.datetime.now() - datetime.timedelta(days=3)
                     ).isoformat(timespec='seconds'),
            'zip': str(self.root.parent / 'arch-backups' / 'arch-backup_x.zip'),
            'files': 12, 'bytes': 3456, 'assets_included': False,
        }
        cache = self.root / '.cache'
        cache.mkdir(exist_ok=True)
        (cache / 'last_backup.json').write_text(json.dumps(stamp), encoding='utf-8')

        result = doctor.run_doctor(self.root, {})
        check = self._backup_check(result)
        self.assertEqual(check['status'], 'ok')
        self.assertIn('3 days ago', check['detail'])
        self.assertIn('arch-backup_x.zip', check['detail'])
        report = '\n'.join(result.data['lines'])
        self.assertIn('last backup:', report)
        self.assertIn('records only', report)
        # Exit contribution stays CLEAN: only the usual fresh-archive
        # warnings (absent caches) set the code, never the backup check.
        self.assertEqual(result.exit_code, EXIT_WARNINGS)

    def test_an_incomplete_backup_says_so_where_he_reads_it(self) -> None:
        """The stamp is where a human checks whether he is covered.

        `fha backup` refuses to write a zip it could not fill, so only his own
        --allow-incomplete run can produce this stamp - and "last backup: 1
        day ago" over a zip missing half his photos is the same false comfort
        one layer further out."""
        stamp = {
            'date': (datetime.datetime.now() - datetime.timedelta(days=1)
                     ).isoformat(timespec='seconds'),
            'zip': str(self.root.parent / 'arch-backup_x-INCOMPLETE.zip'),
            'files': 3, 'bytes': 99, 'assets_included': True,
            'complete': False, 'unreadable_dirs': ['photos/1975'],
        }
        cache = self.root / '.cache'
        cache.mkdir(exist_ok=True)
        (cache / 'last_backup.json').write_text(json.dumps(stamp), encoding='utf-8')

        result = doctor.run_doctor(self.root, {})
        report = '\n'.join(result.data['lines'])
        self.assertIn('INCOMPLETE', report)
        self.assertIn('photos/1975', report)
        self.assertIn('fha backup', report)
        check = self._backup_check(result)
        self.assertIn('incomplete', check['detail'])
        # Still info-level: he chose this backup, and the check's documented
        # contract is that it never moves the exit code.
        self.assertEqual(check['status'], 'info')

    def test_a_stamp_without_the_complete_key_is_read_as_complete(self) -> None:
        # Written before the key existed; it was a complete backup and must
        # not start reporting itself as short.
        stamp = {
            'date': datetime.datetime.now().isoformat(timespec='seconds'),
            'zip': 'old.zip', 'files': 1, 'bytes': 1, 'assets_included': False,
        }
        cache = self.root / '.cache'
        cache.mkdir(exist_ok=True)
        (cache / 'last_backup.json').write_text(json.dumps(stamp), encoding='utf-8')
        result = doctor.run_doctor(self.root, {})
        self.assertNotIn('INCOMPLETE', '\n'.join(result.data['lines']))
        self.assertEqual(self._backup_check(result)['status'], 'ok')

    def test_stamp_absent_names_the_command_and_stays_clean(self) -> None:
        result = doctor.run_doctor(self.root, {})
        check = self._backup_check(result)
        self.assertEqual(check['status'], 'info')
        self.assertIn('fha backup', check['next_step'])
        report = '\n'.join(result.data['lines'])
        self.assertIn('none recorded', report)
        self.assertIn('restore = unzip', report)
        self.assertEqual(result.exit_code, EXIT_WARNINGS)

    def test_timezone_aware_stamp_date_reports_instead_of_crashing(self) -> None:
        """A stamp date carrying a timezone (a hand-edit, a foreign tool's
        stamp) must report normally: naive-now minus aware-when used to raise
        an uncaught TypeError that killed the whole doctor run, violating the
        check's 'unreadable = treated as absent' promise."""
        when = (datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(days=3))
        stamp = {
            'date': when.isoformat(timespec='seconds'),   # e.g. ...+00:00
            'zip': str(self.root.parent / 'arch-backups' / 'arch-backup_x.zip'),
            'files': 12, 'bytes': 3456, 'assets_included': False,
        }
        cache = self.root / '.cache'
        cache.mkdir(exist_ok=True)
        (cache / 'last_backup.json').write_text(json.dumps(stamp), encoding='utf-8')

        result = doctor.run_doctor(self.root, {})
        check = self._backup_check(result)
        self.assertEqual(check['status'], 'ok')
        self.assertIn('3 days ago', check['detail'])
        self.assertIn('last backup:', '\n'.join(result.data['lines']))
        self.assertEqual(result.exit_code, EXIT_WARNINGS)

    def test_unreadable_stamp_degrades_to_none_recorded(self) -> None:
        cache = self.root / '.cache'
        cache.mkdir(exist_ok=True)
        (cache / 'last_backup.json').write_text('{not json', encoding='utf-8')
        result = doctor.run_doctor(self.root, {})
        check = self._backup_check(result)
        self.assertEqual(check['status'], 'info')
        self.assertIn('unreadable', check['detail'])
        self.assertIn('fha backup', check['next_step'])
        self.assertEqual(result.exit_code, EXIT_WARNINGS)


@unittest.skipUnless(shutil.which('git'), 'requires git on PATH')
class SourcesGitignoreTests(unittest.TestCase):
    """#57: an unanchored `.gitignore` pattern (`photos/` instead of
    `/photos/`) also matches `sources/photos/` at any depth - silently
    dropping SOURCE RECORDS from version control, not the binary asset the
    pattern was meant for. Nothing else surfaces this (lint is clean, `git
    status` shows nothing), so doctor has to ask git directly - and stay
    silent, never crash, when there is no git repo or no git binary at all,
    since neither is an error condition for doctor."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write(self.root / 'fha.yaml',
               'roots:\n  photos: photos\n  documents: documents\n')
        (self.root / 'photos').mkdir()
        (self.root / 'documents').mkdir()
        subprocess.run(['git', 'init', '-q'], cwd=self.root, check=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _fha_config(self) -> dict:
        return {'roots': {'photos': 'photos', 'documents': 'documents'}}

    def _gitignore_check(self, result):
        return next(
            (c for c in result.data['checks'] if c['id'] == 'sources_gitignore'),
            None,
        )

    def test_unanchored_pattern_is_flagged_with_pattern_and_fix(self) -> None:
        _write(self.root / '.gitignore', 'photos/\ndocuments/\ninbox/\n')
        (self.root / 'sources' / 'photos').mkdir(parents=True)

        result = doctor.run_doctor(self.root, self._fha_config())
        report = '\n'.join(result.data['lines'])

        self.assertIn('sources ignored', report)
        self.assertIn('sources/photos', report)
        self.assertIn('photos/', report)          # the offending pattern, named
        self.assertIn('/photos/', report)         # the one-line repair, named
        check = self._gitignore_check(result)
        self.assertIsNotNone(check, 'doctor must report a sources_gitignore finding')
        self.assertEqual(check['status'], 'warn')
        self.assertGreaterEqual(result.exit_code, EXIT_WARNINGS)

    def test_anchored_pattern_is_clean(self) -> None:
        _write(self.root / '.gitignore', '/photos/\n/documents/\n/inbox/\n')
        (self.root / 'sources' / 'photos').mkdir(parents=True)

        result = doctor.run_doctor(self.root, self._fha_config())
        report = '\n'.join(result.data['lines'])

        self.assertNotIn('sources ignored', report)
        self.assertIsNone(self._gitignore_check(result))

    def test_no_gitignore_file_is_clean(self) -> None:
        (self.root / 'sources' / 'photos').mkdir(parents=True)
        result = doctor.run_doctor(self.root, self._fha_config())
        self.assertIsNone(self._gitignore_check(result))

    def test_not_a_git_repo_is_silent_never_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp2:
            root2 = Path(tmp2)
            _write(root2 / 'fha.yaml', 'roots:\n  photos: photos\n')
            (root2 / 'photos').mkdir()
            _write(root2 / '.gitignore', 'photos/\n')     # no `git init` at all
            (root2 / 'sources' / 'photos').mkdir(parents=True)

            result = doctor.run_doctor(root2, {'roots': {'photos': 'photos'}})
            self.assertIsNone(self._gitignore_check(result))
            self.assertNotIn('sources ignored', '\n'.join(result.data['lines']))

    def test_git_binary_missing_is_silent_never_an_error(self) -> None:
        """A broken PATH (git uninstalled or unreachable) must degrade this
        one check to silence, exactly like the .gitattributes check it
        borrows the pattern from - never a traceback, never a false finding."""
        _write(self.root / '.gitignore', 'photos/\n')
        (self.root / 'sources' / 'photos').mkdir(parents=True)

        orig_run = doctor.subprocess.run

        def _no_git(cmd, **kwargs):
            if 'check-ignore' in cmd:
                raise FileNotFoundError('git not found on PATH')
            return orig_run(cmd, **kwargs)

        doctor.subprocess.run = _no_git
        try:
            result = doctor.run_doctor(self.root, self._fha_config())
        finally:
            doctor.subprocess.run = orig_run

        self.assertIsNone(self._gitignore_check(result))
        self.assertNotIn('sources ignored', '\n'.join(result.data['lines']))

    def test_custom_source_type_folder_is_probed_too(self) -> None:
        """Not just photos/documents from roots: - any real subfolder under
        sources/ is probed, so a custom source_type folder name colliding
        with some other unrelated pattern is caught too."""
        _write(self.root / '.gitignore', 'newspapers/\n')
        (self.root / 'sources' / 'newspapers').mkdir(parents=True)

        result = doctor.run_doctor(self.root, self._fha_config())
        report = '\n'.join(result.data['lines'])
        self.assertIn('sources/newspapers', report)

    def test_whitelist_style_gitignore_produces_no_finding(self) -> None:
        """PR #60 review finding 1: `git check-ignore -v` also prints a line
        for a `!`-negated pattern - the path is NOT ignored, and git still
        exits 0 - so a whitelist-shaped .gitignore (`*` then `!sources/` /
        `!sources/**`, the common way to track only sources/ in an otherwise-
        ignored tree) must NOT be reported as a finding. Verified against
        real git first (not reasoned about): `git check-ignore -v` on this
        exact shape prints `.gitignore:3:!sources/**  sources/photos/...`
        and exits 0, while `git add -n` confirms the path is genuinely
        trackable. The old code counted every printed -v line as a finding,
        so this shape produced a false "sources ignored" warning - and its
        remedy (`change !sources/** to /!sources/**`) is invalid syntax that
        git parses as a LITERAL path named "!sources/**", silencing the
        unignore rule and re-ignoring the file: the fix would cause the very
        data-loss bug #57 exists to catch."""
        _write(self.root / '.gitignore', '*\n!sources/\n!sources/**\n')
        (self.root / 'sources' / 'photos').mkdir(parents=True)

        result = doctor.run_doctor(self.root, self._fha_config())
        report = '\n'.join(result.data['lines'])

        self.assertNotIn('sources ignored', report)
        self.assertIsNone(self._gitignore_check(result))

    def test_nested_archive_inside_parent_repo_is_still_checked(self) -> None:
        """PR #60 review finding 2: the archive itself need not hold `.git` -
        a normal way to keep an archive is as a records folder inside a
        larger personal git repo, where the archive's files are still
        subject to a `.gitignore` higher up the tree. Verified against real
        git first: `git check-ignore -v` run with cwd set to a subfolder
        that has no `.git` of its own still walks up and answers from the
        parent repo's .gitignore. The old `(archive_root / '.git').exists()`
        gate skipped this case entirely - exactly where #57 bites - even
        though `_check_sources_gitignore` already degrades silently on its
        own when git cannot answer, making the gate redundant everywhere
        else and harmful here."""
        with tempfile.TemporaryDirectory() as tmp2:
            parent = Path(tmp2)
            subprocess.run(['git', 'init', '-q'], cwd=parent, check=True)
            _write(parent / '.gitignore', 'photos/\ndocuments/\ninbox/\n')
            archive = parent / 'family-archive'
            _write(archive / 'fha.yaml',
                   'roots:\n  photos: photos\n  documents: documents\n')
            (archive / 'photos').mkdir()
            (archive / 'documents').mkdir()
            (archive / 'sources' / 'photos').mkdir(parents=True)
            self.assertFalse((archive / '.git').exists(),
                             'the archive itself must NOT hold its own .git')

            result = doctor.run_doctor(archive, self._fha_config())
            report = '\n'.join(result.data['lines'])

            self.assertIn('sources ignored', report)
            check = self._gitignore_check(result)
            self.assertIsNotNone(
                check, 'a nested archive still owes a sources_gitignore finding')

    def test_nested_gitignore_gets_a_remedy_that_actually_works(self) -> None:
        """PR #60 review round 2, finding 1: when the offending pattern lives
        in a NESTED .gitignore (sources/.gitignore, not the archive root's),
        the round-1 anchoring advice - 'change `photos/` to `/photos/`' - is
        a no-op: a leading slash in a nested file anchors to THAT FILE's own
        directory (still sources/photos/), not the archive root. Verified
        against real git: after applying the round-1 advice literally,
        `git add -n sources/photos/rec.md` still refuses the file. The fixed
        report must (a) name the file that actually holds the pattern, (b)
        say plainly that anchoring there will not help, and (c) not repeat
        the round-1 wording verbatim for this case."""
        _write(self.root / 'sources' / '.gitignore', 'photos/\n')
        (self.root / 'sources' / 'photos').mkdir(parents=True)

        result = doctor.run_doctor(self.root, self._fha_config())
        report = '\n'.join(result.data['lines'])

        self.assertIn('sources ignored', report)
        self.assertIn('sources/.gitignore', report)          # names the real file
        self.assertIn('sources/photos', report)
        # The round-1 remedy text must NOT be offered for a nested file - it
        # does not fix anything there (verified against real git above).
        self.assertNotIn('anchor it to the archive root', report)
        # A remedy that is verified to work: delete the line, or negate it
        # in place, in the SAME (nested) file.
        self.assertIn('!photos/', report)

    def test_nonascii_source_folder_name_is_reported_cleanly(self) -> None:
        """PR #60 review round 2, finding 2: a `sources/fötos/` folder
        produced a mangled, unfollowable message on Windows - the pattern
        rendered as `fÃ¶tos/` (locale-codepage mis-decode of git's UTF-8
        output) and the probe path arrived C-quoted (`"sources/f\\303\\266
        tos/probe.md"`, since git quotes non-ASCII paths by default), so the
        `.../probe.md` suffix strip silently failed and the raw quoted probe
        path leaked into the report with a stray `/` appended. A genealogy
        archive is exactly where `Kraków`, `Suwałki`, `fötos` appear, so this
        is not a corner case here. Verified against real git first (not
        reasoned about): `git -c core.quotePath=false check-ignore -v`
        prints the path unquoted, and decoding the subprocess output with
        `encoding='utf-8'` (not the implicit locale codepage) renders it
        correctly."""
        _write(self.root / '.gitignore', 'fötos/\n')  # 'fötos/'
        (self.root / 'sources' / 'fötos').mkdir(parents=True)

        result = doctor.run_doctor(self.root, self._fha_config())
        report = '\n'.join(result.data['lines'])
        gi_line = next((l for l in result.data['lines']
                         if l.startswith('sources ignored')), '')

        self.assertIn('sources ignored', report)
        self.assertIn('sources/fötos/', gi_line)   # the folder, spelled correctly
        self.assertNotIn('Ã', gi_line)                   # locale-codepage mojibake
        self.assertNotIn('\\303\\266', gi_line)          # git's C-quoted octal escape
        self.assertNotIn('"', gi_line)                   # no stray quoting artifact
        self.assertNotIn('probe.md"', gi_line)            # the failed-strip symptom

    def test_source_folder_name_with_spaces_still_works(self) -> None:
        """Not a bug found in review, but named explicitly as a case that
        must not regress while fixing the non-ASCII quoting/encoding bug
        above: git does NOT C-quote a path for a plain space (only for
        non-ASCII or other special bytes), so this already worked - keep it
        working under the `-c core.quotePath=false` + explicit `utf-8`
        decode change."""
        _write(self.root / '.gitignore', 'family photos/\n')
        (self.root / 'sources' / 'family photos').mkdir(parents=True)

        result = doctor.run_doctor(self.root, self._fha_config())
        report = '\n'.join(result.data['lines'])
        gi_line = next((l for l in result.data['lines']
                         if l.startswith('sources ignored')), '')

        self.assertIn('sources ignored', report)
        self.assertIn('sources/family photos/', gi_line)
        self.assertNotIn('"', gi_line)


class OrphanedBackPhotosTests(unittest.TestCase):
    """#113: a `-back`/`_back` photo sibling sitting on disk but not listed
    in its source's `files:` - the standing safety net for anything the
    `fha process` import-time fix (its own back-sibling pull-in) didn't
    catch: a photo filed before that fix existed, a variation set the human
    answered "separate"/"skip" for, or anything filed by hand outside
    `fha process` altogether."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write(self.root / 'fha.yaml',
               'roots:\n  photos: photos\n  documents: documents\n')
        (self.root / 'photos').mkdir()
        (self.root / 'documents').mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _fha_config(self) -> dict:
        return {'roots': {'photos': 'photos', 'documents': 'documents'}}

    def _finding(self, result):
        return next(
            (c for c in result.data['checks'] if c['id'] == 'orphaned_back_photos'),
            None,
        )

    def test_unlisted_back_sibling_is_flagged(self) -> None:
        (self.root / 'photos' / 'x-00100.jpg').write_bytes(b'front')
        (self.root / 'photos' / 'x-00100-back.jpg').write_bytes(b'back')
        _write(self.root / 'sources' / 'photos' / 'card_S-1111111111.md', _SOURCE.format(
            sid='S-1111111111', title='Card',
            line='files:\n  - file: photos/x-00100.jpg\n    role: primary\n'))

        result = doctor.run_doctor(self.root, self._fha_config())
        report = '\n'.join(result.data['lines'])

        self.assertIn('orphaned back scan', report)
        self.assertIn('photos/x-00100-back.jpg', report)
        check = self._finding(result)
        self.assertIsNotNone(check, 'doctor must report an orphaned_back_photos finding')
        self.assertEqual(check['status'], 'warn')
        self.assertGreaterEqual(result.exit_code, EXIT_WARNINGS)

    # ── Codex review, PR #145 followups (PR #161), finding 2 ─────────────────

    def test_unlisted_back_sibling_different_case_is_flagged(self) -> None:
        # Finding #2: a back scan saved in a DIFFERENT CASE than the primary
        # must still be found. The old hand-built candidate check only ever
        # probed the exact lowercase `{base_id}-back{ext}` / `_back{ext}`
        # spellings, so a back saved as `x-00200-BACK.jpg` was invisible to
        # it on a case-sensitive filesystem - the fix scans the directory and
        # parses each candidate's own stem instead, the same way
        # `process.py`'s `_find_back_sibling` already does at import time.
        (self.root / 'photos' / 'x-00200.jpg').write_bytes(b'front')
        (self.root / 'photos' / 'x-00200-BACK.jpg').write_bytes(b'back')
        _write(self.root / 'sources' / 'photos' / 'card2_S-1111111112.md', _SOURCE.format(
            sid='S-1111111112', title='Card 2',
            line='files:\n  - file: photos/x-00200.jpg\n    role: primary\n'))

        result = doctor.run_doctor(self.root, self._fha_config())
        report = '\n'.join(result.data['lines'])

        self.assertIn('orphaned back scan', report)
        self.assertIn('photos/x-00200-BACK.jpg', report)
        check = self._finding(result)
        self.assertIsNotNone(check)
        self.assertEqual(check['status'], 'warn')

    def test_unlisted_back_sibling_different_extension_is_flagged(self) -> None:
        # Finding #2: a back scan saved with a DIFFERENT EXTENSION than the
        # primary must still be found - `x-00300-back.jpeg` beside a
        # `x-00300.jpg` primary, invisible to the old
        # `{base_id}-back{PRIMARY's own extension}` guess.
        (self.root / 'photos' / 'x-00300.jpg').write_bytes(b'front')
        (self.root / 'photos' / 'x-00300-back.jpeg').write_bytes(b'back')
        _write(self.root / 'sources' / 'photos' / 'card3_S-1111111113.md', _SOURCE.format(
            sid='S-1111111113', title='Card 3',
            line='files:\n  - file: photos/x-00300.jpg\n    role: primary\n'))

        result = doctor.run_doctor(self.root, self._fha_config())
        report = '\n'.join(result.data['lines'])

        self.assertIn('orphaned back scan', report)
        self.assertIn('photos/x-00300-back.jpeg', report)

    # ── Codex review, PR #145 followups (PR #161), finding 5 ─────────────────

    def test_missing_primary_routes_to_reconcile_not_a_dead_process_command(self) -> None:
        # Finding #5: when the source's LISTED primary is itself missing from
        # disk while its back scan still sits there, the suggested command
        # must not be `fha process <missing primary> --more ...` - that
        # command's positional FILE argument would not exist, so it would
        # fail the instant it is run. The suggestion must route to the
        # missing-file recovery path (`fha reconcile`) instead.
        (self.root / 'photos' / 'x-00400-back.jpg').write_bytes(b'back')
        # x-00400.jpg (the listed primary) is deliberately never created.
        _write(self.root / 'sources' / 'photos' / 'card4_S-1111111114.md', _SOURCE.format(
            sid='S-1111111114', title='Card 4',
            line='files:\n  - file: photos/x-00400.jpg\n    role: primary\n'))

        result = doctor.run_doctor(self.root, self._fha_config())
        report = '\n'.join(result.data['lines'])

        self.assertIn('orphaned back scan', report)
        self.assertIn('missing from disk', report)
        self.assertIn('reconcile', report)
        self.assertNotIn(
            f'{doctor._LAUNCHER} process {doctor.shell_quote("photos/x-00400.jpg")}', report)
        check = self._finding(result)
        self.assertIsNotNone(check)
        self.assertEqual(check['status'], 'warn')

    def test_listed_back_is_clean(self) -> None:
        (self.root / 'photos' / 'x-00100.jpg').write_bytes(b'front')
        (self.root / 'photos' / 'x-00100-back.jpg').write_bytes(b'back')
        _write(self.root / 'sources' / 'photos' / 'card_S-2222222222.md', _SOURCE.format(
            sid='S-2222222222', title='Card',
            line=('files:\n'
                  '  - file: photos/x-00100.jpg\n    role: primary\n'
                  '  - file: photos/x-00100-back.jpg\n    role: back\n')))

        result = doctor.run_doctor(self.root, self._fha_config())
        self.assertIsNone(self._finding(result))
        self.assertNotIn('orphaned back scan', '\n'.join(result.data['lines']))

    def test_no_back_sibling_on_disk_is_clean(self) -> None:
        (self.root / 'photos' / 'x-00100.jpg').write_bytes(b'front')
        _write(self.root / 'sources' / 'photos' / 'card_S-3333333333.md', _SOURCE.format(
            sid='S-3333333333', title='Card',
            line='files:\n  - file: photos/x-00100.jpg\n    role: primary\n'))

        result = doctor.run_doctor(self.root, self._fha_config())
        self.assertIsNone(self._finding(result))

    def test_copy_letter_sibling_is_not_mistaken_for_a_missing_back(self) -> None:
        # #113's own bare-letter shape: a second print of the same negative
        # must not itself trip this check - it is a print/copy variant, not
        # evidence of a missing back scan.
        (self.root / 'photos' / 'x-00100.jpg').write_bytes(b'front')
        (self.root / 'photos' / 'x-00100b.jpg').write_bytes(b'second print')
        _write(self.root / 'sources' / 'photos' / 'card_S-4444444444.md', _SOURCE.format(
            sid='S-4444444444', title='Card',
            line='files:\n  - file: photos/x-00100.jpg\n    role: primary\n'))

        result = doctor.run_doctor(self.root, self._fha_config())
        self.assertIsNone(self._finding(result))

    def test_documents_root_entries_are_out_of_scope(self) -> None:
        # A documents-root entry never reaches this check - the file was
        # RENAMED on import, so there is no filename-comparable sibling left
        # to check against on disk (see the function's own docstring for why
        # the document side of #113 is fixed at import time instead).
        (self.root / 'documents' / 'letter-back.txt').write_text('x', encoding='utf-8')
        _write(self.root / 'sources' / 'other' / 'letter_S-5555555555.md', _SOURCE.format(
            sid='S-5555555555', title='Letter',
            line='files:\n  - file: documents/letter_S-5555555555.txt\n    role: primary\n'))

        result = doctor.run_doctor(self.root, self._fha_config())
        self.assertIsNone(self._finding(result))

    def test_copy_lettered_listed_file_does_not_borrow_plain_prints_back(self) -> None:
        # Codex review finding #6 (PR #145): a source that lists a
        # COPY-LETTERED print (x-00100b.jpg, filed via #113's own copy-letter
        # grammar as a SEPARATE physical item from the plain print) must
        # never have its letter folded off by grouping_stem for this
        # comparison - that would match it against the UNLETTERED print's
        # OWN back scan and recommend attaching a back scan that belongs
        # with a different print to the wrong source.
        (self.root / 'photos' / 'x-00100b.jpg').write_bytes(b'second print')
        (self.root / 'photos' / 'x-00100-back.jpg').write_bytes(b'plain prints back')
        _write(self.root / 'sources' / 'photos' / 'copy_S-6666666666.md', _SOURCE.format(
            sid='S-6666666666', title='Copy',
            line='files:\n  - file: photos/x-00100b.jpg\n    role: primary\n'))

        result = doctor.run_doctor(self.root, self._fha_config())
        self.assertIsNone(self._finding(result))

    def test_same_basename_different_folders_checked_independently(self) -> None:
        # Codex review finding #7 (PR #145): two listed photos sharing a
        # basename from DIFFERENT folders must be keyed and checked
        # independently. Keying the candidate map by base_id alone silently
        # keeps only the FIRST folder's copy (dict.setdefault), so a real
        # back scan sitting beside the SECOND folder's copy is never even
        # looked for.
        (self.root / 'photos' / 'album-a').mkdir()
        (self.root / 'photos' / 'album-b').mkdir()
        (self.root / 'photos' / 'album-a' / 'scan001.jpg').write_bytes(b'a')
        (self.root / 'photos' / 'album-b' / 'scan001.jpg').write_bytes(b'b')
        (self.root / 'photos' / 'album-b' / 'scan001-back.jpg').write_bytes(b'b back')
        _write(self.root / 'sources' / 'photos' / 'twofolders_S-7777777777.md', _SOURCE.format(
            sid='S-7777777777', title='Two Folders',
            line=('files:\n'
                  '  - file: photos/album-a/scan001.jpg\n    role: primary\n'
                  '  - file: photos/album-b/scan001.jpg\n    role: primary\n')))

        result = doctor.run_doctor(self.root, self._fha_config())
        report = '\n'.join(result.data['lines'])

        self.assertIn('orphaned back scan', report)
        self.assertIn('photos/album-b/scan001-back.jpg', report)

    def test_repair_command_names_real_primary_and_is_quoted(self) -> None:
        # Codex review finding #4 (PR #145): the suggested repair command
        # must name the source's REAL listed primary path - not the literal
        # placeholder text "<that source's primary file>", which cannot be
        # run as typed - and both file arguments must be quoted with
        # `_lib.shell_quote` so a path containing spaces still runs when
        # copied verbatim.
        (self.root / 'photos' / 'my album').mkdir()
        (self.root / 'photos' / 'my album' / 'scan 001.jpg').write_bytes(b'front')
        (self.root / 'photos' / 'my album' / 'scan 001-back.jpg').write_bytes(b'back')
        _write(self.root / 'sources' / 'photos' / 'spaced_S-8888888888.md', _SOURCE.format(
            sid='S-8888888888', title='Spaced',
            line='files:\n  - file: photos/my album/scan 001.jpg\n    role: primary\n'))

        result = doctor.run_doctor(self.root, self._fha_config())
        report = '\n'.join(result.data['lines'])

        self.assertNotIn("<that source's primary file>", report)
        self.assertIn(doctor.shell_quote('photos/my album/scan 001.jpg'), report)
        self.assertIn(doctor.shell_quote('photos/my album/scan 001-back.jpg'), report)


class RenderTests(unittest.TestCase):
    """_cmd_doctor renders data['lines'] verbatim and returns the exit code."""

    def test_cmd_doctor_renders_lines_and_returns_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / 'fha.yaml', 'roots: {}\n')
            result = doctor.run_doctor(root, {})
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = doctor._cmd_doctor(result)
            self.assertEqual(rc, result.exit_code)
            self.assertIn('archive root:', buf.getvalue())


class CopyMeNextStepsNameTheLauncherTests(unittest.TestCase):
    """Doctor's `next:` commands must run when they are pasted back.

    `fha` is a launcher FILE at the archive root - tools/scaffold.py ships
    `fha` and `fha.cmd` and nothing else - so a bare `fha index --root "…"` is
    a command-not-found on macOS, on Linux, and in PowerShell. Doctor is the
    tool a human reaches for when something is already broken, which makes a
    dead-end next step there worse than anywhere else: he is told what is wrong
    and handed a fix his shell refuses.

    The convention this pins is the one commit 7c6ee13 settled for the browser
    companion's copy card and f1a246d reused for transcribe-audio's attach
    line: a command printed to be copied carries the prefix its shell needs.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _make_archive(self.root)
        # An unreachable mapped root is what makes doctor name ITSELF as the
        # next step; nothing else in a healthy report does.
        _write(self.root / 'fha.yaml',
               f'roots:\n  photos: {self.root / "no-such-folder"}\n')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _report(self) -> str:
        """The full report with every command-bearing check made to fire.

        No `.cache/` exists, so index and photoindex report "not yet built" and
        no backup stamp is recorded. The other two need help: lint is made to
        report errors (a clean lint says "no action needed" and names no
        command), and the staged-captures check only speaks when the browser
        companion's staging folder exists.
        """
        staging = self.root / 'staged'
        staging.mkdir()
        import lint as lint_mod
        orig_lint = lint_mod.run_lint_silent
        orig_bundles = capture.staged_bundles
        lint_mod.run_lint_silent = lambda root, cfg: (2, 1, [])
        capture.staged_bundles = lambda cfg: (staging, [staging / 'bundle-1'])
        try:
            result = doctor.run_doctor(self.root, {})
        finally:
            lint_mod.run_lint_silent = orig_lint
            capture.staged_bundles = orig_bundles
        return '\n'.join(result.data['lines'])

    def test_no_next_step_command_is_spelled_bare(self) -> None:
        import re
        report = self._report()
        for verb in ('index', 'photoindex', 'lint', 'doctor', 'backup',
                     'capture --ingest'):
            with self.subTest(verb=verb):
                # It is there at all (a check that never fired proves nothing).
                self.assertIn(f'{doctor._LAUNCHER} {verb} --root', report)
                # And every occurrence carries the launcher: a bare `fha <verb>`
                # is the bug, wherever in the report it sits.
                bare = re.findall(
                    r'(?<![./\\])\bfha ' + re.escape(verb) + r' --root', report)
                self.assertEqual(bare, [], f'bare `fha {verb}` in the report')

    def test_the_report_explains_the_prefix_once(self) -> None:
        # The reader is a non-technical genealogist: a command starting with
        # `./` is unexplained machinery unless the report says what it is, and
        # someone reading the report on a different shell needs to know what to
        # change. Once, at the top - not on every line.
        report = self._report()
        self.assertEqual(report.count('launcher file in the archive folder'), 1)
        self.assertIn('not a program on your PATH', report)
        self.assertIn('Windows Command Prompt', report)

    def test_the_machine_readable_next_step_matches_what_is_printed(self) -> None:
        # data['checks'] is the headless surface: a consumer that shows
        # `next_step` to a human must not show a command the printed report
        # already knows is unrunnable.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / 'fha.yaml', 'roots: {}\n')
            result = doctor.run_doctor(root, {})
        steps = [c['next_step'] for c in result.data['checks'] if c['next_step']]
        named = [s for s in steps if ' --root ' in s]
        self.assertTrue(named, steps)
        for step in named:
            self.assertTrue(step.startswith(doctor._LAUNCHER), step)


class UndecodableRecordCountsTests(unittest.TestCase):
    """`fha doctor` must survive a record whose bytes are not UTF-8 (#68).

    `_counts_from_scan` reads every source and profile record directly, and it
    runs whenever the index is not fresh - exactly the state a broken archive
    is in. An unguarded read crashed the whole report, and the crash message
    the suite prints on an unhandled error says "Run `fha doctor`": the
    recovery command told the human to run the recovery command.

    These are PRIVACY-bearing counts (restricted sources, living persons), so
    surviving is not enough - a low number a human reads as a total is worse
    than no number. The skipped files are counted and said out loud.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _make_archive(self.root)
        (self.root / 'people' / 'muller__anne_P-3333333333.md').write_bytes(
            ('---\nid: P-3333333333\nname: Anne Müller\nliving: true\n---\n\n'
             '## Biography\n\nBorn in Kraków.\n').encode('cp1252'))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_scan_does_not_crash(self) -> None:
        counts = doctor._counts_from_scan(self.root)
        self.assertEqual(counts['unread'], 1)

    def test_the_records_that_do_decode_are_still_counted(self) -> None:
        counts = doctor._counts_from_scan(self.root)
        self.assertEqual(counts['restricted'], 2)
        self.assertEqual(counts['living'], 1)

    def test_the_report_says_the_counts_are_low(self) -> None:
        result = doctor.run_doctor(self.root, {})
        text = '\n'.join(result.data['lines'])
        self.assertIn('counted low', text)
        self.assertIn('UTF-8', text)
        self.assertEqual(result.data['counts']['unread'], 1)


if __name__ == '__main__':
    unittest.main()
