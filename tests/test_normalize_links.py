"""Tests for `fha normalize-links` - the `--dry-run` flag contract.

Dry-run is this tool's DEFAULT, but the operating instructions (AGENTS.md,
TOOLING §17) tell agents to always pass `--dry-run` before any mutating
operation - so the flag must parse as an explicit no-op instead of dying with
an argparse usage error, and the contradictory `--dry-run --write` pair must
be refused with a plain message (exit 2), not silently resolved either way.

Run: python -m unittest tests.test_normalize_links -v   (from the repo root)
"""

import argparse
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import normalize_links
from _lib import EXIT_CLEAN, EXIT_ERRORS, EXIT_WARNINGS

SOURCE_ID = 'S-fa00000001'


def _make_archive(tmp: Path) -> Path:
    """A minimal archive with one source record whose body carries a legacy
    single-bracket cite - the smallest input normalize-links would rewrite."""
    archive = tmp / 'archive'
    (archive / 'sources' / 'other').mkdir(parents=True)
    (archive / 'fha.yaml').write_text(
        'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8')
    (archive / 'sources' / 'other' / f'family-album_{SOURCE_ID}.md').write_text(
        '---\n'
        f'id: {SOURCE_ID}\n'
        'title: Family album\n'
        '---\n'
        '\n'
        f'The portrait is discussed in [{SOURCE_ID}] alongside the letters.\n',
        encoding='utf-8')
    return archive


class NormalizeLinksDryRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive = _make_archive(Path(self._tmp.name))
        self.record = self.archive / 'sources' / 'other' / f'family-album_{SOURCE_ID}.md'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _args(self, **kw) -> argparse.Namespace:
        base = {'root': str(self.archive), 'write': False, 'dry_run': False, 'quiet': True}
        base.update(kw)
        return argparse.Namespace(**base)

    # ── the flag parses, on both parsers ─────────────────────────────────────

    def test_dry_run_flag_parses_on_registered_subparser(self) -> None:
        # The original bug: register() never defined --dry-run, so the flag
        # AGENTS.md tells agents to always pass died with a usage error.
        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers()
        normalize_links.register(subs)
        args = parser.parse_args(['normalize-links', '--dry-run', '--root', str(self.archive)])
        self.assertTrue(args.dry_run)
        self.assertFalse(args.write)

    def test_dry_run_flag_parses_on_standalone_parser(self) -> None:
        before = self.record.read_text(encoding='utf-8')
        rc = normalize_links._standalone_main(['--dry-run', '--root', str(self.archive)])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)

    # ── behavior ─────────────────────────────────────────────────────────────

    def test_dry_run_writes_nothing(self) -> None:
        before = self.record.read_text(encoding='utf-8')
        rc = normalize_links._cmd_normalize_links(self._args(dry_run=True))
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)

    def test_dry_run_with_write_refused_plainly(self) -> None:
        before = self.record.read_text(encoding='utf-8')
        err = io.StringIO()
        with mock.patch('sys.stderr', err):
            rc = normalize_links._cmd_normalize_links(self._args(dry_run=True, write=True))
        self.assertEqual(rc, EXIT_ERRORS)   # exit 2
        self.assertEqual(self.record.read_text(encoding='utf-8'), before)
        message = err.getvalue()
        self.assertIn('--dry-run', message)
        self.assertIn('--write', message)
        self.assertIn('pick one', message)          # names the fix
        self.assertNotIn('Traceback', message)

    def test_write_still_applies(self) -> None:
        rc = normalize_links._cmd_normalize_links(self._args(write=True))
        self.assertEqual(rc, EXIT_CLEAN)
        text = self.record.read_text(encoding='utf-8')
        self.assertIn(f'[[{SOURCE_ID}]]', text)     # legacy cite upgraded
        self.assertNotIn(f' [{SOURCE_ID}] ', text)

    def test_generated_file_with_leading_blank_line_untouched(self) -> None:
        # Round-2 finding 12: the GENERATED skip used to check byte 0, so a
        # generated companion that begins with a blank line was treated as a
        # hand-written record and rewritten in place. Ownership is now judged
        # by the first NON-BLANK line (_lib.is_generated_file), matching lint
        # and views.
        gen = self.archive / 'people' / 'x_timeline_P-fa00000002.md'
        gen.parent.mkdir(parents=True, exist_ok=True)
        original = (
            '\n<!-- GENERATED by fha views timeline on 2026-01-01'
            ' - do not edit; regenerate instead -->\n\n'
            f'A legacy cite [{SOURCE_ID}] inside a generated view.\n'
        )
        gen.write_text(original, encoding='utf-8')
        rc = normalize_links._cmd_normalize_links(self._args(write=True))
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(gen.read_text(encoding='utf-8'), original)   # skipped, byte-for-byte

    # ── end to end through the real fha dispatcher ───────────────────────────

    def test_fha_cli_accepts_dry_run_and_refuses_the_pair(self) -> None:
        fha = str(ROOT / 'tools' / 'fha.py')
        ok = subprocess.run(
            [sys.executable, fha, 'normalize-links', '--dry-run', '--root', str(self.archive)],
            text=True, capture_output=True, check=False)
        self.assertEqual(ok.returncode, 0, ok.stderr + ok.stdout)

        both = subprocess.run(
            [sys.executable, fha, 'normalize-links', '--dry-run', '--write',
             '--root', str(self.archive)],
            text=True, capture_output=True, check=False)
        self.assertEqual(both.returncode, 2, both.stderr + both.stdout)
        self.assertIn('pick one', both.stderr)


