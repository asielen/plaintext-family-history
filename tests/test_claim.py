"""
test_claim.py — fha claim: human-directed claim review write-back.

Covers the surgical `## Claims` edit (`_apply_claim_review`), the `run_claim`
contract (accept/dispute/reject/needs-review round-trip, default-today on accept,
dry-run writes nothing, malformed/unknown C-id), and an end-to-end check that
`fha index` + `fha lint` reflect a status change on a real archive (an accepted
vital relieves the right W101).
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
from _lib import EXIT_CLEAN, EXIT_FAILURE, EXIT_WARNINGS, load_fha_yaml, read_record

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


# ── End-to-end: index + lint reflect the status change ──────────────────────────

class ClaimIndexLintIntegrationTests(unittest.TestCase):
    """A real archive: demoting an accepted vital reopens its W101 gap, and
    re-accepting it through the tool relieves the gap again — proving fha index
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
