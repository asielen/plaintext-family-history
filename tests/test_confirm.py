"""
test_confirm.py - fha confirm: human-directed write-back for detection candidates.

Covers the pure-text surgical edit helpers (link append, scalar set, claim
append, AI-DRAFT flip) and each verb's `run_*` contract (dry-run writes nothing,
invalid/duplicate IDs, not-found), plus end-to-end round-trips against a copy of
the example archive: confirm xref → the claim_link is present after re-index;
dismiss → the pair is excluded from the next cooccur; an accepted relationship →
the edge is derived on re-index (a suggested one is not); discovery → the entry
is in the file; contradiction → lint E009 stays satisfied.

Also covers the P1 indent regression: claim items validly written with a wider
dash-to-key spacing (`-   value:` with keys at column 4) must be edited at their
own column, and the pre-write re-parse guard must turn any block-corrupting
rewrite into a clean refusal (`_EditRefused` / status 'refused') with nothing
written.

Round-2 regressions covered here too: an `id: C-...` line quoted inside a block
scalar must never draw an edit onto the quoting claim (finding 2 - ownership is
the item's own key line, belt-and-braces parse check behind it); the cooccur
idempotency gate must read wikilink/nested `persons:` forms (finding 6); and a
pre-existing duplicate claim id refuses with the E001 repair path, not the
"would hide every claim" corruption wording (finding 15).
"""

import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import confirm
import cooccur
from _lib import (
    EXIT_CLEAN, EXIT_FAILURE, EXIT_WARNINGS, Result, load_fha_yaml, normalize_id, read_record,
)

EXAMPLE = ROOT / 'example-archive'

# Stable IDs from the example archive (fixtures, not the real archive).
CLAIM_A = 'C-77a0c5e218'          # census parent/child (S-4f5f215e60), accepted
CLAIM_B = 'C-fa0000001a'          # typescript parent/child (S-fa1234567b), accepted, no place
SOURCE = 'S-fc3456789d'           # bradford-family genealogy notes (has a ## Claims block)
PERSON_1 = 'P-4d5e6f7g8h'
PERSON_2 = 'P-6f7g8h9jka'
PERSON_3 = 'P-5e6f7g8h9j'
DRAFT_PERSON = 'P-2b3c4d5e6f'      # James Bradford profile carries one AI-DRAFT marker


# ── Pure-text edit helpers ──────────────────────────────────────────────────────

_CLAIMS = '''## Claims
```yaml
- value: "A claim"
  id: C-aa11bb22cc
  type: birth
  persons: [P-aaaaaaaaaa]
  status: accepted
  reviewed: 2026-01-01

- value: "Another"
  id: C-bb22cc33dd
  type: death
  persons: [P-aaaaaaaaaa]
  status: suggested
```
'''


class EditHelperTests(unittest.TestCase):
    def test_add_link_inserts_after_status(self) -> None:
        new, changed, already = confirm._add_link_to_claim(
            _CLAIMS, 'C-aa11bb22cc', 'corroborates', 'C-bb22cc33dd')
        self.assertTrue(changed)
        self.assertFalse(already)
        lines = new.splitlines()
        s = next(i for i, ln in enumerate(lines) if ln.strip() == 'status: accepted')
        self.assertEqual(lines[s + 1].strip(), 'corroborates: [C-bb22cc33dd]')

    def test_add_link_appends_to_existing_list(self) -> None:
        text = _CLAIMS.replace('  status: accepted\n  reviewed: 2026-01-01',
                               '  status: accepted\n  corroborates: [C-1111111111]\n  reviewed: 2026-01-01')
        new, changed, already = confirm._add_link_to_claim(
            text, 'C-aa11bb22cc', 'corroborates', 'C-bb22cc33dd')
        self.assertTrue(changed)
        self.assertIn('corroborates: [C-1111111111, C-bb22cc33dd]', new)

    def test_add_link_idempotent(self) -> None:
        text = _CLAIMS.replace('  status: accepted\n  reviewed: 2026-01-01',
                               '  status: accepted\n  corroborates: [C-bb22cc33dd]\n  reviewed: 2026-01-01')
        new, changed, already = confirm._add_link_to_claim(
            text, 'C-aa11bb22cc', 'corroborates', 'C-bb22cc33dd')
        self.assertFalse(changed)
        self.assertTrue(already)
        self.assertEqual(new, text)

    def test_result_as_dict_is_json_serializable(self) -> None:
        # Result.data routinely holds non-JSON objects (Paths, etc.); as_dict
        # must coerce them so headless callers can json.dumps the contract.
        r = Result(data={'path': Path('/tmp/x'), 'nested': [Path('a'), {'p': Path('b')}], 's': 'ok'})
        dumped = json.loads(json.dumps(r.as_dict()))
        self.assertEqual(dumped['data']['path'], '/tmp/x')
        self.assertEqual(dumped['data']['nested'], ['a', {'p': 'b'}])
        self.assertEqual(dumped['data']['s'], 'ok')

    def test_add_link_unknown_claim_no_change(self) -> None:
        new, changed, already = confirm._add_link_to_claim(
            _CLAIMS, 'C-9999999999', 'corroborates', 'C-bb22cc33dd')
        self.assertFalse(changed)
        self.assertFalse(already)

    def test_add_link_preserves_inline_comment(self) -> None:
        # A hand-written trailing comment on a link line must survive the rewrite
        # rather than be folded into the (now malformed) list.
        text = _CLAIMS.replace(
            '  status: accepted\n  reviewed: 2026-01-01',
            '  status: accepted\n  corroborates: [C-1111111111] # human-checked\n  reviewed: 2026-01-01')
        new, changed, already = confirm._add_link_to_claim(
            text, 'C-aa11bb22cc', 'corroborates', 'C-bb22cc33dd')
        self.assertTrue(changed)
        self.assertIn('corroborates: [C-1111111111, C-bb22cc33dd]  # human-checked', new)
        # …and the rewritten block still parses, with the link list intact.
        rec = _parse(new)['C-aa11bb22cc']
        self.assertEqual(rec['corroborates'], ['C-1111111111', 'C-bb22cc33dd'])

    def test_place_block_quotes_yaml_significant_values(self) -> None:
        # A name/hierarchy with YAML-significant text must be quoted so
        # places.yaml stays parseable and the value is not truncated as a comment.
        import yaml
        lines = confirm._place_block_lines('L-7c1a9f4e22', 'A: B', 'St. John #2')
        rec = yaml.safe_load('\n'.join(lines))[0]
        self.assertEqual(rec['name'], 'A: B')
        self.assertEqual(rec['hierarchy'], 'St. John #2')

    def test_set_scalar_inserts_place(self) -> None:
        new, changed = confirm._set_scalar_on_claim(_CLAIMS, 'C-bb22cc33dd', 'place', 'L-7c1a9f4e22')
        self.assertTrue(changed)
        rec = _parse(new)
        self.assertEqual(str(rec['C-bb22cc33dd']['place']), 'L-7c1a9f4e22')
        # sibling untouched
        self.assertNotIn('place', rec['C-aa11bb22cc'])

    def test_append_claim_keeps_siblings(self) -> None:
        item = ['- value: "New"', '  id: C-cc33dd44ee', '  type: note',
                '  persons: [P-aaaaaaaaaa]', '  status: suggested']
        new, changed = confirm._append_claim_to_source(_CLAIMS, item)
        self.assertTrue(changed)
        rec = _parse(new)
        self.assertEqual(set(rec), {'C-aa11bb22cc', 'C-bb22cc33dd', 'C-cc33dd44ee'})

    def test_append_claim_no_block(self) -> None:
        new, changed = confirm._append_claim_to_source('# no claims here\n', ['- value: x'])
        self.assertFalse(changed)

    def test_ai_draft_flip(self) -> None:
        body = 'Prose.\n<!-- AI-DRAFT 2026-06-14 claude-sonnet-4-6 - drafted -->\nMore.\n'
        new, n = confirm._AI_DRAFT_RE.subn(
            lambda m: f'<!--{m.group(1)}AI-ACCEPTED{m.group(2).rstrip()} (accepted 2026-06-24) -->',
            body)
        self.assertEqual(n, 1)
        self.assertIn('AI-ACCEPTED 2026-06-14 claude-sonnet-4-6 - drafted (accepted 2026-06-24)', new)
        self.assertNotIn('AI-DRAFT', new)


