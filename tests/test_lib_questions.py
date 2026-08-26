"""
test_lib_questions.py - the `## Q:` block parser, shared (issue #117).

`parse_question_blocks`/`parse_questions` used to live only in report.py.
`fha site` needed the same parse to surface a person's open questions on
their own page, and `site.py` cannot import `report.py` (tools never import
tools - report.py is the documented exception, not a precedent to copy), so
both moved to `_lib.py` under public names and report.py imports them back.

These pin the parser itself, independent of either caller:
  - `parse_question_blocks` - the per-file heading/status/refs split
  - `parse_questions` - notes/questions.md + every person research file's
    own `## Open Questions` section, namespaced by file
  - the `refs:`-filtering idiom (`normalize_id`d, `p-` prefix) that both
    `fha report` and `fha site` use to find which people a question concerns

tests/test_report.py's `QuestionNamespacingTests` already covers the
research-file-vs-profile kind-slot ambiguity in detail through report.py's
own (now re-exported) name; this file exists so the parser has a test that
does not depend on report.py at all.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

from _lib import parse_question_blocks, parse_questions


class ParseQuestionBlocksTests(unittest.TestCase):
    """The per-file split, independent of any file I/O."""

    def test_heading_status_and_refs_are_extracted(self) -> None:
        text = (
            '## Q: When did Jane arrive?\n'
            '- origin: human\n'
            '- status: open\n'
            '- refs: [P-aaaaaaaaaa, H-bbbbbbbbbb]\n'
            '- context:\n'
            '  - (human, 2026-06-01) Census says 1880.\n'
        )
        blocks = parse_question_blocks(text)
        self.assertEqual(list(blocks), ['When did Jane arrive?'])
        info = blocks['When did Jane arrive?']
        self.assertEqual(info['status'], 'open')
        self.assertEqual(info['refs'], ['p-aaaaaaaaaa', 'h-bbbbbbbbbb'])
        self.assertIn('Census says 1880', info['block'])

    def test_multiple_blocks_split_on_q_heading_only(self) -> None:
        # A '## Q:' block runs to the NEXT '## Q:' heading - or, absent one,
        # to the end of the text - never to some OTHER heading level. This is
        # the documented (if surprising) behavior a caller that means to
        # DISPLAY a block verbatim must trim for itself (see site.py's
        # `_question_block_body`).
        text = (
            '## Q: First question?\n'
            '- status: open\n'
            '- refs: [P-aaaaaaaaaa]\n\n'
            '## Hypotheses\n\n*(none yet)*\n\n'
            '## Q: Second question?\n'
            '- status: closed (not pursuing)\n'
            '- refs: [P-bbbbbbbbbb]\n'
        )
        blocks = parse_question_blocks(text)
        self.assertEqual(set(blocks), {'First question?', 'Second question?'})
        # The first block's raw text runs through the intervening heading -
        # it does NOT stop at '## Hypotheses'.
        self.assertIn('Hypotheses', blocks['First question?']['block'])
        self.assertEqual(blocks['Second question?']['status'], 'closed (not pursuing)')

    def test_no_refs_yields_empty_list_not_a_crash(self) -> None:
        blocks = parse_question_blocks('## Q: Undecided so far\n- status: open\n')
        self.assertEqual(blocks['Undecided so far']['refs'], [])

    def test_text_with_no_q_heading_yields_nothing(self) -> None:
        self.assertEqual(parse_question_blocks('# Just a title\n\nSome prose.\n'), {})


class ParseQuestionsTests(unittest.TestCase):
    """The whole-archive read: notes/questions.md + every research file."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        (self.archive_root / 'notes').mkdir(parents=True)
        (self.archive_root / 'people').mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_reads_notes_questions_md(self) -> None:
        (self.archive_root / 'notes' / 'questions.md').write_text(
            '# Open Questions (general)\n\n'
            '## Q: Where did the Hartleys settle?\n'
            '- status: open\n'
            '- refs: [P-aaaaaaaaaa]\n',
            encoding='utf-8')
        questions = parse_questions(self.archive_root)
        self.assertEqual(len(questions), 1)
        key = next(iter(questions))
        self.assertEqual(key, 'notes/questions.md :: Where did the Hartleys settle?')
        self.assertEqual(questions[key]['file'], 'notes/questions.md')
        self.assertEqual(questions[key]['heading'], 'Where did the Hartleys settle?')

    def test_reads_a_person_research_files_open_questions_section(self) -> None:
        research = self.archive_root / 'people' / 'roe__test_research_p-bbbbbbbbbb.md'
        research.write_text(
            '---\nid: p-bbbbbbbbbb\ncreated: 2026-06-01\n---\n\n'
            '## Open Questions\n\n'
            '## Q: Are Jane and Bob related?\n'
            '- status: open\n'
            '- refs: [P-aaaaaaaaaa, P-bbbbbbbbbb]\n',
            encoding='utf-8')
        questions = parse_questions(self.archive_root)
        self.assertEqual(len(questions), 1)
        key = next(iter(questions))
        self.assertTrue(key.startswith('people/roe__test_research_p-bbbbbbbbbb.md ::'), key)
        self.assertEqual(
            questions[key]['refs'], ['p-aaaaaaaaaa', 'p-bbbbbbbbbb'])

    def test_a_profiles_own_open_questions_heading_is_not_in_scope(self) -> None:
        # SPEC §16 homes '## Open Questions' in the research companion, never
        # the profile itself - a profile carrying one anyway (a hand-authored
        # slip) must not join the question log just because the heading text
        # matches.
        profile = self.archive_root / 'people' / 'roe__test_p-cccccccccc.md'
        profile.write_text(
            '---\nid: p-cccccccccc\nname: Someone Roe\nliving: false\n---\n\n'
            '## Open Questions\n\n'
            '## Q: Should this even be read?\n'
            '- status: open\n'
            '- refs: [P-cccccccccc]\n',
            encoding='utf-8')
        self.assertEqual(parse_questions(self.archive_root), {})

    def test_refs_filter_by_person_prefix_finds_the_right_people(self) -> None:
        # The documented idiom (report.py's answerable-questions section,
        # site.py's _load_open_questions): filter refs: for the 'p-' prefix
        # to find which people a question concerns, ignoring H-/C-/S- refs.
        (self.archive_root / 'notes' / 'questions.md').write_text(
            '## Q: Mixed refs\n'
            '- status: open\n'
            '- refs: [P-aaaaaaaaaa, H-1111111111, P-bbbbbbbbbb]\n',
            encoding='utf-8')
        info = next(iter(parse_questions(self.archive_root).values()))
        person_refs = [r for r in info['refs'] if r.startswith('p-')]
        self.assertEqual(sorted(person_refs), ['p-aaaaaaaaaa', 'p-bbbbbbbbbb'])

    def test_missing_notes_and_people_dirs_do_not_crash(self) -> None:
        empty_root = Path(self._tmp.name) / 'nested_empty'
        empty_root.mkdir()
        self.assertEqual(parse_questions(empty_root), {})


