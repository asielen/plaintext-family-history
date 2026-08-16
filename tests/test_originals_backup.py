"""Tests for `originals_backup:` - the safety copy taken before fha writes
embedded metadata into an original photo (TOOLING §13f).

The four writes that reach an original photo file all go through one helper,
`_lib.OriginalBackup`, so these tests exercise it from both sides: through
`fha process` (the SOURCE: keyword, the FIRST write a photo ever gets) and
through `fha photoindex`'s two writers (tag-person's P-id keyword,
set-summary's UserComment).

exiftool is never invoked. `subprocess.run` is replaced by a fake that appends
a marker to the file, so a test can prove the safety copy holds the bytes as
they were BEFORE the write, not after. Copy failures are injected at
`_lib.shutil.copy2` rather than with `chmod`: CI runs as root, where a
read-only folder is still writable.

Run: python -m unittest tests.test_originals_backup -v   (from the repo root)
"""

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import _lib
import photoindex
import process
from _lib import EXIT_CLEAN, EXIT_FAILURE, BackupRefused, OriginalBackup

PHOTO_BYTES = b'\xff\xd8\xff\xe0-pristine-original-bytes'
WRITE_MARKER = b'-EXIFTOOL-WROTE-HERE'


class FakeExiftool:
    """Stand-in for `subprocess.run` that answers exiftool's two call shapes.

    A read (`-j …`) returns one empty JSON row - no keywords, no comment. A
    write appends `WRITE_MARKER` to the file, which is what makes "the copy was
    taken before the write" a testable claim rather than an assertion about
    call order: the copy either holds the pristine bytes or it does not.
    """

    def __init__(self, fail_paths: set[str] | None = None) -> None:
        self.writes: list[Path] = []
        self.reads: list[Path] = []
        self.fail_paths = fail_paths or set()

    def __call__(self, cmd, **kwargs):
        path = Path(cmd[-1])
        if '-j' in cmd:
            self.reads.append(path)
            return SimpleNamespace(returncode=0, stdout=json.dumps([{}]), stderr='')
        self.writes.append(path)
        if path.name in self.fail_paths:
            return SimpleNamespace(returncode=1, stdout='', stderr='exiftool: write failed')
        with open(path, 'ab') as fh:
            fh.write(WRITE_MARKER)
        return SimpleNamespace(returncode=0, stdout='', stderr='')


def _make_archive(tmp: Path, *, originals_backup: str | None = None) -> Path:
    """A minimal archive with one photo, optionally configured for safety copies."""
    archive = tmp / 'archive'
    (archive / 'photos' / '1912').mkdir(parents=True)
    (archive / 'documents').mkdir()
    (archive / 'sources').mkdir()
    (archive / 'people').mkdir()
    cfg = 'roots:\n  photos: photos\n  documents: documents\n'
    if originals_backup is not None:
        cfg += f'originals_backup: {originals_backup}\n'
    (archive / 'fha.yaml').write_text(cfg, encoding='utf-8')
    return archive


def _add_photo(archive: Path, name: str = 'margaret.jpg') -> Path:
    photo = archive / 'photos' / '1912' / name
    photo.write_bytes(PHOTO_BYTES)
    return photo


def _config(archive: Path) -> dict:
    return _lib.load_fha_yaml(archive)


