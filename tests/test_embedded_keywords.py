"""The shared keyword reader/writer (#252): every keyword fha writes lands in
every field a photo manager reads, and every read sees all of them.

What the owner would notice without this: a photo processed by fha shows no
`SOURCE:` or person keyword in Lightroom (it reads XMP; fha wrote IPTC only),
and the next "Save Metadata to File" in Lightroom erases the ID that lets
`fha reconcile` find the photo after it moves.

Two layers of tests:

  * no exiftool needed - the field choice per format, the union read, the
    command the writer builds, exiftool's "exit 0 but nothing written"
    report, the backup guard, and that every call site (process, photoindex
    tag-person, source clear-keyword, lint) goes through the shared code;
  * real exiftool (skipped when it is not installed) - each tag spelling is
    really writable, add/remove round-trips in IPTC + both XMP fields on
    JPEG/TIFF and the XMP fields on PNG/PDF, a legacy IPTC-only keyword is
    mirrored rather than duplicated, and the safety copy holds the pristine
    bytes and is never overwritten by a second write.

Run: py -3.14 -m unittest tests.test_embedded_keywords -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))

import _lib  # noqa: E402
import lint  # noqa: E402
import photoindex  # noqa: E402
import process  # noqa: E402
import source as source_mod  # noqa: E402
from _lib import OriginalBackup  # noqa: E402
from keyword_fixture_files import (  # noqa: E402
    EXIFTOOL, fixture_bytes, legacy_iptc_write, read_fields, write_fixture)

SID = 'S-a1b2c3d4e5'
PID = 'P-de957bcda1'
ALL_THREE = ('IPTC:Keywords', 'XMP-dc:Subject', 'XMP-lr:HierarchicalSubject')


class _Recorder:
    """Stand-in for subprocess.run that records each command."""

    def __init__(self, *, returncode=0, stdout='    1 image files updated\n', stderr=''):
        self.calls: list[list[str]] = []
        self.result = SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    def __call__(self, cmd, **_kwargs):
        self.calls.append(list(cmd))
        return self.result


def _no_backup(tmp: Path) -> OriginalBackup:
    return OriginalBackup(tmp, {})


class FieldChoiceTests(unittest.TestCase):
    """Which keyword fields a file of each format gets."""

    def test_jpeg_and_tiff_get_iptc_and_both_xmp_fields(self) -> None:
        for name in ('a.jpg', 'a.JPEG', 'scan.tif', 'scan.tiff', 'raw.dng'):
            with self.subTest(name=name):
                self.assertEqual(_lib.keyword_tags_for(name), ALL_THREE)

    def test_png_gif_heic_and_pdf_get_the_two_xmp_fields(self) -> None:
        # IPTC in a PNG is a non-standard block no photo manager reads, and
        # exiftool silently skips it in a PDF or GIF.
        for name in ('a.png', 'a.gif', 'a.heic', 'letter.pdf'):
            with self.subTest(name=name):
                self.assertEqual(_lib.keyword_tags_for(name),
                                 ('XMP-dc:Subject', 'XMP-lr:HierarchicalSubject'))

    def test_the_writable_hyphen_spelling_is_used(self) -> None:
        # `XMP:dc:Subject` (two colons) is refused by exiftool; photokin's
        # canonical spelling is `XMP-dc:Subject`.
        self.assertIn('XMP-dc:Subject', _lib.KEYWORD_TAGS)
        self.assertFalse(any(tag.count(':') > 1 for tag in _lib.KEYWORD_TAGS))


class ReadUnionTests(unittest.TestCase):
    """Reads union IPTC Keywords, XMP dc:subject and lr:hierarchicalSubject."""

    def test_union_of_all_three_grouped_fields(self) -> None:
        row = {'IPTC:Keywords': ['SOURCE: ' + SID],
               'XMP-dc:Subject': 'Ada Hartley',
               'XMP-lr:HierarchicalSubject': [PID]}
        self.assertEqual(_lib.keyword_values_from_row(row),
                         ['SOURCE: ' + SID, 'Ada Hartley', PID])

    def test_a_keyword_only_in_hierarchical_subject_is_seen(self) -> None:
        self.assertEqual(
            _lib.keyword_values_from_row({'XMP-lr:HierarchicalSubject': 'SOURCE: ' + SID}),
            ['SOURCE: ' + SID])

    def test_lightroom_hierarchy_contributes_its_leaf_once(self) -> None:
        row = {'XMP-dc:Subject': ['Ada Hartley'],
               'XMP-lr:HierarchicalSubject': ['People|Hartley|Ada Hartley']}
        self.assertEqual(_lib.keyword_values_from_row(row), ['Ada Hartley'])

    def test_ungrouped_rows_still_read(self) -> None:
        # photoindex's catalog read (and older canned test rows) carry the
        # bare tag names.
        row = {'Keywords': ['A'], 'Subject': ['A', 'B'], 'HierarchicalSubject': 'C'}
        self.assertEqual(_lib.keyword_values_from_row(row), ['A', 'B', 'C'])

    def test_a_pdf_subject_title_is_not_a_keyword(self) -> None:
        row = {'PDF:Subject': 'Will of Ada Hartley', 'XMP-dc:Subject': 'SOURCE: ' + SID}
        self.assertEqual(_lib.keyword_values_from_row(row), ['SOURCE: ' + SID])

    def test_numbers_come_back_as_text(self) -> None:
        self.assertEqual(_lib.keyword_values_from_row({'IPTC:Keywords': [1911]}), ['1911'])

    def test_every_reader_asks_for_hierarchical_subject(self) -> None:
        self.assertIn('-HierarchicalSubject', _lib.KEYWORD_READ_ARGS)
        self.assertIn('-HierarchicalSubject', photoindex._EXIFTOOL_FIELDS)


class WriterCommandTests(unittest.TestCase):
    """The one exiftool call `write_embedded_keywords` makes."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, name: str, **kw):
        path = self.tmp / name
        path.write_bytes(b'x')
        rec = _Recorder(**kw.pop('proc', {}))
        with unittest.mock.patch.object(_lib.subprocess, 'run', rec):
            err = _lib.write_embedded_keywords(path, backup=_no_backup(self.tmp),
                                               command='test', **kw)
        return err, rec

    def test_an_add_writes_every_field_in_one_call_without_duplicating(self) -> None:
        err, rec = self._write('ada.jpg', add=['SOURCE: ' + SID])
        self.assertIsNone(err)
        self.assertEqual(len(rec.calls), 1)
        cmd = rec.calls[0]
        for tag in ALL_THREE:
            # `-=` then `+=` leaves exactly one copy, whatever was there.
            self.assertIn(f'-{tag}-=SOURCE: {SID}', cmd)
            self.assertIn(f'-{tag}+=SOURCE: {SID}', cmd)
            self.assertLess(cmd.index(f'-{tag}-=SOURCE: {SID}'),
                            cmd.index(f'-{tag}+=SOURCE: {SID}'))
        self.assertIn('-overwrite_original_in_place', cmd)

    def test_a_pdf_add_skips_iptc(self) -> None:
        _err, rec = self._write('will.pdf', add=['SOURCE: ' + SID])
        self.assertFalse(any(a.startswith('-IPTC:') for a in rec.calls[0]))
        self.assertIn(f'-XMP-dc:Subject+=SOURCE: {SID}', rec.calls[0])

    def test_a_remove_takes_the_value_out_of_all_three_fields(self) -> None:
        # Even from a PDF: an older tool may have left an IPTC copy anywhere.
        _err, rec = self._write('will.pdf', remove=[PID])
        for tag in ALL_THREE:
            self.assertIn(f'-{tag}-={PID}', rec.calls[0])
        self.assertFalse(any('+=' in a for a in rec.calls[0]))

    def test_exit_zero_with_nothing_written_is_a_failure(self) -> None:
        # What `exiftool -keywords+=X a.gif` really prints: exit 0, no change.
        err, _rec = self._write('a.jpg', add=[PID], proc={
            'stdout': '    0 image files updated\n    1 image files unchanged\n'})
        self.assertIsNotNone(err)
        self.assertIn('nothing was written', err)

    def test_exit_zero_with_files_not_updated_is_a_failure(self) -> None:
        err, _rec = self._write('a.jpg', add=[PID], proc={
            'stdout': "    0 image files updated\n    1 files weren't updated due to errors\n"})
        self.assertIsNotNone(err)

    def test_a_removal_that_found_nothing_is_not_a_failure(self) -> None:
        err, _rec = self._write('a.jpg', remove=[PID], proc={
            'stdout': '    0 image files updated\n    1 image files unchanged\n'})
        self.assertIsNone(err)

    def test_nonzero_exit_returns_exiftools_words(self) -> None:
        err, _rec = self._write('a.jpg', add=[PID], proc={
            'returncode': 1, 'stdout': '', 'stderr': 'Error: Not a valid JPG - a.jpg'})
        self.assertIn('Not a valid JPG', err)

    def test_a_minor_warning_with_a_real_update_is_success(self) -> None:
        err, _rec = self._write('a.jpg', add=[PID], proc={
            'stdout': '    1 image files updated\n',
            'stderr': 'Warning: [minor] Fixed incorrect URI for xmlns:MicrosoftPhoto'})
        self.assertIsNone(err)

    def test_missing_exiftool_raises_the_narrow_error(self) -> None:
        path = self.tmp / 'a.jpg'
        path.write_bytes(b'x')
        with unittest.mock.patch.object(_lib.subprocess, 'run', side_effect=FileNotFoundError()):
            with self.assertRaises(_lib.ExiftoolUnavailableError):
                _lib.write_embedded_keywords(path, add=[PID], backup=_no_backup(self.tmp),
                                             command='fha process')

    def test_a_refused_safety_copy_refuses_the_write(self) -> None:
        archive = self.tmp / 'archive'
        (archive / 'photos').mkdir(parents=True)
        photo = archive / 'photos' / 'a.jpg'
        photo.write_bytes(b'x')
        backup = OriginalBackup(archive, {'originals_backup': str(self.tmp / 'safe')})
        rec = _Recorder()
        with unittest.mock.patch.object(_lib.subprocess, 'run', rec), \
                unittest.mock.patch.object(_lib.shutil, 'copy2',
                                           side_effect=OSError(28, 'No space left on device')):
            err = _lib.write_embedded_keywords(photo, add=[PID], backup=backup, command='t')
        self.assertEqual(rec.calls, [])
        self.assertIn('No space left on device', err)

    def test_a_keyword_spanning_lines_is_refused_before_exiftool(self) -> None:
        err, rec = self._write('a.jpg', add=['two\nlines'])
        self.assertIsNotNone(err)
        self.assertEqual(rec.calls, [])


