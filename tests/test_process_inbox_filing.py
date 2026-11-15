"""Filing out of the inbox, and attaching with `--more`, on an archive laid out
the way a real install is: the photo library, the documents folder and the
inbox are three separate folders outside the archive (`roots:` in fha.yaml).

Each test names the issue it guards:

  * #274 - a page attached with `--more` to a document source is filed in the
    documents root beside the primary, whatever its extension; a file outside
    every root and the inbox is refused with advice that names the inbox.
  * #275 - `--dest` chooses the photo-library folder for a photo filed out of
    the inbox; without it the live output says the photo is at the top.
  * #276 - only an image or a PDF counts as a back scan.
  * #277 - the line saying the original was kept in `inbox/processed/` is
    said only once the command has succeeded, and reads in order after the
    lines above it.
  * #278 - a `--more` attachment in the documents root is named for its source.
  * PR #287 - the no-safety-copies warning names the right kind of file.

The photo paths never call exiftool: `test_process.FakePhotoStore` stands in.

Run: python -m pytest tests/test_process_inbox_filing.py -q   (from the repo root)
"""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))

import process
from _lib import EXIT_CLEAN, EXIT_ERRORS, is_back_scan_candidate, read_record
from test_process import FakePhotoStore

JPEG = b'\xff\xd8\xff\xe0 synthetic'
TIFF = b'II*\x00 synthetic'
PDF = b'%PDF-1.4 synthetic'


