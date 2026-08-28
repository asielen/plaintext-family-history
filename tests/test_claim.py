"""
test_claim.py - fha claim: human-directed claim review write-back.

Covers the surgical `## Claims` edit (`_apply_claim_review`), the `run_claim`
contract (accept/dispute/reject/needs-review round-trip, default-today on accept,
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
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import claim
from _lib import (
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    claims_edit_problem,
    load_fha_yaml,
    read_record,
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
            text, 'C-aa11bb22cc', status='needs-review', reviewed='2026-06-24')
        self.assertTrue(changed)
        self.assertIn('status: needs-review', new)
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
        self.assertEqual(result['source_id'], 'S-1111111111')
        self.assertRegex(result['source_id'], r'^S-[0-9a-hjkmnp-tv-z]{10}$')
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

    def test_needs_review_round_trip(self) -> None:
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', status='needs-review')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(self._status_of('C-aa11bb22cc'), 'needs-review')

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
            _QUOTED_SOURCE, self.TARGET, status='needs-review', reviewed='2026-07-05')
        self.assertTrue(changed)
        rec = {c['id']: c for c in read_record_from_text(new)}
        self.assertEqual(rec[self.TARGET]['status'], 'needs-review')
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

        # Demote to needs-review → the vital gap reopens; index shows the move.
        demote = claim.run_claim(self.root, claim_id=self.BIRTH_CLAIM, status='needs-review')
        self.assertEqual(demote.exit_code, EXIT_CLEAN)
        self.assertEqual(self._claim_status_in_index(self.BIRTH_CLAIM), 'needs-review')
        self.assertTrue(self._w101_birth_for_person())

        # Re-accept (default today) → gap relieved again; index reflects accepted.
        accept = claim.run_claim(self.root, claim_id=self.BIRTH_CLAIM, status='accepted')
        self.assertEqual(accept.exit_code, EXIT_CLEAN)
        self.assertEqual(self._claim_status_in_index(self.BIRTH_CLAIM), 'accepted')
        self.assertFalse(self._w101_birth_for_person())


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
        self.assertEqual(rec['persons'], ['P-aaaaaaaaaa'])
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
        self.assertIn('P-zzzzzzzzzz', text)
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
        self.assertIn('place: L-baba9801fa', text)
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
        self.assertEqual(str(rec['place']), 'L-baba9801fa')
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
        self.assertEqual(str(rec['place']), 'L-baba9801fa')

    def test_case_and_whitespace_noise_still_matches_exactly(self) -> None:
        result = claim.run_claim_new(
            self.root, source_id='S-1111111111', claim_type='residence',
            value='Lived there', persons=['P-aaaaaaaaaa'],
            place_text='  topeka,   KANSAS  ')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        rec = self._claims()[result['claim_id']]
        self.assertEqual(str(rec['place']), 'L-baba9801fa')

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
        self.assertEqual(str(rec['place']), 'L-9e2210ab44')
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
                self.assertEqual(claims[0]['persons'], ['P-bbbbbbbbbb', 'P-cccccccccc'])

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
                self.assertEqual(claims[0]['persons'], ['P-cccccccccc'])
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
        self.assertEqual(c['persons'], ['P-cccccccccc'])
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
            self.assertEqual(rec['C-aa11bb22cc']['persons'], ['P-bbbbbbbbbb'])
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
        self.assertEqual(self._claims()['C-aa11bb22cc']['persons'], ['P-bbbbbbbbbb'])

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
        self.assertEqual(str(c['place']), 'L-baba9801fa')
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
        self.assertIn(self.DEAD, text)
        self.assertIn('W133', text)
        self.assertIn('#126', text)
        # Nothing written.
        self.assertEqual(self.source.read_text(encoding='utf-8'), before)

    def test_keeping_the_roled_person_in_the_new_list_succeeds(self) -> None:
        self._write(f'  roles:\n    deceased: [{self.DEAD}]\n')
        result = claim.run_claim(
            self.root, claim_id='C-3333333333', persons=[self.DEAD, self.OTHER])
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(self._claims()['C-3333333333']['persons'], [self.DEAD, self.OTHER])

    def test_multiple_orphaned_targets_are_all_named(self) -> None:
        self._write(f'  roles:\n    deceased: [{self.DEAD}]\n    witness: [{self.OTHER}]\n')
        result = claim.run_claim(self.root, claim_id='C-3333333333', persons=[self.WIDOW])
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn(self.DEAD, text)
        self.assertIn(self.OTHER, text)

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
        self.assertEqual(str(rec['place']), 'L-baba9801fa')
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
        self.assertEqual(str(rec['place']), 'L-9e2210ab44')   # untouched
        self.assertEqual(rec['place_text'], 'Topeka, Kansas')
        text = ' '.join(m.text for m in second.messages)
        self.assertNotIn('matched the registered place', text)

    def test_explicit_place_on_the_edit_verb_is_never_overridden(self) -> None:
        result = claim.run_claim(
            self.root, claim_id='C-aa11bb22cc',
            place='L-9e2210ab44', place_text='Topeka, Kansas')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        rec = self._claims()['C-aa11bb22cc']
        self.assertEqual(str(rec['place']), 'L-9e2210ab44')


# ── Batch status moves (TOOLING §3b amendment): run_claim_batch ─────────────────

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
        self.assertEqual(result['applied'], list(self.BOTH))
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
        # Batch-reject (and batch-needs-review) are as legitimate as
        # batch-accept: the gate is status-ONLY, not accept-only.
        result = claim.run_claim_batch(self.root, claim_ids=list(self.BOTH),
                                       status='rejected')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        rec = self._claims()
        for cid in self.BOTH:
            self.assertEqual(rec[cid]['status'], 'rejected')

    def test_batch_needs_review_works(self) -> None:
        result = claim.run_claim_batch(self.root, claim_ids=list(self.BOTH),
                                       status='needs-review')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        rec = self._claims()
        for cid in self.BOTH:
            self.assertEqual(rec[cid]['status'], 'needs-review')

    def test_batch_write_failure_leaves_the_rest_untouched(self) -> None:
        # A later item's write failing must leave THAT claim exactly as it was,
        # and the batch must say so. The promise rests on run_claim writing
        # atomically (write_text_exact_atomic) - a truncating write that died
        # mid-write would leave the source torn, and the advised retry would
        # then read an unparseable file. Patching that writer is what proves it
        # is wired in: a regression to the non-atomic writer would bypass this
        # patch, write the second claim, and fail the untouched assertion.
        before = {c['id']: str(c['status'])
                  for c in read_record(self.source)['claims']}
        orig = claim.write_text_exact_atomic
        calls = {'n': 0}

        def flaky(path, text):
            calls['n'] += 1
            if calls['n'] == 2:
                raise OSError('simulated disk full on the second write')
            return orig(path, text)
        claim.write_text_exact_atomic = flaky
        try:
            result = claim.run_claim_batch(self.root, claim_ids=list(self.BOTH),
                                           status='accepted', reviewed='2026-07-20')
        finally:
            claim.write_text_exact_atomic = orig

        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(len(result['applied']), 1)   # only the first landed
        rec = self._claims()
        self.assertEqual(rec[self.BOTH[0]]['status'], 'accepted')
        self.assertEqual(str(rec[self.BOTH[1]]['status']), before[self.BOTH[1]])
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('left as they were', text)

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
        self.assertIn('C-0000000000', text)
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
            self.assertIn(cid, text)
        self.assertIn('[dry-run]', text)
        # One closing trailer for the whole batch, not one per claim.
        trailers = [m for m in result.messages if 'Re-run without --dry-run' in m.text]
        self.assertEqual(len(trailers), 1)

    def test_duplicate_id_deduped_preserving_order_with_a_note(self) -> None:
        result = claim.run_claim_batch(
            self.root, claim_ids=['C-aa11bb22cc', 'C-bb22cc33dd', 'C-AA11BB22CC'],
            status='accepted')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result['applied'], list(self.BOTH))
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
        self.assertEqual(result['claim_id'], 'C-aa11bb22cc')
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
        self.assertEqual(result['claim_id'], 'C-aa11bb22cc')
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
        self.assertEqual(result['applied'], ['C-dd44ee55ff'])
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('Stopped at C-ee55ff66gh', text)
        self.assertIn('1 of 2 claims applied (C-dd44ee55ff)', text)
        # The finish command carries ONLY the unapplied ids.
        self.assertIn('`fha claim C-ee55ff66gh --status accepted`', text)
        self.assertNotIn('fha claim C-dd44ee55ff', text)
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
            '`fha claim C-dd44ee55ff C-ee55ff66gh --status accepted '
            '--reviewed 2026-07-20 --dry-run`', text)

    def test_empty_id_list_is_a_plain_refusal(self) -> None:
        result = claim.run_claim_batch(self.root, claim_ids=[], status='accepted')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertTrue(result.messages)


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
                                         '--status', 'needs-review',
                                         '--root', str(self.root)])
        self.assertEqual(rc, EXIT_CLEAN)
        rec = self._claims()
        self.assertEqual(rec['C-aa11bb22cc']['status'], 'needs-review')
        self.assertEqual(rec['C-bb22cc33dd']['status'], 'needs-review')

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
    `Robert Justin "Bob" Knipscheer` arrives at the tool as
    `Robert Justin Bob Knipscheer` (embedded double quotes lost before this
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
        correct = 'Robert Justin "Bob" Knipscheer'
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', value=correct)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        all_text = ' '.join(m.text for m in result.messages)
        self.assertIn(correct, all_text)

    def test_a_mangled_value_is_now_visibly_different_from_a_correct_one(self) -> None:
        # The corrupted write LOOKS different in the output now - that is
        # the whole fix. The source is re-seeded between the two writes so
        # both start from the exact same claim state and are comparable.
        correct = 'Robert Justin "Bob" Knipscheer'
        mangled = 'Robert Justin Bob Knipscheer'   # what PowerShell 5.1 delivers
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
        value = 'Robert Justin "Bob" Knipscheer, laborer'
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


if __name__ == '__main__':
    unittest.main()
