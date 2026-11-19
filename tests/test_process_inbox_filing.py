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
        primary = next((self.docs / 'letter').glob('postcard-from-aunt-1908_s-*.pdf'))
        record = next((self.archive / 'sources' / 'letter').glob('*_s-*.md'))
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
        record = next((self.archive / 'sources' / 'photos').glob('wedding-portrait_s-*.md'))
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
        primary = next(folder.glob('hartley-1998_s-*.m4a'))
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

    def test_a_page_already_named_for_the_source_gets_its_role_once(self) -> None:
        # #356's own example: a scanner's `260828-022` with its page 2,
        # `260828-022-page-2.tif`, files as `260828-022-page-2_S`, never
        # `260828-022-page-2-page-2_S`.
        scan = self._put(self.inbox / 'Box' / '260828-022.tif', TIFF)
        rc, _out, err = self._run(str(scan), '--type', 'letter')
        self.assertEqual(rc, EXIT_CLEAN, err)
        primary = next((self.docs / 'letter').glob('260828-022_s-*.tif'))
        sid = primary.stem.split('_')[-1]
        page = self._put(self.inbox / 'Box' / '260828-022-page-2.tif', TIFF)

        rc, _out, err = self._run(str(primary), '--more', str(page), 'page-2')

        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertEqual(sorted(p.name for p in (self.docs / 'letter').iterdir()), sorted([
            f'260828-022_{sid}.tif', f'260828-022-page-2_{sid}.tif']))

    def test_a_scanner_name_on_a_named_source_takes_the_sources_slug(self) -> None:
        primary, record, sid = self._file_letter()
        page = self._put(self.inbox / 'Box' / '260828-022-page-2.tif', TIFF)

        rc, _out, err = self._run(str(primary), '--more', str(page), 'page-2')

        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertEqual(self._files_entries(record)[1]['file'],
                         f'documents/letter/postcard-from-aunt-1908-page-2_{sid}.tif')

    def test_a_main_file_with_its_own_role_lends_only_the_slug(self) -> None:
        # #356: a Find a Grave memorial's main file is `{slug}-grave-1_S`.
        # A photo added later is `{slug}-grave-3_S`, not
        # `{slug}-grave-1-grave-3_S`.
        bundle = self.inbox / 'memorial'
        bundle.mkdir()
        (bundle / 'notes.md').write_text(
            '---\ntitle: Orrin Pell memorial\nsource_type: website\nfiles:\n'
            '  - file: grave-1.jpg\n    role: grave-1\n    is_primary: true\n'
            '  - file: grave-2.jpg\n    role: grave-2\n---\nNotes.\n', encoding='utf-8')
        self._put(bundle / 'grave-1.jpg', JPEG + b' 1')
        self._put(bundle / 'grave-2.jpg', JPEG + b' 2')
        rc, _out, err = self._run(str(bundle))
        self.assertEqual(rc, EXIT_CLEAN, err)
        primary = next((self.docs / 'website').glob('orrin-pell-memorial-grave-1_s-*.jpg'))
        sid = primary.stem.split('_')[-1]
        later = self._put(self.inbox / 'grave-3.jpg', JPEG + b' 3')

        rc, _out, err = self._run(str(primary), '--more', str(later), 'grave-3')

        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertEqual(sorted(p.name for p in (self.docs / 'website').iterdir()), sorted([
            f'orrin-pell-memorial-grave-1_{sid}.jpg',
            f'orrin-pell-memorial-grave-2_{sid}.jpg',
            f'orrin-pell-memorial-grave-3_{sid}.jpg',
        ]))

    def test_a_refiled_photo_keeps_its_whole_name_for_the_next_file(self) -> None:
        # A photo titled "Store front", refiled into the documents, keeps the
        # record's bare name (`store-front_S.jpg`) with role `front`. Its back
        # is `store-front-back_S`: the word "front" is the title's, not a role
        # tail to drop.
        photo = self._put(self.inbox / 'IMG_0500.jpg', JPEG)
        rc, _out, err = self._run(str(photo), '--title', 'Store front')
        self.assertEqual(rc, EXIT_CLEAN, err)
        record = next((self.archive / 'sources' / 'photos').glob('store-front_s-*.md'))
        sid = record.stem.split('_')[-1]
        text = record.read_text(encoding='utf-8')
        self.assertIn('    is_primary: true\n', text)
        record.write_text(text.replace('    is_primary: true\n',
                                       '    role: front\n    is_primary: true\n', 1), encoding='utf-8')
        rc, _out, err = self._run('refile', sid, '--to', 'documents', '--type', 'photo', '--yes')
        self.assertEqual(rc, EXIT_CLEAN, err)
        record = next((self.archive / 'sources').rglob(f'store-front_{sid}.md'))
        main = next(e for e in self._files_entries(record) if e.get('is_primary'))
        self.assertTrue(main['file'].endswith(f'/store-front_{sid}.jpg'), main)
        self.assertEqual(main.get('role'), 'front')
        scan = self._put(self.inbox / 'scan.tif', TIFF)

        rc, _out, err = self._run(str(self.docs / main['file'].split('/', 1)[1]), '--more', str(scan), 'back')

        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertTrue(self._files_entries(record)[-1]['file'].endswith(f'/store-front-back_{sid}.tif'),
                        self._files_entries(record))

    def test_a_main_file_named_by_its_s_id_alone_lends_the_records_slug(self) -> None:
        # #356: a main file with no slug in its name (`_s-….pdf`) has none
        # to share, so the record's name is used, never the word 'source'.
        primary, record, sid = self._file_letter()
        bare = primary.with_name(f'_{sid}.pdf')
        primary.rename(bare)
        record.write_text(record.read_text(encoding='utf-8').replace(primary.name, bare.name),
                          encoding='utf-8')
        page = self._put(self.inbox / 'scan-9.tif', TIFF)

        rc, _out, err = self._run(str(bare), '--more', str(page), 'page-2')

        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertTrue((self.docs / 'letter' / f'postcard-from-aunt-1908-page-2_{sid}.tif')
                        .is_file(), sorted(p.name for p in (self.docs / 'letter').iterdir()))

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
        record = next((self.archive / 'sources' / 'letter').glob('*_s-*.md'))
        entries = self._files_entries(record)
        self.assertTrue(entries[0].get('is_primary'))
        self.assertEqual([e.get('role') for e in entries], [None, 'back'])
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
        record = next((self.archive / 'sources' / 'letter').glob('*_s-*.md'))
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
        record = next((self.archive / 'sources' / 'photos').glob('*_s-*.md'))
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
        primary = next(folder.glob('hartley-1998-audio_s-*.m4a'))
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
        record = next((self.archive / 'sources' / 'photos').glob('*_s-*.md'))
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


