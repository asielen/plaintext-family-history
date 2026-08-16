"""Tests for the `transcribe-source` skill's SKILL.md (BUILD_INTERFACE.md MI8.1).

A SKILL.md is prose and has no unit test (`_STANDARD.md` §9). But this skill's
prose carries three things that are mechanically checkable, and all three fail
silently and expensively when they drift:

 1. THE DOCUMENTED COMMANDS ACTUALLY RUN. Every `fha` verb in a code fence is a
    verb the CLI registers, and the worked `--more` example really produces the
    filename the prose shows when put through `process.attach_more`'s own naming
    rule. A skill is the human's memory of how the tools behave; a documented
    command that does not run as written teaches the wrong thing every time it
    is read.

 2. THE SHIPPED PREDICATES ARE QUOTED, NOT RE-DERIVED. The skill's definition of
    "image-only" and the scope of its marker contract must be the vocabulary
    `_lib` already owns (`TEXT_COMPANION_ROLES`, `SEARCHABLE_TEXT_SUFFIXES`,
    `file_entry_carries_text`). A second hand-written copy of a rule drifts on
    the first new role, and then lint's W124, `fha find --text`'s coverage note
    and this skill disagree about which sources are unreadable.

 3. THE MARKER CONTRACT IS EXECUTED, NOT READ FOR KEYWORDS. `fha find --text`
    is to mark hits drawn from an unreviewed machine reading, so the four states
    and the placement rule are a real contract. The placement rule in particular
    is not decoration: `_lib.strip_unaccepted_drafts` treats an AI marker as
    sitting at the END of the span it covers and a `#`/`##` heading as a block
    boundary, so the documented end-of-file marker on a heading-free body
    withholds the whole unchecked transcript, while the two obvious alternatives
    (marker at the top, or `##` page headings) publish it. Both counter-examples
    are asserted here, because "we chose this placement for a reason" is worth
    nothing unless the reason is executed.
"""

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import confirm  # noqa: E402
import process  # noqa: E402
from _lib import (  # noqa: E402
    SEARCHABLE_TEXT_SUFFIXES,
    TEXT_COMPANION_ROLES,
    file_entry_carries_text,
    strip_unaccepted_drafts,
)
from _lib import _AI_DRAFT_MARK_RE  # noqa: E402

SKILL_DIR = ROOT / '.claude' / 'skills' / 'transcribe-source'
SKILL_MD = SKILL_DIR / 'SKILL.md'
GAP_MD = SKILL_DIR / 'GAP.md'


def fenced_blocks(markdown_text):
    """Every ``` fenced block body in a markdown file.

    Fences inside a numbered step are indented, so the opener and closer are
    matched with leading whitespace allowed - without that this returns nothing
    and every drift guard built on it passes vacuously.
    """
    return re.findall(r'^[ \t]*```[^\n]*\n(.*?)^[ \t]*```',
                      markdown_text, re.S | re.M)


def transcript_template(markdown_text):
    """The worked transcript file the SKILL.md shows the agent to write.

    Pinned by reading it back out of the prose rather than copying it here: a
    copy would keep passing after the documented shape changed, which is the one
    failure this module exists to prevent.
    """
    for block in fenced_blocks(markdown_text):
        if '# Transcript -' in block:
            return block
    raise AssertionError('SKILL.md no longer shows a worked transcript file')


class DocumentedCommandsExistTest(unittest.TestCase):
    """A workflow that stops at a parser error is a broken workflow."""

    def setUp(self):
        self.skill = SKILL_MD.read_text(encoding='utf-8')

    def test_every_fha_verb_in_a_code_fence_is_registered(self):
        verbs = set()
        for block in fenced_blocks(self.skill):
            for line in block.splitlines():
                m = re.match(r'^\s*(?:\./|\.\\)?fha\s+([a-z][a-z-]*)', line)
                if m:
                    verbs.add(m.group(1))
        self.assertTrue(verbs, 'SKILL.md shows no fha commands at all')
        for verb in sorted(verbs):
            proc = subprocess.run(
                [sys.executable, str(ROOT / 'tools' / 'fha.py'), verb, '--help'],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0,
                             'SKILL.md tells the agent to run `fha %s`, which fha '
                             'does not register:\n%s' % (verb, proc.stderr))

    def test_the_more_example_produces_the_filename_the_skill_promises(self):
        """`{stem}.md` -> `{stem}-transcript_{S-id}.md`, by the tool's own rule.

        The skill tells the agent to write the companion under the source's
        SHARED STEM with neither the S-id nor the role in it, because
        `attach_more` appends the role itself. Get that wrong and the file lands
        as `…-transcript-transcript_S-….md`, or `--more` refuses it outright -
        both of which the prose promises will not happen.
        """
        self.assertIn('{stem}-transcript_{S-id}.md', self.skill,
                      'SKILL.md no longer states the resulting filename')
        sid = 'S-9tq4vn2k7b'
        stem = 'harkness-chart'
        # attach_more's naming rule, quoted from process.py's document branch.
        produced = '%s-%s_%s.md' % (process._slugify(stem),
                                    process._slugify('transcript'), sid)
        documented = ('{stem}-transcript_{S-id}.md'
                      .replace('{stem}', stem).replace('{S-id}', sid))
        self.assertEqual(produced, documented,
                         'the documented pattern and attach_more disagree on the '
                         'filename the human will see')

    def test_the_role_it_attaches_is_one_the_tools_read_as_text(self):
        """`transcript` must be in the vocabulary lint/index/find share.

        The whole skill is worthless if the companion it attaches is invisible
        to the surfaces that detect the gap - W124 would keep firing and
        `fha find --text` would keep counting the source as mute.
        """
        self.assertIn('transcript', TEXT_COMPANION_ROLES)
        self.assertTrue(file_entry_carries_text('transcript', 'documents/x.md'))


