"""`fha source merge`: two source records for one piece of evidence become one.

When an index entry and the document it indexes were filed as two sources,
or one record was saved from two sites, the owner merges them. The keeper
takes the retiree's files (documents renamed into its grammar, photos never
renamed, every embedded SOURCE: keyword changed), its claims with their ids,
its notes, people, links and AI passes; it lists the retiree's id in
`aliases:` and a `merged_from:` entry; and the retiree's record is removed
(SPEC §9 "Merged sources").

Covered here: the documents-root happy path, the photos-root keyword-only
path, each refusal (with nothing written, and the same refusal under
--dry-run), --dry-run writing nothing, and a failure part-way through rolling
everything back.

exiftool is never called: a fake keyword store, keyed by each file's bytes so
it follows a rename, stands in for `source._merge_read_keywords` and
`source._merge_write_keywords`.

Run: python -m pytest tests/test_source_merge.py -q   (from the repo root)
"""

import contextlib
import datetime
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import source
from _lib import (EXIT_CLEAN, EXIT_ERRORS, EXIT_FAILURE, EXIT_WARNINGS,
                  ExiftoolUnavailableError, apply_private_fence, read_record)

KEEP = 's-2b3c4d5e6f'
RETIRE = 's-7q2x9c4m1k'
OTHER = 's-9z8y7x6w5v'
THOMAS = 'p-de957bcda1'
MARGARET = 'p-cd795c61e0'
ETHEL = 'p-c4b26bb4bc'


class FakeKeywords:
    """Embedded keywords, keyed by a file's bytes so they survive a rename."""

    def __init__(self) -> None:
        self.by_bytes: dict[bytes, list[str]] = {}
        self.fail_on: set[str] = set()      # file names whose write fails
        self.unavailable = False
        self.writes: list[str] = []

    def read(self, path: Path) -> list[str]:
        if self.unavailable:
            raise ExiftoolUnavailableError('simulated: exiftool not on PATH')
        return list(self.by_bytes.get(Path(path).read_bytes(), []))

    def write(self, path: Path, *, add, remove, backup) -> str | None:
        path = Path(path)
        if path.name in self.fail_on:
            return 'simulated exiftool failure'
        self.writes.append(path.name)
        old = path.read_bytes()
        kws = [k for k in self.by_bytes.get(old, []) if k not in remove]
        for k in add:
            if k not in kws:
                kws.append(k)
        # Like exiftool, a write changes the file's bytes; putting the old
        # bytes back (the documents rollback) brings the old keywords back.
        new = old + f' kw{len(self.writes)}'.encode()
        path.write_bytes(new)
        self.by_bytes[new] = kws
        return None

    def of(self, path: Path) -> list[str]:
        return self.by_bytes.get(path.read_bytes(), [])


def _claim(cid: str, value: str, pid: str, status: str = 'accepted') -> str:
    return (f'- value: {value}\n  id: {cid}\n  type: residence\n  persons:\n'
            f'    - "[[{pid}]]"\n  status: {status}\n')


def _record(sid: str, *, title: str, files: list[str], claims: list[str],
            people: list[str], citation: str, extra: str = '', notes: str = '',
            body_extra: str = '') -> str:
    fm = [f'id: {sid}', f'aliases: [{sid}, {title.lower().replace(" ", "-")}]',
          f'title: {title}', 'source_type: letter', 'source_class: original',
          'repository: family collection', f'citation: {citation}']
    fm.append('people:' if people else 'people: []')
    fm += [f'  - "[[{p}]]"' for p in people]
    fm.append('files:')
    fm += files
    if extra:
        fm.append(extra.rstrip('\n'))
    fm.append('created: 2026-06-10')
    return ('---\n' + '\n'.join(fm) + '\n---\n\n'
            '> **This source\'s record - yours to write.**\n\n'
            '## Claims\n```yaml\n' + '\n'.join(claims) + '```\n\n'
            '## Notes\n' + (notes or '(none yet)') + '\n' + body_extra)


