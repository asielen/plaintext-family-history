"""
test_source.py - fha source note: the surgical ## Notes append-only write-back.

Covers the run_source_note contract (a note appended as a new blank-line-
separated paragraph, a missing ## Notes heading created at end of file,
frontmatter/## Claims left byte-identical, CRLF byte-faithfulness, a
status: superseded source still accepting notes) and every refusal arm
(invalid id shape, blank --text, unknown S-id with the `fha find` next
step, a duplicate ## Notes heading). CLI-level checks ride fha.main: bare
`fha source` (help + exit 2), --text required at argparse, and a live
write through the dispatcher.

Fixtures only (AGENTS_TOOLING §5): everything runs against temp trees; the
real archive is never touched.

Run: python -m unittest tests.test_source -v   (from the repo root)
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
from _lib import EXIT_CLEAN, EXIT_FAILURE, EXIT_WARNINGS, read_record

SID = 'S-aaaaaaaaaa'

SOURCE_WITH_NOTES = (
    '---\n'
    f'id: {SID}\n'
    f'aliases: [{SID}]\n'
    'title: Hartley Family Bible\n'
    'source_type: other\n'
    'created: 2026-01-01\n'
    '---\n'
    '\n'
    '## Claims\n'
    '```yaml\n'
    '- id: C-aaaaaaaaaa\n'
    '  type: note\n'
    '  value: "family bible, inherited"\n'
    '  persons: [P-aaaaaaaaaa]\n'
    '  status: accepted\n'
    '```\n'
    '\n'
    '## Notes\n'
    'Found in the attic in 1998.\n'
)

SOURCE_NO_NOTES = (
    '---\n'
    f'id: {SID}\n'
    f'aliases: [{SID}]\n'
    'title: Hartley Family Bible\n'
    'source_type: other\n'
    'created: 2026-01-01\n'
    '---\n'
    '\n'
    '## Claims\n'
    '```yaml\n'
    '```\n'
)

SOURCE_WITH_STORIES = (
    '---\n'
    f'id: {SID}\n'
    f'aliases: [{SID}]\n'
    'title: Hartley Family Bible\n'
    'source_type: other\n'
    'created: 2026-01-01\n'
    '---\n'
    '\n'
    '## Claims\n'
    '```yaml\n'
    '```\n'
    '\n'
    '## Notes\n'
    'Found in the attic in 1998.\n'
    '\n'
    '## Stories\n'
    'Grandma kept it on the mantel.\n'
)

SOURCE_SUPERSEDED = (
    '---\n'
    f'id: {SID}\n'
    f'aliases: [{SID}]\n'
    'title: Hartley Family Bible\n'
    'source_type: other\n'
    'status: superseded\n'
    'created: 2026-01-01\n'
    '---\n'
    '\n'
    '## Claims\n'
    '```yaml\n'
    '```\n'
    '\n'
    '## Notes\n'
    'Superseded by a clearer scan.\n'
)


def _mk_archive(tmp: Path, source_text: str = SOURCE_WITH_NOTES) -> Path:
    """A minimal spec-shaped archive: fha.yaml + sources/other/one record.

    Written with `newline=''` (no translation) so the fixture is genuinely
    LF on every platform - plain `Path.write_text()` would otherwise let
    Windows CRLF-ify these "LF" fixtures on write, silently testing the CRLF
    path under an LF-named test.
    """
    root = tmp / 'arc'
    (root / 'sources' / 'other').mkdir(parents=True)
    (root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
    (root / 'sources' / 'other' / f'hartley-bible_{SID}.md').write_text(
        source_text, encoding='utf-8', newline='')
    return root


class SourceNoteEditTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _mk_archive(Path(self._tmp.name))
        self.record = self.root / 'sources' / 'other' / f'hartley-bible_{SID}.md'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_note_appended_as_new_paragraph(self) -> None:
        result = source.run_source_note(self.root, SID, text='New note text.')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'ok')
        self.assertEqual(result.changed, [str(self.record)])
        after = self.record.read_text(encoding='utf-8')
        self.assertTrue(after.endswith(
            'Found in the attic in 1998.\n\nNew note text.\n'))
        # Frontmatter and ## Claims are byte-identical up to the heading.
        before_head = SOURCE_WITH_NOTES.split('## Notes')[0]
        after_head = after.split('## Notes')[0]
        self.assertEqual(before_head, after_head)

    def test_creates_heading_when_missing(self) -> None:
        root = _mk_archive(Path(tempfile.mkdtemp()), SOURCE_NO_NOTES)
        record = root / 'sources' / 'other' / f'hartley-bible_{SID}.md'
        result = source.run_source_note(root, SID, text='New note text.')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        after = record.read_text(encoding='utf-8')
        self.assertTrue(after.endswith('## Notes\nNew note text.\n'))
        # The existing ## Claims fence is untouched.
        self.assertIn('## Claims\n```yaml\n```\n', after)
        rec = read_record(record)
        self.assertEqual(rec['parse_errors'], [])

    def test_note_lands_before_a_following_stories_section(self) -> None:
        root = _mk_archive(Path(tempfile.mkdtemp()), SOURCE_WITH_STORIES)
        record = root / 'sources' / 'other' / f'hartley-bible_{SID}.md'
        result = source.run_source_note(root, SID, text='New note text.')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        after = record.read_text(encoding='utf-8')
        self.assertIn(
            'Found in the attic in 1998.\n\nNew note text.\n\n## Stories\n', after)
        self.assertIn('Grandma kept it on the mantel.\n', after)

    def test_superseded_source_still_accepts_notes(self) -> None:
        root = _mk_archive(Path(tempfile.mkdtemp()), SOURCE_SUPERSEDED)
        record = root / 'sources' / 'other' / f'hartley-bible_{SID}.md'
        result = source.run_source_note(root, SID, text='Still worth noting.')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'ok')
        after = record.read_text(encoding='utf-8')
        self.assertTrue(after.endswith(
            'Superseded by a clearer scan.\n\nStill worth noting.\n'))

    def test_crlf_record_stays_fully_crlf(self) -> None:
        crlf = SOURCE_WITH_NOTES.replace('\n', '\r\n')
        self.record.write_bytes(crlf.encode('utf-8'))
        result = source.run_source_note(self.root, SID, text='New note text.')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        after = self.record.read_bytes().decode('utf-8')
        self.assertIn('New note text.\r\n', after)
        # No stray bare-LF line crept into an otherwise CRLF file.
        self.assertNotIn('\n', after.replace('\r\n', ''))

    def test_dry_run_previews_and_writes_nothing(self) -> None:
        before = self.record.read_bytes()
        result = source.run_source_note(self.root, SID, text='New note text.', dry_run=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'dry-run')
        self.assertEqual(result.changed, [])
        text = '\n'.join(m.text for m in result.messages)
        self.assertIn('+New note text.', text)
        self.assertIn('No file written', text)
        self.assertEqual(self.record.read_bytes(), before)

    def test_success_names_the_index_nudge_as_advice(self) -> None:
        result = source.run_source_note(self.root, SID, text='New note text.')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        nudges = [m for m in result.messages if m.next_step == 'fha index']
        self.assertEqual(len(nudges), 1)
        self.assertEqual(nudges[0].level, 'info')


class SourceNoteRefusalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _mk_archive(Path(self._tmp.name))
        self.record = self.root / 'sources' / 'other' / f'hartley-bible_{SID}.md'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_invalid_id_shape_refused(self) -> None:
        result = source.run_source_note(self.root, 'grandmas-bible', text='x')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'refused')
        self.assertIn('S-2b3c4d5e6f', result.messages[0].text)   # the example id

    def test_wrong_id_type_refused(self) -> None:
        result = source.run_source_note(self.root, 'P-aaaaaaaaaa', text='x')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'refused')

    def test_blank_text_refused(self) -> None:
        for blank in ('', '   ', '\n\n'):
            result = source.run_source_note(self.root, SID, text=blank)
            self.assertEqual(result.exit_code, EXIT_FAILURE, repr(blank))
            self.assertEqual(result.data['status'], 'refused', repr(blank))
            self.assertIn('No note text', result.messages[0].text)

    def test_missing_source_id_warns_with_next_step(self) -> None:
        result = source.run_source_note(self.root, 'S-zzzzzzzzzz', text='x')
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result.data['status'], 'not-found')
        self.assertEqual(result.messages[0].next_step, 'fha find S-zzzzzzzzzz')
        self.assertIn('fha find S-zzzzzzzzzz', result.messages[0].text)

    def test_missing_sources_dir_is_also_not_found(self) -> None:
        empty_root = Path(tempfile.mkdtemp()) / 'arc'
        empty_root.mkdir(parents=True)
        (empty_root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        result = source.run_source_note(empty_root, SID, text='x')
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result.data['status'], 'not-found')

    def test_duplicate_notes_heading_refused_untouched(self) -> None:
        dup = SOURCE_WITH_NOTES + '\n## Notes\nA second heading.\n'
        self.record.write_text(dup, encoding='utf-8', newline='')
        before = self.record.read_bytes()
        result = source.run_source_note(self.root, SID, text='x')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'refused')
        self.assertEqual(self.record.read_bytes(), before)


class SourceNoteCliTests(unittest.TestCase):
    """The argparse boundary and dispatcher wiring ride fha.main."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _mk_archive(Path(self._tmp.name))
        self.record = self.root / 'sources' / 'other' / f'hartley-bible_{SID}.md'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        import fha
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fha.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_bare_source_prints_help_exit_2(self) -> None:
        rc, out, _ = self._run(['source', '--root', str(self.root)])
        self.assertEqual(rc, 2)
        self.assertIn('note', out)

    def test_note_without_text_is_argparse_error(self) -> None:
        import fha
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                fha.main(['source', 'note', SID, '--root', str(self.root)])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn('--text', err.getvalue())

    def test_cli_write_succeeds(self) -> None:
        rc, out, _ = self._run(
            ['source', 'note', SID, '--text', 'Found in the attic.',
             '--root', str(self.root)])
        self.assertEqual(rc, 0)
        self.assertIn('Added a note', out)
        self.assertIn('Found in the attic.', self.record.read_text(encoding='utf-8'))

    def test_cli_dry_run_writes_nothing(self) -> None:
        before = self.record.read_bytes()
        rc, out, _ = self._run(
            ['source', 'note', SID, '--text', 'Preview only.',
             '--dry-run', '--root', str(self.root)])
        self.assertEqual(rc, 0)
        self.assertIn('dry-run', out)
        self.assertEqual(self.record.read_bytes(), before)

    def test_cli_unknown_id_exit_1(self) -> None:
        # The not-found message is a warning, not an error - _emit sends
        # warning-level messages to stdout (only 'error' goes to stderr).
        rc, out, _ = self._run(
            ['source', 'note', 'S-zzzzzzzzzz', '--text', 'x', '--root', str(self.root)])
        self.assertEqual(rc, 1)
        self.assertIn('fha find S-zzzzzzzzzz', out)


if __name__ == '__main__':
    unittest.main()