class ShippedPredicatesAreQuotedTest(unittest.TestCase):
    """The skill must reuse `_lib`'s vocabulary, never re-derive it."""

    def setUp(self):
        self.skill = SKILL_MD.read_text(encoding='utf-8')

    def test_image_only_definition_names_the_shipped_predicate(self):
        self.assertIn('file_entry_carries_text', self.skill,
                      'SKILL.md defines "image-only" without pointing at the one '
                      'predicate lint and find share - the definitions will drift')

    def test_image_only_definition_lists_exactly_the_shipped_roles(self):
        section = self.skill.split('A source is **image-only**', 1)[1].split('\n\n', 1)[0]
        for role in TEXT_COMPANION_ROLES:
            self.assertIn(role, section,
                          'the image-only definition omits the %r role, so the '
                          'skill would transcribe a source the tools already '
                          'count as readable' % role)

    def test_marker_scope_lists_exactly_the_indexed_suffixes(self):
        section = self.skill.split('**Scope.**', 1)[1].split('\n\n', 1)[0]
        for suffix in SEARCHABLE_TEXT_SUFFIXES:
            self.assertIn(suffix, section)
        for role in TEXT_COMPANION_ROLES:
            self.assertIn(role, section)


class MarkerContractTest(unittest.TestCase):
    """The four states, and the placement rule, executed rather than read."""

    def setUp(self):
        self.skill = SKILL_MD.read_text(encoding='utf-8')
        self.template = transcript_template(self.skill)

    def test_the_documented_marker_is_the_shipped_grammar(self):
        """The marker the skill writes is one the archive's own regexes match.

        Both are checked: `_lib`'s stripper decides what a publication path
        withholds, and `confirm`'s decides what a flip verb could ever match.
        A marker only one of them recognises is a marker that means two things.
        """
        self.assertRegex(self.template, _AI_DRAFT_MARK_RE)
        self.assertRegex(self.template, confirm._AI_DRAFT_RE)

    def test_all_four_states_are_named(self):
        for state in ('unreviewed', 'verified', 'unmarked', 'damaged'):
            self.assertIn(state, self.skill,
                          'the marker contract no longer names the %r state' % state)

    def test_documented_placement_withholds_the_whole_transcript(self):
        """End-of-file marker, no `#`/`##` below the title => fail closed.

        This is the property the placement rule exists to buy: if any
        publication path ever runs `strip_unaccepted_drafts` over transcript
        text, an unchecked machine reading of a family document is withheld
        entirely rather than published.
        """
        cleaned, problem = strip_unaccepted_drafts(self.template)
        self.assertIsNone(problem)
        self.assertEqual(cleaned.strip(), '',
                         'the documented transcript shape leaks %d characters of '
                         'unreviewed transcript through the publication '
                         'stripper' % len(cleaned.strip()))

    def test_a_top_of_file_marker_would_leak_the_whole_transcript(self):
        """The counter-example the placement rule is chosen against.

        A marker at the TOP of the file cuts only what precedes it, so the
        entire unchecked reading survives the stripper. This is why the rule is
        'last non-blank line' and not 'somewhere in the file'.
        """
        marker = _AI_DRAFT_MARK_RE.search(self.template).group(0)
        body = _AI_DRAFT_MARK_RE.sub('', self.template).strip()
        top_first = marker + '\n\n' + body
        cleaned, problem = strip_unaccepted_drafts(top_first)
        self.assertIsNone(problem)
        self.assertIn('[Page 1]', cleaned,
                      'the top-of-file placement no longer leaks - if the '
                      'stripper changed, re-derive the placement rule rather '
                      'than deleting this test')

    def test_a_heading_inside_the_body_would_leak_the_pages_above_it(self):
        """Why page divisions are `[Page N]` labels and not `##` headings.

        A `#`/`##` heading is a block boundary, so a marker at the end of the
        file only covers back to the LAST heading - every page above it
        publishes.
        """
        with_heading = self.template.replace('[Page 2]', '## Page 2')
        self.assertNotEqual(with_heading, self.template)
        cleaned, problem = strip_unaccepted_drafts(with_heading)
        self.assertIsNone(problem)
        self.assertIn('[Page 1]', cleaned,
                      'headings inside the body no longer leak - if the stripper '
                      'changed, re-derive the "no headings" rule rather than '
                      'deleting this test')

    def test_the_skill_states_both_placement_rules(self):
        self.assertIn('last non-blank line', self.skill)
        self.assertRegex(self.skill, r'no `#`/`##` heading below the title')

    def test_a_damaged_marker_fails_closed(self):
        """`damaged` is treated as unreviewed, never as verified."""
        damaged = self.template.replace('-->', '', 1)
        cleaned, problem = strip_unaccepted_drafts(damaged)
        self.assertIsNotNone(problem)
        self.assertEqual(cleaned, '',
                         'a damaged marker no longer fails closed in _lib; the '
                         'skill promises it does')

    def test_precedence_is_stated_the_safe_way_round(self):
        clause = self.skill.split('**Precedence and posture.**', 1)[1].split('\n\n', 1)[0]
        self.assertIn('*unreviewed* outranks *verified*', clause)
        self.assertIn('*damaged* is treated as **unreviewed**', clause)

    def test_an_extract_dump_carries_no_marker(self):
        """`fha source extract`'s output is mechanical, not a model reading.

        Marking it AI-DRAFT would flag a faithful copy of the PDF's own words as
        an unchecked machine reading, which is false and would train the reader
        to ignore the flag.
        """
        self.assertRegex(self.skill, r'carries \*\*no\*\* AI marker')


