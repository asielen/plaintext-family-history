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
from _lib import EXIT_ERRORS, EXIT_WARNINGS


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
        self.assertEqual(scan, {'restricted': 2, 'living': 1, 'unknown': 1})


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


if __name__ == '__main__':
    unittest.main()
