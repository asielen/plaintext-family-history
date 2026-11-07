"""
test_source_set_primary.py - fha source set-primary (#200, TOOLING §3d).

While a filename bug was live, `fha process` wrote source records for a
front/back pair that named the BACK as the main image (`is_primary: true`,
`role: primary`) and gave the front a made-up role (`farm-01400`). The fix is
this verb: it moves the main-image mark to the named photo, gives the old
main image the role its own filename spells, and changes nothing else in the
record - not a comment, not a line ending, not a claim.

Expected texts here are written out from the TOOLING §6/§9 suffix grammar
(`-back` is role back, a trailing copy letter is copy: b, a plain name has no
role), never captured from the implementation's own output.

Fixtures only (AGENTS_TOOLING §5): temp trees; the real archive is never
touched. Run from tests/: py -3.14 -m unittest test_source_set_primary
"""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import source
from _lib import EXIT_CLEAN, EXIT_FAILURE, EXIT_WARNINGS

SID = 'S-2b3c4d5e6f'

# What `fha process` wrote for `Smith-Farm-01400.tif` + its back while #200
# was live: the back is the main image, the front carries a bogus role. The
# print-B back beside them was always parsed right (role back, copy b).
FRONTMATTER_HEAD = (
    '---\n'
    f'id: {SID}\n'
    f'aliases: [{SID}]\n'
    'title: smith farm 01400 back   # the title the old run picked\n'
    'source_type: photo\n'
)
BUG_ERA_FILES = (
    'files:\n'
    '  - file: photos/1950/smith-farm-01400-back.tif\n'
    '    role: primary\n'
    '    is_primary: true\n'
    '  - file: photos/1950/smith-farm-01400.tif\n'
    '    role: farm-01400\n'
    '  - file: photos/1950/smith-farm-01400b-back.tif\n'
    '    role: back\n'
    '    copy: b\n'
)
FIXED_FILES = (
    'files:\n'
    '  - file: photos/1950/smith-farm-01400-back.tif\n'
    '    role: back\n'
    '    is_primary: false\n'
    '  - file: photos/1950/smith-farm-01400.tif\n'
    '    role: primary\n'
    '    is_primary: true\n'
    '  - file: photos/1950/smith-farm-01400b-back.tif\n'
    '    role: back\n'
    '    copy: b\n'
)
FRONTMATTER_TAIL = (
    'people: [P-aaaaaaaaaa]\n'
    'created: 2026-01-01\n'
    '---\n'
)
BODY = (
    '\n'
    '## Claims\n'
    '```yaml\n'
    '- id: C-aaaaaaaaaa\n'
    '  type: residence\n'
    '  value: "the Smith farm"\n'
    '  persons: [P-aaaaaaaaaa]\n'
    '  status: accepted\n'
    '  reviewed: 2026-02-01\n'
    '```\n'
    '\n'
    '## Notes\n'
    'Written on the back: "the farm, 1950".\n'
)

BUG_ERA = FRONTMATTER_HEAD + BUG_ERA_FILES + FRONTMATTER_TAIL + BODY
FIXED = FRONTMATTER_HEAD + FIXED_FILES + FRONTMATTER_TAIL + BODY


def _mk_archive(tmp: Path, text: str) -> Path:
    """fha.yaml + one source record, written byte for byte (no newline
    translation, so an LF fixture stays LF on Windows and a CRLF one CRLF)."""
    root = tmp / 'arc'
    (root / 'sources' / 'photos').mkdir(parents=True)
    (root / 'fha.yaml').write_text('roots:\n  photos: photos\n', encoding='utf-8')
    record = root / 'sources' / 'photos' / f'smith-farm-01400-back_{SID}.md'
    record.write_bytes(text.encode('utf-8'))
    return root