class InboxFilingTestCase(unittest.TestCase):
    """An archive whose three asset roots are separate folders beside it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.archive = self.tmp / 'archive'
        for sub in ('sources', 'people', 'notes'):
            (self.archive / sub).mkdir(parents=True)
        self.photos = self.tmp / 'lib-photos'
        self.docs = self.tmp / 'lib-docs'
        self.inbox = self.tmp / 'inbox'
        for d in (self.photos, self.docs, self.inbox):
            d.mkdir()
        (self.archive / 'fha.yaml').write_text(
            f'roots:\n  photos: {self.photos.as_posix()}\n'
            f'  documents: {self.docs.as_posix()}\n  inbox: {self.inbox.as_posix()}\n',
            encoding='utf-8')
        self.store = FakePhotoStore()
        self._saved = (process._run_exiftool_read_keywords,
                       process._run_exiftool_embed_source,
                       process._run_exiftool_remove_source,
                       process._run_exiftool_read_meta)
        process._run_exiftool_read_keywords = self.store.read
        process._run_exiftool_embed_source = self.store.embed
        process._run_exiftool_remove_source = self.store.remove
        process._run_exiftool_read_meta = lambda _p: {
            'caption': None, 'user_comment': None, 'edtf': None, 'has_pid_keyword': False}

    def tearDown(self) -> None:
        (process._run_exiftool_read_keywords, process._run_exiftool_embed_source,
         process._run_exiftool_remove_source, process._run_exiftool_read_meta) = self._saved
        self._tmp.cleanup()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = process._standalone_main(list(argv) + ['--root', str(self.archive)])
        return rc, out.getvalue(), err.getvalue()

    def _put(self, path: Path, data: bytes) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def _files(self, root: Path) -> list[str]:
        return sorted(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file())

    def _snapshot(self, root: Path) -> dict[str, bytes]:
        return {p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else b'<dir>')
                for p in root.rglob('*')}

    def _file_letter(self) -> tuple[Path, Path, str]:
        """File `inbox/Letters/scan-0412.pdf` as the letter
        `postcard-from-aunt-1908`; return (primary, record, S-id)."""
        self._put(self.inbox / 'Letters' / 'scan-0412.pdf', PDF)
        rc, _out, err = self._run(str(self.inbox / 'Letters' / 'scan-0412.pdf'),
                                  '--type', 'letter', '--slug', 'postcard-from-aunt-1908')
        self.assertEqual(rc, EXIT_CLEAN, err)
        primary = next((self.docs / 'letter').glob('postcard-from-aunt-1908_S-*.pdf'))
        record = next((self.archive / 'sources' / 'letter').glob('*_S-*.md'))
        return primary, record, primary.stem.split('_')[-1]

    def _files_entries(self, record: Path) -> list[dict]:
        return read_record(record)['meta']['files']


class MoreFollowsTheSourcesRootTests(InboxFilingTestCase):
    """#274: a page joining a record is filed with the record."""

    def test_tif_page_from_inbox_lands_beside_a_document_primary(self) -> None:
        primary, record, sid = self._file_letter()
        page = self._put(self.inbox / 'Letters' / 'scan-0412-p2.tif', TIFF)

        rc, out, err = self._run(str(primary), '--more', str(page), 'page-2')

        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertEqual(self._files(self.photos), [], 'nothing may land in the photo library')
        self.assertTrue((self.docs / 'letter' / f'postcard-from-aunt-1908-page-2_{sid}.tif').is_file())
        entry = self._files_entries(record)[1]
        self.assertEqual(entry['file'], f'documents/letter/postcard-from-aunt-1908-page-2_{sid}.tif')
        self.assertEqual(entry['role'], 'page-2')
        self.assertEqual(entry['original_filename'], 'scan-0412-p2.tif')

    def test_dry_run_previews_the_documents_root(self) -> None:
        primary, _record, _sid = self._file_letter()
        page = self._put(self.inbox / 'Letters' / 'scan-0412-p2.tif', TIFF)
        before = self._snapshot(self.tmp)

        rc, out, err = self._run(str(primary), '--more', str(page), 'page-2', '--dry-run')

        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertIn('into lib-docs/', out.replace(str(self.tmp) + '/', ''))
        self.assertNotIn('lib-photos', out)
        self.assertEqual(self._snapshot(self.tmp), before)

    def test_explicit_type_photo_still_files_a_photograph(self) -> None:
        # The human saying "this one is a photograph" (a snapshot that came in
        # the envelope) is honoured: it goes to the photo library.
        primary, record, _sid = self._file_letter()
        snap = self._put(self.inbox / 'Letters' / 'snapshot.jpg', JPEG)

        rc, _out, err = self._run(str(primary), '--more', str(snap), 'enclosure',
                                  '--type', 'photo')

        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertTrue((self.photos / 'snapshot.jpg').is_file())
        self.assertEqual(self._files_entries(record)[1]['file'], 'photos/snapshot.jpg')

    def test_photo_source_still_takes_a_photo_back_into_the_library(self) -> None:
        # The reverse direction is unchanged: a photo source's back is a photo.
        front = self._put(self.inbox / 'card.jpg', JPEG)
        rc, _out, err = self._run(str(front))
        self.assertEqual(rc, EXIT_CLEAN, err)
        back = self._put(self.inbox / 'card-reverse.jpg', JPEG)
        rc, _out, err = self._run(str(self.photos / 'card.jpg'), '--more', str(back), 'back')
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertTrue((self.photos / 'card-reverse.jpg').is_file())

    def test_file_outside_every_root_and_the_inbox_names_the_inbox(self) -> None:
        primary, record, _sid = self._file_letter()
        stray = self._put(self.tmp / 'elsewhere' / 'camera-0007.jpg', JPEG)

        rc, out, err = self._run(str(primary), '--more', str(stray), 'page-3')

        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn("isn't in your inbox", err)
        self.assertIn('inbox', err)
        self.assertIn('Move it into the inbox first', err)
        self.assertNotIn('photos root', err)
        self.assertNotIn('lib-photos', err)
        self.assertTrue(stray.is_file())
        self.assertEqual(len(self._files_entries(record)), 1)

    def test_archive_inbox_folder_when_the_inbox_lives_elsewhere(self) -> None:
        # The archive's own inbox/ folder is not the inbox once `roots: inbox:`
        # points somewhere else; the refusal names the configured one.
        primary, _record, _sid = self._file_letter()
        misplaced = self._put(self.archive / 'inbox' / 'page-4.jpg', JPEG)

        rc, _out, err = self._run(str(primary), '--more', str(misplaced), 'page-4')

        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn(f"isn't in your inbox ({self.inbox.as_posix()})".replace('/', '/'),
                      err.replace('\\', '/'))

    def test_plain_process_refusal_names_the_inbox_too(self) -> None:
        stray = self._put(self.tmp / 'elsewhere' / 'deed.pdf', PDF)

        rc, _out, err = self._run(str(stray), '--type', 'letter')

        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn("isn't in your inbox", err)
        self.assertNotIn('documents root', err)
        self.assertTrue(stray.is_file())


