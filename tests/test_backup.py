"""
test_backup.py - fha backup: dated zip snapshot + doctor stamp.

The contracts locked here (plan 04, TOOLING §13e):

  - Records-only default: the zip lands in the sibling `{root}-backups/`
    folder and contains the plain-text core (sources/people/places/notes/
    fha.yaml) but nothing rebuildable (.cache/, generated/, out/, .git/), no
    WORKING_COPY marker, and no asset-root files - wherever the asset roots
    live.  `--include-assets` packs each external root under its alias name
    and each internal mapped root under its real relative path, so an unzip
    restores exactly the layout the zipped fha.yaml describes.
  - Destination safety: a destination inside the archive root or inside any
    mapped asset root is refused (exit 3) with a message naming the fix; the
    fha.yaml `backup: path:` key is honored and `--to` beats it.
  - Never overwrite: a same-day second run gets a `_2` suffix and the first
    zip is untouched.
  - Dry-run is byte-for-byte side-effect-free: tree unchanged, destination
    folder not created, stamp not written.
  - Failure posture: a write or verify failure removes the partial zip and
    exits 3 with the "nothing to clean up" message.
  - Working copy: a records-only run succeeds (with the honest note) and
    still stamps; `--include-assets` is refused warning-level (ok=True,
    exit 0, data.status='working-copy').
  - Restore = unzip, literally: an extracted backup lints with zero errors.

Synthetic tmp archives only - the real archive is never a test bed.
"""

import datetime
import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import backup
from _lib import EXIT_CLEAN, EXIT_FAILURE, load_fha_yaml

_PERSON = '''---
id: {pid}
name: {name}
living: false
tier: stub
---

# {name}
'''

_SOURCE = '''---
id: {sid}
title: {title}
source_type: other
---

## Claims
'''


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def _make_archive(parent: Path, name: str = 'my-archive',
                  photos_root: str = 'photos') -> Path:
    """A small synthetic archive inside `parent`, with records, internal or
    external asset roots, and every excluded artifact (.cache, generated, out,
    .git) present so the exclusion contract is actually exercised."""
    root = parent / name
    _write(root / 'fha.yaml',
           f'roots:\n  photos: {photos_root}\n  documents: documents\n')
    _write(root / 'sources' / 'other' / 'letter_S-1111111111.md',
           _SOURCE.format(sid='S-1111111111', title='Old letter'))
    _write(root / 'people' / 'smith__alice_P-aaaaaaaaaa.md',
           _PERSON.format(pid='P-aaaaaaaaaa', name='Alice Smith'))
    _write(root / 'places' / 'places.yaml', '[]\n')
    _write(root / 'notes' / 'log.md', '# Research log\n')
    _write(root / 'inbox' / 'new-scan.txt', 'staged material\n')
    # Rebuildable / machine-local artifacts that must stay out of the zip.
    _write(root / '.cache' / 'index.sqlite', 'not a real db\n')
    _write(root / 'generated' / 'site' / 'index.html', '<html></html>\n')
    _write(root / 'out' / 'old-packet.txt', 'stale export\n')
    _write(root / '.git' / 'config', '[core]\n')
    # Asset files under the configured roots.
    photos = root / photos_root if not Path(photos_root).is_absolute() else Path(photos_root)
    _write(photos / '1920' / 'pic.jpg', 'jpegbytes')
    _write(root / 'documents' / 'letters' / 'scan.txt', 'scanned text\n')
    return root