class InboxTriageDirsTests(InboxFilingTestCase):
    """#290: a triage folder of the inbox (`inbox_triage_dirs:`) is a holding
    stage, not part of the item's place, so it is dropped from the park path."""

    def _set_triage(self, value: str) -> None:
        cfg = self.archive / 'fha.yaml'
        cfg.write_text(cfg.read_text(encoding='utf-8') + f'inbox_triage_dirs: {value}\n',
                       encoding='utf-8')

    def test_a_listed_triage_folder_is_dropped_from_the_park_path(self) -> None:
        self._set_triage('[review, already-existing]')
        letter = self._put(self.inbox / 'review' / 'A' / 'x.txt', b'a letter')
        rc, out, err = self._run(str(letter), '--type', 'letter')
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertTrue((self.inbox / 'processed' / 'A' / 'x.txt').is_file())
        self.assertFalse((self.inbox / 'processed' / 'review').exists())
        self.assertIn('processed/A/x.txt', out.replace('\\', '/'))

    def test_case_and_a_file_straight_in_the_triage_folder(self) -> None:
        self._set_triage('[Review]')
        letter = self._put(self.inbox / 'review' / 'y.txt', b'a letter')
        rc, _out, err = self._run(str(letter), '--type', 'letter')
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertTrue((self.inbox / 'processed' / 'y.txt').is_file())

    def test_nothing_listed_keeps_the_full_path(self) -> None:
        letter = self._put(self.inbox / 'review' / 'A' / 'x.txt', b'a letter')
        rc, _out, err = self._run(str(letter), '--type', 'letter')
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertTrue((self.inbox / 'processed' / 'review' / 'A' / 'x.txt').is_file())

    def test_a_collision_at_the_shortened_park_path_refuses(self) -> None:
        self._set_triage('[review]')
        self._put(self.inbox / 'processed' / 'A' / 'x.txt', b'parked earlier')
        letter = self._put(self.inbox / 'review' / 'A' / 'x.txt', b'a letter')
        before = self._snapshot(self.tmp)
        rc, _out, err = self._run(str(letter), '--type', 'letter')
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('destination already exists', err)
        self.assertIn('processed/A/x.txt', err.replace('\\', '/'))
        self.assertEqual(self._snapshot(self.tmp), before)

    def test_more_parks_by_the_same_rule(self) -> None:
        self._set_triage('[review]')
        primary, _record, _sid = self._file_letter()
        page = self._put(self.inbox / 'review' / 'Letters' / 'p2.jpg', JPEG)
        rc, _out, err = self._run(str(primary), '--more', str(page), 'page-2')
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertTrue((self.inbox / 'processed' / 'Letters' / 'p2.jpg').is_file())
        self.assertFalse((self.inbox / 'processed' / 'review').exists())

    def test_dry_run_names_the_shortened_park_path(self) -> None:
        self._set_triage('[review]')
        letter = self._put(self.inbox / 'review' / 'A' / 'x.txt', b'a letter')
        before = self._snapshot(self.tmp)
        rc, out, err = self._run(str(letter), '--type', 'letter', '--dry-run')
        self.assertEqual(rc, EXIT_CLEAN, err)
        text = out.replace('\\', '/')
        self.assertIn('processed/A/x.txt', text)
        self.assertNotIn('processed/review', text)
        self.assertEqual(self._snapshot(self.tmp), before)

    def test_a_refusal_puts_it_back_and_leaves_no_park_folder(self) -> None:
        self._set_triage('[review]')
        box = self.inbox / 'review' / 'Box'
        front = self._put(box / 'box-001.tif', TIFF)
        self._put(box / 'box-001-back.tif', TIFF)
        self._put(box / 'box-001_back.pdf', PDF)
        rc, _out, _err = self._run(str(front), '--type', 'letter')
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertTrue(front.is_file())
        self.assertFalse((self.inbox / 'processed').exists())

    def test_a_malformed_value_warns_and_files_as_before(self) -> None:
        self._set_triage('{review: yes}')
        letter = self._put(self.inbox / 'review' / 'A' / 'x.txt', b'a letter')
        rc, _out, err = self._run(str(letter), '--type', 'letter')
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertIn('inbox_triage_dirs', err)
        self.assertNotIn('Traceback', err)
        self.assertTrue((self.inbox / 'processed' / 'review' / 'A' / 'x.txt').is_file())

    def test_an_unusable_entry_is_dropped_with_a_warning(self) -> None:
        self._set_triage('[review, ../outside, processed]')
        letter = self._put(self.inbox / 'review' / 'A' / 'x.txt', b'a letter')
        rc, _out, err = self._run(str(letter), '--type', 'letter')
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertIn('inbox_triage_dirs', err)
        self.assertTrue((self.inbox / 'processed' / 'A' / 'x.txt').is_file())

    def test_doctor_names_a_malformed_value(self) -> None:
        import doctor
        from _lib import load_fha_yaml
        self._set_triage('7')
        result = doctor.run_doctor(self.archive, load_fha_yaml(self.archive))
        check = next((c for c in result.data['checks'] if c['id'] == 'inbox_triage_dirs'), None)
        self.assertIsNotNone(check)
        self.assertEqual(check['status'], 'warn')
        self.assertIn('inbox_triage_dirs', '\n'.join(result.data['lines']))


