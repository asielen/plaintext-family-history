"""Tests for `fha process` Stage A (BUILD.md M7.1 documents, M7.2 photos + --more).

The photo paths never call exiftool: the read/embed/remove seams are replaced
here by an in-memory `FakePhotoStore` so the keyword-embed / already-processed
refusal / rollback / --more logic runs without the binary.

Run: python -m unittest tests.test_process -v   (from the repo root)
"""

import argparse
import os
import contextlib
import io
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import packet
import photoindex
import process
from _lib import (
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    grouping_stem,
    load_fha_yaml,
    parse_media_filename,
    read_record,
    select_variation_primary,
)


def _make_archive(tmp: Path) -> Path:
    """A minimal archive root: fha.yaml mapping internal photos/ and documents/."""
    archive = tmp / 'archive'
    (archive / 'documents' / 'census').mkdir(parents=True)
    (archive / 'photos' / '1880').mkdir(parents=True)
    (archive / 'sources').mkdir()
    (archive / 'people').mkdir()
    (archive / 'notes').mkdir()
    (archive / 'fha.yaml').write_text(
        'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8'
    )
    return archive


class FakePhotoStore:
    """In-memory stand-in for embedded photo keywords, keyed by absolute path.

    Patched over process's two exiftool seams so a write here is visible to a
    later read - exactly what the real `SOURCE:` keyword round-trip provides.
    """

    def __init__(self) -> None:
        self.keywords: dict[str, list[str]] = {}
        self.fail_paths: set[str] = set()
        # `read_fail_paths` simulates exiftool RUNNING but failing on this one
        # file (a nonzero exit, or invalid JSON on stdout) - it IS present and
        # runnable, it just could not read this particular file.
        # `unavailable_paths` simulates exiftool itself being absent from the
        # machine entirely - a caller may soft-fail on this (and only this)
        # without soft-failing on an ordinary per-file read failure (#163 P1
        # audit finding on `fha process refile`).
        self.read_fail_paths: set[str] = set()
        self.unavailable_paths: set[str] = set()

    def read(self, file_path: Path) -> list[str]:
        if str(file_path) in self.unavailable_paths:
            raise process.ExiftoolUnavailableError('simulated: exiftool not on PATH')
        if str(file_path) in self.read_fail_paths:
            raise RuntimeError('simulated exiftool read failure')
        return list(self.keywords.get(str(file_path), []))

    def embed(
        self, file_path: Path, s_id: str, extra_keywords: list[str] | None = None,
        *, backup=None,
    ) -> str | None:
        key = str(file_path)
        if key in self.fail_paths:
            return 'simulated exiftool failure'
        kws = self.keywords.setdefault(key, [])
        kws.append(f'SOURCE: {s_id}')
        for kw in extra_keywords or []:
            kws.append(kw)
        return None

    def remove(
        self, file_path: Path, s_id: str, extra_keywords: list[str] | None = None,
        *, backup=None,
    ) -> str | None:
        key = str(file_path)
        to_remove = {f'SOURCE: {s_id}'} | set(extra_keywords or [])
        self.keywords[key] = [kw for kw in self.keywords.get(key, []) if kw not in to_remove]
        return None


class ProcessTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.archive = _make_archive(self.tmp)
        # Photo seams off by default; tests that need them install the fake.
        self._orig_read = process._run_exiftool_read_keywords
        self._orig_embed = process._run_exiftool_embed_source
        self._orig_remove = process._run_exiftool_remove_source
        self._orig_read_meta = process._run_exiftool_read_meta
        self._orig_prompt = process._prompt
        # Folder triage reads caption/date signals per file; default to "no
        # metadata" so scoring is deterministic and never shells out in tests.
        process._run_exiftool_read_meta = lambda _p: {
            'caption': None, 'user_comment': None, 'edtf': None, 'has_pid_keyword': False,
        }

    def tearDown(self) -> None:
        process._run_exiftool_read_keywords = self._orig_read
        process._run_exiftool_embed_source = self._orig_embed
        process._run_exiftool_remove_source = self._orig_remove
        process._run_exiftool_read_meta = self._orig_read_meta
        process._prompt = self._orig_prompt
        self._tmp.cleanup()

    def test_cli_unknown_source_type_teaches_valid_vocabulary(self) -> None:
        asset = self.archive / 'documents' / 'census' / 'loose.pdf'
        asset.write_bytes(b'%PDF-1.4')
        args = type('Args', (), {
            'root': str(self.archive),
            'file': str(asset),
            'source_type': 'bogus',
            'title': None,
            'slug': None,
            'more': None,
            'dry_run': True,
        })()

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = process._run_process(args)

        self.assertEqual(rc, EXIT_ERRORS)
        text = err.getvalue()
        self.assertIn('source category', text)
        self.assertIn('census', text)
        self.assertIn('photo', text)
        self.assertNotIn('Traceback', text)

    def test_sidecar_source_date_error_shows_archive_date_examples(self) -> None:
        asset = self.archive / 'documents' / 'census' / 'loose.pdf'
        asset.write_bytes(b'%PDF-1.4')
        sidecar = asset.with_name('loose.notes.md')
        sidecar.write_text('---\nsource_date: last summer\n---\n', encoding='utf-8')

        with self.assertRaises(process.ProcessError) as ctx:
            process.process_document(
                self.archive, load_fha_yaml(self.archive, strict=True), asset,
                source_type='census', slug=None, title=None, source_date=None,
                dry_run=True,
            )

        text = str(ctx.exception)
        self.assertIn('date the archive can read', text)
        self.assertIn('1880-06-15', text)

    def test_cli_invalid_date_teaches_archive_date_examples(self) -> None:
        asset = self.archive / 'documents' / 'census' / 'loose.pdf'
        asset.write_bytes(b'%PDF-1.4')
        args = type('Args', (), {
            'root': str(self.archive),
            'file': str(asset),
            'source_type': 'census',
            'source_date': 'last summer',
            'title': None,
            'slug': None,
            'more': None,
            'dry_run': True,
        })()

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = process._run_process(args)

        self.assertEqual(rc, EXIT_ERRORS)
        text = err.getvalue()
        self.assertIn('date the archive can read', text)
        self.assertIn('1880-06-15', text)
        self.assertNotIn('Traceback', text)

    def test_cli_loose_date_is_normalized_into_scaffold(self) -> None:
        asset = self.archive / 'documents' / 'census' / 'loose-date.txt'
        asset.write_text('x', encoding='utf-8')

        rc = self._run([str(asset), '--date', 'about 1880'])
        self.assertEqual(rc, EXIT_CLEAN)

        record = next((self.archive / 'sources' / 'other').glob('*_S-*.md'))
        self.assertEqual(read_record(record)['meta']['source_date'], '1880~')

    def _install_photo_store(self) -> FakePhotoStore:
        store = FakePhotoStore()
        process._run_exiftool_read_keywords = store.read
        process._run_exiftool_embed_source = store.embed
        process._run_exiftool_remove_source = store.remove
        return store

    def _install_prompt(self, *answers: str) -> None:
        """Queue scripted answers for the interactive prompt seam."""
        queue = list(answers)
        process._prompt = lambda _msg: queue.pop(0) if queue else ''

    def _run(self, argv: list[str]) -> int:
        return process._standalone_main(argv + ['--root', str(self.archive)])

    def _snapshot_tree(self, root: Path) -> dict[str, tuple[int, int]]:
        """Map every path under root to (size, mtime_ns); directories to a marker.

        Comparing two snapshots proves a dry-run performed ZERO filesystem
        mutations: nothing created, deleted, renamed, resized, or rewritten
        (a rewrite with identical bytes still bumps mtime_ns).
        """
        snap: dict[str, tuple[int, int]] = {}
        for p in sorted(root.rglob('*')):
            rel = p.relative_to(root).as_posix()
            if p.is_file():
                st = p.stat()
                snap[rel] = (st.st_size, st.st_mtime_ns)
            else:
                snap[rel] = (-1, -1)
        return snap

    def _write_temp_record(self, text: str) -> Path:
        path = self.tmp / 'scratch_record.md'
        path.write_text(text, encoding='utf-8')
        return path

    def _write_person(self, pid: str, stem: str = 'hartley__person') -> Path:
        path = self.archive / 'people' / f'{stem}_{pid}.md'
        path.write_text(
            f'---\nid: {pid}\nname: Test Person\nliving: unknown\n---\n',
            encoding='utf-8',
        )
        return path

    # ── M7.1 documents ────────────────────────────────────────────────────────

    def test_document_mints_renames_scaffolds(self) -> None:
        original = self.archive / 'documents' / 'census' / '1880-fairview.txt'
        original.write_text('census content', encoding='utf-8')

        rc = self._run([str(original), '--type', 'census', '--title', 'Fairview 1880'])
        self.assertEqual(rc, EXIT_CLEAN)

        # Original is renamed in place (gone under old name, present with _S-id).
        self.assertFalse(original.exists())
        renamed = list((self.archive / 'documents' / 'census').glob('*_S-*.txt'))
        self.assertEqual(len(renamed), 1)

        records = list((self.archive / 'sources' / 'census').glob('*_S-*.md'))
        self.assertEqual(len(records), 1)
        rec = read_record(records[0])
        self.assertEqual(rec['meta']['source_type'], 'census')
        self.assertEqual(rec['meta']['title'], 'Fairview 1880')
        self.assertEqual(rec['claims'], [])  # empty ## Claims block parses to []
        files = rec['meta']['files']
        self.assertEqual(files[0]['role'], 'primary')
        self.assertEqual(files[0]['original_filename'], '1880-fairview.txt')
        self.assertTrue(files[0]['file'].startswith('documents/census/'))
        self.assertNotIn('is_primary', files[0])  # documents are not is_primary

    def test_document_special_char_title_stays_parseable(self) -> None:
        # A title carrying YAML-significant characters (`: `, leading `-`, ` #`)
        # must still scaffold a record that read_record can load round-trip.
        original = self.archive / 'documents' / 'census' / 'note.txt'
        original.write_text('x', encoding='utf-8')
        tricky = 'Letter: Home #2 - draft'

        rc = self._run([str(original), '--type', 'census', '--title', tricky])
        self.assertEqual(rc, EXIT_CLEAN)

        record = next((self.archive / 'sources' / 'census').glob('*_S-*.md'))
        rec = read_record(record)  # would raise / mis-parse without quoting
        self.assertEqual(rec['meta']['title'], tricky)

    def test_document_dry_run_writes_nothing(self) -> None:
        original = self.archive / 'documents' / 'census' / 'deed.txt'
        original.write_text('x', encoding='utf-8')

        rc = self._run([str(original), '--dry-run'])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertTrue(original.exists())  # not renamed
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])

    def test_document_refuses_already_processed(self) -> None:
        processed = self.archive / 'documents' / 'census' / 'deed_S-0123456789.txt'
        processed.write_text('x', encoding='utf-8')
        rc = self._run([str(processed)])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertTrue(processed.exists())

    def test_document_sidecar_consumed_into_notes(self) -> None:
        asset = self.archive / 'documents' / 'census' / 'letter.txt'
        asset.write_text('letter body', encoding='utf-8')
        sidecar = self.archive / 'documents' / 'census' / 'letter.notes.md'
        sidecar.write_text(
            '---\ntitle: A Letter Home\nsource_type: letter\n---\n'
            'Grandma sent this in 1918; mentions the move to Kansas.\n',
            encoding='utf-8',
        )

        rc = self._run([str(asset)])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(sidecar.exists())  # stub deleted after success

        records = list((self.archive / 'sources' / 'letter').glob('*_S-*.md'))
        self.assertEqual(len(records), 1)  # source_type hint routed it to letter/
        rec = read_record(records[0])
        self.assertEqual(rec['meta']['title'], 'A Letter Home')
        self.assertIn('Grandma sent this', records[0].read_text(encoding='utf-8'))

    def test_document_sidecar_as_input_processes_companion_asset(self) -> None:
        asset = self.archive / 'documents' / 'census' / 'diary.txt'
        asset.write_text('diary body', encoding='utf-8')
        sidecar = self.archive / 'documents' / 'census' / 'diary.notes.md'
        sidecar.write_text('---\ntitle: Diary Page\n---\nA note from the stub.\n', encoding='utf-8')

        rc = self._run([str(sidecar)])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(sidecar.exists())
        self.assertFalse(asset.exists())
        self.assertEqual(list((self.archive / 'documents' / 'census').glob('diary.notes*_S-*.md')), [])
        renamed = list((self.archive / 'documents' / 'census').glob('diary-page_S-*.txt'))
        self.assertEqual(len(renamed), 1)

    def test_document_asset_relocated_out_of_inbox(self) -> None:
        # A bare file dropped straight in inbox/ (no sidecar) - process should
        # file it into documents/ before scaffolding, not refuse it.
        (self.archive / 'inbox').mkdir()
        asset = self.archive / 'inbox' / 'note.txt'
        asset.write_text('inbox body', encoding='utf-8')

        rc = self._run([str(asset)])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(asset.exists())
        self.assertEqual(list((self.archive / 'inbox').iterdir()), [])
        # Owner decision 2026-07-22: an inbox single files into documents/{type}/
        # (default 'other'), never flat at the documents root top level.
        renamed = list((self.archive / 'documents' / 'other').glob('note_S-*.txt'))
        self.assertEqual(len(renamed), 1)

    def test_root_top_document_files_into_type_subfolder(self) -> None:
        # A hand-drop at the documents root TOP level files into
        # documents/{type}/ at processing (owner decision 2026-07-22) - the
        # counterpart of the in-place rename a pre-filed subfolder file gets
        # (test_document_sidecar_as_input_processes_companion_asset above).
        asset = self.archive / 'documents' / 'deed.txt'
        asset.write_text('deed body', encoding='utf-8')

        rc = self._run([str(asset), '--type', 'census'])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(asset.exists())
        renamed = list((self.archive / 'documents' / 'census').glob('deed_S-*.txt'))
        self.assertEqual(len(renamed), 1)
        # The record's inventory alias carries the subfolder path.
        record = next((self.archive / 'sources' / 'census').glob('deed_S-*.md'))
        self.assertIn('documents/census/deed_S-', record.read_text(encoding='utf-8'))

    def test_sidecar_and_companion_relocated_out_of_inbox(self) -> None:
        # The `fha capture --asset` case: a stub + its companion both staged in
        # inbox/. Processing the sidecar should move both into documents/ then
        # scaffold normally, same as if they'd been hand-filed there.
        (self.archive / 'inbox').mkdir()
        asset = self.archive / 'inbox' / 'clipping.txt'
        asset.write_text('clipping body', encoding='utf-8')
        sidecar = self.archive / 'inbox' / 'clipping.notes.md'
        sidecar.write_text('---\ntitle: Newspaper Clipping\n---\nFound online.\n', encoding='utf-8')

        rc = self._run([str(sidecar)])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(list((self.archive / 'inbox').iterdir()), [])
        renamed = list((self.archive / 'documents' / 'other').glob('newspaper-clipping_S-*.txt'))
        self.assertEqual(len(renamed), 1)
        records = list((self.archive / 'sources').rglob('*_S-*.md'))
        self.assertEqual(len(records), 1)

    def test_inbox_relocation_honors_sidecar_source_type_hint(self) -> None:
        # A record image (.jpg) with a sidecar hinting source_type: census must
        # be filed as a document, not misclassified as a photo by extension.
        (self.archive / 'inbox').mkdir()
        asset = self.archive / 'inbox' / 'census.jpg'
        asset.write_text('record image bytes', encoding='utf-8')
        sidecar = self.archive / 'inbox' / 'census.notes.md'
        sidecar.write_text('---\nsource_type: census\n---\nFound online.\n', encoding='utf-8')

        rc = self._run([str(sidecar)])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(list((self.archive / 'inbox').iterdir()), [])
        self.assertEqual(list((self.archive / 'photos').glob('census*')), [])
        renamed = list((self.archive / 'documents' / 'census').glob('census_S-*.jpg'))
        self.assertEqual(len(renamed), 1)
        records = list((self.archive / 'sources' / 'census').glob('*_S-*.md'))
        self.assertEqual(len(records), 1)

    def test_inbox_relocation_rolled_back_on_downstream_refusal(self) -> None:
        # A dna-typed asset relocated out of inbox/ into documents/ (flat) still
        # fails process_document's documents/dna/ requirement; the move itself
        # must be undone, not leave the asset filed outside the inbox.
        (self.archive / 'inbox').mkdir()
        asset = self.archive / 'inbox' / 'kit.txt'
        asset.write_text('kit body', encoding='utf-8')

        rc = self._run([str(asset), '--type', 'dna'])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertTrue(asset.exists())
        self.assertEqual(list((self.archive / 'documents').glob('kit*')), [])
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])

    def test_photo_relocated_out_of_inbox(self) -> None:
        store = self._install_photo_store()
        (self.archive / 'inbox').mkdir()
        photo = self.archive / 'inbox' / 'scan.jpg'
        photo.write_text('photo bytes', encoding='utf-8')

        rc = self._run([str(photo)])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(photo.exists())
        moved = self.archive / 'photos' / 'scan.jpg'
        self.assertTrue(moved.exists())
        self.assertIn(str(moved), store.keywords)

    def test_inbox_relocation_dry_run_writes_nothing(self) -> None:
        (self.archive / 'inbox').mkdir()
        asset = self.archive / 'inbox' / 'note.txt'
        asset.write_text('inbox body', encoding='utf-8')

        rc = self._run([str(asset), '--dry-run'])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertTrue(asset.exists())
        self.assertEqual(list((self.archive / 'documents').glob('*.txt')), [])

    def test_inbox_relocation_refuses_destination_conflict(self) -> None:
        (self.archive / 'inbox').mkdir()
        asset = self.archive / 'inbox' / 'note.txt'
        asset.write_text('inbox body', encoding='utf-8')
        (self.archive / 'documents' / 'note.txt').write_text('already here', encoding='utf-8')

        rc = self._run([str(asset)])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertTrue(asset.exists())

    def test_inbox_photo_dry_run_previews_single_photo_plan(self) -> None:
        # P1 regression: the dry-run relocation returns a destination that does
        # not exist yet; variation grouping must not come back empty and crash
        # (min() over an empty set). The preview must state the full live plan:
        # the move, the minted S-id, the SOURCE keyword embed, the scaffold.
        store = self._install_photo_store()
        (self.archive / 'inbox').mkdir()
        photo = self.archive / 'inbox' / 'scan.jpg'
        photo.write_bytes(b'\xff\xd8\xff')

        before = self._snapshot_tree(self.archive)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self._run([str(photo), '--dry-run'])

        self.assertEqual(rc, EXIT_CLEAN)
        text = out.getvalue()
        self.assertIn('[dry-run] Would move scan.jpg out of inbox/ into photos/', text)
        self.assertIn('[dry-run] Would mint S-', text)
        self.assertIn('Would embed SOURCE: S-', text)
        self.assertIn('in scan.jpg (no rename)', text)
        self.assertIn('Would scaffold sources/photos/scan_S-', text)
        self.assertEqual(self._snapshot_tree(self.archive), before)  # zero writes
        self.assertEqual(store.keywords, {})  # no embed attempted anywhere

    def test_inbox_photo_dry_run_reads_sidecar_hints_from_inbox(self) -> None:
        # The stub sits beside the asset in inbox/, not beside the virtual
        # destination; the preview must still pick up its hints (title -> slug)
        # and state the stub consumption, exactly as the live run will.
        self._install_photo_store()
        (self.archive / 'inbox').mkdir()
        photo = self.archive / 'inbox' / 'scan.jpg'
        photo.write_bytes(b'\xff\xd8\xff')
        sidecar = self.archive / 'inbox' / 'scan.notes.md'
        sidecar.write_text('---\ntitle: Reunion Portrait\n---\nBack says 1912.\n',
                           encoding='utf-8')

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self._run([str(photo), '--dry-run'])

        self.assertEqual(rc, EXIT_CLEAN)
        text = out.getvalue()
        self.assertIn('Would scaffold sources/photos/reunion-portrait_S-', text)
        self.assertIn('Would delete stub scan.notes.md', text)
        self.assertTrue(sidecar.exists())  # preview only - stub untouched

    def test_inbox_photo_dry_run_refuses_already_processed(self) -> None:
        # Keyword reads must target the real inbox file: an already-tagged
        # photo is refused on preview exactly as live would refuse it. Before
        # the fix the read hit the virtual destination, saw no keywords, and
        # previewed a success the live run would never deliver.
        store = self._install_photo_store()
        (self.archive / 'inbox').mkdir()
        photo = self.archive / 'inbox' / 'tagged.jpg'
        photo.write_bytes(b'\xff\xd8\xff')
        store.keywords[str(photo)] = ['SOURCE: S-aaaaaaaaaa']

        rc = self._run([str(photo), '--dry-run'])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertTrue(photo.exists())  # nothing moved

    def test_inbox_document_dry_run_previews_sidecar_hints_and_matches_live(self) -> None:
        # P2 regression: a typed stub (source_type: census) beside an inbox
        # asset. The preview must name the same sources/census/ record path and
        # slug the live run creates and state that the stub gets consumed -
        # before the fix it previewed sources/other/ under the raw filename and
        # hid the stub deletion.
        (self.archive / 'inbox').mkdir()
        asset = self.archive / 'inbox' / 'record.jpg'
        asset.write_text('record image bytes', encoding='utf-8')
        sidecar = self.archive / 'inbox' / 'record.notes.md'
        sidecar.write_text(
            '---\nsource_type: census\ntitle: Fairview Census Page\n---\nFound online.\n',
            encoding='utf-8',
        )

        before = self._snapshot_tree(self.archive)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self._run([str(asset), '--dry-run'])
        self.assertEqual(rc, EXIT_CLEAN)
        preview = out.getvalue()
        self.assertRegex(
            preview,
            r'Would rename record\.jpg -> documents[\/]census[\/]fairview-census-page_S-')
        self.assertIn('Would scaffold sources/census/fairview-census-page_S-', preview)
        self.assertIn('Would delete stub record.notes.md', preview)
        self.assertEqual(self._snapshot_tree(self.archive), before)  # zero writes

        # The live run then does exactly what the preview said (S-id aside).
        rc = self._run([str(asset)])
        self.assertEqual(rc, EXIT_CLEAN)
        records = list((self.archive / 'sources' / 'census').glob('fairview-census-page_S-*.md'))
        self.assertEqual(len(records), 1)
        renamed = list((self.archive / 'documents' / 'census').glob('fairview-census-page_S-*.jpg'))
        self.assertEqual(len(renamed), 1)
        self.assertFalse(sidecar.exists())  # stub consumed, as previewed

    def test_inbox_photo_dry_run_groups_with_photos_root_sibling(self) -> None:
        # An inbox photo whose variation sibling already sits in the photos
        # root: live moves the file in and then groups the pair, so the preview
        # must describe that same 2-file group - and crash on neither the
        # virtual member nor an empty set.
        store = self._install_photo_store()
        (self.archive / 'inbox').mkdir()
        incoming = self.archive / 'inbox' / 'portrait_1880.jpg'
        incoming.write_bytes(b'\xff\xd8\xff')
        sibling = self.archive / 'photos' / 'portrait_1880-back.jpg'
        sibling.write_bytes(b'\xff\xd8\xff')
        self._install_prompt('one')

        before = self._snapshot_tree(self.archive)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self._run([str(incoming), '--dry-run'])
        self.assertEqual(rc, EXIT_CLEAN)
        text = out.getvalue()
        self.assertIn('Found 2 files that appear to be variations', text)
        self.assertIn('for a 2-file variation set', text)
        self.assertIn('in portrait_1880.jpg (primary, no rename)', text)
        self.assertIn('in portrait_1880-back.jpg (back, no rename)', text)
        self.assertEqual(self._snapshot_tree(self.archive), before)  # zero writes
        self.assertEqual(store.keywords, {})  # no embed on preview

        # The live run forms exactly the previewed group: one shared S-id over
        # the moved-in file and the pre-existing sibling.
        self._install_prompt('one')
        rc = self._run([str(incoming)])
        self.assertEqual(rc, EXIT_CLEAN)
        moved = self.archive / 'photos' / 'portrait_1880.jpg'
        self.assertTrue(moved.exists())
        self.assertEqual(store.read(moved), store.read(sibling))
        records = list((self.archive / 'sources' / 'photos').glob('*_S-*.md'))
        self.assertEqual(len(records), 1)
        self.assertEqual(len(read_record(records[0])['meta']['files']), 2)

    def test_document_rollback_on_record_write_failure(self) -> None:
        original = self.archive / 'documents' / 'census' / 'will.txt'
        original.write_text('will body', encoding='utf-8')
        # Make sources/other a *file* so mkdir(parents=True) fails after the
        # rename, forcing the rollback path.
        (self.archive / 'sources' / 'other').write_text('blocker', encoding='utf-8')

        rc = self._run([str(original)])  # default type 'other'
        self.assertEqual(rc, EXIT_FAILURE)
        # Original restored to its pre-run name; nothing left half-done.
        self.assertTrue(original.exists())
        self.assertEqual(list((self.archive / 'documents' / 'census').glob('*_S-*.txt')), [])

    def test_document_refuses_unknown_source_type(self) -> None:
        original = self.archive / 'documents' / 'census' / 'mystery.txt'
        original.write_text('x', encoding='utf-8')
        rc = self._run([str(original), '--type', 'not-a-type'])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertTrue(original.exists())
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])

    def test_document_refuses_destination_conflict(self) -> None:
        original = self.archive / 'documents' / 'census' / 'deed.txt'
        original.write_text('x', encoding='utf-8')
        # Force the minted ID to collide at the destination filename only.
        existing_id = 'S-aaaaaaaaaa'
        (self.archive / 'documents' / 'census' / f'deed_{existing_id}.txt').write_text(
            'already here', encoding='utf-8'
        )
        process.mint_ids = lambda *a, **k: [existing_id]
        try:
            rc = self._run([str(original)])
        finally:
            from _lib import mint_ids as real_mint_ids
            process.mint_ids = real_mint_ids
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertTrue(original.exists())

    # ── M7.2 photos ───────────────────────────────────────────────────────────

    def test_photo_embeds_keyword_no_rename(self) -> None:
        store = self._install_photo_store()
        photo = self.archive / 'photos' / '1880' / 'portrait.jpg'
        photo.write_bytes(b'\xff\xd8\xff')

        rc = self._run([str(photo), '--title', 'Portrait 1880'])
        self.assertEqual(rc, EXIT_CLEAN)

        self.assertTrue(photo.exists())  # NEVER renamed
        kws = store.read(photo)
        self.assertEqual(len(kws), 1)
        self.assertTrue(kws[0].startswith('SOURCE: S-'))

        records = list((self.archive / 'sources' / 'photos').glob('*_S-*.md'))
        self.assertEqual(len(records), 1)
        rec = read_record(records[0])
        self.assertEqual(rec['meta']['source_type'], 'photo')
        self.assertEqual(rec['meta']['files'][0]['role'], 'primary')
        self.assertEqual(rec['meta']['files'][0]['is_primary'], 'true')
        self.assertTrue(rec['meta']['files'][0]['file'].startswith('photos/1880/'))

    def test_photo_refuses_when_source_keyword_present(self) -> None:
        store = self._install_photo_store()
        photo = self.archive / 'photos' / '1880' / 'tagged.jpg'
        photo.write_bytes(b'\xff\xd8\xff')
        store.keywords[str(photo)] = ['SOURCE: S-aaaaaaaaaa']

        rc = self._run([str(photo)])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])

    def test_photo_dry_run_no_embed_no_record(self) -> None:
        store = self._install_photo_store()
        photo = self.archive / 'photos' / '1880' / 'preview.jpg'
        photo.write_bytes(b'\xff\xd8\xff')

        rc = self._run([str(photo), '--dry-run'])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(store.read(photo), [])  # no embed
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])

    def test_photo_embed_failure_aborts_before_scaffold(self) -> None:
        store = self._install_photo_store()
        photo = self.archive / 'photos' / '1880' / 'locked.jpg'
        photo.write_bytes(b'\xff\xd8\xff')
        store.fail_paths.add(str(photo))

        rc = self._run([str(photo)])
        self.assertEqual(rc, EXIT_FAILURE)
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])

    def test_photo_sidecar_consumed_into_notes(self) -> None:
        store = self._install_photo_store()
        photo = self.archive / 'photos' / '1880' / 'portrait-with-notes.jpg'
        photo.write_bytes(b'\xff\xd8\xff')
        sidecar = self.archive / 'photos' / '1880' / 'portrait-with-notes.notes.md'
        sidecar.write_text(
            '---\ntitle: Portrait With Notes\n---\nBack says taken in Denver.\n',
            encoding='utf-8',
        )

        rc = self._run([str(sidecar)])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(sidecar.exists())
        self.assertTrue(photo.exists())
        self.assertEqual(len(store.read(photo)), 1)
        record = next((self.archive / 'sources' / 'photos').glob('*_S-*.md'))
        self.assertIn('Back says taken in Denver', record.read_text(encoding='utf-8'))

    def test_photo_record_write_failure_rolls_back_keyword(self) -> None:
        store = self._install_photo_store()
        photo = self.archive / 'photos' / '1880' / 'blocked.jpg'
        photo.write_bytes(b'\xff\xd8\xff')
        (self.archive / 'sources' / 'photos').write_text('blocker', encoding='utf-8')

        rc = self._run([str(photo)])
        self.assertEqual(rc, EXIT_FAILURE)
        self.assertEqual(store.read(photo), [])
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])

    def test_more_attaches_back_to_existing_photo_source(self) -> None:
        store = self._install_photo_store()
        front = self.archive / 'photos' / '1880' / 'card.jpg'
        front.write_bytes(b'\xff\xd8\xff')
        self.assertEqual(self._run([str(front)]), EXIT_CLEAN)
        record = next((self.archive / 'sources' / 'photos').glob('*_S-*.md'))

        back = self.archive / 'photos' / '1880' / 'card-back.jpg'
        back.write_bytes(b'\xff\xd8\xff')
        rc = self._run([str(front), '--more', str(back), 'back'])
        self.assertEqual(rc, EXIT_CLEAN)

        self.assertTrue(back.exists())  # not renamed
        kws = store.read(back)
        self.assertEqual(len(kws), 1)
        # Both files share the one S-id.
        self.assertEqual(store.read(front)[0], kws[0])
        rec = read_record(record)
        files = rec['meta']['files']
        self.assertEqual(len(files), 2)
        self.assertEqual(files[1]['role'], 'back')
        self.assertTrue(files[1]['file'].endswith('card-back.jpg'))

    def test_more_photo_rolls_back_keyword_when_record_is_malformed(self) -> None:
        store = self._install_photo_store()
        front = self.archive / 'photos' / '1880' / 'card2.jpg'
        front.write_bytes(b'\xff\xd8\xff')
        self.assertEqual(self._run([str(front)]), EXIT_CLEAN)
        record = next((self.archive / 'sources' / 'photos').glob('*_S-*.md'))
        original_record_text = record.read_text(encoding='utf-8')
        record.write_text('no frontmatter here\n', encoding='utf-8')

        back = self.archive / 'photos' / '1880' / 'card2-back.jpg'
        back.write_bytes(b'\xff\xd8\xff')
        rc = self._run([str(front), '--more', str(back), 'back'])
        self.assertEqual(rc, EXIT_FAILURE)
        self.assertEqual(store.read(back), [])
        self.assertEqual(record.read_text(encoding='utf-8'), 'no frontmatter here\n')
        record.write_text(original_record_text, encoding='utf-8')

    def test_photo_refuses_file_outside_photos_root(self) -> None:
        store = self._install_photo_store()
        outside = self.archive / 'stray.jpg'
        outside.write_bytes(b'\xff\xd8\xff')
        rc = self._run([str(outside)])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertEqual(store.read(outside), [])
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])

    def test_more_refuses_photo_attachment_outside_photos_root(self) -> None:
        store = self._install_photo_store()
        front = self.archive / 'photos' / '1880' / 'cardx.jpg'
        front.write_bytes(b'\xff\xd8\xff')
        self.assertEqual(self._run([str(front)]), EXIT_CLEAN)

        outside = self.archive / 'stray-back.jpg'
        outside.write_bytes(b'\xff\xd8\xff')
        rc = self._run([str(front), '--more', str(outside), 'back'])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertEqual(store.read(outside), [])

    def test_more_dry_run_survives_exiftool_read_failure(self) -> None:
        store = self._install_photo_store()
        front = self.archive / 'photos' / '1880' / 'preview-front.jpg'
        front.write_bytes(b'\xff\xd8\xff')
        self.assertEqual(self._run([str(front)]), EXIT_CLEAN)

        back = self.archive / 'photos' / '1880' / 'preview-back.jpg'
        back.write_bytes(b'\xff\xd8\xff')

        def _boom(_path: Path) -> list[str]:
            raise RuntimeError('exiftool not on PATH')

        process._run_exiftool_read_keywords = _boom
        rc = self._run([str(front), '--more', str(back), 'back', '--dry-run'])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(store.read(back), [])  # no keyword embedded

    def test_more_refuses_document_attachment_outside_documents_root(self) -> None:
        page1 = self.archive / 'documents' / 'census' / 'pagex1.txt'
        page1.write_text('p1', encoding='utf-8')
        self.assertEqual(self._run([str(page1)]), EXIT_CLEAN)
        renamed1 = next((self.archive / 'documents' / 'census').glob('*_S-*.txt'))

        outside = self.archive / 'stray-page2.txt'
        outside.write_text('p2', encoding='utf-8')
        rc = self._run([str(renamed1), '--more', str(outside), 'page-2'])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertTrue(outside.exists())  # not renamed

    def test_dna_source_marked_restricted_and_requires_dna_root(self) -> None:
        outside = self.archive / 'documents' / 'census' / 'kit.txt'
        outside.write_text('x', encoding='utf-8')
        rc = self._run([str(outside), '--type', 'dna'])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertTrue(outside.exists())

        (self.archive / 'documents' / 'dna').mkdir(parents=True)
        kit = self.archive / 'documents' / 'dna' / 'kit.txt'
        kit.write_text('x', encoding='utf-8')
        rc = self._run([str(kit), '--type', 'dna'])
        self.assertEqual(rc, EXIT_CLEAN)
        record = next((self.archive / 'sources' / 'dna').glob('*_S-*.md'))
        rec = read_record(record)
        self.assertEqual(rec['meta']['restricted'], 'true')

    def test_sidecar_hint_fields_preserved_into_scaffold(self) -> None:
        asset = self.archive / 'documents' / 'census' / 'hinted.txt'
        asset.write_text('x', encoding='utf-8')
        sidecar = self.archive / 'documents' / 'census' / 'hinted.notes.md'
        sidecar.write_text(
            '---\n'
            'title: Hinted Source\n'
            'citation: A custom citation string.\n'
            'repository: county archive\n'
            'source_date: around 1900\n'
            'provenance: found in a shoebox\n'
            '---\n'
            'body notes\n',
            encoding='utf-8',
        )
        rc = self._run([str(asset)])
        self.assertEqual(rc, EXIT_CLEAN)
        record = next((self.archive / 'sources' / 'other').glob('*_S-*.md'))
        rec = read_record(record)
        self.assertEqual(rec['meta']['repository'], 'county archive')
        self.assertEqual(rec['meta']['source_date'], '1900~')
        self.assertEqual(rec['meta']['provenance'], 'found in a shoebox')
        self.assertIn('A custom citation string.', record.read_text(encoding='utf-8'))

    def test_sidecar_unknown_source_type_hint_refused(self) -> None:
        asset = self.archive / 'documents' / 'census' / 'mistyped.txt'
        asset.write_text('x', encoding='utf-8')
        sidecar = self.archive / 'documents' / 'census' / 'mistyped.notes.md'
        sidecar.write_text(
            '---\nsource_type: cnsus\n---\nbody notes\n', encoding='utf-8',
        )
        rc = self._run([str(asset)])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertTrue(sidecar.exists())  # not consumed on failure
        self.assertTrue(asset.exists())  # not renamed
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])

    def test_proof_argument_routed_to_proofs_dir_with_authored_class(self) -> None:
        argument = self.archive / 'documents' / 'census' / 'birth-order.txt'
        argument.write_text('x', encoding='utf-8')
        rc = self._run([str(argument), '--type', 'proof-argument'])
        self.assertEqual(rc, EXIT_CLEAN)
        record = next((self.archive / 'sources' / 'proofs').glob('*_S-*.md'))
        rec = read_record(record)
        self.assertEqual(rec['meta']['source_class'], 'authored')

    def test_path_to_alias_normalizes_dotdot_external_root(self) -> None:
        # A relative external root like 'documents: ../family-docs' resolves to a
        # path containing '..' segments; path_to_alias must resolve it before
        # comparing against an already-resolved file path, or every file under
        # it falls back to a non-portable absolute alias.
        external_root = self.tmp / 'family-docs'
        external_root.mkdir()
        cfg = {'roots': {'documents': '../family-docs'}}
        doc = external_root / 'sub' / 'deed.pdf'
        doc.parent.mkdir(parents=True)
        doc.write_text('x', encoding='utf-8')
        alias = process.path_to_alias(doc.resolve(), 'documents', cfg, self.archive)
        self.assertEqual(alias, 'documents/sub/deed.pdf')

    def test_more_document_record_read_failure_is_a_tool_failure(self) -> None:
        page1 = self.archive / 'documents' / 'census' / 'unreadable1.txt'
        page1.write_text('p1', encoding='utf-8')
        self.assertEqual(self._run([str(page1), '--type', 'census']), EXIT_CLEAN)
        renamed1 = next((self.archive / 'documents' / 'census').glob('*_S-*.txt'))
        record = next((self.archive / 'sources' / 'census').glob('*_S-*.md'))
        record.unlink()
        record.mkdir()  # read_text() on a directory raises IsADirectoryError (OSError)

        page2 = self.archive / 'documents' / 'census' / 'unreadable2.txt'
        page2.write_text('p2', encoding='utf-8')
        rc = self._run([str(renamed1), '--more', str(page2), 'page-2'])
        self.assertEqual(rc, EXIT_FAILURE)
        self.assertTrue(page2.exists())  # not renamed

    def test_append_file_entry_handles_inline_files_value(self) -> None:
        record_text = '---\nid: S-aaaaaaaaaa\nfiles: []\ncreated: 2026-01-01\n---\n\nbody\n'
        entry = ['  - file: documents/census/page2.txt', '    role: page-2']
        new_text = process._append_file_entry(record_text, entry)
        parsed = read_record(self._write_temp_record(new_text))
        self.assertEqual(parsed['parse_errors'], [])
        self.assertEqual(len(parsed['meta']['files']), 1)
        self.assertEqual(parsed['meta']['files'][0]['role'], 'page-2')

    def test_append_file_entry_preserves_nonempty_inline_files_value(self) -> None:
        record_text = (
            '---\nid: S-aaaaaaaaaa\n'
            'files: [{file: documents/census/page1.pdf, role: primary}]\n'
            'created: 2026-01-01\n---\n\nbody\n'
        )
        entry = ['  - file: documents/census/page2.txt', '    role: page-2']
        new_text = process._append_file_entry(record_text, entry)
        parsed = read_record(self._write_temp_record(new_text))
        self.assertEqual(parsed['parse_errors'], [])
        files = parsed['meta']['files']
        self.assertEqual(len(files), 2)
        self.assertEqual(files[0]['file'], 'documents/census/page1.pdf')
        self.assertEqual(files[0]['role'], 'primary')
        self.assertEqual(files[1]['role'], 'page-2')

    def test_sidecar_multiline_citation_indented_correctly(self) -> None:
        asset = self.archive / 'documents' / 'census' / 'multiline.txt'
        asset.write_text('x', encoding='utf-8')
        sidecar = self.archive / 'documents' / 'census' / 'multiline.notes.md'
        sidecar.write_text(
            '---\n'
            'citation: |\n'
            '  Line one of the citation.\n'
            '  Line two of the citation.\n'
            '---\n'
            'body notes\n',
            encoding='utf-8',
        )
        rc = self._run([str(asset)])
        self.assertEqual(rc, EXIT_CLEAN)
        record = next((self.archive / 'sources' / 'other').glob('*_S-*.md'))
        rec = read_record(record)
        self.assertEqual(rec['parse_errors'], [])
        self.assertIn('Line one of the citation.', rec['meta']['citation'])
        self.assertIn('Line two of the citation.', rec['meta']['citation'])

    def test_more_document_renames_with_shared_sid(self) -> None:
        # Process a document, then attach a second document page to it.
        page1 = self.archive / 'documents' / 'census' / 'page1.txt'
        page1.write_text('p1', encoding='utf-8')
        self.assertEqual(self._run([str(page1), '--type', 'census']), EXIT_CLEAN)
        renamed1 = next((self.archive / 'documents' / 'census').glob('*_S-*.txt'))
        sid = renamed1.stem.split('_')[-1]

        page2 = self.archive / 'documents' / 'census' / 'page2.txt'
        page2.write_text('p2', encoding='utf-8')
        rc = self._run([str(renamed1), '--more', str(page2), 'page-2'])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(page2.exists())
        attached = list((self.archive / 'documents' / 'census').glob(f'*page-2*_{sid}.txt'))
        self.assertEqual(len(attached), 1)

    def test_more_root_top_attachment_files_beside_its_primary(self) -> None:
        # M11.2 symmetry: an attachment sitting at the documents root TOP
        # level files WITH its source - beside the primary, in whatever
        # subfolder the human chose for it - never left flat at the root.
        page1 = self.archive / 'documents' / 'census' / 'page1.txt'
        page1.write_text('p1', encoding='utf-8')
        self.assertEqual(self._run([str(page1), '--type', 'census']), EXIT_CLEAN)
        renamed1 = next((self.archive / 'documents' / 'census').glob('*_S-*.txt'))
        sid = renamed1.stem.split('_')[-1]

        loose = self.archive / 'documents' / 'loose-page.txt'
        loose.write_text('p2', encoding='utf-8')
        rc = self._run([str(renamed1), '--more', str(loose), 'page-2'])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(loose.exists())
        attached = list((self.archive / 'documents' / 'census').glob(f'*page-2*_{sid}.txt'))
        self.assertEqual(len(attached), 1)
        # nothing left flat at the root top
        self.assertEqual([p for p in (self.archive / 'documents').iterdir()
                          if p.is_file()], [])

    # ── #113: a print/copy letter is never read as "back" ────────────────────

    def test_bare_copy_letter_is_never_read_as_back(self) -> None:
        # The reported bug's exact shape: a trailing letter directly after a
        # sequence number ('item-04-01400b') names a SEPARATE print of the
        # same negative - never the back of a print. Only an explicit
        # -back/_back suffix means that.
        letter = parse_media_filename('item-04-01400b')
        self.assertNotEqual(letter.part_kind, 'back')
        self.assertEqual(letter.variant_id, 'b')
        self.assertEqual(letter.base_id, 'item-04-01400')

        explicit_back = parse_media_filename('item-04-01400-back')
        self.assertEqual(explicit_back.part_kind, 'back')
        self.assertEqual(explicit_back.base_id, 'item-04-01400')

    def test_photo_group_assigns_primary_back_and_copy_roles(self) -> None:
        # #113's own fixture: a plain primary, its explicit -back, and a
        # bare-letter second print of the same negative, processed together.
        # Confirms the role-assignment half of #113 end-to-end (already
        # correct in _lib's grammar - this locks it against regression).
        self._install_photo_store()
        self._install_prompt('one')
        primary = self.archive / 'photos' / '1880' / 'x-00100.jpg'
        primary.write_bytes(b'\xff\xd8\xff')
        back = self.archive / 'photos' / '1880' / 'x-00100-back.jpg'
        back.write_bytes(b'\xff\xd8\xff')
        copy_print = self.archive / 'photos' / '1880' / 'x-00100b.jpg'
        copy_print.write_bytes(b'\xff\xd8\xff')

        rc = self._run([str(primary)])
        self.assertEqual(rc, EXIT_CLEAN)

        record = next((self.archive / 'sources' / 'photos').glob('*_S-*.md'))
        files = {Path(f['file']).name: f for f in read_record(record)['meta']['files']}
        self.assertEqual(len(files), 3)
        self.assertEqual(files['x-00100.jpg']['role'], 'primary')
        self.assertEqual(files['x-00100-back.jpg']['role'], 'back')
        self.assertNotEqual(files['x-00100b.jpg']['role'], 'back')
        self.assertEqual(files['x-00100b.jpg']['copy'], 'b')

    def test_document_back_sibling_auto_attached_same_folder(self) -> None:
        # #113: a document's -back scan is pulled in automatically at import
        # time - unlike a copy-letter print, an explicit -back suffix names
        # no other physical item, so there is nothing for a human to be
        # asked about.
        primary = self.archive / 'documents' / 'census' / 'x-00200.txt'
        primary.write_text('front text', encoding='utf-8')
        back = self.archive / 'documents' / 'census' / 'x-00200-back.txt'
        back.write_text('handwritten caption', encoding='utf-8')

        rc = self._run([str(primary), '--type', 'census'])
        self.assertEqual(rc, EXIT_CLEAN)

        self.assertFalse(primary.exists())
        self.assertFalse(back.exists())  # not left on disk unrecorded
        record = next((self.archive / 'sources' / 'census').glob('*_S-*.md'))
        files = read_record(record)['meta']['files']
        self.assertEqual(len(files), 2)
        self.assertEqual(files[0]['role'], 'primary')
        self.assertEqual(files[1]['role'], 'back')
        self.assertEqual(files[1]['original_filename'], 'x-00200-back.txt')
        attached_back = list((self.archive / 'documents' / 'census').glob('*-back_S-*.txt'))
        self.assertEqual(len(attached_back), 1)

    def test_document_back_sibling_auto_attached_from_inbox(self) -> None:
        # Same as above, but both files staged together in inbox/ - the
        # realistic bulk-migration shape #113 was actually found in.
        (self.archive / 'inbox').mkdir()
        primary = self.archive / 'inbox' / 'x-00300.txt'
        primary.write_text('front text', encoding='utf-8')
        back = self.archive / 'inbox' / 'x-00300-back.txt'
        back.write_text('handwritten caption', encoding='utf-8')

        rc = self._run([str(primary), '--type', 'census'])
        self.assertEqual(rc, EXIT_CLEAN)

        self.assertEqual(list((self.archive / 'inbox').iterdir()), [])
        record = next((self.archive / 'sources' / 'census').glob('*_S-*.md'))
        files = read_record(record)['meta']['files']
        self.assertEqual(len(files), 2)
        self.assertEqual(files[1]['role'], 'back')
        self.assertEqual(files[1]['original_filename'], 'x-00300-back.txt')

    def test_document_copy_letter_primary_does_not_pull_back(self) -> None:
        # Scope guard: only a PLAIN scan auto-pulls a back sibling - a
        # copy-letter print processed on its own must not reach for a back
        # scan that conceptually belongs with the group's plain primary.
        copy_print = self.archive / 'documents' / 'census' / 'x-00400b.txt'
        copy_print.write_text('second print', encoding='utf-8')
        back = self.archive / 'documents' / 'census' / 'x-00400-back.txt'
        back.write_text('caption', encoding='utf-8')

        rc = self._run([str(copy_print), '--type', 'census'])
        self.assertEqual(rc, EXIT_CLEAN)

        self.assertTrue(back.exists())  # left untouched, not auto-pulled
        record = next((self.archive / 'sources' / 'census').glob('*_S-*.md'))
        files = read_record(record)['meta']['files']
        self.assertEqual(len(files), 1)

    # ── Codex review, PR #145, finding 1 ────────────────────────────────────────

    def test_back_sibling_relocation_honors_sidecar_hinted_type(self) -> None:
        # Finding #1: the back-sibling relocation must be classified with the
        # SAME resolved type the primary file's own relocation used -
        # including a hint pulled from the PRIMARY's sidecar, which the back
        # sibling has no sidecar of its own to repeat. Before the fix, the
        # back sibling's relocation call saw no sidecar and no --type, so it
        # fell back to its photo extension and previewed a move into
        # photos/ while the primary (thanks to the hint) moved into
        # documents/ - two different roots for one physical item.
        (self.archive / 'inbox').mkdir()
        primary = self.archive / 'inbox' / 'census-01.jpg'
        primary.write_bytes(b'\xff\xd8\xff')
        sidecar = self.archive / 'inbox' / 'census-01.notes.md'
        sidecar.write_text('---\nsource_type: census\n---\n', encoding='utf-8')
        back = self.archive / 'inbox' / 'census-01-back.jpg'
        back.write_bytes(b'\xff\xd8\xff')

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self._run([str(primary), '--dry-run'])
        self.assertEqual(rc, EXIT_CLEAN)
        text = out.getvalue()

        self.assertIn('census-01.jpg out of inbox/ into documents/', text)
        self.assertIn('census-01-back.jpg out of inbox/ into documents/', text)
        self.assertNotIn('census-01-back.jpg out of inbox/ into photos/', text)

    # ── Codex review, PR #145, finding 3 ────────────────────────────────────────

    def test_find_back_sibling_matches_different_extension_via_directory_scan(self) -> None:
        # Finding #3: a back scan saved with a DIFFERENT extension than the
        # primary must still be found. The old implementation constructed
        # only `{primary stem}-back{PRIMARY's own suffix}` candidates, so a
        # back saved as .jpeg beside a .jpg primary was invisible to it -
        # the fix scans the directory and parses each candidate's own stem
        # instead of guessing one hardcoded path.
        primary = self.archive / 'documents' / 'census' / 'x-00500.jpg'
        primary.write_bytes(b'\xff\xd8\xff')
        back = self.archive / 'documents' / 'census' / 'x-00500-back.jpeg'
        back.write_bytes(b'\xff\xd8\xff')

        self.assertEqual(process._find_back_sibling(primary), back)

    def test_find_back_sibling_raises_on_ambiguous_match(self) -> None:
        # Finding #3 (ambiguity half) / finding #4 (Codex PR #145 followup):
        # the old two-candidate guess checked '-back' before '_back' and
        # returned on the FIRST match, blind to any other back-shaped file
        # with a different extension. Scanning the directory can now see both
        # at once - and must surface the ambiguity rather than silently
        # picking one (returning None, indistinguishable from "no back at
        # all") or silently proceeding with neither attached (finding 4).
        primary = self.archive / 'documents' / 'census' / 'ambiguous.txt'
        primary.write_text('front', encoding='utf-8')
        back1 = self.archive / 'documents' / 'census' / 'ambiguous-back.txt'
        back1.write_text('back1', encoding='utf-8')
        back2 = self.archive / 'documents' / 'census' / 'ambiguous_back.pdf'
        back2.write_text('back2', encoding='utf-8')

        with self.assertRaises(process.ProcessError) as ctx:
            process._find_back_sibling(primary)
        text = str(ctx.exception)
        self.assertIn('ambiguous-back.txt', text)
        self.assertIn('ambiguous_back.pdf', text)

    # ── Codex review, PR #145 followups (PR #161), finding 4 ─────────────────

    def test_process_refuses_on_ambiguous_back_candidates(self) -> None:
        # Finding #4: two different files that both look like a back scan
        # (different extensions) must stop the whole `fha process` command,
        # not just silently process the primary alone. Before the fix,
        # `_find_back_sibling` returned the same None for "no back" and "too
        # many backs", so the primary was processed clean and BOTH candidates
        # were left on disk unrecorded with no warning - document intake
        # renames the primary at the same moment (removing the naming link),
        # and `fha doctor`'s orphan check excludes document-root entries, so
        # nothing downstream would ever have caught it.
        primary = self.archive / 'documents' / 'census' / 'twoback.txt'
        primary.write_text('front', encoding='utf-8')
        back1 = self.archive / 'documents' / 'census' / 'twoback-back.txt'
        back1.write_text('back1', encoding='utf-8')
        back2 = self.archive / 'documents' / 'census' / 'twoback_back.pdf'
        back2.write_text('back2', encoding='utf-8')

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = self._run([str(primary), '--type', 'census'])

        self.assertEqual(rc, EXIT_ERRORS)
        text = err.getvalue()
        self.assertIn('twoback-back.txt', text)
        self.assertIn('twoback_back.pdf', text)
        # Nothing moved or minted: the primary is refused before scaffolding,
        # not processed alone with the ambiguity silently dropped.
        self.assertTrue(primary.exists())
        self.assertTrue(back1.exists())
        self.assertTrue(back2.exists())
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])

    # ── #111: --more attaches straight from the inbox ─────────────────────────

    def test_more_attaches_document_straight_from_inbox(self) -> None:
        # Issue #111's own repro, document form: stage front + back together
        # in inbox/, process the front, then --more the back with no manual
        # move in between.
        (self.archive / 'inbox').mkdir()
        front = self.archive / 'inbox' / 'inbox-front.txt'
        front.write_text('front', encoding='utf-8')
        back = self.archive / 'inbox' / 'inbox-back.txt'
        back.write_text('back', encoding='utf-8')

        self.assertEqual(self._run([str(front), '--type', 'census']), EXIT_CLEAN)
        renamed = next((self.archive / 'documents' / 'census').glob('*_S-*.txt'))
        sid = renamed.stem.split('_')[-1]

        rc = self._run([str(renamed), '--more', str(back), 'back'])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(back.exists())  # moved out of inbox and renamed
        self.assertEqual(list((self.archive / 'inbox').iterdir()), [])
        attached = list((self.archive / 'documents' / 'census').glob(f'*back*_{sid}.txt'))
        self.assertEqual(len(attached), 1)
        record = next((self.archive / 'sources' / 'census').glob('*_S-*.md'))
        files = read_record(record)['meta']['files']
        self.assertEqual(len(files), 2)
        self.assertEqual(files[1]['role'], 'back')

    def test_more_attaches_photo_straight_from_inbox(self) -> None:
        # Issue #111's own repro, photo form.
        store = self._install_photo_store()
        (self.archive / 'inbox').mkdir()
        front = self.archive / 'inbox' / 'inbox-front.jpg'
        front.write_bytes(b'\xff\xd8\xff')
        back = self.archive / 'inbox' / 'inbox-back.jpg'
        back.write_bytes(b'\xff\xd8\xff')

        self.assertEqual(self._run([str(front)]), EXIT_CLEAN)
        filed_front = self.archive / 'photos' / 'inbox-front.jpg'
        self.assertTrue(filed_front.exists())
        record = next((self.archive / 'sources' / 'photos').glob('*_S-*.md'))

        rc = self._run([str(filed_front), '--more', str(back), 'back'])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(list((self.archive / 'inbox').iterdir()), [])
        filed_back = self.archive / 'photos' / 'inbox-back.jpg'
        self.assertTrue(filed_back.exists())
        self.assertEqual(len(store.read(filed_back)), 1)
        rec = read_record(record)
        self.assertEqual(len(rec['meta']['files']), 2)
        self.assertEqual(rec['meta']['files'][1]['role'], 'back')

    def test_more_from_inbox_rolled_back_on_failed_attach(self) -> None:
        # A --more file relocated out of inbox must move back there if the
        # attach itself then fails, not be left stranded outside it.
        page1 = self.archive / 'documents' / 'census' / 'unreadable1.txt'
        page1.write_text('p1', encoding='utf-8')
        self.assertEqual(self._run([str(page1), '--type', 'census']), EXIT_CLEAN)
        renamed1 = next((self.archive / 'documents' / 'census').glob('*_S-*.txt'))
        record = next((self.archive / 'sources' / 'census').glob('*_S-*.md'))
        record.unlink()
        record.mkdir()  # read_text() on a directory raises OSError

        (self.archive / 'inbox').mkdir()
        page2 = self.archive / 'inbox' / 'unreadable2.txt'
        page2.write_text('p2', encoding='utf-8')
        rc = self._run([str(renamed1), '--more', str(page2), 'page-2'])
        self.assertEqual(rc, EXIT_FAILURE)
        self.assertTrue(page2.exists())  # relocation undone, back in inbox/
        self.assertEqual(list((self.archive / 'documents' / 'census').glob('*page-2*')), [])

    # ── #108: --more accepts a file already matching this source's S-id ──────

    def test_more_accepts_file_already_matching_this_sources_sid(self) -> None:
        page1 = self.archive / 'documents' / 'census' / 'convention1.txt'
        page1.write_text('p1', encoding='utf-8')
        self.assertEqual(self._run([str(page1), '--type', 'census']), EXIT_CLEAN)
        renamed1 = next((self.archive / 'documents' / 'census').glob('*_S-*.txt'))
        sid = renamed1.stem.split('_')[-1]

        # Named on the archive's own recommended companion convention BEFORE
        # being attached - exactly what a transcript tool would produce.
        pre_named = self.archive / 'documents' / 'census' / f'convention1-transcript_{sid}.md'
        pre_named.write_text('transcript text', encoding='utf-8')

        rc = self._run([str(renamed1), '--more', str(pre_named), 'transcript'])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertTrue(pre_named.exists())  # no rename
        record = next((self.archive / 'sources' / 'census').glob('*_S-*.md'))
        files = read_record(record)['meta']['files']
        self.assertEqual(len(files), 2)
        self.assertEqual(files[1]['role'], 'transcript')
        self.assertTrue(files[1]['file'].endswith(f'convention1-transcript_{sid}.md'))

    def test_more_still_refuses_a_mismatched_sid(self) -> None:
        page1 = self.archive / 'documents' / 'census' / 'convention2.txt'
        page1.write_text('p1', encoding='utf-8')
        self.assertEqual(self._run([str(page1), '--type', 'census']), EXIT_CLEAN)
        renamed1 = next((self.archive / 'documents' / 'census').glob('*_S-*.txt'))

        other = self.archive / 'documents' / 'census' / 'other-transcript_S-zzzzzzzzzz.md'
        other.write_text('x', encoding='utf-8')
        rc = self._run([str(renamed1), '--more', str(other), 'transcript'])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertTrue(other.exists())  # untouched, not renamed

    # ── Codex review, PR #145, finding 2 ────────────────────────────────────────

    def test_more_retry_of_already_attached_file_is_idempotent(self) -> None:
        # Finding #2: retrying the identical --more command for a file that
        # already carries this source's S-id AND is already listed in the
        # record's own files: must be a clean no-op, never a second,
        # duplicate files: entry for the same physical file.
        page1 = self.archive / 'documents' / 'census' / 'convention4.txt'
        page1.write_text('p1', encoding='utf-8')
        self.assertEqual(self._run([str(page1), '--type', 'census']), EXIT_CLEAN)
        renamed1 = next((self.archive / 'documents' / 'census').glob('*_S-*.txt'))
        sid = renamed1.stem.split('_')[-1]

        pre_named = self.archive / 'documents' / 'census' / f'convention4-transcript_{sid}.md'
        pre_named.write_text('transcript text', encoding='utf-8')

        rc1 = self._run([str(renamed1), '--more', str(pre_named), 'transcript'])
        self.assertEqual(rc1, EXIT_CLEAN)
        record = next((self.archive / 'sources' / 'census').glob('*_S-*.md'))
        self.assertEqual(len(read_record(record)['meta']['files']), 2)

        rc2 = self._run([str(renamed1), '--more', str(pre_named), 'transcript'])
        self.assertEqual(rc2, EXIT_CLEAN)
        files = read_record(record)['meta']['files']
        self.assertEqual(len(files), 2)  # still 2 - no duplicate appended

    def test_more_refuses_conflicting_role_for_already_listed_alias(self) -> None:
        # Finding #2: the same physical file, already listed, requested
        # again under a DIFFERENT role must refuse - never a silent second
        # entry carrying a conflicting role for one file.
        page1 = self.archive / 'documents' / 'census' / 'convention5.txt'
        page1.write_text('p1', encoding='utf-8')
        self.assertEqual(self._run([str(page1), '--type', 'census']), EXIT_CLEAN)
        renamed1 = next((self.archive / 'documents' / 'census').glob('*_S-*.txt'))
        sid = renamed1.stem.split('_')[-1]

        pre_named = self.archive / 'documents' / 'census' / f'convention5-transcript_{sid}.md'
        pre_named.write_text('transcript text', encoding='utf-8')

        rc1 = self._run([str(renamed1), '--more', str(pre_named), 'transcript'])
        self.assertEqual(rc1, EXIT_CLEAN)

        rc2 = self._run([str(renamed1), '--more', str(pre_named), 'attachment'])
        self.assertEqual(rc2, EXIT_ERRORS)
        record = next((self.archive / 'sources' / 'census').glob('*_S-*.md'))
        files = read_record(record)['meta']['files']
        self.assertEqual(len(files), 2)  # unchanged - no second entry

    def test_more_refuses_reattaching_the_primary_file_itself(self) -> None:
        # Finding #2 (the primary-file variant named in the review):
        # attaching a source's own already-processed primary file back to
        # itself under a different role must refuse, not add a second
        # files: entry for the primary.
        page1 = self.archive / 'documents' / 'census' / 'convention6.txt'
        page1.write_text('p1', encoding='utf-8')
        self.assertEqual(self._run([str(page1), '--type', 'census']), EXIT_CLEAN)
        renamed1 = next((self.archive / 'documents' / 'census').glob('*_S-*.txt'))

        rc = self._run([str(renamed1), '--more', str(renamed1), 'attachment'])
        self.assertEqual(rc, EXIT_ERRORS)
        record = next((self.archive / 'sources' / 'census').glob('*_S-*.md'))
        files = read_record(record)['meta']['files']
        self.assertEqual(len(files), 1)

    # ── Codex review, PR #145 followups (PR #161), finding 3 ─────────────────

    def test_more_replacement_for_missing_listed_attachment_is_refused(self) -> None:
        # Finding #3: when a source record still lists an attachment whose
        # file has gone missing from disk, and the human supplies a
        # REPLACEMENT file that would be renamed to that SAME alias with the
        # same role/copy, the idempotent-retry check (finding #2's own fix)
        # must not report a false "already attached, nothing to do" - the
        # listed file is still missing, and the replacement is still sitting
        # untouched under its old name.
        primary = self.archive / 'documents' / 'census' / 'main-record.txt'
        primary.write_text('front', encoding='utf-8')
        self.assertEqual(self._run([str(primary), '--type', 'census']), EXIT_CLEAN)
        renamed = next((self.archive / 'documents' / 'census').glob('main-record*_S-*.txt'))
        record = next((self.archive / 'sources' / 'census').glob('*_S-*.md'))

        original_more = self.archive / 'documents' / 'census' / 'replacement.txt'
        original_more.write_text('original page 2', encoding='utf-8')
        rc = self._run([str(renamed), '--more', str(original_more), 'page-2'])
        self.assertEqual(rc, EXIT_CLEAN)
        attached = next((self.archive / 'documents' / 'census').glob('replacement-page-2_S-*.txt'))
        self.assertEqual(len(read_record(record)['meta']['files']), 2)

        # The listed file goes missing from disk (deleted, an external drive
        # unplugged, whatever the cause) - the SAME name is free again.
        attached.unlink()
        replacement = self.archive / 'documents' / 'census' / 'replacement.txt'
        replacement.write_text('rescanned page 2', encoding='utf-8')

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = self._run([str(renamed), '--more', str(replacement), 'page-2'])

        self.assertEqual(rc, EXIT_ERRORS)
        text = err.getvalue()
        self.assertIn('missing from disk', text)
        self.assertNotIn('Traceback', text)
        # No false success: the replacement was never moved into place, the
        # hole is still a hole, and the record still carries only the one
        # (stale) entry - no silent duplicate either.
        self.assertTrue(replacement.exists())
        self.assertFalse(attached.exists())
        self.assertEqual(len(read_record(record)['meta']['files']), 2)

    def test_more_exact_same_file_at_listed_destination_is_still_idempotent(self) -> None:
        # Scope guard alongside finding #3: the genuinely-already-done case
        # (the supplied file IS already sitting at the listed destination)
        # must still short-circuit as a clean no-op - only a file at a
        # DIFFERENT current location triggers the new refusal above.
        primary = self.archive / 'documents' / 'census' / 'main-record2.txt'
        primary.write_text('front', encoding='utf-8')
        self.assertEqual(self._run([str(primary), '--type', 'census']), EXIT_CLEAN)
        renamed = next((self.archive / 'documents' / 'census').glob('main-record2*_S-*.txt'))
        record = next((self.archive / 'sources' / 'census').glob('*_S-*.md'))

        more_file = self.archive / 'documents' / 'census' / 'sidepage.txt'
        more_file.write_text('page 2', encoding='utf-8')
        rc = self._run([str(renamed), '--more', str(more_file), 'page-2'])
        self.assertEqual(rc, EXIT_CLEAN)
        attached = next((self.archive / 'documents' / 'census').glob('sidepage-page-2_S-*.txt'))

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self._run([str(renamed), '--more', str(attached), 'page-2'])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('nothing to do', out.getvalue())
        self.assertEqual(len(read_record(record)['meta']['files']), 2)

    # ── Codex review, PR #145, finding 5 ────────────────────────────────────────

    def test_more_honors_explicit_type_for_photo_extension_attachment(self) -> None:
        # Finding #5: --more must honor an explicit --type the same way the
        # primary file's own classification does - a .jpg page attached with
        # --type census must be filed and renamed as a document page under
        # documents/census/, not defaulted into the photo library by
        # extension with an embedded PHOTO keyword instead.
        self._install_photo_store()  # only exercised if the fix regresses
        primary = self.archive / 'documents' / 'census' / 'convention7.txt'
        primary.write_text('p1', encoding='utf-8')
        self.assertEqual(self._run([str(primary), '--type', 'census']), EXIT_CLEAN)
        renamed = next((self.archive / 'documents' / 'census').glob('*_S-*.txt'))

        (self.archive / 'inbox').mkdir()
        page2 = self.archive / 'inbox' / 'page-2.jpg'
        page2.write_bytes(b'\xff\xd8\xff')

        rc = self._run([str(renamed), '--more', str(page2), 'page-2', '--type', 'census'])
        self.assertEqual(rc, EXIT_CLEAN)

        self.assertEqual(list((self.archive / 'photos').rglob('page-2*')), [])
        attached = list((self.archive / 'documents' / 'census').glob('*page-2*_S-*.jpg'))
        self.assertEqual(len(attached), 1)
        record = next((self.archive / 'sources' / 'census').glob('*_S-*.md'))
        files = read_record(record)['meta']['files']
        self.assertEqual(len(files), 2)
        self.assertEqual(files[1]['role'], 'page-2')

    # ── Codex review, PR #145 followups (PR #161), finding 1 (P1) ────────────
    # An explicit `--type dna` on a `--more` attachment must not leave the
    # SOURCE record itself unrestricted: `fha packet` decides what to copy
    # per SOURCE, not per file, so an unrestricted source that gains a DNA
    # attachment would otherwise ship it under --include-restricted alone -
    # or under no flag at all - defeating the no-override `restricted: dna`
    # contract (AGENTS.md #6).

    def test_force_dna_restriction_text_unit(self) -> None:
        # Direct coverage of the text-surgery helper: an absent or plain
        # marker is upgraded to `restricted: dna`; a marker that already
        # protects at least as well (`dna` itself, or the never-overridden
        # `by-request`) is left untouched rather than downgraded.
        absent = '---\nid: s-1234567890\ntitle: A Letter\n---\n## Claims\n'
        new_text, changed = process._force_dna_restriction_text(absent)
        self.assertTrue(changed)
        self.assertEqual(process.parse_frontmatter_strict(new_text)['restricted'], 'dna')

        plain = '---\nid: s-1234567890\ntitle: A Letter\nrestricted: true\n---\n## Claims\n'
        new_text, changed = process._force_dna_restriction_text(plain)
        self.assertTrue(changed)
        self.assertEqual(process.parse_frontmatter_strict(new_text)['restricted'], 'dna')

        already_dna = '---\nid: s-1234567890\ntitle: A Letter\nrestricted: dna\n---\n## Claims\n'
        new_text, changed = process._force_dna_restriction_text(already_dna)
        self.assertFalse(changed)
        self.assertEqual(new_text, already_dna)

        by_request = ('---\nid: s-1234567890\ntitle: A Letter\n'
                      'restricted: by-request\n---\n## Claims\n')
        new_text, changed = process._force_dna_restriction_text(by_request)
        self.assertFalse(changed)  # MORE restrictive than dna - never downgraded
        self.assertEqual(new_text, by_request)

    # ── Issue #169, finding 1 ──────────────────────────────────────────────
    # A Codex P2 from PR #161's review, tracked rather than fixed at the
    # time: `restricted:` may itself be a valid multi-line YAML block scalar
    # (`restricted: >-` with an indented continuation on the next line). The
    # old single-line replace rewrote only the header, leaving the
    # continuation orphaned in the frontmatter - malformed YAML that still
    # reports success.

    def test_force_dna_restriction_text_replaces_block_scalar_span(self) -> None:
        # `restricted: >-` / `true` (folded block scalar) is valid YAML that
        # parses to the string 'true' - _restricted_type_of reads that as
        # 'plain', i.e. not yet DNA-sufficient, so the upgrade must fire.
        block_scalar = (
            '---\nid: s-1234567890\ntitle: A Letter\n'
            'restricted: >-\n  true\nnote: kept\n---\n## Claims\n'
        )
        new_text, changed = process._force_dna_restriction_text(block_scalar)
        self.assertTrue(changed)
        reparsed = process.parse_frontmatter_strict(new_text)
        self.assertIsNotNone(reparsed)
        self.assertEqual(reparsed['restricted'], 'dna')
        self.assertEqual(reparsed['id'], 's-1234567890')
        self.assertEqual(reparsed['note'], 'kept')  # untouched sibling field
        # No orphaned continuation line left behind: exactly one line opens
        # with 'restricted:' and it is the single rewritten line.
        lines = new_text.split('\n')
        restricted_lines = [ln for ln in lines if ln.strip() == 'restricted: dna']
        self.assertEqual(restricted_lines, ['restricted: dna'])
        self.assertNotIn('  true', lines)

    def test_force_dna_restriction_text_replaces_digit_before_chomp_block_scalar_span(
            self) -> None:
        # YAML permits the block scalar's indentation indicator (a digit)
        # and its chomping indicator (+/-) in EITHER order - `|2-` and `|-2`
        # mean the same thing. The header regex originally only accepted
        # chomp-then-digit; this is the other order (adversarial review,
        # round-2 audit - a real gap, though the belt-and-suspenders
        # re-parse below already caught it by accident before this
        # tightening, via YAML's own line-folding of the orphaned
        # continuation).
        block_scalar = (
            '---\nid: s-1234567890\ntitle: A Letter\n'
            'restricted: |2-\n  true\nnote: kept\n---\n## Claims\n'
        )
        new_text, changed = process._force_dna_restriction_text(block_scalar)
        self.assertTrue(changed)
        reparsed = process.parse_frontmatter_strict(new_text)
        self.assertIsNotNone(reparsed)
        self.assertEqual(reparsed['restricted'], 'dna')
        self.assertEqual(reparsed['note'], 'kept')
        lines = new_text.split('\n')
        self.assertEqual(
            [ln for ln in lines if ln.strip() == 'restricted: dna'],
            ['restricted: dna'])
        self.assertNotIn('  true', lines)

    def test_force_dna_restriction_text_replaces_literal_block_scalar_span_crlf(self) -> None:
        # Same shape with the `|` (literal) block style and CRLF line
        # endings, to confirm the span-removal keeps this function's own
        # documented CRLF-faithful promise even when it has to drop lines
        # from the middle of the frontmatter, not just rewrite one in place.
        block_scalar = (
            '---\r\nid: s-1234567890\r\ntitle: A Letter\r\n'
            'restricted: |\r\n  yes\r\n  really\r\n---\r\n## Claims\r\n'
        )
        new_text, changed = process._force_dna_restriction_text(block_scalar)
        self.assertTrue(changed)
        self.assertNotIn('\n', new_text.replace('\r\n', ''))  # every line still CRLF
        reparsed = process.parse_frontmatter_strict(new_text)
        self.assertIsNotNone(reparsed)
        self.assertEqual(reparsed['restricted'], 'dna')
        self.assertNotIn('yes', new_text)
        self.assertNotIn('really', new_text)

    # ── Issue #169 followup review, finding 2 ─────────────────────────────
    # YAML permits a "node property" - an anchor (`&name`) and/or a type tag
    # (`!!str`, or a bare/custom `!tag`) - to precede the block-scalar
    # indicator on the SAME line: `restricted: &privacy >-` and
    # `restricted: !!str |` are both valid, hand-authored YAML. The regex
    # only recognized `[|>]` as the very first character of the value, so it
    # never even detected these as block-scalar headers at all -
    # `_frontmatter_key_span` fell back to header-only replacement, and the
    # belt-and-suspenders re-parse then refused (raised ProcessError) rather
    # than hand back corrupted frontmatter - reproducing the ORIGINAL #169
    # bug's shape as a false REFUSAL (attaching a DNA file fails outright)
    # instead of silent corruption.

    def test_force_dna_restriction_text_replaces_anchored_block_scalar_span(self) -> None:
        block_scalar = (
            '---\nid: s-1234567890\ntitle: A Letter\n'
            'restricted: &privacy >-\n  true\nnote: kept\n---\n## Claims\n'
        )
        # Confirm the premise: PyYAML really does accept this as valid YAML.
        self.assertEqual(
            process.parse_frontmatter_strict(block_scalar)['restricted'], 'true')

        new_text, changed = process._force_dna_restriction_text(block_scalar)
        self.assertTrue(changed)
        reparsed = process.parse_frontmatter_strict(new_text)
        self.assertIsNotNone(reparsed)
        self.assertEqual(reparsed['restricted'], 'dna')
        self.assertEqual(reparsed['note'], 'kept')
        lines = new_text.split('\n')
        self.assertEqual(
            [ln for ln in lines if ln.strip() == 'restricted: dna'],
            ['restricted: dna'])
        self.assertNotIn('  true', lines)

    def test_force_dna_restriction_text_replaces_tagged_block_scalar_span(self) -> None:
        block_scalar = (
            '---\nid: s-1234567890\ntitle: A Letter\n'
            'restricted: !!str |\n  true\nnote: kept\n---\n## Claims\n'
        )
        self.assertEqual(
            process.parse_frontmatter_strict(block_scalar)['restricted'], 'true\n')

        new_text, changed = process._force_dna_restriction_text(block_scalar)
        self.assertTrue(changed)
        reparsed = process.parse_frontmatter_strict(new_text)
        self.assertIsNotNone(reparsed)
        self.assertEqual(reparsed['restricted'], 'dna')
        self.assertEqual(reparsed['note'], 'kept')
        lines = new_text.split('\n')
        self.assertEqual(
            [ln for ln in lines if ln.strip() == 'restricted: dna'],
            ['restricted: dna'])
        self.assertNotIn('  true', lines)

    def test_force_dna_restriction_text_replaces_anchor_and_tag_block_scalar_span(
            self) -> None:
        # YAML allows both node properties on one node, in either order -
        # confirm the combined anchor+tag form is recognized too.
        block_scalar = (
            '---\nid: s-1234567890\ntitle: A Letter\n'
            'restricted: &privacy !!str >-\n  true\nnote: kept\n---\n## Claims\n'
        )
        self.assertEqual(
            process.parse_frontmatter_strict(block_scalar)['restricted'], 'true')

        new_text, changed = process._force_dna_restriction_text(block_scalar)
        self.assertTrue(changed)
        reparsed = process.parse_frontmatter_strict(new_text)
        self.assertIsNotNone(reparsed)
        self.assertEqual(reparsed['restricted'], 'dna')
        self.assertEqual(reparsed['note'], 'kept')
        lines = new_text.split('\n')
        self.assertEqual(
            [ln for ln in lines if ln.strip() == 'restricted: dna'],
            ['restricted: dna'])
        self.assertNotIn('  true', lines)

    def test_more_type_dna_forces_restriction_on_unrestricted_source(self) -> None:
        # The P1 scenario itself: attach a DNA file, via --more --type dna,
        # to an EXISTING source that started out unrestricted.
        primary = self.archive / 'documents' / 'census' / 'family-letter.txt'
        primary.write_text('Dear cousin,', encoding='utf-8')
        rc = self._run([str(primary), '--type', 'letter'])
        self.assertEqual(rc, EXIT_CLEAN)
        renamed = next((self.archive / 'documents' / 'census').glob('family-letter*_S-*.txt'))
        record = next((self.archive / 'sources' / 'letter').glob('*_S-*.md'))
        self.assertNotIn('restricted', read_record(record)['meta'])  # unrestricted to start

        (self.archive / 'inbox').mkdir()
        kit = self.archive / 'inbox' / 'ancestrydna-kit.txt'
        kit.write_text('rsid,chromosome,position,allele1,allele2\n', encoding='utf-8')

        rc = self._run([str(renamed), '--more', str(kit), 'attachment', '--type', 'dna'])
        self.assertEqual(rc, EXIT_CLEAN)

        rec = read_record(record)['meta']
        self.assertEqual(rec['restricted'], 'dna')
        self.assertEqual(len(rec['files']), 2)

        # Classified as a DOCUMENT (finding #5's own contract - --type dna is
        # not --type photo) - the extension never sends it to the photo
        # library, where this DNA-restriction fix does not reach.
        self.assertEqual(list((self.archive / 'photos').rglob('*ancestrydna*')), [])
        attached = list((self.archive / 'documents' / 'census').glob('*ancestrydna-kit*_S-*'))
        self.assertEqual(len(attached), 1)

        # packet.py's OWN decision function agrees this now reads as the
        # no-override 'dna' type, which --include-restricted alone does NOT
        # open (mirrors test_packet.py's
        # test_dna_source_excluded_even_with_include_restricted).
        value, trouble = packet._record_restriction(record)
        self.assertIsNone(trouble)
        self.assertEqual(packet._restricted_type(value), 'dna')
        self.assertFalse(packet._restricted_included(
            value, include_restricted=True, include_dna=False))
        self.assertTrue(packet._restricted_included(
            value, include_restricted=False, include_dna=True))

    def test_more_type_dna_dry_run_previews_restriction_without_writing(self) -> None:
        primary = self.archive / 'documents' / 'census' / 'dry-letter.txt'
        primary.write_text('Dear cousin,', encoding='utf-8')
        self.assertEqual(self._run([str(primary), '--type', 'letter']), EXIT_CLEAN)
        renamed = next((self.archive / 'documents' / 'census').glob('dry-letter*_S-*.txt'))
        record = next((self.archive / 'sources' / 'letter').glob('*_S-*.md'))

        (self.archive / 'inbox').mkdir()
        kit = self.archive / 'inbox' / 'dry-kit.txt'
        kit.write_text('data', encoding='utf-8')

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self._run(
                [str(renamed), '--more', str(kit), 'attachment', '--type', 'dna', '--dry-run'])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('Would set restricted: dna', out.getvalue())
        self.assertNotIn('restricted', read_record(record)['meta'])  # nothing written
        self.assertTrue(kit.exists())  # not moved

    def test_more_type_dna_upgrades_restriction_on_idempotent_retry(self) -> None:
        # The idempotent-retry no-op (finding #3's own fix) must not let an
        # already-listed DNA attachment stay unrestricted forever just
        # because the file/role/copy already match - re-running the same
        # `--more --type dna` command must still upgrade a stale record even
        # when there is no new files: entry to add.
        primary = self.archive / 'documents' / 'census' / 'family-letter2.txt'
        primary.write_text('Dear cousin,', encoding='utf-8')
        self.assertEqual(self._run([str(primary), '--type', 'letter']), EXIT_CLEAN)
        renamed = next((self.archive / 'documents' / 'census').glob('family-letter2*_S-*.txt'))
        record = next((self.archive / 'sources' / 'letter').glob('*_S-*.md'))

        (self.archive / 'inbox').mkdir()
        kit = self.archive / 'inbox' / 'kit2.txt'
        kit.write_text('data', encoding='utf-8')

        rc = self._run([str(renamed), '--more', str(kit), 'attachment', '--type', 'dna'])
        self.assertEqual(rc, EXIT_CLEAN)
        attached = next((self.archive / 'documents' / 'census').glob('kit2*_S-*.txt'))
        self.assertEqual(read_record(record)['meta']['restricted'], 'dna')

        # Simulate a pre-fix record: strip restricted: back out by hand, as
        # if this attach had happened before finding #1 existed, leaving the
        # files: entry exactly as it already is (the file is already at its
        # listed destination - the genuinely-already-done case finding #3's
        # check is meant for).
        text = record.read_text(encoding='utf-8')
        record.write_text(text.replace('restricted: dna\n', ''), encoding='utf-8')
        self.assertNotIn('restricted', read_record(record)['meta'])

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self._run(
                [str(renamed), '--more', str(attached), 'attachment', '--type', 'dna'])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('already attached', out.getvalue())
        self.assertIn('Set restricted: dna', out.getvalue())
        rec = read_record(record)['meta']
        self.assertEqual(rec['restricted'], 'dna')
        self.assertEqual(len(rec['files']), 2)  # not duplicated

    def test_more_type_dna_attach_replaces_block_scalar_restricted_span(self) -> None:
        # Issue #169, finding 1, end-to-end through `--more --type dna`: a
        # source whose `restricted:` was hand-authored as a multi-line YAML
        # block scalar must come out with a clean single-line
        # `restricted: dna` and no orphaned continuation line - not the
        # malformed frontmatter the old single-line replace produced while
        # still reporting success.
        primary = self.archive / 'documents' / 'census' / 'family-letter3.txt'
        primary.write_text('Dear cousin,', encoding='utf-8')
        self.assertEqual(self._run([str(primary), '--type', 'letter']), EXIT_CLEAN)
        renamed = next((self.archive / 'documents' / 'census').glob('family-letter3*_S-*.txt'))
        record = next((self.archive / 'sources' / 'letter').glob('*_S-*.md'))

        # Hand-author a valid multiline `restricted:` block scalar into the
        # freshly scaffolded record, as a human editing the record directly
        # might (the scenario Codex's review flagged).
        text = record.read_text(encoding='utf-8')
        self.assertIn('---\n', text)
        head, _, tail = text.partition('---\n')
        # head is '' (fence opens the file); split tail at its own closing
        # fence to insert a block-scalar restricted: line inside the frontmatter.
        body, _, rest = tail.partition('\n---\n')
        text = f'---\n{body}\nrestricted: >-\n  true\n---\n{rest}'
        record.write_text(text, encoding='utf-8')
        meta = process.parse_frontmatter_strict(record.read_text(encoding='utf-8'))
        self.assertEqual(meta['restricted'], 'true')  # block scalar reads as a plain string

        (self.archive / 'inbox').mkdir()
        kit = self.archive / 'inbox' / 'kit3.txt'
        kit.write_text('data', encoding='utf-8')

        rc = self._run([str(renamed), '--more', str(kit), 'attachment', '--type', 'dna'])
        self.assertEqual(rc, EXIT_CLEAN)

        final_text = record.read_text(encoding='utf-8')
        reparsed = process.parse_frontmatter_strict(final_text)
        self.assertIsNotNone(reparsed)  # still valid YAML, not malformed
        self.assertEqual(reparsed['restricted'], 'dna')
        rec = read_record(record)['meta']
        self.assertEqual(rec['restricted'], 'dna')
        self.assertEqual(len(rec['files']), 2)
        # No orphaned continuation line ('  true') left dangling in the file.
        lines = final_text.split('\n')
        self.assertNotIn('  true', lines)
        restricted_lines = [ln for ln in lines if ln.strip() == 'restricted: dna']
        self.assertEqual(restricted_lines, ['restricted: dna'])

    def test_more_without_dna_type_never_touches_restriction(self) -> None:
        # Scope guard: a --more attach with no --type (or a non-dna --type)
        # must never add a restricted: marker - only an explicit --type dna
        # triggers the upgrade.
        primary = self.archive / 'documents' / 'census' / 'plain-letter.txt'
        primary.write_text('Dear cousin,', encoding='utf-8')
        self.assertEqual(self._run([str(primary), '--type', 'letter']), EXIT_CLEAN)
        renamed = next((self.archive / 'documents' / 'census').glob('plain-letter*_S-*.txt'))
        record = next((self.archive / 'sources' / 'letter').glob('*_S-*.md'))

        (self.archive / 'inbox').mkdir()
        extra = self.archive / 'inbox' / 'plain-page-2.txt'
        extra.write_text('more text', encoding='utf-8')

        rc = self._run([str(renamed), '--more', str(extra), 'page-2'])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertNotIn('restricted', read_record(record)['meta'])

    # ── Issue #169 followup review, findings 3 and 4 ──────────────────────
    # Finding #2's own fix widened `_BLOCK_SCALAR_HEADER_RE` enough that a
    # naturally hand-authored, still-refusable `restricted:` shape is no
    # longer easy to construct - so these tests force the refusal
    # deterministically by monkeypatching `_force_dna_restriction_text`
    # itself, exactly as the review comments anticipated.

    def test_more_type_dna_dry_run_reports_same_refusal_as_live(self) -> None:
        # Finding #3: on the documents-root --more --type dna path, the
        # rewrite validation used to run only AFTER the dry-run branch had
        # already returned clean - so --dry-run promised a restriction
        # update the live run could not actually deliver whenever
        # _force_dna_restriction_text would refuse. Confirm --dry-run now
        # reports the SAME refusal the live run gives, instead of exiting
        # clean and letting the live run begin the rename and then fail.
        primary = self.archive / 'documents' / 'census' / 'refuse-letter.txt'
        primary.write_text('Dear cousin,', encoding='utf-8')
        self.assertEqual(self._run([str(primary), '--type', 'letter']), EXIT_CLEAN)
        renamed = next((self.archive / 'documents' / 'census').glob('refuse-letter*_S-*.txt'))
        record = next((self.archive / 'sources' / 'letter').glob('*_S-*.md'))

        (self.archive / 'inbox').mkdir()
        kit = self.archive / 'inbox' / 'refuse-kit.txt'
        kit.write_text('data', encoding='utf-8')

        def _refuse(record_text, *, record_label=None):
            raise process.ProcessError('refusing: simulated unsafe restricted: rewrite')

        orig = process._force_dna_restriction_text
        process._force_dna_restriction_text = _refuse
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = self._run([str(renamed), '--more', str(kit), 'attachment',
                                 '--type', 'dna', '--dry-run'])
            self.assertEqual(rc, EXIT_ERRORS)
            self.assertIn('simulated unsafe restricted', err.getvalue())
            self.assertTrue(kit.exists())  # not moved
            self.assertNotIn('restricted', read_record(record)['meta'])  # nothing written

            err2 = io.StringIO()
            with contextlib.redirect_stderr(err2):
                rc_live = self._run(
                    [str(renamed), '--more', str(kit), 'attachment', '--type', 'dna'])
            self.assertEqual(rc_live, EXIT_ERRORS)
            self.assertIn('simulated unsafe restricted', err2.getvalue())
        finally:
            process._force_dna_restriction_text = orig
        self.assertTrue(kit.exists())  # still not moved after the live refusal
        self.assertNotIn('restricted', read_record(record)['meta'])
        self.assertEqual(len(read_record(record)['meta']['files']), 1)  # no new entry

    def test_more_type_dna_photo_preflights_restriction_before_embed(self) -> None:
        # Finding #4 (photo path): the restriction rewrite's validation used
        # to run AFTER the exiftool SOURCE: embed - an irreversible-ish
        # mutation that then needed rollback if the rewrite was refused.
        # Force a refusal and confirm the embed is never even attempted, not
        # "attempted then rolled back".
        store = self._install_photo_store()
        front = self.archive / 'photos' / '1880' / 'dna-card.jpg'
        front.write_bytes(b'\xff\xd8\xff')
        self.assertEqual(self._run([str(front)]), EXIT_CLEAN)
        record = next((self.archive / 'sources' / 'photos').glob('*_S-*.md'))

        kit = self.archive / 'photos' / '1880' / 'dna-card-kit.jpg'
        kit.write_bytes(b'\xff\xd8\xff')

        def _refuse(record_text, *, record_label=None):
            raise process.ProcessError('refusing: simulated unsafe restricted: rewrite')

        orig = process._force_dna_restriction_text
        process._force_dna_restriction_text = _refuse
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = self._run(
                    [str(front), '--more', str(kit), 'attachment', '--type', 'dna'])
        finally:
            process._force_dna_restriction_text = orig

        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('simulated unsafe restricted', err.getvalue())
        # The embed must never have happened - no SOURCE: keyword landed on
        # the kit file, not "embedded then removed on rollback".
        self.assertEqual(store.read(kit), [])
        self.assertNotIn('restricted', read_record(record)['meta'])
        self.assertEqual(len(read_record(record)['meta']['files']), 1)  # no new entry

    def test_more_type_dna_document_preflights_restriction_before_rename(self) -> None:
        # Finding #4 (documents counterpart): the restriction rewrite's
        # validation used to run AFTER the file was already renamed into
        # place. Force a refusal and confirm the rename is never even
        # attempted - spying on Path.rename itself, since an end-state check
        # alone cannot distinguish "never renamed" from "renamed, then
        # successfully rolled back" (the whole point of this finding is that
        # the old code depended on that rollback succeeding).
        primary = self.archive / 'documents' / 'census' / 'refuse-doc.txt'
        primary.write_text('Dear cousin,', encoding='utf-8')
        self.assertEqual(self._run([str(primary), '--type', 'letter']), EXIT_CLEAN)
        renamed = next((self.archive / 'documents' / 'census').glob('refuse-doc*_S-*.txt'))
        record = next((self.archive / 'sources' / 'letter').glob('*_S-*.md'))

        kit = self.archive / 'documents' / 'census' / 'refuse-kit.txt'
        kit.write_text('data', encoding='utf-8')

        def _refuse(record_text, *, record_label=None):
            raise process.ProcessError('refusing: simulated unsafe restricted: rewrite')

        rename_calls: list[tuple[str, str]] = []
        orig_rename = Path.rename

        def _spy_rename(self_path, target):
            rename_calls.append((str(self_path), str(target)))
            return orig_rename(self_path, target)

        orig_force = process._force_dna_restriction_text
        process._force_dna_restriction_text = _refuse
        with unittest.mock.patch.object(Path, 'rename', _spy_rename):
            try:
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    rc = self._run(
                        [str(renamed), '--more', str(kit), 'attachment', '--type', 'dna'])
            finally:
                process._force_dna_restriction_text = orig_force

        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('simulated unsafe restricted', err.getvalue())
        self.assertEqual(rename_calls, [])  # never attempted, not undone
        self.assertTrue(kit.exists())
        self.assertNotIn('restricted', read_record(record)['meta'])
        self.assertEqual(len(read_record(record)['meta']['files']), 1)  # no new entry

    # ── classification + slug units ──────────────────────────────────────────

    def test_classify_asset(self) -> None:
        cfg = {'roots': {'photos': 'photos', 'documents': 'documents'}}
        self.assertEqual(
            process.classify_asset(Path('x.jpg'), cfg, self.archive), 'photo')
        self.assertEqual(
            process.classify_asset(Path('x.pdf'), cfg, self.archive), 'document')
        # Odd extension under the photos root still classifies as a photo.
        odd = self.archive / 'photos' / '1880' / 'scan.xyz'
        odd.write_bytes(b'0')
        self.assertEqual(process.classify_asset(odd, cfg, self.archive), 'photo')

    def test_classify_asset_documents_root_wins_over_photo_extension(self) -> None:
        # A scanned record filed under documents/ but saved as .jpg is still a
        # document: the documents-root identity rule takes precedence over the
        # extension-based photo rule.
        cfg = {'roots': {'photos': 'photos', 'documents': 'documents'}}
        scan = self.archive / 'documents' / 'census' / 'scan.jpg'
        scan.write_bytes(b'0')
        self.assertEqual(process.classify_asset(scan, cfg, self.archive), 'document')

    def test_document_refuses_file_outside_documents_root(self) -> None:
        outside = self.archive / 'inbox.txt'
        outside.write_text('x', encoding='utf-8')
        rc = self._run([str(outside)])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertTrue(outside.exists())
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])

    def test_document_relative_path_resolves_alias_under_cwd(self) -> None:
        original = self.archive / 'documents' / 'census' / 'deed.txt'
        original.write_text('x', encoding='utf-8')
        old_cwd = Path.cwd()
        try:
            os.chdir(original.parent)
            rc = process._standalone_main(['deed.txt', '--root', str(self.archive)])
        finally:
            os.chdir(old_cwd)
        self.assertEqual(rc, EXIT_CLEAN)
        record = next((self.archive / 'sources' / 'other').glob('*_S-*.md'))
        rec = read_record(record)
        self.assertTrue(rec['meta']['files'][0]['file'].startswith('documents/census/'))

    def test_photo_dry_run_survives_exiftool_read_failure(self) -> None:
        photo = self.archive / 'photos' / '1880' / 'broken.jpg'
        photo.write_bytes(b'\xff\xd8\xff')

        def _boom(_path: Path) -> list[str]:
            raise RuntimeError('exiftool not on PATH')

        process._run_exiftool_read_keywords = _boom
        rc = self._run([str(photo), '--dry-run'])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])

    def test_sidecar_malformed_frontmatter_refused(self) -> None:
        asset = self.archive / 'documents' / 'census' / 'broken.txt'
        asset.write_text('x', encoding='utf-8')
        sidecar = self.archive / 'documents' / 'census' / 'broken.notes.md'
        sidecar.write_text('---\ntitle: [unterminated\n---\nnotes\n', encoding='utf-8')

        rc = self._run([str(asset)])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertTrue(sidecar.exists())  # not consumed on failure
        self.assertTrue(asset.exists())  # not renamed
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])

    def test_sidecar_undecodable_refused_cleanly(self) -> None:
        # #68: a sidecar saved in another encoding (cp1252, a Windows editor's
        # default) must get the same clean ProcessError refusal as malformed
        # YAML, naming the real fix (re-save as UTF-8), not a raw
        # UnicodeDecodeError traceback - and the sidecar must survive
        # untouched, exactly like the malformed-frontmatter case above.
        asset = self.archive / 'documents' / 'census' / 'accented.txt'
        asset.write_text('x', encoding='utf-8')
        sidecar = self.archive / 'documents' / 'census' / 'accented.notes.md'
        sidecar.write_bytes(
            '---\ntitle: Kraków deed\n---\nBorn in Kraków.\n'.encode('cp1252'))
        before = sidecar.read_bytes()

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = self._run([str(asset)])

        self.assertEqual(rc, EXIT_ERRORS)
        text = err.getvalue()
        self.assertNotIn('Traceback', text)
        self.assertIn('accented.notes.md', text)
        self.assertIn('not saved as UTF-8', text)
        self.assertTrue(sidecar.exists())  # not consumed on failure
        self.assertEqual(sidecar.read_bytes(), before)  # untouched
        self.assertTrue(asset.exists())  # not renamed
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])

    # ── Pointer-only sources (TOOLING §13b case (c), explicit stub flag) ──────

    def test_sidecar_no_companion_refused_without_flag(self) -> None:
        sidecar = self.archive / 'documents' / 'census' / 'ghost.notes.md'
        sidecar.write_text(
            '---\ntitle: Ghost Record\nexternal_links:\n  - url: https://county.test/courthouse\n'
            '---\nHeld at the county courthouse.\n',
            encoding='utf-8',
        )
        rc = self._run([str(sidecar)])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertTrue(sidecar.exists())  # not consumed on refusal
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])

    def test_sidecar_pointer_only_mints_source_with_no_files_block(self) -> None:
        sidecar = self.archive / 'documents' / 'census' / 'courthouse.notes.md'
        sidecar.write_text(
            '---\n'
            'title: Deed Held at County Courthouse\n'
            'asset_elsewhere: true\n'
            'citation: >\n'
            '  Deed book 12, page 4, Cass County courthouse.\n'
            'external_links:\n'
            '  - url: https://county.test/courthouse\n'
            '    accessed: "2024-01-01"\n'
            '---\n'
            'Recorded but not retrieved yet.\n',
            encoding='utf-8',
        )
        rc = self._run([str(sidecar)])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(sidecar.exists())  # stub consumed

        records = list((self.archive / 'sources').rglob('*_S-*.md'))
        self.assertEqual(len(records), 1)
        rec = read_record(records[0])
        self.assertEqual(rec['meta']['title'], 'Deed Held at County Courthouse')
        self.assertNotIn('files', rec['meta'])  # no asset -> no files: block
        links = rec['meta']['external_links']
        self.assertEqual(links[0]['url'], 'https://county.test/courthouse')
        self.assertIn('Recorded but not retrieved yet', records[0].read_text(encoding='utf-8'))

    def test_sidecar_pointer_only_dry_run_writes_nothing(self) -> None:
        sidecar = self.archive / 'documents' / 'census' / 'preview.notes.md'
        sidecar.write_text(
            '---\nasset_elsewhere: true\nexternal_links:\n  - url: https://x.test/y\n'
            '---\nbody\n',
            encoding='utf-8',
        )
        rc = self._run([str(sidecar), '--dry-run'])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertTrue(sidecar.exists())
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])

    def test_sidecar_pointer_only_refused_without_external_links(self) -> None:
        sidecar = self.archive / 'documents' / 'census' / 'empty.notes.md'
        sidecar.write_text('---\nasset_elsewhere: true\n---\nbody\n', encoding='utf-8')
        rc = self._run([str(sidecar)])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertTrue(sidecar.exists())
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])

    def test_sidecar_pointer_only_accepts_capture_path_stub(self) -> None:
        # P2 codex finding (PR #30): a `fha capture --path` stub sets
        # asset_elsewhere + asset_path/asset_path_absolute but has no
        # external_links, so it used to be permanently unprocessable without
        # a hand-edit. It must now mint like any other pointer-only source -
        # asset_path (the human's own shorthand) folded into provenance, but
        # asset_path_absolute (a machine-specific path) never written into
        # the committed record.
        sidecar = self.archive / 'documents' / 'census' / 'elsewhere.notes.md'
        sidecar.write_text(
            '---\n'
            'title: Grandma Wedding Photo\n'
            'asset_elsewhere: true\n'
            'asset_path: E:/family-photos/grandma-wedding.jpg\n'
            'asset_path_absolute: /mnt/e/family-photos/grandma-wedding.jpg\n'
            '---\n'
            'Still on the old external drive.\n',
            encoding='utf-8',
        )
        rc = self._run([str(sidecar)])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(sidecar.exists())  # stub consumed

        records = list((self.archive / 'sources').rglob('*_S-*.md'))
        self.assertEqual(len(records), 1)
        rec = read_record(records[0])
        self.assertEqual(rec['meta']['title'], 'Grandma Wedding Photo')
        self.assertNotIn('files', rec['meta'])
        self.assertNotIn('external_links', rec['meta'])
        self.assertIn('E:/family-photos/grandma-wedding.jpg', rec['meta']['provenance'])
        # The machine-specific absolute path never lands in the committed record.
        raw = records[0].read_text(encoding='utf-8')
        self.assertNotIn('/mnt/e/family-photos', raw)

    def test_sidecar_pointer_only_asset_path_appends_to_existing_provenance(self) -> None:
        sidecar = self.archive / 'documents' / 'census' / 'elsewhere-note.notes.md'
        sidecar.write_text(
            '---\n'
            'asset_elsewhere: true\n'
            'asset_path: on Dad\'s laptop\n'
            'provenance: Mentioned in Aunt Sue\'s 2019 email.\n'
            '---\nbody\n',
            encoding='utf-8',
        )
        rc = self._run([str(sidecar)])
        self.assertEqual(rc, EXIT_CLEAN)
        rec = read_record(next((self.archive / 'sources').rglob('*_S-*.md')))
        self.assertIn("Mentioned in Aunt Sue's 2019 email.", rec['meta']['provenance'])
        self.assertIn("on Dad's laptop", rec['meta']['provenance'])

    def test_sidecar_pointer_only_honors_cli_overrides(self) -> None:
        sidecar = self.archive / 'documents' / 'census' / 'override.notes.md'
        sidecar.write_text(
            '---\n'
            'title: Sidecar Title\n'
            'asset_elsewhere: true\n'
            'external_links:\n'
            '  - url: https://county.test/courthouse\n'
            '---\nbody\n',
            encoding='utf-8',
        )
        rc = self._run([str(sidecar), '--type', 'newspaper', '--title', 'CLI Title',
                         '--slug', 'cli-slug'])
        self.assertEqual(rc, EXIT_CLEAN)

        record = next((self.archive / 'sources' / 'newspaper').glob('cli-slug_S-*.md'))
        rec = read_record(record)
        self.assertEqual(rec['meta']['title'], 'CLI Title')

    def test_sidecar_pointer_only_rejects_more(self) -> None:
        sidecar = self.archive / 'documents' / 'census' / 'no-more.notes.md'
        sidecar.write_text(
            '---\nasset_elsewhere: true\nexternal_links:\n  - url: https://x.test/y\n'
            '---\nbody\n',
            encoding='utf-8',
        )
        extra = self.tmp / 'extra.txt'
        extra.write_text('x', encoding='utf-8')

        rc = self._run([str(sidecar), '--more', str(extra), 'attachment'])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertTrue(sidecar.exists())  # not consumed on refusal
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])

    def test_sidecar_pointer_only_dna_always_restricted(self) -> None:
        sidecar = self.archive / 'documents' / 'census' / 'dna-pointer.notes.md'
        sidecar.write_text(
            '---\nasset_elsewhere: true\n'
            'external_links:\n  - url: https://dna.test/kit\n'
            '---\nbody\n',
            encoding='utf-8',
        )
        rc = self._run([str(sidecar), '--type', 'dna'])
        self.assertEqual(rc, EXIT_CLEAN)

        record = next((self.archive / 'sources' / 'dna').glob('*_S-*.md'))
        rec = read_record(record)
        self.assertTrue(rec['meta']['restricted'])

    def test_sidecar_pointer_only_normalizes_month_name_source_date(self) -> None:
        sidecar = self.archive / 'documents' / 'census' / 'month-date.notes.md'
        sidecar.write_text(
            '---\nasset_elsewhere: true\nsource_date: June 1880\n'
            'external_links:\n  - url: https://x.test/y\n'
            '---\nbody\n',
            encoding='utf-8',
        )
        rc = self._run([str(sidecar)])
        self.assertEqual(rc, EXIT_CLEAN)
        record = next((self.archive / 'sources' / 'other').glob('*_S-*.md'))
        self.assertEqual(read_record(record)['meta']['source_date'], '1880-06')

    def test_document_normalizes_month_name_sidecar_source_date(self) -> None:
        asset = self.archive / 'documents' / 'census' / 'with-month-date.txt'
        asset.write_text('x', encoding='utf-8')
        sidecar = self.archive / 'documents' / 'census' / 'with-month-date.notes.md'
        sidecar.write_text('---\nsource_date: June 1880\n---\nbody\n', encoding='utf-8')

        rc = self._run([str(asset)])
        self.assertEqual(rc, EXIT_CLEAN)
        record = next((self.archive / 'sources' / 'other').glob('*_S-*.md'))
        self.assertEqual(read_record(record)['meta']['source_date'], '1880-06')

    def test_sidecar_people_hint_surfaces_in_notes(self) -> None:
        asset = self.archive / 'documents' / 'census' / 'people-hint.txt'
        asset.write_text('x', encoding='utf-8')
        sidecar = self.archive / 'documents' / 'census' / 'people-hint.notes.md'
        sidecar.write_text(
            '---\ntitle: Hint Test\npeople:\n  - Calvin Hartley\n  - Edith Hartley\n'
            '---\nPage text.\n',
            encoding='utf-8',
        )
        rc = self._run([str(asset)])
        self.assertEqual(rc, EXIT_CLEAN)
        record = next((self.archive / 'sources').rglob('*_S-*.md'))
        text = record.read_text(encoding='utf-8')
        self.assertIn('Calvin Hartley', text)
        self.assertIn('Edith Hartley', text)
        self.assertIn('unreconciled', text)

    def test_slugify(self) -> None:
        self.assertEqual(process._slugify('Fairview, Kansas! 1880'), 'fairview-kansas-1880')
        self.assertEqual(process._slugify('   '), 'source')
        self.assertEqual(
            process._derive_slug(None, 'A Title', Path('scan.jpg')), 'a-title')
        self.assertEqual(
            process._derive_slug(None, None, Path('1880-census.pdf')), '1880-census')

    def test_missing_file(self) -> None:
        rc = self._run([str(self.archive / 'documents' / 'nope.txt')])
        self.assertEqual(rc, EXIT_ERRORS)

    # ── M7.2b --people flag ────────────────────────────────────────────────────

    def test_photo_people_embeds_pid_keywords_and_populates_record(self) -> None:
        store = self._install_photo_store()
        self._write_person('P-de957bcda1')
        self._write_person('P-ab3c8f0e12', 'hartley__second')
        photo = self.archive / 'photos' / '1880' / 'family.jpg'
        photo.write_bytes(b'\xff\xd8\xff')

        rc = self._run([str(photo), '--people', 'P-de957bcda1,P-ab3c8f0e12'])
        self.assertEqual(rc, EXIT_CLEAN)

        kws = store.read(photo)
        self.assertIn('P-de957bcda1', kws)
        self.assertIn('P-ab3c8f0e12', kws)
        self.assertTrue(any(kw.startswith('SOURCE: S-') for kw in kws))

        record = next((self.archive / 'sources' / 'photos').glob('*_S-*.md'))
        rec = read_record(record)
        people = rec['meta'].get('people', [])
        self.assertIn('P-de957bcda1', people)
        self.assertIn('P-ab3c8f0e12', people)

    def test_photo_people_dry_run_shows_people_without_writing(self) -> None:
        store = self._install_photo_store()
        self._write_person('P-de957bcda1')
        photo = self.archive / 'photos' / '1880' / 'preview-people.jpg'
        photo.write_bytes(b'\xff\xd8\xff')

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self._run([str(photo), '--people', 'P-de957bcda1', '--dry-run'])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('P-de957bcda1', out.getvalue())
        self.assertEqual(store.read(photo), [])
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])

    def test_photo_people_rollback_removes_pid_keywords_on_scaffold_failure(self) -> None:
        """If record write fails after embedding, both SOURCE and P-id keywords roll back."""
        store = self._install_photo_store()
        self._write_person('P-de957bcda1')
        photo = self.archive / 'photos' / '1880' / 'rollback.jpg'
        photo.write_bytes(b'\xff\xd8\xff')

        # Block the record dir by placing a file at sources/photos so mkdir fails.
        (self.archive / 'sources' / 'photos').write_text('blocker', encoding='utf-8')

        rc = self._run([str(photo), '--people', 'P-de957bcda1'])
        self.assertEqual(rc, EXIT_FAILURE)
        self.assertEqual(store.read(photo), [])  # SOURCE and P-id keywords rolled back

    def test_photo_people_invalid_pid_rejected_before_writes(self) -> None:
        store = self._install_photo_store()
        photo = self.archive / 'photos' / '1880' / 'bad-pid.jpg'
        photo.write_bytes(b'\xff\xd8\xff')

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = self._run([str(photo), '--people', 'NotAnId'])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('NotAnId', err.getvalue())
        self.assertIn('P-id', err.getvalue())
        self.assertEqual(store.read(photo), [])  # no writes attempted

    def test_photo_people_unknown_valid_pid_rejected_before_writes(self) -> None:
        store = self._install_photo_store()
        photo = self.archive / 'photos' / '1880' / 'unknown-person.jpg'
        photo.write_bytes(b'\xff\xd8\xff')

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = self._run([str(photo), '--people', 'P-de957bcda1'])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('not a known person', err.getvalue())
        self.assertEqual(store.read(photo), [])

    def test_photo_people_rejected_with_more(self) -> None:
        store = self._install_photo_store()
        self._write_person('P-de957bcda1')
        photo = self.archive / 'photos' / '1880' / 'existing.jpg'
        extra = self.archive / 'photos' / '1880' / 'existing-back.jpg'
        photo.write_bytes(b'\xff\xd8\xff')
        extra.write_bytes(b'\xff\xd8\xff')
        store.keywords[str(photo)] = ['SOURCE: S-aaaaaaaaaa']

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = self._run([str(photo), '--people', 'P-de957bcda1', '--more', str(extra), 'back'])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('--more', err.getvalue())

    def test_photo_people_rejected_for_document(self) -> None:
        self._write_person('P-de957bcda1')
        doc = self.archive / 'documents' / 'census' / 'letter.txt'
        doc.write_text('x', encoding='utf-8')

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = self._run([str(doc), '--people', 'P-de957bcda1'])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('photo', err.getvalue().lower())

    # ── M7.3 variation detection (single photo) ────────────────────────────────

    def _make_pair(self) -> tuple[Path, Path]:
        d = self.archive / 'photos' / '1880'
        front = d / 'portrait_1880.jpg'
        back = d / 'portrait_1880-back.jpg'
        front.write_bytes(b'\xff\xd8\xff')
        back.write_bytes(b'\xff\xd8\xff')
        return front, back

    def test_photo_siblings_detected_by_base_id(self) -> None:
        front, back = self._make_pair()
        # An unrelated photo in the same dir must not join the group.
        (self.archive / 'photos' / '1880' / 'family_reunion.jpg').write_bytes(b'\xff\xd8\xff')
        sibs = process._photo_variation_siblings(front)
        self.assertEqual([p.name for p in sibs],
                         ['portrait_1880-back.jpg', 'portrait_1880.jpg'])

    def test_photo_siblings_always_include_the_file_itself(self) -> None:
        # The file being processed is a member of its own group even when the
        # directory listing cannot yield it: a dry-run inbox relocation hands
        # in a not-yet-existing destination, and an odd-extension photo (a
        # .webp filed under the photos root) fails the extension filter. An
        # empty result used to crash select_variation_primary downstream.
        d = self.archive / 'photos' / '1880'
        (d / 'portrait_1880-back.jpg').write_bytes(b'\xff\xd8\xff')
        virtual = d / 'portrait_1880.jpg'  # never written to disk
        self.assertEqual(
            [p.name for p in process._photo_variation_siblings(virtual)],
            ['portrait_1880-back.jpg', 'portrait_1880.jpg'],
        )
        odd = d / 'ferrotype.webp'
        odd.write_bytes(b'RIFF')
        self.assertEqual(process._photo_variation_siblings(odd), [odd])

    def test_photo_odd_extension_under_photos_root_processes_cleanly(self) -> None:
        # classify_asset calls a photos-root .webp a photo by location, but the
        # variation scan's extension filter used to drop the file from its own
        # group and the empty set crashed even on a live run. It must process
        # as a normal single photo: keyword embedded, record scaffolded, never
        # renamed.
        store = self._install_photo_store()
        photo = self.archive / 'photos' / '1880' / 'ferrotype.webp'
        photo.write_bytes(b'RIFF')

        rc = self._run([str(photo)])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertTrue(photo.exists())  # NEVER renamed
        self.assertEqual(len(store.read(photo)), 1)
        records = list((self.archive / 'sources' / 'photos').glob('ferrotype_S-*.md'))
        self.assertEqual(len(records), 1)

    def test_variation_one_makes_single_record_with_two_files(self) -> None:
        store = self._install_photo_store()
        front, back = self._make_pair()
        self._install_prompt('one')

        rc = self._run([str(front)])
        self.assertEqual(rc, EXIT_CLEAN)
        # Neither photo renamed; both carry the same single S-id.
        self.assertTrue(front.exists() and back.exists())
        self.assertEqual(len(store.read(front)), 1)
        self.assertEqual(store.read(front), store.read(back))

        records = list((self.archive / 'sources' / 'photos').glob('*_S-*.md'))
        self.assertEqual(len(records), 1)
        files = read_record(records[0])['meta']['files']
        self.assertEqual(len(files), 2)
        self.assertEqual(files[0]['role'], 'primary')
        self.assertEqual(files[0]['is_primary'], 'true')
        self.assertTrue(files[0]['file'].endswith('portrait_1880.jpg'))
        self.assertEqual(files[1]['role'], 'back')
        self.assertTrue(files[1]['file'].endswith('portrait_1880-back.jpg'))

    def test_variation_separate_makes_two_records(self) -> None:
        store = self._install_photo_store()
        front, back = self._make_pair()
        self._install_prompt('separate')

        rc = self._run([str(front)])
        self.assertEqual(rc, EXIT_CLEAN)
        records = list((self.archive / 'sources' / 'photos').glob('*_S-*.md'))
        self.assertEqual(len(records), 2)
        # Two distinct S-ids, one per photo.
        self.assertNotEqual(store.read(front)[0], store.read(back)[0])

    def test_variation_skip_writes_nothing(self) -> None:
        store = self._install_photo_store()
        front, back = self._make_pair()
        self._install_prompt('skip')

        rc = self._run([str(front)])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(store.read(front), [])
        self.assertEqual(store.read(back), [])
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])

    def test_variation_unrecognized_answer_is_safe_skip(self) -> None:
        store = self._install_photo_store()
        front, _ = self._make_pair()
        self._install_prompt('maybe?')
        rc = self._run([str(front)])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])

    def test_variation_group_dry_run_writes_nothing(self) -> None:
        store = self._install_photo_store()
        front, back = self._make_pair()
        self._install_prompt('one')
        rc = self._run([str(front), '--dry-run'])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(store.read(front), [])
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])

    def test_variation_group_refuses_partly_processed_set(self) -> None:
        store = self._install_photo_store()
        front, back = self._make_pair()
        store.keywords[str(back)] = ['SOURCE: S-aaaaaaaaaa']
        self._install_prompt('one')
        rc = self._run([str(front)])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertEqual(store.read(front), [])  # nothing minted onto the set
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])

    def test_variation_group_rolls_back_on_record_failure(self) -> None:
        store = self._install_photo_store()
        front, back = self._make_pair()
        self._install_prompt('one')
        # Block the record dir so the scaffold write fails after keyword embeds.
        (self.archive / 'sources' / 'photos').write_text('blocker', encoding='utf-8')
        rc = self._run([str(front)])
        self.assertEqual(rc, EXIT_FAILURE)
        # Both keyword writes rolled back.
        self.assertEqual(store.read(front), [])
        self.assertEqual(store.read(back), [])

    # ── M7.3 folder triage ─────────────────────────────────────────────────────

    def test_folder_triage_processes_selected_group_as_one(self) -> None:
        store = self._install_photo_store()
        d = self.archive / 'photos' / '1880'
        front, back = self._make_pair()
        reunion = d / 'family_reunion.jpg'
        reunion.write_bytes(b'\xff\xd8\xff')
        # Two groups: the portrait pair and the lone reunion photo. Select group
        # holding the pair, then answer the variation prompt with "one".
        self._install_prompt('all', 'one', 'one')

        rc = self._run([str(d)])
        self.assertEqual(rc, EXIT_CLEAN)
        # The pair became one 2-file record; the reunion became its own record.
        records = list((self.archive / 'sources' / 'photos').glob('*_S-*.md'))
        self.assertEqual(len(records), 2)
        self.assertEqual(len(store.read(front)), 1)
        self.assertEqual(store.read(front), store.read(back))
        self.assertEqual(len(store.read(reunion)), 1)

    def test_folder_triage_blank_selection_writes_nothing(self) -> None:
        store = self._install_photo_store()
        d = self.archive / 'photos' / '1880'
        self._make_pair()
        self._install_prompt('')  # blank → nothing selected
        rc = self._run([str(d)])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])

    def test_folder_with_no_photos_is_clean_noop(self) -> None:
        rc = self._run([str(self.archive / 'documents' / 'census')])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])

    # ── M7.4 bundle folder dissolution ─────────────────────────────────────────

    def _make_bundle(self) -> Path:
        # Copy the committed sample bundle (the one the M7.4 done-criteria names)
        # into the throwaway archive's inbox, so the fixture is exercised but the
        # dissolving run only ever deletes the temp copy.
        fixture = ROOT / 'tests' / 'fixtures' / 'bundle-folder'
        bundle = self.archive / 'inbox' / 'reunion-bundle'
        shutil.copytree(fixture, bundle)
        return bundle

    def test_bundle_dissolves_into_one_source(self) -> None:
        store = self._install_photo_store()
        bundle = self._make_bundle()

        rc = self._run([str(bundle)])
        self.assertEqual(rc, EXIT_CLEAN)
        # Bundle folder dissolved.
        self.assertFalse(bundle.exists())
        # One record covering both assets.
        records = list((self.archive / 'sources' / 'photos').glob('*_S-*.md'))
        self.assertEqual(len(records), 1)
        rec = read_record(records[0])
        self.assertEqual(rec['meta']['title'], 'The Reunion')
        files = rec['meta']['files']
        self.assertEqual(len(files), 2)
        self.assertIn('A reunion snapshot', records[0].read_text(encoding='utf-8'))

        # Photo moved to the photos root (kept its name) and carries SOURCE.
        moved_photo = self.archive / 'photos' / 'reunion.jpg'
        self.assertTrue(moved_photo.exists())
        self.assertEqual(len(store.read(moved_photo)), 1)
        # Document renamed + filed under the documents root (same plural subdir
        # mapping the photo record uses).
        docs = list((self.archive / 'documents' / 'photos').glob('caption*_S-*.txt'))
        self.assertEqual(len(docs), 1)

    def test_bundle_refuses_already_processed_photo_before_moves(self) -> None:
        store = self._install_photo_store()
        bundle = self._make_bundle()
        photo = bundle / 'reunion.jpg'
        store.keywords[str(photo)] = ['SOURCE: S-aaaaaaaaaa']

        rc = self._run([str(bundle)])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertTrue(bundle.exists())
        self.assertTrue((bundle / 'reunion.jpg').exists())
        self.assertTrue((bundle / 'caption.txt').exists())
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])
        self.assertFalse((self.archive / 'photos' / 'reunion.jpg').exists())
        self.assertEqual(list((self.archive / 'documents' / 'photos').glob('*_S-*.txt')), [])

    def test_bundle_includes_notes_named_assets(self) -> None:
        bundle = self.archive / 'inbox' / 'interview-bundle'
        bundle.mkdir(parents=True)
        (bundle / 'notes.md').write_text(
            '---\ntitle: Interview Packet\nsource_type: interview\n---\nStub notes.\n',
            encoding='utf-8',
        )
        (bundle / 'interview.mp3').write_bytes(b'audio')
        (bundle / 'interview.notes.md').write_text('transcript-ish notes\n', encoding='utf-8')

        rc = self._run([str(bundle)])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(bundle.exists())
        record = next((self.archive / 'sources' / 'interview').glob('*_S-*.md'))
        files = read_record(record)['meta']['files']
        self.assertEqual(len(files), 2)
        filed = [f['file'] for f in files]
        self.assertTrue(any(path.endswith('.mp3') for path in filed))
        self.assertTrue(any(path.endswith('.md') for path in filed))

    def test_bundle_uses_role_hints_from_notes_frontmatter(self) -> None:
        bundle = self.archive / 'inbox' / 'recording-bundle'
        bundle.mkdir(parents=True)
        (bundle / 'notes.md').write_text(
            '---\n'
            'title: Oral History\n'
            'source_type: interview\n'
            'roles:\n'
            '  oral-history.mp3: recording\n'
            'files:\n'
            '  - file: transcript.txt\n'
            '    role: transcript\n'
            '    is_primary: true\n'
            '---\n'
            'Interview notes.\n',
            encoding='utf-8',
        )
        (bundle / 'oral-history.mp3').write_bytes(b'audio')
        (bundle / 'transcript.txt').write_text('transcript', encoding='utf-8')

        rc = self._run([str(bundle)])
        self.assertEqual(rc, EXIT_CLEAN)
        record = next((self.archive / 'sources' / 'interview').glob('*_S-*.md'))
        files = read_record(record)['meta']['files']
        by_original = {item['original_filename']: item for item in files}
        self.assertEqual(by_original['oral-history.mp3']['role'], 'recording')
        self.assertTrue(by_original['oral-history.mp3']['file'].endswith('-recording_' + record.stem.split('_')[-1] + '.mp3'))
        self.assertEqual(by_original['transcript.txt']['role'], 'transcript')
        self.assertEqual(by_original['transcript.txt']['is_primary'], 'true')
        self.assertTrue(by_original['transcript.txt']['file'].endswith('-transcript_' + record.stem.split('_')[-1] + '.txt'))

    def test_bundle_document_filename_includes_copy_hint(self) -> None:
        bundle = self.archive / 'inbox' / 'translation-bundle'
        bundle.mkdir(parents=True)
        (bundle / 'notes.md').write_text(
            '---\n'
            'title: Land Deed\n'
            'source_type: land-record\n'
            'roles:\n'
            '  deed-translation.txt: translation\n'
            'files:\n'
            '  - file: deed-translation.txt\n'
            '    role: translation\n'
            '    copy: b\n'
            '---\n'
            'Deed notes.\n',
            encoding='utf-8',
        )
        (bundle / 'deed-translation.txt').write_text('translation text', encoding='utf-8')

        rc = self._run([str(bundle)])
        self.assertEqual(rc, EXIT_CLEAN)
        record = next((self.archive / 'sources' / 'land-record').glob('*_S-*.md'))
        files = read_record(record)['meta']['files']
        sid = record.stem.split('_')[-1]
        self.assertEqual(files[0]['copy'], 'b')
        self.assertTrue(files[0]['file'].endswith(f'-b-translation_{sid}.txt'))

    def test_plain_photo_only_bundle_infers_photo_source_type(self) -> None:
        store = self._install_photo_store()
        bundle = self.archive / 'inbox' / 'grandma-rose-portrait'
        bundle.mkdir(parents=True)
        (bundle / 'notes.md').write_text('Grandma Rose portrait, front and back.\n', encoding='utf-8')
        (bundle / 'front.jpg').write_bytes(b'\xff\xd8\xff')
        (bundle / 'back.jpg').write_bytes(b'\xff\xd8\xff')

        rc = self._run([str(bundle)])
        self.assertEqual(rc, EXIT_CLEAN)

        record = next((self.archive / 'sources' / 'photos').glob('*_S-*.md'))
        rec = read_record(record)
        self.assertEqual(rec['meta']['source_type'], 'photo')
        self.assertTrue((self.archive / 'photos' / 'front.jpg').exists())
        self.assertTrue((self.archive / 'photos' / 'back.jpg').exists())
        self.assertEqual(len(store.read(self.archive / 'photos' / 'front.jpg')), 1)

    def test_bundle_refuses_role_hint_for_missing_file(self) -> None:
        bundle = self.archive / 'inbox' / 'missing-hint-bundle'
        bundle.mkdir(parents=True)
        (bundle / 'notes.md').write_text(
            '---\nroles:\n  missing.txt: transcript\n---\nnotes\n',
            encoding='utf-8',
        )
        (bundle / 'actual.txt').write_text('x', encoding='utf-8')

        rc = self._run([str(bundle)])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertTrue((bundle / 'notes.md').exists())
        self.assertTrue((bundle / 'actual.txt').exists())
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])

    def test_bundle_refuses_subfolders_without_touching_assets(self) -> None:
        bundle = self._make_bundle()
        (bundle / 'nested').mkdir()

        rc = self._run([str(bundle)])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertTrue((bundle / 'notes.md').exists())
        self.assertTrue((bundle / 'reunion.jpg').exists())
        self.assertTrue((bundle / 'caption.txt').exists())
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])

    def test_bundle_restores_notes_when_final_rmdir_fails(self) -> None:
        store = self._install_photo_store()
        bundle = self._make_bundle()
        original_notes = (bundle / 'notes.md').read_text(encoding='utf-8')
        original_rmdir = process.Path.rmdir

        def fail_for_bundle(path: Path) -> None:
            if path == bundle:
                raise OSError('simulated rmdir failure')
            original_rmdir(path)

        process.Path.rmdir = fail_for_bundle
        try:
            rc = self._run([str(bundle)])
        finally:
            process.Path.rmdir = original_rmdir

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertTrue(bundle.exists())
        self.assertEqual((bundle / 'notes.md').read_text(encoding='utf-8'), original_notes)
        self.assertTrue((bundle / 'reunion.jpg').exists())
        self.assertTrue((bundle / 'caption.txt').exists())
        self.assertEqual(store.read(self.archive / 'photos' / 'reunion.jpg'), [])
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])

    def test_bundle_dry_run_leaves_folder_intact(self) -> None:
        store = self._install_photo_store()
        bundle = self._make_bundle()
        rc = self._run([str(bundle), '--dry-run'])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertTrue((bundle / 'notes.md').exists())
        self.assertTrue((bundle / 'reunion.jpg').exists())
        self.assertEqual(store.read(bundle / 'reunion.jpg'), [])
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])

    def test_bundle_with_only_notes_refused(self) -> None:
        bundle = self.archive / 'inbox' / 'empty-bundle'
        bundle.mkdir(parents=True)
        (bundle / 'notes.md').write_text('---\ntitle: Nothing\n---\njust notes\n', encoding='utf-8')
        rc = self._run([str(bundle)])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertTrue(bundle.exists())  # not dissolved

    def test_more_rejects_folder(self) -> None:
        d = self.archive / 'photos' / '1880'
        self._make_pair()
        extra = self.archive / 'photos' / '1880' / 'extra.jpg'
        extra.write_bytes(b'\xff\xd8\xff')
        rc = self._run([str(d), '--more', str(extra), 'back'])
        self.assertEqual(rc, EXIT_ERRORS)


