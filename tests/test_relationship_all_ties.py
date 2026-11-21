"""
test_relationship_all_ties.py: a relationship claim records every tie its
roles name (SPEC §8.3).

"Edith, daughter of Clara, married George" is one claim: `parent: [Clara]`,
`child: [Edith]`, `spouse: [Edith, George]`. The tree must show both the
parent tie and the marriage, and a `relationships:` list on Edith's record
owes an entry for each. These tests pin that through the one shared rule
(`_lib.relationship_claim_parties`), the index edges built from it, the
entries the claim-accept writer owes (`claim_owed_relationship_entries`),
and lint W115 with `--fix-applied`. They also pin that a claim naming one
kind of tie reads exactly as it did, for every role shape the rule knows.

Expected values come from SPEC §8.2/§8.3, not from the implementation.
"""

import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import index
import lint
from _lib import (
    claim_owed_relationship_entries,
    read_record,
    relationship_claim_parties,
    relationship_claim_records_tie,
)

EDITH = 'p-aaaaaaaaaa'
CLARA = 'p-bbbbbbbbbb'
GEORGE = 'p-cccccccccc'
WARREN = 'p-dddddddddd'
CLAIM = 'c-eeeeeeeeee'
SOURCE = 's-ffffffffff'


def _kinds(ties):
    return [kind for kind, _parties in ties]


class SharedRuleTests(unittest.TestCase):
    """`relationship_claim_parties` lists each tie the roles support."""

    def test_parent_child_and_couple_give_both_ties(self):
        ties = relationship_claim_parties(
            [(EDITH, 'child'), (EDITH, 'spouse'), (CLARA, 'parent'), (GEORGE, 'spouse')], None)
        self.assertEqual(ties, [('parentage', ([EDITH], [CLARA])),
                                ('spouse', [EDITH, GEORGE])])

    def test_parentage_beside_a_power_tie_gives_both(self):
        ties = relationship_claim_parties(
            [(EDITH, 'child'), (CLARA, 'parent'), (CLARA, 'employer'), (GEORGE, 'employee')],
            None)
        self.assertEqual(_kinds(ties), ['parentage', 'power'])
        self.assertEqual(ties[1][1], [(GEORGE, 'employee', CLARA, 'employer')])

    def test_a_couple_beside_named_friends_gives_both(self):
        ties = relationship_claim_parties(
            [(EDITH, 'spouse'), (GEORGE, 'spouse'), (CLARA, 'friend'), (WARREN, 'friend')],
            'friend')
        self.assertEqual(ties, [('spouse', [EDITH, GEORGE]), ('social', [CLARA, WARREN])])

    def test_one_kind_of_tie_reads_as_before(self):
        # Each shape the rule knows, alone on its claim: one tie, as SPEC
        # §8.3 reads it.
        cases = [
            ([(EDITH, 'child'), (CLARA, 'parent'), (GEORGE, 'parent')], 'adoptive',
             [('parentage', ([EDITH], [CLARA, GEORGE]))]),
            ([(EDITH, 'daughter'), (CLARA, 'mother')], None,
             [('parentage', ([EDITH], [CLARA]))]),
            ([(EDITH, 'spouse'), (GEORGE, 'spouse')], None, [('spouse', [EDITH, GEORGE])]),
            # Two people named, one marked spouse: the couple (case 2).
            ([(EDITH, None), (GEORGE, 'wife')], None, [('spouse', [EDITH, GEORGE])]),
            # A couple beside a lone parent: the marriage only.
            ([(EDITH, 'spouse'), (GEORGE, 'spouse'), (CLARA, 'parent')], None,
             [('spouse', [EDITH, GEORGE])]),
            ([(EDITH, 'friend'), (GEORGE, 'friend'), (CLARA, 'witness')], 'friend',
             [('social', [EDITH, GEORGE])]),
            ([(EDITH, None), (GEORGE, None)], 'neighbor', [('social', [EDITH, GEORGE])]),
            ([(EDITH, 'enslaved'), (CLARA, 'enslaver')], None,
             [('power', [(EDITH, 'enslaved-by', CLARA, 'enslaver')])]),
            # A friend claim that calls someone a parent and names no friends
            # pairs nobody as friends; the parentage stands.
            ([(EDITH, 'child'), (CLARA, 'parent')], 'friend',
             [('parentage', ([EDITH], [CLARA]))]),
        ]
        for pairs, subtype, expected in cases:
            with self.subTest(pairs=pairs, subtype=subtype):
                self.assertEqual(relationship_claim_parties(pairs, subtype), expected)

    def test_one_sided_and_empty_claims_record_nothing(self):
        for pairs in ([(CLARA, 'parent')], [(EDITH, 'child')], [(EDITH, 'spouse')],
                      [(EDITH, 'sibling')], [(EDITH, 'spouse'), (CLARA, 'parent')]):
            with self.subTest(pairs=pairs):
                self.assertFalse(relationship_claim_records_tie(pairs, None))


