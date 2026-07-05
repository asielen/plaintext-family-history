"""
test_lib_text.py - the _lib text helpers shared by export/publication tools.

Covers the round-2 consolidation wave (private/plans/review-round2-fixes.md):

  - read_text_exact / write_text_exact - the newline-exact IO pair (finding 3c):
    a read/modify/write round-trip must not translate CRLF/LF line endings.
  - strip_unaccepted_drafts - THE one draft-strip implementation (cleanup K1),
    with the fail-closed damaged-marker contract (finding 18/X1: a marker the
    grammar cannot account for returns ('', problem), never the draft) and the
    `[ \t]` heading-boundary fix (finding 17/X2: a bare `##` line no longer
    swallows the next line into the "heading").
  - is_generated_text / is_generated_file - first-non-blank-line GENERATED
    ownership, BOM tolerated (finding 12/K3).
  - resolve_typed_ref - the shared typed resolver (K4); index/lint/confirm
    re-point their local copies in the next wave, so these tests pin the
    contract those owners will inherit.
  - resolve_root_arg - the --root chokepoint (finding 10): an explicit --root
    without fha.yaml refuses at the ONE shared site, so report/capture/
    photoindex and every other caller inherit the guard instead of hand-copying
    it (the three copies in index/find/id-check had already diverged).
  - _read_unfenced_claims (via read_record) - strict-first parsing: a ```
    quoted inside an unfenced claim's `value: |` is evidence, not a fence,
    and must survive the read; the drop retry stays for half-typed fences.

The site/wikitree integration halves of the draft-strip contract live in
tests/test_site.py (withhold + warn) and tests/test_wikitree.py (refusal).
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

from _lib import (
    is_generated_file,
    is_generated_text,
    read_record,
    read_text_exact,
    resolve_root_arg,
    resolve_typed_ref,
    strip_unaccepted_drafts,
    write_text_exact,
)


class ExactNewlineIOTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_crlf_file_round_trips_byte_identical(self):
        p = self.dir / 'crlf.md'
        original = b'---\r\nid: S-aaaaaaaaaa\r\n---\r\nline one\r\nline two\r\n'
        p.write_bytes(original)
        text = read_text_exact(p)
        self.assertIn('\r\n', text)          # CRLF survived the read
        write_text_exact(p, text)
        self.assertEqual(p.read_bytes(), original)

    def test_lf_write_not_crlf_translated(self):
        # The write half of the hazard: on Windows the default text mode
        # would turn every \n into \r\n.
        p = self.dir / 'lf.md'
        write_text_exact(p, 'a\nb\n')
        self.assertEqual(p.read_bytes(), b'a\nb\n')


class StripUnacceptedDraftsTests(unittest.TestCase):
    """The marker grammar mirrors confirm.py's _AI_DRAFT_RE (`<!--` + optional
    whitespace + word + lazy body + `-->`, DOTALL); these tests pin that
    grammar's edges plus the fail-closed signaling for damaged markers."""

    # - well-formed input: same behavior the per-tool copies had -

    def test_end_marker_style_draft_cut_human_kept(self):
        out, problem = strip_unaccepted_drafts(
            '## Biography\n'
            'Drafted paragraph one.\n\n'
            'Drafted paragraph two.\n\n'
            '<!-- AI-DRAFT 2026-07-01 claude-x - drafted from census -->\n\n'
            'A human-written paragraph that stays.\n')
        self.assertIsNone(problem)
        self.assertIn('A human-written paragraph that stays.', out)
        self.assertNotIn('Drafted paragraph', out)
        self.assertNotIn('AI-DRAFT', out)

    def test_accepted_publishes_with_marker_removed(self):
        out, problem = strip_unaccepted_drafts(
            'An accepted paragraph.\n\n'
            '<!-- AI-ACCEPTED 2026-06-01 claude-x - v1 (accepted 2026-06-20) -->\n')
        self.assertIsNone(problem)
        self.assertIn('An accepted paragraph.', out)
        self.assertNotIn('AI-ACCEPTED', out)

    def test_emptied_section_heading_dropped(self):
        out, problem = strip_unaccepted_drafts(
            '## Stories\nA drafted tale.\n\n<!-- AI-DRAFT 2026-07-01 m - s -->\n')
        self.assertIsNone(problem)
        self.assertNotIn('## Stories', out)
        self.assertNotIn('A drafted tale.', out)

    def test_nospace_multiline_marker(self):
        # `<!--AI-DRAFT` (no space) and a marker body spanning lines are both
        # inside the grammar.
        out, problem = strip_unaccepted_drafts(
            'X.\n\n<!--AI-DRAFT 2026-07-01 m\n - long note -->\n\nKept.')
        self.assertIsNone(problem)
        self.assertNotIn('X.', out)
        self.assertIn('Kept.', out)

    def test_subheading_inside_draft_is_excluded(self):
        # A ###+ subheading is draft content, not a boundary - otherwise the
        # top of an unaccepted draft would publish.
        out, problem = strip_unaccepted_drafts(
            '## Biography\n\nHuman intro.\n\n<!-- AI-ACCEPTED 1 m - n -->\n\n'
            '### Early life\n\nDraft para.\n\n<!-- AI-DRAFT 2 m - n -->\n')
        self.assertIsNone(problem)
        self.assertIn('Human intro.', out)
        self.assertNotIn('Early life', out)
        self.assertNotIn('Draft para.', out)
        self.assertNotIn('AI-', out)

    def test_text_without_markers_untouched(self):
        text = 'Plain.\n\n\n\nText with <!-- an ordinary comment -->.\n'
        self.assertEqual(strip_unaccepted_drafts(text), (text, None))

    # - X2: the [ \t] heading boundary -

    def test_bare_heading_line_no_longer_swallows_next_line(self):
        # With `\s` in the heading regex, `##\n` matched as a heading whose
        # text was the ENTIRE next line, making that line a boundary survivor:
        # one line of unaccepted draft published. `[ \t]` closes it - a bare
        # `##` is not a boundary, so the cut runs up to the accepted marker.
        out, problem = strip_unaccepted_drafts(
            '## Biography\n\nIntro.\n\n<!-- AI-ACCEPTED 1 m - n -->\n\n'
            '##\nLeaked draft line.\n\nMore draft.\n\n<!-- AI-DRAFT 2 m - n -->\n')
        self.assertIsNone(problem)
        self.assertIn('Intro.', out)
        self.assertNotIn('Leaked draft line.', out)
        self.assertNotIn('More draft.', out)

    # - X1: damaged markers fail closed -

    def test_unterminated_draft_marker_signals_and_returns_nothing(self):
        out, problem = strip_unaccepted_drafts(
            'Human text.\n\nDraft text.\n\n<!-- AI-DRAFT 2026-07-01 m - no arrow\n')
        self.assertEqual(out, '')            # NOTHING publishes, not even human text
        self.assertIsNotNone(problem)
        self.assertIn('AI-DRAFT', problem)
        self.assertIn('-->', problem)

    def test_wrap_style_signals_instead_of_leaking_below(self):
        # Wrap-style authoring (the natural reading of "goes inside markers"):
        # the leading marker is complete, so the old code cut the HUMAN text
        # above it and published the draft below. The orphan closer now trips
        # the residue check and everything is withheld.
        out, problem = strip_unaccepted_drafts(
            'Human text above.\n\n'
            '<!-- AI-DRAFT 2026-07-01 claude-x - wrap -->\n'
            'Wrapped draft paragraph.\n'
            '<!-- /AI-DRAFT -->\n')
        self.assertEqual(out, '')
        self.assertIsNotNone(problem)

    def test_orphan_closer_alone_signals(self):
        out, problem = strip_unaccepted_drafts('Text.\n<!-- /AI-DRAFT -->\n')
        self.assertEqual(out, '')
        self.assertIsNotNone(problem)

    def test_unterminated_accepted_marker_signals(self):
        # Same hazard family: a broken AI-ACCEPTED marker would ship its
        # dangling comment text as visible output, and it corrupts the
        # boundary scan for any draft below it.
        out, problem = strip_unaccepted_drafts(
            'Para.\n\n<!-- AI-ACCEPTED 2026-06-01 m - no arrow\n')
        self.assertEqual(out, '')
        self.assertIn('AI-ACCEPTED', problem)

    def test_prose_mention_of_marker_word_signals(self):
        # Deliberate over-withholding: a bare textual 'AI-DRAFT' cannot be
        # told from a mangled marker without guessing, and guessing is what
        # this function exists to never do. The problem message sends the
        # human to the file either way.
        out, problem = strip_unaccepted_drafts('The AI-DRAFT workflow is neat.\n')
        self.assertEqual(out, '')
        self.assertIsNotNone(problem)

    def test_damaged_marker_beside_valid_marker_still_signals(self):
        # One complete marker must not launder a second, damaged one.
        out, problem = strip_unaccepted_drafts(
            'Draft A.\n\n<!-- AI-DRAFT 1 m - ok -->\n\n'
            'Draft B.\n\n<!-- AI-DRAFT 2 m - broken\n')
        self.assertEqual(out, '')
        self.assertIsNotNone(problem)


class GeneratedOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_header_at_start(self):
        self.assertTrue(is_generated_text(
            '<!-- GENERATED by fha views timeline on 2026-01-01 - do not edit -->\nbody'))

    def test_leading_blank_lines_tolerated(self):
        # The finding-12 shape: normalize-links' byte-0 check treated this as
        # a hand-written record and rewrote inside it.
        self.assertTrue(is_generated_text('\n\n<!-- GENERATED by fha views x -->\n'))

    def test_bom_tolerated(self):
        self.assertTrue(is_generated_text('﻿<!-- GENERATED by fha views x -->'))

    def test_prose_file_is_not_generated(self):
        self.assertFalse(is_generated_text('# A profile\n\n<!-- GENERATED --> later'))

    def test_marker_later_in_body_does_not_count(self):
        self.assertFalse(is_generated_text('text\n<!-- GENERATED by fha views -->'))

    def test_empty_text_is_not_generated(self):
        self.assertFalse(is_generated_text(''))
        self.assertFalse(is_generated_text('\n \n\t\n'))

    def test_narrower_prefix_scopes_to_one_tool(self):
        views_prefix = '<!-- GENERATED by fha views'
        views_file = '<!-- GENERATED by fha views timeline on 2026-01-01 -->'
        other_file = '<!-- GENERATED by fha lint report on 2026-01-01 -->'
        self.assertTrue(is_generated_text(views_file, prefix=views_prefix))
        self.assertFalse(is_generated_text(other_file, prefix=views_prefix))
        self.assertTrue(is_generated_text(other_file))     # generic prefix: any tool

    def test_file_variants(self):
        gen = self.dir / 'gen.md'
        gen.write_text('\n<!-- GENERATED by fha views x -->\n', encoding='utf-8')
        self.assertTrue(is_generated_file(gen))
        bom = self.dir / 'bom.md'
        bom.write_bytes('﻿<!-- GENERATED by fha views x -->\n'.encode('utf-8'))
        self.assertTrue(is_generated_file(bom))
        hand = self.dir / 'hand.md'
        hand.write_text('# My notes\n', encoding='utf-8')
        self.assertFalse(is_generated_file(hand))

    def test_unreadable_path_is_not_generated(self):
        # A directory raises OSError on read: "cannot read" must mean
        # "treat as human-owned, never touch" for the deleters/overwriters.
        self.assertFalse(is_generated_file(self.dir))
        self.assertFalse(is_generated_file(self.dir / 'missing.md'))