class InputPathResolutionTestCase(unittest.TestCase):
    """The forgiving FILE/--more lookup: as typed first, then under --root.

    The cheat sheet tells the user to run commands from the workshop folder
    (the PARENT of the archive) and to name the file as it reads inside the
    archive ("inbox/scan.jpg") - a path that misses relative to the CWD. These
    tests pin the contract of `_resolve_input_file`: a CWD hit always wins
    unchanged, a relative CWD miss retries under the resolved archive root, a
    double miss names both searched locations plus the next step, and the
    retry does not weaken dry-run's zero-mutation promise.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)  # stands in for the workshop folder
        self.archive = _make_archive(self.tmp)
        (self.archive / 'inbox').mkdir()
        self._old_cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self) -> None:
        # chdir back BEFORE cleanup: Windows cannot delete the current directory.
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def _run(self, argv: list[str]) -> int:
        return process._standalone_main(argv + ['--root', str(self.archive)])

    def test_cwd_miss_retries_under_archive_root(self) -> None:
        # The cheat-sheet invocation itself: run from the workshop folder and
        # name the file the way it reads inside the archive.
        asset = self.archive / 'inbox' / 'scan-note.txt'
        asset.write_text('inbox body', encoding='utf-8')

        with contextlib.redirect_stdout(io.StringIO()):
            rc = self._run(['inbox/scan-note.txt'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(asset.exists())  # relocated out of inbox and renamed
        renamed = list((self.archive / 'documents' / 'other').glob('scan-note_S-*.txt'))
        self.assertEqual(len(renamed), 1)

    def test_cwd_hit_wins_over_archive_root_candidate(self) -> None:
        # The same relative path exists BOTH at the CWD and inside the archive;
        # the path as typed (CWD) must win. The workshop copy sits outside the
        # archive's asset roots, so process refuses it - and that distinctive
        # refusal is the proof of precedence: had the retry hijacked the
        # lookup, the archive copy would have processed cleanly instead.
        (self.tmp / 'inbox').mkdir()
        cwd_copy = self.tmp / 'inbox' / 'note.txt'
        cwd_copy.write_text('workshop copy', encoding='utf-8')
        root_copy = self.archive / 'inbox' / 'note.txt'
        root_copy.write_text('archive copy', encoding='utf-8')

        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = self._run(['inbox/note.txt'])

        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('not under the configured documents root', err.getvalue())
        # Neither copy consumed: the CWD file refused in place, the archive
        # copy never relocated or renamed.
        self.assertTrue(cwd_copy.exists())
        self.assertTrue(root_copy.exists())
        self.assertEqual(list((self.archive / 'documents').glob('note*')), [])

    def test_relative_path_from_inside_the_archive_unchanged(self) -> None:
        # Running from inside the archive itself: the typed path hits at the
        # CWD and processes exactly as it did before the retry existed.
        os.chdir(self.archive)
        asset = self.archive / 'inbox' / 'photo-note.txt'
        asset.write_text('body', encoding='utf-8')

        with contextlib.redirect_stdout(io.StringIO()):
            rc = self._run(['inbox/photo-note.txt'])

        self.assertEqual(rc, EXIT_CLEAN)
        renamed = list((self.archive / 'documents' / 'other').glob('photo-note_S-*.txt'))
        self.assertEqual(len(renamed), 1)

    def test_both_miss_error_names_both_searched_locations(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = self._run(['inbox/ghost.txt'])

        self.assertEqual(rc, EXIT_ERRORS)
        text = err.getvalue()
        self.assertIn('file not found: inbox/ghost.txt', text)
        # Both looks are named with their full resolved paths...
        self.assertIn(str(Path('inbox/ghost.txt').resolve()), text)
        self.assertIn(str((self.archive / 'inbox' / 'ghost.txt').resolve()), text)
        # ...and the message carries a plain next step, not a dead end.
        self.assertIn('inside your archive folder', text)
        self.assertNotIn('Traceback', text)

    def test_root_retry_dry_run_writes_nothing(self) -> None:
        asset = self.archive / 'inbox' / 'scan-note.txt'
        asset.write_text('inbox body', encoding='utf-8')

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self._run(['inbox/scan-note.txt', '--dry-run'])

        self.assertEqual(rc, EXIT_CLEAN)
        # The retry fed the preview (the plan names the inbox move), and the
        # preview stayed a preview: nothing moved, renamed, or scaffolded.
        self.assertIn('Would move scan-note.txt out of inbox/', out.getvalue())
        self.assertTrue(asset.exists())
        self.assertEqual(list((self.archive / 'documents').glob('*.txt')), [])
        self.assertEqual(list((self.archive / 'sources').rglob('*_S-*.md')), [])

    def test_more_file_shares_the_forgiving_lookup(self) -> None:
        # --more's file argument resolves through the same door: a both-ways
        # miss names the flag and both locations; a root-relative spelling
        # attaches cleanly from the workshop folder.
        page1 = self.archive / 'documents' / 'census' / 'page1.txt'
        page1.write_text('p1', encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self._run([str(page1), '--type', 'census']), EXIT_CLEAN)
        renamed = next((self.archive / 'documents' / 'census').glob('*_S-*.txt'))

        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = self._run([str(renamed), '--more', 'documents/census/ghost.txt', 'page-2'])
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('--more file not found: documents/census/ghost.txt', err.getvalue())
        self.assertIn('inside your archive folder', err.getvalue())

        page2 = self.archive / 'documents' / 'census' / 'page2.txt'
        page2.write_text('p2', encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()):
            rc = self._run([str(renamed), '--more', 'documents/census/page2.txt', 'page-2'])
        self.assertEqual(rc, EXIT_CLEAN)
        record = next((self.archive / 'sources' / 'census').glob('*_S-*.md'))
        self.assertEqual(len(read_record(record)['meta']['files']), 2)


class SourceIdOverrideTests(unittest.TestCase):
    """P2 codex finding (round 7, PR #30): `fha serve`'s process.file verb
    dry-run preview shows a real minted S-id (`_mint_one_source_id` mints on
    every call, by design - same as person.new/claim.new), but Apply used to
    call `run_process` again with no way to reuse it, drawing a second,
    DIFFERENT id - so the source actually created never matched the id the
    preview showed and the human approved. `_mint_one_source_id` now takes
    an optional `source_id` override (the same id-reuse pattern already
    fixed for person.new/claim.new), threaded through `process_document`,
    `process_photo`, and `process_pointer_only` - the three single-file
    branches `fha serve` can reach without an interactive prompt - and
    reported back through `run_process`'s `Result.data['source_id']` so a
    caller can round-trip a dry-run preview's id into the live Apply call.

    A plain `unittest.TestCase` (not `ProcessTestCase`) on purpose: that
    class's `setUp` doubles as the fixture AND already carries 100+ test_*
    methods of its own, so subclassing it here would silently re-run every
    one of them a second time under this class's name too.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive = _make_archive(Path(self._tmp.name))
        self._orig_read = process._run_exiftool_read_keywords
        self._orig_embed = process._run_exiftool_embed_source
        self._orig_remove = process._run_exiftool_remove_source

    def tearDown(self) -> None:
        process._run_exiftool_read_keywords = self._orig_read
        process._run_exiftool_embed_source = self._orig_embed
        process._run_exiftool_remove_source = self._orig_remove
        self._tmp.cleanup()

    def _install_photo_store(self) -> FakePhotoStore:
        store = FakePhotoStore()
        process._run_exiftool_read_keywords = store.read
        process._run_exiftool_embed_source = store.embed
        process._run_exiftool_remove_source = store.remove
        return store

    def _args(self, file_path, **overrides) -> argparse.Namespace:
        base = {
            'root': str(self.archive), 'file': str(file_path), 'source_type': None,
            'title': None, 'slug': None, 'source_date': None, 'more': None,
            'people': None, 'dry_run': False,
        }
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_document_reports_the_minted_source_id_in_result_data(self) -> None:
        original = self.archive / 'documents' / 'census' / 'report.txt'
        original.write_text('x', encoding='utf-8')
        result = process.run_process(self._args(original, source_type='census'))
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        sid = result.data['source_id']
        self.assertTrue(sid and sid.upper().startswith('S-'))
        record = next((self.archive / 'sources' / 'census').glob('*_S-*.md'))
        self.assertEqual(read_record(record)['meta']['id'].lower(), sid.lower())

    def test_document_apply_reuses_the_previewed_minted_source_id(self) -> None:
        original = self.archive / 'documents' / 'census' / 'letter.txt'
        original.write_text('x', encoding='utf-8')
        preview = process.run_process(self._args(original, source_type='census', dry_run=True))
        self.assertEqual(preview.exit_code, EXIT_CLEAN)
        previewed = preview.data['source_id']
        self.assertTrue(original.exists())  # dry run wrote nothing
        live = process.run_process(
            self._args(original, source_type='census', source_id=previewed))
        self.assertEqual(live.exit_code, EXIT_CLEAN)
        self.assertEqual(live.data['source_id'], previewed)
        record = next((self.archive / 'sources' / 'census').glob('*_S-*.md'))
        self.assertEqual(read_record(record)['meta']['id'].lower(), previewed.lower())

    def test_pointer_only_apply_reuses_the_previewed_minted_source_id(self) -> None:
        sidecar = self.archive / 'documents' / 'census' / 'courthouse.notes.md'
        sidecar.write_text(
            '---\nasset_elsewhere: true\nexternal_links:\n'
            '  - url: https://county.test/courthouse\n---\nbody\n',
            encoding='utf-8',
        )
        preview = process.run_process(self._args(sidecar, dry_run=True))
        self.assertEqual(preview.exit_code, EXIT_CLEAN)
        previewed = preview.data['source_id']
        self.assertTrue(sidecar.exists())  # dry run wrote nothing
        live = process.run_process(self._args(sidecar, source_id=previewed))
        self.assertEqual(live.exit_code, EXIT_CLEAN)
        self.assertEqual(live.data['source_id'], previewed)
        record = next((self.archive / 'sources').rglob('*_S-*.md'))
        self.assertEqual(read_record(record)['meta']['id'].lower(), previewed.lower())

    def test_photo_apply_reuses_the_previewed_minted_source_id(self) -> None:
        self._install_photo_store()
        photo = self.archive / 'photos' / '1880' / 'grandma.jpg'
        photo.write_bytes(b'\xff\xd8\xff')
        preview = process.run_process(self._args(photo, dry_run=True))
        self.assertEqual(preview.exit_code, EXIT_CLEAN)
        previewed = preview.data['source_id']
        live = process.run_process(self._args(photo, source_id=previewed))
        self.assertEqual(live.exit_code, EXIT_CLEAN)
        self.assertEqual(live.data['source_id'], previewed)
        record = next((self.archive / 'sources' / 'photos').glob('*_S-*.md'))
        self.assertEqual(read_record(record)['meta']['id'].lower(), previewed.lower())

    def test_source_id_override_rejects_a_malformed_id(self) -> None:
        with self.assertRaises(process.ProcessError):
            process._mint_one_source_id(self.archive, source_id='not-an-id')

    def test_source_id_override_rejects_the_wrong_id_type(self) -> None:
        with self.assertRaises(process.ProcessError):
            process._mint_one_source_id(self.archive, source_id='P-fa1234567b')

    def test_source_id_override_refuses_a_stale_preview_id_that_now_exists(self) -> None:
        # A colliding override (the archive changed since the preview that
        # minted it) must be refused, not silently reused.
        original = self.archive / 'documents' / 'census' / 'first.txt'
        original.write_text('x', encoding='utf-8')
        first = process.run_process(self._args(original, source_type='census'))
        self.assertEqual(first.exit_code, EXIT_CLEAN)
        taken = first.data['source_id']
        second = self.archive / 'documents' / 'census' / 'second.txt'
        second.write_text('y', encoding='utf-8')
        again = process.run_process(
            self._args(second, source_type='census', source_id=taken))
        self.assertEqual(again.exit_code, EXIT_ERRORS)
        self.assertTrue(second.exists())  # refused, not half-processed

    def test_no_override_still_mints_a_fresh_id_each_call(self) -> None:
        # The CLI never sends `source_id` (`_add_arguments` has no such
        # flag) - `_run_process` must fall back to a fresh mint per call
        # when the Namespace lacks the attribute entirely, exactly as
        # before this fix.
        one = self.archive / 'documents' / 'census' / 'one.txt'
        two = self.archive / 'documents' / 'census' / 'two.txt'
        one.write_text('1', encoding='utf-8')
        two.write_text('2', encoding='utf-8')
        r1 = process.run_process(self._args(one, source_type='census'))
        r2 = process.run_process(self._args(two, source_type='census'))
        self.assertEqual(r1.exit_code, EXIT_CLEAN)
        self.assertEqual(r2.exit_code, EXIT_CLEAN)
        self.assertNotEqual(r1.data['source_id'], r2.data['source_id'])

    def test_more_and_folder_modes_report_no_source_id(self) -> None:
        # Nothing is minted by --more (it attaches to an EXISTING source) or
        # by a bare folder/bundle dispatch (BUILD.md M7.3/M7.4) - `data`
        # must say so plainly (None) rather than leaking a stale value.
        page1 = self.archive / 'documents' / 'census' / 'page1.txt'
        page1.write_text('p1', encoding='utf-8')
        filed = process.run_process(self._args(page1, source_type='census'))
        self.assertEqual(filed.exit_code, EXIT_CLEAN)
        renamed = next((self.archive / 'documents' / 'census').glob('*_S-*.txt'))

        page2 = self.archive / 'documents' / 'census' / 'page2.txt'
        page2.write_text('p2', encoding='utf-8')
        more_result = process.run_process(
            self._args(renamed, more=[str(page2), 'page-2']))
        self.assertEqual(more_result.exit_code, EXIT_CLEAN)
        self.assertIsNone(more_result.data['source_id'])


class TriageDateSignalTests(unittest.TestCase):
    """The confident-date signal in folder triage (TOOLING §15b, SPEC §20).

    A photo's date is never the DATE: keyword body. The keyword states
    precision in letters ('Y!M!D!', 'Y~'); the digits come from the photo's
    EXIF DateTimeOriginal, and the two are paired by `_lib.resolve_photo_edtf`.
    Folder triage used to test the raw keyword body with `is_valid_edtf`, which
    no spec-conformant keyword can ever satisfy, so the signal fired for the
    retired digit form and nothing else - while `fha photoindex triage`, which
    resolves properly, scored the same photo a point higher.

    exiftool is not installed in this container, so each test hands the real
    row-to-signals mapping (`_photo_meta_from_row`) the JSON row the binary
    would have returned. That keeps the resolution under test rather than
    stubbed out.
    """

    def setUp(self) -> None:
        self._orig_read_meta = process._run_exiftool_read_meta
        self._orig_run_exiftool = photoindex._run_exiftool

    def tearDown(self) -> None:
        process._run_exiftool_read_meta = self._orig_read_meta
        photoindex._run_exiftool = self._orig_run_exiftool

    def _score(self, row: dict, name: str = 'portrait.jpg') -> tuple[int, list[str]]:
        """Score a one-photo group whose metadata is `row` (never touches disk)."""
        process._run_exiftool_read_meta = lambda _p: process._photo_meta_from_row(row)
        return process._score_photo_group([Path(name)])

    def test_letter_keyword_with_exif_date_scores_the_confident_date_signal(self) -> None:
        score, signals = self._score(
            {'Keywords': ['DATE: Y!M!D!'], 'DateTimeOriginal': '1942:11:25 10:00:00'})
        self.assertIn('date:Y!+', signals)
        self.assertEqual(score, 1)

        # A confident year on its own is still a confident date.
        self.assertIn('date:Y!+', self._score(
            {'Keywords': ['DATE: Y!'], 'DateTimeOriginal': '1955:08:04 12:00:00'})[1])
        # SPEC §20 calls 'Y!' and 'Y!M?D?' the same statement - an unknown
        # month is dropped, and what remains ('1955') carries no marker, so
        # this scores exactly like 'Y!'. `fha photoindex triage` agrees (see
        # the parity test below); the signal is "year-precise or finer".
        self.assertIn('date:Y!+', self._score(
            {'Subject': ['DATE: Y!M?'], 'DateTimeOriginal': '1955:08:04 12:00:00'})[1])

    def test_approximate_or_unaffirmed_precision_scores_no_date_signal(self) -> None:
        # '~' is a best guess, not a confident date; a component the pipeline
        # never affirmed ('Y', 'YMD', 'Y?') yields no date at all.
        for pattern in ('Y~', 'Y!M~', 'Y?', 'Y', 'YMD'):
            with self.subTest(pattern=pattern):
                score, signals = self._score(
                    {'Keywords': [f'DATE: {pattern}'],
                     'DateTimeOriginal': '1960:05:01 00:00:00'})
                self.assertNotIn('date:Y!+', signals)
                self.assertEqual(score, 0)

        # And the letter form alone is not a date: with no capture date in the
        # file there are no digits to resolve against.
        self.assertNotIn('date:Y!+', self._score({'Keywords': ['DATE: Y!M!D!']})[1])

    def test_retired_digit_keyword_forms_score_nothing(self) -> None:
        # The forms the archive owner ruled outside SPEC §20 on 2026-08-16.
        # The digit-plus-marker one is the form the old code scored on, so it
        # is the one that must now score zero.
        for body in ('1880', '1942!-11!-25!', '1942-11-25', '[..1900]', '188X'):
            with self.subTest(body=body):
                score, signals = self._score(
                    {'Keywords': [f'DATE: {body}'],
                     'DateTimeOriginal': '1880:01:01 00:00:00'})
                self.assertNotIn('date:Y!+', signals)
                self.assertEqual(score, 0)

    def test_both_tools_resolve_dates_through_the_same_lib_function(self) -> None:
        # Tools never import tools, so "the same rule" can only mean the same
        # `_lib` function. Re-adding a local copy in either tool - the drift
        # this whole area is recovering from - breaks this immediately.
        self.assertIs(process.resolve_photo_edtf, photoindex._resolve_photo_edtf)
        self.assertIs(process.edtf_confidence, photoindex._edtf_confidence)

    def test_triage_metadata_read_asks_exiftool_for_the_capture_date(self) -> None:
        # Without DateTimeOriginal in the request there is nothing for the
        # keyword's precision letters to resolve against, and every photo
        # comes back undated however it is keyworded.
        captured: dict = {}

        class _FakeProc:
            returncode = 0
            stdout = '[{}]'
            stderr = ''

        def fake_run(cmd, **_kwargs):
            captured['cmd'] = cmd
            return _FakeProc()

        real_run = process.subprocess.run
        process.subprocess.run = fake_run
        try:
            process._run_exiftool_read_meta(Path('portrait.jpg'))
        finally:
            process.subprocess.run = real_run
        self.assertIn('-DateTimeOriginal', captured['cmd'])

    def test_process_and_photoindex_rank_the_same_folder_the_same_way(self) -> None:
        # `fha process <folder>` and `fha photoindex triage` are documented as
        # ordering the same folder the same way. Score the same four photos
        # through both engines and require the scores AND signal lists to
        # match exactly - a comment claiming parity is what let the date
        # signal drift, so the parity is asserted instead of described.
        rows = {
            # Letter-form date + a bare P-id keyword; its back carries the
            # human caption, so the whole group scores 3+2+1+1.
            'portrait_1880.jpg': {
                'Keywords': ['DATE: Y!M!D!', 'P-de957bcda1'],
                'DateTimeOriginal': '1880:06:14 00:00:00',
            },
            # The back also carries a retired digit-form keyword, which both
            # tools must read as no date at all - the group's date point comes
            # from the front's letter form, and nothing here scores twice.
            'portrait_1880-back.jpg': {
                'Caption-Abstract': 'Written on the back: Margaret, aged nine',
                'Keywords': ['DATE: 1942!-11!-25!'],
                'DateTimeOriginal': '2010:05:06 07:08:09',
            },
            # Approximate date + an AI-only comment: no date point, -2.
            'wedding_1902.jpg': {
                'Keywords': ['DATE: Y~'],
                'UserComment': 'AI: a wedding party on a porch',
                'DateTimeOriginal': '1902:06:14 00:00:00',
            },
            # Confident year, unknown month - SPEC §20's 'Y!M?D?' row, which
            # resolves to a bare confident year and scores.
            'family_reunion.jpg': {
                'Keywords': ['DATE: Y!M?'],
                'DateTimeOriginal': '1955:08:04 12:00:00',
            },
        }

        with tempfile.TemporaryDirectory() as d:
            archive = Path(d) / 'photo-fixture'
            shutil.copytree(ROOT / 'tests' / 'fixtures' / 'photo-fixture', archive,
                            ignore=shutil.ignore_patterns('.cache'))
            cfg = {'roots': {'photos': 'photos'}}

            photoindex._run_exiftool = lambda paths: [
                {'SourceFile': str(p), **rows.get(p.name, {})} for p in paths
            ]
            photoindex.run_scan(archive, cfg)
            catalog_scores = {
                Path(c['path']).name: (c['score'], c['signals'])
                for c in photoindex.run_triage(archive, cfg, top=10)['candidates']
            }

            process._run_exiftool_read_meta = (
                lambda p: process._photo_meta_from_row(rows.get(p.name, {})))
            groups: dict[str, list[Path]] = {}
            for p in sorted((archive / 'photos').iterdir()):
                groups.setdefault(
                    grouping_stem(parse_media_filename(p.stem)), []).append(p)
            folder_scores = {}
            for members in groups.values():
                primary = select_variation_primary(
                    members, lambda q: parse_media_filename(q.stem))
                folder_scores[primary.name] = process._score_photo_group(members)

            self.assertEqual(folder_scores, catalog_scores)
            # Not vacuous: the date signal is actually in play on both sides.
            self.assertEqual(
                folder_scores['portrait_1880.jpg'],
                (7, ['caption', 'pid-keyword', 'date:Y!+', 'back-variant']))
            self.assertEqual(folder_scores['family_reunion.jpg'], (1, ['date:Y!+']))
            self.assertEqual(folder_scores['wedding_1902.jpg'], (-2, ['ai-only']))


class TypeFlagRoutingTests(unittest.TestCase):
    """`fha process --type` decides the root, not the file extension (issue #59).

    The regression these pin: a scanned census supplied as a `.jpg` was routed
    by extension into the family photo library and scaffolded `source_type:
    photo`, with `--type census` accepted and silently discarded. SPEC 12 calls
    the photos root "all photos" and the documents root "scans, clippings,
    recordings, transcripts", and SPEC 14's `source_type` vocabulary is a
    vocabulary of RECORDS - so a stated non-photo type names a record, and a
    record belongs in the documents root whatever its extension.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.archive = _make_archive(self.tmp)
        (self.archive / 'inbox').mkdir()
        self._orig_read = process._run_exiftool_read_keywords
        self._orig_embed = process._run_exiftool_embed_source
        self._orig_remove = process._run_exiftool_remove_source
        self.store = FakePhotoStore()
        process._run_exiftool_read_keywords = self.store.read
        process._run_exiftool_embed_source = self.store.embed
        process._run_exiftool_remove_source = self.store.remove

    def tearDown(self) -> None:
        process._run_exiftool_read_keywords = self._orig_read
        process._run_exiftool_embed_source = self._orig_embed
        process._run_exiftool_remove_source = self._orig_remove
        self._tmp.cleanup()

    def _run(self, argv: list[str]) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return process._standalone_main(argv + ['--root', str(self.archive)])

    def _run_captured(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = process._standalone_main(argv + ['--root', str(self.archive)])
        return rc, out.getvalue(), err.getvalue()

    @contextlib.contextmanager
    def _watch_renames(self):
        """Record every Path.rename destination for the duration of the block.

        Issue #59's acceptance criterion is that nothing is written into the
        photos root AT ANY POINT - a scan that transits the real Lightroom
        library and is moved out again is half the complaint, and an end-state
        assertion cannot see it. Every filing move in `fha process` goes
        through `Path.rename`, so recording its destinations catches a transit
        that a before/after snapshot would miss.
        """
        seen: list[Path] = []
        original = Path.rename

        def spy(self_path, target):
            seen.append(Path(target))
            return original(self_path, target)

        Path.rename = spy
        try:
            yield seen
        finally:
            Path.rename = original

    def _assert_photos_root_untouched(self, renames: list[Path]) -> None:
        photos_root = (self.archive / 'photos').resolve()
        for target in renames:
            self.assertFalse(
                process._is_under(target, photos_root),
                f'{target} was written into the photos root',
            )
        self.assertEqual(
            [p.name for p in (self.archive / 'photos').rglob('*') if p.is_file()],
            [], 'the photos root must hold no files')
        self.assertEqual(self.store.keywords, {},
                         'no photo keyword may be embedded on a document route')

    # -- the reported case -----------------------------------------------------

    def test_image_with_document_type_files_as_a_document(self) -> None:
        scan = self.archive / 'inbox' / '43290879-california-061582-0002.jpg'
        scan.write_bytes(b'jpegbytes')

        with self._watch_renames() as renames:
            rc = self._run([str(scan), '--type', 'census',
                            '--title', 'Jessie R. Boyd Household - 1950 Census',
                            '--date', '1950-04-17'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(scan.exists())
        filed = list((self.archive / 'documents' / 'census').glob('*_S-*.jpg'))
        self.assertEqual(len(filed), 1, 'the scan belongs under the documents root')
        records = list((self.archive / 'sources' / 'census').glob('*_S-*.md'))
        self.assertEqual(len(records), 1, 'the record belongs under sources/census/')
        self.assertEqual(read_record(records[0])['meta']['source_type'], 'census')
        self.assertFalse((self.archive / 'sources' / 'photos').exists())
        self._assert_photos_root_untouched(renames)

    def test_image_with_document_type_dry_run_previews_the_document_route(self) -> None:
        scan = self.archive / 'inbox' / 'census-page.jpg'
        scan.write_bytes(b'jpegbytes')

        with self._watch_renames() as renames:
            rc, out, _err = self._run_captured([str(scan), '--type', 'census', '--dry-run'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('documents', out)
        self.assertIn('sources/census/', out)
        self.assertTrue(scan.exists(), 'a dry run writes nothing')
        self._assert_photos_root_untouched(renames)

    # -- what must NOT change --------------------------------------------------

    def test_image_without_type_is_still_a_photo(self) -> None:
        photo = self.archive / 'inbox' / 'portrait.jpg'
        photo.write_bytes(b'jpegbytes')

        rc = self._run([str(photo)])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertTrue((self.archive / 'photos' / 'portrait.jpg').is_file())
        records = list((self.archive / 'sources' / 'photos').glob('*_S-*.md'))
        self.assertEqual(len(records), 1)
        self.assertEqual(read_record(records[0])['meta']['source_type'], 'photo')

    def test_document_type_on_a_non_image_is_unchanged(self) -> None:
        deed = self.archive / 'inbox' / 'deed.pdf'
        deed.write_bytes(b'%PDF-1.4')

        rc = self._run([str(deed), '--type', 'land-record'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(
            len(list((self.archive / 'documents' / 'land-record').glob('deed_S-*.pdf'))), 1)
        self.assertEqual(
            len(list((self.archive / 'sources' / 'land-record').glob('deed_S-*.md'))), 1)

    def test_photo_type_on_an_image_is_unchanged(self) -> None:
        photo = self.archive / 'inbox' / 'portrait.jpg'
        photo.write_bytes(b'jpegbytes')

        rc = self._run([str(photo), '--type', 'photo'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertTrue((self.archive / 'photos' / 'portrait.jpg').is_file())

    def test_unknown_type_on_an_image_is_refused(self) -> None:
        photo = self.archive / 'inbox' / 'portrait.jpg'
        photo.write_bytes(b'jpegbytes')

        with self._watch_renames() as renames:
            rc, _out, err = self._run_captured([str(photo), '--type', 'bogus'])

        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('census', err)
        self.assertNotIn('Traceback', err)
        self.assertTrue(photo.exists(), 'the asset stays in the inbox')
        self.assertEqual(list((self.archive / 'sources').rglob('*.md')), [])
        self._assert_photos_root_untouched(renames)

    # -- interaction with the sidecar hint ------------------------------------

    def test_explicit_type_beats_the_sidecar_hint(self) -> None:
        scan = self.archive / 'inbox' / 'page.jpg'
        scan.write_bytes(b'jpegbytes')
        sidecar = self.archive / 'inbox' / 'page.notes.md'
        sidecar.write_text('---\nsource_type: photo\n---\nFrom the album.\n',
                           encoding='utf-8')

        with self._watch_renames() as renames:
            rc = self._run([str(scan), '--type', 'newspaper'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(
            len(list((self.archive / 'documents' / 'newspaper').glob('page_S-*.jpg'))), 1)
        self.assertEqual(
            len(list((self.archive / 'sources' / 'newspaper').glob('*_S-*.md'))), 1)
        self._assert_photos_root_untouched(renames)

    # -- a file already living in the photo library ---------------------------

    def test_type_on_a_photos_root_file_types_the_record_and_never_moves_it(self) -> None:
        # A photos-root file is NEVER moved by `fha process` (SPEC 12.1) - only
        # `fha process refile` may carry one out, and only with a confirmation.
        # The flag is still honoured as far as the spec allows: the RECORD is
        # typed and filed as the human said, the file stays exactly where it
        # is, and the output names refile as the way to move it.
        scan = self.archive / 'photos' / '1880' / 'census-page.jpg'
        scan.write_bytes(b'jpegbytes')

        rc, out, _err = self._run_captured([str(scan), '--type', 'census'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertTrue(scan.is_file(), 'the photo library is never rearranged')
        records = list((self.archive / 'sources' / 'census').glob('*_S-*.md'))
        self.assertEqual(len(records), 1)
        self.assertEqual(read_record(records[0])['meta']['source_type'], 'census')
        self.assertIn('refile', out)

    # -- the folder modes honour it too ---------------------------------------

    def test_bundle_with_document_type_files_every_scan_as_a_document(self) -> None:
        # A bundle of census page scans: all .jpg, so the all-photos inference
        # would send the whole set to the photo library. `--type` outranks it.
        bundle = self.archive / 'inbox' / '1950-census-pages'
        bundle.mkdir()
        for name in ('page-1.jpg', 'page-2.jpg'):
            (bundle / name).write_bytes(b'jpegbytes')
        (bundle / 'notes.md').write_text(
            'Two pages of the 1950 census sheet.\n', encoding='utf-8')

        with self._watch_renames() as renames:
            rc = self._run([str(bundle), '--type', 'census'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(bundle.exists(), 'the bundle folder dissolves')
        filed = sorted(p.name for p in (self.archive / 'documents' / 'census').glob('*_S-*.jpg'))
        self.assertEqual(len(filed), 2, filed)
        records = list((self.archive / 'sources' / 'census').glob('*_S-*.md'))
        self.assertEqual(len(records), 1)
        self.assertEqual(read_record(records[0])['meta']['source_type'], 'census')
        self._assert_photos_root_untouched(renames)

    def test_bundle_type_outranks_the_notes_hint(self) -> None:
        bundle = self.archive / 'inbox' / 'clipping'
        bundle.mkdir()
        (bundle / 'scan.jpg').write_bytes(b'jpegbytes')
        (bundle / 'notes.md').write_text(
            '---\nsource_type: photo\n---\nFrom the album.\n', encoding='utf-8')

        with self._watch_renames() as renames:
            rc = self._run([str(bundle), '--type', 'newspaper'])

        self.assertEqual(rc, EXIT_CLEAN)
        records = list((self.archive / 'sources' / 'newspaper').glob('*_S-*.md'))
        self.assertEqual(len(records), 1)
        self._assert_photos_root_untouched(renames)

    def test_bundle_without_type_still_infers_photo(self) -> None:
        bundle = self.archive / 'inbox' / 'album-page'
        bundle.mkdir()
        (bundle / 'front.jpg').write_bytes(b'jpegbytes')
        (bundle / 'notes.md').write_text('From the album.\n', encoding='utf-8')

        rc = self._run([str(bundle)])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertTrue((self.archive / 'photos' / 'front.jpg').is_file())
        self.assertEqual(
            len(list((self.archive / 'sources' / 'photos').glob('*_S-*.md'))), 1)


class ExiftoolReadOnlyRetryTests(unittest.TestCase):
    """#110: a read-only inbox file's embed failure must not block a retry
    with a misleading 'temp file already exists' error once the real cause
    (read-only) is fixed.

    Exercises `_run_exiftool_embed_source` directly (not through the
    FakePhotoStore every other photo test in this file uses) with a mocked
    `subprocess.run`, so the read-only-clear / stray-tmp-cleanup logic these
    tests pin actually runs - the fake seam other tests install replaces
    that whole function and would test nothing here.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.photo = self.tmp / 'locked.jpg'
        self.photo.write_bytes(b'\xff\xd8\xff')
        self.backup = process.OriginalBackup(self.tmp, {})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _tmp_file(self) -> Path:
        return self.photo.with_name(self.photo.name + '_exiftool_tmp')

    def test_embed_clears_read_only_before_writing(self) -> None:
        self.photo.chmod(stat.S_IREAD)
        self.assertFalse(os.access(self.photo, os.W_OK))
        with unittest.mock.patch.object(process.subprocess, 'run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout='', stderr='')
            err = process._run_exiftool_embed_source(self.photo, 'S-aaaaaaaaaa', backup=self.backup)
        self.assertIsNone(err)
        self.assertTrue(os.access(self.photo, os.W_OK))

    def test_stray_tmp_from_an_earlier_failed_run_is_cleared_before_retry(self) -> None:
        # Simulates exactly the issue's own repro sequence: attempt 1 already
        # failed and left its temp file behind; nobody deleted it by hand.
        self._tmp_file().write_bytes(b'stale from a previous attempt')
        with unittest.mock.patch.object(process.subprocess, 'run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout='', stderr='')
            err = process._run_exiftool_embed_source(self.photo, 'S-aaaaaaaaaa', backup=self.backup)
        self.assertIsNone(err)
        self.assertFalse(self._tmp_file().exists())

    def test_a_failed_attempt_cleans_up_its_own_stray_tmp(self) -> None:
        def fake_run(cmd, **kwargs):
            # exiftool's real behaviour: the temp file is created before the
            # write that then fails on a read-only destination.
            self._tmp_file().write_bytes(b'partial write')
            return subprocess.CompletedProcess(
                cmd, 1, stdout='', stderr=f'Error opening {self.photo} for writing')

        with unittest.mock.patch.object(process.subprocess, 'run', side_effect=fake_run):
            err = process._run_exiftool_embed_source(self.photo, 'S-aaaaaaaaaa', backup=self.backup)
        self.assertIsNotNone(err)
        self.assertFalse(self._tmp_file().exists())

    def test_retry_after_fixing_read_only_succeeds_with_no_leftover_tmp(self) -> None:
        # The full round-trip the issue's own test asks for: attempt 1 fails
        # (read-only + exiftool's temp file left behind), the read-only bit
        # is fixed by hand, attempt 2 must succeed with no manual tmp cleanup.
        self.photo.chmod(stat.S_IREAD)
        calls = {'n': 0}

        def fake_run(cmd, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                self._tmp_file().write_bytes(b'partial write')
                return subprocess.CompletedProcess(
                    cmd, 1, stdout='', stderr=f'Error opening {self.photo} for writing')
            if self._tmp_file().exists():
                return subprocess.CompletedProcess(
                    cmd, 1, stdout='',
                    stderr=f'Error: Temporary file already exists: {self._tmp_file()}')
            return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')

        with unittest.mock.patch.object(process.subprocess, 'run', side_effect=fake_run):
            first = process._run_exiftool_embed_source(self.photo, 'S-aaaaaaaaaa', backup=self.backup)
            self.assertIsNotNone(first)
            # Fix the real cause by hand, exactly as the issue's own
            # reproduction does - no manual tmp-file deletion.
            self.photo.chmod(stat.S_IWRITE)
            second = process._run_exiftool_embed_source(self.photo, 'S-aaaaaaaaaa', backup=self.backup)
        self.assertIsNone(second)
        self.assertEqual(calls['n'], 2)

    def test_tmp_exists_error_names_the_real_fix_when_cleanup_cannot_run(self) -> None:
        # Defense in depth for the rare case the stray file itself cannot be
        # deleted (e.g. locked by another process): the message that reaches
        # the user must name the real fix, not just repeat exiftool's own
        # unrelated-sounding text.
        raw = f'Error: Temporary file already exists: {self.photo}_exiftool_tmp'
        rewritten = process._format_exiftool_tmp_error(raw, self.photo)
        self.assertIn('previous failed attempt', rewritten)
        self.assertIn(f'{self.photo}_exiftool_tmp', rewritten)

    def test_ordinary_stderr_is_passed_through_unchanged(self) -> None:
        raw = 'Error: some unrelated exiftool failure'
        self.assertEqual(process._format_exiftool_tmp_error(raw, self.photo), raw)


if __name__ == '__main__':
    unittest.main()