class NormalizeLinksUndecodableTests(unittest.TestCase):
    """#68: `_scan_records` (the alias/name resolve-map builder) walks every
    people/ and sources/ record. A file saved in another encoding (cp1252, a
    Windows editor's default) must not crash the whole `fha normalize-links`
    run - both loops (people/, sources/) share this one test class because
    they are the same shape: skip the bad file, keep scanning, report once
    via the Result the engine returns."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive = _make_archive(Path(self._tmp.name))
        self.record = self.archive / 'sources' / 'other' / f'family-album_{SOURCE_ID}.md'
        (self.archive / 'people').mkdir(parents=True)
        # A readable person, so the fix can be checked NOT to lose good data
        # alongside the bad file.
        (self.archive / 'people' / 'smith__ken_P-1111111111.md').write_text(
            '---\nid: P-1111111111\nname: Ken Smith\nliving: false\n---\n',
            encoding='utf-8')
        # One undecodable file in EACH loop.
        (self.archive / 'people' / 'muller__anne_P-2222222222.md').write_bytes(
            ('---\nid: P-2222222222\nname: Anne Müller\nliving: false\n---\n'
             ).encode('cp1252'))
        (self.archive / 'sources' / 'other' / 'krakow_S-2222222222.md').write_bytes((
            '---\nid: S-2222222222\ntitle: Kraków deed\n---\n\nBorn in Kraków.\n'
        ).encode('cp1252'))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_scan_records_does_not_crash(self) -> None:
        # Pre-fix, `_scan_records` calls `read_record(path)` with no
        # `on_decode_error` in either loop, so a bad decode raises straight
        # out of the walk (read_record's default) and the whole run crashes.
        records = normalize_links._scan_records(
            self.archive,
            on_decode_error=lambda p: None,
        )
        ids = {r['id'] for r in records}
        self.assertIn('p-1111111111', ids)   # the readable person still scanned
        self.assertIn(SOURCE_ID.lower(), ids)  # the readable source still scanned
        self.assertNotIn('p-2222222222', ids)  # the bad person was skipped
        self.assertNotIn('s-2222222222', ids)  # the bad source was skipped

    def test_run_normalize_links_does_not_crash_and_still_rewrites(self) -> None:
        result = normalize_links.run_normalize_links(self.archive, {}, write=True)
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        text = self.record.read_text(encoding='utf-8')
        self.assertIn(f'[[{SOURCE_ID}]]', text)   # the good record still normalized

    def test_result_names_both_skipped_files(self) -> None:
        result = normalize_links.run_normalize_links(self.archive, {})
        warnings = ' '.join(m.text for m in result.messages if m.level == 'warning')
        self.assertNotIn('Traceback', warnings)
        self.assertIn('2 file(s)', warnings)
        self.assertIn('muller__anne_P-2222222222.md', warnings)
        self.assertIn('krakow_S-2222222222.md', warnings)
        self.assertIn('not saved as UTF-8', warnings)


if __name__ == '__main__':
    unittest.main()
