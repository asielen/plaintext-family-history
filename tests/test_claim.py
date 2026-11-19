"""
test_claim.py - fha claim: human-directed claim review write-back.

Covers the surgical `## Claims` edit (`_apply_claim_review`), the `run_claim`
contract (accept/dispute/reject/park-at-suggested round-trip, default-today on accept,
dry-run writes nothing, malformed/unknown C-id), and an end-to-end check that
`fha index` + `fha lint` reflect a status change on a real archive (an accepted
vital relieves the right W101).

Also covers the P1 indent regression: claim items validly written with a wider
dash-to-key spacing (`-   value:` with keys at column 4) must be edited at their
own column, and the pre-write re-parse guard (`_lib.claims_edit_problem`) must
turn any block-corrupting rewrite into a clean refusal with nothing written.

Round-2 regressions covered here too: an `id: C-...` line quoted inside a block
scalar must never draw the review edit onto the quoting claim (finding 2 - the
old shape-only span match made `fha claim` refuse a perfectly reviewable
claim), and a pre-existing duplicate claim id refuses with the E001 repair
path, not the "would hide every claim" corruption wording (finding 15).

Batch status moves (TOOLING §3b amendment, 2026-07) are covered by
RunClaimBatchTests and ClaimBatchCliRoutingTests: several C-ids move status
together (any of the five review statuses), the batch is status-only (a field
flag with more than one id refuses with nothing written), validation is
all-or-nothing (one unknown id refuses the whole batch before any write),
one --dry-run previews every edit, and duplicate ids are deduped with a note.
"""

import contextlib
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import _lib as _lib_mod  # noqa: E402 - #225 lookup-speed tests count its parses
import claim
from _lib import (
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    claims_edit_problem,
    is_valid_edtf,
    load_fha_yaml,
    read_record,
    render_stub_content,
    yaml_edtf,
)

EXAMPLE = ROOT / 'example-archive'


_CLAIM_BLOCK = '''---
id: S-1111111111
title: Test source
source_type: other
source_class: original
repository: example collection
citation: >
  A fictional citation.
people: [P-aaaaaaaaaa]
created: 2026-06-01
---

## Claims
```yaml
- value: "Anna Smith born 1880, Fairview"  # inline comment kept
  id: C-aa11bb22cc
  type: birth
  persons: [P-aaaaaaaaaa]
  date: 1880
  status: suggested
  confidence: high

- value: "Anna Smith died 1950"
  id: C-bb22cc33dd
  type: death
  persons: [P-aaaaaaaaaa]
  status: suggested
  confidence: medium
```

## Notes
*(none yet)*
'''


def _pids(persons) -> list:
    """A claim's persons: as lowercase IDs, whatever written form (a bare ID
    or the `[[p-…|Name]]` link the tools write, SPEC §8.7)."""
    from _lib import link_field_refs
    return [r.lower() for r in link_field_refs(persons)]


def _write_source(archive_root: Path) -> Path:
    path = archive_root / 'sources' / 'other' / 'test-source_S-1111111111.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_CLAIM_BLOCK, encoding='utf-8')
    return path


def _write_person(archive_root: Path, pid: str, name: str) -> Path:
    """A minimal stub person record `claim new`'s --persons check can resolve."""
    slug = name.lower().replace(' ', '_')
    path = archive_root / 'people' / 'stubs' / f'{slug}__{pid}.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'---\nid: {pid}\nname: {name}\nliving: false\ntier: stub\ncreated: 2026-07-01\n---\n',
        encoding='utf-8',
    )
    return path


def _indented_block(pad: int, *, place: str | None = None, place_text: str | None = None) -> str:
    """One claim item at a given dash-to-key spacing (SPEC-legal, see
    KeyIndentVariantTests) - used to round-trip the new type/place/persons
    fields at both the 2-space (pad=1) and 4-space (pad=3) conventions."""
    dash = '-' + ' ' * pad
    ki = ' ' * (1 + pad)
    lines = [
        '## Claims', '```yaml',
        f'{dash}value: "Born 1880"',
        f'{ki}id: C-aa11bb22cc',
        f'{ki}type: birth',
        f'{ki}persons: [P-aaaaaaaaaa]',
    ]
    if place is not None:
        lines.append(f'{ki}place: {place}')
    if place_text is not None:
        lines.append(f'{ki}place_text: "{place_text}"')
    lines.append(f'{ki}status: suggested')
    lines.append('```')
    return '\n'.join(lines) + '\n'


def _notes_repro_block(pad: int, *, place: str | None = None,
                       place_text: str | None = None) -> str:
    """A claim item with a place/place_text key sitting ABOVE a trailing
    `notes: |` block - the exact 2026-07 stale-anchor repro shape - at a given
    dash-to-key spacing (pad=1 -> 2-space, pad=3 -> 4-space). The two
    continuation lines are indented deeper than `notes:`, as YAML requires."""
    dash = '-' + ' ' * pad
    ki = ' ' * (1 + pad)
    cont = ki + '  '            # block-scalar continuation, deeper than notes:
    lines = [
        '## Claims', '```yaml',
        f'{dash}value: "Anna Smith born 1880"',
        f'{ki}id: C-aa11bb22cc',
        f'{ki}type: birth',
        f'{ki}persons: [P-aaaaaaaaaa]',
    ]
    if place is not None:
        lines.append(f'{ki}place: {place}')
    if place_text is not None:
        lines.append(f'{ki}place_text: "{place_text}"')
    lines.append(f'{ki}status: suggested')
    lines.append(f'{ki}notes: |')
    lines.append(f'{cont}First note line.')
    lines.append(f'{cont}Second note line.')
    lines.append('```')
    return '\n'.join(lines) + '\n'


# ── Surgical edit (pure text) ──────────────────────────────────────────────────

class ApplyClaimReviewTests(unittest.TestCase):
    def test_replace_status_and_reviewed_in_place(self) -> None:
        text = (
            '## Claims\n```yaml\n'
            '- id: C-aa11bb22cc\n'
            '  type: birth\n'
            '  status: accepted\n'
            '  reviewed: 2026-01-01\n'
            '```\n'
        )
        new, changed = claim._apply_claim_review(
            text, 'C-aa11bb22cc', status='suggested', reviewed='2026-06-24')
        self.assertTrue(changed)
        self.assertIn('status: suggested', new)
        self.assertIn('reviewed: 2026-06-24', new)
        self.assertNotIn('2026-01-01', new)

    def test_inserts_reviewed_after_status_when_absent(self) -> None:
        text = (
            '## Claims\n```yaml\n'
            '- value: "Born 1880"\n'
            '  id: C-aa11bb22cc\n'
            '  status: suggested\n'
            '  confidence: high\n'
            '```\n'
        )
        new, changed = claim._apply_claim_review(
            text, 'C-aa11bb22cc', status='accepted', reviewed='2026-06-24')
        self.assertTrue(changed)
        lines = new.splitlines()
        s = next(i for i, ln in enumerate(lines) if ln.strip() == 'status: accepted')
        self.assertEqual(lines[s + 1].strip(), 'reviewed: 2026-06-24')

    def test_only_target_claim_touched(self) -> None:
        new, changed = claim._apply_claim_review(
            _CLAIM_BLOCK, 'C-aa11bb22cc', status='accepted', reviewed='2026-06-24')
        self.assertTrue(changed)
        rec = read_record_from_text(new)
        by_id = {c['id']: c for c in rec}
        self.assertEqual(by_id['C-aa11bb22cc']['status'], 'accepted')
        # the sibling death claim is untouched
        self.assertEqual(by_id['C-bb22cc33dd']['status'], 'suggested')
        self.assertNotIn('reviewed', by_id['C-bb22cc33dd'])

    def test_comment_preserved(self) -> None:
        new, _ = claim._apply_claim_review(
            _CLAIM_BLOCK, 'C-aa11bb22cc', status='accepted', reviewed='2026-06-24')
        self.assertIn('# inline comment kept', new)

    def test_unknown_id_no_change(self) -> None:
        new, changed = claim._apply_claim_review(
            _CLAIM_BLOCK, 'C-9999999999', status='accepted', reviewed='2026-06-24')
        self.assertFalse(changed)
        self.assertEqual(new, _CLAIM_BLOCK)

    def test_value_and_date_edits(self) -> None:
        new, changed = claim._apply_claim_review(
            _CLAIM_BLOCK, 'C-aa11bb22cc', status='accepted', reviewed='2026-06-24',
            value='Anna Smith born 1881, Topeka', date='1881')
        self.assertTrue(changed)
        rec = {c['id']: c for c in read_record_from_text(new)}
        self.assertEqual(rec['C-aa11bb22cc']['value'], 'Anna Smith born 1881, Topeka')
        self.assertEqual(str(rec['C-aa11bb22cc']['date']), '1881')

    def test_block_scalar_value_refused(self) -> None:
        text = (
            '## Claims\n```yaml\n'
            '- id: C-aa11bb22cc\n'
            '  value: >\n'
            '    A long\n'
            '    block scalar\n'
            '  status: suggested\n'
            '```\n'
        )
        with self.assertRaises(claim._ClaimEditRefused):
            claim._apply_claim_review(
                text, 'C-aa11bb22cc', status='accepted', reviewed='2026-06-24', value='new')

    def test_status_only_edit_tolerates_block_scalar_value(self) -> None:
        text = (
            '## Claims\n```yaml\n'
            '- id: C-aa11bb22cc\n'
            '  value: >\n'
            '    A long\n'
            '    block scalar\n'
            '  status: suggested\n'
            '```\n'
        )
        new, changed = claim._apply_claim_review(
            text, 'C-aa11bb22cc', status='accepted', reviewed='2026-06-24')
        self.assertTrue(changed)
        self.assertIn('status: accepted', new)
        self.assertIn('block scalar', new)


def read_record_from_text(text: str) -> list:
    """Parse a record's claims from in-memory text via a temp file (read_record)."""
    with tempfile.NamedTemporaryFile('w', suffix='.md', delete=False, encoding='utf-8') as fh:
        fh.write(text)
        tmp = Path(fh.name)
    try:
        return read_record(tmp)['claims']
    finally:
        tmp.unlink(missing_ok=True)


# ── Indent variants (the P1 regression, unit level) ─────────────────────────────

class KeyIndentVariantTests(unittest.TestCase):
    """YAML lets an author pick any dash-to-key spacing; the item's keys then own
    that column. The edit must follow each item's own column - assuming the
    conventional two spaces corrupted every wider item's whole block."""

    def _block(self, pad: int) -> str:
        dash = '-' + ' ' * pad
        ki = ' ' * (1 + pad)     # keys align under the inline first key
        return (
            '## Claims\n```yaml\n'
            f'{dash}value: "Born 1880"\n'
            f'{ki}id: C-aa11bb22cc\n'
            f'{ki}type: birth\n'
            f'{ki}persons: [P-aaaaaaaaaa]\n'
            f'{ki}status: suggested\n'
            '```\n'
        )

    def test_edits_follow_each_items_own_column(self) -> None:
        for pad in (1, 2, 3, 5):
            with self.subTest(pad=pad):
                new, changed = claim._apply_claim_review(
                    self._block(pad), 'C-aa11bb22cc',
                    status='accepted', reviewed='2026-07-03')
                self.assertTrue(changed)
                claims = read_record_from_text(new)
                self.assertEqual(len(claims), 1)
                self.assertEqual(claims[0]['status'], 'accepted')
                self.assertEqual(str(claims[0]['reviewed']), '2026-07-03')

    def test_dash_line_value_edit_keeps_the_items_column(self) -> None:
        # Replacing the inline first key must keep the author's dash spacing,
        # else the rewritten first key changes the column the other keys sit at.
        new, changed = claim._apply_claim_review(
            self._block(3), 'C-aa11bb22cc',
            status='accepted', reviewed='2026-07-03', value='Born 1881')
        self.assertTrue(changed)
        self.assertIn('-   value: Born 1881\n', new)
        claims = read_record_from_text(new)
        self.assertEqual(claims[0]['value'], 'Born 1881')

    def test_standard_two_space_item_stays_byte_identical_elsewhere(self) -> None:
        # The happy path must not be reshaped by the derivation or the guard:
        # the only change is the status line plus the inserted reviewed: line.
        new, changed = claim._apply_claim_review(
            _CLAIM_BLOCK, 'C-aa11bb22cc', status='accepted', reviewed='2026-06-24')
        self.assertTrue(changed)
        expected = _CLAIM_BLOCK.replace(
            '  status: suggested\n',
            '  status: accepted\n  reviewed: 2026-06-24\n', 1)
        self.assertEqual(new, expected)


# ── The shared pre-write guard (_lib.claims_edit_problem) ───────────────────────

class ClaimsEditProblemTests(unittest.TestCase):
    """The guard is the insurance layer: any rewrite that would corrupt the
    block, lose the claim, duplicate it, or drop the requested status must be
    reported as a problem so the writer refuses instead of saving it."""

    GOOD = '## Claims\n```yaml\n- id: C-aa11bb22cc\n  status: accepted\n```\n'

    def test_sound_rewrite_has_no_problem(self) -> None:
        self.assertIsNone(
            claims_edit_problem(self.GOOD, 'C-aa11bb22cc', expect_status='accepted'))

    def test_structural_check_alone_without_a_claim_id(self) -> None:
        self.assertIsNone(claims_edit_problem(self.GOOD))

    def test_broken_yaml_is_a_problem(self) -> None:
        bad = ('## Claims\n```yaml\n'
               '-   value: farmer\n'
               '  status: accepted\n'
               '    id: C-aa11bb22cc\n'
               '```\n')
        self.assertIsNotNone(claims_edit_problem(bad, 'C-aa11bb22cc'))

    def test_vanished_claim_is_a_problem(self) -> None:
        self.assertIsNotNone(claims_edit_problem(self.GOOD, 'C-9999999999'))

    def test_duplicated_claim_is_a_problem(self) -> None:
        dup = ('## Claims\n```yaml\n'
               '- id: C-aa11bb22cc\n  status: accepted\n'
               '- id: C-aa11bb22cc\n  status: suggested\n```\n')
        self.assertIsNotNone(claims_edit_problem(dup, 'C-aa11bb22cc'))

    def test_status_that_did_not_land_is_a_problem(self) -> None:
        self.assertIsNotNone(
            claims_edit_problem(self.GOOD, 'C-aa11bb22cc', expect_status='rejected'))


# ── run_claim contract ──────────────────────────────────────────────────────────

class RunClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = _write_source(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _status_of(self, cid: str) -> str:
        rec = {c['id']: c for c in read_record(self.source)['claims']}
        return rec[cid]['status']

    def test_accept_round_trip_stamps_reviewed(self) -> None:
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc',
                                 status='accepted', reviewed='2026-06-24')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['before_status'], 'suggested')
        self.assertEqual(self._status_of('C-aa11bb22cc'), 'accepted')
        rec = {c['id']: c for c in read_record(self.source)['claims']}
        self.assertEqual(str(rec['C-aa11bb22cc']['reviewed']), '2026-06-24')
        self.assertIn(str(self.source), result.changed)

    def test_publishes_canonical_source_id(self) -> None:
        # data['source_id'] carries the canonical S-id (S- + 10 chars) the
        # holding source is filed under, alongside the kept-for-compat path.
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', status='accepted')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result['source_id'], 's-1111111111')
        self.assertRegex(result['source_id'], r'^s-[0-9a-hjkmnp-tv-z]{10}$')
        self.assertEqual(result['source'], str(self.source))

    def test_accept_defaults_reviewed_to_today_not_refused(self) -> None:
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', status='accepted')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result['reviewed'], claim._today())
        rec = {c['id']: c for c in read_record(self.source)['claims']}
        self.assertEqual(str(rec['C-aa11bb22cc']['reviewed']), claim._today())

    def test_crlf_record_keeps_crlf_no_whole_file_churn(self) -> None:
        # A CRLF-authored record edited on an LF platform (or vice-versa) must
        # not have every line ending flipped by a one-line status edit - the
        # byte-faithful claims-surgery contract (read/write_text_exact).
        crlf = _CLAIM_BLOCK.replace('\n', '\r\n')
        self.source.write_bytes(crlf.encode('utf-8'))
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc',
                                 status='accepted', reviewed='2026-06-24')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        after = self.source.read_bytes()
        self.assertNotIn(b'\r\r\n', after)         # not double-CR'd
        # Endings stayed CRLF, and the ONLY changed lines are the edited ones:
        # the count of LF-without-CR must remain zero (no line was churned LF).
        self.assertEqual(after.count(b'\n'), after.count(b'\r\n'))
        self.assertEqual(self._status_of('C-aa11bb22cc'), 'accepted')

    def test_reject_round_trip(self) -> None:
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', status='rejected')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(self._status_of('C-aa11bb22cc'), 'rejected')

    def test_park_at_suggested_stamps_reviewed(self) -> None:
        # SPEC 2.1 §8.1: looked at, cannot settle it yet -> the claim stays
        # `suggested` and carries today's `reviewed:` (and who looked).
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', status='suggested')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(self._status_of('C-aa11bb22cc'), 'suggested')
        text = self.source.read_text(encoding='utf-8')
        self.assertIn(f'reviewed: {claim._today()}', text)
        self.assertIn('reviewed_by: human', text)

    def test_retired_needs_review_status_is_refused(self) -> None:
        # SPEC 2.1 dropped `needs-review`; no tool writes it any more.
        before = self.source.read_bytes()
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', status='needs-review')
        self.assertNotEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(self.source.read_bytes(), before)

    def test_w142_fix_moves_an_old_needs_review_claim_to_suggested(self) -> None:
        # The fix `fha lint` W142 names for a claim still saying the old
        # `needs-review` must work: it parks the claim at `suggested`, dated.
        text = self.source.read_text(encoding='utf-8')
        self.source.write_text(text.replace('status: suggested', 'status: needs-review', 1),
                               encoding='utf-8')
        self.assertEqual(self._status_of('C-aa11bb22cc'), 'needs-review')
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', status='suggested')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(self._status_of('C-aa11bb22cc'), 'suggested')
        self.assertIn(f'reviewed: {claim._today()}', self.source.read_text(encoding='utf-8'))

    def test_disputed_round_trip(self) -> None:
        # `disputed` is a SPEC §8.1 review outcome (a contested claim) - the tool
        # writes it like any other non-accepted status, trail preserved.
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', status='disputed')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(self._status_of('C-aa11bb22cc'), 'disputed')

    def test_dry_run_writes_nothing(self) -> None:
        before = self.source.read_text(encoding='utf-8')
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc',
                                 status='accepted', dry_run=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.changed, [])
        self.assertEqual(self.source.read_text(encoding='utf-8'), before)
        # the preview carries a diff hunk
        self.assertTrue(any('status: accepted' in m.text for m in result.messages))

    def test_malformed_id_is_plain_refusal(self) -> None:
        result = claim.run_claim(self.root, claim_id='C-bad', status='accepted')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result['status'], 'invalid-id')
        self.assertTrue(result.messages)

    def test_unknown_id_is_not_found(self) -> None:
        result = claim.run_claim(self.root, claim_id='C-0000000000', status='accepted')
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result['status'], 'not-found')

    def test_bad_reviewed_date_refused(self) -> None:
        before = self.source.read_text(encoding='utf-8')
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc',
                                 status='accepted', reviewed='not-a-date')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(self.source.read_text(encoding='utf-8'), before)


# ── run_claim on a wide-indent item (the P1 regression, end to end) ─────────────

_WIDE_SOURCE = '''---
id: S-aaaaaaaaaa
title: Wide-indent test notes
source_type: other
source_class: derivative
citation: >
  A fictional citation.
people: [P-cccccccccc]
created: 2026-07-01
---

## Claims
```yaml
-   value: farmer
    id: C-bbbbbbbbbb
    type: occupation
    persons: [P-cccccccccc]
    status: suggested
    confidence: medium
```

## Notes
*(none yet)*
'''


class WideIndentClaimTests(unittest.TestCase):
    """The reproduced P1: a valid 4-space claim item used to get a SECOND
    status:/reviewed: inserted at column 2, the tool printed success, and the
    whole block stopped parsing - every claim in the source vanished from
    lint/index/report. The fix must edit at the item's real column, and the
    pre-write guard must turn any remaining corruption into a refusal."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = self.root / 'sources' / 'other' / 'test-notes_S-aaaaaaaaaa.md'
        self.source.parent.mkdir(parents=True, exist_ok=True)
        self.source.write_text(_WIDE_SOURCE, encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_four_space_item_accept_round_trip(self) -> None:
        result = claim.run_claim(self.root, claim_id='C-bbbbbbbbbb',
                                 status='accepted', reviewed='2026-07-03')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result['status'], 'ok')
        rec = read_record(self.source)
        self.assertEqual(rec['parse_errors'], [])
        self.assertEqual(len(rec['claims']), 1)
        c = rec['claims'][0]
        self.assertEqual(c['status'], 'accepted')
        self.assertEqual(str(c['reviewed']), '2026-07-03')
        self.assertIn(str(self.source), result.changed)

    def test_four_space_item_dry_run_writes_nothing(self) -> None:
        before = self.source.read_text(encoding='utf-8')
        result = claim.run_claim(self.root, claim_id='C-bbbbbbbbbb',
                                 status='accepted', dry_run=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.changed, [])
        self.assertEqual(self.source.read_text(encoding='utf-8'), before)

    def test_wrong_indent_rewrite_is_refused_file_untouched(self) -> None:
        # Force the old buggy assumption (base indent + 2) back in, simulating
        # a future indent regression: the guard must refuse cleanly - refusal
        # exit code, file byte-identical, message names the file, no traceback.
        import unittest.mock as mock
        before = self.source.read_text(encoding='utf-8')
        with mock.patch.object(claim, 'claim_item_key_indent',
                               lambda item, base: base + '  '):
            result = claim.run_claim(self.root, claim_id='C-bbbbbbbbbb',
                                     status='accepted', reviewed='2026-07-03')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(result.changed, [])
        self.assertEqual(self.source.read_text(encoding='utf-8'), before)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn(str(self.source), text)
        self.assertNotIn('Traceback', text)

    def test_corruption_refusal_keeps_hide_wording(self) -> None:
        # The corruption case (the edit itself would break the block) keeps
        # the "hide every claim" warning - that wording is TRUE here, and it
        # must not be rerouted to the duplicate-id (E001) branch.
        import unittest.mock as mock
        with mock.patch.object(claim, 'claim_item_key_indent',
                               lambda item, base: base + '  '):
            result = claim.run_claim(self.root, claim_id='C-bbbbbbbbbb',
                                     status='accepted', reviewed='2026-07-03')
        self.assertEqual(result['status'], 'refused')
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('hide every claim', text)
        self.assertNotIn('E001', text)


# ── Quoted id lines inside block scalars (round-2 finding 2) ────────────────────

_QUOTED_SOURCE = '''---
id: S-3333333333
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


class QuotedIdClaimTests(unittest.TestCase):
    """The round-2 M4 shape: claim A's `notes: |` quotes claim B's id line.
    The shape-only span match located A, edited A, and the status guard then
    refused - a wrong refusal on a perfectly reviewable claim. Ownership
    matching (the item's own `id:` key line) must make the review land on B."""

    DECOY, TARGET = 'C-aa00000001', 'C-bb00000002'

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = self.root / 'sources' / 'other' / 'quoted-notes_S-3333333333.md'
        self.source.parent.mkdir(parents=True, exist_ok=True)
        self.source.write_text(_QUOTED_SOURCE, encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_review_lands_on_the_owning_claim(self) -> None:
        result = claim.run_claim(self.root, claim_id=self.TARGET,
                                 status='accepted', reviewed='2026-07-05')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result['status'], 'ok')
        rec = {c['id']: c for c in read_record(self.source)['claims']}
        self.assertEqual(rec[self.TARGET]['status'], 'accepted')
        self.assertEqual(str(rec[self.TARGET]['reviewed']), '2026-07-05')
        # the decoy is untouched, its quoted evidence intact
        self.assertEqual(rec[self.DECOY]['status'], 'accepted')
        self.assertEqual(str(rec[self.DECOY]['reviewed']), '2026-01-01')
        self.assertIn(f'id: {self.TARGET}', rec[self.DECOY]['notes'])

    def test_unit_edit_targets_the_owner(self) -> None:
        new, changed = claim._apply_claim_review(
            _QUOTED_SOURCE, self.TARGET, status='suggested', reviewed='2026-07-05')
        self.assertTrue(changed)
        rec = {c['id']: c for c in read_record_from_text(new)}
        self.assertEqual(rec[self.TARGET]['status'], 'suggested')
        self.assertEqual(rec[self.DECOY]['status'], 'accepted')

    def test_quoted_only_id_is_clean_not_found(self) -> None:
        # The quoted id names a claim that exists nowhere - a clean not-found,
        # never an edit onto the quoting claim.
        ghost = 'C-cc00000003'
        self.source.write_text(
            _QUOTED_SOURCE.replace(f'id: {self.TARGET}\n    which covers',
                                   f'id: {ghost}\n    which covers'),
            encoding='utf-8')
        before = self.source.read_text(encoding='utf-8')
        result = claim.run_claim(self.root, claim_id=ghost, status='accepted')
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result['status'], 'not-found')
        self.assertEqual(self.source.read_text(encoding='utf-8'), before)

    def test_belt_refuses_when_ownership_and_parse_disagree(self) -> None:
        # Belt and braces: if line-level ownership ever picks a span whose
        # PARSED claim is not the target, the edit must refuse, not land.
        import unittest.mock as mock
        with mock.patch.object(claim, '_own_id_key_line',
                               lambda lines, start, end, base: (start, self.TARGET)):
            with self.assertRaises(claim._ClaimEditRefused):
                claim._apply_claim_review(
                    _QUOTED_SOURCE, self.TARGET, status='accepted', reviewed='2026-07-05')


# ── Duplicate claim ids refuse with the E001 repair path (round-2 finding 15) ───