class BackupHelperTests(unittest.TestCase):
    """The one rule, read directly: `_lib.OriginalBackup`."""

    def test_setting_resolves_absolute_and_relative_like_roots(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            archive = _make_archive(tmp)
            outside = (tmp / 'safety').resolve()
            self.assertEqual(
                _lib.originals_backup_dir({'originals_backup': str(outside)}, archive),
                outside,
            )
            # A relative value joins the archive root, exactly as `roots:` values do.
            self.assertEqual(
                _lib.originals_backup_dir({'originals_backup': '../safety'}, archive),
                outside,
            )
            self.assertIsNone(_lib.originals_backup_dir({}, archive))

    def test_malformed_setting_is_an_error_not_an_absent_setting(self) -> None:
        """Fail closed. Reading a broken value as "off" would silently drop
        protection the human asked for, at the moment it matters."""
        with tempfile.TemporaryDirectory() as d:
            archive = _make_archive(Path(d))
            for bad in ([], {'path': 'x'}, '', '   '):
                with self.subTest(value=bad):
                    with self.assertRaises(RuntimeError) as ctx:
                        _lib.originals_backup_dir({'originals_backup': bad}, archive)
                    self.assertIn('originals_backup', str(ctx.exception))
                    self.assertIn('D:/PhotoOriginals', str(ctx.exception))

    def test_malformed_setting_refuses_every_write(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _make_archive(Path(d))
            photo = _add_photo(archive)
            backup = OriginalBackup(archive, {'roots': {'photos': 'photos'},
                                              'originals_backup': []})
            with self.assertRaises(BackupRefused) as ctx:
                backup.ensure(photo)
            self.assertIn('originals_backup', str(ctx.exception))

    def test_destination_inside_the_photos_root_is_refused(self) -> None:
        """A copy filed inside the photo library would be scanned, keyworded
        and counted as a second photo of the same picture."""
        with tempfile.TemporaryDirectory() as d:
            archive = _make_archive(Path(d), originals_backup='photos/_safety')
            photo = _add_photo(archive)
            backup = OriginalBackup(archive, _config(archive))
            self.assertIsNotNone(backup.refusal)
            with self.assertRaises(BackupRefused) as ctx:
                backup.ensure(photo)
            text = str(ctx.exception)
            self.assertIn('photos/1912/margaret.jpg', text)
            self.assertIn('your photos root', text)
            self.assertIn('originals_backup', text)
            self.assertIn('Nothing was written', text)

    def test_destination_inside_the_archive_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive = _make_archive(Path(d), originals_backup='safety-copies')
            backup = OriginalBackup(archive, _config(archive))
            self.assertIsNotNone(backup.refusal)
            self.assertIn('your archive', backup.refusal)

    def test_destination_containing_an_asset_root_is_refused(self) -> None:
        """The mistake from the other direction: the live library ends up
        filed inside the safety copies."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            archive = tmp / 'archive'
            (archive / 'sources').mkdir(parents=True)
            (tmp / 'media' / 'photos').mkdir(parents=True)
            (archive / 'fha.yaml').write_text(
                f'roots:\n  photos: {tmp / "media" / "photos"}\n'
                f'originals_backup: {tmp / "media"}\n', encoding='utf-8')
            backup = OriginalBackup(archive, _config(archive))
            self.assertIsNotNone(backup.refusal)
            self.assertIn('your photos root', backup.refusal)

    def test_unconfigured_warns_once_per_run_not_once_per_photo(self) -> None:
        """The motivating library holds 88,000 photos; a per-photo warning is
        not a warning, it is a wall of text."""
        with tempfile.TemporaryDirectory() as d:
            archive = _make_archive(Path(d))
            photos = [_add_photo(archive, f'p{i}.jpg') for i in range(5)]
            backup = OriginalBackup(archive, _config(archive))
            for photo in photos:
                backup.ensure(photo)          # never refuses: the setting is opt-in
            warnings = [m for m in backup.drain_messages() if m[0] == 'warning']
            self.assertEqual(len(warnings), 1)
            self.assertIn('originals_backup', warnings[0][1])
            self.assertIn('no copy to fall back on', warnings[0][1])
            # Drained: a second pass says nothing more.
            self.assertEqual(
                [m for m in backup.drain_messages() if m[0] == 'warning'], [])

    def test_report_counts_copies_and_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            archive = _make_archive(tmp, originals_backup=str(tmp / 'safety'))
            a, b = _add_photo(archive, 'a.jpg'), _add_photo(archive, 'b.jpg')
            backup = OriginalBackup(archive, _config(archive))
            backup.ensure(a)
            backup.ensure(b)
            backup.ensure(a)          # already copied
            self.assertEqual((backup.copied, backup.already), (2, 1))
            self.assertEqual(backup.bytes, 2 * len(PHOTO_BYTES))
            text = ' '.join(t for _lvl, t in backup.drain_messages())
            self.assertIn('2 original(s) copied', text)
            self.assertIn('1 already had a copy from an earlier run', text)
            self.assertIn(_lib.format_size(2 * len(PHOTO_BYTES)), text)

    def test_copy_is_filed_under_the_photo_alias_path(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            dest = tmp / 'safety'
            archive = _make_archive(tmp, originals_backup=str(dest))
            photo = _add_photo(archive)
            OriginalBackup(archive, _config(archive)).ensure(photo)
            self.assertEqual(
                (dest / 'photos' / '1912' / 'margaret.jpg').read_bytes(), PHOTO_BYTES)

    def test_an_interrupted_copy_leaves_no_file_that_looks_pristine(self) -> None:
        """A half-written copy is the one wrong answer: the next run would read
        it as "already backed up" and write the original with no real copy."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            dest = tmp / 'safety'
            archive = _make_archive(tmp, originals_backup=str(dest))
            photo = _add_photo(archive)
            backup = OriginalBackup(archive, _config(archive))

            def _die(src, dst, *a, **k):
                Path(dst).write_bytes(PHOTO_BYTES[:5])     # a partial copy
                raise OSError(28, 'No space left on device')

            real_copy = _lib.shutil.copy2
            _lib.shutil.copy2 = _die
            try:
                with self.assertRaises(BackupRefused):
                    backup.ensure(photo)
            finally:
                _lib.shutil.copy2 = real_copy
            self.assertFalse((dest / 'photos' / '1912' / 'margaret.jpg').exists())
            self.assertEqual(list((dest / 'photos' / '1912').glob('*')), [])


class ProcessBackupTests(unittest.TestCase):
    """`fha process` - the first write a photo ever gets, so the copy it takes
    is the photo as the camera or scanner left it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._real_run = process.subprocess.run
        self._real_copy = _lib.shutil.copy2

    def tearDown(self) -> None:
        process.subprocess.run = self._real_run
        _lib.shutil.copy2 = self._real_copy
        self._tmp.cleanup()

    def _process(self, archive: Path, photo: Path):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = process.process_photo(
                archive, _config(archive), photo,
                slug=None, title=None, source_date=None, dry_run=False)
        return rc, out.getvalue(), err.getvalue()

    def test_the_copy_holds_the_bytes_from_before_the_write(self) -> None:
        dest = self.tmp / 'safety'
        archive = _make_archive(self.tmp, originals_backup=str(dest))
        photo = _add_photo(archive)
        fake = FakeExiftool()
        process.subprocess.run = fake

        rc, out, _err = self._process(archive, photo)

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(fake.writes, [photo])
        self.assertTrue(photo.read_bytes().endswith(WRITE_MARKER))
        copy = dest / 'photos' / '1912' / 'margaret.jpg'
        self.assertEqual(copy.read_bytes(), PHOTO_BYTES)
        self.assertIn('1 original(s) copied', out)

    def test_a_failed_copy_refuses_the_write(self) -> None:
        """Fail closed: a safety copy that silently did not happen is worse
        than none, because it is relied on."""
        dest = self.tmp / 'safety'
        archive = _make_archive(self.tmp, originals_backup=str(dest))
        photo = _add_photo(archive)
        fake = FakeExiftool()
        process.subprocess.run = fake

        def _die(src, dst, *a, **k):
            raise OSError(13, 'Permission denied')

        _lib.shutil.copy2 = _die
        rc, _out, err = self._process(archive, photo)

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertEqual(fake.writes, [])                     # no exiftool write at all
        self.assertEqual(photo.read_bytes(), PHOTO_BYTES)     # the photo is untouched
        self.assertIn('photos/1912/margaret.jpg', err)
        self.assertIn('Permission denied', err)
        self.assertIn('Nothing was written to the file', err)
        self.assertIn('originals_backup', err)
        self.assertNotIn('Traceback', err)
        self.assertEqual(list((archive / 'sources').rglob('*.md')), [])

    def test_an_existing_copy_is_never_overwritten(self) -> None:
        """The valuable artifact is the file as it was before fha ever touched
        it, so a later keyword write must not replace it with a written copy."""
        dest = self.tmp / 'safety'
        archive = _make_archive(self.tmp, originals_backup=str(dest))
        photo = _add_photo(archive)
        copy = dest / 'photos' / '1912' / 'margaret.jpg'
        copy.parent.mkdir(parents=True)
        copy.write_bytes(b'the-copy-made-on-the-very-first-run')
        process.subprocess.run = FakeExiftool()

        # A second write to the same photo: `--more` attaches a companion.
        rc, out, _err = self._process(archive, photo)

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(copy.read_bytes(), b'the-copy-made-on-the-very-first-run')
        self.assertIn('1 already had a copy from an earlier run', out)

    def test_dry_run_says_where_the_copy_would_go_and_writes_nothing(self) -> None:
        """A preview that hides the guard the live run applies is not a preview
        of that run - but announcing is all it does: no copy, no photo write."""
        dest = self.tmp / 'safety'
        archive = _make_archive(self.tmp, originals_backup=str(dest))
        photo = _add_photo(archive)
        fake = FakeExiftool()
        process.subprocess.run = fake

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = process.process_photo(
                archive, _config(archive), photo,
                slug=None, title=None, source_date=None, dry_run=True)

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn(str(dest), out.getvalue())
        self.assertFalse(dest.exists())
        self.assertEqual(photo.read_bytes(), PHOTO_BYTES)
        self.assertEqual(fake.writes, [])

    def test_dry_run_warns_when_no_safety_copies_are_configured(self) -> None:
        archive = _make_archive(self.tmp)
        photo = _add_photo(archive)
        process.subprocess.run = FakeExiftool()

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = process.process_photo(
                archive, _config(archive), photo,
                slug=None, title=None, source_date=None, dry_run=True)

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('No safety copies are being kept', err.getvalue())
        self.assertEqual(photo.read_bytes(), PHOTO_BYTES)

    def test_folder_triage_warns_once_for_the_whole_run(self) -> None:
        """A triage folder processes group after group. The warning belongs to
        the RUN, so twelve sets must not produce twelve copies of it."""
        archive = _make_archive(self.tmp)          # no originals_backup:
        folder = archive / 'photos' / '1912'
        first, second = _add_photo(archive, 'aunt.jpg'), _add_photo(archive, 'uncle.jpg')
        process.subprocess.run = FakeExiftool()
        real_prompt = process._prompt
        process._prompt = lambda _msg: 'all'
        try:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = process.process_folder(
                    archive, _config(archive), folder,
                    source_date=None, dry_run=False)
        finally:
            process._prompt = real_prompt

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(err.getvalue().count('No safety copies are being kept'), 1)
        self.assertTrue(first.read_bytes().endswith(WRITE_MARKER))
        self.assertTrue(second.read_bytes().endswith(WRITE_MARKER))

    def test_destination_inside_an_asset_root_refuses_the_write(self) -> None:
        archive = _make_archive(self.tmp, originals_backup='photos/_safety')
        photo = _add_photo(archive)
        fake = FakeExiftool()
        process.subprocess.run = fake

        rc, _out, err = self._process(archive, photo)

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertEqual(fake.writes, [])
        self.assertEqual(photo.read_bytes(), PHOTO_BYTES)
        self.assertIn('your photos root', err)
        self.assertIn('originals_backup', err)

    def test_unconfigured_warns_once_for_the_run_and_proceeds(self) -> None:
        """One run, two photos, one warning. Per-photo would be unreadable on
        the library this feature exists for; and it must not refuse - safety
        copies are opt-in, so every existing archive keeps working."""
        archive = _make_archive(self.tmp)          # no originals_backup:
        front = _add_photo(archive, 'portrait_1912.jpg')
        back = _add_photo(archive, 'portrait_1912-back.jpg')
        process.subprocess.run = FakeExiftool()

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = process.process_photo_group(
                archive, _config(archive), [front, back],
                slug=None, title=None, source_date=None, dry_run=False)

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(err.getvalue().count('No safety copies are being kept'), 1)
        self.assertIn('originals_backup', err.getvalue())
        # Opt-in: both photos were still written, under one shared S-id.
        self.assertTrue(front.read_bytes().endswith(WRITE_MARKER))
        self.assertTrue(back.read_bytes().endswith(WRITE_MARKER))
        self.assertEqual(len(list((archive / 'sources').rglob('*.md'))), 1)


class PhotoindexBackupTests(unittest.TestCase):
    """The same guard on `fha photoindex`'s two writers - the second and third
    places a photo original is written."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._real_run = photoindex.subprocess.run
        self._real_copy = _lib.shutil.copy2

    def tearDown(self) -> None:
        photoindex.subprocess.run = self._real_run
        _lib.shutil.copy2 = self._real_copy
        self._tmp.cleanup()

    def test_tag_person_write_copies_before_it_writes(self) -> None:
        dest = self.tmp / 'safety'
        archive = _make_archive(self.tmp, originals_backup=str(dest))
        photo = _add_photo(archive)
        fake = FakeExiftool()
        photoindex.subprocess.run = fake
        backup = OriginalBackup(archive, _config(archive))

        results = photoindex._run_exiftool_write([photo], 'P-de957bcda1', backup=backup)

        self.assertEqual(results, {photo: None})
        self.assertEqual(fake.writes, [photo])
        self.assertEqual(
            (dest / 'photos' / '1912' / 'margaret.jpg').read_bytes(), PHOTO_BYTES)

    def test_tag_person_write_is_refused_when_the_copy_fails(self) -> None:
        dest = self.tmp / 'safety'
        archive = _make_archive(self.tmp, originals_backup=str(dest))
        photo = _add_photo(archive)
        fake = FakeExiftool()
        photoindex.subprocess.run = fake
        _lib.shutil.copy2 = lambda *a, **k: (_ for _ in ()).throw(
            OSError(28, 'No space left on device'))
        backup = OriginalBackup(archive, _config(archive))

        results = photoindex._run_exiftool_write([photo], 'P-de957bcda1', backup=backup)

        self.assertEqual(fake.writes, [])
        self.assertEqual(photo.read_bytes(), PHOTO_BYTES)
        message = results[photo]
        self.assertIsNotNone(message)
        self.assertIn('photos/1912/margaret.jpg', message)
        self.assertIn('No space left on device', message)
        self.assertIn('Nothing was written to the file', message)

    def test_set_summary_write_copies_before_it_writes(self) -> None:
        dest = self.tmp / 'safety'
        archive = _make_archive(self.tmp, originals_backup=str(dest))
        photo = _add_photo(archive)
        fake = FakeExiftool()
        photoindex.subprocess.run = fake
        backup = OriginalBackup(archive, _config(archive))

        results = photoindex._run_exiftool_write_comment(
            [(photo, 'AI: Margaret at the farm')], backup=backup)

        self.assertEqual(results, {photo: None})
        self.assertEqual(fake.writes, [photo])
        self.assertEqual(
            (dest / 'photos' / '1912' / 'margaret.jpg').read_bytes(), PHOTO_BYTES)

    def test_set_summary_write_is_refused_when_the_copy_fails(self) -> None:
        dest = self.tmp / 'safety'
        archive = _make_archive(self.tmp, originals_backup=str(dest))
        photo = _add_photo(archive)
        fake = FakeExiftool()
        photoindex.subprocess.run = fake
        _lib.shutil.copy2 = lambda *a, **k: (_ for _ in ()).throw(
            OSError(13, 'Permission denied'))
        backup = OriginalBackup(archive, _config(archive))

        results = photoindex._run_exiftool_write_comment(
            [(photo, 'AI: Margaret at the farm')], backup=backup)

        self.assertEqual(fake.writes, [])
        self.assertEqual(photo.read_bytes(), PHOTO_BYTES)
        self.assertIn('Permission denied', results[photo])


class TagPersonEndToEndTests(unittest.TestCase):
    """`fha photoindex tag-person` end to end over the photo fixture.

    The direct-writer tests above pin the guard; this one pins that the engine
    a headless caller reaches (`apply_tag_person`, called with no backup of its
    own) builds one and goes through it, so no path can write around the rule.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._real_run = photoindex.subprocess.run
        self._real_exiftool = photoindex._run_exiftool

    def tearDown(self) -> None:
        photoindex.subprocess.run = self._real_run
        photoindex._run_exiftool = self._real_exiftool
        self._tmp.cleanup()

    def test_apply_tag_person_copies_each_original_before_writing(self) -> None:
        fixture = ROOT / 'tests' / 'fixtures' / 'photo-fixture'
        archive = self.tmp / 'photo-fixture'
        shutil.copytree(fixture, archive,
                        ignore=shutil.ignore_patterns('.cache'))
        photoindex._run_exiftool = lambda paths: [
            {'SourceFile': str(p)} for p in paths]
        photoindex.run_scan(archive, {'roots': {'photos': 'photos'}})

        dest = self.tmp / 'safety'
        cfg = {'roots': {'photos': 'photos'}, 'originals_backup': str(dest)}
        photo = archive / 'photos' / 'family_reunion.jpg'
        pristine = photo.read_bytes()
        fake = FakeExiftool()
        photoindex.subprocess.run = fake

        result = photoindex.apply_tag_person(
            archive, cfg, 'p-de957bcda1', ['photos/family_reunion.jpg'])

        self.assertEqual(result['tagged'], ['photos/family_reunion.jpg'])
        self.assertEqual(fake.writes, [photo])
        self.assertEqual((dest / 'photos' / 'family_reunion.jpg').read_bytes(),
                         pristine)
        self.assertTrue(photo.read_bytes().endswith(WRITE_MARKER))
        notes = [m.text for m in result.messages
                 if m.code == photoindex.BACKUP_MESSAGE_CODE]
        self.assertTrue(any('1 original(s) copied' in n for n in notes), notes)


if __name__ == '__main__':
    unittest.main()