class MergeTestCase(unittest.TestCase):
    """An archive whose photos and documents roots are folders beside it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.archive = self.tmp / 'archive'
        self.docs = self.tmp / 'lib-docs'
        self.photos = self.tmp / 'lib-photos'
        for d in (self.archive / 'sources' / 'letter', self.archive / 'sources' / 'photos',
                  self.archive / 'people', self.docs / 'letter', self.photos / '1880'):
            d.mkdir(parents=True)
        (self.archive / 'fha.yaml').write_text(
            f'roots:\n  photos: {self.photos.as_posix()}\n'
            f'  documents: {self.docs.as_posix()}\n', encoding='utf-8')
        self.kw = FakeKeywords()
        self._saved = (source._merge_read_keywords, source._merge_write_keywords,
                       source._merge_today, source.write_text_exact_atomic)
        source._merge_read_keywords = self.kw.read
        source._merge_write_keywords = self.kw.write
        source._merge_today = lambda: datetime.date(2026, 11, 20)

    def tearDown(self) -> None:
        (source._merge_read_keywords, source._merge_write_keywords,
         source._merge_today, source.write_text_exact_atomic) = self._saved
        self._tmp.cleanup()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _put(self, path: Path, data: bytes, keywords: list[str] | None = None) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if keywords is not None:
            self.kw.by_bytes[data] = list(keywords)
        return path

    def _write(self, rel: str, text: str) -> Path:
        path = self.archive / rel
        path.write_text(text, encoding='utf-8')
        return path

    def _run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = source._standalone_main(['merge', *argv, '--root', str(self.archive)])
        return rc, out.getvalue(), err.getvalue()

    def _snapshot(self) -> dict:
        files = {p.relative_to(self.tmp).as_posix(): p.read_bytes()
                 for p in self.tmp.rglob('*') if p.is_file() and '.cache' not in p.parts}
        return {'files': files, 'keywords': {k: list(v) for k, v in self.kw.by_bytes.items()}}

    def _letters(self, *, retiree_people=(THOMAS,), keeper_people=(THOMAS,)) -> tuple[Path, Path]:
        """A keeper letter and a retiree letter (a second capture of it, with a
        second page) in the documents root; return the two record paths."""
        self._put(self.docs / 'letter' / f'letter-from-aunt_{KEEP}.pdf', b'%PDF keeper',
                  [f'SOURCE: {KEEP}'])
        self._put(self.docs / 'letter' / f'aunt-letter-copy_{RETIRE}.pdf', b'%PDF retiree',
                  [f'SOURCE: {RETIRE}', 'Margaret Hartley'])
        self._put(self.docs / 'letter' / f'aunt-letter-copy-page-2_{RETIRE}.jpg',
                  b'\xff\xd8 retiree page 2', [f'SOURCE: {RETIRE}'])
        self._put(self.docs / 'letter' / f'aunt-letter-copy-transcript_{RETIRE}.md',
                  b'transcript text')
        keeper = self._write(f'sources/letter/letter-from-aunt_{KEEP}.md', _record(
            KEEP, title='Letter from Aunt Margaret', citation='Letter, family collection.',
            files=[f'  - file: documents/letter/letter-from-aunt_{KEEP}.pdf',
                   '    is_primary: true', '    original_filename: scan-0412.pdf'],
            claims=[_claim('c-1a2b3c4d5e', 'Thomas in Fairview', p) for p in keeper_people[:1]],
            people=list(keeper_people), notes='Kept in the blue box.'))
        retiree = self._write(f'sources/letter/aunt-letter-copy_{RETIRE}.md', _record(
            RETIRE, title='Aunt letter (second scan)',
            citation='Letter from Aunt Margaret, scanned again 2026.',
            files=[f'  - file: documents/letter/aunt-letter-copy_{RETIRE}.pdf',
                   '    is_primary: true', '    original_filename: IMG_0001.pdf',
                   f'  - file: documents/letter/aunt-letter-copy-page-2_{RETIRE}.jpg',
                   '    role: page-2',
                   f'  - file: documents/letter/aunt-letter-copy-transcript_{RETIRE}.md',
                   '    role: transcript', '    derived: true'],
            claims=[_claim('c-6f7g8h9jka', 'Thomas wrote from Easton', p)
                    for p in retiree_people[:1]],
            people=list(retiree_people) + [MARGARET],
            extra=('external_links:\n  - url: https://example.org/letter\n'
                   '    accessed: 2026-06-10\nai_passes:\n'
                   "  - {date: '2026-06-11', by: claude-opus-4, task: claims}"),
            notes='Second scan, better light.',
            body_extra='\n## Stories\nThe letter mentions the move west.\n'))
        return keeper, retiree


class DocumentsMergeTests(MergeTestCase):

    def test_documents_are_rekeyed_and_the_retiree_record_removed(self) -> None:
        keeper, retiree = self._letters()

        rc, out, err = self._run(KEEP, RETIRE)

        self.assertEqual(rc, EXIT_CLEAN, out + err)
        self.assertFalse(retiree.exists())
        letter = self.docs / 'letter'
        main_copy = letter / f'letter-from-aunt-b_{KEEP}.pdf'
        page = letter / f'letter-from-aunt-page-2_{KEEP}.jpg'
        transcript = letter / f'letter-from-aunt-transcript_{KEEP}.md'
        for path in (main_copy, page, transcript):
            self.assertTrue(path.is_file(), sorted(p.name for p in letter.iterdir()))
        self.assertEqual(sorted(p.name for p in letter.iterdir()),
                         sorted([f'letter-from-aunt_{KEEP}.pdf', main_copy.name, page.name,
                                 transcript.name]))
        # The keyword follows the file: the keeper's in, the retiree's out,
        # every other keyword untouched. A Markdown file takes none.
        self.assertEqual(self.kw.of(main_copy), ['Margaret Hartley', f'SOURCE: {KEEP}'])
        self.assertEqual(self.kw.of(page), [f'SOURCE: {KEEP}'])
        self.assertNotIn(transcript.name, self.kw.writes)

        rec = read_record(keeper)
        meta = rec['meta']
        self.assertIn(RETIRE, [str(a).lower() for a in meta['aliases']])
        self.assertIn('aunt-letter-(second-scan)', meta['aliases'])
        files = meta['files']
        self.assertEqual(len(files), 4)
        self.assertEqual(sum(1 for f in files if str(f.get('is_primary')).lower() == 'true'), 1)
        moved = {f['file']: f for f in files[1:]}
        first = moved[f'documents/letter/{main_copy.name}']
        self.assertEqual(first['copy'], 'b')
        self.assertNotIn('is_primary', first)
        # The name it had before it was ever processed stays on record.
        self.assertEqual(first['original_filename'], 'IMG_0001.pdf')
        self.assertEqual(moved[f'documents/letter/{page.name}']['original_filename'],
                         f'aunt-letter-copy-page-2_{RETIRE}.jpg')
        self.assertEqual(moved[f'documents/letter/{page.name}']['role'], 'page-2')
        self.assertEqual(str(moved[f'documents/letter/{transcript.name}']['derived']).lower(),
                         'true')

        self.assertEqual([c['id'] for c in rec['claims']], ['c-1a2b3c4d5e', 'c-6f7g8h9jka'])
        people = ' '.join(str(p) for p in meta['people'])
        self.assertIn(MARGARET, people)
        self.assertEqual(people.count(THOMAS), 1)
        self.assertEqual([str(e['url']) for e in meta['external_links']],
                         ['https://example.org/letter'])
        self.assertEqual(len(meta['ai_passes']), 1)
        self.assertEqual(meta['citation'], 'Letter, family collection.')

        merged = meta['merged_from']
        self.assertEqual(len(merged), 1)
        entry = merged[0]
        self.assertEqual(entry['source'], RETIRE)
        self.assertEqual(str(entry['date']), '2026-11-20')
        self.assertEqual(entry['claims'], ['c-6f7g8h9jka'])
        self.assertIn({'from': f'documents/letter/aunt-letter-copy-page-2_{RETIRE}.jpg',
                       'to': f'documents/letter/{page.name}'}, entry['files'])

        body = keeper.read_text(encoding='utf-8')
        self.assertIn('Kept in the blue box.', body)
        self.assertIn('Second scan, better light.', body)
        self.assertIn('Letter from Aunt Margaret, scanned again 2026.', body)
        self.assertIn('## Stories\nThe letter mentions the move west.', body)
        self.assertIn('fha index', out)

    def test_merged_from_is_in_the_spec_key_order(self) -> None:
        keeper, _retiree = self._letters()
        rc, out, err = self._run(KEEP, RETIRE)
        self.assertEqual(rc, EXIT_CLEAN, out + err)
        keys = list(read_record(keeper)['meta'])
        self.assertLess(keys.index('ai_passes'), keys.index('merged_from'))
        self.assertLess(keys.index('merged_from'), keys.index('created'))

    def test_crlf_records_stay_crlf(self) -> None:
        keeper, retiree = self._letters()
        for path in (keeper, retiree):
            text = path.read_text(encoding='utf-8')
            with open(path, 'w', encoding='utf-8', newline='') as fh:
                fh.write(text.replace('\n', '\r\n'))

        rc, out, err = self._run(KEEP, RETIRE)

        self.assertEqual(rc, EXIT_CLEAN, out + err)
        raw = keeper.read_bytes()
        self.assertEqual(raw.count(b'\n'), raw.count(b'\r\n'), 'a bare LF crept in')
        rec = read_record(keeper)
        self.assertFalse(rec.get('parse_errors'))
        self.assertEqual([c['id'] for c in rec['claims']], ['c-1a2b3c4d5e', 'c-6f7g8h9jka'])

    def test_dry_run_writes_nothing_and_previews_the_same_plan(self) -> None:
        self._letters()
        before = self._snapshot()

        rc, out, err = self._run(KEEP, RETIRE, '--dry-run')

        self.assertEqual(rc, EXIT_CLEAN, out + err)
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self.kw.writes, [])
        self.assertIn('[dry-run] Would merge', out)
        self.assertIn(f'letter-from-aunt-page-2_{KEEP}.jpg', out)
        self.assertIn('c-6f7g8h9jka', out)
        self.assertIn('Nothing was written', out)

    def test_documents_without_exiftool_are_renamed_with_a_warning(self) -> None:
        keeper, retiree = self._letters()
        self.kw.unavailable = True

        rc, out, err = self._run(KEEP, RETIRE)

        self.assertEqual(rc, EXIT_CLEAN, out + err)
        self.assertIn('could not be read', out + err)
        self.assertTrue((self.docs / 'letter' / f'letter-from-aunt-page-2_{KEEP}.jpg').is_file())
        self.assertFalse(retiree.exists())


class PhotosMergeTests(MergeTestCase):

    def _photos(self) -> tuple[Path, Path, Path]:
        keeper_front = self._put(self.photos / '1880' / 'portrait.jpg', b'\xff\xd8 keeper',
                                 [f'SOURCE: {KEEP}'])
        retiree_front = self._put(self.photos / '1880' / 'portrait-larger.jpg',
                                  b'\xff\xd8 retiree', [f'SOURCE: {RETIRE}', THOMAS])
        keeper = self._write(f'sources/photos/studio-portrait_{KEEP}.md', _record(
            KEEP, title='Studio portrait', citation='Portrait, family album.',
            files=['  - file: photos/1880/portrait.jpg', '    role: front',
                   '    is_primary: true'], claims=[], people=[THOMAS]))
        retiree = self._write(f'sources/photos/larger-portrait_{RETIRE}.md', _record(
            RETIRE, title='Studio portrait (larger download)',
            citation='Portrait, family album.',
            files=['  - file: photos/1880/portrait-larger.jpg', '    role: front',
                   '    is_primary: true'], claims=[], people=[THOMAS]))
        return keeper, retiree, retiree_front

    def test_photo_keeps_its_name_and_only_its_keyword_changes(self) -> None:
        keeper, retiree, photo = self._photos()

        rc, out, err = self._run(KEEP, RETIRE)

        self.assertEqual(rc, EXIT_CLEAN, out + err)
        self.assertTrue(photo.is_file())
        self.assertEqual(sorted(p.name for p in (self.photos / '1880').iterdir()),
                         ['portrait-larger.jpg', 'portrait.jpg'])
        self.assertEqual(self.kw.of(photo), [THOMAS, f'SOURCE: {KEEP}'])
        self.assertFalse(retiree.exists())
        files = read_record(keeper)['meta']['files']
        self.assertEqual(files[1]['file'], 'photos/1880/portrait-larger.jpg')
        self.assertEqual(files[1]['role'], 'front')
        self.assertEqual(files[1]['copy'], 'b')
        self.assertNotIn('original_filename', files[1])
        self.assertIn('fha photoindex', out)

    def test_photo_whose_keyword_cannot_be_read_refuses(self) -> None:
        self._photos()
        self.kw.unavailable = True
        before = self._snapshot()

        rc, out, err = self._run(KEEP, RETIRE)

        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('exiftool', err)
        self.assertEqual(self._snapshot(), before)


class RefusalTests(MergeTestCase):
    """Every refusal comes before the first write, the same under --dry-run."""

    def _refused(self, *argv: str, code: int = EXIT_ERRORS) -> str:
        before = self._snapshot()
        rc, out, err = self._run(*argv)
        self.assertEqual(rc, code, out + err)
        self.assertEqual(self._snapshot(), before)
        rc2, out2, err2 = self._run(*argv, '--dry-run')
        self.assertEqual(rc2, code)
        self.assertEqual(err2, err)
        self.assertEqual(self._snapshot(), before)
        return out + err

    def test_invalid_id(self) -> None:
        self._letters()
        self.assertIn('not a valid id', self._refused(KEEP, 's-123'))

    def test_same_id_twice(self) -> None:
        self._letters()
        self.assertIn('both ids are', self._refused(KEEP, KEEP.upper()))

    def test_a_person_id_is_not_a_source(self) -> None:
        self._letters()
        self.assertIn('not a source id', self._refused(KEEP, THOMAS))

    def test_missing_record_is_not_found(self) -> None:
        self._letters()
        text = self._refused(KEEP, OTHER, code=EXIT_WARNINGS)
        self.assertIn(f'fha find {OTHER}', text)

    def test_accepted_claims_about_different_people(self) -> None:
        self._letters(keeper_people=(THOMAS,), retiree_people=(ETHEL,))
        text = self._refused(KEEP, RETIRE)
        self.assertIn('two different records', text)
        self.assertIn(THOMAS, text)
        self.assertIn(ETHEL, text)

    def test_suggested_claims_do_not_count_as_different_people(self) -> None:
        keeper, retiree = self._letters(keeper_people=(THOMAS,), retiree_people=(ETHEL,))
        retiree.write_text(retiree.read_text(encoding='utf-8')
                           .replace('status: accepted', 'status: suggested'), encoding='utf-8')
        rc, out, err = self._run(KEEP, RETIRE)
        self.assertEqual(rc, EXIT_CLEAN, out + err)

    def test_restricted_retiree_into_open_keeper(self) -> None:
        _keeper, retiree = self._letters()
        retiree.write_text(retiree.read_text(encoding='utf-8')
                           .replace('created: 2026-06-10', 'restricted: true\ncreated: 2026-06-10'),
                           encoding='utf-8')
        self.assertIn('restricted', self._refused(KEEP, RETIRE))

    def test_file_carrying_a_third_sources_keyword(self) -> None:
        self._letters()
        page = self.docs / 'letter' / f'aunt-letter-copy-page-2_{RETIRE}.jpg'
        self.kw.by_bytes[page.read_bytes()] = [f'SOURCE: {OTHER}']
        self.assertIn(f'fha find {OTHER}', self._refused(KEEP, RETIRE))

    def test_listed_file_missing_from_disk(self) -> None:
        self._letters()
        (self.docs / 'letter' / f'aunt-letter-copy-page-2_{RETIRE}.jpg').unlink()
        self.assertIn('fha reconcile', self._refused(KEEP, RETIRE))

    def test_claim_on_both_records(self) -> None:
        _keeper, retiree = self._letters()
        retiree.write_text(retiree.read_text(encoding='utf-8')
                           .replace('c-6f7g8h9jka', 'c-1a2b3c4d5e'), encoding='utf-8')
        self.assertIn('on both records', self._refused(KEEP, RETIRE))


class RollbackTests(MergeTestCase):
    """A failure part-way puts everything back."""

    def test_keyword_write_failure_on_a_later_file_rolls_back(self) -> None:
        self._letters()
        before = self._snapshot()
        # The page's keyword write fails after the first file was already
        # renamed and keyworded.
        self.kw.fail_on.add(f'letter-from-aunt-page-2_{KEEP}.jpg')

        rc, out, err = self._run(KEEP, RETIRE)

        self.assertEqual(rc, EXIT_FAILURE, out + err)
        self.assertIn('everything was put back', err)
        # The first file's keyword write was undone byte for byte, so its
        # old keywords are back with its old name.
        self.assertEqual(self._snapshot()['files'], before['files'])
        self.assertEqual(self.kw.of(self.docs / 'letter' / f'aunt-letter-copy_{RETIRE}.pdf'),
                         [f'SOURCE: {RETIRE}', 'Margaret Hartley'])
        self.assertEqual(self.kw.writes, [f'letter-from-aunt-b_{KEEP}.pdf'])

    def test_record_write_failure_rolls_back_the_renames(self) -> None:
        self._letters()
        before = self._snapshot()
        real = source.write_text_exact_atomic

        def failing(path, text):
            if Path(path).name.startswith('letter-from-aunt_'):
                raise OSError('simulated disk full')
            return real(path, text)
        source.write_text_exact_atomic = failing

        rc, out, err = self._run(KEEP, RETIRE)

        self.assertEqual(rc, EXIT_FAILURE, out + err)
        self.assertIn('simulated disk full', err)
        self.assertEqual(self._snapshot()['files'], before['files'])

    def test_retiree_removal_failure_restores_the_keeper(self) -> None:
        _keeper, retiree = self._letters()
        before = self._snapshot()
        real_unlink = Path.unlink

        def failing_unlink(self_path, *a, **k):
            if self_path == retiree:
                raise PermissionError('simulated: file is open in another program')
            return real_unlink(self_path, *a, **k)
        Path.unlink = failing_unlink
        try:
            rc, out, err = self._run(KEEP, RETIRE)
        finally:
            Path.unlink = real_unlink

        self.assertEqual(rc, EXIT_FAILURE, out + err)
        self.assertEqual(self._snapshot()['files'], before['files'])

    def test_ctrl_c_part_way_puts_everything_back(self) -> None:
        # Stopped between two files, a merge would leave some files named for
        # one source and some for the other; it is undone, then Ctrl+C goes on.
        _keeper, retiree = self._letters()
        before = self._snapshot()
        real = self.kw.write
        calls = {'n': 0}

        def write(path, **kw):
            calls['n'] += 1
            if calls['n'] == 2:
                raise KeyboardInterrupt
            return real(path, **kw)
        source._merge_write_keywords = write

        with self.assertRaises(KeyboardInterrupt):
            self._run(KEEP, RETIRE)

        self.assertEqual(self._snapshot()['files'], before['files'])
        self.assertTrue(retiree.exists())

    def test_ctrl_c_with_an_undo_that_fails_says_what_was_left(self) -> None:
        self._letters()
        real = self.kw.write
        calls = {'n': 0}

        def write(path, **kw):
            calls['n'] += 1
            if calls['n'] == 2:
                raise KeyboardInterrupt
            return real(path, **kw)

        def locked(_copy, _path):
            raise PermissionError(13, 'the file is open in another program')
        source._merge_write_keywords = write
        saved = source.restore_undo_copy
        source.restore_undo_copy = locked
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(KeyboardInterrupt):
                    source._standalone_main(['merge', KEEP, RETIRE, '--root', str(self.archive)])
        finally:
            source.restore_undo_copy = saved
        text = err.getvalue()
        self.assertIn('could not finish', text)
        self.assertIn('original bytes are kept at', text)
        self.assertIn('fha doctor', text)

    def test_an_undo_that_fails_names_the_file_as_it_is_now_called(self) -> None:
        # The bytes could not be put back, but the rename back did run: the
        # owner must be sent to the name the file has now, not the merge name.
        self._letters()
        self.kw.fail_on.add(f'letter-from-aunt-page-2_{KEEP}.jpg')

        def locked(_copy, _path):
            raise PermissionError(13, 'the file is open in another program')
        saved = source.restore_undo_copy
        source.restore_undo_copy = locked
        try:
            rc, out, err = self._run(KEEP, RETIRE)
        finally:
            source.restore_undo_copy = saved

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertTrue((self.docs / 'letter' / f'aunt-letter-copy_{RETIRE}.pdf').is_file())
        self.assertIn(f'original bytes of aunt-letter-copy_{RETIRE}.pdf', err)
        self.assertNotIn(f'letter-from-aunt-b_{KEEP}.pdf', err)
        self.assertIn('original bytes are kept at', err)

    def test_a_failed_run_never_says_it_merged(self) -> None:
        self._letters()
        self.kw.fail_on.add(f'letter-from-aunt-page-2_{KEEP}.jpg')

        rc, out, err = self._run(KEEP, RETIRE)

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertNotIn('Merged ', out + err)
        self.assertNotIn('renamed', out)
        self.assertIn('will', out)

    def test_a_good_run_says_it_merged_after_the_plan(self) -> None:
        self._letters()
        rc, out, err = self._run(KEEP, RETIRE)
        self.assertEqual(rc, EXIT_CLEAN, out + err)
        self.assertLess(out.index('The merge will:'), out.index('Merged '))


class PrivacyMarkerTests(MergeTestCase):
    """The keeper must keep the retiree's files at least as private as it did."""

    def _set(self, path: Path, old: str, new: str) -> None:
        path.write_text(path.read_text(encoding='utf-8').replace(old, new, 1),
                        encoding='utf-8')

    def _refused(self) -> str:
        before = self._snapshot()
        rc, out, err = self._run(KEEP, RETIRE)
        self.assertEqual(rc, EXIT_ERRORS, out + err)
        rc2, _out2, err2 = self._run(KEEP, RETIRE, '--dry-run')
        self.assertEqual((rc2, err2), (rc, err))
        self.assertEqual(self._snapshot(), before)
        return err

    def test_dna_retiree_into_plain_restricted_keeper(self) -> None:
        keeper, retiree = self._letters()
        self._set(keeper, 'created:', 'restricted: true\ncreated:')
        self._set(retiree, 'created:', 'restricted: dna\ncreated:')
        self.assertIn('restricted: dna', self._refused())

    def test_by_request_retiree_into_plain_restricted_keeper(self) -> None:
        keeper, retiree = self._letters()
        self._set(keeper, 'created:', 'restricted: true\ncreated:')
        self._set(retiree, 'created:', 'restricted: by-request\ncreated:')
        self.assertIn('restricted: by-request', self._refused())

    def test_dna_source_type_without_a_restricted_line(self) -> None:
        _keeper, retiree = self._letters()
        self._set(retiree, 'source_type: letter', 'source_type: dna')
        self.assertIn('source_type: dna', self._refused())

    def test_not_for_publication_retiree(self) -> None:
        _keeper, retiree = self._letters()
        self._set(retiree, 'created:', 'rights:\n  publication_ok: false\ncreated:')
        self.assertIn('publication_ok', self._refused())

    def test_a_stricter_keeper_is_refused_without_advice_to_weaken_it(self) -> None:
        # Keeper `dna`, retiree `true`: the advice must not be "add
        # `restricted: true` to the keeper", which would replace `dna`.
        keeper, retiree = self._letters()
        self._set(keeper, 'created:', 'restricted: dna\ncreated:')
        self._set(retiree, 'created:', 'restricted: true\ncreated:')
        err = self._refused()
        self.assertIn('protected differently', err)
        self.assertIn('`restricted: dna`', err)
        self.assertIn('`restricted: true`', err)
        self.assertNotIn('Add `', err)

    def test_an_unmarked_retiree_takes_the_keepers_protection(self) -> None:
        keeper, _retiree = self._letters()
        self._set(keeper, 'created:', 'restricted: by-request\ncreated:')
        rc, out, err = self._run(KEEP, RETIRE)
        self.assertEqual(rc, EXIT_CLEAN, out + err)
        self.assertEqual(read_record(keeper)['meta']['restricted'], 'by-request')

    def test_same_markers_on_both_merge(self) -> None:
        keeper, retiree = self._letters()
        self._set(keeper, 'created:', 'restricted: dna\ncreated:')
        self._set(retiree, 'created:', 'restricted: dna\ncreated:')
        rc, out, err = self._run(KEEP, RETIRE)
        self.assertEqual(rc, EXIT_CLEAN, out + err)