class MoreNamingTests(InboxFilingTestCase):
    """#278: `{source slug}[-{copy}]-{role}_{S-id}.{ext}`."""

    def test_attachments_take_the_sources_name(self) -> None:
        primary, record, sid = self._file_letter()
        p3 = self._put(self.inbox / 'x' / 'scan-0412-p3.jpg', JPEG)
        back = self._put(self.inbox / 'x' / 'card-77-back.tif', TIFF)

        rc, _out, err = self._run(str(primary), '--more', str(p3), 'page-3')
        self.assertEqual(rc, EXIT_CLEAN, err)
        rc, _out, err = self._run(str(primary), '--more', str(back), 'back:b')
        self.assertEqual(rc, EXIT_CLEAN, err)

        self.assertEqual(sorted(p.name for p in (self.docs / 'letter').iterdir()), sorted([
            f'postcard-from-aunt-1908_{sid}.pdf',
            f'postcard-from-aunt-1908-page-3_{sid}.jpg',
            f'postcard-from-aunt-1908-b-back_{sid}.tif',
        ]))
        entries = self._files_entries(record)
        self.assertEqual(entries[1]['original_filename'], 'scan-0412-p3.jpg')
        self.assertEqual(entries[2]['original_filename'], 'card-77-back.tif')
        self.assertEqual(entries[2]['copy'], 'b')

    def test_a_photo_primary_lends_the_records_slug(self) -> None:
        photo = self._put(self.inbox / 'IMG_0420.jpg', JPEG)
        rc, _out, err = self._run(str(photo), '--title', 'Wedding portrait')
        self.assertEqual(rc, EXIT_CLEAN, err)
        record = next((self.archive / 'sources' / 'photos').glob('wedding-portrait_S-*.md'))
        sid = record.stem.split('_')[-1]
        note = self._put(self.docs / 'photos' / 'my-notes.md', b'# notes\n')

        rc, _out, err = self._run(str(self.photos / 'IMG_0420.jpg'), '--more', str(note),
                                  'transcript')

        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertTrue((self.docs / 'photos' / f'wedding-portrait-transcript_{sid}.md').is_file())

    def test_a_file_already_named_on_the_sources_stem_keeps_its_topic(self) -> None:
        # The recording/transcript skills name companions `{session}-{topic}`
        # on purpose; the role is added to that, the topic is not dropped.
        folder = self.docs / 'interviews' / 'hartley-1998'
        audio = self._put(folder / 'hartley-1998-wedding.m4a', b'audio')
        rc, _out, err = self._run(str(audio), '--type', 'interview', '--slug', 'hartley-1998')
        self.assertEqual(rc, EXIT_CLEAN, err)
        primary = next(folder.glob('hartley-1998_S-*.m4a'))
        sid = primary.stem.split('_')[-1]
        farm = self._put(folder / 'hartley-1998-farm.m4a', b'audio 2')

        rc, _out, err = self._run(str(primary), '--more', str(farm), 'audio')

        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertTrue((folder / f'hartley-1998-farm-audio_{sid}.m4a').is_file())

    def test_already_named_for_this_source_keeps_its_name(self) -> None:
        # #108's rule survives.
        primary, record, sid = self._file_letter()
        named = self._put(self.docs / 'letter' / f'aunt-notes-transcript_{sid}.md', b'text')

        rc, _out, err = self._run(str(primary), '--more', str(named), 'transcript')

        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertTrue(named.is_file())

    def test_two_attachments_with_one_role_are_told_to_use_a_copy_letter(self) -> None:
        primary, record, sid = self._file_letter()
        one = self._put(self.inbox / 'a.jpg', JPEG)
        two = self._put(self.inbox / 'b.jpg', JPEG)
        rc, _out, err = self._run(str(primary), '--more', str(one), 'page')
        self.assertEqual(rc, EXIT_CLEAN, err)

        rc, _out, err = self._run(str(primary), '--more', str(two), 'page')

        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('page:b', err)
        self.assertTrue(two.is_file(), 'the refused file goes back to the inbox')
        self.assertEqual(len(self._files_entries(record)), 2)