class SetPrimarySwapTests(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _mk_archive(Path(self._tmp.name), BUG_ERA)
        self.record = self.root / 'sources' / 'photos' / f'smith-farm-01400-back_{SID}.md'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, **kw):
        kw.setdefault('file', 'smith-farm-01400.tif')
        return source.run_source_set_primary(self.root, SID, **kw)

    def test_bug_era_record_swaps_front_and_back(self) -> None:
        result = self._run()
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'ok')
        self.assertEqual(result.data['primary'], 'photos/1950/smith-farm-01400.tif')
        self.assertEqual(result.data['previous'], 'photos/1950/smith-farm-01400-back.tif')
        self.assertEqual(self.record.read_bytes().decode('utf-8'), FIXED)
        self.assertIn(str(self.record), result.changed)

    def test_a_role_only_main_image_is_stepped_down(self) -> None:
        # #210 review: a hand-made record marking its back only `role:
        # primary` (no is_primary) kept that role, so two entries said
        # primary and the source page showed the first - still the back.
        self.record.write_bytes(BUG_ERA.replace('    is_primary: true\n', '', 1).encode('utf-8'))
        result = self._run()
        self.assertEqual(result.exit_code, EXIT_CLEAN, result.messages)
        after = self.record.read_bytes().decode('utf-8')
        self.assertEqual(after.count('role: primary'), 1)
        self.assertIn('smith-farm-01400-back.tif\n    role: back\n', after)

    def test_conflicting_main_image_markers_are_refused(self) -> None:
        # One entry says is_primary, another only role: primary. Which one to
        # step down is a guess, so nothing is written.
        conflicting = BUG_ERA.replace(
            '    role: primary\n', '    role: back\n', 1
        ).replace('    role: back\n    copy: b\n', '    role: primary\n    copy: b\n', 1)
        self.record.write_bytes(conflicting.encode('utf-8'))
        before = self.record.read_bytes()
        result = self._run()
        self.assertNotEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(self.record.read_bytes(), before)

    def test_handwritten_comments_on_changed_lines_are_kept(self) -> None:
        # #210 review: a note on a role:/is_primary: line was silently
        # dropped, and the YAML referee cannot see comments.
        commented = BUG_ERA.replace(
            '    role: primary\n', '    role: primary   # caption transcribed by Aunt May\n', 1
        ).replace(
            '    is_primary: true\n', '    is_primary: true  # keep?\n', 1
        ).replace(
            '    role: farm-01400\n', '    role: farm-01400 # odd role from the old run\n', 1)
        self.record.write_bytes(commented.encode('utf-8'))
        result = self._run()
        self.assertEqual(result.exit_code, EXIT_CLEAN, result.messages)
        after = self.record.read_bytes().decode('utf-8')
        self.assertIn('    role: back   # caption transcribed by Aunt May\n', after)
        self.assertIn('    is_primary: false  # keep?\n', after)
        self.assertIn('    role: primary # odd role from the old run\n', after)

    def test_one_pasted_crlf_line_does_not_turn_the_frontmatter_crlf(self) -> None:
        # #210 audit: the line ending was chosen for the whole file.
        self.record.write_bytes((BUG_ERA + 'pasted line\r\nmore\n').encode('utf-8'))
        self.assertEqual(self._run().exit_code, EXIT_CLEAN)
        head = self.record.read_bytes().decode('utf-8').split('## Claims')[0]
        self.assertNotIn('\r', head)

    def test_claims_and_every_other_field_are_byte_identical(self) -> None:
        self._run()
        after = self.record.read_bytes().decode('utf-8')
        # Everything outside the files: block - the hand comment on title:,
        # people:, the whole ## Claims block and ## Notes - is untouched.
        self.assertTrue(after.startswith(FRONTMATTER_HEAD))
        self.assertTrue(after.endswith(FRONTMATTER_TAIL + BODY))

    def test_crlf_record_stays_crlf_throughout(self) -> None:
        self.record.write_bytes(BUG_ERA.replace('\n', '\r\n').encode('utf-8'))
        result = self._run()
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        after = self.record.read_bytes()
        self.assertEqual(after, FIXED.replace('\n', '\r\n').encode('utf-8'))
        self.assertNotIn(b'\n', after.replace(b'\r\n', b''))   # no bare-LF island

    def test_dry_run_previews_and_writes_nothing(self) -> None:
        before = self.record.read_bytes()
        mtime = self.record.stat().st_mtime_ns
        result = self._run(dry_run=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'dry-run')
        self.assertEqual(self.record.read_bytes(), before)
        self.assertEqual(self.record.stat().st_mtime_ns, mtime)
        self.assertEqual(result.changed, [])
        text = '\n'.join(m.text for m in result.messages)
        self.assertIn('Would make smith-farm-01400.tif the main image', text)
        self.assertIn('the back of the photo (role: back)', text)

    def test_running_it_twice_is_already_done(self) -> None:
        self._run()
        after = self.record.read_bytes()
        again = self._run()
        self.assertEqual(again.exit_code, EXIT_CLEAN)
        self.assertEqual(again.data['status'], 'already')
        self.assertEqual(self.record.read_bytes(), after)

    def test_match_ignores_capitals_and_accepts_the_full_path(self) -> None:
        result = self._run(file='SMITH-FARM-01400.TIF', dry_run=True)
        self.assertEqual(result.data['status'], 'dry-run')
        result = self._run(file='photos/1950/smith-farm-01400.tif')
        self.assertEqual(result.data['status'], 'ok')
        self.assertEqual(self.record.read_bytes().decode('utf-8'), FIXED)

    def test_old_main_image_with_a_copy_letter_gains_copy_line(self) -> None:
        # The old main image is print B; its name says so, and the entry had
        # no copy: line, so it gains one. The plain scan had no is_primary
        # line at all (process omits it on a non-primary entry), so one is
        # added after its role.
        self.record.write_bytes((
            FRONTMATTER_HEAD
            + 'files:\n'
              '  - file: photos/x-00100b.jpg\n'
              '    role: primary\n'
              '    is_primary: true\n'
              '  - file: photos/x-00100.jpg\n'
              '    role: variant\n'
            + FRONTMATTER_TAIL + BODY).encode('utf-8'))
        result = self._run(file='x-00100.jpg')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(self.record.read_bytes().decode('utf-8'), (
            FRONTMATTER_HEAD
            + 'files:\n'
              '  - file: photos/x-00100b.jpg\n'
              '    role: variant\n'
              '    copy: b\n'
              '    is_primary: false\n'
              '  - file: photos/x-00100.jpg\n'
              '    role: primary\n'
              '    is_primary: true\n'
            + FRONTMATTER_TAIL + BODY))

    def test_a_record_that_does_not_read_back_is_restored(self) -> None:
        # The post-write re-read is the last guard: if the record does not
        # read back as planned, it is put back exactly as it was.
        before = self.record.read_bytes()
        real = source.read_record
        source.read_record = lambda p, *a, **k: {'meta': {'files': []}}
        try:
            result = self._run()
        finally:
            source.read_record = real
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(self.record.read_bytes(), before)
        self.assertIn('put back exactly as it was', result.messages[-1].text)


