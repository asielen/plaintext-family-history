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
import sys
import tempfile
import unittest
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

    def test_stamp_absent_names_the_command_and_stays_clean(self) -> None:
        result = doctor.run_doctor(self.root, {})
        check = self._backup_check(result)
        self.assertEqual(check['status'], 'info')
        self.assertIn('fha backup', check['next_step'])
        report = '\n'.join(result.data['lines'])
        self.assertIn('none recorded', report)
        self.assertIn('restore = unzip', report)
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


if __name__ == '__main__':
    unittest.main()