def _parse(text: str) -> dict:
    with tempfile.NamedTemporaryFile('w', suffix='.md', delete=False, encoding='utf-8') as fh:
        fh.write(text)
        tmp = Path(fh.name)
    try:
        return {c['id']: c for c in read_record(tmp)['claims']}
    finally:
        tmp.unlink(missing_ok=True)


# ── Wide-indent items (the P1 regression) and the pre-write guard ────────────────

_WIDE_CLAIMS = '''## Claims
```yaml
-   value: "A claim"
    id: C-aa11bb22cc
    type: birth
    persons: [P-aaaaaaaaaa]
    status: accepted
    reviewed: 2026-01-01

-   value: "Another"
    id: C-bb22cc33dd
    type: death
    persons: [P-aaaaaaaaaa]
    status: suggested
```
'''


class WideIndentEditTests(unittest.TestCase):
    """Claim items validly written `-   value:` (keys at column 4) must survive
    the surgical editors: the edit lands at the item's own column and the block
    still parses. These writers shared claim.py's base+2 indent assumption, so
    the same edit used to corrupt the whole block while reporting success."""

    def test_add_link_lands_at_the_items_column(self) -> None:
        new, changed, already = confirm._add_link_to_claim(
            _WIDE_CLAIMS, 'C-aa11bb22cc', 'corroborates', 'C-bb22cc33dd')
        self.assertTrue(changed)
        self.assertFalse(already)
        rec = _parse(new)
        self.assertEqual(rec['C-aa11bb22cc']['corroborates'], ['C-bb22cc33dd'])
        self.assertEqual(rec['C-bb22cc33dd']['status'], 'suggested')   # sibling intact

    def test_add_link_extends_wide_existing_list(self) -> None:
        text = _WIDE_CLAIMS.replace(
            '    status: accepted',
            '    status: accepted\n    corroborates: [C-1111111111]')
        new, changed, already = confirm._add_link_to_claim(
            text, 'C-aa11bb22cc', 'corroborates', 'C-bb22cc33dd')
        self.assertTrue(changed)
        self.assertIn('    corroborates: [C-1111111111, C-bb22cc33dd]', new)
        rec = _parse(new)
        self.assertEqual(rec['C-aa11bb22cc']['corroborates'],
                         ['C-1111111111', 'C-bb22cc33dd'])

    def test_set_scalar_lands_at_the_items_column(self) -> None:
        new, changed = confirm._set_scalar_on_claim(
            _WIDE_CLAIMS, 'C-bb22cc33dd', 'place', 'L-7c1a9f4e22')
        self.assertTrue(changed)
        rec = _parse(new)
        self.assertEqual(str(rec['C-bb22cc33dd']['place']), 'L-7c1a9f4e22')
        self.assertNotIn('place', rec['C-aa11bb22cc'])

    def test_wrong_indent_rewrite_is_refused(self) -> None:
        # Force the old buggy assumption (base indent + 2) back in, simulating a
        # future indent regression: the pre-write guard must raise the refusal
        # instead of returning text that would corrupt the block.
        import unittest.mock as mock
        with mock.patch.object(confirm, 'claim_item_key_indent',
                               lambda item, base: base + '  '):
            with self.assertRaises(confirm._EditRefused):
                confirm._add_link_to_claim(
                    _WIDE_CLAIMS, 'C-aa11bb22cc', 'corroborates', 'C-bb22cc33dd')
            with self.assertRaises(confirm._EditRefused):
                confirm._set_scalar_on_claim(
                    _WIDE_CLAIMS, 'C-bb22cc33dd', 'place', 'L-7c1a9f4e22')

    def test_append_to_indented_block_is_refused_not_corrupted(self) -> None:
        # A hand-indented block (items at column 2) cannot take the column-0
        # templated item without breaking its YAML; the guard refuses instead
        # of writing a block no tool can read.
        indented = ('## Claims\n```yaml\n'
                    '  - id: C-aa11bb22cc\n    status: accepted\n```\n')
        item = ['- value: "New"', '  id: C-cc33dd44ee', '  type: note',
                '  persons: [P-aaaaaaaaaa]', '  status: suggested']
        with self.assertRaises(confirm._EditRefused):
            confirm._append_claim_to_source(indented, item)


# ── Quoted id lines inside block scalars (round-2 finding 2, unit level) ────────

_QUOTED_ID_CLAIMS = '''## Claims
```yaml
- value: "Claim A - the decoy"
  id: C-aa11bb22cc
  type: residence
  persons: [P-aaaaaaaaaa]
  status: accepted
  reviewed: 2026-01-01
  notes: |
    Compare with the other claim:
    id: C-bb22cc33dd
    which covers the same event.

- value: "Claim B - the real target"
  id: C-bb22cc33dd
  type: death
  persons: [P-aaaaaaaaaa]
  status: suggested
```
'''


