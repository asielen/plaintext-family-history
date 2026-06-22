"""Tests for `fha process` Stage A (BUILD.md M7.1 documents, M7.2 photos + --more).

The photo paths never call exiftool: the read/embed/remove seams are replaced
here by an in-memory `FakePhotoStore` so the keyword-embed / already-processed
refusal / rollback / --more logic runs without the binary.

Run: python -m unittest tests.test_process -v   (from the repo root)
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import process
from _lib import EXIT_CLEAN, EXIT_ERRORS, EXIT_FAILURE, read_record


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

    def read(self, file_path: Path) -> list[str]:
        return list(self.keywords.get(str(file_path), []))

    def embed(self, file_path: Path, s_id: str) -> str | None:
        key = str(file_path)
        if key in self.fail_paths:
            return 'simulated exiftool failure'
        self.keywords.setdefault(key, []).append(f'SOURCE: {s_id}')
        return None

    def remove(self, file_path: Path, s_id: str) -> str | None:
        key = str(file_path)
        keyword = f'SOURCE: {s_id}'
        self.keywords[key] = [kw for kw in self.keywords.get(key, []) if kw != keyword]
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

    def _run(self, argv: list[str]) -> int:
        return process._standalone_main(argv + ['--root', str(self.archive)])

    def _write_temp_record(self, text: str) -> Path:
        path = self.tmp / 'scratch_record.md'
        path.write_text(text, encoding='utf-8')
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
            'source_date: 1900-01\n'
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
        self.assertEqual(rec['meta']['source_date'], '1900-01')
        self.assertEqual(rec['meta']['provenance'], 'found in a shoebox')
        self.assertIn('A custom citation string.', record.read_text(encoding='utf-8'))

    def test_append_file_entry_handles_inline_files_value(self) -> None:
        record_text = '---\nid: S-aaaaaaaaaa\nfiles: []\ncreated: 2026-01-01\n---\n\nbody\n'
        entry = ['  - file: documents/census/page2.txt', '    role: page-2']
        new_text = process._append_file_entry(record_text, entry)
        parsed = read_record(self._write_temp_record(new_text))
        self.assertEqual(parsed['parse_errors'], [])
        self.assertEqual(len(parsed['meta']['files']), 1)
        self.assertEqual(parsed['meta']['files'][0]['role'], 'page-2')

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

    def test_slugify(self) -> None:
        self.assertEqual(process._slugify('Fairview, Kansas! 1880'), 'fairview-kansas-1880')
        self.assertEqual(process._slugify('   '), 'source')
        self.assertEqual(
            process._derive_slug(None, 'A Title', Path('scan.jpg')), 'a-title')
        self.assertEqual(
            process._derive_slug(None, None, Path('1880-census.pdf')), '1880-census')

    def test_folder_mode_refused(self) -> None:
        rc = self._run([str(self.archive / 'documents')])
        self.assertEqual(rc, EXIT_ERRORS)

    def test_missing_file(self) -> None:
        rc = self._run([str(self.archive / 'documents' / 'nope.txt')])
        self.assertEqual(rc, EXIT_ERRORS)


if __name__ == '__main__':
    unittest.main()