def _tree_snapshot(base: Path) -> dict:
    """Relative path -> content hash for every file under base (byte-for-byte)."""
    snap = {}
    for p in sorted(base.rglob('*')):
        if p.is_file():
            snap[str(p.relative_to(base))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return snap


def _run(root: Path, **kwargs):
    return backup.run_backup(root, load_fha_yaml(root, strict=True), **kwargs)


def _message_text(result) -> str:
    return '\n'.join(m.text for m in result.messages)


class DefaultRunTests(unittest.TestCase):
    """The zero-flags run: sibling folder, records only, honest notes, stamp."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.parent = Path(self._tmp.name)
        self.root = _make_archive(self.parent)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_zip_lands_in_sibling_folder_with_records_only(self) -> None:
        result = _run(self.root)
        self.assertTrue(result.ok)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'ok')

        today = datetime.date.today().isoformat()
        expected = self.parent / 'my-archive-backups' / f'my-archive-backup_{today}.zip'
        self.assertEqual(Path(result.data['zip_path']), expected)
        self.assertTrue(expected.is_file())

        with zipfile.ZipFile(expected) as zf:
            names = zf.namelist()
        self.assertIn('fha.yaml', names)
        self.assertIn('sources/other/letter_S-1111111111.md', names)
        self.assertIn('people/smith__alice_P-aaaaaaaaaa.md', names)
        self.assertIn('places/places.yaml', names)
        self.assertIn('notes/log.md', names)
        # The inside-the-root inbox is irreplaceable staging - included.
        self.assertIn('inbox/new-scan.txt', names)
        for banned in ('.cache/', 'generated/', 'out/', '.git/', 'photos/', 'documents/'):
            self.assertFalse(any(n.startswith(banned) for n in names),
                             f'{banned} leaked into the backup: {names}')
        self.assertNotIn('WORKING_COPY', names)

        # The assets note names the skipped roots in plain words.
        text = _message_text(result)
        self.assertIn('NOT in this backup', text)
        self.assertIn('--include-assets', text)
        self.assertIn('unzip', text)

        # changed[] lists the zip and the stamp; the stamp carries the facts.
        stamp_path = self.root / '.cache' / 'last_backup.json'
        self.assertEqual(result.changed, [str(expected), str(stamp_path)])
        stamp = json.loads(stamp_path.read_text(encoding='utf-8'))
        self.assertEqual(stamp['zip'], str(expected))
        self.assertEqual(stamp['files'], len(names))
        self.assertEqual(stamp['bytes'], expected.stat().st_size)
        self.assertFalse(stamp['assets_included'])
        datetime.datetime.fromisoformat(stamp['date'])  # parseable timestamp

    def test_include_assets_packs_roots_under_alias_names(self) -> None:
        result = _run(self.root, include_assets=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        with zipfile.ZipFile(result.data['zip_path']) as zf:
            names = zf.namelist()
        self.assertIn('photos/1920/pic.jpg', names)
        self.assertIn('documents/letters/scan.txt', names)
        self.assertTrue(json.loads(
            (self.root / '.cache' / 'last_backup.json').read_text(encoding='utf-8')
        )['assets_included'])

    def test_same_day_second_run_never_overwrites(self) -> None:
        first = Path(_run(self.root).data['zip_path'])
        first_bytes = first.read_bytes()
        second = Path(_run(self.root).data['zip_path'])
        self.assertNotEqual(first, second)
        self.assertTrue(second.name.endswith('_2.zip'))
        self.assertEqual(first.read_bytes(), first_bytes, 'first zip was touched')


class ExternalRootTests(unittest.TestCase):
    """Asset roots mapped outside the archive: excluded by default with the
    note naming the real path; picked up by --include-assets."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.parent = Path(self._tmp.name)
        self.ext_photos = self.parent / 'external-photos'
        self.root = _make_archive(self.parent, photos_root=str(self.ext_photos))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_default_excludes_external_root_and_names_it(self) -> None:
        result = _run(self.root)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        with zipfile.ZipFile(result.data['zip_path']) as zf:
            names = zf.namelist()
        self.assertFalse(any(n.startswith('photos/') for n in names))
        self.assertIn(str(self.ext_photos), _message_text(result))
        skipped = {alias: path for alias, path, _est in result.data['skipped_roots']}
        self.assertEqual(skipped.get('photos'), str(self.ext_photos))

    def test_include_assets_picks_up_external_root(self) -> None:
        result = _run(self.root, include_assets=True)
        with zipfile.ZipFile(result.data['zip_path']) as zf:
            names = zf.namelist()
        self.assertIn('photos/1920/pic.jpg', names)
        # The restored-layout wrinkle is stated in plain words.
        self.assertIn('outside the archive folder', _message_text(result))


class InternalMappedRootTests(unittest.TestCase):
    """A root mapped INSIDE the archive at a non-default path (`roots:
    photos: media/photos`) must keep its real relative path in the zip.
    Re-homing it under the alias made a 'verified' backup whose unzip put
    the photos at photos/ while the restored fha.yaml still said
    media/photos - a layout-corrupting restore with exit 0."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.parent = Path(self._tmp.name)
        self.root = _make_archive(self.parent, photos_root='media/photos')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_internal_root_keeps_real_path_and_restore_is_faithful(self) -> None:
        result = _run(self.root, include_assets=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        with zipfile.ZipFile(result.data['zip_path']) as zf:
            names = zf.namelist()
        self.assertIn('media/photos/1920/pic.jpg', names)
        self.assertFalse(any(n.startswith('photos/') for n in names),
                         f'internal root was re-homed under its alias: {names}')
        # The external-root restore note must NOT print: the layout in the
        # zip already matches what the zipped fha.yaml describes.
        self.assertNotIn('outside the archive folder', _message_text(result))
        # Restore = unzip, literally: the mapped root resolves after unzip.
        restored = self.parent / 'restored'
        with zipfile.ZipFile(result.data['zip_path']) as zf:
            zf.extractall(restored)
        cfg = load_fha_yaml(restored, strict=True)
        self.assertEqual(cfg['roots']['photos'], 'media/photos')
        self.assertTrue((restored / 'media' / 'photos' / '1920' / 'pic.jpg').is_file())


class ArcnameCollisionTests(unittest.TestCase):
    """An archive-internal top-level folder named like an external root's
    alias would put two files at the same name inside the zip; extraction
    silently keeps one, so the run must refuse (exit 3) before writing
    anything - a backup tool never guesses which copy the human meant."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.parent = Path(self._tmp.name)
        self.ext_photos = self.parent / 'external-photos'
        self.root = _make_archive(self.parent, photos_root=str(self.ext_photos))
        # An ordinary in-archive folder that happens to share the alias name
        # AND a relative file path with the external photos root.
        _write(self.root / 'photos' / '1920' / 'pic.jpg', 'a different picture')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_alias_collision_refuses_before_writing(self) -> None:
        result = _run(self.root, include_assets=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'name-collision')
        text = _message_text(result)
        self.assertIn("'photos/'", text)                 # the colliding folder
        self.assertIn('roots: photos:', text)            # the fha.yaml line
        self.assertIn('rename', text.lower())            # the fix
        self.assertIn('photos/1920/pic.jpg', text)       # an example collision
        # Nothing was written: no destination folder, no zip, no stamp.
        self.assertFalse((self.parent / 'my-archive-backups').exists())
        self.assertFalse((self.root / '.cache' / 'last_backup.json').exists())

    def test_records_only_run_with_lookalike_folder_still_works(self) -> None:
        # Without --include-assets there is no alias packing, so the
        # in-archive photos/ folder is just an ordinary records folder.
        result = _run(self.root)
        self.assertTrue(result.ok)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        with zipfile.ZipFile(result.data['zip_path']) as zf:
            self.assertIn('photos/1920/pic.jpg', zf.namelist())


class DestinationGuardTests(unittest.TestCase):
    """No destination inside the tree is possible; config key and --to obey
    their precedence."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.parent = Path(self._tmp.name)
        self.ext_photos = self.parent / 'external-photos'
        self.root = _make_archive(self.parent, photos_root=str(self.ext_photos))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_to_inside_archive_root_is_refused(self) -> None:
        result = _run(self.root, to=str(self.root / 'backups'))
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'bad-destination')
        text = _message_text(result)
        self.assertIn('inside your archive', text)
        self.assertIn('--to', text)                      # names the fix
        self.assertFalse((self.root / 'backups').exists())

    def test_to_inside_asset_root_is_refused(self) -> None:
        result = _run(self.root, to=str(self.ext_photos / 'backups'))
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertIn('photos root', _message_text(result))

    def test_backup_path_key_honored_and_to_beats_it(self) -> None:
        configured = self.parent / 'configured-backups'
        (self.root / 'fha.yaml').write_text(
            f'roots:\n  photos: {self.ext_photos}\n  documents: documents\n'
            f'backup:\n  path: {configured}\n',
            encoding='utf-8',
        )
        result = _run(self.root)
        self.assertEqual(Path(result.data['zip_path']).parent, configured)

        flagged = self.parent / 'flag-backups'
        result = _run(self.root, to=str(flagged))
        self.assertEqual(Path(result.data['zip_path']).parent, flagged)

    def test_unrecognized_backup_key_is_refused_not_ignored(self) -> None:
        (self.root / 'fha.yaml').write_text(
            'roots: {}\nbackup: [what, is, this]\n', encoding='utf-8')
        result = _run(self.root)
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertIn('backup: setting', _message_text(result))

    def test_inside_handles_filesystem_root_parent(self) -> None:
        # An asset root at a drive/filesystem root must still contain its
        # children ('d:\' + os.sep would be a double separator otherwise).
        anchor = Path(self.root.anchor)
        self.assertTrue(backup._inside(self.root, anchor))
        self.assertTrue(backup._inside(anchor, anchor))
        self.assertFalse(backup._inside(anchor, self.root))


class DryRunTests(unittest.TestCase):
    """Byte-for-byte side-effect-free, including the destination folder."""

    def test_dry_run_writes_nothing_anywhere(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = _make_archive(parent)
            before = _tree_snapshot(parent)
            result = _run(root, dry_run=True)
            self.assertEqual(result.exit_code, EXIT_CLEAN)
            self.assertEqual(result.data['status'], 'dry-run')
            self.assertEqual(result.changed, [])
            self.assertEqual(_tree_snapshot(parent), before)
            self.assertFalse((parent / 'my-archive-backups').exists())
            self.assertFalse((root / '.cache' / 'last_backup.json').exists())
            # The plan names the destination and the exclusions with reasons.
            text = _message_text(result)
            self.assertIn('DRY RUN', text)
            self.assertIn('my-archive-backup_', text)
            self.assertIn('.cache/', text)
            self.assertIn('rebuildable', text)


class FailureInjectionTests(unittest.TestCase):
    """A failed write or a failed verify removes the partial zip, exits 3."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.parent = Path(self._tmp.name)
        self.root = _make_archive(self.parent)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_write_failure_removes_partial_zip(self) -> None:
        def _boom(zip_path, entries):
            zip_path.write_bytes(b'partial garbage')
            raise OSError('disk full')

        orig = backup._write_zip
        backup._write_zip = _boom
        try:
            result = _run(self.root)
        finally:
            backup._write_zip = orig

        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'write-failed')
        text = _message_text(result)
        self.assertIn('partial file was removed', text)
        self.assertIn('Nothing to clean up', text)
        self.assertFalse(Path(result.data['zip_path']).exists())
        self.assertFalse((self.root / '.cache' / 'last_backup.json').exists())

    def test_verify_failure_removes_zip(self) -> None:
        orig = backup._verify_zip
        backup._verify_zip = lambda zip_path: 'a member failed its integrity check'
        try:
            result = _run(self.root)
        finally:
            backup._verify_zip = orig

        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertFalse(Path(result.data['zip_path']).exists())
        self.assertIn('integrity check', _message_text(result))