class SecondReviewTests(InboxFilingTestCase):
    """PR #287 second review: naming edge cases, clearer refusals, and the
    places `--dest` could still file a photo where it does not belong."""

    def _set_yaml(self, text: str) -> None:
        cfg = self.archive / 'fha.yaml'
        cfg.write_text(cfg.read_text(encoding='utf-8') + text, encoding='utf-8')

    def test_copy_letter_already_in_stem_is_not_doubled(self) -> None:
        primary, _record, sid = self._file_letter()
        back = self._put(self.inbox / 'Letters' / 'postcard-from-aunt-1908-c-back.tif', TIFF)
        rc, _out, err = self._run(str(primary), '--more', str(back), 'back:c')
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertTrue(
            (self.docs / 'letter' / f'postcard-from-aunt-1908-c-back_{sid}.tif').is_file(),
            sorted(p.name for p in (self.docs / 'letter').iterdir()))

    def test_audio_session_keeps_topic_for_every_recording(self) -> None:
        folder = self.docs / 'interviews'
        audio = self._put(folder / 'h98-audio.m4a', b'audio')
        rc, _out, err = self._run(str(audio), '--type', 'interview', '--slug', 'h98-audio')
        self.assertEqual(rc, EXIT_CLEAN, err)
        primary = next(folder.glob('h98-audio_s-*.m4a'))
        sid = primary.stem.split('_')[-1]
        for name, role in (('h98-farm.m4a', 'audio'), ('h98-school.m4a', 'audio'),
                           ('h98-farm.md', 'whisper-transcript'),
                           ('h98-school-whisper-transcript.md', 'whisper-transcript')):
            more = self._put(folder / name, b'x ' + name.encode())
            rc, _out, err = self._run(str(primary), '--more', str(more), role)
            self.assertEqual(rc, EXIT_CLEAN, f'{name}: {err}')
        names = sorted(p.name for p in folder.iterdir())
        self.assertEqual(names, sorted([
            f'h98-audio_{sid}.m4a',
            f'h98-farm-audio_{sid}.m4a',
            f'h98-school-audio_{sid}.m4a',
            f'h98-farm-whisper-transcript_{sid}.md',
            f'h98-school-whisper-transcript_{sid}.md',
        ]))

    def test_dest_example_drops_triage_folder(self) -> None:
        self._set_yaml('inbox_triage_dirs: [review]\n')
        photo = self._put(self.inbox / 'review' / 'C' / 'p.jpg', JPEG)
        rc, out, err = self._run(str(photo), '--type', 'photo')
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertIn('--dest C)', out)
        self.assertNotIn('review/C', out.replace('processed/', ''))
        # The inbox-folder refusal suggests the same folder.
        self._put(self.inbox / 'review' / 'D' / 'q.jpg', JPEG)
        rc, _out, err = self._run(str(self.inbox / 'review' / 'D'))
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('--dest D`', err)

    def test_park_collision_message_names_a_fix(self) -> None:
        self._put(self.inbox / 'processed' / 'A' / 'x.txt', b'kept earlier')
        letter = self._put(self.inbox / 'A' / 'x.txt', b'a letter')
        before = self._snapshot(self.tmp)
        rc, _out, err = self._run(str(letter), '--type', 'letter')
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('already kept there', err)
        self.assertIn('rename', err)
        self.assertIn('Nothing was moved', err)
        self.assertEqual(self._snapshot(self.tmp), before)

    def test_more_photo_name_taken_in_primary_folder_suggests_copy_letter(self) -> None:
        front = self._put(self.inbox / 'T' / 'x.jpg', JPEG)
        rc, _out, err = self._run(str(front), '--type', 'photo', '--dest', 'C')
        self.assertEqual(rc, EXIT_CLEAN, err)
        other = self._put(self.inbox / 'U' / 'x.jpg', JPEG + b'2')
        rc, _out, err = self._run(str(self.photos / 'C' / 'x.jpg'), '--more', str(other), 'crop')
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('--more "inbox/U/x.jpg" crop:b', err)
        self.assertIn('rename it in the inbox', err)
        self.assertTrue(other.is_file())

    def test_dest_into_photos_ignore_folder_is_refused(self) -> None:
        self._set_yaml("photos_ignore:\n  - 'Flickr Export'\n")
        photo = self._put(self.inbox / 'x.jpg', JPEG)
        before = self._snapshot(self.tmp)
        rc, _out, err = self._run(str(photo), '--type', 'photo', '--dest', 'Flickr Export/2019')
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('photos_ignore', err)
        self.assertIn('Flickr Export', err)
        self.assertEqual(self._snapshot(self.tmp), before)

    def test_text_file_in_photo_library_attached_to_photo_source_refuses_plainly(self) -> None:
        front = self._put(self.inbox / 'card.jpg', JPEG)
        rc, _out, err = self._run(str(front))
        self.assertEqual(rc, EXIT_CLEAN, err)
        record = next((self.archive / 'sources' / 'photos').glob('*_s-*.md'))
        for name in ('notes.md', 'notes.txt', 'scan.pdf'):
            with self.subTest(name=name):
                stray = self._put(self.photos / name, b'%PDF-1.4' if name.endswith('.pdf') else b'text')
                rc, _out, err = self._run(str(self.photos / 'card.jpg'), '--more', str(stray),
                                          'transcript')
                self.assertEqual(rc, EXIT_ERRORS, err)
                self.assertIn('inbox', err)
                self.assertIn('Move it into the inbox', err)
                self.assertNotIn('exiftool', err.lower())
                self.assertTrue(stray.is_file())
                self.assertEqual(len(self._files_entries(record)), 1)

    def test_doctor_inbox_age_reads_the_configured_inbox(self) -> None:
        import os
        import time
        import doctor
        from _lib import load_fha_yaml
        self._set_yaml('inbox_triage_dirs: [review]\n')
        old = self._put(self.inbox / 'review' / 'stale-letter.pdf', PDF)
        (self.inbox / 'review').mkdir(exist_ok=True)
        when = time.time() - 30 * 86400
        os.utime(old, (when, when))
        result = doctor.run_doctor(self.archive, load_fha_yaml(self.archive))
        check = next(c for c in result.data['checks'] if c['id'] == 'inbox')
        self.assertEqual(check['status'], 'warn')
        self.assertIn('stale-letter.pdf', '\n'.join(result.data['lines']))