class QuotedIdSpanTests(unittest.TestCase):
    """A block scalar quoting an `id: C-...` line must never draw an edit onto
    the quoting claim: the id-key regex describes a SHAPE, but only a line at
    the item's own key column is the item's identity. Span matching on shape
    alone wrote xref links and place relinks onto the wrong claim under a
    success message (round-2 finding 2)."""

    def test_add_link_lands_on_the_owning_claim(self) -> None:
        new, changed, already = confirm._add_link_to_claim(
            _QUOTED_ID_CLAIMS, 'C-bb22cc33dd', 'corroborates', 'C-cc33dd44ee')
        self.assertTrue(changed)
        self.assertFalse(already)
        rec = _parse(new)
        self.assertEqual(rec['C-bb22cc33dd']['corroborates'], ['C-cc33dd44ee'])
        self.assertNotIn('corroborates', rec['C-aa11bb22cc'])
        # the quoted evidence text survives byte-for-byte
        self.assertIn('id: C-bb22cc33dd', rec['C-aa11bb22cc']['notes'])

    def test_set_scalar_lands_on_the_owning_claim(self) -> None:
        new, changed = confirm._set_scalar_on_claim(
            _QUOTED_ID_CLAIMS, 'C-bb22cc33dd', 'place', 'L-7c1a9f4e22')
        self.assertTrue(changed)
        rec = _parse(new)
        self.assertEqual(str(rec['C-bb22cc33dd']['place']), 'L-7c1a9f4e22')
        self.assertNotIn('place', rec['C-aa11bb22cc'])

    def test_set_scalar_insert_follows_the_items_own_id_line(self) -> None:
        # The owning claim itself quotes an id line ABOVE its real id: key;
        # the fallback insert-after-id must land after the item's own key,
        # never split the quoted evidence inside the block scalar.
        text = ('## Claims\n```yaml\n'
                '- value: "B"\n'
                '  notes: |\n'
                '    quoting its own id line:\n'
                '    id: C-bb22cc33dd\n'
                '  id: C-bb22cc33dd\n'
                '  type: death\n'
                '  persons: [P-aaaaaaaaaa]\n'
                '  status: suggested\n'
                '```\n')
        new, changed = confirm._set_scalar_on_claim(
            text, 'C-bb22cc33dd', 'place', 'L-7c1a9f4e22')
        self.assertTrue(changed)
        rec = _parse(new)
        self.assertEqual(str(rec['C-bb22cc33dd']['place']), 'L-7c1a9f4e22')
        self.assertIn('id: C-bb22cc33dd', rec['C-bb22cc33dd']['notes'])

    def test_quoted_only_id_is_a_clean_no_change(self) -> None:
        # The id appears ONLY inside scalar content - no claim owns it, so the
        # editors must report no-change (the callers turn that into not-found),
        # never write onto the quoting claim.
        text = ('## Claims\n```yaml\n'
                '- value: "Only quoting"\n'
                '  id: C-aa11bb22cc\n'
                '  status: accepted\n'
                '  notes: |\n'
                '    id: C-bb22cc33dd\n'
                '```\n')
        new, changed, already = confirm._add_link_to_claim(
            text, 'C-bb22cc33dd', 'corroborates', 'C-cc33dd44ee')
        self.assertFalse(changed)
        self.assertFalse(already)
        self.assertEqual(new, text)
        new2, changed2 = confirm._set_scalar_on_claim(
            text, 'C-bb22cc33dd', 'place', 'L-7c1a9f4e22')
        self.assertFalse(changed2)
        self.assertEqual(new2, text)

    def test_belt_refuses_when_ownership_and_parse_disagree(self) -> None:
        # Belt and braces: if line-level ownership ever picks a span whose
        # PARSED claim is not the target (an ownership bug), the edit must
        # refuse rather than land on the wrong claim.
        import unittest.mock as mock
        with mock.patch.object(confirm, '_own_id_key_line',
                               lambda lines, start, end, base: (start, 'C-bb22cc33dd')):
            with self.assertRaises(confirm._EditRefused):
                confirm._add_link_to_claim(
                    _CLAIMS, 'C-bb22cc33dd', 'corroborates', 'C-cc33dd44ee')


# ── Verb contracts against a copy of the example archive ────────────────────────

class ConfirmArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not EXAMPLE.is_dir():
            raise unittest.SkipTest('example-archive not present')
        import index
        cls.index = index

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / 'arc'
        shutil.copytree(EXAMPLE, self.root)
        self.config = load_fha_yaml(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _reindex(self) -> sqlite3.Connection:
        self.index.build_index(self.root, self.config)
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        conn.row_factory = sqlite3.Row
        return conn

    # xref ---------------------------------------------------------------------

    def test_xref_corroborates_round_trip(self) -> None:
        result = confirm.run_confirm_xref(
            self.root, claim_a=CLAIM_A, claim_b=CLAIM_B, relation='corroborates')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(result.changed), 2)   # two source files
        conn = self._reindex()
        try:
            links = conn.execute(
                "SELECT claim_id, target_id FROM claim_links WHERE rel='corroborates'").fetchall()
        finally:
            conn.close()
        pairs = {frozenset((r['claim_id'], r['target_id'])) for r in links}
        self.assertIn(frozenset((CLAIM_A.lower(), CLAIM_B.lower())), pairs)

    def test_xref_dry_run_writes_nothing(self) -> None:
        src = self.root / 'sources' / 'other' / 'hartley-family-notes_S-fa1234567b.md'
        before = src.read_text(encoding='utf-8')
        result = confirm.run_confirm_xref(
            self.root, claim_a=CLAIM_A, claim_b=CLAIM_B, relation='corroborates', dry_run=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.changed, [])
        self.assertEqual(src.read_text(encoding='utf-8'), before)

    def test_xref_rolls_back_on_second_write_failure(self) -> None:
        # The reciprocal pair lands in two source files; if the second write
        # fails, the first must be rolled back so no one-sided link survives.
        import unittest.mock as mock
        srcs = sorted((self.root / 'sources').rglob('*.md'))
        before = {p: p.read_text(encoding='utf-8') for p in srcs}
        # The surgical editors write byte-faithfully via write_text_exact, so the
        # failure is injected there (not Path.write_text).
        real_write = confirm.write_text_exact
        state = {'n': 0}

        def flaky(path, text):
            state['n'] += 1
            if state['n'] == 2:
                raise OSError('simulated disk full')
            return real_write(path, text)

        with mock.patch.object(confirm, 'write_text_exact', flaky):
            r = confirm.run_confirm_xref(
                self.root, claim_a=CLAIM_A, claim_b=CLAIM_B, relation='corroborates')
        self.assertEqual(r.exit_code, EXIT_FAILURE)
        self.assertEqual(r.changed, [])
        for p in srcs:
            self.assertEqual(p.read_text(encoding='utf-8'), before[p],
                             f'{p.name} not rolled back after a mid-write failure')

    def test_xref_contradiction_spawns_question_and_no_e009(self) -> None:
        import lint
        result = confirm.run_confirm_xref(
            self.root, claim_a=CLAIM_A, claim_b=CLAIM_B, relation='contradicts')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertTrue(result['question_spawned'])
        q = (self.root / 'notes' / 'questions.md').read_text(encoding='utf-8')
        self.assertIn(CLAIM_A, q)
        self.assertIn(CLAIM_B, q)
        self._reindex().close()
        lint_result = lint.run_lint(self.root, self.config)
        e009 = [m for m in lint_result.messages if m.code == 'E009']
        self.assertEqual(e009, [], f'E009 should be satisfied by the spawned question: {e009}')

    def test_xref_already_linked(self) -> None:
        confirm.run_confirm_xref(self.root, claim_a=CLAIM_A, claim_b=CLAIM_B, relation='corroborates')
        again = confirm.run_confirm_xref(self.root, claim_a=CLAIM_A, claim_b=CLAIM_B, relation='corroborates')
        self.assertEqual(again['status'], 'already')
        self.assertEqual(again.changed, [])

    def test_xref_invalid_and_same(self) -> None:
        bad = confirm.run_confirm_xref(self.root, claim_a='C-bad', claim_b=CLAIM_B, relation='corroborates')
        self.assertEqual(bad.exit_code, EXIT_FAILURE)
        self.assertEqual(bad['status'], 'invalid-id')
        same = confirm.run_confirm_xref(self.root, claim_a=CLAIM_A, claim_b=CLAIM_A, relation='corroborates')
        self.assertEqual(same['status'], 'same-claim')

    def test_xref_unknown_claim_not_found(self) -> None:
        r = confirm.run_confirm_xref(self.root, claim_a='C-0000000000', claim_b=CLAIM_B, relation='corroborates')
        self.assertEqual(r.exit_code, EXIT_WARNINGS)
        self.assertEqual(r['status'], 'not-found')

    def test_xref_guard_refusal_is_clean_and_writes_nothing(self) -> None:
        # Simulate an indent regression (derive column 4 for the archive's
        # column-2 items): the pre-write guard must surface as a clean refusal
        # from the planning pass - refusal exit code, no file touched.
        import unittest.mock as mock
        srcs = sorted((self.root / 'sources').rglob('*.md'))
        before = {p: p.read_text(encoding='utf-8') for p in srcs}
        with mock.patch.object(confirm, 'claim_item_key_indent',
                               lambda item, base: base + '    '):
            r = confirm.run_confirm_xref(
                self.root, claim_a=CLAIM_A, claim_b=CLAIM_B, relation='corroborates')
        self.assertEqual(r.exit_code, EXIT_FAILURE)
        self.assertEqual(r['status'], 'refused')
        self.assertEqual(r.changed, [])
        text = ' '.join(m.text for m in r.messages)
        self.assertNotIn('Traceback', text)
        for p in srcs:
            self.assertEqual(p.read_text(encoding='utf-8'), before[p])

    # cooccur ------------------------------------------------------------------

    def test_cooccur_suggested_not_an_edge(self) -> None:
        result = confirm.run_confirm_cooccur(
            self.root, person_a=PERSON_1, person_b=PERSON_3, source_id=SOURCE, subtype='associate')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result['claim_status'], 'suggested')
        cid = result['claim_id'].lower()
        conn = self._reindex()
        try:
            claim = conn.execute('SELECT status FROM claims WHERE id=?', (cid,)).fetchone()
            # Scope to the suggested pair: the example archive legitimately carries
            # other (accepted) associate edges - e.g. the lodge-roster FAN-club tie -
            # so assert only that THIS suggested claim derived no edge of its own.
            edges = {
                frozenset((r['person_id'], r['other_id']))
                for r in conn.execute(
                    "SELECT person_id, other_id FROM relationships WHERE rel='associate'")
            }
        finally:
            conn.close()
        self.assertEqual(claim['status'], 'suggested')
        self.assertNotIn(frozenset((PERSON_1.lower(), PERSON_3.lower())), edges)

    def test_cooccur_accept_derives_edge(self) -> None:
        result = confirm.run_confirm_cooccur(
            self.root, person_a=PERSON_1, person_b=PERSON_2, source_id=SOURCE,
            subtype='friend', accept=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result['claim_status'], 'accepted')
        conn = self._reindex()
        try:
            edges = {
                frozenset((r['person_id'], r['other_id']))
                for r in conn.execute("SELECT person_id, other_id FROM relationships WHERE rel='friend'")
            }
        finally:
            conn.close()
        self.assertIn(frozenset((PERSON_1.lower(), PERSON_2.lower())), edges)

    def test_cooccur_dry_run_writes_nothing(self) -> None:
        src = self.root / 'sources' / 'other' / 'bradford-family-genealogy-notes_S-fc3456789d.md'
        before = src.read_text(encoding='utf-8')
        result = confirm.run_confirm_cooccur(
            self.root, person_a=PERSON_1, person_b=PERSON_2, source_id=SOURCE,
            subtype='friend', dry_run=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.changed, [])
        self.assertEqual(src.read_text(encoding='utf-8'), before)

    def test_cooccur_bad_source_not_found(self) -> None:
        r = confirm.run_confirm_cooccur(
            self.root, person_a=PERSON_1, person_b=PERSON_2, source_id='S-0000000000', subtype='friend')
        self.assertEqual(r.exit_code, EXIT_WARNINGS)
        self.assertEqual(r['status'], 'not-found')

    def test_cooccur_invalid_subtype(self) -> None:
        r = confirm.run_confirm_cooccur(
            self.root, person_a=PERSON_1, person_b=PERSON_2, source_id=SOURCE, subtype='cousin')
        self.assertEqual(r.exit_code, EXIT_FAILURE)
        self.assertEqual(r['status'], 'invalid-subtype')

    def test_cooccur_unknown_person_not_minted(self) -> None:
        # A syntactically valid but nonexistent P-id must abort before minting,
        # so the write never leaves an E005 missing-person reference behind.
        src_before = (self.root / 'sources' / 'other'
                      / 'bradford-family-genealogy-notes_S-fc3456789d.md').read_text(encoding='utf-8')
        r = confirm.run_confirm_cooccur(
            self.root, person_a=PERSON_1, person_b='P-0000000000', source_id=SOURCE, subtype='friend')
        self.assertEqual(r.exit_code, EXIT_WARNINGS)
        self.assertEqual(r['status'], 'not-found')
        self.assertEqual(r.changed, [])
        self.assertEqual(
            (self.root / 'sources' / 'other'
             / 'bradford-family-genealogy-notes_S-fc3456789d.md').read_text(encoding='utf-8'),
            src_before)

    def test_cooccur_repeat_confirm_is_already_not_duplicate(self) -> None:
        # Idempotency: `fha cooccur` keeps re-proposing a pair while its claim
        # sits suggested, so the same confirm is easy to run twice - the second
        # run must report `already` and write nothing, not mint a duplicate.
        first = confirm.run_confirm_cooccur(
            self.root, person_a=PERSON_1, person_b=PERSON_2, source_id=SOURCE, subtype='friend')
        self.assertEqual(first.exit_code, EXIT_CLEAN)
        self.assertEqual(first['status'], 'ok')
        src = Path(first['source'])
        after_first = src.read_text(encoding='utf-8')

        again = confirm.run_confirm_cooccur(
            self.root, person_a=PERSON_1, person_b=PERSON_2, source_id=SOURCE, subtype='friend')
        self.assertEqual(again['status'], 'already')
        self.assertEqual(again.exit_code, EXIT_CLEAN)
        self.assertEqual(again.changed, [])
        self.assertEqual(again['claim_id'], first['claim_id'])
        self.assertEqual(again['claim_status'], 'suggested')
        self.assertEqual(src.read_text(encoding='utf-8'), after_first)

        # Order of the P-ids must not matter - the pair is unordered.
        swapped = confirm.run_confirm_cooccur(
            self.root, person_a=PERSON_2, person_b=PERSON_1, source_id=SOURCE, subtype='friend')
        self.assertEqual(swapped['status'], 'already')
        self.assertEqual(src.read_text(encoding='utf-8'), after_first)

        # Exactly one claim covers the pair + subtype in the record.
        pair = {PERSON_1.lower(), PERSON_2.lower()}
        matches = [
            c for c in read_record(src)['claims']
            if c.get('type') == 'relationship' and c.get('subtype') == 'friend'
            and pair <= {str(p).lower() for p in (c.get('persons') or [])}
        ]
        self.assertEqual(len(matches), 1)

    def test_cooccur_accept_promotes_existing_suggested(self) -> None:
        # A first confirm without --accept mints a `suggested` claim; a second
        # run WITH --accept must promote that same claim to `accepted` (honoring
        # the flag), not report `already` and drop the accept nor mint a second.
        first = confirm.run_confirm_cooccur(
            self.root, person_a=PERSON_1, person_b=PERSON_2, source_id=SOURCE, subtype='friend')
        self.assertEqual(first['claim_status'], 'suggested')
        src = Path(first['source'])

        promoted = confirm.run_confirm_cooccur(
            self.root, person_a=PERSON_1, person_b=PERSON_2, source_id=SOURCE,
            subtype='friend', accept=True)
        self.assertEqual(promoted.exit_code, EXIT_CLEAN)
        self.assertEqual(promoted['status'], 'accepted')
        self.assertEqual(promoted['claim_id'], first['claim_id'])
        self.assertEqual(promoted['claim_status'], 'accepted')

        # The same claim is now accepted (no duplicate), so the edge derives.
        target = normalize_id(first['claim_id'])
        matches = [c for c in read_record(src)['claims'] if normalize_id(str(c.get('id'))) == target]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].get('status'), 'accepted')
        conn = self._reindex()
        try:
            edges = {
                frozenset((r['person_id'], r['other_id']))
                for r in conn.execute("SELECT person_id, other_id FROM relationships WHERE rel='friend'")
            }
        finally:
            conn.close()
        self.assertIn(frozenset((PERSON_1.lower(), PERSON_2.lower())), edges)

    def test_cooccur_accept_promote_dry_run_writes_nothing(self) -> None:
        first = confirm.run_confirm_cooccur(
            self.root, person_a=PERSON_1, person_b=PERSON_2, source_id=SOURCE, subtype='friend')
        src = Path(first['source'])
        before = src.read_text(encoding='utf-8')
        preview = confirm.run_confirm_cooccur(
            self.root, person_a=PERSON_1, person_b=PERSON_2, source_id=SOURCE,
            subtype='friend', accept=True, dry_run=True)
        self.assertEqual(preview['status'], 'accepted')
        self.assertEqual(preview.changed, [])
        self.assertEqual(src.read_text(encoding='utf-8'), before)

    def test_cooccur_rejected_claim_does_not_block_fresh_confirm(self) -> None:
        # A dead claim (rejected/superseded) is not a live edge; a human who
        # rejected one bad claim may later confirm the same pair for real.
        first = confirm.run_confirm_cooccur(
            self.root, person_a=PERSON_1, person_b=PERSON_2, source_id=SOURCE, subtype='friend')
        self.assertEqual(first['status'], 'ok')
        src = Path(first['source'])
        text = src.read_text(encoding='utf-8')
        head, sep, tail = text.partition(f'id: {first["claim_id"]}')
        self.assertTrue(sep, 'minted claim id not found in the source record')
        tail = tail.replace('status: suggested', 'status: rejected', 1)
        src.write_text(head + sep + tail, encoding='utf-8')

        fresh = confirm.run_confirm_cooccur(
            self.root, person_a=PERSON_1, person_b=PERSON_2, source_id=SOURCE, subtype='friend')
        self.assertEqual(fresh['status'], 'ok')
        self.assertEqual(fresh.exit_code, EXIT_CLEAN)
        self.assertNotEqual(fresh['claim_id'], first['claim_id'])

        pair = {PERSON_1.lower(), PERSON_2.lower()}
        matches = [
            c for c in read_record(src)['claims']
            if c.get('type') == 'relationship' and c.get('subtype') == 'friend'
            and pair <= {str(p).lower() for p in (c.get('persons') or [])}
        ]
        self.assertEqual(sorted(str(c.get('status')) for c in matches),
                         ['rejected', 'suggested'])

    def test_cooccur_different_subtype_not_blocked(self) -> None:
        # The dedup is scoped to the SAME subtype: a friend claim must not
        # swallow a neighbor confirm for the same pair.
        confirm.run_confirm_cooccur(
            self.root, person_a=PERSON_1, person_b=PERSON_2, source_id=SOURCE, subtype='friend')
        other = confirm.run_confirm_cooccur(
            self.root, person_a=PERSON_1, person_b=PERSON_2, source_id=SOURCE, subtype='neighbor')
        self.assertEqual(other['status'], 'ok')
        self.assertEqual(other.exit_code, EXIT_CLEAN)

    def _seed_pair_claim(self, persons_yaml: str) -> Path:
        """Append a hand-written friend claim for PERSON_1/PERSON_2 into the
        SOURCE record's ## Claims fence, `persons:` spelled as given - the
        wikilink/nested forms the quickstart teaches and the R2 review found
        the idempotency gate blind to (round-2 finding 6)."""
        src = (self.root / 'sources' / 'other'
               / 'bradford-family-genealogy-notes_S-fc3456789d.md')
        text = src.read_text(encoding='utf-8')
        lines = text.splitlines()
        _open_f, close_f = confirm._find_claims_block(lines)
        lines[close_f:close_f] = [
            '',
            '- value: "Sam and Ken: friend (hand-noted)"',
            '  id: C-ee00000005',
            '  type: relationship',
            '  subtype: friend',
            f'  persons: {persons_yaml}',
            '  status: suggested',
        ]
        src.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return src

    def test_cooccur_wikilink_persons_report_already(self) -> None:
        src = self._seed_pair_claim('["[[P-4d5e6f7g8h|Sam]]", "[[P-6f7g8h9jka]]"]')
        before = src.read_text(encoding='utf-8')
        r = confirm.run_confirm_cooccur(
            self.root, person_a=PERSON_1, person_b=PERSON_2, source_id=SOURCE, subtype='friend')
        self.assertEqual(r['status'], 'already')
        self.assertEqual(r.exit_code, EXIT_CLEAN)
        self.assertEqual(r.changed, [])
        self.assertEqual(r['claim_id'], 'C-ee00000005')
        self.assertEqual(src.read_text(encoding='utf-8'), before)

    def test_cooccur_nested_persons_report_already(self) -> None:
        # An UNquoted [[P-x]] parses as a nested YAML list; the gate must read
        # it the way every other link-field consumer does.
        src = self._seed_pair_claim('[[P-4d5e6f7g8h], [P-6f7g8h9jka]]')
        before = src.read_text(encoding='utf-8')
        r = confirm.run_confirm_cooccur(
            self.root, person_a=PERSON_1, person_b=PERSON_2, source_id=SOURCE, subtype='friend')
        self.assertEqual(r['status'], 'already')
        self.assertEqual(r.changed, [])
        self.assertEqual(src.read_text(encoding='utf-8'), before)

    def test_place_rejects_name_and_into_together(self) -> None:
        r = confirm.run_confirm_place(
            self.root, claim_ids=[CLAIM_B], name='Marsh Creek', into='L-7c1a9f4e22')
        self.assertEqual(r.exit_code, EXIT_FAILURE)
        self.assertEqual(r['status'], 'failed')

    # dismiss ------------------------------------------------------------------

    def test_dismiss_excludes_from_next_cooccur(self) -> None:
        # PERSON_1/PERSON_3 co-occur in the example; dismiss removes them.
        self._reindex().close()   # run_cooccur reads .cache/index.sqlite
        before = cooccur.run_cooccur(self.root)
        present = any(
            frozenset((c['person_a'], c['person_b'])) == frozenset((PERSON_1.lower(), PERSON_3.lower()))
            for c in before['person_pairs'])
        self.assertTrue(present, 'expected the example pair to co-occur before dismissal')

        result = confirm.run_dismiss(self.root, person_a=PERSON_1, person_b=PERSON_3)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        data = json.loads((self.root / '.cache' / 'cooccur_dismissed.json').read_text(encoding='utf-8'))
        self.assertIn([PERSON_1.lower(), PERSON_3.lower()], data['pairs'])

        after = cooccur.run_cooccur(self.root)
        still = any(
            frozenset((c['person_a'], c['person_b'])) == frozenset((PERSON_1.lower(), PERSON_3.lower()))
            for c in after['person_pairs'])
        self.assertFalse(still, 'dismissed pair must not be re-proposed')

    def test_dismiss_dry_run_writes_nothing(self) -> None:
        result = confirm.run_dismiss(self.root, person_a=PERSON_1, person_b=PERSON_3, dry_run=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertFalse((self.root / '.cache' / 'cooccur_dismissed.json').exists())

    def test_dismiss_already(self) -> None:
        confirm.run_dismiss(self.root, person_a=PERSON_1, person_b=PERSON_3)
        again = confirm.run_dismiss(self.root, person_a=PERSON_1, person_b=PERSON_3)
        self.assertEqual(again['status'], 'already')

    # place --------------------------------------------------------------------

    def test_place_mint_and_relink(self) -> None:
        result = confirm.run_confirm_place(
            self.root, claim_ids=[CLAIM_B], name='Marsh Creek', hierarchy='Marsh Creek, Kansas, USA')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        lid = result['place_id']
        self.assertIn(str(self.root / 'places' / 'places.yaml'),
                      [str(p) for p in result.changed])
        # the new place is registered and the claim relinked
        yaml_text = (self.root / 'places' / 'places.yaml').read_text(encoding='utf-8')
        self.assertIn(lid, yaml_text)
        conn = self._reindex()
        try:
            row = conn.execute('SELECT place_id FROM claims WHERE id=?', (CLAIM_B.lower(),)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row['place_id'], lid.lower())

    def test_place_relinks_all_claims_in_one_source_file(self) -> None:
        # Regression: two claims that live in the SAME source record must both
        # keep their relink. Building each preview from the pristine file text
        # and then writing them one after another let the second write clobber
        # the first relink while still reporting both C-ids as relinked.
        c1, c2 = 'C-fc0000001a', 'C-fc0000002b'   # both in S-fc3456789d
        result = confirm.run_confirm_place(
            self.root, claim_ids=[c1, c2], name='Marsh Creek', hierarchy='Marsh Creek, Kansas, USA')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result['relinked'], [c1, c2])
        lid = result['place_id']
        conn = self._reindex()
        try:
            placed = {
                cid: conn.execute(
                    'SELECT place_id FROM claims WHERE id=?', (cid.lower(),)).fetchone()['place_id']
                for cid in (c1, c2)
            }
        finally:
            conn.close()
        self.assertEqual(placed[c1], lid.lower())
        self.assertEqual(placed[c2], lid.lower(),
                         'second claim in the same source file lost its relink')

    def test_place_into_existing(self) -> None:
        result = confirm.run_confirm_place(
            self.root, claim_ids=[CLAIM_B], into='L-7c1a9f4e22')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result['into'], 'L-7c1a9f4e22')
        self.assertEqual(result['relinked'], [CLAIM_B])

    def test_place_requires_name_or_into(self) -> None:
        r = confirm.run_confirm_place(self.root, claim_ids=[CLAIM_B])
        self.assertEqual(r.exit_code, EXIT_FAILURE)

    def test_place_unknown_claim(self) -> None:
        r = confirm.run_confirm_place(self.root, claim_ids=['C-0000000000'], name='X')
        self.assertEqual(r.exit_code, EXIT_WARNINGS)
        self.assertEqual(r['status'], 'not-found')

    def test_place_guard_refusal_is_clean_and_writes_nothing(self) -> None:
        # Same simulated indent regression as the xref twin: the refusal must
        # land in the planning pass, before the registry write, leaving both
        # places.yaml and every source file byte-identical.
        import unittest.mock as mock
        srcs = sorted((self.root / 'sources').rglob('*.md'))
        before = {p: p.read_text(encoding='utf-8') for p in srcs}
        places_yaml = self.root / 'places' / 'places.yaml'
        places_before = places_yaml.read_text(encoding='utf-8')
        with mock.patch.object(confirm, 'claim_item_key_indent',
                               lambda item, base: base + '    '):
            r = confirm.run_confirm_place(self.root, claim_ids=[CLAIM_B], name='Marsh Creek')
        self.assertEqual(r.exit_code, EXIT_FAILURE)
        self.assertEqual(r['status'], 'refused')
        self.assertEqual(r.changed, [])
        text = ' '.join(m.text for m in r.messages)
        self.assertNotIn('Traceback', text)
        self.assertEqual(places_yaml.read_text(encoding='utf-8'), places_before)
        for p in srcs:
            self.assertEqual(p.read_text(encoding='utf-8'), before[p])

    # discovery ----------------------------------------------------------------

    def test_discovery_appends(self) -> None:
        result = confirm.run_add_discovery(
            self.root, text='Found the marriage record', refs=[SOURCE, PERSON_1])
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        text = (self.root / 'notes' / 'discoveries.md').read_text(encoding='utf-8')
        self.assertIn('Found the marriage record', text)
        self.assertIn(f'[{SOURCE}]', text)
        self.assertIn(f'[{PERSON_1}]', text)

    def test_discovery_dry_run_writes_nothing(self) -> None:
        before = (self.root / 'notes' / 'discoveries.md').read_text(encoding='utf-8')
        result = confirm.run_add_discovery(self.root, text='Nope', dry_run=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual((self.root / 'notes' / 'discoveries.md').read_text(encoding='utf-8'), before)

    def test_discovery_bad_ref(self) -> None:
        r = confirm.run_add_discovery(self.root, text='x', refs=['not-an-id'])
        self.assertEqual(r.exit_code, EXIT_FAILURE)
        self.assertEqual(r['status'], 'invalid-id')

    def test_discovery_unknown_ref_not_appended(self) -> None:
        # A valid-shaped but nonexistent ref must abort before writing so the
        # log never carries an E004 orphan reference.
        before = (self.root / 'notes' / 'discoveries.md').read_text(encoding='utf-8')
        r = confirm.run_add_discovery(self.root, text='x', refs=[SOURCE, 'S-0000000000'])
        self.assertEqual(r.exit_code, EXIT_WARNINGS)
        self.assertEqual(r['status'], 'not-found')
        self.assertEqual(r.changed, [])
        self.assertEqual((self.root / 'notes' / 'discoveries.md').read_text(encoding='utf-8'), before)

    def test_discovery_setup_failure_is_reported(self) -> None:
        # notes/ blocked by a file of the same name → a clean failure Result,
        # not a traceback out of run_add_discovery.
        import shutil as _shutil
        _shutil.rmtree(self.root / 'notes')
        (self.root / 'notes').write_text('not a dir', encoding='utf-8')
        r = confirm.run_add_discovery(self.root, text='x')
        self.assertEqual(r.exit_code, EXIT_FAILURE)
        self.assertEqual(r['status'], 'failed')

    # draft --------------------------------------------------------------------

    def _seed_draft_marker(self) -> None:
        """Append a fresh AI-DRAFT block to the DRAFT_PERSON profile copy.

        The example archive's demo draft was accepted by the owner
        (`fha confirm draft P-2b3c4d5e6f`, 2026-07-03), so the fixture now
        carries an AI-ACCEPTED marker. These tests seed their own draft so
        coverage never depends on the fixture holding an unaccepted draft -
        and the pre-existing AI-ACCEPTED marker doubles as proof the flip
        ignores already-accepted blocks (count stays 1).
        """
        kinds = ('_research_', '_timeline_', '_sources-index_', '_draft-queue_')
        profile = next(
            p for p in (self.root / 'people').rglob(f'*_{DRAFT_PERSON}.md')
            if not any(k in p.name for k in kinds)
        )
        text = profile.read_text(encoding='utf-8')
        profile.write_text(
            text
            + '\nSeeded draft paragraph for this test.\n\n'
            + '<!-- AI-DRAFT 2026-07-04 test-model - seeded by test_confirm -->\n',
            encoding='utf-8',
        )

    def test_draft_accept_flips_marker(self) -> None:
        self._seed_draft_marker()
        result = confirm.run_accept_draft(self.root, person_id=DRAFT_PERSON)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result['count'], 1)
        profile = Path(result['profile']).read_text(encoding='utf-8')
        self.assertIn('AI-ACCEPTED', profile)
        self.assertNotIn('AI-DRAFT', profile)

    def test_draft_dry_run_writes_nothing(self) -> None:
        self._seed_draft_marker()
        result = confirm.run_accept_draft(self.root, person_id=DRAFT_PERSON, dry_run=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        profile = Path(result['profile']).read_text(encoding='utf-8')
        self.assertIn('AI-DRAFT', profile)

    def test_draft_no_marker_warns(self) -> None:
        # PERSON_3 is a stub-ish profile without an AI-DRAFT marker.
        result = confirm.run_accept_draft(self.root, person_id=PERSON_3)
        self.assertIn(result['status'], ('none', 'not-found'))
        self.assertNotEqual(result.exit_code, EXIT_CLEAN)


# ── The cooccur idempotency gate reads every taught persons: form ────────────────

class ExistingPairClaimFormTests(unittest.TestCase):
    """`persons:` in hand-written claims carries every taught link form; the
    gate compared `normalize_id(str(p))` raw, so wikilink and nested forms
    never blocked a re-mint (round-2 finding 6). A plain name still cannot
    block without an alias map - that boundary is documented here too."""

    PA, PB = 'p-aaaaaaaaaa', 'p-bbbbbbbbbb'

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _src(self, persons_yaml: str, status: str = 'suggested') -> Path:
        text = ('---\n'
                'id: S-2222222222\n'
                'title: Pair test notes\n'
                'source_type: other\n'
                'source_class: derivative\n'
                'citation: >\n  A fictional citation.\n'
                'created: 2026-07-01\n'
                '---\n\n'
                '## Claims\n```yaml\n'
                '- value: "pair"\n'
                '  id: C-aa11bb22cc\n'
                '  type: relationship\n'
                '  subtype: friend\n'
                f'  persons: {persons_yaml}\n'
                f'  status: {status}\n'
                '```\n')
        path = self.dir / 'pair-notes_S-2222222222.md'
        path.write_text(text, encoding='utf-8')
        return path

    def test_wikilink_with_display_blocks(self) -> None:
        p = self._src('["[[P-aaaaaaaaaa|Sam]]", "[[P-bbbbbbbbbb]]"]')
        self.assertIsNotNone(confirm._existing_pair_claim(p, self.PA, self.PB, 'friend'))

    def test_nested_unquoted_wikilinks_block(self) -> None:
        p = self._src('[[P-aaaaaaaaaa], [P-bbbbbbbbbb]]')
        self.assertIsNotNone(confirm._existing_pair_claim(p, self.PA, self.PB, 'friend'))

    def test_bare_ids_still_block(self) -> None:
        p = self._src('[P-aaaaaaaaaa, P-bbbbbbbbbb]')
        self.assertIsNotNone(confirm._existing_pair_claim(p, self.PA, self.PB, 'friend'))

    def test_rejected_wikilink_claim_does_not_block(self) -> None:
        p = self._src('["[[P-aaaaaaaaaa]]", "[[P-bbbbbbbbbb]]"]', status='rejected')
        self.assertIsNone(confirm._existing_pair_claim(p, self.PA, self.PB, 'friend'))

    def test_name_only_persons_do_not_block(self) -> None:
        # Without an alias map a plain name cannot resolve to a P-id; failing
        # toward a visible duplicate beats silently skipping a legitimate mint.
        p = self._src('["[[Sam Rivera]]", "[[Ken Smith]]"]')
        self.assertIsNone(confirm._existing_pair_claim(p, self.PA, self.PB, 'friend'))


# ── Quoted id lines, end to end (round-2 finding 2) ─────────────────────────────

_M4_SOURCE_ONE = '''---
id: S-m400000001
title: Quoted-id notes
source_type: other
source_class: derivative
citation: >
  A fictional citation.
people: [P-aaaaaaaaaa]
created: 2026-07-01
---

## Claims
```yaml
- value: "Claim A - the decoy"
  id: C-aa00000001
  type: residence
  persons: [P-aaaaaaaaaa]
  status: accepted
  reviewed: 2026-01-01
  notes: |
    Compare with the other claim:
    id: C-bb00000002
    which covers the same event.

- value: "Claim B - the real target"
  id: C-bb00000002
  type: occupation
  persons: [P-aaaaaaaaaa]
  status: suggested
```
'''

_M4_SOURCE_TWO = '''---
id: S-m400000002
title: Far-end notes
source_type: other
source_class: derivative
citation: >
  A fictional citation.
people: [P-aaaaaaaaaa]
created: 2026-07-01
---

## Claims
```yaml
- value: "Claim D - the far end"
  id: C-dd00000004
  type: occupation
  persons: [P-aaaaaaaaaa]
  status: suggested
```
'''


class QuotedIdArchiveTests(unittest.TestCase):
    """The exact round-2 M4 repro: claim A's `notes: |` quotes claim B's id
    line; xref/place aimed at B used to edit A under a success message (and
    the reciprocal file linked to B, leaving an asymmetric wrong link)."""

    DECOY, TARGET, FAR = 'C-aa00000001', 'C-bb00000002', 'C-dd00000004'

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        other = self.root / 'sources' / 'other'
        other.mkdir(parents=True)
        self.src_one = other / 'quoted-id-notes_S-m400000001.md'
        self.src_one.write_text(_M4_SOURCE_ONE, encoding='utf-8')
        self.src_two = other / 'far-end-notes_S-m400000002.md'
        self.src_two.write_text(_M4_SOURCE_TWO, encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _claims(self, path: Path) -> dict:
        return {c['id']: c for c in read_record(path)['claims']
                if isinstance(c, dict) and c.get('id')}

    def test_xref_links_the_owning_claim_symmetrically(self) -> None:
        r = confirm.run_confirm_xref(
            self.root, claim_a=self.TARGET, claim_b=self.FAR, relation='corroborates')
        self.assertEqual(r.exit_code, EXIT_CLEAN)
        one, two = self._claims(self.src_one), self._claims(self.src_two)
        self.assertEqual(one[self.TARGET]['corroborates'], [self.FAR])
        self.assertNotIn('corroborates', one[self.DECOY])
        self.assertEqual(two[self.FAR]['corroborates'], [self.TARGET])
        # the quoted evidence inside the decoy's notes is untouched
        self.assertIn(f'id: {self.TARGET}', one[self.DECOY]['notes'])

    def test_place_relinks_the_owning_claim(self) -> None:
        r = confirm.run_confirm_place(
            self.root, claim_ids=[self.TARGET], name='Marsh Creek')
        self.assertEqual(r.exit_code, EXIT_CLEAN)
        one = self._claims(self.src_one)
        self.assertEqual(str(one[self.TARGET]['place']), r['place_id'])
        self.assertNotIn('place', one[self.DECOY])

    def test_quoted_only_id_is_clean_not_found(self) -> None:
        # Rewrite the fixture so the quoted id names a claim that exists
        # NOWHERE - the confirm must be a clean not-found, never a write.
        ghost = 'C-cc00000003'
        self.src_one.write_text(
            _M4_SOURCE_ONE.replace(f'id: {self.TARGET}\n    which covers',
                                   f'id: {ghost}\n    which covers'),
            encoding='utf-8')
        before = self.src_one.read_text(encoding='utf-8')
        r = confirm.run_confirm_xref(
            self.root, claim_a=ghost, claim_b=self.FAR, relation='corroborates')
        self.assertEqual(r.exit_code, EXIT_WARNINGS)
        self.assertEqual(r['status'], 'not-found')
        self.assertEqual(r.changed, [])
        self.assertEqual(self.src_one.read_text(encoding='utf-8'), before)


# ── Duplicate claim ids refuse with the E001 repair path (round-2 finding 15) ───

_DUP_SOURCE = '''---
id: S-f150000001
title: Duplicate-id notes
source_type: other
source_class: derivative
citation: >
  A fictional citation.
people: [P-aaaaaaaaaa]
created: 2026-07-01
---

## Claims
```yaml
- value: "First twin"
  id: C-aa00000001
  type: occupation
  persons: [P-aaaaaaaaaa]
  status: suggested

- value: "Second twin"
  id: C-aa00000001
  type: occupation
  persons: [P-aaaaaaaaaa]
  status: suggested
```
'''


class DuplicateIdRefusalTests(unittest.TestCase):
    """A pre-existing duplicate C-id (lint E001) must refuse with the repair
    that actually helps - name E001 and `fha id mint C` - instead of the
    corruption wording ("would hide every claim"), which is false for this
    case and closes the repair path with wrong advice (round-2 finding 15).
    The corruption wording stays for real corrupting rewrites."""

    DUP, FAR = 'C-aa00000001', 'C-dd00000004'

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        other = self.root / 'sources' / 'other'
        other.mkdir(parents=True)
        self.dup_src = other / 'dup-notes_S-f150000001.md'
        self.dup_src.write_text(_DUP_SOURCE, encoding='utf-8')
        self.far_src = other / 'far-end-notes_S-m400000002.md'
        self.far_src.write_text(_M4_SOURCE_TWO, encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _messages(self, result) -> str:
        return ' '.join(m.text for m in result.messages)

    def test_xref_duplicate_refusal_names_e001_and_mint(self) -> None:
        before = self.dup_src.read_text(encoding='utf-8')
        r = confirm.run_confirm_xref(
            self.root, claim_a=self.DUP, claim_b=self.FAR, relation='corroborates')
        self.assertEqual(r.exit_code, EXIT_FAILURE)
        self.assertEqual(r['status'], 'refused')
        self.assertEqual(r.changed, [])
        msg = self._messages(r)
        self.assertIn('E001', msg)
        self.assertIn('fha id mint C', msg)
        self.assertNotIn('hide every claim', msg)
        self.assertEqual(self.dup_src.read_text(encoding='utf-8'), before)

    def test_place_duplicate_refusal_names_e001_and_mint(self) -> None:
        r = confirm.run_confirm_place(
            self.root, claim_ids=[self.DUP], name='Marsh Creek')
        self.assertEqual(r.exit_code, EXIT_FAILURE)
        self.assertEqual(r['status'], 'refused')
        msg = self._messages(r)
        self.assertIn('E001', msg)
        self.assertIn('fha id mint C', msg)
        self.assertNotIn('hide every claim', msg)
        # refused in the planning pass - the registry was never touched
        self.assertFalse((self.root / 'places' / 'places.yaml').exists())

    def test_corruption_refusal_keeps_hide_wording(self) -> None:
        # A rewrite the edit itself would corrupt (simulated indent
        # regression) keeps the original warning - that case really would
        # hide every claim in the file, and it is not an E001 problem.
        import unittest.mock as mock
        with mock.patch.object(confirm, 'claim_item_key_indent',
                               lambda item, base: base + '    '):
            r = confirm.run_confirm_xref(
                self.root, claim_a=self.FAR, claim_b=self.DUP, relation='corroborates')
        self.assertEqual(r['status'], 'refused')
        msg = self._messages(r)
        self.assertIn('hide every claim', msg)
        self.assertNotIn('E001', msg)


if __name__ == '__main__':
    unittest.main()