class ResolveTypedRefTests(unittest.TestCase):
    ALIASES = {
        'sam rivera': 'p-aaaaaaaaaa',
        'fairview': 'l-bbbbbbbbbb',
    }

    def test_wikilinked_name_resolves_through_aliases(self):
        self.assertEqual(
            resolve_typed_ref('[[Sam Rivera]]', self.ALIASES, 'P'), 'p-aaaaaaaaaa')

    def test_type_filter_blocks_cross_type_edges(self):
        self.assertIsNone(resolve_typed_ref('[[Sam Rivera]]', self.ALIASES, 'L'))
        self.assertEqual(
            resolve_typed_ref('Fairview', self.ALIASES, 'L'), 'l-bbbbbbbbbb')

    def test_id_shaped_target_kept_even_when_dangling(self):
        # Integrity is lint's job (E005); the resolver keeps a real ID as-is.
        self.assertEqual(
            resolve_typed_ref('[[P-cccccccccc|Sam]]', self.ALIASES, 'P'),
            'p-cccccccccc')

    def test_bare_id_normalized_without_alias_map(self):
        self.assertEqual(resolve_typed_ref('P-CCCCCCCCCC', None, 'P'), 'p-cccccccccc')

    def test_near_miss_id_is_inert(self):
        # 9 chars / bad letter: not ID-shaped-valid, not an alias -> None.
        # Lint speaks about these separately (round-2 finding 7, its owner's
        # wave); the resolver's contract is only "never store garbage".
        self.assertIsNone(resolve_typed_ref('P-de957bcda', self.ALIASES, 'P'))
        self.assertIsNone(resolve_typed_ref('P-de957bcdal', self.ALIASES, 'P'))

    def test_unknown_and_empty_inputs_are_inert(self):
        self.assertIsNone(resolve_typed_ref('[[Nobody Known]]', self.ALIASES, 'P'))
        self.assertIsNone(resolve_typed_ref('', self.ALIASES, 'P'))
        self.assertIsNone(resolve_typed_ref(None, self.ALIASES, 'P'))
        self.assertIsNone(resolve_typed_ref('[[Sam Rivera]]', None, 'P'))

    def test_want_none_accepts_any_type(self):
        self.assertEqual(
            resolve_typed_ref('[[Fairview]]', self.ALIASES), 'l-bbbbbbbbbb')