_DUP_SOURCE = '''---
id: S-4444444444
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


class DuplicateIdClaimRefusalTests(unittest.TestCase):
    """A pre-existing duplicate C-id must refuse with the repair that helps -
    E001 plus `fha id mint C` - not the corruption wording, which is false
    for this case and closed the repair path with wrong advice."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = self.root / 'sources' / 'other' / 'dup-notes_S-4444444444.md'
        self.source.parent.mkdir(parents=True, exist_ok=True)
        self.source.write_text(_DUP_SOURCE, encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_duplicate_refusal_names_e001_and_mint(self) -> None:
        before = self.source.read_text(encoding='utf-8')
        result = claim.run_claim(self.root, claim_id='C-aa00000001', status='accepted')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(result.changed, [])
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('E001', text)
        self.assertIn('fha id mint C', text)
        self.assertNotIn('hide every claim', text)
        self.assertIn(str(self.source), text)
        self.assertEqual(self.source.read_text(encoding='utf-8'), before)


# ── End-to-end: index + lint reflect the status change ──────────────────────────

class ClaimIndexLintIntegrationTests(unittest.TestCase):
    """A real archive: demoting an accepted vital reopens its W101 gap, and
    re-accepting it through the tool relieves the gap again - proving fha index
    and fha lint pick up what `fha claim` wrote."""

    BIRTH_CLAIM = 'C-fd0000001a'           # James Bradford's accepted birth
    PERSON = 'P-2b3c4d5e6f'                # curated; birth is his only birth claim

    @classmethod
    def setUpClass(cls) -> None:
        if not EXAMPLE.is_dir():
            raise unittest.SkipTest('example-archive not present')
        import index
        import lint
        cls.index = index
        cls.lint = lint

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / 'archive'
        shutil.copytree(EXAMPLE, self.root)
        self.config = load_fha_yaml(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _claim_status_in_index(self, cid: str) -> str | None:
        self.index.build_index(self.root, self.config)
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        try:
            row = conn.execute('SELECT status FROM claims WHERE id=?', (cid.lower(),)).fetchone()
        finally:
            conn.close()
        return row[0] if row else None

    def _w101_birth_for_person(self) -> bool:
        result = self.lint.run_lint(self.root, self.config)
        person = self.PERSON.lower()
        return any(
            m.code == 'W101' and person in m.text.lower() and 'birth' in m.text.lower()
            for m in result.messages
        )

    def test_demote_then_reaccept_round_trip(self) -> None:
        # Baseline: birth accepted, no W101-birth gap for this person.
        self.assertEqual(self._claim_status_in_index(self.BIRTH_CLAIM), 'accepted')
        self.assertFalse(self._w101_birth_for_person())

        # Park it back at suggested → the vital gap reopens; index shows the move.
        demote = claim.run_claim(self.root, claim_id=self.BIRTH_CLAIM, status='suggested')
        self.assertEqual(demote.exit_code, EXIT_CLEAN)
        self.assertEqual(self._claim_status_in_index(self.BIRTH_CLAIM), 'suggested')
        self.assertTrue(self._w101_birth_for_person())

        # Re-accept (default today) → gap relieved again; index reflects accepted.
        accept = claim.run_claim(self.root, claim_id=self.BIRTH_CLAIM, status='accepted')
        self.assertEqual(accept.exit_code, EXIT_CLEAN)
        self.assertEqual(self._claim_status_in_index(self.BIRTH_CLAIM), 'accepted')
        self.assertFalse(self._w101_birth_for_person())


class DateRoundTripTests(unittest.TestCase):
    """#205: every date the archive accepts must survive being written.

    A genealogist corrected a claim to "before 1 Oct 1884". `fha claim`
    confirmed it, wrote `date: [..1884-10-01]` bare, and YAML read that as
    a one-item list. The next `fha lint` reported E014 for the claim just
    fixed. Every date form `is_valid_edtf` accepts goes through each writer
    here and must read back as the same string.
    """

    # Years before 1000 too: '0750' (digits 0-7 only) is an octal number to
    # YAML 1.1 and read back as 488 until yaml_edtf checked the round trip
    # (#208 audit); '0999' and '0750/0760' read back intact either way.
    FORMS = ('1880', '1880-06', '1880-06-15', '1880~', '1880?', '188X',
             '1850-~05', '[..1884-10-01]', '[..1920]', '1871-02/1871-03',
             '1824/1825', '0750', '0123?', '0999', '0750/0760', '0750-03')

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = _write_source(self.root)
        _write_person(self.root, 'P-aaaaaaaaaa', 'Anna Smith')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _date_of(self, cid: str) -> str:
        claims = {c['id']: c for c in read_record(self.source)['claims']}
        return str(claims[cid]['date'])

    def test_every_form_is_one_the_archive_accepts(self) -> None:
        for form in self.FORMS:
            self.assertTrue(is_valid_edtf(form), form)

    def test_editing_a_claim_date_round_trips(self) -> None:
        for form in self.FORMS:
            with self.subTest(form=form):
                result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', date=form)
                self.assertEqual(result.exit_code, EXIT_CLEAN, result.messages)
                self.assertEqual(self._date_of('C-aa11bb22cc'), form)

    def test_adding_a_date_to_an_undated_claim_round_trips(self) -> None:
        # C-bb22cc33dd has no date: line, so this is the insert branch.
        result = claim.run_claim(self.root, claim_id='C-bb22cc33dd', date='[..1950-03-01]')
        self.assertEqual(result.exit_code, EXIT_CLEAN, result.messages)
        self.assertEqual(self._date_of('C-bb22cc33dd'), '[..1950-03-01]')

    def test_plain_words_before_round_trips(self) -> None:
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', date='before 1884-10-01')
        self.assertEqual(result.exit_code, EXIT_CLEAN, result.messages)
        self.assertEqual(self._date_of('C-aa11bb22cc'), '[..1884-10-01]')

    def test_a_new_claim_round_trips(self) -> None:
        for form in self.FORMS:
            with self.subTest(form=form):
                result = claim.run_claim_new(
                    self.root, source_id='S-1111111111', claim_type='residence',
                    value='Lived in Topeka', date=form)
                self.assertEqual(result.exit_code, EXIT_CLEAN, result.messages)
                self.assertEqual(self._date_of(result['claim_id']), form)

    def test_plain_dates_stay_unquoted(self) -> None:
        # Quoting only where YAML would misread: no restyling of ordinary dates.
        claim.run_claim(self.root, claim_id='C-aa11bb22cc', date='1880-06-15')
        self.assertIn('  date: 1880-06-15\n', self.source.read_text(encoding='utf-8'))
        self.assertEqual(yaml_edtf('1850~'), '1850~')
        self.assertEqual(yaml_edtf('[..1920]'), "'[..1920]'")

    def test_lint_accepts_what_was_written(self) -> None:
        import lint
        claim.run_claim(self.root, claim_id='C-aa11bb22cc', date='[..1884-10-01]')
        (self.root / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
        result = lint.run_lint(self.root, load_fha_yaml(self.root))
        e014 = [m.text for m in result.messages if m.code == 'E014']
        self.assertEqual(e014, [])

    def test_a_person_stub_birth_round_trips(self) -> None:
        # `fha person new --birth` / `fha stubs` share render_stub_content.
        stub = self.root / 'people' / 'stubs' / 'cole__ida_P-cccccccccc.md'
        stub.write_text(render_stub_content(
            'P-cccccccccc', 'Ida Cole', birth='[..1920]', death='1990~'), encoding='utf-8')
        meta = read_record(stub)['meta']
        self.assertEqual(str(meta['birth']), '[..1920]')
        self.assertEqual(str(meta['death']), '1990~')


# ── fha claim new: run_claim_new contract ────────────────────────────────────────

class RunClaimNewTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = _write_source(self.root)
        _write_person(self.root, 'P-aaaaaaaaaa', 'Anna Smith')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _claims(self) -> dict:
        return {c['id']: c for c in read_record(self.source)['claims']}

    def test_accepted_happy_path_writes_block_and_stamps_reviewed(self) -> None:
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='occupation',
            value='Bookkeeper, Plains Junction Railroad', persons=['P-aaaaaaaaaa'],
            date='1874', status='accepted')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result['status'], 'ok')
        self.assertIn(str(self.source), result.changed)
        cid = result['claim_id']
        self.assertTrue(cid)
        rec = self._claims()[cid]
        self.assertEqual(rec['type'], 'occupation')
        self.assertEqual(rec['value'], 'Bookkeeper, Plains Junction Railroad')
        self.assertEqual(_pids(rec['persons']), ['p-aaaaaaaaaa'])
        self.assertEqual(str(rec['date']), '1874')
        self.assertEqual(rec['status'], 'accepted')
        self.assertEqual(str(rec['reviewed']), claim._today())

    def test_suggested_has_no_reviewed_stamp(self) -> None:
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='occupation',
            value='Bookkeeper', persons=['P-aaaaaaaaaa'], status='suggested')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        rec = self._claims()[result['claim_id']]
        self.assertEqual(rec['status'], 'suggested')
        self.assertNotIn('reviewed', rec)

    def test_dry_run_writes_nothing(self) -> None:
        before = self.source.read_text(encoding='utf-8')
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='occupation',
            value='Bookkeeper', persons=['P-aaaaaaaaaa'], dry_run=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.changed, [])
        self.assertEqual(self.source.read_text(encoding='utf-8'), before)
        self.assertTrue(any('Bookkeeper' in m.text for m in result.messages))

    def test_claim_id_override_reuses_a_previously_minted_id(self) -> None:
        # P2 codex finding (round 5, PR #30): the workbench's dry-run preview
        # mints and shows a real C-id, but Apply used to call run_claim_new
        # AGAIN with no override, drawing a second, DIFFERENT id (mint_ids
        # is random) - so the claim actually created never matched the one
        # the human approved. `claim_id` lets a caller that already minted
        # one (via an earlier dry run) reuse that exact id on the live write.
        preview = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='occupation',
            value='Bookkeeper', persons=['P-aaaaaaaaaa'], dry_run=True)
        previewed_id = preview['claim_id']
        self.assertTrue(previewed_id)
        live = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='occupation',
            value='Bookkeeper', persons=['P-aaaaaaaaaa'], claim_id=previewed_id)
        self.assertEqual(live.exit_code, EXIT_CLEAN)
        self.assertEqual(live['claim_id'], previewed_id)
        self.assertIn(previewed_id, self._claims())

    def test_claim_id_override_rejects_a_malformed_id(self) -> None:
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='occupation',
            value='Bookkeeper', claim_id='not-an-id')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result['status'], 'invalid-id')

    def test_claim_id_override_rejects_the_wrong_id_type(self) -> None:
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='occupation',
            value='Bookkeeper', claim_id='P-aaaaaaaaaa')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result['status'], 'invalid-id')

    def test_claim_id_override_refuses_a_stale_preview_id_that_now_exists(self) -> None:
        first = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='occupation',
            value='Bookkeeper', persons=['P-aaaaaaaaaa'])
        self.assertEqual(first.exit_code, EXIT_CLEAN)
        again = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='residence',
            value='Elsewhere', claim_id=first['claim_id'])
        self.assertEqual(again.exit_code, EXIT_FAILURE)
        self.assertEqual(again['status'], 'refused')

    def test_missing_source_is_not_found_with_next_step(self) -> None:
        result = claim.run_claim_new(
            self.root, source_id='S-0000000000', claim_type='occupation', value='Bookkeeper')
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result['status'], 'not-found')
        self.assertTrue(any(m.next_step and 'fha find' in m.next_step for m in result.messages))

    def test_relationship_type_refused_names_sanctioned_paths(self) -> None:
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='relationship', value='Friends')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result['status'], 'refused')
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('fha person relate', text)
        self.assertIn('fha confirm cooccur', text)
        self.assertIn('roles:', text)
        # nothing written
        self.assertEqual(len(read_record(self.source)['claims']), 2)

    def test_unresolvable_person_refused_naming_id_and_fix(self) -> None:
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='occupation',
            value='Bookkeeper', persons=['P-zzzzzzzzzz'])
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result['status'], 'not-found')
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('p-zzzzzzzzzz', text)
        self.assertIn('fha person new', text)
        self.assertIn('fha stubs --from-names', text)
        self.assertEqual(len(read_record(self.source)['claims']), 2)

    def test_loose_date_normalized(self) -> None:
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='residence',
            value='Lived in Topeka', date='circa 1880')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        rec = self._claims()[result['claim_id']]
        self.assertEqual(str(rec['date']), '1880~')

    def test_nonsense_date_gets_plain_error(self) -> None:
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='residence',
            value='Lived somewhere', date='sometime maybe idk')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('1880', text)   # a concrete example, not a bare EDTF error

    def test_negated_mints_confirmed_absence_with_evidence_pairing(self) -> None:
        # SPEC §8.6: a confirmed absence is a normal claim of its type with
        # negated: true PAIRED with evidence: negative - the flag writes both,
        # since one without the other is a half-recorded conclusion.
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='marriage',
            value='No marriage found - negative searches assembled here',
            persons=['P-aaaaaaaaaa'], negated=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        rec = self._claims()[result['claim_id']]
        # The lenient record parser may keep the YAML literal as the string
        # 'true'; every consumer (index.py, lint.py) accepts both forms.
        self.assertIn(rec['negated'], (True, 'true'))
        self.assertEqual(rec['evidence'], 'negative')
        self.assertEqual(rec['status'], 'accepted')
        self.assertEqual(str(rec['reviewed']), claim._today())
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('ABSENCE', text)

    def test_positive_claim_carries_no_negated_or_evidence_keys(self) -> None:
        # The pairing is written exactly when --negated is given - a positive
        # claim must not grow either key.
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='marriage',
            value='Married at Fairview', persons=['P-aaaaaaaaaa'])
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        rec = self._claims()[result['claim_id']]
        self.assertNotIn('negated', rec)
        self.assertNotIn('evidence', rec)

    def test_place_and_place_text_coexist_on_a_new_claim(self) -> None:
        # SPEC §15: place: (the normalized link) and place_text: (the place
        # as the source wrote it) are different facts and legally coexist -
        # a new claim may carry both from the start.
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='residence',
            value='Lived somewhere', place='L-baba9801fa', place_text='Topeka')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        text = self.source.read_text(encoding='utf-8')
        self.assertIn('place: l-baba9801fa', text)
        self.assertIn('place_text: Topeka', text)

    def test_appends_after_existing_claims_with_one_blank_line(self) -> None:
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='note', value='A late addition')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        lines = self.source.read_text(encoding='utf-8').splitlines()
        idx = next(i for i, ln in enumerate(lines) if 'A late addition' in ln)
        self.assertEqual(lines[idx - 1].strip(), '')
        self.assertEqual(len(read_record(self.source)['claims']), 3)

    def test_crlf_round_trip(self) -> None:
        crlf = _CLAIM_BLOCK.replace('\n', '\r\n')
        self.source.write_bytes(crlf.encode('utf-8'))
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='note', value='CRLF test')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        after = self.source.read_bytes()
        self.assertNotIn(b'\r\r\n', after)
        self.assertEqual(after.count(b'\n'), after.count(b'\r\n'))

    def test_missing_persons_warns_but_still_mints(self) -> None:
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='note', value='No persons yet')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertTrue(any('persons:' in m.text for m in result.messages))
        self.assertIn(result['claim_id'], self._claims())

    def test_confidence_defaults_from_source_type(self) -> None:
        # confidence: is required on every claim (SPEC §8.5, lint E010), and
        # §8.5 directs tooling to DEFAULT it from source_type rather than
        # leave it missing. The fixture source is source_type: other, which
        # the rubric maps to the conservative 'medium'.
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='occupation',
            value='Bookkeeper', persons=['P-aaaaaaaaaa'])
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(self._claims()[result['claim_id']].get('confidence'), 'medium')
        self.assertTrue(any('defaulted to medium' in m.text for m in result.messages))

    def test_confidence_override_wins_and_skips_default_message(self) -> None:
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='occupation',
            value='Bookkeeper', persons=['P-aaaaaaaaaa'], confidence='high')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(self._claims()[result['claim_id']].get('confidence'), 'high')
        self.assertFalse(any('defaulted' in m.text for m in result.messages))

    def test_confidence_invalid_refused_without_write(self) -> None:
        before = (self.root / 'sources' / 'other'
                  / 'test-source_S-1111111111.md').read_text(encoding='utf-8')
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='occupation',
            value='Bookkeeper', confidence='certain')
        self.assertNotEqual(result.exit_code, EXIT_CLEAN)
        self.assertTrue(any('high, medium, low' in m.text for m in result.messages))
        after = (self.root / 'sources' / 'other'
                 / 'test-source_S-1111111111.md').read_text(encoding='utf-8')
        self.assertEqual(before, after)

    def test_default_confidence_rubric_anchors(self) -> None:
        # The SPEC §8.5 anchors, pinned: vital-record -> high, interview ->
        # low, everything else (census included) -> medium.
        from _lib import default_confidence
        self.assertEqual(default_confidence('vital-record'), 'high')
        self.assertEqual(default_confidence('interview'), 'low')
        self.assertEqual(default_confidence('census'), 'medium')
        self.assertEqual(default_confidence(None), 'medium')

    def test_edit_verb_confidence_field_only(self) -> None:
        # Field-only --confidence edit: replaces the existing confidence: line
        # in place, leaves status and reviewed untouched.
        result = claim.run_claim(
            self.root, claim_id='C-aa11bb22cc', confidence='low')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        text = (self.root / 'sources' / 'other'
                / 'test-source_S-1111111111.md').read_text(encoding='utf-8')
        self.assertIn('confidence: low', text)
        self.assertNotIn('confidence: high', text)
        self.assertIn('status: suggested', text)
        self.assertNotIn('reviewed:', text.split('C-aa11bb22cc')[1].split('- value:')[0])


# ── Issue #79 point 3: write-time place resolution on `fha claim new` ───────────

def _write_registry(root: Path, text: str) -> None:
    (root / 'places').mkdir(parents=True, exist_ok=True)
    (root / 'places' / 'places.yaml').write_text(text, encoding='utf-8')


class RunClaimNewPlaceResolutionTests(unittest.TestCase):
    """A brand-new claim minted with `place_text` and no `place` looks the
    text up against `places/places.yaml` before it is written - the "real
    fix" issue #79 asked for: an exact match after normalization attaches
    `place:` automatically, a near match is surfaced but never attached, and
    a genuine miss changes nothing from today's behavior."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = _write_source(self.root)
        _write_person(self.root, 'P-aaaaaaaaaa', 'Anna Smith')
        _write_registry(
            self.root,
            '- id: L-baba9801fa\n'
            '  name: Topeka, Kansas\n'
            '  alt_names: ["Topeka County, Kansas"]\n')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _claims(self) -> dict:
        return {c['id']: c for c in read_record(self.source)['claims']}

    def test_exact_match_attaches_place_id_automatically(self) -> None:
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='residence',
            value='Lived in Topeka, Kansas', persons=['P-aaaaaaaaaa'],
            place_text='Topeka, Kansas')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        rec = self._claims()[result['claim_id']]
        self.assertEqual(str(rec['place']), 'l-baba9801fa')
        self.assertEqual(rec['place_text'], 'Topeka, Kansas')   # unaltered, SPEC §15
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('matched the registered place', text)
        self.assertIn('attached place: automatically', text)

    def test_dry_run_exact_match_says_would_attach_not_attached(self) -> None:
        # Codex review, PR #150: a --dry-run preview used to say "attached
        # place: automatically" (implying a write happened) in the same
        # breath the dry-run trailer said nothing was written - directly
        # contradictory mutation-status text in one preview. The dry-run
        # phrasing must say what WOULD happen, never what already did, and
        # nothing must actually be written.
        before = self.source.read_text(encoding='utf-8')
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='residence',
            value='Lived in Topeka, Kansas', persons=['P-aaaaaaaaaa'],
            place_text='Topeka, Kansas', dry_run=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(self.source.read_text(encoding='utf-8'), before)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('would attach place: automatically', text)
        self.assertNotIn('- attached place: automatically', text)
        self.assertIn('No file written', text)

    def test_exact_match_on_alt_name_also_attaches(self) -> None:
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='residence',
            value='Lived there', persons=['P-aaaaaaaaaa'],
            place_text='Topeka County, Kansas')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        rec = self._claims()[result['claim_id']]
        self.assertEqual(str(rec['place']), 'l-baba9801fa')

    def test_case_and_whitespace_noise_still_matches_exactly(self) -> None:
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='residence',
            value='Lived there', persons=['P-aaaaaaaaaa'],
            place_text='  topeka,   KANSAS  ')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        rec = self._claims()[result['claim_id']]
        self.assertEqual(str(rec['place']), 'l-baba9801fa')

    def test_near_match_is_not_auto_attached_but_is_noted(self) -> None:
        # Word order differs ("Kansas, Topeka" vs the registered "Topeka,
        # Kansas") - same token set, not the same string. A wrong place_id
        # is worse than an unlinked place_text, so this must NOT attach.
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='residence',
            value='Lived there', persons=['P-aaaaaaaaaa'],
            place_text='Kansas, Topeka')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        rec = self._claims()[result['claim_id']]
        self.assertNotIn('place', rec)
        self.assertEqual(rec['place_text'], 'Kansas, Topeka')
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('close to the registered place', text)
        self.assertIn('NOT linked automatically', text)
        self.assertIn('fha confirm place', text)

    def test_genuine_miss_leaves_place_text_unlinked_same_as_today(self) -> None:
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='residence',
            value='Lived there', persons=['P-aaaaaaaaaa'],
            place_text='Wichita, Kansas')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        rec = self._claims()[result['claim_id']]
        self.assertNotIn('place', rec)
        self.assertEqual(rec['place_text'], 'Wichita, Kansas')
        text = ' '.join(m.text for m in result.messages)
        self.assertNotIn('matched the registered place', text)
        self.assertNotIn('close to the registered place', text)

    def test_explicit_place_is_never_overridden_by_the_lookup(self) -> None:
        # A caller who already named a place (even a different, unrelated
        # one) is never second-guessed by the registry lookup.
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='residence',
            value='Lived there', persons=['P-aaaaaaaaaa'],
            place='L-9e2210ab44', place_text='Topeka, Kansas')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        rec = self._claims()[result['claim_id']]
        self.assertEqual(str(rec['place']), 'l-9e2210ab44')
        text = ' '.join(m.text for m in result.messages)
        self.assertNotIn('matched the registered place', text)

    def test_no_registry_file_is_a_quiet_no_op(self) -> None:
        (self.root / 'places' / 'places.yaml').unlink()
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='residence',
            value='Lived there', persons=['P-aaaaaaaaaa'],
            place_text='Topeka, Kansas')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        rec = self._claims()[result['claim_id']]
        self.assertNotIn('place', rec)

    def test_malformed_registry_warns_instead_of_a_silent_miss(self) -> None:
        # Codex review, PR #150: a malformed (as opposed to genuinely
        # missing) places.yaml used to degrade to an empty list, which
        # looked identical to "the registry has this place_text but nothing
        # matched" - the human got no signal the lookup never actually ran.
        # The mint must still succeed (place_text unlinked), but now with an
        # honest warning naming the parse problem and the fix.
        (self.root / 'places' / 'places.yaml').write_text(
            'not_a_list: true\n', encoding='utf-8')
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='residence',
            value='Lived there', persons=['P-aaaaaaaaaa'],
            place_text='Topeka, Kansas')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        rec = self._claims()[result['claim_id']]
        self.assertNotIn('place', rec)
        self.assertEqual(rec['place_text'], 'Topeka, Kansas')
        warnings = [m.text for m in result.messages if m.level == 'warning']
        self.assertTrue(any('places.yaml has a problem' in w for w in warnings))
        self.assertTrue(any('fha lint' in w for w in warnings))
        # The write actually succeeded here, so the live wording ("was") is
        # correct in this case - see the two tests below for the cases
        # where it must NOT say this.
        self.assertTrue(any('was still written' in w for w in warnings))

    def test_malformed_registry_dry_run_says_would_not_was(self) -> None:
        # Codex review, PR #150 follow-up: a --dry-run preview used to say
        # "The claim was still written" - a false claim, since --dry-run
        # writes nothing - in the very same breath the dry-run trailer said
        # "No file written". The registry-error warning must be dry-run-
        # aware exactly like the place-match note already is.
        (self.root / 'places' / 'places.yaml').write_text(
            'not_a_list: true\n', encoding='utf-8')
        before = self.source.read_text(encoding='utf-8')
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='residence',
            value='Lived there', persons=['P-aaaaaaaaaa'],
            place_text='Topeka, Kansas', dry_run=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(self.source.read_text(encoding='utf-8'), before)
        warnings = [m.text for m in result.messages if m.level == 'warning']
        self.assertTrue(any('would still be written' in w for w in warnings), warnings)
        self.assertFalse(any('was still written' in w for w in warnings), warnings)

    def test_malformed_registry_warning_is_silent_on_a_genuine_write_failure(self) -> None:
        # Codex review, PR #150 follow-up: a genuine live write failure used
        # to report the false-success "was still written" wording before the
        # write error even surfaced - the warning was added to the result
        # before the write was even attempted. Once the write actually
        # fails, the honest thing is silence on that point (the write-error
        # message already says nothing was saved), not a reassurance that
        # never came true.
        (self.root / 'places' / 'places.yaml').write_text(
            'not_a_list: true\n', encoding='utf-8')
        orig = claim.write_text_exact_atomic

        def failing(path, text):
            raise OSError('simulated disk full')
        claim.write_text_exact_atomic = failing
        try:
            result = claim.run_claim_new(
                self.root, source_id='S-1111111111', claim_type='residence',
                value='Lived there', persons=['P-aaaaaaaaaa'],
                place_text='Topeka, Kansas')
        finally:
            claim.write_text_exact_atomic = orig
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        texts = [m.text for m in result.messages]
        self.assertFalse(any('still written' in t for t in texts), texts)
        self.assertTrue(any('cannot write' in t for t in texts), texts)


# ── fha claim new: CLI routing (fha.main and the standalone parser) ─────────────

class ClaimNewCliRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        # resolve_root_arg (the CLI-layer path _cmd_claim/_cmd_claim_new use,
        # unlike run_claim/run_claim_new called directly) requires fha.yaml.
        (self.root / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8')
        self.source = _write_source(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_fha_main_routes_claim_new(self) -> None:
        import fha
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fha.main(['claim', 'new', '--source', 'S-1111111111', '--type', 'note',
                          '--value', 'via fha.main', '--root', str(self.root)])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('Minted', out.getvalue())
        claims = read_record(self.source)['claims']
        self.assertTrue(any(c['value'] == 'via fha.main' for c in claims))

    def test_fha_main_flat_claim_verb_still_works(self) -> None:
        import fha
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fha.main(['claim', 'C-aa11bb22cc', '--status', 'accepted',
                          '--root', str(self.root)])
        self.assertEqual(rc, EXIT_CLEAN)
        claims = {c['id']: c for c in read_record(self.source)['claims']}
        self.assertEqual(claims['C-aa11bb22cc']['status'], 'accepted')

    def test_standalone_new_subcommand(self) -> None:
        rc = claim._standalone_main(['new', '--source', 'S-1111111111', '--type', 'note',
                                     '--value', 'via standalone', '--root', str(self.root)])
        self.assertEqual(rc, EXIT_CLEAN)
        claims = read_record(self.source)['claims']
        self.assertTrue(any(c['value'] == 'via standalone' for c in claims))

    def test_negated_flag_reaches_the_engine_through_fha_main(self) -> None:
        import fha
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fha.main(['claim', 'new', '--source', 'S-1111111111', '--type', 'marriage',
                          '--value', 'no marriage found', '--negated', '--root', str(self.root)])
        self.assertEqual(rc, EXIT_CLEAN)
        claims = read_record(self.source)['claims']
        minted = [c for c in claims if c['value'] == 'no marriage found']
        self.assertEqual(len(minted), 1)
        self.assertIn(minted[0]['negated'], (True, 'true'))
        self.assertEqual(minted[0]['evidence'], 'negative')

    def test_standalone_flat_verb_still_works(self) -> None:
        rc = claim._standalone_main(['C-aa11bb22cc', '--status', 'accepted', '--root', str(self.root)])
        self.assertEqual(rc, EXIT_CLEAN)
        claims = {c['id']: c for c in read_record(self.source)['claims']}
        self.assertEqual(claims['C-aa11bb22cc']['status'], 'accepted')


# ── Field extension (Task 3): _apply_claim_review unit-level round-trips ────────

class FieldExtensionApplyTests(unittest.TestCase):
    """Each new field (type/place/place_text/persons) round-trips at both the
    2-space (pad=1) and 4-space (pad=3) dash-to-key conventions - the same
    discipline KeyIndentVariantTests exercises for status/value/date."""

    def test_type_round_trips_both_indents(self) -> None:
        for pad in (1, 3):
            with self.subTest(pad=pad):
                new, changed = claim._apply_claim_review(
                    _indented_block(pad), 'C-aa11bb22cc', type_='occupation')
                self.assertTrue(changed)
                claims = read_record_from_text(new)
                self.assertEqual(claims[0]['type'], 'occupation')
                self.assertEqual(claims[0]['status'], 'suggested')  # untouched

    def test_place_round_trips_both_indents(self) -> None:
        for pad in (1, 3):
            with self.subTest(pad=pad):
                new, changed = claim._apply_claim_review(
                    _indented_block(pad), 'C-aa11bb22cc', place='L-baba9801fa')
                self.assertTrue(changed)
                claims = read_record_from_text(new)
                self.assertEqual(str(claims[0]['place']), 'L-baba9801fa')

    def test_place_text_round_trips_both_indents(self) -> None:
        for pad in (1, 3):
            with self.subTest(pad=pad):
                new, changed = claim._apply_claim_review(
                    _indented_block(pad), 'C-aa11bb22cc', place_text='Topeka, Kansas')
                self.assertTrue(changed)
                claims = read_record_from_text(new)
                self.assertEqual(claims[0]['place_text'], 'Topeka, Kansas')

    def test_persons_round_trips_both_indents_and_replaces(self) -> None:
        for pad in (1, 3):
            with self.subTest(pad=pad):
                new, changed = claim._apply_claim_review(
                    _indented_block(pad), 'C-aa11bb22cc',
                    persons=['P-bbbbbbbbbb', 'P-cccccccccc'])
                self.assertTrue(changed)
                claims = read_record_from_text(new)
                self.assertEqual(_pids(claims[0]['persons']), ['p-bbbbbbbbbb', 'p-cccccccccc'])

    def test_place_text_edit_preserves_the_place_link(self) -> None:
        # SPEC §15: the two place keys are independent facts. Rewording the
        # source-as-written text must not silently unlink the registry place.
        new, changed = claim._apply_claim_review(
            _indented_block(1, place='L-baba9801fa'), 'C-aa11bb22cc',
            place_text='Fairview City')
        self.assertTrue(changed)
        claims = read_record_from_text(new)
        self.assertEqual(claims[0]['place_text'], 'Fairview City')
        self.assertEqual(str(claims[0]['place']), 'L-baba9801fa')

    def test_place_backfill_preserves_place_text(self) -> None:
        # The elevation flow's per-claim backfill ("place_text itself is
        # never altered", SPEC §15) - and the workbench claim edit, whose
        # untouched-fields POST carries only the place id (P2 codex finding,
        # round 4, PR #31): setting place: must not erase the wording.
        new, changed = claim._apply_claim_review(
            _indented_block(1, place_text='Fairview City'), 'C-aa11bb22cc',
            place='L-baba9801fa')
        self.assertTrue(changed)
        claims = read_record_from_text(new)
        self.assertEqual(str(claims[0]['place']), 'L-baba9801fa')
        self.assertEqual(claims[0]['place_text'], 'Fairview City')

    def test_status_optional_field_only_edit_leaves_status_and_reviewed_untouched(self) -> None:
        new, changed = claim._apply_claim_review(
            _CLAIM_BLOCK, 'C-aa11bb22cc', place='L-baba9801fa')
        self.assertTrue(changed)
        rec = {c['id']: c for c in read_record_from_text(new)}
        self.assertEqual(rec['C-aa11bb22cc']['status'], 'suggested')
        self.assertNotIn('reviewed', rec['C-aa11bb22cc'])
        self.assertEqual(str(rec['C-aa11bb22cc']['place']), 'L-baba9801fa')
        # sibling untouched
        self.assertNotIn('place', rec['C-bb22cc33dd'])


# ── --persons against a block-style existing list (P2 codex finding, PR #30) ────

def _block_persons_block(pad: int, *, trailing_confidence: bool = False) -> str:
    """One claim item whose `persons:` is a hand-written BLOCK list (the shape
    `set_scalar`/`remove_key` used to touch only the header line of, leaving
    the old `- P-…` continuation lines stranded under the new inline
    rewrite). `trailing_confidence` adds a `confidence:` key AFTER persons so
    a fresh `--confidence` insert exercises the `anchor` still landing after
    persons' collapsed block, not on one of its orphaned lines."""
    dash = '-' + ' ' * pad
    ki = ' ' * (1 + pad)
    li = ki + '  '   # list item indent, deeper than the persons: key itself
    lines = [
        '## Claims', '```yaml',
        f'{dash}value: "Born 1880"',
        f'{ki}id: C-aa11bb22cc',
        f'{ki}type: birth',
        f'{ki}persons:',
        f'{li}- P-aaaaaaaaaa',
        f'{li}- P-bbbbbbbbbb',
        f'{ki}status: suggested',
    ]
    if trailing_confidence:
        lines.append(f'{ki}confidence: low')
    lines.append('```')
    return '\n'.join(lines) + '\n'


class PersonsBlockStyleRegressionTests(unittest.TestCase):
    """--persons against an existing BLOCK-style (not flow `[...]`) list."""

    def test_persons_block_list_replaced_as_a_unit(self) -> None:
        for pad in (1, 3):
            with self.subTest(pad=pad):
                new, changed = claim._apply_claim_review(
                    _block_persons_block(pad), 'C-aa11bb22cc',
                    persons=['P-cccccccccc'])
                self.assertTrue(changed)
                claims = read_record_from_text(new)
                self.assertEqual(len(claims), 1)
                self.assertEqual(_pids(claims[0]['persons']), ['p-cccccccccc'])
                self.assertEqual(claims[0]['status'], 'suggested')  # untouched
                # No orphaned `- P-…` line survives from the old block list.
                self.assertNotIn('- P-aaaaaaaaaa', new)
                self.assertNotIn('- P-bbbbbbbbbb', new)

    def test_persons_block_list_replace_keeps_later_key_placement_correct(self) -> None:
        # A block-style persons: sits ABOVE confidence:. Replacing persons
        # shrinks the item; a later fresh insert (--date, not present yet)
        # relies on `anchor`, which must be shifted by that shrink or the new
        # line lands mid-block instead of after status.
        new, changed = claim._apply_claim_review(
            _block_persons_block(1, trailing_confidence=True), 'C-aa11bb22cc',
            persons=['P-cccccccccc'], date='1880')
        self.assertTrue(changed)
        claims = read_record_from_text(new)
        self.assertEqual(len(claims), 1)
        c = claims[0]
        self.assertEqual(_pids(c['persons']), ['p-cccccccccc'])
        self.assertEqual(str(c['date']), '1880')
        self.assertEqual(c['confidence'], 'low')  # untouched, still readable
        self.assertEqual(c['status'], 'suggested')

    def test_persons_block_list_via_run_claim(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            root = Path(tmp.name)
            source = _write_source(root)
            _write_person(root, 'P-aaaaaaaaaa', 'Anna Smith')
            _write_person(root, 'P-bbbbbbbbbb', 'Ben Smith')
            text = source.read_text(encoding='utf-8')
            # Rewrite C-aa11bb22cc's persons as a block list, matching a
            # human hand-edit or an older tool's output.
            text = text.replace(
                'persons: [P-aaaaaaaaaa]',
                'persons:\n    - P-aaaaaaaaaa',
                1,
            )
            source.write_text(text, encoding='utf-8')
            result = claim.run_claim(root, claim_id='C-aa11bb22cc', persons=['P-bbbbbbbbbb'])
            self.assertEqual(result.exit_code, EXIT_CLEAN)
            rec = {c['id']: c for c in read_record(source)['claims']}
            self.assertEqual(_pids(rec['C-aa11bb22cc']['persons']), ['p-bbbbbbbbbb'])
        finally:
            tmp.cleanup()


# ── The 2026-07 stale-anchor corruption (place/place_text over a notes block) ───

class StaleAnchorRegressionTests(unittest.TestCase):
    """A place_text/place key sitting above a `notes: |` block. The switch to
    the other place kind deletes that key, but the pre-fix code fixed
    status_idx/anchor BEFORE the deletion, so every line below shifted up one
    and the new place key was spliced between `notes:` and its continuation -
    silently emptying the note, folding its text into the place value, while
    the block still parsed (the guard let it through). Removing the old place
    key up front, before any index is computed, keeps the notes block intact."""

    NOTES = ['First note line.', 'Second note line.']

    def test_place_over_notes_does_not_corrupt_the_notes_block(self) -> None:
        # The EXACT repro: place_text above a two-line `notes: |`, switched to
        # --place while accepting. Rounds-trip at 2-space (pad=1) and 4-space
        # (pad=3) dash indents.
        for pad in (1, 3):
            with self.subTest(pad=pad):
                text = _notes_repro_block(pad, place_text='Fairview, as written')
                new, changed = claim._apply_claim_review(
                    text, 'C-aa11bb22cc',
                    status='accepted', reviewed='2026-07-12', place='L-baba9801fa')
                self.assertTrue(changed)
                claims = read_record_from_text(new)
                self.assertEqual(len(claims), 1)
                c = claims[0]
                # The note survived: both lines, in order, nothing folded away.
                self.assertEqual(c['notes'].splitlines(), self.NOTES)
                # The place link landed - and the source's own wording stays
                # (SPEC §15: backfill never alters place_text).
                self.assertEqual(str(c['place']), 'L-baba9801fa')
                self.assertEqual(c['place_text'], 'Fairview, as written')
                self.assertEqual(c['status'], 'accepted')
                self.assertEqual(str(c['reviewed']), '2026-07-12')
                # The `place:` line sits BEFORE the `notes:` line in the text.
                new_lines = new.splitlines()
                place_line = next(i for i, ln in enumerate(new_lines)
                                  if ln.strip().startswith('place:'))
                notes_line = next(i for i, ln in enumerate(new_lines)
                                  if ln.strip().startswith('notes:'))
                self.assertLess(place_line, notes_line)

    def test_place_text_over_notes_mirror(self) -> None:
        # The mirror: an existing place above a notes block, switched to
        # --place-text while accepting. Same two-indent round-trip.
        for pad in (1, 3):
            with self.subTest(pad=pad):
                text = _notes_repro_block(pad, place='L-1234567890')
                new, changed = claim._apply_claim_review(
                    text, 'C-aa11bb22cc',
                    status='accepted', reviewed='2026-07-12',
                    place_text='Fairview, Kansas')
                self.assertTrue(changed)
                claims = read_record_from_text(new)
                self.assertEqual(len(claims), 1)
                c = claims[0]
                self.assertEqual(c['notes'].splitlines(), self.NOTES)
                self.assertEqual(c['place_text'], 'Fairview, Kansas')
                self.assertEqual(str(c['place']), 'L-1234567890')
                new_lines = new.splitlines()
                pt_line = next(i for i, ln in enumerate(new_lines)
                               if ln.strip().startswith('place_text:'))
                notes_line = next(i for i, ln in enumerate(new_lines)
                                  if ln.strip().startswith('notes:'))
                self.assertLess(pt_line, notes_line)


# ── Field extension (Task 3): run_claim contract ─────────────────────────────────

class RunClaimFieldEditTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = _write_source(self.root)
        _write_person(self.root, 'P-aaaaaaaaaa', 'Anna Smith')
        _write_person(self.root, 'P-bbbbbbbbbb', 'Ben Smith')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _claims(self) -> dict:
        return {c['id']: c for c in read_record(self.source)['claims']}

    def test_status_now_optional_field_only_edit(self) -> None:
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', place_text='Topeka')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        rec = self._claims()['C-aa11bb22cc']
        self.assertEqual(rec['status'], 'suggested')       # untouched
        self.assertNotIn('reviewed', rec)                  # untouched
        self.assertEqual(rec['place_text'], 'Topeka')

    def test_no_mutation_flag_is_plainly_refused(self) -> None:
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result['status'], 'no-op')
        self.assertTrue(result.messages)
        self.assertIn('--status', result.messages[0].text)

    def test_persons_replaces_the_whole_list(self) -> None:
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', persons=['P-bbbbbbbbbb'])
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(_pids(self._claims()['C-aa11bb22cc']['persons']), ['p-bbbbbbbbbb'])

    def test_unresolvable_person_refused(self) -> None:
        before = self.source.read_text(encoding='utf-8')
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', persons=['P-zzzzzzzzzz'])
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result['status'], 'not-found')
        self.assertEqual(self.source.read_text(encoding='utf-8'), before)

    def test_type_relationship_refused(self) -> None:
        before = self.source.read_text(encoding='utf-8')
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', claim_type='relationship')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result['status'], 'refused')
        self.assertIn('fha confirm cooccur', ' '.join(m.text for m in result.messages))
        self.assertEqual(self.source.read_text(encoding='utf-8'), before)

    def test_type_change_round_trips(self) -> None:
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', claim_type='baptism')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(self._claims()['C-aa11bb22cc']['type'], 'baptism')

    def test_place_and_place_text_set_together_write_both(self) -> None:
        # SPEC §15: the normalized link and the source wording coexist, so
        # one review edit may set both keys at once.
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc',
                                 place='L-baba9801fa', place_text='Topeka')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        c = self._claims()['C-aa11bb22cc']
        self.assertEqual(str(c['place']), 'l-baba9801fa')
        self.assertEqual(c['place_text'], 'Topeka')

    def test_reviewed_without_status_refused(self) -> None:
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc',
                                 reviewed='2026-07-11', value='corrected value')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertIn('--status', ' '.join(m.text for m in result.messages))

    def test_status_move_still_stamps_reviewed_as_before(self) -> None:
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', status='accepted')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        rec = self._claims()['C-aa11bb22cc']
        self.assertEqual(rec['status'], 'accepted')
        self.assertEqual(str(rec['reviewed']), claim._today())

    def test_existing_status_only_call_unaffected(self) -> None:
        # The pre-Task-3 call shape (status only) must behave exactly as before.
        result = claim.run_claim(self.root, claim_id='C-bb22cc33dd', status='rejected')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(self._claims()['C-bb22cc33dd']['status'], 'rejected')


# ── #126/#173 follow-up: `--persons` must not silently orphan a roles: target ──

class PersonsReplaceRolesGuardTests(unittest.TestCase):
    """`--persons` REPLACES the whole `persons:` list (issue #50's own scope
    note). A claim minted by hand can already carry a `roles:` map, and this
    file never amends `roles:` alongside it - so before this guard, a
    `--persons` replace that dropped someone the map still named would
    silently orphan that role target: the exact #126/#173 hand-edit mistake
    W133 (`fha lint`) exists to catch (`persons: [P-widow]`, `roles: {deceased:
    [P-dead]}` with P-dead just missing from the new list), just reached
    through the sanctioned edit verb instead of a typo. `run_claim` now
    refuses that specific edit before writing it.
    """

    WIDOW = 'P-d4d4d4d4d4'
    DEAD = 'P-d5d5d5d5d5'
    OTHER = 'P-d6d6d6d6d6'

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write_person(self.root, self.WIDOW, 'Widow Smith')
        _write_person(self.root, self.DEAD, 'Dead Smith')
        _write_person(self.root, self.OTHER, 'Other Smith')
        self.source = self.root / 'sources' / 'notes' / 'rec_s-9999999999.md'
        self.source.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _claims(self) -> dict:
        return {c['id']: c for c in read_record(self.source)['claims']}

    def _write(self, roles_block: str) -> None:
        self.source.write_text(
            '---\nid: S-9999999999\ntitle: Rec\nsource_type: vital-record\n---\n\n'
            '## Claims\n```yaml\n'
            '- value: "a death"\n'
            '  id: C-3333333333\n'
            '  type: death\n'
            f'  persons: [{self.WIDOW}, {self.DEAD}]\n'
            '  status: accepted\n  reviewed: 2026-01-01\n  confidence: high\n'
            '  date: 1902-04-17\n  information: primary\n  evidence: direct\n'
            '  notes: x.\n'
            + roles_block
            + '```\n', encoding='utf-8')

    def test_dropping_the_deceased_role_target_is_refused(self) -> None:
        self._write(f'  roles:\n    deceased: [{self.DEAD}]\n')
        before = self.source.read_text(encoding='utf-8')
        result = claim.run_claim(self.root, claim_id='C-3333333333', persons=[self.WIDOW])
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result['status'], 'refused')
        text = ' '.join(m.text for m in result.messages)
        self.assertIn(self.DEAD.lower(), text)
        self.assertIn('W133', text)
        self.assertIn('#126', text)
        # Nothing written.
        self.assertEqual(self.source.read_text(encoding='utf-8'), before)

    def test_keeping_the_roled_person_in_the_new_list_succeeds(self) -> None:
        self._write(f'  roles:\n    deceased: [{self.DEAD}]\n')
        result = claim.run_claim(
            self.root, claim_id='C-3333333333', persons=[self.DEAD, self.OTHER])
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(_pids(self._claims()['C-3333333333']['persons']), [self.DEAD.lower(), self.OTHER.lower()])

    def test_multiple_orphaned_targets_are_all_named(self) -> None:
        self._write(f'  roles:\n    deceased: [{self.DEAD}]\n    witness: [{self.OTHER}]\n')
        result = claim.run_claim(self.root, claim_id='C-3333333333', persons=[self.WIDOW])
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn(self.DEAD.lower(), text)
        self.assertIn(self.OTHER.lower(), text)

    def test_empty_roles_dict_is_unaffected(self) -> None:
        self._write('  roles: {}\n')
        result = claim.run_claim(self.root, claim_id='C-3333333333', persons=[self.WIDOW])
        self.assertEqual(result.exit_code, EXIT_CLEAN)

    def test_no_roles_at_all_is_unaffected(self) -> None:
        self._write('')
        result = claim.run_claim(self.root, claim_id='C-3333333333', persons=[self.WIDOW])
        self.assertEqual(result.exit_code, EXIT_CLEAN)

    def test_list_shorthand_roles_never_crashes(self) -> None:
        self._write('  roles: [spouse, child]\n')
        result = claim.run_claim(self.root, claim_id='C-3333333333', persons=[self.WIDOW])
        self.assertEqual(result.exit_code, EXIT_CLEAN)

    def test_a_name_link_role_target_is_a_documented_blind_spot(self) -> None:
        # This file edits one claim straight off sources/, never a built
        # index/registry (_find_claim_record's own contract) - so there is no
        # alias map here to resolve a name-link role value through. Same
        # boundary W133 itself draws (an unresolvable NAME is a different,
        # pre-existing problem); `fha lint` remains the backstop for this
        # narrower shape.
        self._write('  roles:\n    deceased: ["[[Dead Smith]]"]\n')
        result = claim.run_claim(self.root, claim_id='C-3333333333', persons=[self.WIDOW])
        self.assertEqual(result.exit_code, EXIT_CLEAN)

    def test_dry_run_still_refuses_and_writes_nothing(self) -> None:
        self._write(f'  roles:\n    deceased: [{self.DEAD}]\n')
        before = self.source.read_text(encoding='utf-8')
        result = claim.run_claim(
            self.root, claim_id='C-3333333333', persons=[self.WIDOW], dry_run=True)
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(self.source.read_text(encoding='utf-8'), before)


# ── Issue #79 point 3: write-time place resolution on `fha claim <C-id>` ────────

class RunClaimFieldEditPlaceResolutionTests(unittest.TestCase):
    """The edit verb gets the same write-time registry lookup as `fha claim
    new` whenever `--place-text` is set with no `--place` - but only when
    the claim does not already carry a `place:`, so correcting the wording
    of an already-linked claim never silently re-points its link."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = _write_source(self.root)
        _write_person(self.root, 'P-aaaaaaaaaa', 'Anna Smith')
        _write_registry(
            self.root,
            '- id: L-baba9801fa\n'
            '  name: Topeka, Kansas\n'
            '  alt_names: ["Topeka County, Kansas"]\n')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _claims(self) -> dict:
        return {c['id']: c for c in read_record(self.source)['claims']}

    def test_exact_match_attaches_place_id_on_a_field_only_edit(self) -> None:
        # C-aa11bb22cc (the fixture claim) starts with no place: at all.
        result = claim.run_claim(
            self.root, claim_id='C-aa11bb22cc', place_text='Topeka, Kansas')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        rec = self._claims()['C-aa11bb22cc']
        self.assertEqual(str(rec['place']), 'l-baba9801fa')
        self.assertEqual(rec['place_text'], 'Topeka, Kansas')
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('matched the registered place', text)

    def test_dry_run_exact_match_says_would_attach_not_attached(self) -> None:
        # Codex review, PR #150: the edit verb had the identical ordering
        # bug as run_claim_new - the "attached ... automatically" message
        # was added before the dry_run branch, contradicting the dry-run
        # trailer's "nothing was written" in the same preview.
        before = self.source.read_text(encoding='utf-8')
        result = claim.run_claim(
            self.root, claim_id='C-aa11bb22cc', place_text='Topeka, Kansas', dry_run=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(self.source.read_text(encoding='utf-8'), before)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('would attach place: automatically', text)
        self.assertNotIn('- attached place: automatically', text)
        self.assertIn('No file written', text)

    def test_malformed_registry_warns_instead_of_a_silent_miss(self) -> None:
        # Codex review, PR #150: the edit verb gets the same honest warning
        # run_claim_new does when places.yaml exists but fails to parse -
        # the edit must still succeed, place_text unlinked, with a warning
        # naming the parse problem rather than a silent ordinary-miss look.
        (self.root / 'places' / 'places.yaml').write_text(
            'not_a_list: true\n', encoding='utf-8')
        result = claim.run_claim(
            self.root, claim_id='C-aa11bb22cc', place_text='Topeka, Kansas')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        rec = self._claims()['C-aa11bb22cc']
        self.assertNotIn('place', rec)
        self.assertEqual(rec['place_text'], 'Topeka, Kansas')
        warnings = [m.text for m in result.messages if m.level == 'warning']
        self.assertTrue(any('places.yaml has a problem' in w for w in warnings))
        self.assertTrue(any('fha lint' in w for w in warnings))
        # The write actually succeeded here, so the live wording ("was") is
        # correct in this case - see the two tests below for the cases
        # where it must NOT say this.
        self.assertTrue(any('was still written' in w for w in warnings))

    def test_malformed_registry_dry_run_says_would_not_was(self) -> None:
        # Codex review, PR #150 follow-up: a --dry-run preview used to say
        # "The claim was still written" - false, since --dry-run writes
        # nothing - in the very same breath the dry-run trailer said "No
        # file written". Dry-run-aware exactly like the place-match note.
        (self.root / 'places' / 'places.yaml').write_text(
            'not_a_list: true\n', encoding='utf-8')
        before = self.source.read_text(encoding='utf-8')
        result = claim.run_claim(
            self.root, claim_id='C-aa11bb22cc', place_text='Topeka, Kansas', dry_run=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(self.source.read_text(encoding='utf-8'), before)
        warnings = [m.text for m in result.messages if m.level == 'warning']
        self.assertTrue(any('would still be written' in w for w in warnings), warnings)
        self.assertFalse(any('was still written' in w for w in warnings), warnings)

    def test_malformed_registry_warning_is_silent_on_a_genuine_write_failure(self) -> None:
        # Codex review, PR #150 follow-up: a genuine live write failure used
        # to report the false-success "was still written" wording before the
        # write error even surfaced. Once the write actually fails, silence
        # on that point is the honest thing - the write-error message
        # already says nothing was saved.
        (self.root / 'places' / 'places.yaml').write_text(
            'not_a_list: true\n', encoding='utf-8')
        orig = claim.write_text_exact_atomic

        def failing(path, text):
            raise OSError('simulated disk full')
        claim.write_text_exact_atomic = failing
        try:
            result = claim.run_claim(
                self.root, claim_id='C-aa11bb22cc', place_text='Topeka, Kansas')
        finally:
            claim.write_text_exact_atomic = orig
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        texts = [m.text for m in result.messages]
        self.assertFalse(any('still written' in t for t in texts), texts)
        self.assertTrue(any('cannot write' in t for t in texts), texts)

    def test_near_match_is_not_auto_attached_on_edit_either(self) -> None:
        result = claim.run_claim(
            self.root, claim_id='C-aa11bb22cc', place_text='Kansas, Topeka')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        rec = self._claims()['C-aa11bb22cc']
        self.assertNotIn('place', rec)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('NOT linked automatically', text)

    def test_genuine_miss_edits_place_text_only_same_as_today(self) -> None:
        result = claim.run_claim(
            self.root, claim_id='C-aa11bb22cc', place_text='Wichita, Kansas')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        rec = self._claims()['C-aa11bb22cc']
        self.assertNotIn('place', rec)
        self.assertEqual(rec['place_text'], 'Wichita, Kansas')

    def test_a_claim_that_already_has_a_place_link_is_never_reattached(self) -> None:
        # Correcting place_text wording on an ALREADY-linked claim must not
        # silently re-point (or even just re-confirm) its existing place:
        # link from a fresh registry lookup - that link was a human/tool
        # decision made separately, and this feature only fills emptiness.
        first = claim.run_claim(
            self.root, claim_id='C-bb22cc33dd', place='L-9e2210ab44')
        self.assertEqual(first.exit_code, EXIT_CLEAN)
        second = claim.run_claim(
            self.root, claim_id='C-bb22cc33dd', place_text='Topeka, Kansas')
        self.assertEqual(second.exit_code, EXIT_CLEAN)
        rec = self._claims()['C-bb22cc33dd']
        self.assertEqual(str(rec['place']), 'l-9e2210ab44')   # untouched
        self.assertEqual(rec['place_text'], 'Topeka, Kansas')
        text = ' '.join(m.text for m in second.messages)
        self.assertNotIn('matched the registered place', text)

    def test_explicit_place_on_the_edit_verb_is_never_overridden(self) -> None:
        result = claim.run_claim(
            self.root, claim_id='C-aa11bb22cc',
            place='L-9e2210ab44', place_text='Topeka, Kansas')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        rec = self._claims()['C-aa11bb22cc']
        self.assertEqual(str(rec['place']), 'l-9e2210ab44')


# ── Batch status moves (TOOLING §3b amendment): run_claim_batch ─────────────────

class ClaimLookupSpeedTests(unittest.TestCase):
    """#225: on a 3,231-source archive `fha claim` took ~18 s per claim (every
    record YAML-parsed per lookup, twice per claim in a batch), so a 119-claim
    accept took ~36 minutes and reviewers hand-edited `status:` instead. Only
    the files whose text names a wanted id are parsed now, and a batch scans
    sources/ once."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = _write_source(self.root)
        other = self.root / 'sources' / 'other'
        for i in range(12):
            (other / f'a-unrelated-{i:02d}_S-22222222{i:02d}.md').write_text(
                _CLAIM_BLOCK.replace('C-aa11bb22cc', f'C-ee00ff00{i:02d}')
                            .replace('C-bb22cc33dd', f'C-ff00ee00{i:02d}'),
                encoding='utf-8')
        self.parsed: list[str] = []
        real = _lib_mod.read_record

        def counting(path, *a, **k):
            self.parsed.append(Path(path).name)
            return real(path, *a, **k)
        patcher = mock.patch.object(_lib_mod, 'read_record', side_effect=counting)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_one_claim_parses_only_the_file_that_names_it(self) -> None:
        result = claim.run_claim(self.root, claim_id='C-bb22cc33dd',
                                 status='accepted', reviewed='2026-07-20')
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(self.parsed)
        self.assertEqual(set(self.parsed), {self.source.name})

    def test_a_batch_scans_once_and_never_parses_unrelated_files(self) -> None:
        result = claim.run_claim_batch(self.root, claim_ids=['C-aa11bb22cc', 'C-bb22cc33dd'],
                                       status='accepted', reviewed='2026-07-20')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(set(self.parsed), {self.source.name})
        # One parse finds both ids for the gate; each write re-reads its one
        # file (plus run_claim's own reads of it) - never a rescan per id.
        before = len(self.parsed)
        self.parsed.clear()
        claim._locate_claims(self.root, ['c-aa11bb22cc', 'c-bb22cc33dd'])
        self.assertEqual(len(self.parsed), 1)
        self.assertLess(before, 12)

    def test_an_id_mentioned_in_another_files_notes_still_finds_the_claim(self) -> None:
        # Sorts before the real record and names the id only in prose: the text
        # check lets it through, the parse rules it out.
        decoy = self.root / 'sources' / 'other' / 'aaa-decoy_S-3333333333.md'
        decoy.write_text(_CLAIM_BLOCK.replace('C-aa11bb22cc', 'C-dd00dd00dd')
                         .replace('C-bb22cc33dd', 'C-dd11dd11dd')
                         .replace('*(none yet)*', 'See also C-bb22cc33dd.'),
                         encoding='utf-8')
        found = claim._find_claim_record(self.root, 'C-bb22cc33dd')
        self.assertIsNotNone(found)
        self.assertEqual(found[0], self.source)
        self.assertEqual(found[1]['type'], 'death')

    def test_a_stale_hint_falls_back_to_the_scan(self) -> None:
        wrong = self.root / 'sources' / 'other' / 'a-unrelated-00_S-2222222200.md'
        found = claim._find_claim_record(self.root, 'C-aa11bb22cc', hint=wrong)
        self.assertEqual(found[0], self.source)


class RunClaimBatchTests(unittest.TestCase):
    """The review gesture "accept 1, 2 and 4" is one human decision per claim
    delivered in one breath - the batch form turns it into one command. The
    gates under test: status-only (a field correction is inherently per-claim),
    all-or-nothing validation (one bad id refuses the whole batch before any
    write), dedupe with a note, one preview per --dry-run batch."""

    BOTH = ('C-aa11bb22cc', 'C-bb22cc33dd')

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = _write_source(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _claims(self) -> dict:
        return {c['id']: c for c in read_record(self.source)['claims']}

    def test_batch_accept_stamps_reviewed_on_each(self) -> None:
        result = claim.run_claim_batch(self.root, claim_ids=list(self.BOTH),
                                       status='accepted', reviewed='2026-07-20')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['applied'], [c.lower() for c in self.BOTH])
        rec = self._claims()
        for cid in self.BOTH:
            self.assertEqual(rec[cid]['status'], 'accepted')
            self.assertEqual(str(rec[cid]['reviewed']), '2026-07-20')
        self.assertEqual(result.changed, [str(self.source)])

    def test_batch_accept_defaults_reviewed_to_today_on_each(self) -> None:
        result = claim.run_claim_batch(self.root, claim_ids=list(self.BOTH),
                                       status='accepted')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        rec = self._claims()
        for cid in self.BOTH:
            self.assertEqual(str(rec[cid]['reviewed']), claim._today())
        self.assertEqual(result['reviewed'], claim._today())

    def test_batch_reject_works_any_status_is_legal(self) -> None:
        # Batch-reject (and batch-park at suggested) are as legitimate as
        # batch-accept: the gate is status-ONLY, not accept-only.
        result = claim.run_claim_batch(self.root, claim_ids=list(self.BOTH),
                                       status='rejected')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        rec = self._claims()
        for cid in self.BOTH:
            self.assertEqual(rec[cid]['status'], 'rejected')

    def test_batch_park_at_suggested_works(self) -> None:
        result = claim.run_claim_batch(self.root, claim_ids=list(self.BOTH),
                                       status='suggested')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        rec = self._claims()
        for cid in self.BOTH:
            self.assertEqual(rec[cid]['status'], 'suggested')

    def test_multi_id_with_field_flag_refused_nothing_written(self) -> None:
        before = self.source.read_text(encoding='utf-8')
        result = claim.run_claim_batch(self.root, claim_ids=list(self.BOTH),
                                       status='accepted', value='corrected')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(result.changed, [])
        self.assertEqual(self.source.read_text(encoding='utf-8'), before)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('--value', text)
        self.assertIn('per-claim', text)

    def test_multi_id_with_confidence_refused_too(self) -> None:
        # Every field flag hits the same gate, not just --value.
        before = self.source.read_text(encoding='utf-8')
        result = claim.run_claim_batch(self.root, claim_ids=list(self.BOTH),
                                       status='accepted', confidence='high')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(self.source.read_text(encoding='utf-8'), before)
        self.assertIn('--confidence', ' '.join(m.text for m in result.messages))

    def test_one_unknown_id_refuses_the_whole_batch_before_any_write(self) -> None:
        before = self.source.read_text(encoding='utf-8')
        result = claim.run_claim_batch(
            self.root, claim_ids=['C-aa11bb22cc', 'C-0000000000'], status='accepted')
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result['status'], 'not-found')
        self.assertEqual(result.changed, [])
        # ALL-or-nothing: the known first claim was not touched either.
        self.assertEqual(self.source.read_text(encoding='utf-8'), before)
        self.assertEqual(self._claims()['C-aa11bb22cc']['status'], 'suggested')
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('c-0000000000', text)
        self.assertTrue(any(m.next_step and 'fha find' in m.next_step
                            for m in result.messages))

    def test_one_malformed_id_refuses_the_whole_batch(self) -> None:
        before = self.source.read_text(encoding='utf-8')
        result = claim.run_claim_batch(
            self.root, claim_ids=['C-aa11bb22cc', 'C-bad'], status='accepted')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result['status'], 'invalid-id')
        self.assertEqual(self.source.read_text(encoding='utf-8'), before)
        self.assertIn('C-bad', ' '.join(m.text for m in result.messages))

    def test_dry_run_batch_writes_nothing_and_previews_each(self) -> None:
        before = self.source.read_text(encoding='utf-8')
        result = claim.run_claim_batch(self.root, claim_ids=list(self.BOTH),
                                       status='accepted', dry_run=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result.changed, [])
        self.assertEqual(self.source.read_text(encoding='utf-8'), before)
        text = ' '.join(m.text for m in result.messages)
        for cid in self.BOTH:
            self.assertIn(cid.lower(), text)
        self.assertIn('[dry-run]', text)
        # One closing trailer for the whole batch, not one per claim.
        trailers = [m for m in result.messages if 'Re-run without --dry-run' in m.text]
        self.assertEqual(len(trailers), 1)

    def test_duplicate_id_deduped_preserving_order_with_a_note(self) -> None:
        result = claim.run_claim_batch(
            self.root, claim_ids=['C-aa11bb22cc', 'C-bb22cc33dd', 'C-AA11BB22CC'],
            status='accepted')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result['applied'], [c.lower() for c in self.BOTH])
        self.assertEqual(len(result['results']), 2)
        rec = self._claims()
        for cid in self.BOTH:
            self.assertEqual(rec[cid]['status'], 'accepted')
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('more than once', text)

    def test_duplicates_of_one_claim_behave_as_single_id(self) -> None:
        # After dedupe only one distinct claim remains, so this is the plain
        # single-claim contract (run_claim's data shape) plus the dedupe note.
        result = claim.run_claim_batch(
            self.root, claim_ids=['C-aa11bb22cc', 'C-aa11bb22cc'], status='accepted')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result['claim_id'], 'c-aa11bb22cc')
        self.assertEqual(self._claims()['C-aa11bb22cc']['status'], 'accepted')
        self.assertIn('more than once', result.messages[0].text)

    def test_multi_id_without_status_refused(self) -> None:
        before = self.source.read_text(encoding='utf-8')
        result = claim.run_claim_batch(self.root, claim_ids=list(self.BOTH))
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result['status'], 'no-op')
        self.assertEqual(self.source.read_text(encoding='utf-8'), before)
        self.assertIn('--status', ' '.join(m.text for m in result.messages))

    def test_batch_bad_reviewed_date_refused_before_any_write(self) -> None:
        before = self.source.read_text(encoding='utf-8')
        result = claim.run_claim_batch(self.root, claim_ids=list(self.BOTH),
                                       status='accepted', reviewed='not-a-date')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(self.source.read_text(encoding='utf-8'), before)

    def test_single_id_through_the_batch_front_door_is_unchanged(self) -> None:
        # _cmd_claim now routes every call through run_claim_batch; a single
        # id must keep run_claim's exact data shape and behavior.
        result = claim.run_claim_batch(self.root, claim_ids=['C-aa11bb22cc'],
                                       status='rejected')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['claim_id'], 'c-aa11bb22cc')
        self.assertEqual(result['before_status'], 'suggested')
        self.assertEqual(self._claims()['C-aa11bb22cc']['status'], 'rejected')

    def test_single_id_field_edit_through_the_batch_front_door_still_legal(self) -> None:
        # The status-only gate binds BATCHES; one id keeps its field edits.
        result = claim.run_claim_batch(self.root, claim_ids=['C-aa11bb22cc'],
                                       place_text='Topeka')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(self._claims()['C-aa11bb22cc']['place_text'], 'Topeka')

    def test_index_reminder_appears_once_for_the_whole_batch(self) -> None:
        result = claim.run_claim_batch(self.root, claim_ids=list(self.BOTH),
                                       status='accepted')
        reminders = [m for m in result.messages if 'fha index' in m.text]
        self.assertEqual(len(reminders), 1)

    def _write_duplicate_source(self) -> Path:
        # A pre-existing E001 duplicate C-id on the SECOND batch member: it
        # passes the up-front existence gate but run_claim refuses it
        # mid-loop - the one realistic route into the stop branch.
        path = self.root / 'sources' / 'other' / 'dup-source_S-2222222222.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '---\nid: S-2222222222\ntitle: Dup Source\nsource_type: other\n---\n\n'
            '## Claims\n```yaml\n'
            '- value: "Fact one"\n  id: C-dd44ee55ff\n  type: note\n'
            '  persons: [P-aaaaaaaaaa]\n  status: suggested\n  confidence: medium\n'
            '- value: "Fact two"\n  id: C-ee55ff66gh\n  type: note\n'
            '  persons: [P-aaaaaaaaaa]\n  status: suggested\n  confidence: medium\n'
            '- value: "Fact two twin"\n  id: C-ee55ff66gh\n  type: note\n'
            '  persons: [P-aaaaaaaaaa]\n  status: suggested\n  confidence: medium\n'
            '```\n', encoding='utf-8')
        return path

    def test_live_mid_batch_stop_names_applied_and_finish_command(self) -> None:
        dup = self._write_duplicate_source()
        result = claim.run_claim_batch(
            self.root, claim_ids=['C-dd44ee55ff', 'C-ee55ff66gh'],
            status='accepted')
        self.assertNotEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result['applied'], ['c-dd44ee55ff'])
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('Stopped at c-ee55ff66gh', text)
        self.assertIn('1 of 2 claims applied (c-dd44ee55ff)', text)
        # The finish command carries ONLY the unapplied ids.
        self.assertIn('`fha claim c-ee55ff66gh --status accepted`', text)
        self.assertNotIn('fha claim c-dd44ee55ff', text)
        claims = read_record(dup)['claims']
        self.assertEqual(claims[0]['status'], 'accepted')     # applied stays applied
        self.assertEqual(claims[1]['status'], 'suggested')    # refused stays untouched
        self.assertEqual(claims[2]['status'], 'suggested')

    def test_dry_run_mid_batch_stop_says_previewed_with_full_rerun(self) -> None:
        # The stop branch under --dry-run must never claim anything was
        # "applied" nor print a recovery command that drops the previewed
        # ids - a human following it would silently lose the first decision.
        dup = self._write_duplicate_source()
        before = dup.read_text(encoding='utf-8')
        result = claim.run_claim_batch(
            self.root, claim_ids=['C-dd44ee55ff', 'C-ee55ff66gh'],
            status='accepted', reviewed='2026-07-20', dry_run=True)
        self.assertNotEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(dup.read_text(encoding='utf-8'), before)   # zero writes
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('nothing was written (dry-run)', text)
        self.assertNotIn('claims applied', text)
        self.assertIn(
            '`fha claim c-dd44ee55ff c-ee55ff66gh --status accepted '
            '--reviewed 2026-07-20 --dry-run`', text)

    def test_empty_id_list_is_a_plain_refusal(self) -> None:
        result = claim.run_claim_batch(self.root, claim_ids=[], status='accepted')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertTrue(result.messages)


_THIRD_CLAIM = """
- value: "Anna Smith, a farmer's daughter"
  id: C-cc33dd44ee
  type: occupation
  persons: [P-aaaaaaaaaa]
  status: suggested
  confidence: medium
```"""


def _write_three_claim_source(root: Path, sid: str = 'S-1111111111',
                              prefix: str = '') -> tuple[Path, tuple[str, str, str]]:
    """A record holding three claims; `prefix` swaps the ids' first letters
    so a second record's claims never collide with the first's."""
    ids = ('C-aa11bb22cc', 'C-bb22cc33dd', 'C-cc33dd44ee')
    text = _CLAIM_BLOCK.replace('```\n\n## Notes', _THIRD_CLAIM + '\n\n## Notes', 1)
    text = text.replace('S-1111111111', sid)
    if prefix:
        new_ids = tuple('C-' + prefix + cid[3:] for cid in ids)
        for old, new in zip(ids, new_ids):
            text = text.replace(old, new)
        ids = new_ids
    path = root / 'sources' / 'other' / f'record-{sid.lower()}_{sid}.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path, ids


class OneWritePerRecordTests(unittest.TestCase):
    """#225 follow-up: a batch used to rewrite a record once per claim in it, a
    fraction of a second apart - and on Windows a rewrite can find antivirus
    still reading the last one, so a big batch stopped part way. A batch now
    edits every claim in memory and writes each record once, with the same
    result, the same messages and the same stop-and-report when a write fails."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.first, self.a = _write_three_claim_source(self.root)
        self.second, self.b = _write_three_claim_source(self.root, 'S-2222222222', prefix='d')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _spy(self, fail_on: Path | None = None):
        writes: list[Path] = []
        real = claim.write_text_exact_atomic

        def spy(path, text):
            writes.append(Path(path))
            if fail_on is not None and Path(path) == fail_on:
                raise PermissionError(13, 'Access is denied', str(path))
            return real(path, text)
        return writes, mock.patch.object(claim, 'write_text_exact_atomic', spy)

    def _status(self, path: Path) -> dict:
        return {c['id']: str(c['status']) for c in read_record(path)['claims']}

    def test_three_claims_in_one_record_write_it_once(self) -> None:
        # Expected bytes: the same three accepts made one claim at a time,
        # which is exactly what the batch did before (one run_claim write each).
        with tempfile.TemporaryDirectory() as d:
            twin, _ = _write_three_claim_source(Path(d))
            for cid in self.a:
                one = claim.run_claim(Path(d), claim_id=cid, status='accepted',
                                      reviewed='2026-07-20')
                self.assertEqual(one['status'], 'ok')
            expected = twin.read_bytes()

        writes, spy = self._spy()
        with spy:
            result = claim.run_claim_batch(self.root, claim_ids=list(self.a),
                                           status='accepted', reviewed='2026-07-20')
        self.assertEqual(writes, [self.first])
        self.assertEqual(self.first.read_bytes(), expected)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result['applied'], [c.lower() for c in self.a])
        self.assertEqual(result.changed, [str(self.first)])
        text = '\n'.join(m.text for m in result.messages)
        for cid in self.a:
            self.assertIn(f'Set {cid.lower()}: status suggested -> accepted (reviewed 2026-07-20)', text)

    def test_a_failed_write_on_the_second_record_keeps_the_first(self) -> None:
        # Ids interleave the two records: both of the first record's claims
        # land in its one write, then the second record's write fails.
        ids = [self.a[0], self.b[0], self.a[1]]
        before_second = self.second.read_bytes()
        writes, spy = self._spy(fail_on=self.second)
        with spy:
            result = claim.run_claim_batch(self.root, claim_ids=ids,
                                           status='accepted', reviewed='2026-07-20')
        self.assertEqual(writes, [self.first, self.second])
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['applied'], [self.a[0].lower(), self.a[1].lower()])
        self.assertEqual(result.changed, [str(self.first)])
        first = self._status(self.first)
        self.assertEqual((first[self.a[0]], first[self.a[1]], first[self.a[2]]),
                         ('accepted', 'accepted', 'suggested'))
        self.assertEqual(self.second.read_bytes(), before_second)
        self.assertEqual([d['status'] for d in result['results']], ['ok', 'failed', 'ok'])

        text = '\n'.join(m.text for m in result.messages)
        self.assertIn(f'cannot write {self.second}', text)
        self.assertIn('Access is denied', text)
        self.assertIn(f'Stopped at {self.b[0].lower()}: 2 of 3 claims applied '
                      f'({self.a[0].lower()}, {self.a[1].lower()}), the rest left as they were', text)
        self.assertIn(f'`fha claim {self.b[0].lower()} --status accepted --reviewed 2026-07-20`', text)
        # Only claims whose record really was written say "Set".
        self.assertNotIn(f'Set {self.b[0]}', text)

    def test_records_after_a_failed_write_are_not_written(self) -> None:
        third, c = _write_three_claim_source(self.root, 'S-3333333333', prefix='f')
        before_third = third.read_bytes()
        ids = [self.a[0], self.b[0], c[0]]
        writes, spy = self._spy(fail_on=self.second)
        with spy:
            result = claim.run_claim_batch(self.root, claim_ids=ids, status='rejected')
        self.assertEqual(writes, [self.first, self.second])   # never reached the third
        self.assertEqual(third.read_bytes(), before_third)
        self.assertEqual(result['applied'], [self.a[0].lower()])
        self.assertEqual([d['status'] for d in result['results']],
                         ['ok', 'failed', 'not-written'])
        self.assertIn(f'`fha claim {self.b[0].lower()} {c[0].lower()} --status rejected`',
                      ' '.join(m.text for m in result.messages))

    def _edit_meanwhile(self, path: Path, after_calls: int):
        """Patch run_claim so that, after `after_calls` claims are edited in
        memory, someone else saves a note into `path` - a hand edit, or a
        second session's `fha claim` - before the batch writes anything."""
        real = claim.run_claim
        seen = {'n': 0}

        def run_then_edit(*a, **k):
            out = real(*a, **k)
            seen['n'] += 1
            if seen['n'] == after_calls:
                text = path.read_text(encoding='utf-8')
                path.write_text(text.replace('*(none yet)*', 'A NOTE TYPED MEANWHILE'),
                                encoding='utf-8')
            return out
        return mock.patch.object(claim, 'run_claim', run_then_edit)

    def test_an_edit_made_while_the_batch_runs_is_never_overwritten(self) -> None:
        # Writing each record once means holding its edits in memory for a
        # moment; a save that lands in that moment must survive, and the batch
        # must stop plainly instead of quietly writing over it.
        with self._edit_meanwhile(self.first, after_calls=1):
            result = claim.run_claim_batch(self.root, claim_ids=[self.a[0], self.a[1]],
                                           status='accepted', reviewed='2026-07-20')
        text = self.first.read_text(encoding='utf-8')
        self.assertIn('A NOTE TYPED MEANWHILE', text)
        self.assertNotIn('status: accepted', text)
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['applied'], [])
        self.assertEqual(result.changed, [])
        msgs = '\n'.join(m.text for m in result.messages)
        self.assertIn(f'{self.first} was changed by something else', msgs)
        self.assertIn('nothing in it was changed by this batch', msgs)
        self.assertIn(f'`fha claim {self.a[0].lower()} {self.a[1].lower()} --status accepted '
                      '--reviewed 2026-07-20`', msgs)
        # The rerun it names finishes the job and keeps the note.
        again = claim.run_claim_batch(self.root, claim_ids=[self.a[0], self.a[1]],
                                      status='accepted', reviewed='2026-07-20')
        self.assertEqual(again['status'], 'ok')
        text = self.first.read_text(encoding='utf-8')
        self.assertIn('A NOTE TYPED MEANWHILE', text)
        self.assertEqual(text.count('status: accepted'), 2)

    def test_records_before_the_changed_one_stay_written(self) -> None:
        with self._edit_meanwhile(self.second, after_calls=2):
            result = claim.run_claim_batch(self.root, claim_ids=[self.a[0], self.b[0]],
                                           status='accepted', reviewed='2026-07-20')
        self.assertEqual(result['applied'], [self.a[0].lower()])
        self.assertEqual(result.changed, [str(self.first)])
        self.assertEqual(self._status(self.first)[self.a[0]], 'accepted')
        self.assertIn('A NOTE TYPED MEANWHILE', self.second.read_text(encoding='utf-8'))
        self.assertEqual(self._status(self.second)[self.b[0]], 'suggested')
        self.assertEqual([d['status'] for d in result['results']], ['ok', 'failed'])
        self.assertIn(f'Stopped at {self.b[0].lower()}: 1 of 2 claims applied ({self.a[0].lower()})',
                      ' '.join(m.text for m in result.messages))

    def test_a_record_resaved_in_cp1252_meanwhile_stops_plainly(self) -> None:
        # Codex review, #234: a Windows editor saving the second record in
        # cp1252 (an accented name) mid-batch made the final check's UTF-8
        # read raise UnicodeDecodeError - a ValueError the write loop did not
        # catch - so the first record's report and the rerun command were lost.
        real = claim.run_claim
        seen = {'n': 0}

        def run_then_resave(*a, **k):
            out = real(*a, **k)
            seen['n'] += 1
            if seen['n'] == 2:
                text = self.second.read_text(encoding='utf-8')
                self.second.write_bytes(
                    text.replace('*(none yet)*', 'Renée visited').encode('cp1252'))
            return out
        with mock.patch.object(claim, 'run_claim', run_then_resave):
            result = claim.run_claim_batch(self.root, claim_ids=[self.a[0], self.b[0]],
                                           status='accepted', reviewed='2026-07-20')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result['applied'], [self.a[0].lower()])
        self.assertEqual(result.changed, [str(self.first)])
        self.assertEqual(self._status(self.first)[self.a[0]], 'accepted')
        self.assertIn(b'Ren\xe9e visited', self.second.read_bytes())   # left exactly as saved
        msgs = '\n'.join(m.text for m in result.messages)
        self.assertIn(f'Set {self.a[0].lower()}: status suggested -> accepted', msgs)
        self.assertIn(f'{self.second} was changed by something else', msgs)
        self.assertIn('It also is not saved as UTF-8 text', msgs)
        self.assertIn('W128', msgs)
        self.assertIn(f'Stopped at {self.b[0].lower()}: 1 of 2 claims applied ({self.a[0].lower()})', msgs)
        self.assertIn(f'`fha claim {self.b[0].lower()} --status accepted --reviewed 2026-07-20`', msgs)

    def test_a_record_that_will_not_decode_when_edited_is_a_plain_refusal(self) -> None:
        # The same encoding change landing between finding the claim and
        # reading the record to edit it: a refusal, never a traceback.
        real = claim._find_claim_record

        def find_then_resave(*a, **k):
            out = real(*a, **k)
            self.first.write_bytes(self.first.read_text(encoding='utf-8')
                                   .replace('*(none yet)*', 'Renée').encode('cp1252'))
            return out
        with mock.patch.object(claim, '_find_claim_record', find_then_resave):
            result = claim.run_claim(self.root, claim_id=self.a[0], status='accepted')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('is not saved as UTF-8 text', result.messages[-1].text)
        self.assertIn(b'Ren\xe9e', self.first.read_bytes())

    def test_dry_run_preview_is_each_claims_own_preview(self) -> None:
        # The preview is unchanged: each claim's diff against the file as it
        # stands, exactly what a single-claim dry run shows.
        ids = [self.a[0], self.b[0], self.a[1]]
        expected = []
        for cid in ids:
            one = claim.run_claim(self.root, claim_id=cid, status='accepted',
                                  reviewed='2026-07-20', dry_run=True)
            expected += [m.text for m in one.messages if m.text != claim._DRY_RUN_TRAILER]
        writes, spy = self._spy()
        with spy:
            result = claim.run_claim_batch(self.root, claim_ids=ids, status='accepted',
                                           reviewed='2026-07-20', dry_run=True)
        self.assertEqual(writes, [])
        self.assertEqual([m.text for m in result.messages][:-1], expected)


class BatchRerunIsANoOpTests(unittest.TestCase):
    """#225 follow-up: a batch that stopped part way is rerun with the same ids,
    and the claims it had already applied came back as "accepted -> accepted"
    with their files rewritten byte for byte. On Windows every needless
    rewrite is another chance to collide with a scanner still reading the last
    one, so a rerun kept failing on the same records. A claim that already says
    what was asked now writes nothing and counts as done."""

    BOTH = ('C-aa11bb22cc', 'C-bb22cc33dd')
    OLD = 1_600_000_000   # a fixed past mtime, so a rewrite cannot hide in the same tick

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = _write_source(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _accept_then_freeze(self) -> bytes:
        first = claim.run_claim_batch(self.root, claim_ids=list(self.BOTH),
                                      status='accepted', reviewed='2026-07-20')
        self.assertEqual(first['status'], 'ok')
        os.utime(self.source, (self.OLD, self.OLD))
        return self.source.read_bytes()

    def _counting_writer(self):
        calls: list[Path] = []
        real = claim.write_text_exact_atomic

        def spy(path, text):
            calls.append(Path(path))
            return real(path, text)
        return calls, mock.patch.object(claim, 'write_text_exact_atomic', spy)

    def test_rerunning_an_applied_batch_writes_no_file(self) -> None:
        before = self._accept_then_freeze()
        calls, spy = self._counting_writer()
        with spy:
            result = claim.run_claim_batch(self.root, claim_ids=list(self.BOTH),
                                           status='accepted', reviewed='2026-07-20')
        self.assertEqual(calls, [])
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(os.stat(self.source).st_mtime, self.OLD)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['applied'], [c.lower() for c in self.BOTH])   # done, not failed
        self.assertEqual([d['status'] for d in result['results']],
                         ['already-done', 'already-done'])
        self.assertEqual(result.changed, [])
        text = '\n'.join(m.text for m in result.messages)
        for cid in self.BOTH:
            self.assertIn(f'{cid.lower()}: already accepted (reviewed 2026-07-20)', text)
        self.assertIn('All 2 claims were already accepted with that review date', text)
        self.assertNotIn('fha index', text)

    def test_a_rerun_writes_only_the_claims_still_to_do(self) -> None:
        claim.run_claim(self.root, claim_id=self.BOTH[0], status='accepted',
                        reviewed='2026-07-20')
        result = claim.run_claim_batch(self.root, claim_ids=list(self.BOTH),
                                       status='accepted', reviewed='2026-07-20')
        self.assertEqual(result['applied'], [c.lower() for c in self.BOTH])
        self.assertEqual([d['status'] for d in result['results']], ['already-done', 'ok'])
        self.assertEqual(result.changed, [str(self.source)])
        rec = {c['id']: c for c in read_record(self.source)['claims']}
        self.assertEqual(rec[self.BOTH[1]]['status'], 'accepted')
        self.assertIn('1 of the 2 claims was already accepted with that review date - '
                      'nothing was rewritten for it.',
                      ' '.join(m.text for m in result.messages))

    def test_one_claim_already_done_is_a_success_with_no_write(self) -> None:
        before = self._accept_then_freeze()
        result = claim.run_claim(self.root, claim_id=self.BOTH[0], status='accepted',
                                 reviewed='2026-07-20')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertTrue(result.ok)
        self.assertEqual(result['status'], 'already-done')
        self.assertEqual(result.changed, [])
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(os.stat(self.source).st_mtime, self.OLD)

    def test_a_new_review_date_is_still_written(self) -> None:
        # Only an edit that changes nothing is skipped: re-accepting with a
        # different date is a real change to the record.
        self._accept_then_freeze()
        result = claim.run_claim_batch(self.root, claim_ids=list(self.BOTH),
                                       status='accepted', reviewed='2026-07-21')
        self.assertEqual([d['status'] for d in result['results']], ['ok', 'ok'])
        rec = {c['id']: c for c in read_record(self.source)['claims']}
        self.assertEqual(str(rec[self.BOTH[0]]['reviewed']), '2026-07-21')

    def test_an_already_done_edit_still_warns_about_a_broken_place_registry(self) -> None:
        claim.run_claim(self.root, claim_id=self.BOTH[0], place_text='Topeka, Kansas')
        _write_registry(self.root, 'not_a_list: true\n')
        result = claim.run_claim(self.root, claim_id=self.BOTH[0], place_text='Topeka, Kansas')
        self.assertEqual(result['status'], 'already-done')
        warnings = [m.text for m in result.messages if m.level == 'warning']
        self.assertTrue(any('places.yaml has a problem' in w for w in warnings))
        self.assertTrue(any('already said this' in w for w in warnings))
        self.assertFalse(any('was still written' in w for w in warnings))

    def test_dry_run_of_a_rerun_says_nothing_would_change(self) -> None:
        before = self._accept_then_freeze()
        result = claim.run_claim_batch(self.root, claim_ids=list(self.BOTH),
                                       status='accepted', reviewed='2026-07-20',
                                       dry_run=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(self.source.read_bytes(), before)
        text = '\n'.join(m.text for m in result.messages)
        self.assertIn('nothing would change', text)
        self.assertNotIn('Would set', text)


# ── Batch status moves: CLI routing (both parsers) ──────────────────────────────

class ClaimBatchCliRoutingTests(unittest.TestCase):
    """The batch positional must work through BOTH parsers - the fha subparser
    (register/_add_arguments) and the standalone tools/claim.py parser - and
    must not disturb the `fha claim new` early interception in fha.py."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8')
        self.source = _write_source(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _claims(self) -> dict:
        return {c['id']: c for c in read_record(self.source)['claims']}

    def test_fha_main_batch_accept(self) -> None:
        import fha
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fha.main(['claim', 'C-aa11bb22cc', 'C-bb22cc33dd',
                           '--status', 'accepted', '--root', str(self.root)])
        self.assertEqual(rc, EXIT_CLEAN)
        rec = self._claims()
        self.assertEqual(rec['C-aa11bb22cc']['status'], 'accepted')
        self.assertEqual(rec['C-bb22cc33dd']['status'], 'accepted')

    def test_fha_main_batch_with_field_flag_refused(self) -> None:
        import fha
        before = self.source.read_text(encoding='utf-8')
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fha.main(['claim', 'C-aa11bb22cc', 'C-bb22cc33dd',
                           '--status', 'accepted', '--value', 'nope',
                           '--root', str(self.root)])
        self.assertEqual(rc, EXIT_FAILURE)
        self.assertEqual(self.source.read_text(encoding='utf-8'), before)
        self.assertIn('per-claim', err.getvalue())

    def test_standalone_batch(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = claim._standalone_main(['C-aa11bb22cc', 'C-bb22cc33dd',
                                         '--status', 'suggested',
                                         '--root', str(self.root)])
        self.assertEqual(rc, EXIT_CLEAN)
        rec = self._claims()
        self.assertEqual(rec['C-aa11bb22cc']['status'], 'suggested')
        self.assertEqual(rec['C-bb22cc33dd']['status'], 'suggested')

    def test_fha_main_single_id_still_byte_compatible(self) -> None:
        import fha
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fha.main(['claim', 'C-aa11bb22cc', '--status', 'accepted',
                           '--root', str(self.root)])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(self._claims()['C-aa11bb22cc']['status'], 'accepted')

    def test_claim_new_interception_still_routes_before_the_batch_parser(self) -> None:
        import fha
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fha.main(['claim', 'new', '--source', 'S-1111111111', '--type', 'note',
                           '--value', 'still intercepted', '--root', str(self.root)])
        self.assertEqual(rc, EXIT_CLEAN)
        claims = read_record(self.source)['claims']
        self.assertTrue(any(c['value'] == 'still intercepted' for c in claims))


# ── Issue #50: --information/--evidence/--anchor/--notes ───────────────────────

class MillsFieldEditTests(unittest.TestCase):
    """The edit verb's new --information/--evidence/--anchor/--notes flags."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = _write_source(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _claims(self) -> dict:
        return {c['id']: c for c in read_record(self.source)['claims']}

    def test_information_evidence_anchor_notes_round_trip(self) -> None:
        result = claim.run_claim(
            self.root, claim_id='C-aa11bb22cc', information='secondary',
            evidence='indirect', anchor='p. 12', notes='Correlated against the marriage record.')
        self.assertEqual(result.exit_code, EXIT_CLEAN, result.messages)
        rec = self._claims()['C-aa11bb22cc']
        self.assertEqual(rec['information'], 'secondary')
        self.assertEqual(rec['evidence'], 'indirect')
        self.assertEqual(rec['anchor'], 'p. 12')
        self.assertEqual(rec['notes'], 'Correlated against the marriage record.')
        # Sibling claim and every other field on this one untouched.
        self.assertEqual(self._claims()['C-bb22cc33dd']['value'], 'Anna Smith died 1950')
        self.assertEqual(rec['value'], 'Anna Smith born 1880, Fairview')
        self.assertEqual(rec['status'], 'suggested')   # --status not given

    def test_invalid_information_refused_naming_valid_list(self) -> None:
        before = self.source.read_text(encoding='utf-8')
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', information='nope')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('primary', text)
        self.assertIn('secondary', text)
        self.assertIn('undetermined', text)
        self.assertEqual(self.source.read_text(encoding='utf-8'), before)

    def test_invalid_evidence_refused_naming_valid_list(self) -> None:
        before = self.source.read_text(encoding='utf-8')
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', evidence='nope')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('direct', text)
        self.assertIn('indirect', text)
        self.assertIn('negative', text)
        self.assertEqual(self.source.read_text(encoding='utf-8'), before)

    def test_notes_alone_is_enough_to_avoid_the_no_op_refusal(self) -> None:
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', notes='Just a note.')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(self._claims()['C-aa11bb22cc']['notes'], 'Just a note.')

    def test_notes_replaces_an_existing_multiline_block_scalar_cleanly(self) -> None:
        # SPEC §8.4's own illustrative claim uses `notes: >`; --value refuses
        # on a block-scalar (a deliberate, narrower conservatism kept as-is),
        # but --notes must NOT share that refusal - correcting notes on an
        # already-reviewed claim is exactly issue #50's use case. Round-trips
        # at both dash indents the block-scalar repro fixture covers.
        for pad in (1, 3):
            with self.subTest(pad=pad):
                text = _notes_repro_block(pad, place_text='Fairview, as written')
                new, changed = claim._apply_claim_review(
                    text, 'C-aa11bb22cc', notes='Replaced in one shot.')
                self.assertTrue(changed)
                claims = read_record_from_text(new)
                self.assertEqual(len(claims), 1)
                c = claims[0]
                self.assertEqual(c['notes'], 'Replaced in one shot.')
                # Nothing else on the claim was disturbed by the block collapse.
                self.assertEqual(c['place_text'], 'Fairview, as written')
                self.assertEqual(c['type'], 'birth')
                self.assertEqual(str(c['persons']), "['P-aaaaaaaaaa']")

    def test_batch_refuses_each_new_field_flag_with_more_than_one_id(self) -> None:
        for flag, kwargs in (
            ('--information', {'information': 'primary'}),
            ('--evidence', {'evidence': 'direct'}),
            ('--anchor', {'anchor': '00:14:32'}),
            ('--notes', {'notes': 'x'}),
        ):
            with self.subTest(flag=flag):
                before = self.source.read_text(encoding='utf-8')
                result = claim.run_claim_batch(
                    self.root, claim_ids=['C-aa11bb22cc', 'C-bb22cc33dd'],
                    status='accepted', **kwargs)
                self.assertEqual(result.exit_code, EXIT_FAILURE)
                self.assertEqual(result.data['status'], 'refused')
                self.assertIn(flag, result.messages[-1].text)
                self.assertEqual(self.source.read_text(encoding='utf-8'), before)

    def test_single_id_through_batch_front_door_still_takes_new_fields(self) -> None:
        # The existing single-claim-only guard must still pass a lone id
        # through untouched, new flags included (per the batch's own
        # "delegates straight to run_claim" contract).
        result = claim.run_claim_batch(
            self.root, claim_ids=['C-aa11bb22cc'], evidence='direct', anchor='p. 3')
        self.assertEqual(result.exit_code, EXIT_CLEAN, result.messages)
        rec = read_record(self.source)['claims'][0]
        self.assertEqual(rec['evidence'], 'direct')
        self.assertEqual(rec['anchor'], 'p. 3')


class MillsFieldCliTests(unittest.TestCase):
    """CLI wiring for --information/--evidence/--anchor/--notes."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        # resolve_root_arg (the CLI-layer path _cmd_claim/_cmd_claim_new use,
        # unlike run_claim/run_claim_new called directly) requires fha.yaml.
        (self.root / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8')
        self.source = _write_source(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_cli_edit_verb_writes_all_four(self) -> None:
        import fha
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = fha.main(['claim', 'C-aa11bb22cc', '--information', 'primary',
                           '--evidence', 'direct', '--anchor', '00:01:00',
                           '--notes', 'CLI-written note.', '--root', str(self.root)])
        self.assertEqual(rc, 0)
        rec = read_record(self.source)['claims'][0]
        self.assertEqual(rec['information'], 'primary')
        self.assertEqual(rec['evidence'], 'direct')
        self.assertEqual(rec['anchor'], '00:01:00')
        self.assertEqual(rec['notes'], 'CLI-written note.')

    def test_cli_new_verb_mints_all_four(self) -> None:
        import fha
        _write_person(self.root, 'P-aaaaaaaaaa', 'Anna Smith')
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = fha.main([
                'claim', 'new', '--source', 'S-1111111111', '--type', 'occupation',
                '--value', 'Bookkeeper', '--persons', 'P-aaaaaaaaaa',
                '--information', 'primary', '--evidence', 'direct',
                '--anchor', 'p. 4', '--notes', 'Listed in the 1874 directory.',
                '--root', str(self.root)])
        self.assertEqual(rc, 0)
        claims = read_record(self.source)['claims']
        minted = next(c for c in claims if c['value'] == 'Bookkeeper')
        self.assertEqual(minted['information'], 'primary')
        self.assertEqual(minted['evidence'], 'direct')
        self.assertEqual(minted['anchor'], 'p. 4')
        self.assertEqual(minted['notes'], 'Listed in the 1874 directory.')


class NewMillsFieldsLintCleanTests(unittest.TestCase):
    """A `claim new` with the full #50 field set must pass `fha lint` clean -
    the issue's own regression-test requirement (a claim minted WITHOUT these
    fields trips W106/W109; one minted WITH them should not)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        # lint.run_lint refuses outright (E010) with no fha.yaml at all.
        (self.root / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8')
        self.source = _write_source(self.root)
        _write_person(self.root, 'P-aaaaaaaaaa', 'Anna Smith')
        import lint
        self.lint = lint
        self.config = load_fha_yaml(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _findings_for(self, cid: str) -> set:
        result = self.lint.run_lint(self.root, self.config)
        return {m.code for m in result.messages if cid.lower() in m.text.lower()}

    def test_full_field_set_produces_no_w106_or_w109_on_this_claim(self) -> None:
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='occupation',
            value='Bookkeeper, Plains Junction Railroad', persons=['P-aaaaaaaaaa'],
            date='1874', status='accepted', confidence='high',
            information='primary', evidence='direct', anchor='p. 4',
            notes='Listed as book-keeper for the Plains Junction RR in the 1874 directory.')
        self.assertEqual(result.exit_code, EXIT_CLEAN, result.messages)
        codes = self._findings_for(result.data['claim_id'])
        self.assertNotIn('W106', codes)
        self.assertNotIn('W109', codes)

    def test_missing_field_set_does_trip_w106_control(self) -> None:
        # Control: the SAME mint, minus information/evidence/notes, DOES trip
        # W106 (missing Mills fields) - proves the clean result above is the
        # fields' doing, not a fluke of the fixture/lint pass.
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='occupation',
            value='Bookkeeper, Plains Junction Railroad', persons=['P-aaaaaaaaaa'],
            date='1874', status='accepted', confidence='high')
        self.assertEqual(result.exit_code, EXIT_CLEAN, result.messages)
        codes = self._findings_for(result.data['claim_id'])
        self.assertIn('W106', codes)


# ── Issue #50: --negated / --evidence conflict on claim new ────────────────────

class NegatedEvidenceConflictTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = _write_source(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_negated_with_contradicting_evidence_refused(self) -> None:
        before = self.source.read_text(encoding='utf-8')
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='marriage',
            value='no marriage found', negated=True, evidence='direct')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'refused')
        self.assertIn('negative', result.messages[-1].text)
        self.assertEqual(self.source.read_text(encoding='utf-8'), before)

    def test_negated_with_agreeing_evidence_negative_is_fine(self) -> None:
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='marriage',
            value='no marriage found', negated=True, evidence='negative')
        self.assertEqual(result.exit_code, EXIT_CLEAN, result.messages)
        claims = read_record(self.source)['claims']
        minted = next(c for c in claims if c['value'] == 'no marriage found')
        self.assertEqual(minted['evidence'], 'negative')
        # read_record coerces YAML booleans to 'true'/'false' strings
        # (_lib._coerce_yaml) - matching test_negated_mints_confirmed_absence_
        # with_evidence_pairing's own convention elsewhere in this file.
        self.assertEqual(str(minted.get('negated')), 'true')


# ── Issue #54: echo the written value on success ────────────────────────────

class EchoOnSuccessTests(unittest.TestCase):
    """The exact PowerShell single-quote de-quoting repro from issue #54:
    `Calvin "Cal" Hartley` arrives at the tool as
    `Calvin Cal Hartley` (embedded double quotes lost before this
    tool ever sees the argument). Guard-proven: before this fix, the two
    runs below produced byte-IDENTICAL success messages ('Set C-…: value
    updated') - captured against `tools/claim.py` as of the branch point
    (2026-07 baseline, pre-#54): both a correctly-quoted and a de-quoted
    --value landed with the message
    'Set C-aa11bb22cc: value updated | Reminder: ...', so a human (or a
    script) reading the CLI's own output had no way to tell the write was
    wrong. This class pins the fixed behavior: the actual written value now
    appears in the message either way, so the corruption is visible."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = _write_source(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_value_success_message_now_shows_the_written_value(self) -> None:
        correct = 'Calvin "Cal" Hartley'
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', value=correct)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        all_text = ' '.join(m.text for m in result.messages)
        self.assertIn(correct, all_text)

    def test_a_mangled_value_is_now_visibly_different_from_a_correct_one(self) -> None:
        # The corrupted write LOOKS different in the output now - that is
        # the whole fix. The source is re-seeded between the two writes so
        # both start from the exact same claim state and are comparable.
        correct = 'Calvin "Cal" Hartley'
        mangled = 'Calvin Cal Hartley'   # what PowerShell 5.1 delivers
        r_correct = claim.run_claim(self.root, claim_id='C-aa11bb22cc', value=correct)
        self.source.write_text(_CLAIM_BLOCK, encoding='utf-8')
        r_mangled = claim.run_claim(self.root, claim_id='C-aa11bb22cc', value=mangled)
        text_correct = ' '.join(m.text for m in r_correct.messages)
        text_mangled = ' '.join(m.text for m in r_mangled.messages)
        self.assertNotEqual(text_correct, text_mangled)
        self.assertIn(correct, text_correct)
        self.assertIn(mangled, text_mangled)
        self.assertNotIn(correct, text_mangled)

    def test_place_text_success_message_also_echoes(self) -> None:
        # place_text was the other field this fix covers (said "place_text
        # updated" before, same blind spot as value).
        value = 'Fairview City, Breton Co., Kansas'
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', place_text=value)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        all_text = ' '.join(m.text for m in result.messages)
        self.assertIn(value, all_text)

    def test_anchor_and_notes_echo_on_success(self) -> None:
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc',
                                 anchor='00:14:32', notes='Context that must be visible.')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        all_text = ' '.join(m.text for m in result.messages)
        self.assertIn('00:14:32', all_text)
        self.assertIn('Context that must be visible.', all_text)

    def test_dry_run_still_shows_the_full_diff_no_duplicate_echo_needed(self) -> None:
        # --dry-run already reveals the exact written bytes via its diff -
        # the extra echo line is a live-write-only addition (dry-run was
        # never the blind spot the issue reported).
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc',
                                 value='Preview Value', dry_run=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        all_text = '\n'.join(m.text for m in result.messages)
        self.assertIn('Preview Value', all_text)
        self.assertIn('[dry-run]', all_text)

    def test_claim_new_live_mint_also_echoes_value(self) -> None:
        # Extended per the same principle to the mint path (`fha claim new`)
        # - a live mint's summary line never showed the value it just wrote
        # either, the identical blind spot one level over.
        _write_person(self.root, 'P-aaaaaaaaaa', 'Anna Smith')
        value = 'Calvin "Cal" Hartley, laborer'
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='occupation',
            value=value, persons=['P-aaaaaaaaaa'])
        self.assertEqual(result.exit_code, EXIT_CLEAN, result.messages)
        all_text = ' '.join(m.text for m in result.messages)
        self.assertIn(value, all_text)

    def test_claim_new_live_mint_also_shows_information_and_evidence(self) -> None:
        # `run_claim`'s edit verb already named --information/--evidence
        # inline in its summary; `run_claim_new`'s own summary line omitted
        # both entirely (unlike --value/--place-text/--anchor/--notes, which
        # got the explicit echo line) - the same issue #54 blind spot on the
        # mint path, just for the two closed-vocabulary Mills fields instead
        # of a free-text one.
        _write_person(self.root, 'P-aaaaaaaaaa', 'Anna Smith')
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='occupation',
            value='Bookkeeper', persons=['P-aaaaaaaaaa'],
            information='primary', evidence='direct')
        self.assertEqual(result.exit_code, EXIT_CLEAN, result.messages)
        all_text = ' '.join(m.text for m in result.messages)
        self.assertIn('information -> primary', all_text)
        self.assertIn('evidence -> direct', all_text)


from _lib import format_edtf_error, normalize_date  # noqa: E402  (#216 tests)


class BackwardsIntervalTests(unittest.TestCase):
    """#216: a range typed backwards is refused, in plain words, everywhere.

    "Between 1940 and 1950" typed as `1950/1940`, or a death's two candidate
    days in the wrong order, used to be written without a word: both halves
    are fine dates, and nothing compared them. The fact then sorted in the
    wrong place and fell out of every date-window search. An interval must
    run forwards (TOOLING §1): its start may not be wholly after its end.
    """

    BACKWARDS = ('1950/1940', '1941-01-19/1941-01-18', '1950-06/1950-03')
    # Some reading makes each of these forwards, so none is refused.
    FORWARDS = ('1940/1950', '1950/1950', '1950-06/1950', '1941~/1940',
                '1950/1950-03', '[..1920]/1930')

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = _write_source(self.root)
        _write_person(self.root, 'P-aaaaaaaaaa', 'Anna Smith')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _date_of(self, cid: str):
        claims = {c['id']: c for c in read_record(self.source)['claims']}
        return claims[cid].get('date')

    def test_a_backwards_interval_is_not_a_date_the_archive_reads(self) -> None:
        for form in self.BACKWARDS:
            with self.subTest(form=form):
                self.assertFalse(is_valid_edtf(form))
                self.assertIsNone(normalize_date(form))

    def test_a_forwards_or_overlapping_interval_still_reads(self) -> None:
        for form in self.FORWARDS:
            with self.subTest(form=form):
                self.assertTrue(is_valid_edtf(form))

    def test_plain_words_backwards_are_refused_not_swapped(self) -> None:
        # Either year may be the typo, so the tool must not guess 1940/1950.
        for words in ('1950 to 1940', 'between 1950 and 1940', '1950-1940'):
            with self.subTest(words=words):
                self.assertIsNone(normalize_date(words))
        self.assertEqual(normalize_date('1940 to 1950'), '1940/1950')

    def test_the_message_says_it_runs_backwards_and_offers_the_swap(self) -> None:
        message = format_edtf_error('1950/1940')
        self.assertIn('runs backwards', message)
        self.assertIn('1940/1950', message)
        self.assertIn('typo', message)
        words = format_edtf_error('between 1950 and 1940', field='--date')
        self.assertIn('runs backwards', words)

    def test_a_hedged_backwards_range_is_explained_as_backwards_too(self) -> None:
        # #217 audit: normalize_date reads "circa"/"about"/"the" off first, so
        # the explanation must too - not the generic "not a date" message.
        for words in ('circa 1950 to 1940', 'about 1950-1940', 'the 1950 to 1940',
                      'maybe 1950 to 1940'):
            with self.subTest(words=words):
                self.assertIsNone(normalize_date(words))
                self.assertIn('runs backwards', format_edtf_error(words))

    def test_fha_claim_date_refuses_and_leaves_the_claim_alone(self) -> None:
        before = self.source.read_bytes()
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', date='1950/1940')
        self.assertNotEqual(result.exit_code, EXIT_CLEAN)
        self.assertIn('runs backwards', ' '.join(m.text for m in result.messages))
        self.assertEqual(self.source.read_bytes(), before)

    def test_fha_claim_date_keeps_the_hedge_on_a_range(self) -> None:
        # #293 review: 'about 1891-1893' used to be written as 1891/1893.
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc',
                                 date='about 1891-1893')
        self.assertEqual(result.exit_code, EXIT_CLEAN,
                         [m.text for m in result.messages])
        self.assertEqual(str(self._date_of('C-aa11bb22cc')), '1891~/1893~')

    def test_fha_claim_new_date_refuses(self) -> None:
        before = self.source.read_bytes()
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='residence',
            value='Lived in Topeka', date='1941-01-19/1941-01-18')
        self.assertNotEqual(result.exit_code, EXIT_CLEAN)
        self.assertIn('runs backwards', ' '.join(m.text for m in result.messages))
        self.assertEqual(self.source.read_bytes(), before)

    def test_lint_reports_a_hand_edited_backwards_range_as_e014(self) -> None:
        import lint
        text = self.source.read_text(encoding='utf-8')
        self.assertIn('  date: 1880\n', text)
        text = text.replace('  date: 1880\n', '  date: 1950/1940\n')
        self.source.write_text(text, encoding='utf-8')
        (self.root / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
        result = lint.run_lint(self.root, load_fha_yaml(self.root))
        e014 = [m.text for m in result.messages if m.code == 'E014']
        self.assertEqual(len(e014), 1, e014)
        self.assertIn('runs backwards', e014[0])


from _lib import edtf_bounds  # noqa: E402  (#206 tests)


class OnOrAfterDateTests(unittest.TestCase):
    """#206: "on or after", the mirror of [..X], is a date the archive keeps.

    A burial record often gives only the death date, so the honest burial
    date is "on or after 30 Sep 1880". The archive used to reject every way
    of writing that (E014), leaving two bad choices: drop the date, or
    invent an upper bound. SPEC §11 now has the After row, `[1920..]`.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = _write_source(self.root)
        _write_person(self.root, 'P-aaaaaaaaaa', 'Anna Smith')
        (self.root / 'fha.yaml').write_text('roots:\n  documents: documents\n',
                                           encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _claims(self) -> dict:
        return {c['id']: c for c in read_record(self.source)['claims']}

    def _e014(self) -> list[str]:
        import lint
        result = lint.run_lint(self.root, load_fha_yaml(self.root))
        return [m.text for m in result.messages if m.code == 'E014']

    def test_the_after_form_is_a_date_the_archive_reads(self) -> None:
        for form in ('[1920..]', '[1920-06..]', '[1880-09-30..]'):
            with self.subTest(form=form):
                self.assertTrue(is_valid_edtf(form))
                self.assertEqual(normalize_date(form), form)

    def test_plain_words_after_normalize_like_before_does(self) -> None:
        self.assertEqual(normalize_date('after 1920'), '[1920..]')
        self.assertEqual(normalize_date('after 1920-06-15'), '[1920-06-15..]')
        self.assertEqual(normalize_date('on or after 30 Sep 1880'), '[1880-09-30..]')
        self.assertEqual(normalize_date('before 1920'), '[..1920]')

    def test_the_open_interval_spelling_is_not_a_synonym(self) -> None:
        # One spelling per meaning (SPEC §11 decision log).
        self.assertFalse(is_valid_edtf('1920/..'))
        self.assertIsNone(normalize_date('1920/..'))

    def test_an_impossible_after_bound_is_refused(self) -> None:
        self.assertFalse(is_valid_edtf('[1900-02-31..]'))
        self.assertFalse(is_valid_edtf('[1900-13..]'))

    def test_bounds_start_at_the_date_and_stay_open(self) -> None:
        self.assertEqual(edtf_bounds('[1920..]'), ('1920-01-01', '9999-12-31'))
        self.assertEqual(edtf_bounds('[1880-09-30..]'), ('1880-09-30', '9999-12-31'))

    def test_the_open_forms_do_not_trip_the_backwards_check(self) -> None:
        # #216's backwards-range refusal must never misfire on an open bound.
        for form in ('[1920..]', '[..1920]', '1910/[1920..]', '[..1900]/1910'):
            with self.subTest(form=form):
                self.assertTrue(is_valid_edtf(form))
        # ...but an interval that starts after it ends is still backwards.
        self.assertFalse(is_valid_edtf('[1920..]/1910'))

    def test_an_after_date_ranks_below_a_dated_claim_for_the_headline(self) -> None:
        from _lib import vital_pick_key
        exact = {'date_min': '1880-01-01', 'date_max': '1880-12-31',
                 'confidence': 'low', 'id': 'C-zzzzzzzzzz'}
        after_min, after_max = edtf_bounds('[1880..]')
        after = {'date_min': after_min, 'date_max': after_max,
                 'confidence': 'high', 'id': 'C-0000000000'}
        self.assertLess(vital_pick_key(exact), vital_pick_key(after))

    def test_it_reads_as_after_in_plain_words(self) -> None:
        from _lib import humanize_edtf
        self.assertEqual(humanize_edtf('[1920..]'), 'after 1920')

    def test_fha_claim_date_round_trips_quoted_and_lint_is_clean(self) -> None:
        for given, stored in (('[1920..]', '[1920..]'),
                              ('after 1920-06-15', '[1920-06-15..]')):
            with self.subTest(given=given):
                result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', date=given)
                self.assertEqual(result.exit_code, EXIT_CLEAN, result.messages)
                self.assertEqual(str(self._claims()['C-aa11bb22cc']['date']), stored)
                # Bare, YAML would read the bracket as a list (#205).
                self.assertIn(f"  date: '{stored}'\n",
                              self.source.read_text(encoding='utf-8'))
                self.assertEqual(self._e014(), [])

    def test_fha_claim_new_round_trips(self) -> None:
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='residence',
            value='Lived in Topeka', date='after 1920')
        self.assertEqual(result.exit_code, EXIT_CLEAN, result.messages)
        self.assertEqual(str(self._claims()[result['claim_id']]['date']), '[1920..]')
        self.assertEqual(self._e014(), [])

    def test_a_burial_on_or_after_the_death_date(self) -> None:
        # TI-068: the record gives only the death date (30 Sep 1880), so the
        # burial is "on or after" it - kept as such, not dropped, not bounded.
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='burial',
            value='Buried after her death on 30 Sep 1880', persons=['P-aaaaaaaaaa'],
            date='on or after 30 Sep 1880')
        self.assertEqual(result.exit_code, EXIT_CLEAN, result.messages)
        stored = str(self._claims()[result['claim_id']]['date'])
        self.assertEqual(stored, '[1880-09-30..]')
        self.assertEqual(edtf_bounds(stored)[0], '1880-09-30')
        self.assertEqual(self._e014(), [])

    def test_gedcom_exports_aft(self) -> None:
        import gedcom
        self.assertEqual(gedcom._edtf_to_gedcom('[1920..]'), 'AFT 1920')
        self.assertEqual(gedcom._edtf_to_gedcom('[1880-09-30..]'), 'AFT 30 SEP 1880')
        self.assertEqual(gedcom._edtf_to_gedcom('[..1920]'), 'BEF 1920')

    def test_gedcom_import_reads_aft_back(self) -> None:
        import gedcom_import
        self.assertEqual(gedcom_import.gedcom_date_to_edtf('AFT 30 SEP 1880'), '[1880-09-30..]')

    def test_site_prose_reads_a_stray_after_bracket_in_words(self) -> None:
        import importlib.util
        spec = importlib.util.spec_from_file_location('fha_site_206', ROOT / 'tools' / 'site.py')
        site_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(site_mod)
        self.assertEqual(site_mod._scrub_internal_encoding('Buried [1880-09-30..] at Fairview'),
                         'Buried after September 30, 1880 at Fairview')
        # An impossible bound is left as written, never guessed at.
        self.assertEqual(site_mod._scrub_internal_encoding('[1900-02-31..]'), '[1900-02-31..]')

    def test_the_index_sorts_it_at_its_start_date(self) -> None:
        # Timeline order is ORDER BY date_min: the after-form sorts where it
        # starts, between a claim dated before it and one dated after it.
        import index
        claim.run_claim(self.root, claim_id='C-aa11bb22cc', date='1870')
        claim.run_claim(self.root, claim_id='C-bb22cc33dd', date='1890')
        new = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='residence',
            value='Lived in Topeka', date='[1880..]')
        index.build_index(self.root, load_fha_yaml(self.root))
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        try:
            rows = conn.execute(
                'SELECT id, date_min, date_max FROM claims ORDER BY date_min').fetchall()
        finally:
            conn.close()
        self.assertEqual([r[0] for r in rows],
                         ['c-aa11bb22cc', new['claim_id'].lower(), 'c-bb22cc33dd'])
        self.assertEqual(rows[1][1:], ('1880-01-01', '9999-12-31'))

    def test_lint_glosses_a_loose_after_date(self) -> None:
        text = self.source.read_text(encoding='utf-8')
        self.source.write_text(text.replace('  date: 1880\n', '  date: after 1880\n'),
                               encoding='utf-8')
        import lint
        result = lint.run_lint(self.root, load_fha_yaml(self.root))
        w109 = [m.text for m in result.messages if m.code == 'W109']
        self.assertEqual(len(w109), 1, w109)
        self.assertIn("'[1880..]' (on or after 1880)", w109[0])


class OnOrAfterAuditTests(unittest.TestCase):
    """#218 audit: the after-form never silently changes a family fact.

    "after 1880s" was stored as 188X (sometime IN the 1880s) and "after
    1880-1885" as the range itself - the word "after" dropped without a
    sign. An interval ending in an after-form was exported to GEDCOM as a
    closed BET range, and a report showed "9999-12-31" as if it were a date.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = _write_source(self.root)
        _write_person(self.root, 'P-aaaaaaaaaa', 'Anna Smith')
        (self.root / 'fha.yaml').write_text('roots:\n  documents: documents\n',
                                           encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_before_or_after_on_a_decade_or_range_is_refused_not_dropped(self) -> None:
        for words in ('after 1880s', 'after 187x', 'after 1880-1885', 'after 1880 to 1885',
                      'after between 1880 and 1885', 'before 1880s', 'before 1880-1885'):
            with self.subTest(words=words):
                self.assertIsNone(normalize_date(words))
                message = format_edtf_error(words, field='--date')
                self.assertIn('single date', message)
        # The single-date forms are unaffected.
        self.assertEqual(normalize_date('after 1880'), '[1880..]')
        self.assertEqual(normalize_date('before 1880-06'), '[..1880-06]')

    def test_the_refusal_names_the_two_readings(self) -> None:
        message = format_edtf_error('after 1880s', field='--date')
        self.assertIn('"after 1880" (stored as [1880..])', message)
        self.assertIn('"after 1889"', message)

    def test_fha_claim_new_refuses_after_a_decade_and_writes_nothing(self) -> None:
        before = self.source.read_bytes()
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='burial',
            value='Buried some time after the 1880s', date='after 1880s')
        self.assertNotEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(self.source.read_bytes(), before)

    def test_lint_does_not_suggest_dropping_the_word(self) -> None:
        import lint
        text = self.source.read_text(encoding='utf-8')
        self.source.write_text(text.replace('  date: 1880\n', '  date: after 1880s\n'),
                               encoding='utf-8')
        result = lint.run_lint(self.root, load_fha_yaml(self.root))
        self.assertEqual([m for m in result.messages if m.code == 'W109'], [])
        e014 = [m.text for m in result.messages if m.code == 'E014']
        self.assertEqual(len(e014), 1)
        self.assertIn('single date', e014[0])

    def test_gedcom_keeps_an_open_end_open(self) -> None:
        import gedcom
        self.assertEqual(gedcom._edtf_to_gedcom('1910/[1920..]'), 'AFT 1910')
        self.assertEqual(gedcom._edtf_to_gedcom('[1920..]/[1930..]'), 'AFT 1920')
        self.assertEqual(gedcom._edtf_to_gedcom('[..1900]/1910'), 'BEF 1910')
        self.assertIsNone(gedcom._edtf_to_gedcom('[..1900]/[1920..]'))
        # A closed range, and an after-form START, are still BET ranges.
        self.assertEqual(gedcom._edtf_to_gedcom('[1920..]/1930'), 'BET 1920 AND 1930')
        self.assertEqual(gedcom._edtf_to_gedcom('1870/1875'), 'BET 1870 AND 1875')

    def test_a_place_report_never_shows_the_year_9999(self) -> None:
        import index
        import places
        for date in ('on or after 30 Sep 1880', 'after 1 Oct 1881', '1882-05-01'):
            result = claim.run_claim_new(
                self.root, source_id='S-1111111111', claim_type='burial',
                value='Buried at Oak Hill', persons=['P-aaaaaaaaaa'],
                place_text='Oak Hill Cemetery', date=date)
            self.assertEqual(result.exit_code, EXIT_CLEAN, result.messages)
        index.build_index(self.root, load_fha_yaml(self.root))
        groups = places.run_candidates(self.root, load_fha_yaml(self.root), threshold=2)['groups']
        line = next(g for g in groups if 'Oak Hill' in g)
        self.assertNotIn('9999', line)
        self.assertIn('1880-09-30/1882-05-01', line)

    def test_the_spread_reads_an_open_side_in_words(self) -> None:
        from _lib import format_date_spread
        self.assertEqual(format_date_spread('1880-09-30', '9999-12-31'), 'from 1880-09-30')
        self.assertEqual(format_date_spread('0001-01-01', '1920-12-31'), 'up to 1920-12-31')
        self.assertEqual(format_date_spread(None, None), 'no dates')


# ── #246: accepting a death marks the person as no longer living ─────────────

_ADA = 'P-aaaaaaaaaa'        # living: unknown - the ordinary stub
_THOMAS = 'P-bbbbbbbbbb'     # living: true - recorded as alive on purpose
_MARY = 'P-cccccccccc'       # living: unknown
_JOHN = 'P-dddddddddd'       # living: unknown - the narrator / informant
_EDITH = 'P-eeeeeeeeee'      # living: false already
_OLD = 'P-ffffffffff'        # a merged record (tombstone)


def _death_person(root: Path, pid: str, name: str, living: str, sex: str = 'F') -> Path:
    first, last = name.lower().split()
    path = root / 'people' / 'stubs' / f'{last}__{first}_{pid}.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'---\nid: {pid}\nname: {name}\nsex: {sex}\nliving: {living}\n'
        'tier: stub\ncreated: 2026-07-01\n---\n', encoding='utf-8')
    return path


def _death_source(root: Path, claims: str) -> Path:
    path = root / 'sources' / 'other' / 'records_S-1111111111.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '---\nid: S-1111111111\ntitle: Family records\nsource_type: other\n'
        'source_class: original\nrepository: example collection\n'
        'citation: A fictional citation.\ncreated: 2026-06-01\n---\n\n'
        f'## Claims\n```yaml\n{claims}```\n', encoding='utf-8')
    return path


def _claim_item(cid: str, ctype: str, persons: str, *, roles: str = '',
                extra: str = '', status: str = 'suggested') -> str:
    return (f'- value: "a {ctype} record"\n  id: {cid}\n  type: {ctype}\n'
            f'  persons: [{persons}]\n{roles}{extra}  status: {status}\n'
            '  confidence: high\n\n')


class _DeathFixture:
    """Ada, Thomas, Mary, John and Edith Hartley, plus a merged record - the
    people the #246 death-claim tests accept records for."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ada = _death_person(self.root, _ADA, 'Ada Hartley', 'unknown')
        self.thomas = _death_person(self.root, _THOMAS, 'Thomas Hartley', 'true', 'M')
        self.mary = _death_person(self.root, _MARY, 'Mary Hartley', 'unknown')
        self.john = _death_person(self.root, _JOHN, 'John Hartley', 'unknown', 'M')
        self.edith = _death_person(self.root, _EDITH, 'Edith Hartley', 'false')
        self.old = self.root / 'people' / 'stubs' / \
            f'MERGED-INTO-{_ADA}__hartley__ada_{_OLD}.md'
        self.old.write_text(
            f'---\nid: {_OLD}\nname: Ada Hartley\nliving: unknown\nstatus: merged\n'
            f'merged_into: {_ADA}\n---\n', encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _living(self, path: Path) -> str:
        return str(read_record(path)['meta'].get('living')).lower()

    def _text(self, result) -> str:
        return '\n'.join(m.text for m in result.messages)

    def _accept(self, *cids: str, **kw):
        return claim.run_claim_batch(self.root, claim_ids=list(cids),
                                     status='accepted', **kw)


class DeathClaimMarksNotLivingTests(_DeathFixture, unittest.TestCase):
    """#246: a person with an accepted death certificate, grave or obituary
    stayed `living: unknown`, so every shared export and the site left them
    out. Accepting the claim now records that they died - for the person the
    claim says died, and nobody else."""

    def test_single_person_death_marks_them_no_longer_living(self) -> None:
        _death_source(self.root, _claim_item('C-aa11bb22cc', 'death', _ADA))
        result = self._accept('C-aa11bb22cc')
        self.assertEqual(result.exit_code, EXIT_CLEAN, self._text(result))
        self.assertEqual(self._living(self.ada), 'false')
        self.assertIn(str(self.ada), result.changed)
        self.assertIn('Ada Hartley is now marked as no longer living', self._text(result))
        self.assertIn('may now include her', self._text(result))
        self.assertEqual(result['marked_not_living'], [_ADA.lower()])

    def test_deceased_role_marks_only_the_deceased(self) -> None:
        _death_source(self.root, _claim_item(
            'C-aa11bb22cc', 'death', f'{_ADA}, {_JOHN}',
            roles=f'  roles:\n    deceased: [{_ADA}]\n    informant: [{_JOHN}]\n'))
        self._accept('C-aa11bb22cc')
        self.assertEqual(self._living(self.ada), 'false')
        self.assertEqual(self._living(self.john), 'unknown')

    def test_family_plot_burial_never_marks_the_narrator(self) -> None:
        # "The family plot holds Ada and Mary, with a space for me." The
        # narrator carries a part too (SPEC §8.3: everyone on an event claim).
        _death_source(self.root, _claim_item(
            'C-aa11bb22cc', 'burial', f'{_ADA}, {_MARY}, {_JOHN}',
            roles=f'  roles:\n    deceased: [{_ADA}, {_MARY}]\n    informant: [{_JOHN}]\n'))
        result = self._accept('C-aa11bb22cc')
        self.assertEqual(self._living(self.ada), 'false')
        self.assertEqual(self._living(self.mary), 'false')
        self.assertEqual(self._living(self.john), 'unknown')
        self.assertNotIn('John Hartley', self._text(result))

    def test_a_death_for_someone_marked_living_is_not_accepted(self) -> None:
        # A death recorded for a person marked living is how a mother's
        # obituary filed under her son's name makes him dead. Refused, not
        # accepted with a warning a reader may miss - nothing is written.
        source = _death_source(self.root, _claim_item('C-aa11bb22cc', 'death', _THOMAS))
        before = (self.thomas.read_bytes(), source.read_bytes())
        result = self._accept('C-aa11bb22cc')
        self.assertNotEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual((self.thomas.read_bytes(), source.read_bytes()), before)
        text = self._text(result)
        self.assertIn('records the DEATH of Thomas Hartley', text)
        self.assertIn('marked as living', text)
        self.assertIn('--confirm-deceased', text)

    def test_a_confirmed_death_for_someone_marked_living_is_accepted(self) -> None:
        # The human looked: the record really is Thomas's death. One reviewed
        # yes accepts it and marks him no longer living - no separate step.
        source = _death_source(self.root, _claim_item('C-aa11bb22cc', 'death', _THOMAS))
        result = self._accept('C-aa11bb22cc', confirm_deceased=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN, self._text(result))
        self.assertIn('status: accepted', source.read_text(encoding='utf-8'))
        self.assertEqual(self._living(self.thomas), 'false')
        self.assertIn('Thomas Hartley is now marked as no longer living', self._text(result))

    def test_confirming_one_death_never_marks_a_living_bystander(self) -> None:
        # The confirmation is about the deceased only: a living spouse named
        # on the same record keeps her flag.
        _death_source(self.root, _claim_item(
            'C-aa11bb22cc', 'death', f'{_ADA}, {_THOMAS}',
            roles=f'  roles:\n    deceased: [{_ADA}]\n    spouse: [{_THOMAS}]\n'))
        self._accept('C-aa11bb22cc', confirm_deceased=True)
        self.assertEqual(self._living(self.ada), 'false')
        self.assertEqual(self._living(self.thomas), 'true')

    def test_a_burial_naming_several_people_with_no_roles_is_not_accepted(self) -> None:
        # Whose burial? Accepting would make it a fact without saying whose
        # (SPEC §8.3): refused, nothing written, the fix named.
        source = _death_source(self.root, _claim_item('C-aa11bb22cc', 'burial',
                                                      f'{_ADA}, {_MARY}, {_JOHN}'))
        before = source.read_bytes()
        result = self._accept('C-aa11bb22cc')
        self.assertNotEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(source.read_bytes(), before)
        for path in (self.ada, self.mary, self.john):
            self.assertEqual(self._living(path), 'unknown')
        text = self._text(result)
        self.assertIn('does not say whose burial it is', text)
        self.assertIn('deceased: [p-', text)

    def test_a_death_read_only_by_elimination_is_not_accepted(self) -> None:
        # The older shape SPEC §8.3 still reads: nobody marked deceased:, the
        # widower has a role, so Ada would be read as the one who died. One
        # forgotten role is how a record lands on the wrong person, so a
        # claim of this shape is no longer accepted until it says.
        _death_source(self.root, _claim_item(
            'C-aa11bb22cc', 'death', f'{_ADA}, {_JOHN}',
            roles=f'  roles:\n    spouse: [{_JOHN}]\n'))
        result = self._accept('C-aa11bb22cc')
        self.assertNotEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(self._living(self.ada), 'unknown')
        self.assertEqual(self._living(self.john), 'unknown')
        self.assertIn(f'gives no part to {_ADA.lower()}', self._text(result))

    def test_negated_death_marks_nobody(self) -> None:
        # "We searched and Ada did not die then" says nobody died. Mary's
        # ordinary death in the same batch shows the step did run.
        _death_source(self.root, _claim_item(
            'C-aa11bb22cc', 'death', _ADA,
            extra='  negated: true\n  evidence: negative\n')
            + _claim_item('C-bb22cc33dd', 'death', _MARY))
        result = self._accept('C-aa11bb22cc', 'C-bb22cc33dd')
        self.assertEqual(self._living(self.ada), 'unknown')
        self.assertNotIn('Ada Hartley is now marked', self._text(result))
        self.assertIn('records that there was NO DEATH of Ada Hartley', self._text(result))
        self.assertEqual(self._living(self.mary), 'false')

    def test_already_not_living_is_unchanged_and_silent(self) -> None:
        _death_source(self.root, _claim_item('C-aa11bb22cc', 'death', _EDITH))
        before = self.edith.read_bytes()
        result = self._accept('C-aa11bb22cc')
        self.assertEqual(self.edith.read_bytes(), before)
        self.assertNotIn('Edith Hartley is now marked', self._text(result))
        self.assertIn('records the DEATH of Edith Hartley', self._text(result))
        self.assertNotIn(str(self.edith), result.changed)
        self.assertEqual(result['marked_not_living'], [])

    def test_only_an_accept_marks_anyone(self) -> None:
        _death_source(self.root, _claim_item('C-aa11bb22cc', 'death', _ADA))
        for status in ('suggested', 'disputed', 'rejected', 'superseded'):
            with self.subTest(status=status):
                claim.run_claim(self.root, claim_id='C-aa11bb22cc', status=status)
                self.assertEqual(self._living(self.ada), 'unknown')
        claim.run_claim(self.root, claim_id='C-aa11bb22cc', status='accepted')
        self.assertEqual(self._living(self.ada), 'false')

    def test_other_claim_types_mark_nobody(self) -> None:
        _death_source(self.root, _claim_item('C-aa11bb22cc', 'residence', _ADA)
                      + _claim_item('C-bb22cc33dd', 'birth', _JOHN)
                      + _claim_item('C-cc33dd44ee', 'burial', _MARY))
        self._accept('C-aa11bb22cc', 'C-bb22cc33dd', 'C-cc33dd44ee')
        self.assertEqual(self._living(self.ada), 'unknown')
        self.assertEqual(self._living(self.john), 'unknown')
        self.assertEqual(self._living(self.mary), 'false')

    def test_batch_accept_marks_each_person_once(self) -> None:
        _death_source(self.root,
                      _claim_item('C-aa11bb22cc', 'death', _ADA)
                      + _claim_item('C-bb22cc33dd', 'burial', _ADA)
                      + _claim_item('C-cc33dd44ee', 'death', _MARY)
                      + _claim_item('C-dd44ee55ff', 'birth', _JOHN))
        result = self._accept('C-aa11bb22cc', 'C-bb22cc33dd', 'C-cc33dd44ee', 'C-dd44ee55ff')
        self.assertEqual(result.exit_code, EXIT_CLEAN, self._text(result))
        self.assertEqual(self._living(self.ada), 'false')
        self.assertEqual(self._living(self.mary), 'false')
        self.assertEqual(self._living(self.john), 'unknown')
        self.assertEqual(self._text(result).count('Ada Hartley is now marked'), 1)
        self.assertEqual(sorted(result['marked_not_living']), [_ADA.lower(), _MARY.lower()])

    def test_dry_run_writes_nothing_and_says_what_would_happen(self) -> None:
        _death_source(self.root, _claim_item('C-aa11bb22cc', 'death', _ADA)
                      + _claim_item('C-bb22cc33dd', 'death', _MARY))
        for cids in (['C-aa11bb22cc'], ['C-aa11bb22cc', 'C-bb22cc33dd']):
            with self.subTest(cids=cids):
                before = self.ada.read_bytes()
                result = self._accept(*cids, dry_run=True)
                self.assertEqual(self.ada.read_bytes(), before)
                self.assertEqual(self._text(result).count(
                    '[dry-run] Would mark Ada Hartley as no longer living'), 1)

    def test_keep_living_accepts_without_marking(self) -> None:
        source = _death_source(self.root, _claim_item('C-aa11bb22cc', 'death', _ADA))
        result = self._accept('C-aa11bb22cc', keep_living=True)
        self.assertEqual(self._living(self.ada), 'unknown')
        self.assertIn('status: accepted', source.read_text(encoding='utf-8'))
        self.assertIn('--keep-living', self._text(result))

    def test_a_merged_record_is_skipped_and_the_survivor_named(self) -> None:
        _death_source(self.root, _claim_item('C-aa11bb22cc', 'death', _OLD))
        before = self.old.read_bytes()
        result = self._accept('C-aa11bb22cc')
        self.assertEqual(self.old.read_bytes(), before)
        self.assertEqual(self._living(self.ada), 'unknown')
        self.assertIn(f'fha person set-living {_ADA.lower()} false', self._text(result))

    def test_a_name_link_beside_an_id_marks_nobody(self) -> None:
        # Without the index "[[John Hartley]]" cannot be resolved here, and
        # reading the claim as Ada's alone could mark the wrong person dead.
        _death_source(self.root, _claim_item('C-aa11bb22cc', 'death',
                                             f'{_ADA}, "[[John Hartley]]"'))
        result = self._accept('C-aa11bb22cc')
        self.assertEqual(self._living(self.ada), 'unknown')
        self.assertIn('a name rather than an ID', self._text(result))

    def test_accept_with_a_type_correction_reads_the_corrected_claim(self) -> None:
        _death_source(self.root, _claim_item('C-aa11bb22cc', 'residence', _ADA))
        claim.run_claim(self.root, claim_id='C-aa11bb22cc', status='accepted',
                        claim_type='death')
        self.assertEqual(self._living(self.ada), 'false')

    def test_claim_new_accepted_death_marks_them(self) -> None:
        _death_source(self.root, '')
        result = claim.run_claim_new(self.root, source_id='S-1111111111',
                                     claim_type='death', value='Ada died 1980',
                                     persons=[_ADA], status='accepted')
        self.assertEqual(result.exit_code, EXIT_CLEAN, self._text(result))
        self.assertEqual(self._living(self.ada), 'false')

    def test_claim_new_will_not_mint_an_accepted_death_without_saying_whose(self) -> None:
        # `claim new` writes no roles:, so a death naming two people cannot
        # say who died: it may be minted suggested, never straight to accepted.
        source = _death_source(self.root, '')
        before = source.read_bytes()
        result = claim.run_claim_new(self.root, source_id='S-1111111111',
                                     claim_type='death', value='Ada died 1980',
                                     persons=[_ADA, _JOHN], status='accepted')
        self.assertNotEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(source.read_bytes(), before)
        self.assertIn('--status suggested', self._text(result))
        self.assertEqual(self._living(self.ada), 'unknown')

    def test_an_accept_says_whose_event_it_records(self) -> None:
        _death_source(self.root, _claim_item(
            'C-aa11bb22cc', 'death', f'{_ADA}, {_JOHN}',
            roles=f'  roles:\n    deceased: [{_ADA}]\n    spouse: [{_JOHN}]\n'))
        result = self._accept('C-aa11bb22cc')
        self.assertIn('c-aa11bb22cc records the DEATH of Ada Hartley', self._text(result))

    def test_claim_new_suggested_or_negated_marks_nobody(self) -> None:
        _death_source(self.root, '')
        claim.run_claim_new(self.root, source_id='S-1111111111', claim_type='death',
                            value='Ada died 1980', persons=[_ADA], status='suggested')
        claim.run_claim_new(self.root, source_id='S-1111111111', claim_type='death',
                            value='no death record found', persons=[_ADA],
                            status='accepted', negated=True)
        self.assertEqual(self._living(self.ada), 'unknown')
        claim.run_claim_new(self.root, source_id='S-1111111111', claim_type='death',
                            value='Mary died 1975', persons=[_MARY], status='accepted')
        self.assertEqual(self._living(self.mary), 'false')

    def test_cli_accept_marks_and_keep_living_flag_opts_out(self) -> None:
        import fha
        (self.root / 'fha.yaml').write_text('roots:\n  documents: documents\n',
                                            encoding='utf-8')
        _death_source(self.root, _claim_item('C-aa11bb22cc', 'death', _ADA)
                      + _claim_item('C-bb22cc33dd', 'death', _MARY))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fha.main(['claim', 'C-aa11bb22cc', '--status', 'accepted',
                           '--root', str(self.root)])
            rc2 = fha.main(['claim', 'C-bb22cc33dd', '--status', 'accepted',
                            '--keep-living', '--root', str(self.root)])
        self.assertEqual((rc, rc2), (EXIT_CLEAN, EXIT_CLEAN), err.getvalue())
        self.assertIn('Ada Hartley is now marked as no longer living', out.getvalue())
        self.assertEqual(self._living(self.ada), 'false')
        self.assertEqual(self._living(self.mary), 'unknown')

    def test_accept_and_lint_agree(self) -> None:
        # The accept and lint W136 read one rule (`_lib.death_claim_deceased`):
        # after the accept, lint reports exactly the people it did not mark.
        import lint
        # Ada is marked on accept; Mary's burial is accepted with
        # --keep-living, so she is the one lint reports.
        _death_source(self.root,
                      _claim_item('C-aa11bb22cc', 'death', _ADA)
                      + _claim_item('C-cc33dd44ee', 'burial', f'{_MARY}, {_JOHN}',
                                    roles=f'  roles:\n    deceased: [{_MARY}]\n'
                                          f'    spouse: [{_JOHN}]\n'))
        (self.root / 'fha.yaml').write_text('roots:\n  documents: documents\n',
                                            encoding='utf-8')
        self._accept('C-aa11bb22cc')
        self._accept('C-cc33dd44ee', keep_living=True)
        findings, _reg = lint._run_lint_core(self.root, load_fha_yaml(self.root))
        flagged = sorted(f.message.split(' (')[0] for f in findings if f.code == 'W136')
        self.assertEqual(flagged, ['Mary Hartley'])


class DeathClaimReviewFixTests(_DeathFixture, unittest.TestCase):
    """#250 review: the cases where the first version of the death-claim step
    marked the wrong person, kept quiet, or forgot the human's choice."""

    # 1. A roles: entry naming someone missing from persons: must never make
    # the widow look like the only person on her husband's death record.
    def test_a_role_for_someone_missing_from_persons_marks_nobody(self) -> None:
        for roles in (f'  roles:\n    deceased: [{_JOHN}]\n',
                      '  roles:\n    deceased: ["[[John Hartley]]"]\n',
                      f'  roles:\n    decedent: [{_JOHN}]\n'):
            with self.subTest(roles=roles):
                _death_source(self.root, _claim_item(
                    'C-aa11bb22cc', 'death', _ADA, roles=roles))
                result = self._accept('C-aa11bb22cc')
                self.assertEqual(self._living(self.ada), 'unknown')
                self.assertEqual(self._living(self.john), 'unknown')
                self.assertEqual(result['marked_not_living'], [])
                self.assertIn('cannot be told for sure who died', self._text(result))
                self.assertNotIn(f'set-living {_ADA}', self._text(result))

    # 2. Moving an accepted death claim out of accepted warns (never reverts).
    def test_un_accepting_a_death_warns_and_names_the_way_back(self) -> None:
        _death_source(self.root, _claim_item('C-aa11bb22cc', 'death', _ADA)
                      + _claim_item('C-bb22cc33dd', 'death', _MARY))
        self._accept('C-aa11bb22cc', 'C-bb22cc33dd')
        self.assertEqual(self._living(self.ada), 'false')
        # dry run, then live (single), then a batch moving one more out
        for status, cids, dry, pid in (('rejected', ['C-aa11bb22cc'], True, _ADA),
                                       ('rejected', ['C-aa11bb22cc'], False, _ADA),
                                       ('suggested', ['C-aa11bb22cc', 'C-bb22cc33dd'],
                                        False, _MARY)):
            with self.subTest(status=status, cids=cids, dry=dry):
                result = claim.run_claim_batch(self.root, claim_ids=cids, status=status,
                                               dry_run=dry)
                self.assertEqual(self._living(self.ada), 'false')
                self.assertEqual(self._living(self.mary), 'false')
                warnings = [m.text for m in result.messages if m.level == 'warning']
                self.assertEqual(len(warnings), 1, self._text(result))
                self.assertIn(f'fha person set-living {pid.lower()} unknown', warnings[0])

    def test_un_accepting_names_only_people_still_marked(self) -> None:
        # Ada was kept private (--keep-living), Mary was marked: rejecting
        # both warns about Mary alone.
        _death_source(self.root, _claim_item('C-aa11bb22cc', 'death', _ADA)
                      + _claim_item('C-bb22cc33dd', 'death', _MARY))
        self._accept('C-aa11bb22cc', keep_living=True)
        self._accept('C-bb22cc33dd')
        result = claim.run_claim_batch(self.root, claim_ids=['C-aa11bb22cc', 'C-bb22cc33dd'],
                                       status='rejected')
        self.assertNotIn(f'set-living {_ADA}', self._text(result))
        self.assertIn(f'fha person set-living {_MARY.lower()} unknown', self._text(result))

    # 3. A claim that was already accepted never marks anyone: a rerun must
    # not undo an earlier --keep-living.
    def test_keep_living_sticks_through_a_later_re_accept(self) -> None:
        _death_source(self.root, _claim_item('C-aa11bb22cc', 'death', _ADA)
                      + _claim_item('C-bb22cc33dd', 'residence', _ADA))
        self._accept('C-aa11bb22cc', keep_living=True)
        self._accept('C-aa11bb22cc', 'C-bb22cc33dd')
        self.assertEqual(self._living(self.ada), 'unknown')
        claim.run_claim(self.root, claim_id='C-aa11bb22cc', status='accepted',
                        reviewed='2026-12-01')
        self.assertEqual(self._living(self.ada), 'unknown')

    # 4. One claim naming the person is enough, whatever order the batch is in.
    def test_a_naming_claim_after_a_by_elimination_one_still_marks(self) -> None:
        # The by-elimination claim is now refused (SPEC §8.3), so the batch
        # stops before it; the claim naming Ada outright, accepted on its
        # own, still marks her.
        _death_source(self.root,
                      _claim_item('C-aa11bb22cc', 'death', f'{_ADA}, {_JOHN}',
                                  roles=f'  roles:\n    spouse: [{_JOHN}]\n')
                      + _claim_item('C-bb22cc33dd', 'death', _ADA))
        refused = self._accept('C-aa11bb22cc', 'C-bb22cc33dd')
        self.assertNotEqual(refused.exit_code, EXIT_CLEAN)
        self.assertEqual(self._living(self.ada), 'unknown')
        result = self._accept('C-bb22cc33dd')
        self.assertEqual(self._living(self.ada), 'false')
        self.assertNotIn('does not name anyone as `deceased:`', self._text(result))

    # 5. A name link in roles: never turns into "mark the son as dead".
    def test_a_name_link_in_roles_never_names_another_person_to_mark(self) -> None:
        _death_source(self.root, _claim_item(
            'C-aa11bb22cc', 'death', f'{_THOMAS}, {_MARY}, {_JOHN}',
            roles=f'  roles:\n    spouse: [{_MARY}]\n    child: ["[[John Hartley]]"]\n'))
        result = self._accept('C-aa11bb22cc')
        for path in (self.mary, self.john):
            self.assertEqual(self._living(path), 'unknown')
        self.assertNotIn(f'set-living {_JOHN}', self._text(result))
        self.assertNotIn(f'set-living {_MARY}', self._text(result))

    # 6. A stopped batch's recovery command keeps --keep-living.
    def test_recovery_commands_carry_keep_living(self) -> None:
        _death_source(self.root, _claim_item('C-aa11bb22cc', 'death', _ADA))
        dup = self.root / 'sources' / 'other' / 'dup_S-2222222222.md'
        dup.write_text(
            '---\nid: S-2222222222\ntitle: Dup\nsource_type: other\n---\n\n'
            '## Claims\n```yaml\n'
            + _claim_item('C-ee55ff66gh', 'death', _MARY)
            + _claim_item('C-ee55ff66gh', 'death', _MARY) + '```\n', encoding='utf-8')
        for dry in (False, True):
            with self.subTest(dry_run=dry):
                result = self._accept('C-aa11bb22cc', 'C-ee55ff66gh',
                                      keep_living=True, dry_run=dry)
                self.assertNotEqual(result.exit_code, EXIT_CLEAN)
                stop = [m.text for m in result.messages if m.text.startswith('Stopped at')]
                self.assertEqual(len(stop), 1, self._text(result))
                self.assertIn('--keep-living', stop[0])
        self.assertEqual(self._living(self.ada), 'unknown')

    # 7. The batch reads the claim it actually wrote, not its first snapshot.
    def test_batch_marks_who_the_written_claim_names(self) -> None:
        # The human corrects Ada -> Mary while the batch is starting up.
        source = _death_source(self.root, _claim_item('C-aa11bb22cc', 'death', _ADA)
                               + _claim_item('C-bb22cc33dd', 'residence', _JOHN))
        ids = [claim.normalize_id('C-aa11bb22cc'), claim.normalize_id('C-bb22cc33dd')]
        stale = claim._locate_claims(self.root, ids)
        source.write_text(source.read_text(encoding='utf-8').replace(
            f'persons: [{_ADA}]', f'persons: [{_MARY}]'), encoding='utf-8')
        with mock.patch.object(claim, '_locate_claims', return_value=stale):
            result = self._accept('C-aa11bb22cc', 'C-bb22cc33dd')
        self.assertEqual(self._living(self.ada), 'unknown', self._text(result))
        self.assertEqual(self._living(self.mary), 'false')

    # 8. Plain English: "so he stays out", never "so him stays out".
    def test_warnings_use_a_subject_pronoun(self) -> None:
        _death_source(self.root, _claim_item('C-aa11bb22cc', 'death', _JOHN))
        result = self._accept('C-aa11bb22cc', keep_living=True)
        self.assertIn('so he stays out', self._text(result))
        self.assertNotIn('so him stays', self._text(result))

    # 9. An edit to an accepted death claim that stops naming someone as the
    # deceased warns like an un-accept (never reverts); whoever it newly
    # names is not marked - only a move into accepted marks anyone.
    def test_editing_an_accepted_death_away_from_someone_warns(self) -> None:
        _death_source(self.root, _claim_item('C-aa11bb22cc', 'death', _ADA)
                      + _claim_item('C-bb22cc33dd', 'burial', _JOHN))
        self._accept('C-aa11bb22cc', 'C-bb22cc33dd')
        self.assertEqual(self._living(self.ada), 'false')
        self.assertEqual(self._living(self.john), 'false')
        cases = (
            ('dry run, --persons', 'C-aa11bb22cc', {'persons': [_MARY], 'dry_run': True}, _ADA),
            ('--persons Ada -> Mary', 'C-aa11bb22cc', {'persons': [_MARY]}, _ADA),
            ('--type away from burial', 'C-bb22cc33dd', {'claim_type': 'residence'}, _JOHN),
        )
        for label, cid, kw, pid in cases:
            with self.subTest(label):
                result = claim.run_claim_batch(self.root, claim_ids=[cid], **kw)
                self.assertEqual(result.exit_code, EXIT_CLEAN, self._text(result))
                warnings = [m.text for m in result.messages if m.level == 'warning']
                self.assertEqual(len(warnings), 1, self._text(result))
                self.assertIn('as the one who died', warnings[0])
                self.assertIn(f'fha person set-living {pid.lower()} unknown', warnings[0])
        self.assertEqual(self._living(self.ada), 'false')
        self.assertEqual(self._living(self.john), 'false')
        self.assertEqual(self._living(self.mary), 'unknown')   # newly named: not marked

    def test_an_edit_that_still_names_the_deceased_says_nothing_extra(self) -> None:
        _death_source(self.root, _claim_item('C-aa11bb22cc', 'death', _ADA))
        self._accept('C-aa11bb22cc')
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', date='1980')
        self.assertEqual([m for m in result.messages if m.level == 'warning'], [])
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc',
                                 persons=[_ADA, _JOHN])   # now unscoped: Ada dropped
        self.assertIn(f'fha person set-living {_ADA.lower()} unknown', self._text(result))

    # 10. Compare-and-set: a `living: true` saved after the accept looked at
    # the record (another editor, another session) is never overwritten.
    def test_a_living_true_saved_meanwhile_is_never_overwritten(self) -> None:
        _death_source(self.root, _claim_item('C-aa11bb22cc', 'death', _ADA))
        real_read = claim._read_person

        def read_then_someone_saves_true(root, pid):
            path, meta = real_read(root, pid)
            if pid.lower() == _ADA.lower():
                path.write_text(path.read_text(encoding='utf-8').replace(
                    'living: unknown', 'living: true'), encoding='utf-8')
            return path, meta          # the stale look: still unknown
        with mock.patch.object(claim, '_read_person', read_then_someone_saves_true):
            result = self._accept('C-aa11bb22cc')
        self.assertEqual(self._living(self.ada), 'true')
        self.assertEqual(result['marked_not_living'], [])
        self.assertIn('is marked as living', self._text(result))

    def test_set_living_itself_still_obeys_the_human(self) -> None:
        # The explicit command is the human's direct order: unchanged.
        import person
        result = person.run_set_living(self.root, _THOMAS, 'false')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(self._living(self.thomas), 'false')

    # 11. A single accept judges the claim in the text it writes, not the
    # lookup taken before that text was read.
    def test_single_accept_marks_who_the_written_claim_names(self) -> None:
        source = _death_source(self.root, _claim_item('C-aa11bb22cc', 'death', _ADA))
        real_find = claim._find_claim_record

        def find_then_corrected(*a, **k):
            found = real_find(*a, **k)    # sees Ada ...
            source.write_text(source.read_text(encoding='utf-8').replace(
                f'persons: [{_ADA}]', f'persons: [{_MARY}]'), encoding='utf-8')
            return found                   # ... but the file now names Mary
        with mock.patch.object(claim, '_find_claim_record', find_then_corrected):
            result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', status='accepted')
        self.assertEqual(self._living(self.ada), 'unknown', self._text(result))
        self.assertEqual(self._living(self.mary), 'false')

    # 12. (verification for Codex P2) a status move plus a field edit in one
    # command warns about the ORIGINAL deceased.
    def test_un_accept_with_a_field_edit_warns_about_the_original_deceased(self) -> None:
        _death_source(self.root, _claim_item('C-aa11bb22cc', 'death', _ADA)
                      + _claim_item('C-bb22cc33dd', 'death', _JOHN))
        self._accept('C-aa11bb22cc', 'C-bb22cc33dd')
        for cid, kw, pid in (('C-aa11bb22cc', {'persons': [_MARY]}, _ADA),
                             ('C-bb22cc33dd', {'claim_type': 'residence'}, _JOHN)):
            with self.subTest(kw=kw):
                result = claim.run_claim(self.root, claim_id=cid, status='rejected', **kw)
                self.assertIn(f'fha person set-living {pid.lower()} unknown', self._text(result))
        self.assertEqual(self._living(self.mary), 'unknown')

    # 13. "they are still marked", never "they is".
    def test_still_marked_warning_agrees_with_its_pronoun(self) -> None:
        sam = _death_person(self.root, 'P-gggggggggg', 'Sam Hartley', 'unknown', 'U')
        _death_source(self.root, _claim_item('C-aa11bb22cc', 'death', 'P-gggggggggg'))
        self._accept('C-aa11bb22cc')
        self.assertEqual(self._living(sam), 'false')
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', persons=[_MARY])
        self.assertIn('but they are still marked', self._text(result))
        self.assertNotIn('they is', self._text(result))
        # she / he read "is"
        _death_source(self.root, _claim_item('C-bb22cc33dd', 'death', _ADA))
        self._accept('C-bb22cc33dd')
        result = claim.run_claim(self.root, claim_id='C-bb22cc33dd', persons=[_MARY])
        self.assertIn('but she is still marked', self._text(result))

    # 14. A batch whose record will not read back after the write says so.
    def test_batch_says_so_when_the_written_record_will_not_read_back(self) -> None:
        _death_source(self.root, _claim_item('C-aa11bb22cc', 'death', _ADA)
                      + _claim_item('C-bb22cc33dd', 'residence', _ADA))
        real = claim._record_claims

        def unreadable_after_write(path):
            if 'status: accepted' in Path(path).read_text(encoding='utf-8'):
                return {}
            return real(path)
        with mock.patch.object(claim, '_record_claims', unreadable_after_write):
            result = self._accept('C-aa11bb22cc', 'C-bb22cc33dd')
        self.assertEqual(self._living(self.ada), 'unknown')
        self.assertIn('could not be read back', self._text(result))
        self.assertIn('W136', self._text(result))



class ClearFieldTests(unittest.TestCase):
    """#273: `fha claim <C-id> --clear FIELD` removes an optional field, and an
    empty value is never written as `key: ''`.

    Expected texts are derived from the record as it stood (the one key line
    deleted, nothing else), and lint is compared before/after - SPEC §8.4
    makes place_text/anchor/notes optional and value required, so a clear
    must add no lint finding and an empty value must not reach the file."""

    CID = 'C-1b9d4e7a30'   # 1880 census birth: carries place_text, notes; no anchor
    REL = Path('sources/census/1880-census-fairview-hartley-household_s-4f5f215e60.md')

    @classmethod
    def setUpClass(cls) -> None:
        if not EXAMPLE.is_dir():
            raise unittest.SkipTest('example-archive not present')
        import index
        import lint
        cls.index = index
        cls.lint = lint

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / 'archive'
        shutil.copytree(EXAMPLE, self.root)
        self.path = self.root / self.REL
        self.before = self.path.read_bytes()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _claim(self) -> dict:
        return next(c for c in read_record(self.path)['claims']
                    if str(c['id']).lower() == self.CID.lower())

    def _lint_lines(self) -> set[tuple]:
        config = load_fha_yaml(self.root)
        self.index.build_index(self.root, config)
        result = self.lint.run_lint(self.root, config)
        return {(m.level, m.code, m.text) for m in result.messages}

    def _without_line(self, line: str) -> bytes:
        text = self.before.decode('utf-8')
        self.assertEqual(text.count(line + '\n'), 1)
        return text.replace(line + '\n', '', 1).encode('utf-8')

    def test_clear_place_text_removes_only_that_line_and_lint_adds_nothing(self) -> None:
        lint_before = self._lint_lines()
        result = claim.run_claim(self.root, claim_id=self.CID, clear=['place_text'])
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(self.path.read_bytes(), self._without_line('  place_text: New York'))
        self.assertNotIn('place_text', self._claim())
        self.assertEqual(self._lint_lines(), lint_before)

    def test_clear_is_repeatable_and_accepts_the_flag_spelling(self) -> None:
        result = claim.run_claim(self.root, claim_id=self.CID,
                                 clear=['place-text', 'information', 'evidence'])
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        expected = self.before.decode('utf-8')
        for line in ('  place_text: New York', '  information: secondary',
                     '  evidence: indirect'):
            expected = expected.replace(line + '\n', '', 1)
        self.assertEqual(self.path.read_bytes(), expected.encode('utf-8'))

    def test_value_empty_is_refused_and_the_file_is_byte_identical(self) -> None:
        lint_before = self._lint_lines()
        for empty in ('', '   '):
            with self.subTest(value=empty):
                result = claim.run_claim(self.root, claim_id=self.CID, value=empty)
                self.assertEqual(result.exit_code, EXIT_FAILURE)
                self.assertIn('--value cannot be empty', result.messages[0].text)
                self.assertEqual(self.path.read_bytes(), self.before)
        lint_after = self._lint_lines()
        self.assertEqual(lint_after, lint_before)
        self.assertFalse(any(code == 'E010' for _lvl, code, _t in lint_after))

    def test_empty_free_text_flag_is_refused_and_points_at_clear(self) -> None:
        for key in ('place_text', 'anchor', 'notes'):
            with self.subTest(key=key):
                result = claim.run_claim(self.root, claim_id=self.CID, **{key: ' '})
                self.assertEqual(result.exit_code, EXIT_FAILURE)
                self.assertIn(f'--clear {key}', result.messages[0].text)
                self.assertEqual(self.path.read_bytes(), self.before)

    def test_dry_run_previews_the_removal_and_writes_nothing(self) -> None:
        result = claim.run_claim(self.root, claim_id=self.CID, clear=['place_text'],
                                 dry_run=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        joined = '\n'.join(m.text for m in result.messages)
        self.assertIn('-  place_text: New York', joined)
        self.assertIn('place_text removed', joined)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_clearing_a_field_the_claim_lacks_says_so_and_succeeds(self) -> None:
        result = claim.run_claim(self.root, claim_id=self.CID, clear=['anchor'])
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'already-done')
        joined = '\n'.join(m.text for m in result.messages)
        self.assertIn('no anchor line, so there was nothing to clear', joined)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_absent_clear_alongside_a_real_change_still_applies_the_change(self) -> None:
        result = claim.run_claim(self.root, claim_id=self.CID,
                                 clear=['anchor', 'place_text'])
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(self.path.read_bytes(), self._without_line('  place_text: New York'))

    def test_required_fields_cannot_be_cleared(self) -> None:
        for field, flag in (('value', '--value'), ('confidence', '--confidence'),
                            ('persons', '--persons'), ('type', '--type'),
                            ('status', '--status')):
            with self.subTest(field=field):
                result = claim.run_claim(self.root, claim_id=self.CID, clear=[field])
                self.assertEqual(result.exit_code, EXIT_FAILURE)
                self.assertIn('required on every claim', result.messages[0].text)
                self.assertIn(flag, result.messages[0].text)
                self.assertEqual(self.path.read_bytes(), self.before)

    def test_unknown_field_is_refused_with_the_valid_list(self) -> None:
        result = claim.run_claim(self.root, claim_id=self.CID, clear=['placetext'])
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertIn('place_text', result.messages[0].text)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_set_and_clear_of_the_same_field_is_refused(self) -> None:
        result = claim.run_claim(self.root, claim_id=self.CID, clear=['notes'],
                                 notes='New notes.')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertIn('both given', result.messages[0].text)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_clear_place_stops_place_text_from_auto_linking(self) -> None:
        # Without --clear place, an exact registry match on --place-text
        # attaches place: (#79). Asking to clear place means "no link".
        result = claim.run_claim(self.root, claim_id=self.CID,
                                 place_text='Fairview', clear=['place'])
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        c = self._claim()
        self.assertEqual(c.get('place_text'), 'Fairview')
        self.assertNotIn('place', c)

    def test_batch_with_clear_is_refused(self) -> None:
        result = claim.run_claim_batch(
            self.root, claim_ids=[self.CID, 'C-bdfbce2a11'], status='accepted',
            clear=['notes'])
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertIn('--clear', result.messages[0].text)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_cli_clear_and_empty_persons_message(self) -> None:
        import fha

        def run(argv: list[str]) -> tuple[int, str, str]:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = fha.main(argv)
            return rc, out.getvalue(), err.getvalue()

        rc, _out, err = run(['claim', self.CID, '--persons', '', '--root', str(self.root)])
        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('must name at least one person', err)
        self.assertNotIn('Nothing to change', err)
        self.assertEqual(self.path.read_bytes(), self.before)

        rc, out, _err = run(['claim', self.CID, '--clear', 'place_text', '--clear', 'notes',
                             '--root', str(self.root)])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('place_text removed; notes removed', out)
        c = self._claim()
        self.assertNotIn('place_text', c)
        self.assertNotIn('notes', c)


class ClearFieldShapeTests(unittest.TestCase):
    """#273: removing a key keeps the rest of the claim byte for byte, whatever
    shape the key was written in."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _source(self, block: str) -> Path:
        path = self.root / 'sources' / 'other' / 'shape_S-1111111111.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('---\nid: S-1111111111\ntitle: T\nsource_type: other\n---\n\n'
                        '## Claims\n```yaml\n' + block + '```\n', encoding='utf-8')
        return path

    def test_first_key_on_the_dash_line_hands_the_dash_on(self) -> None:
        block = ('- place_text: "Somewhere"\n'
                 '  value: "Anna born 1880"\n'
                 '  id: C-aa11bb22cc\n'
                 '  type: birth\n'
                 '  persons: [P-aaaaaaaaaa]\n'
                 '  status: suggested\n'
                 '  confidence: high\n')
        path = self._source(block)
        before = path.read_text(encoding='utf-8')
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', clear=['place_text'])
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        expected = before.replace('- place_text: "Somewhere"\n  value:', '- value:', 1)
        self.assertEqual(path.read_text(encoding='utf-8'), expected)

    def test_multiline_notes_block_goes_whole_and_the_separator_stays(self) -> None:
        block = ('- value: "Anna born 1880"\n'
                 '  id: C-aa11bb22cc\n'
                 '  type: birth\n'
                 '  persons: [P-aaaaaaaaaa]\n'
                 '  status: suggested\n'
                 '  confidence: high\n'
                 '  notes: >\n'
                 '    First line of the note,\n'
                 '    second line of the note.\n'
                 '\n'
                 '- value: "Anna died 1950"\n'
                 '  id: C-bb22cc33dd\n'
                 '  type: death\n'
                 '  persons: [P-aaaaaaaaaa]\n'
                 '  status: suggested\n'
                 '  confidence: medium\n')
        path = self._source(block)
        before = path.read_text(encoding='utf-8')
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', clear=['notes'])
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        expected = before.replace(
            '  notes: >\n    First line of the note,\n    second line of the note.\n', '', 1)
        self.assertEqual(path.read_text(encoding='utf-8'), expected)


    # PR #284 review: a comment after the cleared field is not its value.
    _TAIL = ('  status: suggested\n'
             '  confidence: high\n')

    def _notes_then(self, between: str, notes: str = '  notes: A plain note.\n') -> str:
        return ('- value: "Anna born 1880"\n'
                '  id: C-aa11bb22cc\n'
                '  type: birth\n'
                '  persons: [P-aaaaaaaaaa]\n'
                + notes + between + self._TAIL
                + '\n- value: "Anna died 1950"\n'
                  '  id: C-bb22cc33dd\n'
                  '  type: death\n'
                  '  persons: [P-aaaaaaaaaa]\n'
                  '  status: suggested\n'
                  '  confidence: medium\n')

    def _clear_notes_keeps(self, block: str, notes: str) -> None:
        path = self._source(block)
        before = path.read_text(encoding='utf-8')
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', clear=['notes'])
        self.assertEqual(result.exit_code, EXIT_CLEAN, [m.text for m in result.messages])
        self.assertEqual(path.read_text(encoding='utf-8'), before.replace(notes, '', 1))

    def test_column_zero_comment_after_the_cleared_field_is_kept(self) -> None:
        for notes in ('  notes: A plain note.\n',
                      '  notes: >\n    First line,\n    second line.\n'):
            for comment in ('# Keep this research reminder\n',
                            '  # Keep this research reminder\n',
                            '    # Keep this research reminder\n'):
                with self.subTest(notes=notes, comment=comment):
                    if notes.endswith('>\n    First line,\n    second line.\n') \
                            and comment.startswith('    #'):
                        continue   # deeper than the key inside a block scalar: content
                    self._clear_notes_keeps(self._notes_then(comment, notes), notes)

    def test_comment_after_the_last_key_of_a_claim_is_kept(self) -> None:
        notes = '  notes: A plain note.\n'
        block = ('- value: "Anna born 1880"\n'
                 '  id: C-aa11bb22cc\n'
                 '  type: birth\n'
                 '  persons: [P-aaaaaaaaaa]\n'
                 '  status: suggested\n'
                 '  confidence: high\n'
                 + notes +
                 '# Keep this research reminder\n'
                 '\n'
                 '- value: "Anna died 1950"\n'
                 '  id: C-bb22cc33dd\n'
                 '  type: death\n'
                 '  persons: [P-aaaaaaaaaa]\n'
                 '  status: suggested\n'
                 '  confidence: medium\n')
        self._clear_notes_keeps(block, notes)

    def test_first_key_dash_moves_past_a_blank_line_or_comment(self) -> None:
        for sep in ('\n', '# a reminder\n', '\n  # a reminder\n'):
            with self.subTest(sep=sep):
                block = ('- notes: First note.\n'
                         + sep +
                         '  status: suggested\n'
                         '  value: "Anna born 1880"\n'
                         '  id: C-aa11bb22cc\n'
                         '  type: birth\n'
                         '  persons: [P-aaaaaaaaaa]\n'
                         '  confidence: high\n')
                path = self._source(block)
                before = path.read_text(encoding='utf-8')
                result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', clear=['notes'])
                self.assertEqual(result.exit_code, EXIT_CLEAN,
                                 [m.text for m in result.messages])
                expected = before.replace('- notes: First note.\n' + sep + '  status:',
                                          sep + '- status:', 1)
                self.assertEqual(path.read_text(encoding='utf-8'), expected)
                c = read_record(path)['claims'][0]
                self.assertNotIn('notes', c)
                self.assertEqual(str(c['id']).lower(), 'c-aa11bb22cc')



class ClearFieldReviewTests(unittest.TestCase):
    """PR #284 review, round two: `--clear` never reports a removal it did not
    make, reads each value's extent the way YAML does, and every edit is
    checked claim by claim after the rewrite (`_edit_drift`)."""

    HEAD = ('- value: "Anna born 1880"\n'
            '  id: C-aa11bb22cc\n'
            '  type: birth\n'
            '  persons: [P-aaaaaaaaaa]\n'
            '  status: suggested\n'
            '  confidence: high\n')
    NEXT = ('\n- value: "Anna died 1950"\n'
            '  id: C-bb22cc33dd\n'
            '  type: death\n'
            '  persons: [P-aaaaaaaaaa]\n'
            '  status: suggested\n'
            '  confidence: medium\n')

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.path = self.root / 'sources' / 'other' / 'shape_S-1111111111.md'
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, block: str, nl: str = '\n') -> bytes:
        text = ('---\nid: S-1111111111\ntitle: T\nsource_type: other\n---\n\n'
                '## Claims\n```yaml\n' + block + '```\n')
        data = text.replace('\n', nl).encode('utf-8')
        self.path.write_bytes(data)
        return data

    def _clear(self, *fields: str):
        return claim.run_claim(self.root, claim_id='C-aa11bb22cc', clear=list(fields))

    # P2-1: a key this edit cannot find is refused, not reported as done.
    def test_setting_a_key_written_in_an_unusual_form_is_refused_untouched(self) -> None:
        # The set_scalar twin: it used to insert a second key line (YAML keeps
        # the last, so nothing failed) - silent duplication.
        for key, flag_value in (('notes', 'New notes.'), ('place_text', 'Fairview'),
                                ('anchor', 'p. 9')):
            for line in (f'  "{key}": hi\n', f'  {key} : hi\n'):
                with self.subTest(key=key, line=line):
                    before = self._write(self.HEAD + line + self.NEXT)
                    result = claim.run_claim(self.root, claim_id='C-aa11bb22cc',
                                             **{key: flag_value})
                    self.assertEqual(result.exit_code, EXIT_FAILURE)
                    self.assertIn('cannot safely change', result.messages[-1].text)
                    self.assertEqual(self.path.read_bytes(), before)

    def test_a_key_listed_twice_is_refused_untouched(self) -> None:
        # PR #284 re-review: YAML keeps the last copy, so rewriting the first
        # `notes:` reported success while the claim still read the same.
        for kw in ({'notes': 'NEW'}, {'clear': ['notes']}):
            with self.subTest(kw=kw):
                before = self._write(self.HEAD + '  notes: first\n  anchor: p. 1\n'
                                     '  notes: second\n' + self.NEXT)
                result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', **kw)
                self.assertEqual(result.exit_code, EXIT_FAILURE)
                self.assertIn('lists notes: 2 times', result.messages[-1].text)
                self.assertEqual(self.path.read_bytes(), before)

    def test_key_written_in_an_unusual_form_is_refused_untouched(self) -> None:
        for line in ('  "notes": hi\n', '  notes : foo\n'):
            with self.subTest(line=line):
                before = self._write(self.HEAD + line + self.NEXT)
                result = self._clear('notes')
                self.assertEqual(result.exit_code, EXIT_FAILURE)
                self.assertIn('cannot safely remove', result.messages[-1].text)
                self.assertIn('by hand', result.messages[-1].text)
                self.assertEqual(self.path.read_bytes(), before)

    # L1: inside an open quoted scalar a `#` line is text, not a comment.
    def test_multiline_double_quoted_value_goes_whole(self) -> None:
        notes = '  notes: "first line of it\n    # still in string"\n'
        before = self._write(self.HEAD + notes + self.NEXT).decode('utf-8')
        result = self._clear('notes')
        self.assertEqual(result.exit_code, EXIT_CLEAN, [m.text for m in result.messages])
        self.assertEqual(self.path.read_text(encoding='utf-8'), before.replace(notes, '', 1))

    # L2: a comment shallower than a block scalar's text is a real comment.
    def test_comment_shallower_than_block_text_is_kept(self) -> None:
        notes = '  notes: |\n      Indented text.\n      More text.\n'
        comment = '    # a real comment\n'
        before = self._write(self.HEAD + notes + comment + self.NEXT).decode('utf-8')
        result = self._clear('notes')
        self.assertEqual(result.exit_code, EXIT_CLEAN, [m.text for m in result.messages])
        self.assertEqual(self.path.read_text(encoding='utf-8'), before.replace(notes, '', 1))

    def test_block_scalar_with_indentation_indicator_goes_whole(self) -> None:
        notes = '  notes: |2\n      deeper first line\n    text at the stated indent\n'
        before = self._write(self.HEAD + notes + '  anchor: p. 4\n' + self.NEXT).decode('utf-8')
        result = self._clear('notes')
        self.assertEqual(result.exit_code, EXIT_CLEAN, [m.text for m in result.messages])
        self.assertEqual(self.path.read_text(encoding='utf-8'), before.replace(notes, '', 1))
        c = read_record(self.path)['claims'][0]
        self.assertNotIn('notes', c)
        self.assertEqual(c['anchor'], 'p. 4')

    def test_crlf_line_endings_are_kept(self) -> None:
        notes = '  notes: A plain note.\n'
        before = self._write(self.HEAD + notes + self.NEXT, nl='\r\n')
        result = self._clear('notes')
        self.assertEqual(result.exit_code, EXIT_CLEAN, [m.text for m in result.messages])
        self.assertEqual(self.path.read_bytes(),
                         before.replace(notes.replace('\n', '\r\n').encode('utf-8'), b'', 1))

    # L3: a confirmed absence keeps its evidence: negative.
    def test_clearing_evidence_on_a_negated_claim_is_refused(self) -> None:
        before = self._write(self.HEAD + '  negated: true\n  evidence: negative\n' + self.NEXT)
        result = self._clear('evidence')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertIn('§8.6', result.messages[-1].text)
        self.assertEqual(self.path.read_bytes(), before)

    # L4: an empty --date / --place points at --clear.
    def test_empty_date_and_place_point_at_clear(self) -> None:
        before = self._write(self.HEAD + '  date: 1880\n' + self.NEXT)
        for key in ('date', 'place'):
            with self.subTest(key=key):
                result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', **{key: ''})
                self.assertEqual(result.exit_code, EXIT_FAILURE)
                self.assertIn(f'--clear {key}', result.messages[-1].text)
                self.assertEqual(self.path.read_bytes(), before)

    # P2-3: the after-the-edit comparison refuses a rewrite that did more.
    def _with_extra_damage(self, damage):
        real = claim._apply_claim_review

        def broken(text, claim_id, **kw):
            new_text, changed = real(text, claim_id, **kw)
            return damage(new_text), changed
        return mock.patch.object(claim, '_apply_claim_review', side_effect=broken)

    def test_drift_guard_refuses_a_dropped_sibling_key(self) -> None:
        before = self._write(self.HEAD + '  notes: A note.\n  anchor: p. 4\n' + self.NEXT)
        with self._with_extra_damage(lambda t: t.replace('  anchor: p. 4\n', '', 1)):
            result = self._clear('notes')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertIn('also change its anchor', result.messages[-1].text)
        self.assertEqual(self.path.read_bytes(), before)

    def test_drift_guard_refuses_a_cleared_key_left_behind(self) -> None:
        before = self._write(self.HEAD + '  notes: A note.\n' + self.NEXT)
        with self._with_extra_damage(lambda t: t.replace(
                '  confidence: high\n', '  confidence: high\n  notes: A note.\n', 1)):
            result = self._clear('notes')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertIn('leave notes on the claim', result.messages[-1].text)
        self.assertEqual(self.path.read_bytes(), before)

    def test_drift_guard_refuses_a_change_to_another_claim(self) -> None:
        before = self._write(self.HEAD + '  notes: A note.\n' + self.NEXT)
        with self._with_extra_damage(lambda t: t.replace(
                '  confidence: medium\n', '  confidence: low\n', 1)):
            result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', notes='New.')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertIn('another claim', result.messages[-1].text)
        self.assertEqual(self.path.read_bytes(), before)


class ReviewedByStampTests(unittest.TestCase):
    """SPEC §8.4: `reviewed_by:` says who made the review decision - `human`,
    which a tool recording the decision writes by default, or a researcher's
    name. Before this, `fha claim` wrote only the date, so no claim the tools
    reviewed ever said a person had decided it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = _write_source(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _claim(self, cid: str) -> dict:
        claims = read_record(self.source)['claims']
        return {str(c['id']).lower(): c for c in claims}[cid.lower()]

    def _claim_lines(self, cid: str) -> list[str]:
        lines = self.source.read_text(encoding='utf-8').splitlines()
        start = next(i for i, ln in enumerate(lines) if cid.lower() in ln.lower())
        out = []
        for ln in lines[start:]:
            if ln.startswith('- ') and out:
                break
            if ln.startswith('```'):
                break
            out.append(ln.strip())
        return out

    def test_a_status_move_stamps_reviewed_by_human_right_after_reviewed(self) -> None:
        for status in ('accepted', 'disputed', 'rejected', 'suggested', 'superseded'):
            with self.subTest(status=status):
                self.source.write_text(_CLAIM_BLOCK, encoding='utf-8')
                result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', status=status)
                self.assertEqual(result.exit_code, EXIT_CLEAN, result.messages)
                self.assertEqual(self._claim('C-aa11bb22cc').get('reviewed_by'), 'human')
                lines = self._claim_lines('C-aa11bb22cc')
                keys = [ln.split(':', 1)[0] for ln in lines]
                self.assertEqual(keys[keys.index('reviewed') + 1], 'reviewed_by')
                # The sibling claim is untouched.
                self.assertNotIn('reviewed_by', self._claim('C-bb22cc33dd'))

    def test_reviewed_by_override_names_the_researcher(self) -> None:
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', status='accepted',
                                 reviewed_by='Ruth Cole')
        self.assertEqual(result.exit_code, EXIT_CLEAN, result.messages)
        self.assertEqual(self._claim('C-aa11bb22cc')['reviewed_by'], 'Ruth Cole')

    def test_reviewed_by_without_status_is_refused(self) -> None:
        before = self.source.read_bytes()
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', value='x',
                                 reviewed_by='Ruth Cole')
        self.assertNotEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(self.source.read_bytes(), before)

    def test_a_field_only_edit_never_adds_reviewed_by(self) -> None:
        claim.run_claim(self.root, claim_id='C-aa11bb22cc', value='Anna born 1880')
        self.assertNotIn('reviewed_by', self._claim('C-aa11bb22cc'))

    def test_a_rerun_the_record_already_meets_does_not_back_fill(self) -> None:
        # An older accepted claim, written before reviewed_by existed: running
        # the same accept again (same date) is already-done, not a back-fill.
        text = _CLAIM_BLOCK.replace(
            '  status: suggested\n  confidence: high',
            '  status: accepted\n  reviewed: 2026-01-02\n  confidence: high', 1)
        self.source.write_text(text, encoding='utf-8')
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', status='accepted',
                                 reviewed='2026-01-02')
        self.assertEqual(result.data['status'], 'already-done')
        self.assertEqual(self.source.read_text(encoding='utf-8'), text)

    def test_a_batch_stamps_every_claim(self) -> None:
        result = claim.run_claim_batch(self.root, claim_ids=['C-aa11bb22cc', 'C-bb22cc33dd'],
                                       status='accepted')
        self.assertEqual(result.exit_code, EXIT_CLEAN, result.messages)
        for cid in ('C-aa11bb22cc', 'C-bb22cc33dd'):
            self.assertEqual(self._claim(cid).get('reviewed_by'), 'human')

    def test_claim_new_accepted_stamps_it_and_suggested_does_not(self) -> None:
        accepted = claim.run_claim_new(self.root, source_id='S-1111111111',
                                       claim_type='residence', value='Lived in Topeka')
        self.assertEqual(accepted.exit_code, EXIT_CLEAN, accepted.messages)
        self.assertEqual(self._claim(accepted.data['claim_id']).get('reviewed_by'), 'human')
        lines = self._claim_lines(accepted.data['claim_id'])
        keys = [ln.split(':', 1)[0] for ln in lines]
        self.assertEqual(keys[keys.index('reviewed') + 1], 'reviewed_by')

        drafted = claim.run_claim_new(self.root, source_id='S-1111111111',
                                      claim_type='residence', value='Lived in Salina',
                                      status='suggested')
        self.assertEqual(drafted.exit_code, EXIT_CLEAN, drafted.messages)
        self.assertNotIn('reviewed_by', self._claim(drafted.data['claim_id']))
        self.assertNotIn('reviewed', self._claim(drafted.data['claim_id']))

    def test_claim_new_suggested_refuses_a_reviewer_name(self) -> None:
        before = self.source.read_bytes()
        result = claim.run_claim_new(self.root, source_id='S-1111111111',
                                     claim_type='residence', value='Lived in Salina',
                                     status='suggested', reviewed_by='Ruth Cole')
        self.assertNotEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(self.source.read_bytes(), before)

    def test_cli_flag_reaches_the_record(self) -> None:
        import fha
        (self.root / 'fha.yaml').write_text(
            'roots:' + chr(10) + '  documents: documents' + chr(10), encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = fha.main(['claim', 'C-aa11bb22cc', '--status', 'accepted',
                           '--reviewed-by', 'Ruth Cole', '--root', str(self.root)])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(self._claim('C-aa11bb22cc')['reviewed_by'], 'Ruth Cole')


class PersonsWrittenWithNamesTests(unittest.TestCase):
    """D6 / SPEC §8.7: `fha claim new` and a `--persons` edit write the
    claim's people one per line with the name beside the ID, read from the
    person record - so the claim says who it is about without the tools. A
    person whose record has no usable name is written as the bare ID."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = _write_source(self.root)
        _write_person(self.root, 'P-aaaaaaaaaa', 'Anna Smith')
        _write_person(self.root, 'P-bbbbbbbbbb', 'unknown')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_claim_new(self) -> None:
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='residence',
            value='Lived in Fairview', persons=['P-aaaaaaaaaa', 'P-bbbbbbbbbb'],
            status='suggested')
        self.assertEqual(result.exit_code, EXIT_CLEAN, result.messages)
        text = self.source.read_text(encoding='utf-8')
        self.assertIn('  persons:\n    - "[[p-aaaaaaaaaa|Anna Smith]]"\n    - p-bbbbbbbbbb\n', text)

    def test_persons_edit_replaces_the_list(self) -> None:
        result = claim.run_claim(self.root, claim_id='C-bb22cc33dd',
                                 persons=['P-bbbbbbbbbb', 'P-aaaaaaaaaa'])
        self.assertEqual(result.exit_code, EXIT_CLEAN, result.messages)
        text = self.source.read_text(encoding='utf-8')
        self.assertIn('  type: death\n  persons:\n    - p-bbbbbbbbbb\n'
                      '    - "[[p-aaaaaaaaaa|Anna Smith]]"\n  status: suggested\n', text)
        # The other claim is untouched, and the edit reads back as the IDs.
        self.assertIn('  type: birth\n  persons: [P-aaaaaaaaaa]\n', text)
        again = claim.run_claim(self.root, claim_id='C-bb22cc33dd',
                                persons=['P-bbbbbbbbbb', 'P-aaaaaaaaaa'])
        self.assertEqual(again['status'], 'already-done')


class PersonsEditKeepsCommentsTests(unittest.TestCase):
    """A `--persons` edit never loses a note a person wrote beside a
    persons: entry, nor a display name they chose."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = _write_source(self.root)
        text = self.source.read_text(encoding='utf-8').replace(
            "  type: death\n  persons: [P-aaaaaaaaaa]\n",
            "  type: death\n  persons:\n    - P-aaaaaaaaaa  # informant, per the clerk\n"
            "    - \"[[P-cccccccccc|Cal]]\"\n")
        self.source.write_text(text, encoding='utf-8')
        _write_person(self.root, 'P-aaaaaaaaaa', 'Anna Smith')
        _write_person(self.root, 'P-bbbbbbbbbb', 'Ben Smith')
        _write_person(self.root, 'P-cccccccccc', 'Cal Smith')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_note_kept_display_kept(self) -> None:
        result = claim.run_claim(self.root, claim_id='C-bb22cc33dd',
                                 persons=['P-aaaaaaaaaa', 'P-cccccccccc', 'P-bbbbbbbbbb'])
        self.assertEqual(result.exit_code, EXIT_CLEAN, result.messages)
        text = self.source.read_text(encoding='utf-8')
        self.assertIn('  persons:\n'
                      '    - "[[p-aaaaaaaaaa|Anna Smith]]"  # informant, per the clerk\n'
                      '    - "[[p-cccccccccc|Cal]]"\n'
                      '    - "[[p-bbbbbbbbbb|Ben Smith]]"\n', text)
class PronounsTests(unittest.TestCase):
    """SPEC A.7: pronouns come from `gender:` when present, else `sex:`, else
    neutral - so a line about a trans woman recorded `sex: M`, `gender: woman`
    reads "she", and a record that says neither never guesses."""

    def test_gender_wins_over_sex(self) -> None:
        self.assertEqual(claim._pronouns({'sex': 'M', 'gender': 'woman'}), ('she', 'her'))
        self.assertEqual(claim._pronouns({'sex': 'F', 'gender': 'Man'}), ('he', 'him'))

    def test_gender_read_by_its_first_word(self) -> None:
        self.assertEqual(claim._pronouns({'gender': 'she/her'}), ('she', 'her'))
        self.assertEqual(claim._pronouns({'gender': 'He/Him'}), ('he', 'him'))
        self.assertEqual(claim._pronouns({'sex': 'M', 'gender': 'female-identified'}),
                         ('she', 'her'))
        self.assertEqual(claim._pronouns({'gender': 'they/them'}), ('they', 'them'))

    def test_unlisted_gender_is_neutral(self) -> None:
        self.assertEqual(claim._pronouns({'sex': 'F', 'gender': 'non-binary'}),
                         ('they', 'them'))

    def test_sex_when_no_gender(self) -> None:
        self.assertEqual(claim._pronouns({'sex': 'F'}), ('she', 'her'))
        self.assertEqual(claim._pronouns({'sex': 'M', 'gender': ''}), ('he', 'him'))

    def test_neither_is_neutral(self) -> None:
        self.assertEqual(claim._pronouns({}), ('they', 'them'))
        self.assertEqual(claim._pronouns({'sex': 'unknown'}), ('they', 'them'))


class HashInClaimTextTests(unittest.TestCase):
    """#355: a value like "Lot #4 of block 2" written unquoted reads as "Lot"
    (YAML starts a comment at ` #`), so the timeline and site showed a stub.
    Everything `fha claim` writes keeps the whole text, in the file and in the
    index; and the command lint's W156 suggests repairs a hand-typed one."""

    SOURCE = 's-7k2m9q4w1e'       # the 1850 census source in the example archive
    CLAIM = 'c-9r4t7y2b6h'        # its accepted residence claim
    PERSON = 'p-075114a0f8'
    TEXT = 'Lot #4 of block 2'

    @classmethod
    def setUpClass(cls) -> None:
        if not EXAMPLE.is_dir():
            raise unittest.SkipTest('example-archive not present')
        import index
        import lint
        cls.index = index
        cls.lint = lint

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / 'archive'
        shutil.copytree(EXAMPLE, self.root)
        self.config = load_fha_yaml(self.root)
        self.source = next((self.root / 'sources').rglob(f'*_{self.SOURCE}.md'))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _indexed(self, cid: str) -> tuple:
        self.index.build_index(self.root, self.config)
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        try:
            return conn.execute(
                'SELECT value, place_text FROM claims WHERE id=?', (cid.lower(),)).fetchone()
        finally:
            conn.close()

    def _w156(self) -> list:
        return [m for m in self.lint.run_lint(self.root, self.config).messages
                if m.code == 'W156']

    def test_the_shared_quoting_rule_keeps_a_hash(self) -> None:
        # `fha claim`, `fha convert-mining` and `fha gedcom import` all write
        # claim text through `_lib.yaml_inline`.
        import yaml
        for text in (self.TEXT, 'Apt # B', 'Motor #444932', 'tab\t#here', 'ends #'):
            with self.subTest(text=text):
                line = f'value: {_lib_mod.yaml_inline(text)}'
                self.assertEqual(yaml.safe_load(line)['value'], text)

    def test_a_new_claim_keeps_every_hash_field_whole(self) -> None:
        result = claim.run_claim_new(
            self.root, source_id=self.SOURCE, claim_type='residence', value=self.TEXT,
            persons=[self.PERSON], place_text='12 Main St #3', anchor='page 4 #2',
            notes='Apt # B on the plat', status='suggested')
        self.assertEqual(result.exit_code, EXIT_CLEAN, result.messages)
        cid = result['claim_id']
        written = {c['id']: c for c in read_record(self.source)['claims']}[cid]
        self.assertEqual(written['value'], self.TEXT)
        self.assertEqual(written['notes'], 'Apt # B on the plat')
        self.assertEqual(written['anchor'], 'page 4 #2')
        self.assertEqual(self._indexed(cid), (self.TEXT, '12 Main St #3'))
        self.assertEqual(self._w156(), [])

    def test_the_fix_lint_suggests_repairs_a_hand_typed_value(self) -> None:
        text = self.source.read_text(encoding='utf-8')
        old = '- value: Caleb C. Hartley household residing in Easton, New York (1850)'
        self.assertIn(old, text)
        self.source.write_text(text.replace(old, f'- value: {self.TEXT}'), encoding='utf-8')
        self.assertEqual(self._indexed(self.CLAIM)[0], 'Lot')
        (w,) = self._w156()
        self.assertIn(f'--value {_lib_mod.shell_quote(self.TEXT)}', w.text)

        # The accepted claim changes only on the human's own command.
        result = claim.run_claim(self.root, claim_id=self.CLAIM, value=self.TEXT)
        self.assertEqual(result.exit_code, EXIT_CLEAN, result.messages)
        self.assertEqual(self._indexed(self.CLAIM)[0], self.TEXT)
        self.assertEqual(self._w156(), [])


if __name__ == '__main__':
    unittest.main()