class CallSitesGoThroughTheSharedCodeTests(unittest.TestCase):
    """Every keyword writer and reader routes through `_lib` (one place to
    get the field list right)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.photo = self.tmp / 'ada.jpg'
        self.photo.write_bytes(b'x')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _cmd_of(self, fn) -> list[str]:
        rec = _Recorder()
        with unittest.mock.patch.object(_lib.subprocess, 'run', rec):
            fn()
        self.assertEqual(len(rec.calls), 1)
        return rec.calls[0]

    def test_process_embed_and_rollback(self) -> None:
        backup = _no_backup(self.tmp)
        cmd = self._cmd_of(lambda: process._run_exiftool_embed_source(
            self.photo, SID, extra_keywords=[PID], backup=backup))
        for tag in ALL_THREE:
            self.assertIn(f'-{tag}+=SOURCE: {SID}', cmd)
            self.assertIn(f'-{tag}+={PID}', cmd)
        cmd = self._cmd_of(lambda: process._run_exiftool_remove_source(
            self.photo, SID, extra_keywords=[PID], backup=backup))
        for tag in ALL_THREE:
            self.assertIn(f'-{tag}-=SOURCE: {SID}', cmd)
            self.assertIn(f'-{tag}-={PID}', cmd)

    def test_photoindex_tag_person(self) -> None:
        cmd = self._cmd_of(lambda: photoindex._run_exiftool_write(
            [self.photo], PID, backup=_no_backup(self.tmp)))
        for tag in ALL_THREE:
            self.assertIn(f'-{tag}+={PID}', cmd)

    def test_source_clear_keyword(self) -> None:
        cmd = self._cmd_of(lambda: source_mod._run_exiftool_edit_keyword_fields(
            self.photo, remove=[('subject', 'Wrong Name')], add=[('subject', 'Right Name')],
            backup=_no_backup(self.tmp)))
        for tag in ALL_THREE:
            self.assertIn(f'-{tag}-=Wrong Name', cmd)
            self.assertIn(f'-{tag}+=Right Name', cmd)

    def test_readers_ask_for_all_three_fields(self) -> None:
        rec = _Recorder(stdout='[{}]')
        with unittest.mock.patch.object(_lib.subprocess, 'run', rec), \
                unittest.mock.patch.object(lint.subprocess, 'run', rec):
            process._run_exiftool_read_keywords(self.photo)
            source_mod._run_exiftool_read_keyword_fields(self.photo)
            lint._run_exiftool_keyword_rows([self.photo])
        for cmd in rec.calls:
            for arg in ('-Keywords', '-Subject', '-HierarchicalSubject'):
                self.assertIn(arg, cmd)

    def test_readers_see_a_keyword_in_hierarchical_subject_only(self) -> None:
        # E012 / the refile identity check / clear-keyword must not miss a
        # SOURCE: keyword just because only Lightroom's field holds it.
        stdout = f'[{{"SourceFile": "{self.photo.as_posix()}", ' \
                 f'"XMP-lr:HierarchicalSubject": "SOURCE: {SID}"}}]'
        rec = _Recorder(stdout=stdout)
        with unittest.mock.patch.object(_lib.subprocess, 'run', rec):
            self.assertEqual(process._read_all_source_keywords(self.photo), [SID.lower()])
            fields = source_mod._run_exiftool_read_keyword_fields(self.photo)
        self.assertEqual(fields['hierarchical'], [f'SOURCE: {SID}'])
        with unittest.mock.patch.object(lint.subprocess, 'run', rec):
            sids, _raw = lint._read_exif_keywords([self.photo], set())
        self.assertEqual(sids[self.photo.resolve()], {SID.lower()})


@unittest.skipUnless(EXIFTOOL, 'exiftool is not installed - these run on a machine that has it')
class RealExiftoolTests(unittest.TestCase):
    """Against the real binary: what actually lands in each field."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, path: Path, **kw) -> str | None:
        return _lib.write_embedded_keywords(path, backup=_no_backup(self.tmp),
                                            command='test', **kw)

    def test_each_tag_spelling_is_really_writable(self) -> None:
        # The guard photokin keeps for its own tags: write each spelling into a
        # real file and read the value back, rather than trust the exit code.
        for tag in _lib.KEYWORD_TAGS:
            with self.subTest(tag=tag):
                path = write_fixture(self.tmp / f'probe-{tag.replace(":", "_")}.jpg')
                import subprocess
                proc = subprocess.run(['exiftool', f'-{tag}+=probe', '-overwrite_original_in_place',
                                       str(path)], capture_output=True, text=True)
                self.assertNotIn("isn't writable", proc.stderr)
                self.assertNotIn('is not defined', proc.stderr)
                self.assertEqual(read_fields(path)[tag], ['probe'])

    def test_add_then_remove_round_trips_in_every_field_per_format(self) -> None:
        for suffix, tags in (('.jpg', ALL_THREE), ('.tif', ALL_THREE),
                             ('.png', ALL_THREE[1:]), ('.pdf', ALL_THREE[1:])):
            with self.subTest(suffix=suffix):
                path = write_fixture(self.tmp / f'evidence{suffix}')
                self.assertIsNone(self._write(path, add=[f'SOURCE: {SID}', PID]))
                fields = read_fields(path)
                for tag in ALL_THREE:
                    expected = [f'SOURCE: {SID}', PID] if tag in tags else []
                    self.assertEqual(fields[tag], expected, f'{suffix} {tag}')
                self.assertEqual(_lib.read_embedded_keywords(path, command='t'),
                                 [f'SOURCE: {SID}', PID])
                self.assertIsNone(self._write(path, remove=[f'SOURCE: {SID}', PID]))
                self.assertEqual(read_fields(path),
                                 {tag: [] for tag in ALL_THREE}, suffix)

    def test_a_legacy_iptc_only_keyword_is_mirrored_not_doubled(self) -> None:
        path = write_fixture(self.tmp / 'old.jpg')
        legacy_iptc_write(path, PID)
        self.assertEqual(_lib.read_embedded_keywords(path, command='t'), [PID])
        self.assertIsNone(self._write(path, add=[PID]))
        self.assertEqual(read_fields(path), {tag: [PID] for tag in ALL_THREE})

    def test_existing_keywords_are_kept(self) -> None:
        path = write_fixture(self.tmp / 'kept.jpg')
        self.assertIsNone(self._write(path, add=['Hartley farm']))
        self.assertIsNone(self._write(path, add=[f'SOURCE: {SID}']))
        self.assertIsNone(self._write(path, remove=[f'SOURCE: {SID}']))
        self.assertEqual(read_fields(path)['XMP-dc:Subject'], ['Hartley farm'])

    def test_a_file_exiftool_cannot_write_is_reported(self) -> None:
        path = self.tmp / 'not-really.jpg'
        path.write_bytes(b'this is not a jpeg')
        err = self._write(path, add=[PID])
        self.assertIsNotNone(err)
        self.assertNotIn('_exiftool_tmp', [p.name[-13:] for p in self.tmp.iterdir()])

    def test_safety_copy_is_pristine_and_never_overwritten(self) -> None:
        archive = self.tmp / 'archive'
        photo = write_fixture(archive / 'photos' / '1911' / 'ada.jpg')
        (archive / 'fha.yaml').write_text('roots:\n  photos: photos\n', encoding='utf-8')
        pristine = photo.read_bytes()
        cfg = {'roots': {'photos': 'photos'}, 'originals_backup': str(self.tmp / 'safe')}
        copy = self.tmp / 'safe' / 'photos' / '1911' / 'ada.jpg'
        for keyword in (f'SOURCE: {SID}', PID):   # two runs, two backups
            backup = OriginalBackup(archive, cfg)
            self.assertIsNone(_lib.write_embedded_keywords(
                photo, add=[keyword], backup=backup, command='t'))
            self.assertEqual(copy.read_bytes(), pristine)
        self.assertNotEqual(photo.read_bytes(), pristine)

    def test_process_reads_a_source_keyword_held_only_in_hierarchical_subject(self) -> None:
        # Lightroom's own field: before #252 no reader asked for it, so a
        # SOURCE: keyword there was invisible to `fha process`'s
        # already-processed check and the refile identity check.
        path = write_fixture(self.tmp / 'lr.jpg')
        legacy_iptc_write(path, 'Hartley farm')
        import subprocess
        subprocess.run(['exiftool', f'-XMP-lr:HierarchicalSubject+=SOURCE: {SID}',
                        '-overwrite_original_in_place', str(path)], check=True,
                       capture_output=True)
        self.assertEqual(process._read_all_source_keywords(path), [SID.lower()])


class FixtureSanityTests(unittest.TestCase):
    def test_fixture_bytes_exist_for_every_document_format(self) -> None:
        for suffix in ('.jpg', '.tif', '.png', '.pdf'):
            self.assertTrue(fixture_bytes(suffix))


if __name__ == '__main__':
    unittest.main()