def _person(pid, given, surname, relationships=''):
    return ('---\n'
            f'id: {pid}\n'
            f'name: {given} {surname}\n'
            'living: false\n'
            'tier: stub\n'
            f'{relationships}'
            '---\n\n'
            f'# {given} {surname}\n')


CLAIM_YAML = (
    '- value: "Edith, daughter of Clara, married George"\n'
    f'  id: {CLAIM}\n'
    '  type: relationship\n'
    f'  persons: [{EDITH}, {CLARA}, {GEORGE}]\n'
    '  roles:\n'
    f'    parent: [{CLARA}]\n'
    f'    child: [{EDITH}]\n'
    f'    spouse: [{EDITH}, {GEORGE}]\n'
    '  status: accepted\n'
    '  reviewed: 2026-11-20\n')


class ArchiveTests(unittest.TestCase):
    """The index, the writer and W115 all record both ties."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / 'fha.yaml').write_text('roots:\n  documents: documents\n',
                                            encoding='utf-8')
        stubs = self.root / 'people' / 'stubs'
        stubs.mkdir(parents=True)
        edith_rel = ('relationships:\n'
                     f'  - to: "[[{CLARA}|Clara Frost]]"\n'
                     '    type: parent\n'
                     f'    claims: ["[[{CLAIM}]]"]\n')
        self.edith = stubs / f'hartley__edith_{EDITH}.md'
        self.edith.write_text(_person(EDITH, 'Edith', 'Hartley', edith_rel), encoding='utf-8')
        (stubs / f'frost__clara_{CLARA}.md').write_text(
            _person(CLARA, 'Clara', 'Frost'), encoding='utf-8')
        (stubs / f'bradford__george_{GEORGE}.md').write_text(
            _person(GEORGE, 'George', 'Bradford'), encoding='utf-8')
        src = self.root / 'sources' / 'notes'
        src.mkdir(parents=True)
        (src / f'family-note_{SOURCE}.md').write_text(
            '---\n'
            f'id: {SOURCE}\n'
            'title: Family note\n'
            'source_type: other\n'
            '---\n\n## Claims\n\n```yaml\n' + CLAIM_YAML + '```\n', encoding='utf-8')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_index_records_the_parent_tie_and_the_marriage(self):
        index.build_index(self.root, {})
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        try:
            edges = set(conn.execute(
                'SELECT person_id, rel, other_id FROM relationships WHERE claim_id = ?',
                (CLAIM,)).fetchall())
        finally:
            conn.close()
        self.assertIn((EDITH, 'spouse', GEORGE), edges)
        self.assertIn((GEORGE, 'spouse', EDITH), edges)
        self.assertTrue(any(EDITH in e and CLARA in e and e[1] != 'spouse' for e in edges),
                        edges)
        self.assertFalse(any(CLARA in e and GEORGE in e for e in edges), edges)

    def test_the_claim_owes_edith_both_entries(self):
        claim = yaml.safe_load(CLAIM_YAML)[0]
        owed = {(p, t, o) for p, t, o, _n in claim_owed_relationship_entries(claim)}
        self.assertEqual(owed, {(EDITH, 'parent', CLARA), (EDITH, 'spouse', GEORGE),
                                (CLARA, 'child', EDITH), (GEORGE, 'spouse', EDITH)})

    def test_w115_asks_for_the_marriage_and_fix_applied_adds_it(self):
        res = lint.run_lint(self.root, {})
        w115 = [m for m in res.messages if m.code == 'W115']
        self.assertEqual(len(w115), 1, [m.text for m in res.messages])
        self.assertIn(CLAIM, w115[0].text.lower())
        lint.run_lint(self.root, {}, fix_applied=True)
        block = read_record(self.edith)['meta']['relationships']
        self.assertEqual(sorted(e['type'] for e in block), ['parent', 'spouse'])
        spouse = next(e for e in block if e['type'] == 'spouse')
        self.assertIn(GEORGE, spouse['to'])
        res = lint.run_lint(self.root, {})
        self.assertEqual([m.text for m in res.messages if m.code in ('W115', 'W159')], [])



def _claim_yaml(subtype: str = '') -> str:
    sub = f'  subtype: {subtype}\n' if subtype else ''
    return CLAIM_YAML.replace('  type: relationship\n', '  type: relationship\n' + sub)


class TieNatureTests(unittest.TestCase):
    """The claim's `subtype:` names the parent tie's nature (SPEC §8.2). The
    marriage the same claim records is never written or read as adoptive
    or biological."""

    def _archive(self, subtype='', george_rel='', edith_rel=''):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        (root / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
        stubs = root / 'people' / 'stubs'
        stubs.mkdir(parents=True)
        (stubs / f'hartley__edith_{EDITH}.md').write_text(
            _person(EDITH, 'Edith', 'Hartley', edith_rel), encoding='utf-8')
        george = stubs / f'bradford__george_{GEORGE}.md'
        george.write_text(_person(GEORGE, 'George', 'Bradford', george_rel), encoding='utf-8')
        (stubs / f'frost__clara_{CLARA}.md').write_text(
            _person(CLARA, 'Clara', 'Frost'), encoding='utf-8')
        src = root / 'sources' / 'notes'
        src.mkdir(parents=True)
        (src / f'family-note_{SOURCE}.md').write_text(
            f'---\nid: {SOURCE}\ntitle: Family note\nsource_type: other\n---\n\n'
            '## Claims\n\n```yaml\n' + _claim_yaml(subtype) + '```\n', encoding='utf-8')
        return root, george

    def test_owed_spouse_entries_carry_no_parentage_nature(self):
        for subtype in ('', 'adoptive'):
            with self.subTest(subtype=subtype):
                claim = yaml.safe_load(_claim_yaml(subtype))[0]
                owed = {(p, t, o): n for p, t, o, n in claim_owed_relationship_entries(claim)}
                self.assertEqual(owed[(GEORGE, 'spouse', EDITH)], '')
                self.assertEqual(owed[(EDITH, 'spouse', GEORGE)], '')
                self.assertEqual(owed[(EDITH, 'parent', CLARA)], subtype or 'biological')

    def test_fix_applied_writes_a_spouse_entry_with_no_subtype(self):
        for subtype in ('', 'adoptive'):
            with self.subTest(subtype=subtype):
                root, george = self._archive(subtype, george_rel='relationships: []\n')
                lint.run_lint(root, {}, fix_applied=True)
                (entry,) = read_record(george)['meta']['relationships']
                self.assertEqual(entry['type'], 'spouse')
                self.assertNotIn('subtype', entry)

    def test_an_existing_spouse_entry_takes_the_claim_without_a_new_nature(self):
        for existing in ('', '    subtype: civil\n'):
            with self.subTest(existing=existing):
                root, george = self._archive('adoptive', george_rel=(
                    'relationships:\n'
                    f'  - to: "[[{EDITH}|Edith Hartley]]"\n'
                    '    type: spouse\n' + existing))
                lint.run_lint(root, {}, fix_applied=True)
                block = read_record(george)['meta']['relationships']
                self.assertEqual(len(block), 1, block)
                self.assertEqual(block[0].get('subtype'), existing.split(':')[-1].strip() or None)
                self.assertIn(CLAIM, str(block[0].get('claims')).lower())

    def test_a_civil_spouse_entry_citing_the_claim_is_not_a_mismatch(self):
        root, _george = self._archive('adoptive', george_rel=(
            'relationships:\n'
            f'  - to: "[[{EDITH}|Edith Hartley]]"\n'
            '    type: spouse\n    subtype: civil\n'
            f'    claims: ["[[{CLAIM}]]"]\n'))
        res = lint.run_lint(root, {})
        self.assertEqual([m.text for m in res.messages
                          if m.code == 'W115' and 'subtype' in m.text], [])

    def test_w115_names_the_missing_marriage_not_the_parent_tie(self):
        root, _george = self._archive(edith_rel=(
            'relationships:\n'
            f'  - to: "[[{CLARA}|Clara Frost]]"\n'
            '    type: parent\n'
            f'    claims: ["[[{CLAIM}]]"]\n'))
        res = lint.run_lint(root, {})
        (w115,) = [m for m in res.messages if m.code == 'W115']
        self.assertIn('a spouse edge', w115.text)
        self.assertNotIn('parent edge', w115.text)

    def test_tree_views_draw_the_marriage_without_the_parent_nature(self):
        import views
        root, _george = self._archive('adoptive')
        index.build_index(root, {})
        conn = sqlite3.connect(str(root / '.cache' / 'index.sqlite'))
        conn.row_factory = sqlite3.Row
        try:
            edges = views._collect_edges(conn, EDITH, ['spouse', 'parent'])
        finally:
            conn.close()
        by_type = {e['type']: e for e in edges}
        self.assertIsNone(by_type['spouse']['subtype'])
        self.assertTrue(by_type['spouse']['genetic'])
        self.assertEqual(by_type['parent']['subtype'], 'adoptive')


if __name__ == '__main__':
    unittest.main()
