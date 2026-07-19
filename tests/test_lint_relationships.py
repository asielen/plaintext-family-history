"""
test_lint_relationships.py - the relationship spine's tooling half (SPEC §9 / §8.2,
TOOLING §3).

Covers:
  * the roles-based parent/child migration: a parent edge is found by its roles:
    map, so both a new `subtype: biological` claim and a legacy `subtype: child-of`
    claim are detected (back-compat);
  * W115 relationship-reconciliation drift (missing claim, subtype mismatch, an
    opted-in block that omits an accepted kin claim);
  * W116 missing reciprocal edge, and `--fix-reciprocal` adding the mirror;
  * the needs-sourcing backlog listing unsourced / hypothesis entries;
  * a clean, fully reconciled pair producing neither W115 nor W116.

Like test_lint.py, it builds tiny real archive trees and drives lint's tool logic
directly (`_run_lint_core` / `run_lint`) over a fresh in-memory registry.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import lint
import index as index_mod
from _lib import load_fha_yaml

# Valid Crockford IDs (no i/l/o/u), distinct per record.
CHILD = 'P-aaaaaaaaaa'
PARENT = 'P-bbbbbbbbbb'
OTHERKID = 'P-cccccccccc'
CLAIM = 'C-dddddddddd'
CLAIM2 = 'C-eeeeeeeeee'
SOURCE = 'S-ffffffffff'


def _person(pid: str, surname: str, given: str, *, relationships: str = '',
            tier: str = 'stub') -> tuple[str, str]:
    """Return (relative filename, file text) for a person record. `relationships`
    is YAML lines already indented for the frontmatter block (or '')."""
    block = (relationships.rstrip('\n') + '\n') if relationships else ''
    text = (
        '---\n'
        f'id: {pid}\n'
        f'name: {given} {surname}\n'
        'living: false\n'
        f'tier: {tier}\n'
        f'{block}'
        '---\n\n'
        f'# {given} {surname}\n\n## Biography\n\nText.\n'
    )
    fname = f'people/stubs/{surname.lower()}__{given.lower()}_{pid}.md'
    return fname, text


def _rel_source(sid: str, cid: str, child: str, parent: str, *,
                subtype: str = 'biological', status: str = 'accepted',
                extra_claim: str = '') -> tuple[str, str]:
    """A source carrying one parent/child relationship claim (plus optional extra)."""
    text = (
        '---\n'
        f'id: {sid}\n'
        'title: Relationship source\n'
        'source_type: other\n'
        '---\n\n'
        '## Claims\n\n```yaml\n'
        f'- value: "child of parent"\n'
        f'  id: {cid}\n'
        '  type: relationship\n'
        f'  subtype: {subtype}\n'
        f'  persons: [{child}, {parent}]\n'
        '  roles:\n'
        f'    child: {child}\n'
        f'    parent: {parent}\n'
        f'  status: {status}\n'
        '  reviewed: 2026-01-01\n'
        '  confidence: high\n'
        '  information: primary\n'
        '  evidence: direct\n'
        '  notes: A test relationship claim.\n'
        f'{extra_claim}'
        '```\n'
    )
    return f'sources/notes/{sid.lower()}.md', text


def _build(files: dict[str, str]) -> Path:
    tmp = tempfile.mkdtemp()
    root = Path(tmp)
    (root / 'people' / 'stubs').mkdir(parents=True)
    (root / 'sources' / 'notes').mkdir(parents=True)
    (root / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8')
    return root


def _codes(findings, code):
    return [f for f in findings if f.code == code]


# Reusable relationships blocks (frontmatter-indented).
def _parent_entry(to_pid: str, name: str, *, subtype: str = 'biological',
                  claim: str = CLAIM, role: str = 'parent', status: str = '') -> str:
    lines = ['relationships:', f'  - to: "[[{to_pid}|{name}]]"', f'    type: {role}']
    if subtype:
        lines.append(f'    subtype: {subtype}')
    if claim:
        lines.append(f'    claim: "[[{claim}]]"')
    if status:
        lines.append(f'    status: {status}')
    return '\n'.join(lines)


class RolesBasedDetectionTests(unittest.TestCase):
    """The migration: a parent edge is identified by roles:, regardless of the
    subtype string - so `biological` (new) and `child-of` (legacy) both count."""

    def test_biological_and_legacy_child_of_both_detected(self) -> None:
        child_a, _ = _person(CHILD, 'kid', 'ann')
        parent_a, _ = _person(PARENT, 'kid', 'bob')
        other_a, _ = _person(OTHERKID, 'kid', 'cara')
        # CLAIM uses the new vocab; CLAIM2 uses the legacy child-of role-marker.
        legacy = (
            '- value: "legacy child of parent"\n'
            f'  id: {CLAIM2}\n'
            '  type: relationship\n'
            '  subtype: child-of\n'
            f'  persons: [{OTHERKID}, {PARENT}]\n'
            '  roles:\n'
            f'    child: {OTHERKID}\n'
            f'    parent: {PARENT}\n'
            '  status: accepted\n'
            '  reviewed: 2026-01-01\n'
            '  confidence: high\n'
            '  information: primary\n'
            '  evidence: direct\n'
            '  notes: legacy.\n'
        )
        src_rel, src_text = _rel_source(SOURCE, CLAIM, CHILD, PARENT, extra_claim=legacy)
        root = _build({child_a: _person(CHILD, 'kid', 'ann')[1],
                       parent_a: _person(PARENT, 'kid', 'bob')[1],
                       other_a: _person(OTHERKID, 'kid', 'cara')[1],
                       src_rel: src_text})
        _findings, registry = lint._run_lint_core(root, {})
        children_of = lint._build_children_of(registry)
        parent_norm = PARENT.lower()
        self.assertIn(parent_norm, children_of)
        # Both the biological child and the legacy child-of child map to the parent.
        self.assertEqual(children_of[parent_norm], {CHILD.lower(), OTHERKID.lower()})

    def test_index_derives_parent_edge_from_biological_subtype(self) -> None:
        child_f, child_t = _person(CHILD, 'kid', 'ann')
        parent_f, parent_t = _person(PARENT, 'kid', 'bob')
        src_rel, src_text = _rel_source(SOURCE, CLAIM, CHILD, PARENT)
        root = _build({child_f: child_t, parent_f: parent_t, src_rel: src_text})
        config = load_fha_yaml(root)
        index_mod.build_index(root, config)
        import sqlite3
        conn = sqlite3.connect(str(root / '.cache' / 'index.sqlite'))
        try:
            rows = conn.execute(
                "SELECT person_id, rel, other_id FROM relationships ORDER BY rel"
            ).fetchall()
        finally:
            conn.close()
        self.assertIn((CHILD.lower(), 'parent', PARENT.lower()), rows)
        self.assertIn((PARENT.lower(), 'child', CHILD.lower()), rows)


class ReconciliationW115Tests(unittest.TestCase):
    def test_clean_pair_has_no_w115_or_w116(self) -> None:
        child_f, child_t = _person(
            CHILD, 'kid', 'ann',
            relationships=_parent_entry(PARENT, 'Bob Kid', role='parent'))
        parent_f, parent_t = _person(
            PARENT, 'kid', 'bob',
            relationships=_parent_entry(CHILD, 'Ann Kid', role='child'))
        src_rel, src_text = _rel_source(SOURCE, CLAIM, CHILD, PARENT)
        root = _build({child_f: child_t, parent_f: parent_t, src_rel: src_text})
        findings, _ = lint._run_lint_core(root, {})
        self.assertEqual(_codes(findings, 'W115'), [])
        self.assertEqual(_codes(findings, 'W116'), [])

    def test_entry_citing_missing_claim_is_w115(self) -> None:
        child_f, child_t = _person(
            CHILD, 'kid', 'ann',
            relationships=_parent_entry(PARENT, 'Bob Kid', claim='C-9999999999'))
        parent_f, parent_t = _person(PARENT, 'kid', 'bob')
        src_rel, src_text = _rel_source(SOURCE, CLAIM, CHILD, PARENT)
        root = _build({child_f: child_t, parent_f: parent_t, src_rel: src_text})
        findings, _ = lint._run_lint_core(root, {})
        w115 = _codes(findings, 'W115')
        self.assertTrue(any('9999999999' in f.message for f in w115))

    def test_subtype_mismatch_is_w115(self) -> None:
        # The claim says adoptive; the entry says biological → drift.
        child_f, child_t = _person(
            CHILD, 'kid', 'ann',
            relationships=_parent_entry(PARENT, 'Bob Kid', subtype='biological'))
        # Mirror on the parent matches the claim's nature, so only the child drifts.
        parent_f, parent_t = _person(
            PARENT, 'kid', 'bob',
            relationships=_parent_entry(CHILD, 'Ann Kid', subtype='adoptive', role='child'))
        src_rel, src_text = _rel_source(SOURCE, CLAIM, CHILD, PARENT, subtype='adoptive')
        root = _build({child_f: child_t, parent_f: parent_t, src_rel: src_text})
        findings, _ = lint._run_lint_core(root, {})
        w115 = _codes(findings, 'W115')
        self.assertTrue(any('subtype' in f.message and 'biological' in f.message for f in w115))

    def test_opted_in_block_missing_a_claim_is_w115(self) -> None:
        # The child's block applies CLAIM (parent) but not CLAIM2 (their own child).
        child_f, child_t = _person(
            CHILD, 'kid', 'ann',
            relationships=_parent_entry(PARENT, 'Bob Kid', role='parent'))
        parent_f, parent_t = _person(PARENT, 'kid', 'bob',
                                     relationships=_parent_entry(CHILD, 'Ann Kid', role='child'))
        grandkid_f, grandkid_t = _person(OTHERKID, 'kid', 'cara')
        # A second accepted claim: CHILD is a parent of OTHERKID, unreferenced.
        extra = (
            '- value: "grandchild of child"\n'
            f'  id: {CLAIM2}\n'
            '  type: relationship\n'
            '  subtype: biological\n'
            f'  persons: [{OTHERKID}, {CHILD}]\n'
            '  roles:\n'
            f'    child: {OTHERKID}\n'
            f'    parent: {CHILD}\n'
            '  status: accepted\n'
            '  reviewed: 2026-01-01\n'
            '  confidence: high\n'
            '  information: primary\n'
            '  evidence: direct\n'
            '  notes: extra.\n'
        )
        src_rel, src_text = _rel_source(SOURCE, CLAIM, CHILD, PARENT, extra_claim=extra)
        root = _build({child_f: child_t, parent_f: parent_t,
                       grandkid_f: grandkid_t, src_rel: src_text})
        findings, _ = lint._run_lint_core(root, {})
        w115 = _codes(findings, 'W115')
        self.assertTrue(any(CLAIM2.lower() in f.message.lower() for f in w115))


class ReciprocityW116Tests(unittest.TestCase):
    def _one_sided(self) -> Path:
        """CHILD records a sourced parent edge; PARENT has no block (no mirror)."""
        child_f, child_t = _person(
            CHILD, 'kid', 'ann',
            relationships=_parent_entry(PARENT, 'Bob Kid', role='parent'))
        parent_f, parent_t = _person(PARENT, 'kid', 'bob')
        src_rel, src_text = _rel_source(SOURCE, CLAIM, CHILD, PARENT)
        return _build({child_f: child_t, parent_f: parent_t, src_rel: src_text})

    def test_missing_mirror_is_w116(self) -> None:
        root = self._one_sided()
        findings, _ = lint._run_lint_core(root, {})
        w116 = _codes(findings, 'W116')
        self.assertEqual(len(w116), 1)
        self.assertIn('child', w116[0].message)   # the expected mirror role

    def test_fix_reciprocal_dry_run_writes_nothing(self) -> None:
        root = self._one_sided()
        parent_path = root / 'people' / 'stubs' / f'kid__bob_{PARENT}.md'
        before = parent_path.read_text(encoding='utf-8')
        result = lint.run_lint(root, {}, fix_reciprocal=True, dry_run=True)
        self.assertEqual(parent_path.read_text(encoding='utf-8'), before)
        self.assertTrue(any('would add' in p.lower() for p in result.data['progress']))

    def test_fix_reciprocal_adds_mirror_then_clean(self) -> None:
        root = self._one_sided()
        parent_path = root / 'people' / 'stubs' / f'kid__bob_{PARENT}.md'
        result = lint.run_lint(root, {}, fix_reciprocal=True)
        text = parent_path.read_text(encoding='utf-8')
        self.assertIn('relationships:', text)
        self.assertIn('type: child', text)
        self.assertIn(CLAIM, text)
        self.assertTrue(any(str(parent_path) == c for c in result.changed))
        # Re-lint: the mirror now reconciles, so no W116 (and no new W115).
        findings, _ = lint._run_lint_core(root, {})
        self.assertEqual(_codes(findings, 'W116'), [])
        self.assertEqual(_codes(findings, 'W115'), [])

    def test_fix_reciprocal_owner_name_with_quote_is_escaped(self) -> None:
        # Sweep of PR #30's YAML-quoting review fixes: `owner_name` is read
        # from an EXISTING person record, never validated by this fixer - a
        # human may have typed a quote into it long before --fix-reciprocal
        # ran (same class as person.py's relationship-mirror fix).
        child_f, child_t = _person(
            CHILD, 'kid', 'Ann "Annie"',
            relationships=_parent_entry(PARENT, 'Bob Kid', role='parent'))
        parent_f, parent_t = _person(PARENT, 'kid', 'bob')
        src_rel, src_text = _rel_source(SOURCE, CLAIM, CHILD, PARENT)
        root = _build({child_f: child_t, parent_f: parent_t, src_rel: src_text})
        lint.run_lint(root, {}, fix_reciprocal=True)
        parent_path = root / 'people' / 'stubs' / f'kid__bob_{PARENT}.md'

        from _lib import read_record
        rec = read_record(parent_path)
        self.assertEqual(rec['parse_errors'], [])
        rel = rec['meta']['relationships'][0]
        self.assertEqual(rel['to'], '[[P-aaaaaaaaaa|Ann "Annie" kid]]')

    def test_fix_reciprocal_subtype_with_yaml_chars_is_quoted(self) -> None:
        # Same sweep, other free-text field: the claim's subtype is whatever a
        # human once typed (not restricted to the KIN_SUBTYPES vocabulary) - a
        # ': ' or ' #' in it written bare would corrupt the mirrored record's
        # frontmatter. person._relationship_item_lines already quotes it; the
        # lint mirror writer must too.
        child_f, child_t = _person(
            CHILD, 'kid', 'ann',
            relationships=_parent_entry(PARENT, 'Bob Kid', role='parent',
                                        subtype='"step: half"'))
        parent_f, parent_t = _person(PARENT, 'kid', 'bob')
        src_rel, src_text = _rel_source(SOURCE, CLAIM, CHILD, PARENT,
                                        subtype='"step: half"')
        root = _build({child_f: child_t, parent_f: parent_t, src_rel: src_text})
        lint.run_lint(root, {}, fix_reciprocal=True)
        parent_path = root / 'people' / 'stubs' / f'kid__bob_{PARENT}.md'

        from _lib import read_record
        rec = read_record(parent_path)
        self.assertEqual(rec['parse_errors'], [])
        rel = rec['meta']['relationships'][0]
        self.assertEqual(rel['subtype'], 'step: half')


class NeedsSourcingBacklogTests(unittest.TestCase):
    def test_unsourced_and_hypothesis_entries_land_on_backlog(self) -> None:
        rel = '\n'.join([
            'relationships:',
            '  - to: "[[Walter Doe]]"',
            '    type: parent',
            '    status: hypothesis',
            '  - to: Robert Roe',
            '    type: parent',
        ])
        child_f, child_t = _person(CHILD, 'kid', 'ann', relationships=rel)
        root = _build({child_f: child_t})
        findings, registry = lint._run_lint_core(root, {})
        # Unsourced beliefs are never findings.
        self.assertEqual(_codes(findings, 'W115'), [])
        backlog = lint._needs_sourcing_backlog(registry)
        joined = '\n'.join(backlog)
        self.assertIn('Walter Doe', joined)
        self.assertIn('Robert Roe', joined)
        self.assertIn('hypothesis', joined)


if __name__ == '__main__':
    unittest.main()