class ResolveRootArgTests(unittest.TestCase):
    """The one chokepoint for --root validation (round-2 finding 10). Every
    tool that calls resolve_root_arg inherits the refusal; the per-tool guard
    copies in index/find/id-check were deleted in its favor, so these unit
    tests now pin the shared behavior every CLI-level guard test rides on."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _resolve(self, root, command=None):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            got = resolve_root_arg(SimpleNamespace(root=root), command=command)
        return got, err.getvalue()

    def test_explicit_root_without_fha_yaml_refused(self):
        got, err = self._resolve(str(self.dir))
        self.assertIsNone(got)
        self.assertIn('does not look like an archive', err)
        self.assertIn('fha.yaml', err)
        self.assertIn('--root', err)
        # The fabrication guarantee the message makes must be checkable.
        self.assertIn('Nothing was changed or created', err)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_explicit_root_with_fha_yaml_resolves(self):
        (self.dir / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        got, err = self._resolve(str(self.dir))
        self.assertEqual(got, self.dir.resolve())
        self.assertEqual(err, '')

    def test_fha_yaml_directory_is_not_an_archive_marker(self):
        # .is_file(), not .exists(): a folder named fha.yaml is not a config
        # (this is exactly where the hand copies had diverged).
        (self.dir / 'fha.yaml').mkdir()
        got, err = self._resolve(str(self.dir))
        self.assertIsNone(got)
        self.assertIn('does not look like an archive', err)

    def test_command_parameter_names_the_command(self):
        got, err = self._resolve(str(self.dir), command='fha id check')
        self.assertIsNone(got)
        self.assertIn('Run `fha id check` from inside your archive', err)

    def test_command_derived_from_dispatcher_namespace(self):
        # fha.py's add_subparsers(dest='command') stamps the subcommand name
        # on every dispatched namespace - tools that don't pass the parameter
        # still get an exact message through the fha front door.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            got = resolve_root_arg(
                SimpleNamespace(root=str(self.dir), command='photoindex'))
        self.assertIsNone(got)
        self.assertIn('Run `fha photoindex` from inside your archive', err.getvalue())

    def test_generic_wording_when_no_command_known(self):
        # A tool's standalone parser namespace has neither hint.
        got, err = self._resolve(str(self.dir))
        self.assertIsNone(got)
        self.assertIn('Run the command from inside your archive', err)

    def test_no_root_and_no_archive_above_cwd_refused(self):
        # Auto-detect path unchanged: from a folder with no fha.yaml anywhere
        # above, the established missing-root message still fires. (A tempdir
        # is outside the repo tree, so nothing above it carries fha.yaml.)
        cwd = os.getcwd()
        os.chdir(self.dir)
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                got = resolve_root_arg(SimpleNamespace(root=None))
        finally:
            os.chdir(cwd)
        self.assertIsNone(got)
        self.assertIn('cannot find archive root', err.getvalue())


_UNFENCED_QUOTED_FENCE = '''---
id: S-aaaaaaaaaa
title: Letter with a code block
source_type: letter
---

## Claims

- id: C-aaaaaaaaaa
  type: note
  persons: [P-aaaaaaaaaa]
  status: suggested
  value: |
    The letter includes a snippet:
    ```
    do not lose me
    ```
    and continues after it.

## Notes
'''

_UNFENCED_HALF_TYPED_FENCE = '''---
id: S-bbbbbbbbbb
title: Half-typed fence
source_type: letter
---

## Claims

```yaml
- id: C-bbbbbbbbbb
  type: note
  persons: [P-aaaaaaaaaa]
  status: suggested
  value: plain enough

## Notes
'''


class ReadUnfencedClaimsStrictFirstTests(unittest.TestCase):
    """`_read_unfenced_claims` must not mutate evidence as it reads (the
    review-fixes flag on finding 4's reader half): lint's --fix-claims-fence
    already REFUSES to drop ```-lookalike lines on disk, so the in-memory
    reader silently dropping the same lines meant every consumer saw
    different evidence than the file holds. Strict-first keeps the author's
    bytes when they parse; the drop retry keeps a half-typed fence readable."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _claims(self, text):
        p = self.dir / 'src.md'
        p.write_text(text, encoding='utf-8')
        return read_record(p)

    def test_quoted_fence_inside_value_survives_the_read(self):
        rec = self._claims(_UNFENCED_QUOTED_FENCE)
        self.assertTrue(rec['unfenced_claims'])
        self.assertEqual(len(rec['claims']), 1)
        value = rec['claims'][0]['value']
        self.assertIn('```', value)                    # the evidence bytes are intact
        self.assertIn('do not lose me', value)
        self.assertIn('and continues after it.', value)

    def test_half_typed_fence_still_parses_via_retry(self):
        # An opening ```yaml with no close breaks the strict parse; the
        # forgiving retry (drop fence lines) recovers the claims exactly as
        # the old reader did.
        rec = self._claims(_UNFENCED_HALF_TYPED_FENCE)
        self.assertTrue(rec['unfenced_claims'])
        self.assertEqual(len(rec['claims']), 1)
        self.assertEqual(rec['claims'][0]['id'], 'C-bbbbbbbbbb')

    def test_prose_under_claims_heading_still_never_claims(self):
        rec = self._claims(
            '---\nid: S-cccccccccc\ntitle: T\nsource_type: other\n---\n\n'
            '## Claims\n\nJust a sentence about claims, not YAML.\n')
        self.assertEqual(rec['claims'], [])
        self.assertFalse(rec['unfenced_claims'])

    def test_fence_only_section_is_empty(self):
        rec = self._claims(
            '---\nid: S-dddddddddd\ntitle: T\nsource_type: other\n---\n\n'
            '## Claims\n\n```\n')
        self.assertEqual(rec['claims'], [])
        self.assertFalse(rec['unfenced_claims'])


if __name__ == '__main__':
    unittest.main()
