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


def _child_of(cid: str, child: str, parents: list[str], subtype: str = 'biological') -> str:
    plist = ', '.join(parents)
    persons = ', '.join([child] + parents)
    return (
        f'- value: "{child} child of {plist}"\n'
        f'  id: {cid}\n  type: relationship\n  subtype: {subtype}\n'
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


def _half_siblings(cid: str, a: str, b: str) -> str:
    """A source that says outright that two people are half-siblings."""
    return (
        f'- value: "{a} and {b} are half-siblings"\n'
        f'  id: {cid}\n  type: relationship\n  subtype: half\n'
        f'  persons: [{a}, {b}]\n  roles: {{sibling: [{a}, {b}]}}\n'
        f'  status: accepted\n  reviewed: 2026-01-01\n  confidence: high\n'
        f'  information: primary\n  evidence: direct\n  notes: x.\n'
    )


def _write_archive(claims: str, sexes: dict[str, str], names: dict[str, str]) -> Path:
    """A temp archive holding these people and one source with these claims,
    indexed."""
    root = Path(tempfile.mkdtemp())
    (root / 'people' / 'stubs').mkdir(parents=True)
    (root / 'sources' / 'notes').mkdir(parents=True)
    (root / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
    for pid, sex in sexes.items():
        name = names[pid]
        (root / 'people' / 'stubs' / f'p__{pid[2:]}_{pid}.md').write_text(
            f'---\nid: {pid}\nname: {name}\nsex: {sex}\nliving: false\n'
            f'tier: stub\n---\n\n# {name}\n\n## Biography\n\nx\n', encoding='utf-8')
    src = (f'---\nid: {SID}\ntitle: Family\nsource_type: other\n---\n\n'
           f'## Claims\n```yaml\n{claims}```\n')
    (root / 'sources' / 'notes' / f'{SID.lower()}.md').write_text(src, encoding='utf-8')
    index_mod.build_index(root, load_fha_yaml(root))
    return root


# A second family, for half-siblings (`_build_family`). GF, GM, XP, YP, A,
# SIB and B are the people above; here A and SIB have both parents recorded.
XW = 'P-kkkkkkkkkk'      # (F) XP's wife, mother of A and SIB
XW2 = 'P-mmmmmmmmmm'     # (F) XP's other wife, mother of HALF
HALF = 'P-nnnnnnnnnn'    # (M) child of XP and XW2: A's half-brother
MAYBE = 'P-pppppppppp'   # (F) child of XP, no mother recorded
YH = 'P-qqqqqqqqqq'      # (M) YP's husband, father of B
HP = 'P-rrrrrrrrrr'      # (M) the one recorded parent of PA and PB
PA = 'P-ssssssssss'      # (M) child of HP; a source says PA and PB are half-siblings
PB = 'P-tttttttttt'      # (F) child of HP
Q1 = 'P-vvvvvvvvvv'      # (M) child of GF only; a source says Q1 and Q2 are half-siblings
Q2 = 'P-wwwwwwwwww'      # (F) child of GF only
QC1 = 'P-xxxxxxxxxx'     # (M) child of Q1
QC2 = 'P-yyyyyyyyyy'     # (F) child of Q2: QC1's half first cousin
R1 = 'P-zzzzzzzzzz'      # (M) child of GF only, nothing said about halves
R2 = 'P-2222222222'      # (F) child of GF only
RC1 = 'P-3333333333'     # (M) child of R1
RC2 = 'P-4444444444'     # (F) child of R2: RC1 may be her half first cousin

_FAMILY_SEX = {GF: 'M', GM: 'F', XP: 'M', YP: 'F', A: 'M', B: 'F', SIB: 'F',
               XW: 'F', XW2: 'F', HALF: 'M', MAYBE: 'F', YH: 'M', HP: 'M',
               PA: 'M', PB: 'F', Q1: 'M', Q2: 'F', QC1: 'M', QC2: 'F',
               R1: 'M', R2: 'F', RC1: 'M', RC2: 'F'}
_FAMILY_NAME = dict(_NAME, **{
    XW: 'Wren Aye', XW2: 'Willa Aye', HALF: 'Hal Aye', MAYBE: 'May Aye',
    YH: 'Hugh Bee', HP: 'Hap Pea', PA: 'Pat Pea', PB: 'Pam Pea',
    Q1: 'Quin Cue', Q2: 'Quilla Cue', QC1: 'Cal Cue', QC2: 'Cora Cue',
    R1: 'Rory Are', R2: 'Rita Are', RC1: 'Ross Are', RC2: 'Rose Are'})


def _build_family() -> Path:
    claims = (
        _child_of('C-a000000001', XP, [GF, GM])
        + _child_of('C-a000000002', YP, [GF, GM])
        + _child_of('C-a000000003', A, [XP, XW])
        + _child_of('C-a000000004', SIB, [XP, XW])
        + _child_of('C-a000000005', HALF, [XP, XW2])
        + _child_of('C-a000000006', MAYBE, [XP])
        + _child_of('C-a000000007', B, [YP, YH])
        + _child_of('C-a000000008', PA, [HP])
        + _child_of('C-a000000009', PB, [HP])
        + _half_siblings('C-a00000000a', PA, PB)
        + _child_of('C-a00000000b', Q1, [GF])
        + _child_of('C-a00000000c', Q2, [GF])
        + _half_siblings('C-a00000000d', Q1, Q2)
        + _child_of('C-a00000000e', QC1, [Q1])
        + _child_of('C-a00000000f', QC2, [Q2])
        + _child_of('C-a00000000g', R1, [GF])
        + _child_of('C-a00000000h', R2, [GF])
        + _child_of('C-a00000000j', RC1, [R1])
        + _child_of('C-a00000000k', RC2, [R2])
    )
    return _write_archive(claims, _FAMILY_SEX, _FAMILY_NAME)


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


class HalfSiblingTests(unittest.TestCase):
    """Two people who share one parent are not "brother and sister", and when
    the other parent is missing from the record the tool says it could be
    either instead of guessing."""

    @classmethod
    def setUpClass(cls):
        cls.root = _build_family()

    def _blood(self, a, b):
        return _blood(self.root, a, b)

    def _text(self, a, b):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            relate._cmd_relate(relate.run_relate(self.root, load_fha_yaml(self.root), a, b))
        return out.getvalue()

    def test_shared_father_two_recorded_mothers_is_half_brother(self):
        blood = self._blood(A, HALF)
        self.assertEqual(blood['relationship'], 'half-brother')
        self.assertIs(blood['half'], True)
        self.assertIsNone(blood['half_relationship'])
        self.assertIsNone(blood['note'])
        self.assertIn("Ann Aye is Hal Aye's half-brother", self._text(A, HALF))

    def test_full_siblings_with_both_parents_are_brother(self):
        blood = self._blood(A, SIB)
        self.assertEqual(blood['relationship'], 'brother')
        self.assertIs(blood['half'], False)
        self.assertIsNone(blood['note'])

    def test_one_parent_unrecorded_is_possibly_half(self):
        blood = self._blood(A, MAYBE)
        self.assertEqual(blood['relationship'], 'brother')
        self.assertIsNone(blood['half'])
        self.assertEqual(blood['half_relationship'], 'half-brother')
        self.assertEqual(blood['note'],
                         'Only one parent is recorded for at least one of them, '
                         "so Ann Aye may be May Aye's half-brother.")
        text = self._text(A, MAYBE)
        self.assertIn("Blood:  Ann Aye is May Aye's brother  (common ancestor:", text)
        self.assertIn("\n        Only one parent is recorded for at least one of them, "
                      "so Ann Aye may be May Aye's half-brother.\n", text)

    def test_half_sibling_claim_says_half_with_parents_unrecorded(self):
        blood = self._blood(PA, PB)
        self.assertEqual(blood['relationship'], 'half-brother')
        self.assertIs(blood['half'], True)

    def test_half_sibling_claim_reaches_their_children(self):
        # Q1 and Q2 are recorded half-siblings, so their children are half
        # first cousins, though neither one's other parent is recorded.
        blood = self._blood(QC1, QC2)
        self.assertEqual(blood['relationship'], 'half first cousin')
        self.assertIs(blood['half'], True)

    def test_cousins_through_one_grandparent_may_be_half(self):
        blood = self._blood(RC1, RC2)
        self.assertEqual(blood['relationship'], 'first cousin')
        self.assertIsNone(blood['half'])
        self.assertEqual(blood['half_relationship'], 'half first cousin')
        self.assertIn("\n        A parent on the line that connects them isn't recorded, "
                      "so Ross Are may be Rose Are's half first cousin.\n",
                      self._text(RC1, RC2))

    def test_old_fixture_siblings_now_possibly_half(self):
        blood = _blood(_build(), A, SIB)
        self.assertEqual(blood['relationship'], 'brother')
        self.assertIsNone(blood['half'])

    def test_cousins_through_a_couple_are_full(self):
        blood = self._blood(A, B)
        self.assertEqual(blood['relationship'], 'first cousin')
        self.assertIs(blood['half'], False)
        self.assertIsNone(blood['note'])

    def test_lineal_tie_is_never_half(self):
        blood = self._blood(MAYBE, GF)
        self.assertEqual(blood['relationship'], 'granddaughter')
        self.assertIs(blood['half'], False)

    def test_uncle_through_one_grandparent_may_be_half_uncle(self):
        blood = self._blood(R1, RC2)        # Rory is Rose's uncle, perhaps half
        self.assertEqual(blood['relationship'], 'uncle')
        self.assertIsNone(blood['half'])
        self.assertEqual(blood['half_relationship'], 'half-uncle')

    def test_path_line_still_says_brother(self):
        self.assertIn('Path:   your brother', self._text(MAYBE, A))


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