class FileIdentityTests(MergeTestCase):
    """One file spelled two ways is still one file."""

    def test_keepers_file_listed_on_the_retiree_in_other_capitals(self) -> None:
        if os.path.normcase('A') == os.path.normcase('a'):
            spelled = f'documents/letter/Letter-from-aunt_{KEEP}.pdf'
        else:
            spelled = f'documents/letter/./letter-from-aunt_{KEEP}.pdf'
        _keeper, retiree = self._letters()
        retiree.write_text(retiree.read_text(encoding='utf-8').replace(
            'files:\n', f'files:\n  - file: {spelled}\n    role: copy\n', 1), encoding='utf-8')
        before = self._snapshot()
        rc, out, err = self._run(KEEP, RETIRE)
        self.assertEqual(rc, EXIT_ERRORS, out + err)
        self.assertIn('listed on both records', err)
        self.assertEqual(self._snapshot(), before)

    def test_one_file_listed_twice_on_the_retiree_refuses_in_the_dry_run_too(self) -> None:
        if os.path.normcase('A') == os.path.normcase('a'):
            spelled = f'documents/letter/Aunt-letter-copy-page-2_{RETIRE}.jpg'
        else:
            spelled = f'documents/letter/./aunt-letter-copy-page-2_{RETIRE}.jpg'
        _keeper, retiree = self._letters()
        retiree.write_text(retiree.read_text(encoding='utf-8').replace(
            'files:\n', f'files:\n  - file: {spelled}\n    role: page-2\n', 1), encoding='utf-8')
        before = self._snapshot()
        rc, _out, err = self._run(KEEP, RETIRE, '--dry-run')
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('same file twice', err)
        rc, _out, err = self._run(KEEP, RETIRE)
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertEqual(self._snapshot(), before)

    def test_a_retiree_file_already_named_for_the_keeper_gets_the_id_once(self) -> None:
        _keeper, retiree = self._letters()
        odd = self._put(self.docs / 'letter' / f'stray-scan_{KEEP}.tif', b'II* stray')
        retiree.write_text(retiree.read_text(encoding='utf-8').replace(
            'files:\n', f'files:\n  - file: documents/letter/{odd.name}\n    role: page-3\n', 1),
            encoding='utf-8')
        rc, out, err = self._run(KEEP, RETIRE)
        self.assertEqual(rc, EXIT_CLEAN, out + err)
        names = [p.name for p in (self.docs / 'letter').iterdir()]
        self.assertFalse([n for n in names if n.lower().count(KEEP) > 1], names)


