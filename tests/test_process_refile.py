"""Tests for `fha process refile` - the sanctioned cross-root correction move.

Covers both directions (documents -> photos with the last-chance rename and
keyword embed; photos -> documents with the catalog confirm and the S13
grammar rename), the refusal surface (missing/escaping --dest, wrong-root,
multi-file ambiguity, working copy, missing asset), dry-run zero-writes, and
transactional rollback. The exiftool seams are replaced by the in-memory
FakePhotoStore from test_process (the _install_photo_store pattern) so no test
ever shells out.

Run: py -3.14 -m unittest tests.test_process_refile -v   (from the repo root)
"""

import contextlib
import io
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT))

import process
from _lib import EXIT_CLEAN, EXIT_FAILURE, EXIT_WARNINGS, read_record
from tests.test_process import FakePhotoStore

SID = 'S-abc123defg'


def _make_archive(tmp: Path) -> Path:
    """A minimal archive root mapping internal photos/ and documents/ roots."""
    archive = tmp / 'archive'
    (archive / 'documents' / 'census').mkdir(parents=True)
    (archive / 'photos' / '1880').mkdir(parents=True)
    (archive / 'sources' / 'census').mkdir(parents=True)
    (archive / 'sources' / 'photos').mkdir(parents=True)
    (archive / 'people').mkdir()
    (archive / 'notes').mkdir()
    (archive / 'fha.yaml').write_text(
        'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8'
    )
    return archive


def _record_text(alias_lines: str, source_type: str = 'census') -> str:
    """A minimal but complete S14 source record around the given files: lines."""
    return (
        '---\n'
        f'id: {SID}\n'
        f'aliases: [{SID}]\n'
        'title: Campaign card\n'
        f'source_type: {source_type}\n'
        'source_class: original\n'
        'repository: unknown\n'
        'citation: Campaign card\n'
        'people: []\n'
        'files:\n'
        f'{alias_lines}'
        'created: 2026-07-01\n'
        '---\n'
        '\n'
        '## Claims\n'
        '```yaml\n'
        '```\n'
        '\n'
        '## Notes\n'
        '*(none yet - drafted in the AI pass)*\n'
    )


class ProcessRefileTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.archive = _make_archive(self.tmp)
        self._orig_read = process._run_exiftool_read_keywords
        self._orig_embed = process._run_exiftool_embed_source
        self._orig_remove = process._run_exiftool_remove_source
        self._orig_prompt = process._prompt
        self._orig_interactive = process._stdin_is_interactive
        self._orig_write_text_exact = process.write_text_exact_atomic

    def tearDown(self) -> None:
        process._run_exiftool_read_keywords = self._orig_read
        process._run_exiftool_embed_source = self._orig_embed
        process._run_exiftool_remove_source = self._orig_remove
        process._prompt = self._orig_prompt
        process._stdin_is_interactive = self._orig_interactive
        process.write_text_exact_atomic = self._orig_write_text_exact
        self._tmp.cleanup()

    # -- fixture builders ------------------------------------------------------

    def _install_photo_store(self) -> FakePhotoStore:
        store = FakePhotoStore()
        process._run_exiftool_read_keywords = store.read
        process._run_exiftool_embed_source = store.embed
        process._run_exiftool_remove_source = store.remove
        return store

    def _write_doc_source(self, *, with_original_filename: bool = True) -> tuple[Path, Path]:
        """A processed document source: asset + record. Returns (asset, record)."""
        asset = self.archive / 'documents' / 'census' / f'campaign-card_{SID}.jpg'
        asset.write_bytes(b'jpegbytes')
        entry = f'  - file: documents/census/campaign-card_{SID}.jpg\n    role: primary\n'
        if with_original_filename:
            entry += '    original_filename: campaign-card.jpg\n'
        record = self.archive / 'sources' / 'census' / f'campaign-card_{SID}.md'
        record.write_bytes(_record_text(entry).encode('utf-8'))
        return asset, record

    def _write_doc_source_with_derived(self) -> tuple[Path, Path, Path]:
        """A processed doc source: a primary scan plus a derived transcript."""
        card = self.archive / 'documents' / 'census' / f'campaign-card_{SID}.jpg'
        card.write_bytes(b'jpegbytes')
        transcript = self.archive / 'documents' / 'census' / f'campaign-card_{SID}.txt'
        transcript.write_text('extracted text', encoding='utf-8')
        entry = (f'  - file: documents/census/campaign-card_{SID}.jpg\n'
                 '    role: primary\n'
                 '    original_filename: campaign-card.jpg\n'
                 f'  - file: documents/census/campaign-card_{SID}.txt\n'
                 '    role: transcript\n'
                 '    derived: true\n')
        record = self.archive / 'sources' / 'census' / f'campaign-card_{SID}.md'
        record.write_bytes(_record_text(entry).encode('utf-8'))
        return card, transcript, record

    def _write_photo_source(self) -> tuple[Path, Path]:
        """A processed photo source: asset + record. Returns (asset, record)."""
        asset = self.archive / 'photos' / '1880' / 'portrait.jpg'
        asset.write_bytes(b'jpegbytes')
        entry = ('  - file: photos/1880/portrait.jpg\n'
                 '    role: primary\n'
                 '    is_primary: true\n')
        record = self.archive / 'sources' / 'photos' / f'portrait_{SID}.md'
        record.write_bytes(_record_text(entry, source_type='photo').encode('utf-8'))
        return asset, record

    def _run(self, argv: list[str]) -> int:
        return process._standalone_main(['refile'] + argv + ['--root', str(self.archive)])

    def _run_captured(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self._run(argv)
        return rc, out.getvalue(), err.getvalue()

    def _snapshot_tree(self, root: Path) -> dict[str, tuple[int, int]]:
        snap: dict[str, tuple[int, int]] = {}
        for p in sorted(root.rglob('*')):
            rel = p.relative_to(root).as_posix()
            if p.is_file():
                st = p.stat()
                snap[rel] = (st.st_size, st.st_mtime_ns)
            else:
                snap[rel] = (-1, -1)
        return snap

    # -- documents -> photos ---------------------------------------------------

    def test_doc_to_photo_happy_path(self) -> None:
        store = self._install_photo_store()
        asset, record = self._write_doc_source()

        rc, out, err = self._run_captured([SID, '--to', 'photos', '--dest', '1880s'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(asset.exists())
        moved = self.archive / 'photos' / '1880s' / 'campaign-card.jpg'
        self.assertTrue(moved.is_file(), 'original_filename should be restored')
        self.assertIn(f'SOURCE: {SID}', store.keywords.get(str(moved), []))

        rec = read_record(record)
        self.assertEqual(rec['meta']['files'][0]['file'], 'photos/1880s/campaign-card.jpg')
        body = record.read_text(encoding='utf-8')
        self.assertIn('Refiled', body)
        self.assertIn(f'documents/census/campaign-card_{SID}.jpg -> '
                      'photos/1880s/campaign-card.jpg', body)
        self.assertIn('fha index', out)
        self.assertIn('fha photoindex', out)
        self.assertIn('Lightroom', out)
        self.assertNotIn('Traceback', err)

    def test_doc_to_photo_without_original_filename_strips_sid(self) -> None:
        self._install_photo_store()
        asset, record = self._write_doc_source(with_original_filename=False)

        rc = self._run([SID, '--to', 'photos', '--dest', '1880s'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(asset.exists())
        moved = self.archive / 'photos' / '1880s' / 'campaign-card.jpg'
        self.assertTrue(moved.is_file(), 'the _S-id suffix should be stripped')
        rec = read_record(record)
        self.assertEqual(rec['meta']['files'][0]['file'], 'photos/1880s/campaign-card.jpg')

    def test_doc_to_photo_missing_dest_refused(self) -> None:
        self._install_photo_store()
        asset, _record = self._write_doc_source()

        rc, _out, err = self._run_captured([SID, '--to', 'photos'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('--dest', err)
        self.assertIn('1880s', err)  # the message ships an example
        self.assertTrue(asset.exists(), 'a refusal must move nothing')

    def test_doc_to_photo_dest_escapes_refused(self) -> None:
        self._install_photo_store()
        asset, record = self._write_doc_source()
        before = record.read_bytes()

        for bad_dest in ('../outside', 'a/../../b', str(self.tmp / 'abs')):
            rc, _out, err = self._run_captured([SID, '--to', 'photos', '--dest', bad_dest])
            self.assertEqual(rc, EXIT_FAILURE, f'--dest {bad_dest!r} must refuse')
            self.assertIn('--dest', err)
            self.assertNotIn('Traceback', err)
            self.assertTrue(asset.exists())
            self.assertEqual(record.read_bytes(), before)

    def test_doc_to_photo_unsupported_format_warns_and_proceeds(self) -> None:
        self._install_photo_store()
        asset = self.archive / 'documents' / 'census' / f'campaign-card_{SID}.pdf'
        asset.write_bytes(b'%PDF-1.4')
        entry = (f'  - file: documents/census/campaign-card_{SID}.pdf\n'
                 '    role: primary\n    original_filename: campaign-card.pdf\n')
        record = self.archive / 'sources' / 'census' / f'campaign-card_{SID}.md'
        record.write_bytes(_record_text(entry).encode('utf-8'))

        rc, _out, err = self._run_captured([SID, '--to', 'photos', '--dest', '1880s'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('WARNING', err)
        self.assertIn('keyword', err.lower())
        self.assertTrue((self.archive / 'photos' / '1880s' / 'campaign-card.pdf').is_file())

    def test_doc_to_photo_exiftool_absent_warns_and_proceeds(self) -> None:
        def boom(_path, _sid, extra_keywords=None, *, backup=None):
            raise RuntimeError('exiftool is not installed or not on PATH.')
        process._run_exiftool_embed_source = boom
        asset, record = self._write_doc_source()

        rc, _out, err = self._run_captured([SID, '--to', 'photos', '--dest', '1880s'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('WARNING', err)
        self.assertIn('exiftool', err)
        self.assertTrue((self.archive / 'photos' / '1880s' / 'campaign-card.jpg').is_file())
        self.assertIn('photos/1880s/campaign-card.jpg',
                      record.read_text(encoding='utf-8'))

    # -- photos -> documents ---------------------------------------------------
    #
    # These fixtures are photo-TYPED records, so they pass `--type photo`: since
    # issue #59 a record still typed photo must say what it becomes when it
    # leaves the photo library, and `--type photo` is the human keeping the type
    # (and with it the documents/photos/ destination) on purpose. The refusal
    # when he says nothing at all has its own tests further down.

    def test_photo_to_doc_with_yes(self) -> None:
        store = self._install_photo_store()
        asset, record = self._write_photo_source()
        store.keywords[str(asset)] = [f'SOURCE: {SID}']

        rc, out, _err = self._run_captured([SID, '--to', 'documents', '--type', 'photo', '--yes'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(asset.exists())
        moved = self.archive / 'documents' / 'photos' / f'portrait_{SID}.jpg'
        self.assertTrue(moved.is_file(), 'renamed into the {slug}_{S-id} grammar')
        rec = read_record(record)
        self.assertEqual(rec['meta']['files'][0]['file'], f'documents/photos/portrait_{SID}.jpg')
        body = record.read_text(encoding='utf-8')
        self.assertIn('previously named portrait.jpg', body)
        # The embedded SOURCE keyword is deliberately NOT stripped.
        self.assertEqual(store.keywords[str(asset)], [f'SOURCE: {SID}'])
        self.assertIn('Lightroom', out)
        self.assertIn('fha index', out)

    def test_photo_to_doc_dest_override(self) -> None:
        self._install_photo_store()
        asset, _record = self._write_photo_source()

        rc = self._run([SID, '--to', 'documents', '--type', 'photo', '--yes', '--dest', 'letters'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(asset.exists())
        self.assertTrue((self.archive / 'documents' / 'letters' / f'portrait_{SID}.jpg').is_file())

    def test_photo_to_doc_noninteractive_without_yes_refused(self) -> None:
        self._install_photo_store()
        process._stdin_is_interactive = lambda: False
        asset, record = self._write_photo_source()
        before = record.read_bytes()

        rc, _out, err = self._run_captured([SID, '--to', 'documents', '--type', 'photo'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('--yes', err)
        self.assertTrue(asset.exists())
        self.assertEqual(record.read_bytes(), before)

    def test_photo_to_doc_interactive_decline_changes_nothing(self) -> None:
        self._install_photo_store()
        process._stdin_is_interactive = lambda: True
        process._prompt = lambda _msg: 'n'
        asset, record = self._write_photo_source()
        before = record.read_bytes()

        rc, out, _err = self._run_captured([SID, '--to', 'documents', '--type', 'photo'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('nothing changed', out.lower())
        self.assertTrue(asset.exists())
        self.assertEqual(record.read_bytes(), before)

    # -- shared refusal surface ------------------------------------------------

    def test_already_under_target_root_names_reconcile(self) -> None:
        self._install_photo_store()
        asset, _record = self._write_doc_source()

        rc, _out, err = self._run_captured([SID, '--to', 'documents', '--yes'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('already under the documents root', err)
        self.assertIn('fha reconcile', err)
        self.assertTrue(asset.exists())

    def test_multi_file_without_file_flag_lists_candidates(self) -> None:
        self._install_photo_store()
        card = self.archive / 'documents' / 'census' / 'card.jpg'
        card.write_bytes(b'jpegbytes')
        sidecar = self.archive / 'documents' / 'census' / 'card.jpg.txt'
        sidecar.write_text('notes', encoding='utf-8')
        entry = ('  - file: documents/census/card.jpg\n'
                 '    role: primary\n'
                 '    original_filename: card.jpg\n'
                 '  - file: documents/census/card.jpg.txt\n'
                 '    role: transcript\n')
        record = self.archive / 'sources' / 'census' / f'campaign-card_{SID}.md'
        record.write_bytes(_record_text(entry).encode('utf-8'))

        rc, _out, err = self._run_captured([SID, '--to', 'photos', '--dest', '1880s'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('--file', err)
        self.assertIn('card.jpg', err)
        self.assertIn('card.jpg.txt', err)
        self.assertTrue(card.exists())

    def test_file_flag_selects_and_siblings_survive_value_exact(self) -> None:
        # card_{SID}.jpg's alias is a SUBSTRING of card_{SID}.jpg.txt's, and the
        # superstring sibling is listed FIRST so the trap is armed: a first-match
        # substring rewriter (`old_alias in value`) would grab card_{SID}.jpg.txt's
        # line before reaching card_{SID}.jpg's. The value-exact one must skip it
        # and rewrite the real target only. (Both filenames carry the record's own
        # S-id suffix, SPEC §13 - the shape a genuinely processed file always has,
        # and the shape refile's own identity check now requires.)
        self._install_photo_store()
        card = self.archive / 'documents' / 'census' / f'card_{SID}.jpg'
        card.write_bytes(b'jpegbytes')
        sidecar = self.archive / 'documents' / 'census' / f'card_{SID}.jpg.txt'
        sidecar.write_text('notes', encoding='utf-8')
        entry = (f'  - file: documents/census/card_{SID}.jpg.txt\n'
                 '    role: transcript\n'
                 f'  - file: documents/census/card_{SID}.jpg\n'
                 '    role: primary\n')
        record = self.archive / 'sources' / 'census' / f'campaign-card_{SID}.md'
        record.write_bytes(_record_text(entry).encode('utf-8'))

        rc = self._run([SID, '--file', f'card_{SID}.jpg', '--to', 'photos', '--dest', '1880s'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(card.exists())
        self.assertTrue((self.archive / 'photos' / '1880s' / 'card.jpg').is_file())
        self.assertTrue(sidecar.exists(), 'the unselected sibling never moves')
        body = record.read_text(encoding='utf-8')
        self.assertIn('file: photos/1880s/card.jpg\n', body.replace('\r\n', '\n'))
        self.assertIn(f'file: documents/census/card_{SID}.jpg.txt', body)

    def test_file_flag_naming_nothing_lists_what_exists(self) -> None:
        self._install_photo_store()
        self._write_doc_source()

        rc, _out, err = self._run_captured(
            [SID, '--file', 'nope.jpg', '--to', 'photos', '--dest', '1880s'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('nope.jpg', err)
        self.assertIn(f'campaign-card_{SID}.jpg', err)

    def test_asset_missing_on_disk_names_reconcile(self) -> None:
        self._install_photo_store()
        entry = (f'  - file: documents/census/campaign-card_{SID}.jpg\n'
                 '    role: primary\n')
        record = self.archive / 'sources' / 'census' / f'campaign-card_{SID}.md'
        record.write_bytes(_record_text(entry).encode('utf-8'))

        rc, _out, err = self._run_captured([SID, '--to', 'photos', '--dest', '1880s'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('not on disk', err)
        self.assertIn('fha reconcile', err)

    # -- containment + identity guards (audit finding, mirrors the #147-review --
    # -- fix to `fha source clear-keyword`) ------------------------------------

    def test_stored_alias_escaping_documents_root_refused(self) -> None:
        # A hand-edited/corrupted files: entry that STARTS WITH 'documents' as
        # text (passing a naive prefix check) but actually resolves outside the
        # configured documents root via a '..' segment. Before the fix this
        # silently moved (and thereby deleted, since _move_file removes the
        # source) a file that was never part of the archive at all - proven by
        # a repro against the pre-fix code. Nothing outside the archive may be
        # touched, moved, or deleted.
        self._install_photo_store()
        outside = self.tmp / 'outside-secret.tif'
        outside.write_bytes(b'not part of the archive')
        entry = f'  - file: documents/../../{outside.name}\n    role: primary\n'
        record = self.archive / 'sources' / 'census' / f'campaign-card_{SID}.md'
        record.write_bytes(_record_text(entry).encode('utf-8'))

        rc, _out, err = self._run_captured([SID, '--to', 'photos', '--dest', '1880s'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('resolves outside', err)
        self.assertIn('documents', err)
        self.assertTrue(outside.exists(), 'a file outside the archive must never be touched')
        self.assertFalse((self.archive / 'photos' / '1880s').exists())

    def test_stored_alias_escaping_photos_root_refused(self) -> None:
        self._install_photo_store()
        outside = self.tmp / 'outside-secret.jpg'
        outside.write_bytes(b'not part of the archive')
        entry = f'  - file: photos/../../{outside.name}\n    role: primary\n    is_primary: true\n'
        record = self.archive / 'sources' / 'photos' / f'portrait_{SID}.md'
        record.write_bytes(_record_text(entry, source_type='photo').encode('utf-8'))

        rc, _out, err = self._run_captured(
            [SID, '--to', 'documents', '--type', 'photo', '--yes'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('resolves outside', err)
        self.assertIn('photos', err)
        self.assertTrue(outside.exists(), 'a file outside the archive must never be touched')
        self.assertFalse((self.archive / 'documents' / 'photos').exists())

    def _patch_resolve_to_loop_on(self, target_name: str):
        """Make `Path.resolve()` raise `RuntimeError` for any path whose
        final component is `target_name`, and behave normally for every
        other path.

        A real symlink loop needs a privilege this Windows test environment
        does not have (mirrors test_packet.py's own note on the same
        constraint for its `_is_under_root` symlink-loop test); `.resolve()`
        raising `RuntimeError` is the documented pathlib contract a real loop
        triggers, so patching it directly - scoped to one specific filename
        so every OTHER `.resolve()` call refile makes along the way (the
        record path, the archive root, the destination root, ...) is
        unaffected - reproduces the same failure a loop would.
        """
        real_resolve = Path.resolve

        def looping_resolve(path_self, *args, **kwargs):
            if path_self.name == target_name:
                raise RuntimeError(f'Symlink loop resolving {target_name}')
            return real_resolve(path_self, *args, **kwargs)

        return unittest.mock.patch.object(Path, 'resolve', looping_resolve)

    def test_source_asset_symlink_loop_refused_cleanly_live(self) -> None:
        # Issue #170 finding 2 (round-3 audit): `_is_under`'s containment
        # check calls `.resolve()` on both the candidate source path and the
        # claimed root; a symlink loop on either side makes `.resolve()`
        # raise `RuntimeError`, which - before the fix - only the guard's
        # `except (ValueError, OSError):` clause did NOT catch, letting it
        # escape uncaught. It must be refused cleanly, not a crash - and
        # (Codex review, round-5 audit) with a message that names the
        # symlink loop as the thing to fix, not the ordinary "edit the
        # files: entry by hand" advice `test_stored_alias_escaping_documents_root_refused`
        # above gets for a genuine escaping-path entry: that advice is wrong
        # here, since the entry may be perfectly correct and an on-disk
        # symlink is what actually needs repairing.
        self._install_photo_store()
        asset, record = self._write_doc_source()

        with self._patch_resolve_to_loop_on(asset.name):
            rc, _out, err = self._run_captured([SID, '--to', 'photos', '--dest', '1880s'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertNotIn('Traceback', err)
        self.assertIn('symlink loop', err)
        self.assertNotIn('resolves outside', err)
        self.assertTrue(asset.exists(),
                         'a file whose containment could not be verified must never move')

    def test_source_asset_symlink_loop_refused_cleanly_dry_run(self) -> None:
        # Same as the live case above, but --dry-run: the containment check
        # runs unconditionally before process_refile's own dry-run branch, so
        # a symlink loop must be refused the same clean way in preview too,
        # never a crash that leaves the human unsure whether anything moved.
        self._install_photo_store()
        asset, record = self._write_doc_source()

        with self._patch_resolve_to_loop_on(asset.name):
            rc, _out, err = self._run_captured(
                [SID, '--to', 'photos', '--dest', '1880s', '--dry-run'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertNotIn('Traceback', err)
        self.assertIn('symlink loop', err)
        self.assertNotIn('resolves outside', err)
        self.assertTrue(asset.exists(), 'a dry-run must never move anything, loop or not')

    def test_dest_subpath_symlink_loop_refused_cleanly(self) -> None:
        # Issue #170 finding 2 (round-3 audit): `_validate_dest_subpath`'s own
        # containment check (--dest under the target root) had the identical
        # narrow `except (ValueError, OSError):` gap - a symlink loop while
        # resolving the DESTINATION folder must get the same clean
        # `--dest ... does not resolve to a folder inside the ... root`
        # refusal every other bad --dest already gets, not a crash.
        self._install_photo_store()
        asset, record = self._write_doc_source()

        with self._patch_resolve_to_loop_on('very-unique-dest-xyz'):
            rc, _out, err = self._run_captured(
                [SID, '--to', 'photos', '--dest', 'very-unique-dest-xyz'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertNotIn('Traceback', err)
        self.assertIn('does not resolve to a folder inside', err)
        self.assertTrue(asset.exists(), 'a file must never move on a --dest refusal')

    @contextlib.contextmanager
    def _patch_313_nonstrict_resolve_success_on(self, target_name: str):
        """Simulate the Python 3.13+ `Path.resolve()` behavior gap finding 5
        exploits (round-11 audit, post-merge Codex review of #197): on 3.13+,
        NON-strict `resolve()` (what `_is_under` calls) stopped raising for a
        genuine symlink loop at all - it silently returns a best-effort,
        still-UNRESOLVED path instead (see `_resolve_hits_symlink_loop`'s own
        docstring in tools/process.py). Only `resolve(strict=True)` (what
        `_resolve_hits_symlink_loop` calls) still raises for the same loop.

        A real on-disk loop needs a privilege this Windows test environment
        does not have (the same constraint every other symlink-loop test in
        this file already notes), so this patches `Path.resolve` directly,
        scoped to one filename, to reproduce exactly that asymmetry - and
        patches `Path.is_file` for the SAME filename to return False,
        matching what a genuine loop's OS-level stat() call actually does:
        `_is_under`'s non-strict resolve never touches the filesystem at all,
        so without this the target's ordinary on-disk existence would mask
        the very fallthrough this test exists to catch (the old code, having
        wrongly trusted `_is_under`'s True, falls through to `.is_file()`
        next - and only reports the bug's wrong "not on disk" message if
        that check also fails, exactly as a real loop's stat() would)."""
        real_resolve = Path.resolve
        real_is_file = Path.is_file

        def flaky_resolve(path_self, *args, **kwargs):
            if path_self.name == target_name:
                if kwargs.get('strict'):
                    raise RuntimeError(f'Symlink loop resolving {target_name}')
                return path_self  # 3.13+ non-strict best-effort: unresolved, not raised
            return real_resolve(path_self, *args, **kwargs)

        def flaky_is_file(path_self, *args, **kwargs):
            if path_self.name == target_name:
                return False  # a real loop's stat() fails, same as an ordinary broken path
            return real_is_file(path_self, *args, **kwargs)

        with unittest.mock.patch.object(Path, 'resolve', flaky_resolve), \
                unittest.mock.patch.object(Path, 'is_file', flaky_is_file):
            yield

    def test_source_asset_313_style_symlink_loop_names_the_loop_not_missing(self) -> None:
        # Finding 5 (round-11 audit): on Python 3.13+, `_is_under`'s
        # non-strict `.resolve()` no longer raises for a genuine symlink
        # loop - it returns a best-effort, still-unresolved path, which can
        # still read as textually "under" the claimed root and make
        # `_is_under` report True. Before the fix, the loop check lived only
        # inside `if not _is_under(...):`, so a True there skipped it
        # entirely and fell through to `if not src.is_file():` - which a
        # genuine loop's own stat() call also fails, producing the WRONG
        # diagnosis ("is not on disk", with the wrong remedy: plug in a
        # drive, run reconcile) instead of naming the actual problem (a
        # broken symlink to find and fix). The fix checks
        # `_resolve_hits_symlink_loop` BEFORE ever trusting `_is_under`'s
        # result, so the correct "symlink loop" refusal fires regardless of
        # which way `_is_under` itself answers.
        self._install_photo_store()
        asset, record = self._write_doc_source()

        with self._patch_313_nonstrict_resolve_success_on(asset.name):
            rc, _out, err = self._run_captured([SID, '--to', 'photos', '--dest', '1880s'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertNotIn('Traceback', err)
        self.assertIn('symlink loop', err)
        self.assertNotIn('not on disk', err)
        self.assertTrue(asset.exists(),
                         'a file whose containment could not be verified must never move')

    def test_documents_asset_belonging_to_different_source_refused(self) -> None:
        # Inventory drift: this source's files: entry names a documents-root
        # file whose OWN filename (SPEC #13's `_{S-id}` suffix) says it belongs
        # to a DIFFERENT source. Before the fix, refile trusted the record's
        # files: entry alone and would relocate the other source's asset,
        # relabelling it as this source's own while the other source's record
        # was left pointing at nothing.
        self._install_photo_store()
        other_sid = 'S-zzyy888888'
        other_asset = self.archive / 'documents' / 'census' / f'other-deed_{other_sid}.jpg'
        other_asset.write_bytes(b'belongs to a different source')
        stored_alias = f'documents/census/other-deed_{other_sid}.jpg'
        entry = f'  - file: {stored_alias}\n    role: primary\n'
        record = self.archive / 'sources' / 'census' / f'campaign-card_{SID}.md'
        record.write_bytes(_record_text(entry).encode('utf-8'))

        rc, _out, err = self._run_captured([SID, '--to', 'photos', '--dest', '1880s'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('inventory drift', err)
        self.assertIn(other_sid, err)
        self.assertTrue(other_asset.exists(), "another source's file must never move")
        # #2 audit finding: `fha reconcile` cannot heal this - it only
        # re-links a files: entry whose path no longer resolves, and this
        # one DOES resolve (just to the wrong file). The message must say so
        # and name the exact hand-edit instead of pointing at a command that
        # would run clean and change nothing.
        self.assertIn('cannot fix this', err)
        self.assertIn('Fix it by hand', err)
        self.assertIn(record.name, err)
        self.assertIn(stored_alias, err)

    def test_documents_asset_with_no_source_id_at_all_refused(self) -> None:
        # A files: entry naming a file that carries no `_{S-id}` suffix at all
        # (never processed through fha, or the suffix was stripped by hand) is
        # refused on the same footing as a definite mismatch - absence of the
        # identity marker is exactly what a `fha source clear-keyword` target
        # refuses too.
        self._install_photo_store()
        stray = self.archive / 'documents' / 'census' / 'unlabeled.jpg'
        stray.write_bytes(b'no source id in this filename')
        entry = '  - file: documents/census/unlabeled.jpg\n    role: primary\n'
        record = self.archive / 'sources' / 'census' / f'campaign-card_{SID}.md'
        record.write_bytes(_record_text(entry).encode('utf-8'))

        rc, _out, err = self._run_captured([SID, '--to', 'photos', '--dest', '1880s'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('no source id at all', err)
        self.assertTrue(stray.exists())

    def test_photo_asset_belonging_to_different_source_refused(self) -> None:
        # Same drift, photos-root direction: the target's embedded SOURCE:
        # keyword (photos are never renamed, so there is no filename signal)
        # names a different source outright - unambiguous evidence this photo
        # is not the requested source's own asset.
        store = self._install_photo_store()
        asset, record = self._write_photo_source()
        other_sid = 'S-zzyy999999'
        store.keywords[str(asset)] = [f'SOURCE: {other_sid}']

        rc, _out, err = self._run_captured(
            [SID, '--to', 'documents', '--type', 'photo', '--yes'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('inventory drift', err)
        self.assertIn(other_sid, err)
        self.assertTrue(asset.exists(), "another source's photo must never move")
        # Issue #170 finding 3 (round-3 audit): the photo branch of this same
        # conflict used to suggest `fha reconcile`, which cannot fix it -
        # its document pass ignores photos/ aliases and its photo pass only
        # re-ties the photo catalog, not this record. It must name the exact
        # files: entry to fix by hand instead, mirroring the documents-root
        # branch's own fix (test_documents_asset_belonging_to_different_
        # source_refused above).
        self.assertNotIn('Run `fha reconcile`', err)
        self.assertIn('cannot fix this', err)
        self.assertIn('Fix it by hand', err)
        self.assertIn(record.name, err)
        self.assertIn('photos/1880/portrait.jpg', err)

    def test_photo_asset_with_no_keyword_still_proceeds(self) -> None:
        # A photo with no embedded SOURCE: keyword at all (or one that cannot
        # be read because exiftool is unavailable) has no positive evidence of
        # drift - refile is left to the containment check rather than refusing
        # on an inference it cannot confirm, so this is unaffected by the new
        # identity guard (regression check: a bare-bones photo fixture like
        # every other photos->documents test in this file must still work).
        self._install_photo_store()
        asset, _record = self._write_photo_source()

        rc = self._run([SID, '--to', 'documents', '--type', 'photo', '--yes'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(asset.exists())

    def test_photo_asset_genuinely_unavailable_exiftool_still_proceeds(self) -> None:
        # The one soft-fail case that IS supposed to degrade to the weaker
        # containment-only check: exiftool itself is not installed on this
        # machine, so nothing at all can be learned about the file. Pinned
        # here specifically because the P1 fix below narrows the soft-fail
        # from "any RuntimeError" to "only ExiftoolUnavailableError" - this
        # proves the narrowing did not also break the legitimate case.
        store = self._install_photo_store()
        asset, _record = self._write_photo_source()
        store.unavailable_paths.add(str(asset))

        rc = self._run([SID, '--to', 'documents', '--type', 'photo', '--yes'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(asset.exists())

    def test_photo_asset_unreadable_for_other_reason_refused(self) -> None:
        # P1 audit finding: `_read_source_keyword`'s underlying read raises the
        # SAME RuntimeError whether exiftool is genuinely absent OR exiftool
        # is present but rejects the file (corrupt/unsupported) or returns
        # invalid JSON. Before the fix, refile caught RuntimeError blanket and
        # treated every case identically to "exiftool unavailable" - so a
        # photo that is unreadable for this OTHER reason, but which actually
        # carries a different source's embedded id, would silently pass the
        # identity check and get moved/relabelled as the wrong source's
        # asset. It must refuse instead: the file cannot be verified, so it
        # cannot be moved.
        store = self._install_photo_store()
        asset, _record = self._write_photo_source()
        store.read_fail_paths.add(str(asset))

        rc, _out, err = self._run_captured(
            [SID, '--to', 'documents', '--type', 'photo', '--yes'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('could not be read', err)
        self.assertNotIn('Traceback', err)
        self.assertTrue(asset.exists(), 'an unverifiable photo must never move')

    def test_photo_asset_with_conflicting_second_keyword_refused(self) -> None:
        # #5 audit finding: a photo can carry more than one SOURCE: value
        # across its Keywords/Subject fields (a hand edit, or a duplicate
        # embed). Before the fix, the identity check read only the FIRST
        # matching value - so when that first value happened to equal the
        # requested source's id, a CONFLICTING second value naming a
        # different source was never even looked at, and refile moved the
        # ambiguous file, rewriting only the requested source's inventory
        # while the other named source's own record was left pointing at
        # nothing.
        store = self._install_photo_store()
        asset, _record = self._write_photo_source()
        other_sid = 'S-zzyy777777'
        # First value agrees with the requested source (e.g. Keywords); the
        # conflicting second value sits behind it (e.g. Subject).
        store.keywords[str(asset)] = [f'SOURCE: {SID}', f'SOURCE: {other_sid}']

        rc, _out, err = self._run_captured(
            [SID, '--to', 'documents', '--type', 'photo', '--yes'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('inventory drift', err)
        self.assertIn(other_sid, err)
        self.assertTrue(asset.exists(), 'an ambiguously-marked photo must never move')
        self.assertIn('cannot fix this', err)
        # Codex review, round-6 audit: the fix-by-hand advice must not
        # contradict existing behavior, which deliberately also accepts an
        # UNTAGGED photo (embedded_sids empty -> conflicting empty -> this
        # whole refusal branch never fires on retry) as a valid target -
        # telling the owner it must carry a matching keyword would risk
        # them discarding a perfectly good, merely-untagged photo.
        self.assertIn('untagged', err)

    def test_working_copy_refusal(self) -> None:
        (self.archive / 'WORKING_COPY').write_text('marker', encoding='utf-8')
        self._write_doc_source()

        rc, _out, err = self._run_captured([SID, '--to', 'photos', '--dest', '1880s'])

        self.assertEqual(rc, EXIT_WARNINGS)
        self.assertIn('working-copy', err)
        self.assertIn('main machine', err)

    def test_invalid_id_shape_refused(self) -> None:
        rc, _out, err = self._run_captured(['notanid', '--to', 'photos', '--dest', 'x'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('not a valid source ID', err)
        self.assertIn('S-2b3c4d5e6f', err)  # jargon ships an example
        self.assertNotIn('Traceback', err)

    def test_record_not_found_exits_one_and_names_find(self) -> None:
        rc, _out, err = self._run_captured(['S-2b3c4d5e6f', '--to', 'photos', '--dest', 'x'])

        self.assertEqual(rc, EXIT_WARNINGS)
        self.assertIn('fha find S-2b3c4d5e6f', err)

    def test_destination_collision_refused(self) -> None:
        self._install_photo_store()
        asset, _record = self._write_doc_source()
        blocker = self.archive / 'photos' / '1880s'
        blocker.mkdir()
        (blocker / 'campaign-card.jpg').write_bytes(b'already here')

        rc, _out, err = self._run_captured([SID, '--to', 'photos', '--dest', '1880s'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('already exists', err)
        self.assertTrue(asset.exists())

    # -- dry-run ---------------------------------------------------------------

    def test_dry_run_zero_writes_doc_to_photo(self) -> None:
        store = self._install_photo_store()
        self._write_doc_source()
        before = self._snapshot_tree(self.archive)

        rc, out, _err = self._run_captured([SID, '--to', 'photos', '--dest', '1880s', '--dry-run'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(self._snapshot_tree(self.archive), before)
        self.assertEqual(store.keywords, {})
        self.assertIn('Would move', out)
        self.assertIn('Would rename', out)
        self.assertIn('Would embed', out)
        self.assertIn('Would rewrite the files: entry', out)
        self.assertIn('Would add a Notes line', out)

    def test_dry_run_zero_writes_photo_to_doc(self) -> None:
        store = self._install_photo_store()
        self._write_photo_source()
        before = self._snapshot_tree(self.archive)
        # No --yes and no interactive answer needed: dry-run never prompts.
        process._stdin_is_interactive = lambda: False

        rc, out, _err = self._run_captured([SID, '--to', 'documents', '--type', 'photo', '--dry-run'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(self._snapshot_tree(self.archive), before)
        self.assertEqual(store.keywords, {})
        self.assertIn('Would move', out)
        self.assertIn('Lightroom', out)
        self.assertIn('Would rewrite the files: entry', out)

    # -- transactionality ------------------------------------------------------

    def test_rollback_on_record_write_failure(self) -> None:
        store = self._install_photo_store()
        asset, record = self._write_doc_source()
        before_record = record.read_bytes()

        real_write = self._orig_write_text_exact
        writes = {'n': 0}

        def flaky_write(path, text):
            # Fail ONLY the forward record write (triggering rollback); let the
            # rollback's restore of the original text succeed. This is the clean,
            # complete rollback: file back home, record whole again. (A restore
            # write that also failed is the damaged-record case, covered by
            # test_rollback_record_restore_failure_reports_damaged_record.)
            writes['n'] += 1
            if writes['n'] == 1:
                raise OSError('simulated disk full')
            return real_write(path, text)
        process.write_text_exact_atomic = flaky_write

        rc, _out, err = self._run_captured([SID, '--to', 'photos', '--dest', '1880s'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('rolled back', err)
        self.assertIn('Nothing was left changed', err)
        self.assertTrue(asset.exists(), 'the file must be moved back')
        self.assertEqual(record.read_bytes(), before_record)
        moved = self.archive / 'photos' / '1880s' / 'campaign-card.jpg'
        self.assertFalse(moved.exists())
        self.assertFalse((self.archive / 'photos' / '1880s').exists(),
                         'a folder this run created is removed again')
        self.assertNotIn(f'SOURCE: {SID}', store.keywords.get(str(moved), []),
                         'the just-embedded keyword is rolled back')

    def test_keyword_cleanup_failure_is_named_not_reported_clean(self) -> None:
        # doc->photos embeds a SOURCE keyword; then the record write fails and
        # rollback moves the file home and restores the record - but stripping the
        # just-embedded keyword ALSO fails. That residual metadata must be named
        # with the exact exiftool cleanup, never reported as "nothing changed".
        self._install_photo_store()
        asset, record = self._write_doc_source()
        before = record.read_bytes()
        # Keyword removal fails during rollback.
        process._run_exiftool_remove_source = (
            lambda p, s_id, extra_keywords=None, *, backup=None: 'exiftool: write locked')
        # Fail the FORWARD record write; let the rollback restore (2nd write) land.
        real_atomic = process.write_text_exact_atomic
        rec_writes = {'n': 0}

        def failing_atomic(path, text):
            if Path(path).name == record.name:
                rec_writes['n'] += 1
                if rec_writes['n'] == 1:
                    raise OSError('simulated disk full on record write')
            return real_atomic(path, text)

        process.write_text_exact_atomic = failing_atomic
        try:
            rc, _out, err = self._run_captured([SID, '--to', 'photos', '--dest', '1880s'])
        finally:
            process.write_text_exact_atomic = real_atomic

        self.assertEqual(rc, EXIT_FAILURE)
        # Record and file location are restored...
        self.assertEqual(record.read_bytes(), before)
        self.assertTrue(asset.exists())
        # ...but the residual keyword is NAMED with a cleanup step, not "clean".
        self.assertNotIn('Nothing was left changed', err)
        self.assertIn(f'SOURCE: {SID}', err)
        self.assertIn('exiftool', err)

    def test_refile_refuses_when_destination_root_is_offline(self) -> None:
        # The photos root's external drive is unplugged. Refile must refuse
        # rather than let dest_dir.mkdir(parents=True) recreate the mount path
        # on the local disk and bury the moved original under it.
        import shutil as _shutil
        self._install_photo_store()
        asset, record = self._write_doc_source()
        before = record.read_bytes()
        _shutil.rmtree(self.archive / 'photos')          # destination root offline

        rc, _out, err = self._run_captured([SID, '--to', 'photos', '--dest', '1880s'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('photos root is not reachable', err)
        self.assertEqual(record.read_bytes(), before, 'the record is untouched')
        self.assertTrue(asset.exists(), 'the original stays put')
        self.assertFalse((self.archive / 'photos').exists(),
                         'the offline mount path was NOT recreated on local disk')

    def test_forward_move_partial_that_cannot_be_removed_is_named(self) -> None:
        # A cross-filesystem forward move whose copy dies mid-write leaves a
        # partial at the destination; if that partial cannot be removed (a
        # locked handle on Windows), the rollback must NOT report a clean
        # "nothing changed" - it must name the stray file and the manual step.
        self._install_photo_store()
        asset, record = self._write_doc_source()
        before_record = record.read_bytes()
        dest = self.archive / 'photos' / '1880s' / 'campaign-card.jpg'

        real_move = process._move_file
        real_unlink = Path.unlink

        def fake_move(src: Path, dst: Path) -> None:
            # Forward move only: drop a partial at the destination, then fail.
            if src == asset:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(b'partial')
                raise OSError('simulated cross-fs copy failure')
            return real_move(src, dst)

        def fake_unlink(self, *a, **k):
            if self == dest:
                raise OSError('simulated locked handle')
            return real_unlink(self, *a, **k)

        process._move_file = fake_move
        Path.unlink = fake_unlink
        try:
            rc, _out, err = self._run_captured([SID, '--to', 'photos', '--dest', '1880s'])
        finally:
            process._move_file = real_move
            Path.unlink = real_unlink

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('partial copy was left at', err)
        self.assertIn('campaign-card.jpg', err)
        self.assertNotIn('Nothing was left changed', err)
        self.assertEqual(record.read_bytes(), before_record, 'the record is untouched')
        self.assertTrue(asset.exists(), 'the original is kept')

    def test_rollback_move_back_failure_reports_and_stays_consistent(self) -> None:
        # The reviewer-named hazard: the record write fails so rollback begins,
        # then the asset drive disconnects and the file cannot be moved back.
        # The command must NOT claim a clean rollback. It reports the move-back
        # failure, leaves the file where it is with the record pointing THERE (a
        # consistent archive, not a record aimed at a now-missing old path),
        # names the recovery command, and exits non-zero.
        self._install_photo_store()
        asset, record = self._write_doc_source()

        real_write = self._orig_write_text_exact
        writes = {'n': 0}

        def flaky_write(path, text):
            # Fail the forward record write (triggering rollback); let the
            # rollback's re-point of the record succeed so the archive is healed
            # to a consistent "still refiled" state.
            writes['n'] += 1
            if writes['n'] == 1:
                raise OSError('simulated disk full on the record write')
            return real_write(path, text)
        process.write_text_exact_atomic = flaky_write

        real_move = process._move_file

        def move_back_fails(src, dest):
            # The forward move into photos/1880s works; the rollback move back
            # OUT of it fails, as if the drive vanished mid-undo.
            if Path(src).parent.name == '1880s':
                raise OSError('simulated drive disconnected during rollback')
            return real_move(src, dest)
        process._move_file = move_back_fails
        try:
            rc, _out, err = self._run_captured([SID, '--to', 'photos', '--dest', '1880s'])
        finally:
            process._move_file = real_move

        self.assertEqual(rc, EXIT_FAILURE)
        # It does NOT falsely claim a clean, complete rollback ...
        self.assertNotIn('refile failed, rolled back:', err)
        # ... it names the failed step and the true on-disk result.
        self.assertIn('could not be moved back', err)
        moved = self.archive / 'photos' / '1880s' / 'campaign-card.jpg'
        self.assertTrue(moved.is_file(), 'the file is left where the move-back could not reach')
        self.assertFalse(asset.exists())
        self.assertIn('photos/1880s/campaign-card.jpg', err)
        # The record points at the file's real location: file and record agree.
        rec = read_record(record)
        self.assertEqual(rec['meta']['files'][0]['file'], 'photos/1880s/campaign-card.jpg')
        # A concrete recovery command is offered.
        self.assertIn('fha process refile', err)
        self.assertNotIn('Traceback', err)

    def test_rollback_double_failure_reports_inconsistency(self) -> None:
        # Worst case: the record write fails, the file cannot be moved back, AND
        # re-pointing the record also fails. The command must own up to an
        # inconsistent archive, name both halves (where the file is, where the
        # record still points), and give the manual repair - never a clean claim.
        self._install_photo_store()
        asset, record = self._write_doc_source()

        def always_boom(path, text):
            raise OSError('simulated disk full')
        process.write_text_exact_atomic = always_boom

        real_move = process._move_file

        def move_back_fails(src, dest):
            if Path(src).parent.name == '1880s':
                raise OSError('simulated drive disconnected during rollback')
            return real_move(src, dest)
        process._move_file = move_back_fails
        try:
            rc, _out, err = self._run_captured([SID, '--to', 'photos', '--dest', '1880s'])
        finally:
            process._move_file = real_move

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertNotIn('refile failed, rolled back:', err)
        self.assertIn('INCONSISTENT', err)
        self.assertIn('fha reconcile', err)
        self.assertIn('fha doctor', err)
        # The file is stranded at the destination ...
        self.assertTrue((self.archive / 'photos' / '1880s' / 'campaign-card.jpg').is_file())
        self.assertFalse(asset.exists())
        # ... and the record still names the old location it could not update.
        self.assertIn(f'documents/census/campaign-card_{SID}.jpg', err)
        self.assertNotIn('Traceback', err)

    def test_rollback_record_restore_failure_reports_damaged_record(self) -> None:
        # The reviewer-named hazard: the forward record write fails so rollback
        # begins, the file IS moved back home, but restoring the record's
        # original text ALSO fails. The move-back succeeded, yet the record on
        # disk is now truncated - the command must NOT call this a clean
        # rollback. It names the damaged record and the recovery (restore from
        # git or a backup, then `fha lint`), and exits non-zero.
        store = self._install_photo_store()
        asset, record = self._write_doc_source()

        def always_boom(_path, _text):
            raise OSError('simulated disk full')
        process.write_text_exact_atomic = always_boom

        rc, _out, err = self._run_captured([SID, '--to', 'photos', '--dest', '1880s'])

        self.assertEqual(rc, EXIT_FAILURE)
        # It does NOT claim the clean, complete rollback ...
        self.assertNotIn('Nothing was left changed', err)
        self.assertNotIn('refile failed, rolled back:', err)
        # ... it names the damaged record and that the record is damaged.
        self.assertIn('could not be restored', err)
        self.assertIn('damaged', err)
        rel_record = record.relative_to(self.archive).as_posix()
        self.assertIn(rel_record, err)
        # The file WAS moved back to its original location.
        self.assertTrue(asset.exists(), 'the file must be moved back home')
        moved = self.archive / 'photos' / '1880s' / 'campaign-card.jpg'
        self.assertFalse(moved.exists())
        self.assertNotIn(f'SOURCE: {SID}', store.keywords.get(str(moved), []))
        # The recovery is spelled out: restore from git/backup, then lint.
        self.assertIn('git', err)
        self.assertIn('fha lint', err)
        self.assertNotIn('Traceback', err)

    def test_refile_preserves_trailing_inventory_comment(self) -> None:
        # A hand-written note on the file: line ('# fragile original') is the
        # owner's annotation. A successful refile rewrites the path but must
        # carry the comment onto the new line, never silently drop it.
        self._install_photo_store()
        asset = self.archive / 'documents' / 'census' / f'campaign-card_{SID}.jpg'
        asset.write_bytes(b'jpegbytes')
        entry = (f'  - file: documents/census/campaign-card_{SID}.jpg  # fragile original\n'
                 '    role: primary\n'
                 '    original_filename: campaign-card.jpg\n')
        record = self.archive / 'sources' / 'census' / f'campaign-card_{SID}.md'
        record.write_bytes(_record_text(entry).encode('utf-8'))

        rc, _out, err = self._run_captured([SID, '--to', 'photos', '--dest', '1880s'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertNotIn('Traceback', err)
        body = record.read_text(encoding='utf-8')
        # The rewritten inventory line points at the new location AND keeps the note.
        self.assertIn('- file: photos/1880s/campaign-card.jpg  # fragile original', body)
        # The comment did not corrupt the value: it parses back to the new path.
        rec = read_record(record)
        self.assertEqual(rec['meta']['files'][0]['file'], 'photos/1880s/campaign-card.jpg')

    def test_record_keeps_lf_line_endings(self) -> None:
        self._install_photo_store()
        _asset, record = self._write_doc_source()  # written via write_bytes: pure LF

        rc = self._run([SID, '--to', 'photos', '--dest', '1880s'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertNotIn(b'\r', record.read_bytes())

    # -- CLI plumbing ----------------------------------------------------------

    def test_fha_dispatcher_intercepts_process_refile(self) -> None:
        self._install_photo_store()
        asset, _record = self._write_doc_source()
        import fha

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fha.main(['process', 'refile', SID, '--to', 'photos',
                           '--dest', '1880s', '--root', str(self.archive)])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(asset.exists())
        self.assertTrue((self.archive / 'photos' / '1880s' / 'campaign-card.jpg').is_file())

    def test_fha_dispatcher_dual_position_root(self) -> None:
        self._install_photo_store()
        asset, _record = self._write_doc_source()
        import fha

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fha.main(['process', '--root', str(self.archive), 'refile', SID,
                           '--to', 'photos', '--dest', '1880s'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(asset.exists())

    # -- the confirm gate, accept path -----------------------------------------

    def test_photo_to_doc_interactive_accept_moves_file(self) -> None:
        # The decline and --yes paths are covered; without this, a confirm gate
        # that ALWAYS declined would pass the whole suite.
        self._install_photo_store()
        process._stdin_is_interactive = lambda: True
        process._prompt = lambda _msg: 'y'
        asset, record = self._write_photo_source()

        rc = self._run([SID, '--to', 'documents', '--type', 'photo'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(asset.exists())
        moved = self.archive / 'documents' / 'photos' / f'portrait_{SID}.jpg'
        self.assertTrue(moved.is_file())
        self.assertIn(f'documents/photos/portrait_{SID}.jpg',
                      record.read_text(encoding='utf-8'))

    def test_eof_at_prompt_refuses_with_yes_hint(self) -> None:
        # A closed stdin (a scheduler run where isatty() lies, or Ctrl+D) must
        # get the crafted "re-run with --yes" refusal, NOT the generic
        # catch-all's misleading "run fha lint".
        self._install_photo_store()
        process._stdin_is_interactive = lambda: True

        def eof(_msg):
            raise EOFError('EOF when reading a line')
        process._prompt = eof
        asset, record = self._write_photo_source()
        before = record.read_bytes()

        rc, _out, err = self._run_captured([SID, '--to', 'documents', '--type', 'photo'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('--yes', err)
        self.assertNotIn('fha lint', err)
        self.assertNotIn('Traceback', err)
        self.assertTrue(asset.exists())
        self.assertEqual(record.read_bytes(), before)

    # -- derived-entry selection contract --------------------------------------

    def test_derived_entry_never_the_default_pick(self) -> None:
        self._install_photo_store()
        card, transcript, _record = self._write_doc_source_with_derived()

        rc = self._run([SID, '--to', 'photos', '--dest', '1880s'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(card.exists())
        self.assertTrue((self.archive / 'photos' / '1880s' / 'campaign-card.jpg').is_file())
        self.assertTrue(transcript.exists(), 'the derived entry is never the default pick')

    def test_derived_entry_moves_only_when_named(self) -> None:
        self._install_photo_store()
        card, transcript, _record = self._write_doc_source_with_derived()

        rc, _out, _err = self._run_captured(
            [SID, '--file', f'campaign-card_{SID}.txt', '--to', 'photos', '--dest', '1880s'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(transcript.exists())
        self.assertTrue((self.archive / 'photos' / '1880s' / 'campaign-card.txt').is_file())
        self.assertTrue(card.exists(), 'the primary stays when the derived file is named')

    # -- S13 rename grammar edges ----------------------------------------------

    def test_doc_to_photo_strips_role_suffix(self) -> None:
        # No original_filename, role != primary: the -{role} suffix is stripped
        # along with _{S-id} so no jargon lands in the photo library.
        self._install_photo_store()
        asset = self.archive / 'documents' / 'census' / f'campaign-card-back_{SID}.jpg'
        asset.write_bytes(b'jpegbytes')
        entry = (f'  - file: documents/census/campaign-card-back_{SID}.jpg\n'
                 '    role: back\n')
        record = self.archive / 'sources' / 'census' / f'campaign-card_{SID}.md'
        record.write_bytes(_record_text(entry).encode('utf-8'))

        rc = self._run([SID, '--to', 'photos', '--dest', '1880s'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(asset.exists())
        self.assertTrue((self.archive / 'photos' / '1880s' / 'campaign-card.jpg').is_file())

    def test_photo_to_doc_named_into_grammar_with_role_and_copy(self) -> None:
        # The {slug}[-{copy}][-{role}]_{S-id} construction with a non-empty
        # suffix - the only photo fixture elsewhere is role: primary (suffix '').
        self._install_photo_store()
        asset = self.archive / 'photos' / '1880' / 'portrait.jpg'
        asset.write_bytes(b'jpegbytes')
        entry = ('  - file: photos/1880/portrait.jpg\n'
                 '    role: back\n'
                 '    copy: b\n')
        record = self.archive / 'sources' / 'photos' / f'portrait_{SID}.md'
        record.write_bytes(_record_text(entry, source_type='photo').encode('utf-8'))

        rc = self._run([SID, '--to', 'documents', '--type', 'photo', '--yes'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(asset.exists())
        moved = self.archive / 'documents' / 'photos' / f'portrait-b-back_{SID}.jpg'
        self.assertTrue(moved.is_file(), 'copy then role suffix, in grammar order')
        rec = read_record(record)
        self.assertEqual(rec['meta']['files'][0]['file'],
                         f'documents/photos/portrait-b-back_{SID}.jpg')

    # -- YAML-hostile paths: round-trip, not corruption ------------------------

    def test_dest_with_hash_round_trips_not_truncated(self) -> None:
        # ' #' opens a YAML comment in an unquoted scalar. The new alias must be
        # quoted so read_record parses back the WHOLE path, not a truncation.
        self._install_photo_store()
        _asset, record = self._write_doc_source()

        rc = self._run([SID, '--to', 'photos', '--dest', 'Box #3'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertTrue((self.archive / 'photos' / 'Box #3' / 'campaign-card.jpg').is_file())
        rec = read_record(record)
        self.assertEqual(rec['meta']['files'][0]['file'], 'photos/Box #3/campaign-card.jpg')
        self.assertEqual(rec['meta'].get('parse_errors', []), [])

    def test_original_filename_with_hash_round_trips(self) -> None:
        self._install_photo_store()
        asset = self.archive / 'documents' / 'census' / f'campaign-card_{SID}.jpg'
        asset.write_bytes(b'jpegbytes')
        entry = (f'  - file: documents/census/campaign-card_{SID}.jpg\n'
                 '    role: primary\n'
                 "    original_filename: 'Scan #12.jpg'\n")
        record = self.archive / 'sources' / 'census' / f'campaign-card_{SID}.md'
        record.write_bytes(_record_text(entry).encode('utf-8'))

        rc = self._run([SID, '--to', 'photos', '--dest', '1880s'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertTrue((self.archive / 'photos' / '1880s' / 'Scan #12.jpg').is_file())
        rec = read_record(record)
        self.assertEqual(rec['meta']['files'][0]['file'], 'photos/1880s/Scan #12.jpg')

    def test_quoted_hash_alias_photo_to_doc_matches(self) -> None:
        # A photos-root file legitimately keeps a '#' name (SPEC 13, never
        # renamed), so its record alias is quoted. The rewriter must MATCH it
        # (quote-strip before comment-split), not refuse spuriously.
        self._install_photo_store()
        asset = self.archive / 'photos' / '1880' / 'Scan #12.jpg'
        asset.write_bytes(b'jpegbytes')
        entry = ("  - file: 'photos/1880/Scan #12.jpg'\n"
                 '    role: primary\n')
        record = self.archive / 'sources' / 'photos' / f'portrait_{SID}.md'
        record.write_bytes(_record_text(entry, source_type='photo').encode('utf-8'))

        rc = self._run([SID, '--to', 'documents', '--type', 'photo', '--yes', '--dest', 'letters'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(asset.exists())
        self.assertTrue((self.archive / 'documents' / 'letters' / f'portrait_{SID}.jpg').is_file())

    # -- record-surgery refusals -----------------------------------------------

    def test_dest_trailing_dot_refused(self) -> None:
        self._install_photo_store()
        asset, record = self._write_doc_source()
        before = record.read_bytes()

        for bad_dest in ('1880s.', '1880s. /sub'):
            rc, _out, err = self._run_captured([SID, '--to', 'photos', '--dest', bad_dest])
            self.assertEqual(rc, EXIT_FAILURE, f'--dest {bad_dest!r} must refuse')
            self.assertIn('--dest', err)
            self.assertNotIn('Traceback', err)
            self.assertTrue(asset.exists())
            self.assertEqual(record.read_bytes(), before)

    def test_mangled_original_filename_refused(self) -> None:
        self._install_photo_store()
        asset = self.archive / 'documents' / 'census' / f'campaign-card_{SID}.jpg'
        asset.write_bytes(b'jpegbytes')
        entry = (f'  - file: documents/census/campaign-card_{SID}.jpg\n'
                 '    role: primary\n'
                 "    original_filename: '../1880/other.jpg'\n")
        record = self.archive / 'sources' / 'census' / f'campaign-card_{SID}.md'
        record.write_bytes(_record_text(entry).encode('utf-8'))
        before = record.read_bytes()

        rc, _out, err = self._run_captured([SID, '--to', 'photos', '--dest', '1880s'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('original_filename', err)
        self.assertNotIn('Traceback', err)
        self.assertTrue(asset.exists())
        self.assertEqual(record.read_bytes(), before)

    def test_duplicate_file_entry_refused(self) -> None:
        # The same path listed twice: the rewriter cannot know which entry to
        # touch, so it refuses rather than half-rewriting the wrong one.
        self._install_photo_store()
        card = self.archive / 'documents' / 'census' / 'card.jpg'
        card.write_bytes(b'jpegbytes')
        entry = ('  - file: documents/census/card.jpg\n'
                 '    role: transcript\n'
                 '    derived: true\n'
                 '  - file: documents/census/card.jpg\n'
                 '    role: primary\n')
        record = self.archive / 'sources' / 'census' / f'campaign-card_{SID}.md'
        record.write_bytes(_record_text(entry).encode('utf-8'))
        before = record.read_bytes()

        rc, _out, err = self._run_captured(
            [SID, '--file', 'card.jpg', '--to', 'photos', '--dest', '1880s'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('more than one', err)
        self.assertTrue(card.exists())
        self.assertEqual(record.read_bytes(), before)

    def test_flow_style_frontmatter_with_body_bullet_refuses(self) -> None:
        # The inventory is flow-style YAML the line matcher cannot see; a body
        # bullet reads '- file: <same path>'. The scan is fence-bounded, so it
        # never rewrites the body line - it refuses, archive untouched. (The
        # file carries its own S-id suffix, SPEC §13, so refile's identity
        # check passes and this test still exercises the line-matcher refusal.)
        self._install_photo_store()
        card = self.archive / 'documents' / 'census' / f'card_{SID}.jpg'
        card.write_bytes(b'jpegbytes')
        record = self.archive / 'sources' / 'census' / f'campaign-card_{SID}.md'
        record.write_bytes((
            '---\n'
            f'id: {SID}\n'
            f'aliases: [{SID}]\n'
            'title: Campaign card\n'
            'source_type: census\n'
            'source_class: original\n'
            'repository: unknown\n'
            'citation: Campaign card\n'
            'people: []\n'
            f'files: [{{file: documents/census/card_{SID}.jpg, role: primary}}]\n'
            'created: 2026-07-01\n'
            '---\n'
            '\n'
            '## Claims\n'
            '```yaml\n'
            '```\n'
            '\n'
            '## Notes\n'
            f'- file: documents/census/card_{SID}.jpg\n'
        ).encode('utf-8'))
        before = record.read_bytes()

        rc, _out, err = self._run_captured([SID, '--to', 'photos', '--dest', '1880s'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('could not find the files: line', err)
        self.assertTrue(card.exists())
        self.assertEqual(record.read_bytes(), before)

    def test_undecodable_record_refused_cleanly(self) -> None:
        # #68: a source record saved in another encoding (cp1252, a Windows
        # editor's default) must get a clean ProcessError naming the real fix
        # (re-save as UTF-8), not a raw UnicodeDecodeError traceback, and the
        # refile must refuse before anything moves - same contract as the
        # malformed-frontmatter refusal above, different cause and message.
        self._install_photo_store()
        card = self.archive / 'documents' / 'census' / 'card.jpg'
        card.write_bytes(b'jpegbytes')
        record = self.archive / 'sources' / 'census' / f'campaign-card_{SID}.md'
        record.write_bytes((
            '---\n'
            f'id: {SID}\n'
            f'aliases: [{SID}]\n'
            'title: Kraków campaign card\n'
            'source_type: census\n'
            'source_class: original\n'
            'repository: unknown\n'
            'citation: Kraków campaign card\n'
            'people: []\n'
            'files: [{file: documents/census/card.jpg, role: primary}]\n'
            'created: 2026-07-01\n'
            '---\n'
            '\n'
            '## Claims\n'
            '```yaml\n'
            '```\n'
            '\n'
            '## Notes\n'
            'Born in Kraków.\n'
        ).encode('cp1252'))
        before = record.read_bytes()

        rc, _out, err = self._run_captured([SID, '--to', 'photos', '--dest', '1880s'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertNotIn('Traceback', err)
        self.assertIn('not saved as UTF-8', err)
        self.assertTrue(card.exists())  # nothing moved
        self.assertEqual(record.read_bytes(), before)  # record untouched

    def test_move_fallback_unlink_failure_leaves_no_stray_copy(self) -> None:
        # rename fails, copy2 succeeds, src.unlink fails (a locked source on
        # Windows): the destination copy must be cleaned up and the original
        # kept, so the run rolls back to exactly one file and no debris.
        self._install_photo_store()
        asset, record = self._write_doc_source()
        before_record = record.read_bytes()

        real_rename = Path.rename
        real_unlink = Path.unlink

        def no_rename(self, target):
            raise OSError('simulated cross-filesystem rename')

        def locked_unlink(self, *a, **k):
            if self.name == asset.name:
                raise PermissionError('simulated locked source')
            return real_unlink(self, *a, **k)

        Path.rename = no_rename
        Path.unlink = locked_unlink
        try:
            rc, _out, err = self._run_captured([SID, '--to', 'photos', '--dest', '1880s'])
        finally:
            Path.rename = real_rename
            Path.unlink = real_unlink

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('rolled back', err)
        self.assertTrue(asset.exists(), 'the original is kept as the sole copy')
        self.assertFalse((self.archive / 'photos' / '1880s' / 'campaign-card.jpg').exists(),
                         'the partial destination copy is cleaned up')
        self.assertFalse((self.archive / 'photos' / '1880s').exists(),
                         'a folder this run created is removed again')
        self.assertEqual(record.read_bytes(), before_record)

    # -- refile carries the type across the roots (issue #59) ------------------
    #
    # Before this, refile moved the asset and rewrote files: but left the record
    # typed photo in sources/photos/, and defaulted the asset destination to the
    # documents root plus the record own type subdirectory - which for a
    # photo-typed record is a junk documents/photos/ folder it created for the
    # purpose. Both halves were left as hand-edits fha lint flags neither.

    def test_photo_typed_record_without_type_refuses_and_names_the_flag(self) -> None:
        self._install_photo_store()
        asset, record = self._write_photo_source()
        before = record.read_bytes()

        rc, _out, err = self._run_captured([SID, '--to', 'documents', '--yes'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('--type', err)
        self.assertIn('census', err)
        self.assertNotIn('Traceback', err)
        self.assertTrue(asset.is_file(), 'nothing moved')
        self.assertEqual(record.read_bytes(), before)
        self.assertFalse((self.archive / 'documents' / 'photos').exists(),
                         'no junk documents/photos/ folder is ever created')

    def test_dry_run_prints_the_identical_refusal(self) -> None:
        self._install_photo_store()
        self._write_photo_source()

        rc, _out, err = self._run_captured([SID, '--to', 'documents', '--dry-run'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('--type', err)
        self.assertFalse((self.archive / 'documents' / 'photos').exists())

    def test_type_moves_asset_record_and_rewrites_source_type(self) -> None:
        self._install_photo_store()
        asset, record = self._write_photo_source()

        rc, out, _err = self._run_captured(
            [SID, '--to', 'documents', '--yes', '--type', 'census'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(asset.exists())
        moved = self.archive / 'documents' / 'census' / f'portrait_{SID}.jpg'
        self.assertTrue(moved.is_file())
        self.assertFalse(record.exists(), 'the record left sources/photos/')
        new_record = self.archive / 'sources' / 'census' / f'portrait_{SID}.md'
        self.assertTrue(new_record.is_file(), 'the record followed its type')
        rec = read_record(new_record)
        self.assertEqual(rec['meta']['source_type'], 'census')
        self.assertEqual(rec['meta']['files'][0]['file'],
                         f'documents/census/portrait_{SID}.jpg')
        self.assertIn('census', out)
        self.assertFalse((self.archive / 'documents' / 'photos').exists())

    def test_dry_run_with_type_writes_nothing(self) -> None:
        self._install_photo_store()
        self._write_photo_source()
        before = self._snapshot_tree(self.archive)

        rc, out, _err = self._run_captured(
            [SID, '--to', 'documents', '--type', 'census', '--dry-run'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(self._snapshot_tree(self.archive), before)
        self.assertIn('sources/census/', out.replace('\\', '/'))

    def test_explicit_photo_type_into_documents_is_honoured(self) -> None:
        # Asked for explicitly, `documents/photos/` is the human's choice, not
        # a folder the tool invented: the record keeps its type and its place.
        self._install_photo_store()
        asset, record = self._write_photo_source()

        rc = self._run([SID, '--to', 'documents', '--yes', '--type', 'photo'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(asset.exists())
        self.assertTrue((self.archive / 'documents' / 'photos'
                         / f'portrait_{SID}.jpg').is_file())
        self.assertTrue(record.is_file(), 'the type did not change, so neither did the record')

    def test_unknown_type_refused_before_anything_moves(self) -> None:
        self._install_photo_store()
        asset, record = self._write_photo_source()
        before = self._snapshot_tree(self.archive)

        rc, _out, err = self._run_captured(
            [SID, '--to', 'documents', '--yes', '--type', 'bogus'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('census', err)
        self.assertNotIn('Traceback', err)
        self.assertTrue(asset.is_file())
        self.assertEqual(self._snapshot_tree(self.archive), before)
        self.assertTrue(record.is_file())

    def test_non_photo_typed_record_without_type_is_unchanged(self) -> None:
        # A census-typed source whose scan was filed in the photo library needs
        # no re-typing: its type was right all along, only its filing was wrong.
        self._install_photo_store()
        asset = self.archive / 'photos' / '1880' / 'sheet.jpg'
        asset.write_bytes(b'jpegbytes')
        entry = '  - file: photos/1880/sheet.jpg\n    role: primary\n'
        record = self.archive / 'sources' / 'census' / f'sheet_{SID}.md'
        record.write_bytes(_record_text(entry).encode('utf-8'))

        rc = self._run([SID, '--to', 'documents', '--yes'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertTrue((self.archive / 'documents' / 'census'
                         / f'sheet_{SID}.jpg').is_file())
        self.assertTrue(record.is_file(), 'the record stayed in sources/census/')
        self.assertEqual(read_record(record)['meta']['source_type'], 'census')

    def test_already_under_documents_keeps_the_reconcile_message(self) -> None:
        # The type refusal must not shadow a better-fitting one: a photo-typed
        # record whose file is ALREADY in the documents root is a reconcile
        # question, not a "what kind of record is this" question.
        self._install_photo_store()
        asset = self.archive / 'documents' / 'census' / f'card_{SID}.jpg'
        asset.write_bytes(b'jpegbytes')
        entry = f'  - file: documents/census/card_{SID}.jpg\n    role: primary\n'
        record = self.archive / 'sources' / 'photos' / f'card_{SID}.md'
        record.write_bytes(_record_text(entry, source_type='photo').encode('utf-8'))

        rc, _out, err = self._run_captured([SID, '--to', 'documents', '--yes'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('fha reconcile', err)
        self.assertNotIn('--type', err)

    def test_type_carries_across_into_photos_too(self) -> None:
        # The mirror direction: a document source that turns out to be a family
        # photo is re-typed and its record moves the same way.
        store = self._install_photo_store()
        asset, record = self._write_doc_source()

        rc = self._run([SID, '--to', 'photos', '--dest', '1880s', '--type', 'photo'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(asset.exists())
        moved = self.archive / 'photos' / '1880s' / 'campaign-card.jpg'
        self.assertTrue(moved.is_file())
        self.assertEqual(store.keywords[str(moved)], [f'SOURCE: {SID}'])
        self.assertFalse(record.exists())
        new_record = self.archive / 'sources' / 'photos' / f'campaign-card_{SID}.md'
        self.assertTrue(new_record.is_file())
        self.assertEqual(read_record(new_record)['meta']['source_type'], 'photo')

    def test_record_move_collision_refused_before_anything_moves(self) -> None:
        self._install_photo_store()
        asset, record = self._write_photo_source()
        (self.archive / 'sources' / 'census').mkdir(parents=True, exist_ok=True)
        squatter = self.archive / 'sources' / 'census' / f'portrait_{SID}.md'
        squatter.write_text('someone else got here first\n', encoding='utf-8')
        before = self._snapshot_tree(self.archive)

        rc, _out, err = self._run_captured(
            [SID, '--to', 'documents', '--yes', '--type', 'census'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('portrait', err)
        self.assertTrue(asset.is_file())
        self.assertTrue(record.is_file())
        self.assertEqual(self._snapshot_tree(self.archive), before)

    def test_rollback_puts_the_record_back_in_its_own_folder(self) -> None:
        self._install_photo_store()
        asset, record = self._write_photo_source()
        before_text = record.read_bytes()

        real_write = process.write_text_exact_atomic
        calls = {'n': 0}

        def flaky(path: Path, text: str) -> None:
            calls['n'] += 1
            if calls['n'] == 1:
                raise OSError('simulated record write failure')
            real_write(path, text)

        process.write_text_exact_atomic = flaky
        rc, _out, _err = self._run_captured(
            [SID, '--to', 'documents', '--yes', '--type', 'census'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertTrue(asset.is_file(), 'the photo went home')
        self.assertTrue(record.is_file(), 'the record went home')
        self.assertEqual(record.read_bytes(), before_text)
        self.assertFalse((self.archive / 'sources' / 'census'
                          / f'portrait_{SID}.md').exists())

class RefilePickEntryUnitTests(unittest.TestCase):
    """`_refile_pick_entry` normalizes a stored alias before basename matching.

    Aliases are forward-slash by contract, but a record written on Windows can
    carry backslashes ('documents\\letters\\scan.pdf'); on POSIX,
    Path(alias).name is the whole string, so an un-normalized `--file scan.pdf`
    lookup would miss a listed file and refuse. Mirrors the reconcile fix.
    """

    def test_windows_alias_matched_by_basename(self) -> None:
        entries = [
            {'file': 'documents\\letters\\scan.pdf', 'role': 'primary'},
            {'file': 'documents\\letters\\scan.pdf.txt', 'role': 'transcript',
             'derived': True},
        ]
        picked = process._refile_pick_entry(entries, 'scan.pdf', 'letter.md')
        self.assertEqual(picked['file'], 'documents\\letters\\scan.pdf')

    def test_windows_alias_matched_by_full_posix_path(self) -> None:
        entries = [{'file': 'documents\\letters\\scan.pdf'}]
        picked = process._refile_pick_entry(
            entries, 'documents/letters/scan.pdf', 'letter.md')
        self.assertEqual(picked['file'], 'documents\\letters\\scan.pdf')

    def test_refusal_lists_normalized_basenames_not_backslash_strings(self) -> None:
        entries = [
            {'file': 'documents\\census\\one.pdf'},
            {'file': 'documents\\census\\two.pdf'},
        ]
        with self.assertRaises(process.ProcessError) as ctx:
            process._refile_pick_entry(entries, 'nope.pdf', 'census.md')
        msg = str(ctx.exception)
        self.assertIn('one.pdf', msg)
        self.assertIn('two.pdf', msg)
        self.assertNotIn('documents\\census\\one.pdf', msg)


if __name__ == '__main__':
    unittest.main()