class SetPrimaryRefusalTests(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _mk_archive(Path(self._tmp.name), BUG_ERA)
        self.record = self.root / 'sources' / 'photos' / f'smith-farm-01400-back_{SID}.md'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _files(self, block: str) -> None:
        self.record.write_bytes((FRONTMATTER_HEAD + block + FRONTMATTER_TAIL + BODY).encode('utf-8'))

    def _refused(self, file: str, sid: str = SID) -> str:
        before = self.record.read_bytes()
        result = source.run_source_set_primary(self.root, sid, file=file)
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'refused')
        self.assertFalse(result.ok)
        self.assertEqual(self.record.read_bytes(), before, 'a refusal must write nothing')
        return result.messages[-1].text

    def test_invalid_id_refused(self) -> None:
        self.assertIn('not a valid source ID', self._refused('x.tif', sid='S-nope'))

    def test_no_file_named_refused(self) -> None:
        self.assertIn('No file was named', self._refused('   '))

    def test_unknown_file_refused_listing_the_photos(self) -> None:
        msg = self._refused('nothing-like-it.tif')
        self.assertIn('does not match any file', msg)
        self.assertIn('smith-farm-01400.tif', msg)

    def test_ambiguous_bare_name_refused(self) -> None:
        self._files(
            'files:\n'
            '  - file: photos/1950/x-00100-back.jpg\n'
            '    role: primary\n'
            '    is_primary: true\n'
            '  - file: photos/1950/x-00100.jpg\n'
            '    role: variant\n'
            '  - file: photos/album2/x-00100.jpg\n'
            '    role: variant\n')
        msg = self._refused('x-00100.jpg')
        self.assertIn('matches more than one file', msg)
        self.assertIn('photos/album2/x-00100.jpg', msg)

    def test_documents_root_file_refused(self) -> None:
        self._files(
            'files:\n'
            '  - file: photos/1950/x-00100-back.jpg\n'
            '    role: primary\n'
            '    is_primary: true\n'
            f'  - file: documents/letters/letter_{SID}.pdf\n'
            '    role: attachment\n')
        self.assertIn('not in the photos folder', self._refused(f'letter_{SID}.pdf'))

    def test_two_existing_main_images_refused(self) -> None:
        self._files(
            'files:\n'
            '  - file: photos/x-00100-back.jpg\n'
            '    role: primary\n'
            '    is_primary: true\n'
            '  - file: photos/x-00100-neg.jpg\n'
            '    role: primary\n'
            '    is_primary: true\n'
            '  - file: photos/x-00100.jpg\n'
            '    role: variant\n')
        self.assertIn('more than one file as its main image', self._refused('x-00100.jpg'))

    def test_one_line_files_list_refused(self) -> None:
        self._files(
            'files: [{file: photos/x-00100-back.jpg, role: primary, is_primary: true}, '
            '{file: photos/x-00100.jpg, role: variant}]\n')
        self.assertIn('written on one line', self._refused('x-00100.jpg'))

    def test_missing_record_is_not_found_with_find_next_step(self) -> None:
        result = source.run_source_set_primary(self.root, 'S-zzzzzzzzzz', file='x.tif')
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result.data['status'], 'not-found')
        self.assertEqual(result.messages[-1].next_step, 'fha find S-zzzzzzzzzz')