class BackScanCandidateTests(InboxFilingTestCase):
    """#276: a note named like a back is not a back scan."""

    def test_the_shared_rule(self) -> None:
        for name in ('x-back.tif', 'x-back.JPG', 'x-back.pdf', 'x-back.png'):
            self.assertTrue(is_back_scan_candidate(name), name)
        for name in ('x-back.md', 'x-back.xmp', 'x-back.txt', 'x-back.json'):
            self.assertFalse(is_back_scan_candidate(name), name)

    def _assert_files_with_tif_back(self, note_name: str) -> None:
        box = self.inbox / 'Box'
        front = self._put(box / 'box-001.tif', TIFF)
        self._put(box / 'box-001-back.tif', TIFF)
        note = self._put(box / note_name, b'a note another program wrote')

        rc, _out, err = self._run(str(front), '--type', 'letter')

        self.assertEqual(rc, EXIT_CLEAN, err)
        record = next((self.archive / 'sources' / 'letter').glob('*_S-*.md'))
        entries = self._files_entries(record)
        self.assertEqual([e['role'] for e in entries], ['primary', 'back'])
        self.assertEqual(entries[1]['original_filename'], 'box-001-back.tif')
        self.assertTrue(note.is_file(), 'the note stays in the inbox, untouched')
        self.assertEqual(note.read_bytes(), b'a note another program wrote')

    def test_md_note_beside_a_real_back(self) -> None:
        self._assert_files_with_tif_back('box-001-back.md')

    def test_xmp_sidecar_beside_a_real_back(self) -> None:
        self._assert_files_with_tif_back('box-001-back.xmp')

    def test_lone_md_note_is_not_filed_as_the_back(self) -> None:
        box = self.inbox / 'Box3'
        front = self._put(box / 'box-003.tif', TIFF)
        note = self._put(box / 'box-003-back.md', b'note')

        rc, _out, err = self._run(str(front), '--type', 'letter')

        self.assertEqual(rc, EXIT_CLEAN, err)
        record = next((self.archive / 'sources' / 'letter').glob('*_S-*.md'))
        self.assertEqual(len(self._files_entries(record)), 1)
        self.assertTrue(note.is_file())
        self.assertEqual(list(self.docs.rglob('*.md')), [])


class FiledLineOnlyOnSuccessTests(InboxFilingTestCase):
    """#277: no "Filed ..." line from a run that refused and put things back."""

    def test_refusal_after_relocation_says_nothing_was_filed(self) -> None:
        box = self.inbox / 'Box'
        front = self._put(box / 'box-001.tif', TIFF)
        self._put(box / 'box-001-back.tif', TIFF)
        self._put(box / 'box-001_back.pdf', PDF)  # a second back: refused
        before = self._snapshot(self.inbox)

        rc, out, err = self._run(str(front), '--type', 'letter')

        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('more than one possible back scan', err)
        self.assertNotIn('is kept at', out)
        self.assertNotIn('Filed', out)
        self.assertEqual(self._snapshot(self.inbox), before)
        self.assertEqual(self._files(self.docs), [])

    def test_more_refusal_after_relocation_says_nothing_was_filed(self) -> None:
        primary, record, _sid = self._file_letter()
        page = self._put(self.inbox / 'Letters' / 'page-9_S-abcdefgh23.jpg', JPEG)
        before_inbox = self._snapshot(self.inbox)
        before_docs = self._files(self.docs)

        rc, out, err = self._run(str(primary), '--more', str(page), 'page-9')

        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('different S-id', err)
        self.assertNotIn('is kept at', out)
        self.assertNotIn('Filed', out)
        self.assertEqual(self._snapshot(self.inbox), before_inbox)
        self.assertEqual(self._files(self.docs), before_docs)

    def test_success_still_reports_the_filing_last(self) -> None:
        self._put(self.inbox / 'Letters' / 'scan-0412.pdf', PDF)
        rc, out, err = self._run(str(self.inbox / 'Letters' / 'scan-0412.pdf'), '--type', 'letter')
        self.assertEqual(rc, EXIT_CLEAN, err)
        lines = out.splitlines()
        kept = [i for i, ln in enumerate(lines)
                if ln.startswith('The original scan-0412.pdf is kept at')]
        self.assertEqual(len(kept), 1, out)
        # Review L6: it reads in order - after the rename it refers back to.
        renamed = [i for i, ln in enumerate(lines) if ln.startswith('Renamed scan-0412.pdf')]
        self.assertTrue(renamed and renamed[0] < kept[0], out)
        self.assertIn('the copy filed above', lines[kept[0]])