class WorkingCopyTests(unittest.TestCase):
    """Records-only runs work on a working copy (with the honest note);
    --include-assets is refused warning-level."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.parent = Path(self._tmp.name)
        self.root = _make_archive(self.parent)
        (self.root / 'WORKING_COPY').write_text('working copy marker\n', encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_records_only_run_succeeds_with_note_and_stamp(self) -> None:
        result = _run(self.root)
        self.assertTrue(result.ok)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'ok')
        self.assertIn('working copy', _message_text(result))
        with zipfile.ZipFile(result.data['zip_path']) as zf:
            self.assertNotIn('WORKING_COPY', zf.namelist())
        self.assertTrue((self.root / '.cache' / 'last_backup.json').is_file())

    def test_include_assets_is_refused_warning_level(self) -> None:
        result = _run(self.root, include_assets=True)
        self.assertTrue(result.ok)                       # not a failure
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'working-copy')
        self.assertIn('main archive', _message_text(result))
        self.assertFalse((self.parent / 'my-archive-backups').exists())


class RestoreSmokeTests(unittest.TestCase):
    """Restore = unzip, literally: the extracted tree is a working archive."""

    def test_extracted_backup_lints_clean(self) -> None:
        from lint import run_lint_silent
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = _make_archive(parent)
            result = _run(root)
            restored = parent / 'restored'
            with zipfile.ZipFile(result.data['zip_path']) as zf:
                zf.extractall(restored)
            self.assertTrue((restored / 'fha.yaml').is_file())
            n_errors, _n_warnings, _e018 = run_lint_silent(
                restored, load_fha_yaml(restored, strict=True))
            self.assertEqual(n_errors, 0, 'restored archive has lint errors')


if __name__ == '__main__':
    unittest.main()
