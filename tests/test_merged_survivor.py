"""
test_merged_survivor.py - a claim naming a merged person counts for the survivor.

SPEC §9: when two person records prove to be one human, the duplicate becomes a
tombstone (`status: merged`, `merged_into: p-survivor`) and "a claim whose
`persons:` or `roles:` still names the merged record counts for the survivor in
every derivation, view and export". Before this, `fha index` stored the
tombstone's own id in `claim_persons`, so a birth certificate filed against the
duplicate before the merge vanished from the survivor's timeline and left his
mother out of the tree - and lint called it an error (E016) the owner had to
fix by hand before anything worked.

The fixture: Thomas (the survivor), his duplicate stub merged into him, and his
mother Mary. One accepted birth claim names the duplicate as the child.

Run: py -3.14 -m pytest tests/test_merged_survivor.py -q   (from the repo root)
"""

import contextlib
import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import index
import lint
import views
from _lib import merge_survivor_map

THOMAS = 'p-aaaaaaaaa1'
DUP = 'p-aaaaaaaaa2'
MARY = 'p-aaaaaaaaa3'
SRC = 's-bbbbbbbbb1'
CLAIM = 'c-ccccccccc1'


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


class MergedPersonCountsForSurvivorTests(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write(self.root / 'fha.yaml', 'roots:\n  documents: documents\n')
        _write(self.root / 'people' / f'hartley__thomas_{THOMAS}.md',
               f'---\nid: {THOMAS}\nname: Thomas Hartley\nliving: false\nsex: M\n'
               f'tier: curated\n---\n\n# Thomas Hartley\n\n## Biography\n\nHis life.\n')
        _write(self.root / 'people' / 'stubs'
               / f'MERGED-INTO-{THOMAS}__hartley__thos_{DUP}.md',
               f'---\nid: {DUP}\nname: Thos Hartley\nliving: false\ntier: stub\n'
               f'status: merged\nmerged_into: {THOMAS}\nmerge_reason: same man\n'
               f'merged_date: 2026-11-01\n---\n\n# Thos Hartley\n')
        _write(self.root / 'people' / 'stubs' / f'cole__mary_{MARY}.md',
               f'---\nid: {MARY}\nname: Mary Cole\nliving: false\nsex: F\n'
               f'tier: stub\n---\n\n# Mary Cole\n')
        _write(self.root / 'sources' / 'vital-records' / f'birth_{SRC}.md',
               f'---\nid: {SRC}\ntitle: Birth register\nsource_type: vital-record\n'
               f'people:\n  - "[[{DUP}|Thos Hartley]]"\n---\n\n'
               '## Claims\n```yaml\n'
               f'- id: {CLAIM}\n  type: birth\n  value: born\n'
               f'  persons:\n    - "[[{DUP}|Thos Hartley]]"\n    - "[[{MARY}|Mary Cole]]"\n'
               f'  roles: {{child: {DUP}, parent: {MARY}}}\n'
               '  date: 1850-03-02\n  status: accepted\n  confidence: high\n'
               '  reviewed: 2026-06-10\n```\n')
        with contextlib.redirect_stdout(io.StringIO()):
            index.build_index(self.root, {'roots': {'documents': 'documents'}})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _rows(self, sql: str, args: tuple = ()) -> list:
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def test_claim_persons_and_source_people_name_the_survivor(self) -> None:
        self.assertEqual(
            {r[0] for r in self._rows(
                'SELECT person_id FROM claim_persons WHERE claim_id=?', (CLAIM,))},
            {THOMAS, MARY})
        self.assertEqual(
            [r[0] for r in self._rows(
                'SELECT person_id FROM source_people WHERE source_id=?', (SRC,))],
            [THOMAS])

    def test_tree_edge_lands_on_the_survivor(self) -> None:
        edges = set(self._rows(
            "SELECT person_id, rel, other_id FROM relationships WHERE claim_id=?", (CLAIM,)))
        self.assertIn((THOMAS, 'parent', MARY), edges)
        self.assertIn((MARY, 'child', THOMAS), edges)
        self.assertFalse([e for e in edges if DUP in e],
                         'no edge may hang on the tombstone')

    def test_survivor_timeline_shows_the_birth(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            result = views.run_timeline(self.root, person_id=THOMAS)
        self.assertEqual(len(result.changed), 1, result.changed)
        text = Path(result.changed[0]).read_text(encoding='utf-8')
        self.assertIn('1850', text)
        self.assertIn(SRC, text.lower())

    def test_upsert_matches_the_full_build(self) -> None:
        before = sorted(self._rows('SELECT claim_id, person_id, role FROM claim_persons'))
        with contextlib.redirect_stdout(io.StringIO()):
            index.upsert_source(self.root, {'roots': {'documents': 'documents'}}, SRC)
        after = sorted(self._rows('SELECT claim_id, person_id, role FROM claim_persons'))
        self.assertEqual(before, after)

    def test_lint_warns_not_errors_and_names_the_survivor(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            result = lint.run_lint(self.root, {'roots': {'documents': 'documents'}})
        findings = [(m.code, m.text) for m in result.messages]
        self.assertEqual([f for f in findings if f[0] == 'E016'], [],
                         'a claim on a merged person is no longer an error')
        w107 = [t for c, t in findings if c == 'W107' and CLAIM in t.lower()]
        self.assertEqual(len(w107), 1, findings)
        self.assertIn(THOMAS, w107[0].lower())
        # The birth counts for Thomas, so lint must not ask for one either.
        self.assertFalse([t for c, t in findings if c == 'W101' and 'birth' in t], findings)


    def test_lint_tree_twin_puts_the_survivor_under_his_mother(self) -> None:
        # Two-sided rule: index's relationships (above) and lint's in-memory
        # twin must agree, or W103/W110 ask for a change `fha views brackets`
        # (which reads the index) will never make.
        from _lib import load_fha_yaml
        _findings, registry = lint._run_lint_core(self.root, load_fha_yaml(self.root))
        edges = lint._build_child_edges(registry)
        self.assertIn(THOMAS, edges.get(MARY, {}))
        self.assertNotIn(DUP, edges.get(MARY, {}))


class MergeSurvivorMapTests(unittest.TestCase):

    def test_chain_is_followed_to_the_end(self) -> None:
        known = {'p-a', 'p-b', 'p-c'}
        self.assertEqual(
            merge_survivor_map({'p-a': 'p-b', 'p-b': 'p-c'}, known),
            {'p-a': 'p-c', 'p-b': 'p-c'})

    def test_cycle_and_dangling_pointer_leave_the_record_alone(self) -> None:
        known = {'p-a', 'p-b', 'p-d'}
        self.assertEqual(
            merge_survivor_map({'p-a': 'p-b', 'p-b': 'p-a', 'p-d': 'p-gone'}, known), {})


class _MergeFixture(unittest.TestCase):
    """Thomas, his merged duplicate, Mary; each test adds its own records."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write(self.root / 'fha.yaml', 'roots:\n  documents: documents\n')
        self.person(THOMAS, 'Thomas Hartley', tier='curated', sex='M')
        self.person(DUP, 'Thos Hartley', extra=f'status: merged\nmerged_into: {THOMAS}\n',
                    prefix=f'MERGED-INTO-{THOMAS}__')
        self.person(MARY, 'Mary Cole', sex='F')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def person(self, pid, name, *, tier='stub', sex='M', extra='', prefix='', body=''):
        folder = self.root / 'people' / ('stubs' if tier == 'stub' else '')
        slug = name.lower().replace(' ', '_')
        _write(folder / f'{prefix}{slug}_{pid}.md',
               f'---\nid: {pid}\nname: {name}\nliving: false\nsex: {sex}\ntier: {tier}\n'
               f'{extra}---\n\n# {name}\n\n{body}## Biography\n\nx\n')

    def source(self, sid, claims):
        _write(self.root / 'sources' / 'other' / f'rec_{sid}.md',
               f'---\nid: {sid}\ntitle: Record\nsource_type: other\n---\n\n'
               f'## Claims\n```yaml\n{claims}```\n')

    def build(self):
        with contextlib.redirect_stdout(io.StringIO()):
            index.build_index(self.root, {'roots': {'documents': 'documents'}})

    def rows(self, sql, args=()):
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def lint_findings(self):
        from _lib import load_fha_yaml
        findings, registry = lint._run_lint_core(self.root, load_fha_yaml(self.root))
        return findings, registry


def _claim(cid, ctype, persons, roles=None, date='1850'):
    text = (f'- id: {cid}\n  type: {ctype}\n  value: "{ctype} record"\n'
            f'  persons: [{", ".join(persons)}]\n')
    if roles:
        text += '  roles: {' + ', '.join(f'{k}: {v}' for k, v in roles.items()) + '}\n'
    return text + (f'  date: {date}\n  status: accepted\n  confidence: high\n'
                   '  reviewed: 2026-06-10\n')


class LintPairsRolesThroughSurvivorsTests(_MergeFixture):
    """C3: `persons:` names the survivor, `roles:` still names the tombstone.
    The index reads both through the survivor and derives Mary -> Thomas; lint
    used to drop the child role before resolving it and report E013 on
    Thomas's correct summary line."""

    def setUp(self) -> None:
        super().setUp()
        self.person(THOMAS, 'Thomas Hartley', tier='curated',
                    body=f'**Parents:** [[{MARY}|Mary Cole]] [[{SRC}]]\n\n')
        self.source(SRC, _claim(CLAIM, 'birth', [THOMAS, MARY],
                                {'child': DUP, 'parent': MARY}))
        self.build()

    def test_index_and_lint_agree_on_the_parent_edge(self) -> None:
        self.assertIn((THOMAS, 'parent', MARY),
                      set(self.rows('SELECT person_id, rel, other_id FROM relationships')))
        findings, registry = self.lint_findings()
        self.assertIn(THOMAS, lint._build_child_edges(registry).get(MARY, {}))
        claim = registry.source_claims[SRC][0]
        self.assertEqual(lint._claim_parentage_pids(claim, registry.alias_map),
                         ({THOMAS}, {MARY}))
        self.assertEqual([f.message for f in findings if f.code == 'E013'], [])


class BrokenMergePointerTests(_MergeFixture):
    """A1/A2: a merge the tools cannot follow leaves the claims where they are
    - and lint must say so, not claim they count for someone."""

    def test_dangling_pointer_is_an_error_and_w107_does_not_promise(self) -> None:
        ghost = 'p-zzzzzzzzz9'
        stuck = 'p-aaaaaaaaa4'
        self.person(stuck, 'Xavier Dangle', extra=f'status: merged\nmerged_into: {ghost}\n')
        self.source(SRC, _claim(CLAIM, 'residence', [stuck]))
        self.build()
        self.assertEqual(self.rows('SELECT person_id FROM claim_persons'), [(stuck,)])
        findings, _reg = self.lint_findings()
        e020 = [f.message for f in findings if f.code == 'E020']
        self.assertEqual(len(e020), 1, e020)
        self.assertIn(ghost, e020[0])
        w107 = [f.message for f in findings if f.code == 'W107' and CLAIM in f.message.lower()]
        self.assertEqual(len(w107), 1)
        self.assertNotIn('already counts', w107[0])

    def test_loop_is_an_error(self) -> None:
        a, b = 'p-aaaaaaaaa4', 'p-aaaaaaaaa5'
        self.person(a, 'Ann Loop', extra=f'status: merged\nmerged_into: {b}\n')
        self.person(b, 'Bea Loop', extra=f'status: merged\nmerged_into: {a}\n')
        self.build()
        findings, _reg = self.lint_findings()
        self.assertEqual(len([f for f in findings if f.code == 'E020']), 2)

    def test_merged_into_without_status_moves_nothing_and_warns(self) -> None:
        half = 'p-aaaaaaaaa5'
        self.person(half, 'Yolanda Half', extra=f'merged_into: {MARY}\n')
        self.source(SRC, _claim(CLAIM, 'occupation', [half]))
        self.build()
        self.assertEqual(self.rows('SELECT person_id FROM claim_persons'), [(half,)])
        findings, registry = self.lint_findings()
        self.assertEqual(len([f for f in findings if f.code == 'W140']), 1)
        self.assertNotIn(half, lint._registry_merge_survivors(registry))
        self.assertFalse([f for f in findings if f.code == 'W107'])


class OnePersonOneRowTests(_MergeFixture):
    """A4: a claim naming the tombstone AND the survivor is one person."""

    def test_claim_persons_has_one_row_for_the_survivor(self) -> None:
        self.source(SRC, _claim(CLAIM, 'residence', [DUP, THOMAS]))
        self.build()
        self.assertEqual(self.rows('SELECT person_id, position FROM claim_persons'),
                         [(THOMAS, 0)])


class FindExplainsTheMergeTests(_MergeFixture):
    """A3: `fha find <tombstone>` says where its claims went."""

    def test_find_names_the_survivor(self) -> None:
        import find
        self.source(SRC, _claim(CLAIM, 'birth', [DUP, MARY], {'child': DUP, 'parent': MARY}))
        self.build()
        from _lib import load_fha_yaml
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            find.run_find(DUP, self.root, load_fha_yaml(self.root))
        text = out.getvalue()
        self.assertIn(f'merged into:   {THOMAS}', text)
        self.assertIn(f'fha find {THOMAS}', text)
        self.assertNotIn('claims: none\n', text)



class RelationshipsBlockThroughSurvivorTests(_MergeFixture):
    """Round 4: a `relationships:` entry naming a merged record reads through
    the survivor, like the claims - no false W115 for Mary, whose entry still
    names her son's duplicate (retained) or already names him (updated)."""

    def _mary_block(self, to_pid: str) -> None:
        _write(self.root / 'people' / 'stubs' / f'mary_cole_{MARY}.md',
               f'---\nid: {MARY}\nname: Mary Cole\nliving: false\nsex: F\ntier: stub\n'
               f'relationships:\n  - to: "[[{to_pid}|Thomas]]"\n    type: child\n'
               f'    claims: ["[[{CLAIM}]]"]\n---\n\n# Mary Cole\n\n## Biography\n\nx\n')
        self.source(SRC, _claim(CLAIM, 'birth', [DUP, MARY], {'child': DUP, 'parent': MARY}))
        self.build()

    def test_retained_reference_to_the_tombstone(self) -> None:
        self._mary_block(DUP)
        findings, _reg = self.lint_findings()
        self.assertEqual([f.message for f in findings if f.code == 'W115'], [])

    def test_updated_reference_to_the_survivor(self) -> None:
        self._mary_block(THOMAS)
        findings, _reg = self.lint_findings()
        self.assertEqual([f.message for f in findings if f.code == 'W115'], [])


class UnreadableSurvivorTests(_MergeFixture):
    """Round 4: a survivor whose file is not UTF-8 is still a person on disk
    (the index keeps his row from the filename, #267). Lint must not call the
    merge broken - E020 told him to "restore the record", which invites a
    second Thomas - nor say the claims are missing from everyone's timeline."""

    def test_no_false_e020_and_w107_says_it_counts(self) -> None:
        self.source(SRC, _claim(CLAIM, 'birth', [DUP, MARY], {'child': DUP, 'parent': MARY}))
        surv = next((self.root / 'people').glob(f'*_{THOMAS}.md'))
        surv.write_bytes(surv.read_text(encoding='utf-8')
                         .replace('Thomas Hartley', 'Thomas H\u00e4rtley').encode('cp1252'))
        self.build()
        self.assertIn((THOMAS,), self.rows('SELECT person_id FROM claim_persons'))
        findings, _reg = self.lint_findings()
        self.assertEqual([f.message for f in findings if f.code == 'E020'], [])
        w107 = [f.message for f in findings if f.code == 'W107' and CLAIM in f.message.lower()]
        self.assertEqual(len(w107), 1)
        self.assertIn('already counts', w107[0])


if __name__ == '__main__':
    unittest.main()