class IntakeDefaultsTests(InboxFilingTestCase):
    """#294: `intake_defaults:` gives an inbox folder a standing source type,
    so "every postcard is a record" is applied without remembering --type."""

    RULE = ('intake_defaults:\n'
            '  - match: "Postcards/**"\n'
            '    source_type: letter\n')

    def _set_yaml(self, text: str) -> None:
        cfg = self.archive / 'fha.yaml'
        cfg.write_text(cfg.read_text(encoding='utf-8') + text, encoding='utf-8')

    def test_a_matching_inbox_file_is_filed_as_the_rules_type(self) -> None:
        self._set_yaml(self.RULE)
        card = self._put(self.inbox / 'Postcards' / 'card-01.tif', TIFF)
        before = self._snapshot(self.tmp)
        rc, out, err = self._run(str(card), '--dry-run')
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertIn("type letter from intake_defaults 'Postcards/**'", out)
        self.assertEqual(self._snapshot(self.tmp), before)

        rc, out, err = self._run(str(card))
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertIn("type letter from intake_defaults 'Postcards/**'", out)
        self.assertEqual(self._files(self.photos), [])
        filed = list((self.docs / 'letter').glob('card-01_s-*.tif'))
        self.assertEqual(len(filed), 1)
        record = next((self.archive / 'sources' / 'letter').glob('*_s-*.md'))
        self.assertEqual(read_record(record)['meta']['source_type'], 'letter')

    def test_an_explicit_type_beats_the_default(self) -> None:
        self._set_yaml(self.RULE)
        card = self._put(self.inbox / 'Postcards' / 'card-02.tif', TIFF)
        rc, out, err = self._run(str(card), '--type', 'photo')
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertEqual(self._files(self.photos), ['card-02.tif'])
        self.assertNotIn('intake_defaults', out)

    def test_no_rules_keeps_todays_behaviour(self) -> None:
        card = self._put(self.inbox / 'Postcards' / 'card-03.tif', TIFF)
        rc, _out, err = self._run(str(card))
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertEqual(self._files(self.photos), ['card-03.tif'])

    def test_a_stub_hint_beats_the_default(self) -> None:
        self._set_yaml(self.RULE)
        card = self._put(self.inbox / 'Postcards' / 'card-04.tif', TIFF)
        (self.inbox / 'Postcards' / 'card-04.notes.md').write_text(
            '---\nsource_type: census\n---\n', encoding='utf-8')
        rc, _out, err = self._run(str(card))
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertEqual(len(list((self.docs / 'census').glob('card-04_s-*.tif'))), 1)

    def test_a_triage_folder_is_left_out_before_matching(self) -> None:
        self._set_yaml(self.RULE + 'inbox_triage_dirs: [review]\n')
        card = self._put(self.inbox / 'review' / 'Postcards' / 'card-05.tif', TIFF)
        rc, out, err = self._run(str(card))
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertEqual(len(list((self.docs / 'letter').glob('card-05_s-*.tif'))), 1)

    def test_more_attachment_takes_the_default(self) -> None:
        self._set_yaml(self.RULE)
        front = self._put(self.inbox / 'card.jpg', JPEG)
        rc, _out, err = self._run(str(front))
        self.assertEqual(rc, EXIT_CLEAN, err)
        back = self._put(self.inbox / 'Postcards' / 'card-back-scan.tif', TIFF)
        rc, out, err = self._run(str(self.photos / 'card.jpg'), '--more', str(back), 'back')
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertIn("type letter from intake_defaults 'Postcards/**'", out)
        self.assertNotIn('card-back-scan.tif', self._files(self.photos))
        self.assertEqual(len(list(self.docs.rglob('*-back_s-*.tif'))), 1)

    def test_a_bundle_takes_the_default(self) -> None:
        self._set_yaml(self.RULE)
        folder = self.inbox / 'Postcards' / 'aunt-1908'
        self._put(folder / 'front.tif', TIFF)
        (folder / 'notes.md').write_text('---\ntitle: Card from aunt\n---\n', encoding='utf-8')
        rc, out, err = self._run(str(folder))
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertIn("type letter from intake_defaults 'Postcards/**'", out)
        self.assertEqual(self._files(self.photos), [])
        self.assertEqual(len(list((self.archive / 'sources' / 'letter').glob('*_s-*.md'))), 1)

    def test_an_inbox_folder_of_records_is_not_told_type_photo(self) -> None:
        self._set_yaml(self.RULE)
        self._put(self.inbox / 'Postcards' / 'card-06.tif', TIFF)
        rc, _out, err = self._run(str(self.inbox / 'Postcards'))
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('inbox/Postcards/card-06.tif', err)
        self.assertNotIn('--type photo', err)
        self.assertIn('intake_defaults', err)

    def test_the_workbench_verb_takes_the_default(self) -> None:
        import argparse
        self._set_yaml(self.RULE)
        card = self._put(self.inbox / 'Postcards' / 'card-07.tif', TIFF)
        ns = argparse.Namespace(
            file=str(card), source_type=None, title=None, slug=None, source_date=None,
            more=None, people=None, dry_run=False, root=str(self.archive), source_id=None,
            offer_dest=False)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            res = process.run_process(ns)
        self.assertTrue(res.ok)
        self.assertEqual(len(list((self.docs / 'letter').glob('card-07_s-*.tif'))), 1)

    def test_a_malformed_rule_warns_and_files_as_before(self) -> None:
        self._set_yaml('intake_defaults:\n'
                       '  - match: "Postcards/**"\n'
                       '    source_type: postcrd\n'
                       '  - just a string\n')
        card = self._put(self.inbox / 'Postcards' / 'card-08.tif', TIFF)
        rc, _out, err = self._run(str(card))
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertIn('intake_defaults', err)
        self.assertIn('postcrd', err)
        self.assertNotIn('Traceback', err)
        self.assertEqual(self._files(self.photos), ['card-08.tif'])

    def test_doctor_names_a_malformed_rule(self) -> None:
        import doctor
        from _lib import load_fha_yaml
        self._set_yaml('intake_defaults: 7\n')
        result = doctor.run_doctor(self.archive, load_fha_yaml(self.archive))
        check = next((c for c in result.data['checks'] if c['id'] == 'intake_defaults'), None)
        self.assertIsNotNone(check)
        self.assertEqual(check['status'], 'warn')