class HandWrittenTextTests(MergeTestCase):
    """Nothing a person wrote on the retiree is lost or let out."""

    def test_frontmatter_comments_and_own_blockquote_are_kept(self) -> None:
        keeper, retiree = self._letters()
        text = retiree.read_text(encoding='utf-8')
        text = text.replace('repository: family collection',
                            'repository: family collection   # original in the cedar chest', 1)
        text = text.replace('created:', '# Cousin Ruth has the second page\ncreated:', 1)
        text = text.replace('## Claims', '> Written the week after the funeral.\n\n## Claims', 1)
        retiree.write_text(text, encoding='utf-8')

        rc, out, err = self._run(KEEP, RETIRE)

        self.assertEqual(rc, EXIT_CLEAN, out + err)
        body = keeper.read_text(encoding='utf-8')
        for kept in ('original in the cedar chest', 'Cousin Ruth has the second page',
                     'Written the week after the funeral.'):
            self.assertIn(kept, body)
        # The scaffold's purpose block is not copied a second time.
        self.assertEqual(body.count("This source's record"), 1)

    def test_a_private_fence_across_a_heading_stays_whole(self) -> None:
        secret = 'Uncle Fred was not her father; the family never spoke of it.'
        keeper, retiree = self._letters()
        keeper.write_text(keeper.read_text(encoding='utf-8').replace(
            '## Notes', '## Stories\nThe move west in 1871.\n\n## Notes', 1), encoding='utf-8')
        text = retiree.read_text(encoding='utf-8')
        text = text.replace('\n## Stories\nThe letter mentions the move west.\n', '')
        text = text.replace('## Notes', f'<!-- private -->\n## Stories\n{secret}\n'
                                        '<!-- /private -->\n\n## Notes', 1)
        retiree.write_text(text, encoding='utf-8')
        self.assertNotIn(secret, apply_private_fence(text, drop=True))

        rc, out, err = self._run(KEEP, RETIRE)

        self.assertEqual(rc, EXIT_CLEAN, out + err)
        after = keeper.read_text(encoding='utf-8')
        self.assertIn(secret, after)
        shared = apply_private_fence(after, drop=True)
        self.assertNotIn(secret, shared)
        self.assertIn('Kept in the blue box.', shared)
        self.assertIn('The move west in 1871.', shared)

    def test_notes_after_a_lifted_fence_stay_visible(self) -> None:
        # The lifted fence must not start a section of its own: the retiree's
        # ordinary notes and a later `fha source note` land after its closer.
        keeper, retiree = self._letters()
        keeper.write_text(keeper.read_text(encoding='utf-8').replace(
            '## Notes', '## Stories\nThe move west in 1871.\n\n## Notes', 1), encoding='utf-8')
        text = retiree.read_text(encoding='utf-8')
        text = text.replace('\n## Stories\nThe letter mentions the move west.\n', '')
        text = text.replace('## Notes', '<!-- private -->\n## Stories\nA family secret.\n'
                                        '<!-- /private -->\n\n## Notes', 1)
        retiree.write_text(text, encoding='utf-8')

        rc, out, err = self._run(KEEP, RETIRE)
        self.assertEqual(rc, EXIT_CLEAN, out + err)
        out2, err2 = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out2), contextlib.redirect_stderr(err2):
            rc2 = source._standalone_main(['note', KEEP, '--text', 'A later note.',
                                           '--root', str(self.archive)])
        self.assertEqual(rc2, EXIT_CLEAN, out2.getvalue() + err2.getvalue())

        after = keeper.read_text(encoding='utf-8')
        shared = apply_private_fence(after, drop=True)
        self.assertNotIn('A family secret.', shared)
        self.assertIn('Second scan, better light.', shared)
        self.assertIn('A later note.', shared)
        self.assertEqual(after.count('\n## Stories'), 1)

    def _nested_fence_case(self, block: str) -> None:
        """Merge a retiree whose body ends in `block`, add a note afterwards,
        and check the note shows and every fence in the keeper is closed."""
        keeper, retiree = self._letters()
        text = retiree.read_text(encoding='utf-8')
        text = text.replace('\n## Stories\nThe letter mentions the move west.\n', '')
        retiree.write_text(text[:text.index('## Notes')] + block, encoding='utf-8')

        rc, out, err = self._run(KEEP, RETIRE)
        self.assertEqual(rc, EXIT_CLEAN, out + err)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            source._standalone_main(['note', KEEP, '--text', 'A later note.',
                                     '--root', str(self.archive)])

        after = keeper.read_text(encoding='utf-8')
        shared = apply_private_fence(after, drop=True)
        self.assertNotIn('Secret', shared)
        self.assertIn('A later note.', shared)
        self.assertIn('Kept in the blue box.', shared)
        self.assertEqual(source._open_fence_depth(after), 0, 'a fence is left open')

    def test_an_unclosed_outer_fence_holding_a_closed_one(self) -> None:
        self._nested_fence_case(
            '<!-- private -->\nSecret A.\n## Stories\nSecret B.\n\n## Notes\nOld note.\n'
            '<!-- private -->example<!-- /private -->\n')

    def test_two_openers_never_closed(self) -> None:
        self._nested_fence_case(
            '<!-- private -->\nSecret A.\n## Stories\n<!-- private -->\nSecret B.\n\n'
            '## Notes\nOld note.\n')

    def test_an_unclosed_fence_does_not_hide_the_keepers_later_text(self) -> None:
        keeper, retiree = self._letters()
        keeper.write_text(keeper.read_text(encoding='utf-8') + '\n## Sources seen\nThe county index.\n',
                          encoding='utf-8')
        retiree.write_text(retiree.read_text(encoding='utf-8').replace(
            'Second scan, better light.', 'Second scan.\n<!-- private -->\nA hunch.'), encoding='utf-8')

        rc, out, err = self._run(KEEP, RETIRE)

        self.assertEqual(rc, EXIT_CLEAN, out + err)
        shared = apply_private_fence(keeper.read_text(encoding='utf-8'), drop=True)
        self.assertNotIn('A hunch.', shared)
        self.assertIn('The county index.', shared)


class MergedPersonTests(MergeTestCase):
    """A claim naming a merged person counts for the survivor."""

    def test_a_merged_person_id_is_the_same_person_as_its_survivor(self) -> None:
        old = 'p-bbbbbbbbbb'
        (self.archive / 'people').mkdir(exist_ok=True)
        (self.archive / 'people' / f'hartley__thomas_{THOMAS}.md').write_text(
            f'---\nid: {THOMAS}\nname: Thomas Hartley\n---\n', encoding='utf-8')
        (self.archive / 'people' / f'MERGED-INTO-{THOMAS}__hartley__thos_{old}.md').write_text(
            f'---\nid: {old}\nname: Thos. Hartley\nstatus: merged\nmerged_into: {THOMAS}\n---\n',
            encoding='utf-8')
        self._letters(keeper_people=(THOMAS,), retiree_people=(old,))

        rc, out, err = self._run(KEEP, RETIRE)

        self.assertEqual(rc, EXIT_CLEAN, out + err)


if __name__ == '__main__':
    unittest.main()
