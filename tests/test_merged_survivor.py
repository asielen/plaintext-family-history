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


if __name__ == '__main__':
    unittest.main()