class PhotoDestTests(InboxFilingTestCase):
    """#275: `--dest` chooses the photo-library folder on a first filing."""

    def test_dest_files_the_photo_into_that_folder(self) -> None:
        photo = self._put(self.inbox / 'A' / 'B' / 'x.jpg', JPEG)

        rc, out, err = self._run(str(photo), '--type', 'photo', '--dest', 'C')

        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertEqual(self._files(self.photos), ['C/x.jpg'])
        record = next((self.archive / 'sources' / 'photos').glob('*_S-*.md'))
        self.assertEqual(self._files_entries(record)[0]['file'], 'photos/C/x.jpg')
        self.assertTrue((self.inbox / 'processed' / 'A' / 'B' / 'x.jpg').is_file())
        self.assertNotIn('top of your photo library', out)

    def test_dest_that_escapes_the_library_is_refused(self) -> None:
        photo = self._put(self.inbox / 'A' / 'x.jpg', JPEG)
        for bad in ('../outside', str(self.tmp / 'abs'), 'C/../..'):
            with self.subTest(dest=bad):
                before = self._snapshot(self.tmp)
                rc, _out, err = self._run(str(photo), '--type', 'photo', '--dest', bad)
                self.assertEqual(rc, EXIT_ERRORS)
                self.assertIn('--dest', err)
                self.assertEqual(self._snapshot(self.tmp), before)

    def test_two_img_0001_from_different_folders_both_file(self) -> None:
        one = self._put(self.inbox / 'Cemetery Visit 2021' / 'IMG_0001.jpg', JPEG)
        two = self._put(self.inbox / 'Beach Trip 2019' / 'IMG_0001.jpg', JPEG + b'2')

        rc, _out, err = self._run(str(one), '--type', 'photo', '--dest', 'Cemetery Visit 2021')
        self.assertEqual(rc, EXIT_CLEAN, err)
        rc, _out, err = self._run(str(two), '--type', 'photo', '--dest', 'Beach Trip 2019')
        self.assertEqual(rc, EXIT_CLEAN, err)

        self.assertEqual(self._files(self.photos),
                         ['Beach Trip 2019/IMG_0001.jpg', 'Cemetery Visit 2021/IMG_0001.jpg'])

    def test_without_dest_the_live_run_says_top_of_library_and_names_dest(self) -> None:
        photo = self._put(self.inbox / 'Cemetery Visit 2021' / 'IMG_0001.jpg', JPEG)

        rc, out, err = self._run(str(photo), '--type', 'photo')

        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertEqual(self._files(self.photos), ['IMG_0001.jpg'])  # the owner's default
        self.assertIn('top of your photo library', out)
        self.assertIn('--dest "Cemetery Visit 2021"', out)

    def test_dry_run_shows_the_full_destination(self) -> None:
        photo = self._put(self.inbox / 'Trip' / 'IMG_0002.jpg', JPEG)
        before = self._snapshot(self.tmp)

        rc, out, err = self._run(str(photo), '--type', 'photo', '--dest', 'Trips/1999', '--dry-run')

        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertIn('lib-photos/Trips/1999/IMG_0002.jpg', out.replace('\\', '/'))
        self.assertEqual(self._snapshot(self.tmp), before)

    def test_name_taken_refusal_suggests_dest(self) -> None:
        self._put(self.photos / 'IMG_0001.jpg', JPEG)
        photo = self._put(self.inbox / 'Beach Trip 2019' / 'IMG_0001.jpg', JPEG + b'2')

        rc, _out, err = self._run(str(photo), '--type', 'photo')

        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('destination already exists', err)
        self.assertIn('--dest "Beach Trip 2019"', err)
        self.assertTrue(photo.is_file())

    def test_refused_run_leaves_no_empty_dest_folder(self) -> None:
        photo = self._put(self.inbox / 'x.jpg', JPEG)
        self.store.fail_paths.add(str(self.photos / 'New' / 'Deep' / 'x.jpg'))

        rc, _out, _err = self._run(str(photo), '--type', 'photo', '--dest', 'New/Deep')

        self.assertNotEqual(rc, EXIT_CLEAN)
        self.assertTrue(photo.is_file())
        self.assertEqual(list(self.photos.iterdir()), [])

    def test_dest_on_a_document_is_refused(self) -> None:
        doc = self._put(self.inbox / 'deed.pdf', PDF)
        rc, _out, err = self._run(str(doc), '--type', 'letter', '--dest', 'C')
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('filed as a document', err)
        self.assertTrue(doc.is_file())

    def test_dest_with_more_is_refused(self) -> None:
        primary, _record, _sid = self._file_letter()
        page = self._put(self.inbox / 'p.jpg', JPEG)
        rc, _out, err = self._run(str(primary), '--more', str(page), 'page-2', '--dest', 'C')
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('drop --dest', err)
        self.assertTrue(page.is_file())

    def test_dest_on_a_filed_photo_is_refused(self) -> None:
        filed = self._put(self.photos / 'old.jpg', JPEG)
        rc, _out, err = self._run(str(filed), '--dest', 'C')
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('not in the inbox', err)
        self.assertEqual(self.store.read(filed), [])

    def test_inbox_photo_folder_is_refused_with_the_per_photo_command(self) -> None:
        folder = self.inbox / 'Cemetery Visit 2021'
        self._put(folder / 'IMG_0001.jpg', JPEG)

        rc, out, err = self._run(str(folder))

        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('--dest "Cemetery Visit 2021"', err)
        self.assertIn('inbox/Cemetery Visit 2021/IMG_0001.jpg', err)
        self.assertNotIn('Select groups', out)

    def test_bundle_photos_follow_dest(self) -> None:
        folder = self.inbox / 'Reunion'
        self._put(folder / 'group.jpg', JPEG)
        (folder / 'notes.md').write_text('---\ntitle: Reunion 1950\n---\nEveryone came.\n',
                                         encoding='utf-8')

        rc, _out, err = self._run(str(folder), '--dest', 'Reunions')

        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertEqual(self._files(self.photos), ['Reunions/group.jpg'])


