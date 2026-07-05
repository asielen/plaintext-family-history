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
"""

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

    def test_accept_defaults_reviewed_to_today_not_refused(self) -> None:
        result = claim.run_claim(self.root, claim_id='C-aa11bb22cc', status='accepted')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result['reviewed'], claim._today())
        rec = {c['id']: c for c in read_record(self.source)['claims']}
        self.assertEqual(str(rec['C-aa11bb22cc']['reviewed']), claim._today())

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


if __name__ == '__main__':
    unittest.main()
