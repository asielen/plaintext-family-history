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
  - A folder that will not open: no zip at all (exit 3, status
    'unreadable-folders', nothing written, the folder named and
    --allow-incomplete offered), and with that flag a zip whose NAME and whose
    BACKUP_INCOMPLETE.txt member both say what is missing, plus a stamp
    doctor can read it from.
  - Path identity: which of two names is one folder is asked of the
    filesystem, not of a string, so a destination that a case-insensitive
    volume would put inside the archive is refused rather than written there.
  - Restore = unzip, literally: an extracted backup lints with zero errors.

Synthetic tmp archives only - the real archive is never a test bed.
"""

import datetime
import hashlib
import json
import os
import sys
import tempfile
import unicodedata
import unittest
import unittest.mock
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
        def _boom(zip_path, entries, notice=None):
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

    def test_keyboard_interrupt_removes_partial_zip(self) -> None:
        """The docstring promise - an interrupted run leaves nothing behind -
        must hold for BaseException too: a Ctrl-C mid-write used to skip the
        typed cleanup arms and leave a partial zip on disk."""
        def _interrupt(zip_path, entries, notice=None):
            zip_path.write_bytes(b'partial garbage')
            raise KeyboardInterrupt

        orig = backup._write_zip
        backup._write_zip = _interrupt
        try:
            with self.assertRaises(KeyboardInterrupt):
                _run(self.root)
        finally:
            backup._write_zip = orig

        dest = self.parent / 'my-archive-backups'
        leftovers = sorted(dest.glob('*.zip')) if dest.is_dir() else []
        self.assertEqual(leftovers, [], 'a partial zip survived the interrupt')
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


class ResultDataContractTests(unittest.TestCase):
    """run_backup's docstring promises the SAME data keys on every status;
    a headless consumer reading data['folders'] or data['excluded'] must
    never hit a KeyError on the early arms (working-copy, bad-destination,
    name-collision, write-failed)."""

    _KEYS = {'status', 'zip_path', 'files', 'bytes', 'assets_included',
             'skipped_roots', 'folders', 'excluded', 'unreadable_dirs',
             'complete'}

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.parent = Path(self._tmp.name)
        self.root = _make_archive(self.parent)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _assert_documented_keys(self, result, status: str) -> None:
        self.assertEqual(result.data['status'], status)
        missing = self._KEYS - set(result.data)
        self.assertFalse(missing, f'{status} arm omits documented keys: {missing}')

    def test_working_copy_arm_carries_all_documented_keys(self) -> None:
        (self.root / 'WORKING_COPY').write_text('marker\n', encoding='utf-8')
        result = _run(self.root, include_assets=True)
        self._assert_documented_keys(result, 'working-copy')

    def test_bad_destination_arm_carries_all_documented_keys(self) -> None:
        result = _run(self.root, to=str(self.root / 'backups'))
        self._assert_documented_keys(result, 'bad-destination')

    def test_write_failed_arm_carries_all_documented_keys(self) -> None:
        def _boom(zip_path, entries, notice=None):
            raise OSError('disk full')

        orig = backup._write_zip
        backup._write_zip = _boom
        try:
            result = _run(self.root)
        finally:
            backup._write_zip = orig
        self._assert_documented_keys(result, 'write-failed')

    def test_name_collision_arm_carries_all_documented_keys(self) -> None:
        # Remap photos to an external root that shares a relative file path
        # with the archive's own (now ordinary) photos/ folder.
        ext = self.parent / 'ext-photos'
        _write(ext / '1920' / 'pic.jpg', 'external copy')
        (self.root / 'fha.yaml').write_text(
            f'roots:\n  photos: {ext}\n  documents: documents\n', encoding='utf-8')
        result = _run(self.root, include_assets=True)
        self._assert_documented_keys(result, 'name-collision')

    def test_unreadable_folders_arm_carries_all_documented_keys(self) -> None:
        with unittest.mock.patch(
                'os.scandir',
                new=_scandir_denying(self.root / 'sources' / 'other')):
            result = _run(self.root)
        self._assert_documented_keys(result, 'unreadable-folders')

    def test_ok_and_dry_run_arms_carry_all_documented_keys(self) -> None:
        self._assert_documented_keys(_run(self.root, dry_run=True), 'dry-run')
        self._assert_documented_keys(_run(self.root), 'ok')


def _scandir_denying(unreadable: Path):
    """An os.scandir stand-in that refuses to list `unreadable`.

    `os.walk` and pathlib's `rglob` both reach the filesystem through
    `os.scandir`, so the failure goes in there: os.walk reports it to its
    `onerror` callback, rglob swallows it and calls the folder empty. chmod
    cannot produce this - CI runs as root, and Windows has no equivalent.
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


