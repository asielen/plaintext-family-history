"""Tests for `fha process` Stage A (BUILD.md M7.1 documents, M7.2 photos + --more).

The photo paths never call exiftool: the read/embed/remove seams are replaced
here by an in-memory `FakePhotoStore` so the keyword-embed / already-processed
refusal / rollback / --more logic runs without the binary.

Run: python -m unittest tests.test_process -v   (from the repo root)
"""

import os
import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import process
from _lib import EXIT_CLEAN, EXIT_ERRORS, EXIT_FAILURE, load_fha_yaml, read_record


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
    later read — exactly what the real `SOURCE:` keyword round-trip provides.
    """

    def __init__(self) -> None:
        self.keywords: dict[str, list[str]] = {}
        self.fail_paths: set[str] = set()
        self.read_fail_paths: set[str] = set()

    def read(self, file_path: Path) -> list[str]:
        if str(file_path) in self.read_fail_paths:
            raise RuntimeError('simulated exiftool read failure')
        return list(self.keywords.get(str(file_path), []))

    def embed(
        self, file_path: Path, s_id: str, extra_keywords: list[str] | None = None
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
        self, file_path: Path, s_id: str, extra_keywords: list[str] | None = None
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
        # A bare file dropped straight in inbox/ (no sidecar) — process should
        # file it into documents/ before scaffolding, not refuse it.
        (self.archive / 'inbox').mkdir()
        asset = self.archive / 'inbox' / 'note.txt'
        asset.write_text('inbox body', encoding='utf-8')

        rc = self._run([str(asset)])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(asset.exists())
        self.assertEqual(list((self.archive / 'inbox').iterdir()), [])
        renamed = list((self.archive / 'documents').glob('note_S-*.txt'))
        self.assertEqual(len(renamed), 1)

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
        renamed = list((self.archive / 'documents').glob('newspaper-clipping_S-*.txt'))
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
        renamed = list((self.archive / 'documents').glob('census_S-*.jpg'))
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


if __name__ == '__main__':
    unittest.main()