class SetPrimaryCliTests(unittest.TestCase):
    """The verb is reachable through the main dispatcher, --file is required,
    and --dry-run reaches the engine."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _mk_archive(Path(self._tmp.name), BUG_ERA)
        self.record = self.root / 'sources' / 'photos' / f'smith-farm-01400-back_{SID}.md'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        import fha
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fha.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_file_is_required(self) -> None:
        import fha
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                fha.main(['source', 'set-primary', SID, '--root', str(self.root)])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn('--file', err.getvalue())

    def test_cli_dry_run_then_write(self) -> None:
        before = self.record.read_bytes()
        rc, out, _ = self._run(['source', 'set-primary', SID, '--file', 'smith-farm-01400.tif',
                                '--dry-run', '--root', str(self.root)])
        self.assertEqual(rc, 0)
        self.assertIn('dry-run', out)
        self.assertEqual(self.record.read_bytes(), before)
        rc, out, _ = self._run(['source', 'set-primary', SID, '--file', 'smith-farm-01400.tif',
                                '--root', str(self.root)])
        self.assertEqual(rc, 0)
        self.assertIn('is now the main image', out)
        self.assertEqual(self.record.read_bytes().decode('utf-8'), FIXED)

    def test_standalone_main_knows_the_verb(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = source._standalone_main(['set-primary', SID, '--file', 'smith-farm-01400.tif',
                                          '--dry-run', '--root', str(self.root)])
        self.assertEqual(rc, 0)
        self.assertIn('Would make smith-farm-01400.tif the main image', out.getvalue())


if __name__ == '__main__':
    unittest.main()