class ParseQuestionsUndecodableFileTests(unittest.TestCase):
    """PR #179 review, finding 2: a `## Q:` log saved in the wrong text
    encoding used to raise `UnicodeDecodeError` straight out of this
    function - `path.read_text(encoding='utf-8')` behind a plain `except
    OSError`, and `UnicodeDecodeError` is a `ValueError`, not an `OSError`.
    Harmless while only `fha report` called `parse_questions` (one CLI
    command failing); a real crash once `fha site --linked` started calling
    it too (issue #117), since that build promises to always return a
    `Result`. `read_text_or_report` is this codebase's shared fix for
    exactly this failure shape - these tests pin `parse_questions` onto it,
    independent of either caller."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        (self.archive_root / 'notes').mkdir(parents=True)
        (self.archive_root / 'people').mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_undecodable_questions_md_is_skipped_not_raised(self) -> None:
        # No on_decode_error given: the old behavior (before this fix) was to
        # raise here. Now it is a silent skip, same as a missing file always
        # was - a caller with somewhere to put a warning opts in below.
        (self.archive_root / 'notes' / 'questions.md').write_bytes(
            '## Q: Kraków connection?\n- status: open\n'.encode('cp1252'))
        self.assertEqual(parse_questions(self.archive_root), {})

    def test_undecodable_questions_md_is_reported_when_asked(self) -> None:
        (self.archive_root / 'notes' / 'questions.md').write_bytes(
            '## Q: Kraków connection?\n- status: open\n'.encode('cp1252'))
        reported: list[Path] = []
        questions = parse_questions(self.archive_root, on_decode_error=reported.append)
        self.assertEqual(questions, {})
        self.assertEqual(len(reported), 1)

    def test_undecodable_research_file_is_skipped_other_files_still_parse(self) -> None:
        (self.archive_root / 'notes' / 'questions.md').write_text(
            '## Q: Healthy question?\n- status: open\n- refs: [P-aaaaaaaaaa]\n',
            encoding='utf-8')
        bad = self.archive_root / 'people' / 'roe__test_research_p-bbbbbbbbbb.md'
        bad.write_bytes(
            ('---\nid: p-bbbbbbbbbb\n---\n\n## Open Questions\n\n'
             '## Q: Kraków connection?\n- status: open\n- refs: [P-bbbbbbbbbb]\n')
            .encode('cp1252'))
        reported: list[Path] = []
        questions = parse_questions(self.archive_root, on_decode_error=reported.append)
        self.assertEqual(len(questions), 1)
        self.assertIn('notes/questions.md :: Healthy question?', questions)
        self.assertEqual(len(reported), 1)
        self.assertTrue(str(reported[0]).endswith('roe__test_research_p-bbbbbbbbbb.md'))


if __name__ == '__main__':
    unittest.main()
