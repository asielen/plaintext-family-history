"""
test_relate.py - fha relate: blood relationship (LCA over genetic edges) and the
shortest social path (BFS over all edges), SPEC Part IV / TOOLING §7, chunk 05.

A pure read over the index: builds a tiny family, indexes it, and asks how pairs
relate - cousins, a grandparent, siblings, a friend with no blood tie - plus the
error paths (bad ID, missing index). Temp fixtures throughout; never the real
archive or example-archive.
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import relate
import index as index_mod
from _lib import load_fha_yaml, EXIT_CLEAN, EXIT_FAILURE

GF = 'P-aaaaaaaaaa'      # grandfather (M)
GM = 'P-bbbbbbbbbb'      # grandmother (F)
XP = 'P-cccccccccc'      # a child of GF/GM (M); parent of A and SIB
YP = 'P-dddddddddd'      # a child of GF/GM (F); parent of B
A = 'P-eeeeeeeeee'       # (M) cousin of B, sibling of SIB, grandchild of GF
B = 'P-ffffffffff'       # (F) cousin of A
SIB = 'P-gggggggggg'     # (F) sibling of A
FRIEND = 'P-hhhhhhhhhh'  # (M) a friend of A, no blood tie
SID = 'S-9999999999'

_SEX = {GF: 'M', GM: 'F', XP: 'M', YP: 'F', A: 'M', B: 'F', SIB: 'F', FRIEND: 'M'}
_NAME = {GF: 'Grand Pa', GM: 'Grand Ma', XP: 'Ex Parent', YP: 'Why Parent',
         A: 'Ann Aye', B: 'Bea Bee', SIB: 'Sis Aye', FRIEND: 'Fred Friend'}


def _ptext(pid: str) -> str:
    name = _NAME[pid]
    return (f'---\nid: {pid}\nname: {name}\nsex: {_SEX[pid]}\nliving: false\n'
            f'tier: stub\n---\n\n# {name}\n\n## Biography\n\nx\n')


def _child_of(cid: str, child: str, parents: list[str]) -> str:
    plist = ', '.join(parents)
    persons = ', '.join([child] + parents)
    return (
        f'- value: "{child} child of {plist}"\n'
        f'  id: {cid}\n  type: relationship\n  subtype: biological\n'
        f'  persons: [{persons}]\n  roles:\n    child: {child}\n    parent: [{plist}]\n'
        f'  status: accepted\n  reviewed: 2026-01-01\n  confidence: high\n'
        f'  information: primary\n  evidence: direct\n  notes: x.\n'
    )


def _friend(cid: str, p1: str, p2: str) -> str:
    return (
        f'- value: "{p1} and {p2} are friends"\n'
        f'  id: {cid}\n  type: relationship\n  subtype: friend\n'
        f'  persons: [{p1}, {p2}]\n  roles: {{associate: [{p1}, {p2}]}}\n'
        f'  status: accepted\n  reviewed: 2026-01-01\n  confidence: medium\n'
        f'  information: secondary\n  evidence: direct\n  notes: x.\n'
    )


def _build() -> Path:
    root = Path(tempfile.mkdtemp())
    (root / 'people' / 'stubs').mkdir(parents=True)
    (root / 'sources' / 'notes').mkdir(parents=True)
    (root / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
    for pid in _SEX:
        (root / 'people' / 'stubs' / f'p__{pid[2:]}_{pid}.md').write_text(_ptext(pid), encoding='utf-8')
    claims = (
        _child_of('C-1111111111', XP, [GF, GM])
        + _child_of('C-2222222222', YP, [GF, GM])
        + _child_of('C-3333333333', A, [XP])
        + _child_of('C-4444444444', SIB, [XP])
        + _child_of('C-5555555555', B, [YP])
        + _friend('C-6666666666', A, FRIEND)
    )
    src = (f'---\nid: {SID}\ntitle: Family\nsource_type: other\n---\n\n'
           f'## Claims\n```yaml\n{claims}```\n')
    (root / 'sources' / 'notes' / f'{SID.lower()}.md').write_text(src, encoding='utf-8')
    index_mod.build_index(root, load_fha_yaml(root))
    return root


def _blood(root, a, b):
    r = relate.run_relate(root, load_fha_yaml(root), a, b)
    return r.data.get('blood')


class BloodRelationshipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = _build()

    def test_first_cousins(self):
        blood = _blood(self.root, A, B)
        self.assertEqual(blood['relationship'], 'first cousin')
        self.assertEqual({c.lower() for c in blood['common_ancestors']}, {GF.lower(), GM.lower()})

    def test_grandparent_is_lineal(self):
        # A relate GF: A is GF's grandchild (A is male -> grandson).
        blood = _blood(self.root, A, GF)
        self.assertEqual(blood['relationship'], 'grandson')

    def test_grandparent_other_direction(self):
        # GF relate A: GF is A's grandfather.
        blood = _blood(self.root, GF, A)
        self.assertEqual(blood['relationship'], 'grandfather')

    def test_siblings(self):
        blood = _blood(self.root, A, SIB)
        self.assertEqual(blood['relationship'], 'brother')   # A is male
        self.assertEqual({c.lower() for c in blood['common_ancestors']}, {XP.lower()})

    def test_friend_has_no_blood(self):
        self.assertIsNone(_blood(self.root, A, FRIEND))


class SocialPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = _build()

    def test_friend_path_rendered(self):
        r = relate.run_relate(self.root, load_fha_yaml(self.root), A, FRIEND)
        self.assertEqual(r.data['any']['rendered'], 'your friend')

    def test_cousin_social_path_exists(self):
        # A -> XP (parent) -> GF? no; the BFS finds *some* shortest chain to B.
        r = relate.run_relate(self.root, load_fha_yaml(self.root), A, B)
        self.assertTrue(r.data['any']['path'])
        self.assertTrue(r.data['any']['rendered'].startswith('your '))

    def test_sibling_path_is_one_hop(self):
        r = relate.run_relate(self.root, load_fha_yaml(self.root), A, SIB)
        self.assertEqual(r.data['any']['rendered'], 'your sister')   # SIB is female


class ErrorPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = _build()

    def test_bad_id_fails_cleanly(self):
        r = relate.run_relate(self.root, load_fha_yaml(self.root), 'not-an-id', B)
        self.assertFalse(r.ok)
        self.assertEqual(r.exit_code, EXIT_FAILURE)

    def test_unknown_person_fails_cleanly(self):
        r = relate.run_relate(self.root, load_fha_yaml(self.root), 'P-0000000000', B)
        self.assertFalse(r.ok)
        self.assertEqual(r.exit_code, EXIT_FAILURE)

    def test_missing_index_is_hard_error(self):
        bare = Path(tempfile.mkdtemp())
        (bare / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
        r = relate.run_relate(bare, load_fha_yaml(bare), A, B)
        self.assertFalse(r.ok)
        self.assertEqual(r.exit_code, EXIT_FAILURE)

    def test_clean_result_is_json_serializable(self):
        import json
        r = relate.run_relate(self.root, load_fha_yaml(self.root), A, B)
        self.assertEqual(r.exit_code, EXIT_CLEAN)
        json.loads(json.dumps(r.as_dict()))   # must not raise



class NameInsteadOfIdTests(unittest.TestCase):
    """Either person may be given by full name: one person carrying it is
    the person meant; a shared name or nobody is refused, never guessed."""

    @classmethod
    def setUpClass(cls):
        cls.root = _build()

    def test_an_unambiguous_name_resolves(self):
        r = relate.run_relate(self.root, load_fha_yaml(self.root), 'ann aye', 'Bea Bee')
        self.assertEqual(r.exit_code, EXIT_CLEAN)
        self.assertEqual(r.data['blood']['relationship'], 'first cousin')

    def test_a_name_nobody_carries_points_to_find(self):
        r = relate.run_relate(self.root, load_fha_yaml(self.root), 'Nobody Here', B)
        self.assertEqual(r.exit_code, EXIT_FAILURE)
        self.assertIn('fha find "Nobody Here"', r.messages[0].text)

    def test_a_shared_name_is_refused_with_both_listed(self):
        root = _build()
        twin = 'P-jjjjjjjjjj'
        (root / 'people' / 'stubs' / f'p__{twin[2:]}_{twin}.md').write_text(
            f'---\nid: {twin}\nname: Bea Bee\nsex: F\nliving: false\ntier: stub\n---\n',
            encoding='utf-8')
        index_mod.build_index(root, load_fha_yaml(root))
        r = relate.run_relate(root, load_fha_yaml(root), A, 'Bea Bee')
        self.assertEqual(r.exit_code, EXIT_FAILURE)
        self.assertEqual(r.data['error'], 'ambiguous-name')
        self.assertEqual(sorted(c.lower() for c in r.data['candidates']),
                         sorted([B.lower(), twin.lower()]))


if __name__ == '__main__':
    unittest.main()