class NestedInboxTests(unittest.TestCase):
    """PR #287 third review, P1: SPEC §12.4's own example puts the inbox
    INSIDE the photos root (`C:/Photos/_inbox`). A file in that inbox is not
    "already in the photo library": the rules, --type and #274 all apply."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.archive = self.tmp / 'archive'
        for sub in ('sources', 'people', 'notes'):
            (self.archive / sub).mkdir(parents=True)
        self.photos = self.tmp / 'lib-photos'
        self.docs = self.tmp / 'lib-docs'
        self.inbox = self.photos / '_inbox'
        for d in (self.photos, self.docs, self.inbox):
            d.mkdir(parents=True, exist_ok=True)
        (self.archive / 'fha.yaml').write_text(
            f'roots:\n  photos: {self.photos.as_posix()}\n'
            f'  documents: {self.docs.as_posix()}\n  inbox: {self.inbox.as_posix()}\n'
            'intake_defaults:\n  - match: "Postcards/**"\n    source_type: letter\n',
            encoding='utf-8')
        self.store = FakePhotoStore()
        self._saved = (process._run_exiftool_read_keywords,
                       process._run_exiftool_embed_source,
                       process._run_exiftool_remove_source)
        process._run_exiftool_read_keywords = self.store.read
        process._run_exiftool_embed_source = self.store.embed
        process._run_exiftool_remove_source = self.store.remove

    def tearDown(self) -> None:
        (process._run_exiftool_read_keywords, process._run_exiftool_embed_source,
         process._run_exiftool_remove_source) = self._saved
        self._tmp.cleanup()

    def _run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = process._standalone_main(list(argv) + ['--root', str(self.archive)])
        return rc, out.getvalue(), err.getvalue()

    def _library_outside_inbox(self) -> list[str]:
        return sorted(p.relative_to(self.photos).as_posix() for p in self.photos.rglob('*')
                      if p.is_file() and self.inbox not in p.parents)

    def test_inbox_nested_in_photos_root_honours_rule_and_type(self) -> None:
        card = self.inbox / 'Postcards' / 'card-01.tif'
        card.parent.mkdir(parents=True)
        card.write_bytes(TIFF)
        rc, out, err = self._run(str(card))
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertIn("type letter from intake_defaults", out)
        self.assertEqual(len(list((self.docs / 'letter').glob('card-01_s-*.tif'))), 1)

        note = self.inbox / 'note.txt'
        note.write_bytes(b'a typed note')
        rc, _out, err = self._run(str(note), '--type', 'letter')
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertNotIn('exiftool', err.lower())
        self.assertEqual(len(list((self.docs / 'letter').glob('note_s-*.txt'))), 1)
        self.assertEqual(self._library_outside_inbox(), [])

    def test_dest_inside_nested_inbox_is_refused(self) -> None:
        # Fourth review P2-A: with the inbox inside the photos root, --dest
        # must not name the inbox (or its processed/ folder) as a library folder.
        for dest in ('_inbox', '_inbox/processed', '_inbox/Postcards'):
            with self.subTest(first_filing=dest):
                photo = self.inbox / 'x.jpg'
                photo.write_bytes(JPEG)
                rc, _out, err = self._run(str(photo), '--type', 'photo', '--dest', dest)
                self.assertEqual(rc, EXIT_ERRORS, err)
                self.assertIn('your inbox, not part of your photo library', err)
                self.assertTrue(photo.is_file())
                self.assertEqual(self._library_outside_inbox(), [])
        scan = self.inbox / 'scan.jpg'
        scan.write_bytes(JPEG)
        rc, _out, err = self._run(str(scan), '--type', 'letter')
        self.assertEqual(rc, EXIT_CLEAN, err)
        sid = next((self.docs / 'letter').glob('scan_s-*.jpg')).stem.split('_')[-1]
        for dest in ('_inbox', '_inbox/processed'):
            with self.subTest(refile=dest):
                out, err_io = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err_io):
                    rc = process._standalone_main(
                        ['refile', sid, '--to', 'photos', '--dest', dest, '--yes',
                         '--root', str(self.archive)])
                self.assertNotEqual(rc, EXIT_CLEAN)
                self.assertIn('your inbox, not part of your photo library',
                              out.getvalue() + err_io.getvalue())
                self.assertEqual(len(list((self.docs / 'letter').glob('scan_s-*.jpg'))), 1)

    def test_parked_original_under_processed_is_refused_nested(self) -> None:
        kept = self.inbox / 'processed' / 'A' / 'x.txt'
        kept.parent.mkdir(parents=True)
        kept.write_bytes(b'kept')
        rc, _out, err = self._run(str(kept), '--type', 'letter')
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('kept original of something already filed', err)
        self.assertTrue(kept.is_file())

    def test_more_page_from_a_nested_inbox_joins_its_document(self) -> None:
        letter = self.inbox / 'letter.pdf'
        letter.write_bytes(PDF)
        rc, _out, err = self._run(str(letter), '--type', 'letter')
        self.assertEqual(rc, EXIT_CLEAN, err)
        primary = next((self.docs / 'letter').glob('letter_s-*.pdf'))
        page = self.inbox / 'p2.tif'
        page.write_bytes(TIFF)
        rc, _out, err = self._run(str(primary), '--more', str(page), 'page-2')
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertEqual(self._library_outside_inbox(), [])
        self.assertEqual(len(list((self.docs / 'letter').glob('letter-page-2_s-*.tif'))), 1)


class ThirdReviewTests(InboxFilingTestCase):
    """PR #287 third review: intake rule patterns, photo rules, bundles,
    session suffixes, and plain refusals."""

    def _set_yaml(self, text: str) -> None:
        cfg = self.archive / 'fha.yaml'
        cfg.write_text(cfg.read_text(encoding='utf-8') + text, encoding='utf-8')

    def _rule(self, pattern: str, stype: str = 'letter') -> None:
        self._set_yaml(f'intake_defaults:\n  - match: "{pattern}"\n    source_type: {stype}\n')

    def test_folder_pattern_forms_all_match(self) -> None:
        for i, pattern in enumerate(('Postcards', 'Postcards/', 'Postcards/**', 'Postcards/*')):
            with self.subTest(pattern=pattern):
                cfg = self.archive / 'fha.yaml'
                base = cfg.read_text(encoding='utf-8').split('intake_defaults:')[0]
                cfg.write_text(base, encoding='utf-8')
                self._rule(pattern)
                card = self._put(self.inbox / 'Postcards' / '1908' / f'card-{i}.tif', TIFF)
                rc, out, err = self._run(str(card))
                self.assertEqual(rc, EXIT_CLEAN, err)
                self.assertIn('intake_defaults', out)
                self.assertEqual(len(list((self.docs / 'letter').glob(f'card-{i}_s-*.tif'))), 1)

    def test_nested_double_star_matches_below_the_top_only(self) -> None:
        self._rule('**/Postcards/**')
        deep = self._put(self.inbox / 'Box' / 'Postcards' / 'a.tif', TIFF)
        top = self._put(self.inbox / 'Postcards' / 'b.tif', TIFF)
        rc, _out, err = self._run(str(deep))
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertEqual(len(list((self.docs / 'letter').glob('a_s-*.tif'))), 1)
        rc, _out, err = self._run(str(top))
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertIn('b.tif', self._files(self.photos))  # documented: top-level not matched

    def test_unusable_patterns_are_warned_about(self) -> None:
        for pattern in ('inbox/Postcards/**', '/abs/Postcards', 'C:/Postcards', '../Postcards/**'):
            with self.subTest(pattern=pattern):
                cfg = self.archive / 'fha.yaml'
                base = cfg.read_text(encoding='utf-8').split('intake_defaults:')[0]
                cfg.write_text(base, encoding='utf-8')
                self._rule(pattern)
                card = self._put(self.inbox / f'x{len(pattern)}.txt', b'x')
                rc, _out, err = self._run(str(card), '--dry-run')
                self.assertEqual(rc, EXIT_CLEAN, err)
                self.assertIn('intake_defaults', err)
                self.assertIn(repr(pattern), err)
                import doctor
                from _lib import load_fha_yaml
                result = doctor.run_doctor(self.archive, load_fha_yaml(self.archive))
                check = next((c for c in result.data['checks'] if c['id'] == 'intake_defaults'),
                             None)
                self.assertIsNotNone(check)

    def test_bundle_asset_matching_a_file_rule_is_not_reclassified_silently(self) -> None:
        self._rule('*.tif')
        folder = self.inbox / 'Reunion'
        self._put(folder / 'front.tif', TIFF)
        (folder / 'notes.md').write_text('---\ntitle: Reunion\n---\n', encoding='utf-8')
        rc, out, err = self._run(str(folder))
        self.assertEqual(rc, EXIT_CLEAN, err)
        if 'intake_defaults' in out:
            # Applied at bundle level, said, and the record agrees.
            record = next((self.archive / 'sources').rglob('*_s-*.md'))
            self.assertEqual(read_record(record)['meta']['source_type'], 'letter')
        else:
            self.assertEqual(self._files(self.photos), ['front.tif'])
            record = next((self.archive / 'sources').rglob('*_s-*.md'))
            self.assertEqual(read_record(record)['meta']['source_type'], 'photo')

    def test_bundle_intake_line_names_the_folder(self) -> None:
        self._rule('Postcards/**')
        folder = self.inbox / 'Postcards' / 'aunt-1908'
        self._put(folder / 'front.tif', TIFF)
        (folder / 'notes.md').write_text('---\ntitle: Card\n---\n', encoding='utf-8')
        rc, out, err = self._run(str(folder))
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertIn("aunt-1908: type letter from intake_defaults", out)
        self.assertNotIn('notes.md: type', out)

    def test_photo_rule_does_not_send_more_page_to_photo_library(self) -> None:
        self._rule('Letters/**', 'photo')
        primary, record, _sid = self._file_letter()
        page = self._put(self.inbox / 'Letters' / 'p2.tif', TIFF)
        rc, _out, err = self._run(str(primary), '--more', str(page), 'page-2')
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertEqual(self._files(self.photos), [])
        self.assertTrue(self._files_entries(record)[1]['file'].startswith('documents/'))
        self.assertIn('intake_defaults', err)  # the photo rule is warned about

    def test_photo_rule_does_not_stamp_photo_on_a_pdf(self) -> None:
        self._rule('Scans/**', 'photo')
        pdf = self._put(self.inbox / 'Scans' / 'deed.pdf', PDF)
        rc, _out, err = self._run(str(pdf))
        self.assertEqual(rc, EXIT_CLEAN, err)
        record = next((self.archive / 'sources').rglob('*_s-*.md'))
        self.assertNotEqual(read_record(record)['meta']['source_type'], 'photo')

    def test_video_and_recording_sessions_keep_topics(self) -> None:
        for suffix, ext in (('-video', 'mp4'), ('-recording', 'wav')):
            with self.subTest(suffix=suffix):
                folder = self.docs / 'interviews' / suffix.strip('-')
                audio = self._put(folder / f's9{suffix}.{ext}', b'media')
                rc, _out, err = self._run(str(audio), '--type', 'interview',
                                          '--slug', f's9{suffix}')
                self.assertEqual(rc, EXIT_CLEAN, err)
                primary = next(folder.glob(f's9{suffix}_s-*.{ext}'))
                sid = primary.stem.split('_')[-1]
                for name, role in ((f's9-farm.{ext}', suffix.strip('-')),
                                   (f's9-school.{ext}', suffix.strip('-')),
                                   ('s9-farm.md', 'whisper-transcript')):
                    more = self._put(folder / name, b'x ' + name.encode())
                    rc, _out, err = self._run(str(primary), '--more', str(more), role)
                    self.assertEqual(rc, EXIT_CLEAN, f'{name}: {err}')
                names = sorted(p.name for p in folder.iterdir())
                self.assertIn(f's9-farm{suffix}_{sid}.{ext}', names)
                self.assertIn(f's9-school{suffix}_{sid}.{ext}', names)
                self.assertIn(f's9-farm-whisper-transcript_{sid}.md', names)

    def test_session_suffixes_agree_with_the_transcribe_script(self) -> None:
        import re
        from _lib import SESSION_MEDIA_SUFFIXES
        script = (ROOT / '.claude' / 'skills' / 'transcribe-audio' / 'scripts'
                  / 'transcribe_audio.py').read_text(encoding='utf-8')
        m = re.search(r'^_MEDIA_ROLE_SUFFIXES = (\(.*?\))$', script, re.M)
        self.assertIsNotNone(m)
        import ast
        self.assertEqual(tuple(ast.literal_eval(m.group(1))), tuple(SESSION_MEDIA_SUFFIXES))

    def test_non_image_primary_in_photo_library_refuses_plainly(self) -> None:
        note = self._put(self.photos / 'Family' / 'notes.txt', b'text')
        for dry in (True, False):
            with self.subTest(dry_run=dry):
                argv = [str(note)] + (['--dry-run'] if dry else [])
                rc, out, err = self._run(*argv)
                self.assertEqual(rc, EXIT_ERRORS, out + err)
                self.assertIn('Move it into the inbox', err)
                self.assertNotIn('exiftool', err.lower())
                self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])

    def test_an_unlisted_picture_format_in_the_library_is_still_a_photo(self) -> None:
        # Only formats that are certainly not pictures are refused; a .webp
        # the library holds is still attached as a photo.
        front = self._put(self.inbox / 'card.jpg', JPEG)
        rc, _out, err = self._run(str(front))
        self.assertEqual(rc, EXIT_CLEAN, err)
        webp = self._put(self.photos / 'card-reverse.webp', b'RIFF')
        rc, _out, err = self._run(str(self.photos / 'card.jpg'), '--more', str(webp), 'back')
        self.assertEqual(rc, EXIT_CLEAN, err)
        self.assertEqual(len(self.store.read(webp)), 1)

    def test_dest_refusal_names_the_rule(self) -> None:
        self._rule('Postcards/**')
        card = self._put(self.inbox / 'Postcards' / 'c.tif', TIFF)
        rc, _out, err = self._run(str(card), '--dest', 'C')
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn("intake_defaults 'Postcards/**'", err)
        self.assertIn('--type photo --dest C', err)

    def test_doctor_inbox_age_skips_bookkeeping_entries(self) -> None:
        import os
        import time
        import doctor
        from _lib import load_fha_yaml
        when = time.time() - 30 * 86400
        for rel in ('processed/old.txt', '.DS_Store', '_TEMPLATE.notes.md'):
            f = self._put(self.inbox / rel, b'x')
            os.utime(f, (when, when))
        os.utime(self.inbox / 'processed', (when, when))
        result = doctor.run_doctor(self.archive, load_fha_yaml(self.archive))
        check = next(c for c in result.data['checks'] if c['id'] == 'inbox')
        self.assertEqual(check['status'], 'ok', '\n'.join(result.data['lines']))


class FourthReviewTests(InboxFilingTestCase):
    """PR #287 fourth review: kept originals, bundle --dest wording, and the
    not-a-picture list."""

    def test_parked_original_under_processed_is_refused(self) -> None:
        kept = self._put(self.inbox / 'processed' / 'A' / 'x.txt', b'kept')
        before = self._snapshot(self.tmp)
        rc, _out, err = self._run(str(kept), '--type', 'letter')
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('kept original of something already filed', err)
        self.assertEqual(self._snapshot(self.tmp), before)
        # As a --more file too.
        primary, _record, _sid = self._file_letter()
        page = self._put(self.inbox / 'processed' / 'p.jpg', JPEG)
        rc, _out, err = self._run(str(primary), '--more', str(page), 'page-2')
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('kept original of something already filed', err)
        self.assertTrue(page.is_file())

    def test_bundle_at_the_rules_own_folder_matches(self) -> None:
        # Codex on PR #287: a bundle sitting directly at `inbox/Postcards/` is
        # matched as its notes.md would be (`Postcards/notes.md`), so
        # `Postcards/**` covers it; a bundle one folder down still matches.
        cfg = self.archive / 'fha.yaml'
        cfg.write_text(cfg.read_text(encoding='utf-8')
                       + 'intake_defaults:\n  - match: "Postcards/**"\n    source_type: letter\n',
                       encoding='utf-8')
        for folder in (self.inbox / 'Postcards' / 'b1', self.inbox / 'Postcards'):
            with self.subTest(folder=folder.name):
                self._put(folder / f'front-{folder.name}.jpg', JPEG)
                (folder / 'notes.md').write_text('---\ntitle: Card\n---\n', encoding='utf-8')
                rc, out, err = self._run(str(folder))
                self.assertEqual(rc, EXIT_CLEAN, err)
                self.assertIn(f"{folder.name}: type letter from intake_defaults 'Postcards/**'", out)
                self.assertEqual(self._files(self.photos), [])
                self.assertEqual(len(list((self.docs / 'letter').glob(f'*_s-*.jpg'))),
                                 1 if folder.name == 'b1' else 2)

    def test_dest_whose_files_photos_ignore_skips_is_refused(self) -> None:
        # Codex on PR #287: `Phone Backups/*` skips the files inside the folder,
        # so `--dest "Phone Backups"` would file a photo the catalog never sees.
        cfg = self.archive / 'fha.yaml'
        cfg.write_text(cfg.read_text(encoding='utf-8') + "photos_ignore:\n  - 'Phone Backups/*'\n",
                       encoding='utf-8')
        photo = self._put(self.inbox / 'card.jpg', JPEG)
        folder = self.inbox / 'Reunion'
        self._put(folder / 'group.jpg', JPEG)
        (folder / 'notes.md').write_text('---\ntitle: Reunion\n---\n', encoding='utf-8')
        before = self._snapshot(self.tmp)
        for target in (photo, folder):
            with self.subTest(target=target.name):
                rc, _out, err = self._run(str(target), '--dest', 'Phone Backups')
                self.assertEqual(rc, EXIT_ERRORS, err)
                self.assertIn("photos_ignore: 'Phone Backups/*'", err)
                self.assertEqual(self._snapshot(self.tmp), before)

    def test_folder_refusal_keeps_the_requested_type(self) -> None:
        folder = self.inbox / 'Scans'
        self._put(folder / 'sheet-1.jpg', JPEG)
        rc, _out, err = self._run(str(folder), '--type', 'census')
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('--type census', err)
        self.assertNotIn('--type photo', err)
        # A stub's type hint is not overridden either.
        (folder / 'sheet-1.notes.md').write_text('---\nsource_type: letter\n---\n',
                                                 encoding='utf-8')
        rc, _out, err = self._run(str(folder))
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertNotIn('--type photo', err)

    def test_bundle_dest_refusal_names_the_rule(self) -> None:
        cfg = self.archive / 'fha.yaml'
        cfg.write_text(cfg.read_text(encoding='utf-8')
                       + 'intake_defaults:\n  - match: "Postcards/**"\n    source_type: letter\n',
                       encoding='utf-8')
        folder = self.inbox / 'Postcards' / 'aunt'
        self._put(folder / 'front.tif', TIFF)
        (folder / 'notes.md').write_text('---\ntitle: Card\n---\n', encoding='utf-8')
        rc, _out, err = self._run(str(folder), '--dest', 'C')
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn("type letter from intake_defaults 'Postcards/**'", err)
        self.assertIn('--type photo --dest C', err)

    def test_more_non_picture_formats(self) -> None:
        for suffix in ('.ini', '.db', '.aae', '.zip', '.svg', '.md', '.pdf'):
            self.assertTrue(process._is_known_non_image(Path('x' + suffix)), suffix)
        for suffix in ('.jpg', '.webp', '.mp4'):
            self.assertFalse(process._is_known_non_image(Path('x' + suffix)), suffix)


if __name__ == '__main__':
    unittest.main()