class ContractPostureTest(unittest.TestCase):
    """The skill produces text and hands off - it never touches a claim."""

    def setUp(self):
        self.skill = SKILL_MD.read_text(encoding='utf-8')

    def test_no_fenced_command_writes_a_claim(self):
        for block in fenced_blocks(self.skill):
            for line in block.splitlines():
                self.assertNotRegex(
                    line, r'^\s*(?:\./|\.\\)?fha\s+claim\b',
                    'SKILL.md shows a runnable `fha claim` command; this skill '
                    'drafts and edits no claims - contradictions become open '
                    'questions and new facts hand off to mine-transcript')

    def test_it_says_the_image_stays_the_evidence_of_record(self):
        self.assertIn('The image remains the evidence of record', self.skill)
        self.assertIn('index into it', self.skill)

    def test_a_contradiction_goes_to_the_question_log(self):
        self.assertIn('notes/questions.md', self.skill)
        self.assertIn('## Q:', self.skill)
        self.assertIn('origin: agent', self.skill)

    def test_the_uncertainty_vocabulary_is_present_and_closed(self):
        for token in ('[illegible]', '[sic]', '[torn]', '[struck:', '[unclear:'):
            self.assertIn(token, self.skill,
                          'the uncertainty vocabulary no longer documents %s - a '
                          'reading it cannot express becomes a silent guess' % token)

    def test_the_backfill_batch_is_bounded_and_resumed_from_lint(self):
        section = self.skill.split('## Backfill', 1)[1]
        self.assertIn('five sources', section)
        self.assertIn('W124', section)
        self.assertIn('fha lint --json', section)
        self.assertIn('Sessions are an interface, not memory', section)

    def test_the_blocked_gap_is_recorded_not_worked_around(self):
        self.assertTrue(GAP_MD.is_file(), 'the skill records no GAP.md')
        gap = GAP_MD.read_text(encoding='utf-8')
        self.assertIn('confirm', gap)
        self.assertIn('blocked', gap.lower())
        self.assertIn('GAP.md', self.skill,
                      'SKILL.md never points at the gap it blocks on')


class SkillShapeTest(unittest.TestCase):
    """`_STANDARD.md` §2 and §10: two frontmatter keys, the skeleton's sections."""

    def setUp(self):
        self.text = SKILL_MD.read_text(encoding='utf-8')

    def test_frontmatter_has_exactly_name_and_description(self):
        m = re.match(r'^---\n(.*?)\n---\n', self.text, re.S)
        self.assertIsNotNone(m, 'SKILL.md has no frontmatter block')
        keys = re.findall(r'^([a-z-]+):', m.group(1), re.M)
        self.assertEqual(keys, ['name', 'description'],
                         'a harness-specific key breaks portability '
                         '(_STANDARD.md §2)')
        self.assertRegex(m.group(1),
                         re.compile(r'^name: transcribe-source$', re.M))

    def test_the_skeleton_sections_are_present(self):
        for heading in ('## When this runs', '## The contract for this skill',
                        '## Flow', '## Guardrails', '## Done when'):
            self.assertIn(heading, self.text,
                          'SKILL.md is missing the %r section (_STANDARD.md §10)'
                          % heading)

    def test_it_carries_no_machine_specific_absolute_path(self):
        """AGENTS_TOOLING §11 - and the rule the skill itself imposes."""
        for line in self.text.splitlines():
            self.assertNotRegex(line, r'(?<![\w./])/(?:home|Users)/',
                                'SKILL.md carries a machine-specific path')


if __name__ == '__main__':
    unittest.main()