class UnreadableFolderTests(unittest.TestCase):
    """A backup that could not read a folder must not be written at all.

    The zip outlives the terminal session: the human keeps it for years
    believing it is his archive, and finds out otherwise on the one day
    recovery is impossible. So the plan itself is the failure, exactly like a
    duplicate in-zip name."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.parent = Path(self._tmp.name)
        self.root = _make_archive(self.parent)
        self.shut = self.root / 'sources' / 'other'
        self.dest = self.parent / 'my-archive-backups'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_no_zip_is_written_and_the_folder_is_named(self) -> None:
        with unittest.mock.patch('os.scandir', new=_scandir_denying(self.shut)):
            result = _run(self.root)
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'unreadable-folders')
        self.assertEqual(result.data['unreadable_dirs'], ['sources/other'])
        self.assertFalse(result.data['complete'])
        text = _message_text(result)
        self.assertIn('no backup was written', text)
        self.assertIn('sources/other', text)
        self.assertIn('--allow-incomplete', text)
        # Nothing on disk, not even an empty destination folder.
        self.assertFalse(self.dest.exists())
        self.assertFalse((self.root / '.cache' / 'last_backup.json').is_file())

    def test_dry_run_previews_the_same_refusal(self) -> None:
        # A preview that promises a backup the real run would refuse is not a
        # preview of that run.
        with unittest.mock.patch('os.scandir', new=_scandir_denying(self.shut)):
            result = _run(self.root, dry_run=True)
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'unreadable-folders')
        self.assertFalse(self.dest.exists())

    def test_allow_incomplete_marks_the_zip_in_its_name_and_inside_it(self) -> None:
        with unittest.mock.patch('os.scandir', new=_scandir_denying(self.shut)):
            result = _run(self.root, allow_incomplete=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        zip_path = Path(result.data['zip_path'])
        self.assertIn('-INCOMPLETE', zip_path.name)
        self.assertFalse(result.data['complete'])
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            self.assertIn('BACKUP_INCOMPLETE.txt', names)
            notice = zf.read('BACKUP_INCOMPLETE.txt').decode('utf-8')
            self.assertEqual(zf.testzip(), None)
        self.assertIn('THIS BACKUP IS INCOMPLETE', notice)
        self.assertIn('sources/other', notice)
        # The zip really is short of the archive - the notice is not decoration.
        self.assertNotIn('sources/other/letter_S-1111111111.md', names)
        # And doctor can say so months later.
        stamp = json.loads(
            (self.root / '.cache' / 'last_backup.json').read_text(encoding='utf-8'))
        self.assertIs(stamp['complete'], False)
        self.assertEqual(stamp['unreadable_dirs'], ['sources/other'])

    def test_an_unreadable_folder_in_a_SKIPPED_asset_root_does_not_refuse(self) -> None:
        # The records-only run only ESTIMATES the size of the photo root it is
        # not packing. A folder it could not read there makes one printed
        # number low; refusing the records backup over that would be the
        # opposite of the rule.
        with unittest.mock.patch(
                'os.scandir', new=_scandir_denying(self.root / 'photos' / '1920')):
            result = _run(self.root)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'ok')
        self.assertTrue(result.data['complete'])

    def test_included_asset_root_is_covered_by_the_refusal(self) -> None:
        with unittest.mock.patch(
                'os.scandir', new=_scandir_denying(self.root / 'photos' / '1920')):
            result = _run(self.root, include_assets=True)
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'unreadable-folders')
        self.assertEqual(result.data['unreadable_dirs'], ['photos/1920'])


class PathIdentityTests(unittest.TestCase):
    """Which of two names is one folder is a question for the filesystem.

    `os.path.normcase` folds case on Windows only, so on macOS - where a
    case-insensitive volume is the default - a destination spelled
    `.../Archive` compared unequal to a root spelled `.../archive` and the
    backup was written INSIDE the tree it was backing up, to be swept into the
    next backup and lost with the same disk.

    What this container can and cannot prove: it is a case-sensitive
    filesystem, so the samefile arm cannot be shown doing something the old
    string comparison did not (with no case folding and no second mount, the
    two agree here). The folded arm is exercised directly, and `_file_id` is
    checked on aliases that DO exist here - a symlink and a relative spelling.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.parent = Path(self._tmp.name)
        self.root = _make_archive(self.parent)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_destination_differing_only_in_capitals_is_refused(self) -> None:
        # On this Linux volume those are two folders; on the Mac or Windows PC
        # the same archive will be opened on, they are one. Refusing costs a
        # sentence, writing costs the backup.
        dest = self.parent / 'MY-ARCHIVE' / 'backups'
        result = _run(self.root, to=str(dest))
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'bad-destination')
        text = _message_text(result)
        self.assertIn('capital letters or accents', text)
        self.assertIn('--to', text)
        self.assertFalse(dest.exists())

    def test_destination_differing_only_in_unicode_form_is_refused(self) -> None:
        root = _make_archive(self.parent, name=unicodedata.normalize('NFC', 'Ärchiv'))
        nfd = unicodedata.normalize('NFD', 'Ärchiv')
        result = _run(root, to=str(self.parent / nfd / 'backups'))
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertIn('capital letters or accents', _message_text(result))

    def test_a_plainly_different_destination_is_still_allowed(self) -> None:
        # The blunt arm must not swallow ordinary destinations.
        result = _run(self.root, to=str(self.parent / 'elsewhere'))
        self.assertEqual(result.exit_code, EXIT_CLEAN)

    def test_destination_reached_through_a_symlink_is_refused(self) -> None:
        # Guard, not proof: resolve() already followed the link before this
        # change. It is here so the rewrite of _inside cannot lose it.
        link = self.parent / 'archive-link'
        try:
            link.symlink_to(self.root, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('this platform cannot create symlinks')
        result = _run(self.root, to=str(link / 'backups'))
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertIn('inside', _message_text(result))

    def test_fold_flattens_case_and_unicode_form(self) -> None:
        self.assertEqual(backup._fold('Archive'), backup._fold('archive'))
        self.assertEqual(
            backup._fold(unicodedata.normalize('NFC', 'Ärchiv')),
            backup._fold(unicodedata.normalize('NFD', 'Ärchiv')))
        self.assertNotEqual(backup._fold('archive'), backup._fold('archives'))

    def test_file_id_is_one_key_for_two_names_of_one_folder(self) -> None:
        link = self.parent / 'photos-link'
        try:
            link.symlink_to(self.root / 'photos', target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('this platform cannot create symlinks')
        self.assertEqual(backup._file_id(link), backup._file_id(self.root / 'photos'))
        self.assertNotEqual(
            backup._file_id(self.root / 'photos'),
            backup._file_id(self.root / 'documents'))

    def test_an_internal_mapped_root_is_pruned_from_the_records_walk(self) -> None:
        # The pruning that _file_id now decides: a records-only backup carries
        # no photo files, and a photos root mapped inside the archive must not
        # sneak in through the records walk.
        root = _make_archive(self.parent, name='mapped', photos_root='media/photos')
        result = _run(root)
        with zipfile.ZipFile(result.data['zip_path']) as zf:
            names = zf.namelist()
        self.assertFalse([n for n in names if n.startswith('media/photos/')], names)


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