class SafetyCopyWordingTests(InboxFilingTestCase):
    """PR #287: the warning names the kind of file being written."""

    def test_attaching_a_pdf_does_not_warn_about_photo_files(self) -> None:
        primary, _record, _sid = self._file_letter()
        page = self._put(self.inbox / 'Letters' / 'page-2.pdf', PDF)

        rc, _out, err = self._run(str(primary), '--more', str(page), 'page-2')

        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertIn('No safety copies are being kept', err)
        self.assertNotIn('original photo files', err)
        self.assertIn('scanned documents', err)

    def test_a_photo_still_gets_the_photo_wording(self) -> None:
        photo = self._put(self.inbox / 'x.jpg', JPEG)
        rc, _out, err = self._run(str(photo))
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertIn('original photo files', err)


class ReviewFollowupTests(InboxFilingTestCase):
    """PR #287 review: the cases the first pass got wrong or left silent."""

    # P2-2: the narrowed naming rule (#278, owner decision)

    def test_a_short_stem_takes_the_sources_slug(self) -> None:
        primary, _record, sid = self._file_letter()
        page = self._put(self.inbox / 'postcard.jpg', JPEG)
        rc, _out, err = self._run(str(primary), '--more', str(page), 'page-4')
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertTrue(
            (self.docs / 'letter' / f'postcard-from-aunt-1908-page-4_{sid}.jpg').is_file(),
            sorted(p.name for p in (self.docs / 'letter').iterdir()))

    def test_a_role_already_in_the_stem_is_not_doubled(self) -> None:
        primary, _record, sid = self._file_letter()
        back = self._put(self.inbox / 'postcard-from-aunt-1908-back.tif', TIFF)
        rc, _out, err = self._run(str(primary), '--more', str(back), 'back:c')
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertTrue(
            (self.docs / 'letter' / f'postcard-from-aunt-1908-c-back_{sid}.tif').is_file(),
            sorted(p.name for p in (self.docs / 'letter').iterdir()))

    def test_a_session_transcript_keeps_its_stem_on_an_audio_source(self) -> None:
        folder = self.docs / 'interviews'
        audio = self._put(folder / 'hartley-1998-audio.m4a', b'audio')
        rc, _out, err = self._run(str(audio), '--type', 'interview', '--slug', 'hartley-1998-audio')
        self.assertEqual(rc, EXIT_CLEAN, err)
        primary = next(folder.glob('hartley-1998-audio_S-*.m4a'))
        sid = primary.stem.split('_')[-1]
        whisper = self._put(folder / 'hartley-1998.md', b'# Whisper transcript\n')
        rc, _out, err = self._run(str(primary), '--more', str(whisper), 'whisper-transcript')
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertTrue((folder / f'hartley-1998-whisper-transcript_{sid}.md').is_file())

    # P2-3: a photo source's --more photo files beside its primary

    def test_more_photo_lands_beside_a_photo_primary(self) -> None:
        front = self._put(self.inbox / 'T4' / 'x.jpg', JPEG)
        rc, _out, err = self._run(str(front), '--type', 'photo', '--dest', 'C')
        self.assertEqual(rc, EXIT_CLEAN, err)
        back = self._put(self.inbox / 'T4' / 'x-back.jpg', JPEG)
        rc, out, err = self._run(str(self.photos / 'C' / 'x.jpg'), '--more', str(back), 'back')
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertEqual(self._files(self.photos), ['C/x-back.jpg', 'C/x.jpg'])
        record = next((self.archive / 'sources' / 'photos').glob('*_S-*.md'))
        self.assertEqual(self._files_entries(record)[1]['file'], 'photos/C/x-back.jpg')
        self.assertNotIn('top of your photo library', out)

    # P2-4: --dest is never accepted and then ignored

    def test_dest_on_a_bundle_without_photos_is_refused(self) -> None:
        folder = self.inbox / 'Papers'
        self._put(folder / 'deed.pdf', PDF)
        (folder / 'notes.md').write_text('---\ntitle: Deed\n---\n', encoding='utf-8')
        before = self._snapshot(self.tmp)
        rc, _out, err = self._run(str(folder), '--dest', 'C')
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('--dest', err)
        self.assertEqual(self._snapshot(self.tmp), before)

    def test_dest_on_a_pointer_only_stub_is_refused(self) -> None:
        stub = self.inbox / 'census.notes.md'
        stub.write_text('---\nasset_elsewhere: true\nexternal_links:\n'
                        '  - https://example.org/record/1\n---\n', encoding='utf-8')
        before = self._snapshot(self.tmp)
        rc, _out, err = self._run(str(stub), '--dest', 'C')
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('--dest', err)
        self.assertEqual(self._snapshot(self.tmp), before)

    # L1: no empty --dest folder survives a refused run

    def test_unparkable_original_leaves_no_empty_dest_folder(self) -> None:
        photo = self._put(self.inbox / 'x.jpg', JPEG)
        (self.inbox / 'processed').write_bytes(b'a plain file where the folder goes')
        rc, _out, err = self._run(str(photo), '--type', 'photo', '--dest', 'New/Deep')
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertTrue(photo.is_file())
        self.assertEqual(list(self.photos.iterdir()), [])

    def test_bundle_rollback_leaves_no_empty_dest_folder(self) -> None:
        folder = self.inbox / 'Reunion'
        self._put(folder / 'group.jpg', JPEG)
        (folder / 'notes.md').write_text('---\ntitle: Reunion\n---\n', encoding='utf-8')
        self.store.fail_paths.add(str(self.photos / 'Reunions' / 'Deep' / 'group.jpg'))
        rc, _out, _err = self._run(str(folder), '--dest', 'Reunions/Deep')
        self.assertNotEqual(rc, EXIT_CLEAN)
        self.assertTrue((folder / 'group.jpg').is_file())
        self.assertEqual(list(self.photos.iterdir()), [])

    # L2: the copy-letter suggestion can be run as shown

    def test_copy_letter_suggestion_names_the_inbox_path(self) -> None:
        primary, _record, _sid = self._file_letter()
        self._put(self.inbox / 'x' / 'a.jpg', JPEG)
        two = self._put(self.inbox / 'x' / 'b.jpg', JPEG)
        rc, _out, err = self._run(str(primary), '--more', str(self.inbox / 'x' / 'a.jpg'), 'page')
        self.assertEqual(rc, EXIT_CLEAN, err)
        rc, _out, err = self._run(str(primary), '--more', str(two), 'page')
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('--more "inbox/x/b.jpg" page:b', err)
        self.assertNotIn('#278', err)

    # L3: the warning goes by where the file is filed, not its extension

    def test_a_tif_page_filed_as_a_document_is_not_called_a_photo(self) -> None:
        primary, _record, _sid = self._file_letter()
        page = self._put(self.inbox / 'p2.tif', TIFF)
        rc, _out, err = self._run(str(primary), '--more', str(page), 'page-2')
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertIn('No safety copies are being kept', err)
        self.assertNotIn('original photo files', err)

    # L4: --dest on a file outside everything gets the inbox advice

    def test_dest_on_a_file_outside_every_root_names_the_inbox(self) -> None:
        stray = self._put(self.tmp / 'elsewhere' / 'x.jpg', JPEG)
        rc, _out, err = self._run(str(stray), '--type', 'photo', '--dest', 'C')
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn("isn't in your inbox", err)

    # L5: a caller that cannot pass --dest is not told to

    def test_no_dest_advice_when_the_caller_cannot_pass_it(self) -> None:
        import argparse
        photo = self._put(self.inbox / 'Trip' / 'x.jpg', JPEG)
        ns = argparse.Namespace(
            file=str(photo), source_type='photo', title=None, slug=None, source_date=None,
            more=None, people=None, dry_run=False, root=str(self.archive), source_id=None,
            offer_dest=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            res = process.run_process(ns)
        self.assertTrue(res.ok)
        self.assertIn('top of your photo library', out.getvalue())
        self.assertNotIn('--dest', out.getvalue())

    # L7: "skip" at the variation prompt puts the photo back

    def test_skip_at_the_variation_prompt_puts_the_photo_back(self) -> None:
        self._put(self.photos / 'card-back.jpg', JPEG)  # an unprocessed back already filed
        front = self._put(self.inbox / 'card.jpg', JPEG)
        saved = process._prompt
        process._prompt = lambda _msg: 'skip'
        try:
            rc, out, _err = self._run(str(front))
        finally:
            process._prompt = saved
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertTrue(front.is_file(), 'the photo is back in the inbox')
        self.assertFalse((self.photos / 'card.jpg').exists())
        self.assertFalse((self.inbox / 'processed').exists())
        self.assertNotIn('is kept at', out)
        self.assertIn('back in the inbox', out)
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])


if __name__ == '__main__':
    unittest.main()
