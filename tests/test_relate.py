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
from _lib import load_fha_yaml, fmt_id_display, EXIT_CLEAN, EXIT_ERRORS, EXIT_FAILURE

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


def _married(cid: str, p1: str, p2: str, date: str | None = None) -> str:
    """Two people married, with the year when the record gives one."""
    when = f'  date: "{date}"\n' if date else ''
    return (
        f'- value: "{p1} married {p2}"\n'
        f'  id: {cid}\n  type: marriage\n'
        f'  persons: [{p1}, {p2}]\n  roles: {{spouse: [{p1}, {p2}]}}\n{when}'
        f'  status: accepted\n  reviewed: 2026-01-01\n  confidence: high\n'
        f'  information: primary\n  evidence: direct\n  notes: x.\n'
    )


def _born(cid: str, child: str, date: str) -> str:
    return (
        f'- value: "{child} born {date}"\n'
        f'  id: {cid}\n  type: birth\n'
        f'  persons: [{child}]\n  roles: {{child: [{child}]}}\n  date: "{date}"\n'
        f'  status: accepted\n  reviewed: 2026-01-01\n  confidence: high\n'
        f'  information: primary\n  evidence: direct\n  notes: x.\n'
    )


def _died(cid: str, pid: str, date: str) -> str:
    return (
        f'- value: "{pid} died {date}"\n'
        f'  id: {cid}\n  type: death\n'
        f'  persons: [{pid}]\n  roles: {{deceased: [{pid}]}}\n  date: "{date}"\n'
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
# Ties by marriage, in the same family (`_build_family`).
AW = 'P-5555555555'      # (F) A's wife
AWF = 'P-6666666666'     # (M) AW's father, and AWB's
AWB = 'P-7777777777'     # (M) AW's brother: A's brother-in-law
SH = 'P-8888888888'      # (M) SIB's husband: A's brother-in-law
XW0 = 'P-9999999999'     # (F) XP's wife 1880, died 1885, before A was born (1900)
XW3 = 'P-1111111111'     # (F) XP's wife, no dates recorded
AD = 'P-a1a1a1a1a1'      # (F) XP's adopted daughter
STEPKID = 'P-b1b1b1b1b1'  # (M) child of XW2 and EH, born 1905: A's stepbrother
EH = 'P-c1c1c1c1c1'      # (M) XW2's earlier husband

_FAMILY_SEX = {GF: 'M', GM: 'F', XP: 'M', YP: 'F', A: 'M', B: 'F', SIB: 'F',
               XW: 'F', XW2: 'F', HALF: 'M', MAYBE: 'F', YH: 'M', HP: 'M',
               PA: 'M', PB: 'F', Q1: 'M', Q2: 'F', QC1: 'M', QC2: 'F',
               R1: 'M', R2: 'F', RC1: 'M', RC2: 'F',
               AW: 'F', AWF: 'M', AWB: 'M', SH: 'M', XW0: 'F', XW3: 'F',
               AD: 'F', STEPKID: 'M', EH: 'M'}
_FAMILY_NAME = dict(_NAME, **{
    XW: 'Wren Aye', XW2: 'Willa Aye', HALF: 'Hal Aye', MAYBE: 'May Aye',
    YH: 'Hugh Bee', HP: 'Hap Pea', PA: 'Pat Pea', PB: 'Pam Pea',
    Q1: 'Quin Cue', Q2: 'Quilla Cue', QC1: 'Cal Cue', QC2: 'Cora Cue',
    R1: 'Rory Are', R2: 'Rita Are', RC1: 'Ross Are', RC2: 'Rose Are',
    AW: 'Wanda Aye', AWF: 'Walt Wells', AWB: 'Wes Wells', SH: 'Sam Hill',
    XW0: 'Olive Aye', XW3: 'Thea Aye', AD: 'Ruth Aye', STEPKID: 'Stan Eby',
    EH: 'Earl Eby'})


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
        # Ties by marriage.
        + _married('C-b000000001', A, AW)
        + _child_of('C-b000000002', AW, [AWF])
        + _child_of('C-b000000003', AWB, [AWF])
        + _married('C-b000000004', SH, SIB)
        + _married('C-b000000005', YH, YP)
        + _born('C-b000000006', A, '1900')
        + _died('C-b000000007', XW, '1910')
        + _married('C-b000000008', XP, XW2, '1912')
        + _married('C-b000000009', XP, XW0, '1880')
        + _died('C-b00000000a', XW0, '1885')
        + _married('C-b00000000b', XP, XW3)
        + _child_of('C-b00000000c', AD, [XP], subtype='adoptive')
        + _child_of('C-b00000000d', STEPKID, [XW2, EH])
        + _born('C-b00000000e', STEPKID, '1905')
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
        self.assertIn("Blood:        Ann Aye is May Aye's brother  (common ancestor:", text)
        self.assertIn("\n              Only one parent is recorded for at least one of them, "
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
        self.assertIn("\n              A parent on the line that connects them isn't recorded, "
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
        self.assertIn('Path:         your brother', self._text(MAYBE, A))



class ByMarriageTests(unittest.TestCase):
    """His wife's brother is "brother-in-law", his father's second wife is
    "stepmother" (or, when the dates say she was married to his father only
    before he was born, "father's earlier wife"), and his aunt's husband is
    "uncle by marriage". A blood tie always wins over a tie by marriage."""

    @classmethod
    def setUpClass(cls):
        cls.root = _build_family()

    def _run(self, a, b):
        return relate.run_relate(self.root, load_fha_yaml(self.root), a, b)

    def _bm(self, a, b):
        return self._run(a, b).data['by_marriage']

    def _text(self, a, b):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            relate._cmd_relate(self._run(a, b))
        return out.getvalue()

    def test_wifes_brother_is_brother_in_law(self):
        bm = self._bm(AWB, A)
        self.assertEqual(bm['relationship'], 'brother-in-law')
        self.assertEqual(bm['kind'], 'in-law')
        self.assertEqual(bm['explain'], "Ann Aye's wife's brother")
        self.assertEqual(bm['via'], [AW.lower()])

    def test_sisters_husband_is_brother_in_law(self):
        bm = self._bm(SH, A)
        self.assertEqual(bm['relationship'], 'brother-in-law')
        self.assertEqual(bm['explain'], "Ann Aye's sister's husband")

    def test_wifes_father_is_father_in_law(self):
        self.assertEqual(self._bm(AWF, A)['relationship'], 'father-in-law')

    def test_husband_is_son_in_law(self):
        self.assertEqual(self._bm(A, AWF)['relationship'], 'son-in-law')

    def test_aunts_husband_is_uncle_by_marriage(self):
        bm = self._bm(YH, A)
        self.assertEqual(bm['relationship'], 'uncle by marriage')
        self.assertEqual(bm['kind'], 'by-marriage')
        self.assertEqual(bm['explain'], "Ann Aye's aunt's husband")

    def test_second_wife_married_after_birth_is_stepmother(self):
        bm = self._bm(XW2, A)
        self.assertEqual(bm['relationship'], 'stepmother')
        self.assertEqual(bm['kind'], 'step')
        self.assertIs(bm['certain'], True)
        self.assertIsNone(bm['hedge'])
        self.assertEqual(bm['explain'], "Ann Aye's father's wife")

    def test_stepmother_the_other_way_is_stepson(self):
        bm = self._bm(A, XW2)
        self.assertEqual(bm['relationship'], 'stepson')
        self.assertIs(bm['certain'], True)

    def test_wife_who_died_before_birth_is_earlier_wife(self):
        bm = self._bm(XW0, A)
        self.assertEqual(bm['relationship'], "father's earlier wife")
        self.assertNotIn('step', bm['relationship'])
        self.assertIs(bm['certain'], True)
        self.assertEqual(bm['explain'], 'married to Ex Parent before Ann Aye was born')

    def test_undated_wife_is_hedged_stepmother(self):
        bm = self._bm(XW3, A)
        self.assertEqual(bm['relationship'], 'stepmother')
        self.assertIs(bm['certain'], False)
        self.assertEqual(
            bm['hedge'],
            "The marriage and birth dates that would settle this aren't all recorded, "
            "so Thea Aye may instead have been Ex Parent's wife only before Ann Aye was born.")
        text = self._text(XW3, A)
        self.assertIn("\nBy marriage:  Thea Aye is Ann Aye's stepmother  "
                      "(Ann Aye's father's wife)\n", text)
        self.assertIn('\n              The marriage and birth dates', text)

    def test_adoptive_edge_names_the_kind(self):
        self.assertEqual(self._bm(XP, AD)['relationship'], 'adoptive father')
        self.assertEqual(self._bm(XP, AD)['kind'], 'adoptive')
        self.assertEqual(self._bm(AD, XP)['relationship'], 'adopted daughter')
        self.assertEqual(self._bm(A, AD)['relationship'], 'adoptive brother')
        self.assertEqual(self._bm(AD, A)['relationship'], 'adoptive sister')

    def test_adoption_makes_no_blood_tie(self):
        self.assertIsNone(self._run(XP, AD).data['blood'])
        self.assertIsNone(self._run(A, AD).data['blood'])

    def test_step_sibling(self):
        bm = self._bm(STEPKID, A)
        self.assertEqual(bm['relationship'], 'stepbrother')
        self.assertEqual(bm['kind'], 'step')
        self.assertIs(bm['certain'], True)
        self.assertEqual(bm['explain'], "Ann Aye's father's wife's son")

    def test_spouse(self):
        bm = self._bm(AW, A)
        self.assertEqual((bm['relationship'], bm['kind']), ('wife', 'spouse'))
        self.assertIsNone(bm['explain'])

    def test_blood_tie_suppresses_by_marriage(self):
        r = self._run(A, B)
        self.assertEqual(r.data['blood']['relationship'], 'first cousin')
        self.assertIsNone(r.data['by_marriage'])
        self.assertNotIn('By marriage:', self._text(A, B))

    def test_father_is_never_his_mothers_husband_by_marriage(self):
        # A parent is married to the other parent, but blood wins.
        r = self._run(YH, B)
        self.assertEqual(r.data['blood']['relationship'], 'father')
        self.assertIsNone(r.data['by_marriage'])

    def test_path_hop_names_adoptive_kind(self):
        self.assertEqual(self._run(AD, XP).data['any']['rendered'], 'your adoptive father')
        self.assertEqual(self._run(XP, AD).data['any']['rendered'], 'your adopted daughter')

    def test_text_output_has_by_marriage_line(self):
        text = self._text(AWB, A)
        self.assertIn('Blood:        no blood relationship found in the recorded tree\n', text)
        self.assertIn("By marriage:  Wes Wells is Ann Aye's brother-in-law  "
                      "(Ann Aye's wife's brother)\n", text)
        self.assertIn('Path:         your', text)

    def test_names_cover_the_people_in_the_chain(self):
        r = self._run(AWB, A)
        self.assertEqual(r.data['names'][AW.lower()], 'Wanda Aye')

    def test_json_round_trip(self):
        import json
        r = self._run(XW3, A)
        back = json.loads(json.dumps(r.as_dict()))
        self.assertEqual(back['data']['by_marriage']['relationship'], 'stepmother')


class StepDatesTests(unittest.TestCase):
    """A father's later wife is called the child's stepmother as fact only when
    the dates show the marriage and the child's life overlapped. A birth
    written as "before 1910" or "190X", a wife who died months before the
    birth in the same year, and a baby who died before his father remarried
    must never come out as a certain "stepmother"."""

    F, W, K = 'P-ffffffffff', 'P-wwwwwwwwww', 'P-kkkkkkkkkk'
    SEXES = {F: 'M', W: 'F', K: 'M'}
    NAMES = {F: 'Frank Ames', W: 'Wilma Ames', K: 'Baby Ames'}

    def _bm(self, *claims, a=W, b=K):
        body = _child_of('C-0000000001', self.K, [self.F]) + ''.join(claims)
        root = _write_archive(body, self.SEXES, self.NAMES)
        return relate.run_relate(root, load_fha_yaml(root), a, b).data['by_marriage']

    def test_married_after_birth_child_alive_is_certain_stepmother(self):
        bm = self._bm(_married('C-0000000002', self.F, self.W, '1903'),
                      _born('C-0000000003', self.K, '1899'))
        self.assertEqual((bm['relationship'], bm['certain']), ('stepmother', True))

    def test_birth_before_a_year_is_hedged(self):
        bm = self._bm(_married('C-0000000002', self.F, self.W, '1905'),
                      _born('C-0000000003', self.K, '[..1910]'))
        self.assertEqual((bm['relationship'], bm['certain']), ('stepmother', False))

    def test_decade_birth_is_hedged(self):
        bm = self._bm(_married('C-0000000002', self.F, self.W, '1905'),
                      _born('C-0000000003', self.K, '190X'))
        self.assertEqual((bm['relationship'], bm['certain']), ('stepmother', False))

    def test_wife_dead_months_before_birth_same_year_is_earlier_wife(self):
        bm = self._bm(_married('C-0000000002', self.F, self.W, '1890'),
                      _died('C-0000000004', self.W, '1903-01-15'),
                      _born('C-0000000003', self.K, '1903-12-01'))
        self.assertEqual(bm['relationship'], "father's earlier wife")
        self.assertNotIn('step', bm['relationship'])

    def test_decade_death_of_wife_is_not_proof_she_died_before_birth(self):
        bm = self._bm(_married('C-0000000002', self.F, self.W, '1890'),
                      _died('C-0000000004', self.W, '190X'),
                      _born('C-0000000003', self.K, '1904'))
        self.assertEqual((bm['relationship'], bm['certain']), ('stepmother', False))

    def test_child_dead_before_the_marriage_has_no_stepmother(self):
        bm = self._bm(_married('C-0000000002', self.F, self.W, '1903'),
                      _born('C-0000000003', self.K, '1899'),
                      _died('C-0000000004', self.K, '1900'))
        self.assertEqual(bm['relationship'], "father's later wife")
        self.assertEqual(bm['explain'], 'married to Frank Ames after Baby Ames died')
        # And the other way: the baby is not her stepson.
        back = self._bm(_married('C-0000000002', self.F, self.W, '1903'),
                        _born('C-0000000003', self.K, '1899'),
                        _died('C-0000000004', self.K, '1900'), a=self.K, b=self.W)
        self.assertNotIn('step', back['relationship'])

    def test_child_death_decade_overlapping_marriage_is_hedged(self):
        bm = self._bm(_married('C-0000000002', self.F, self.W, '1903'),
                      _born('C-0000000003', self.K, '1899'),
                      _died('C-0000000004', self.K, '190X'))
        self.assertEqual((bm['relationship'], bm['certain']), ('stepmother', False))
        self.assertIn('or only after Baby Ames died', bm['hedge'])

    @staticmethod
    def _undated(cid, ctype, pid):
        """A second record of the event that gives no date (a birthplace
        from a census, a death noted in a letter)."""
        role = 'child' if ctype == 'birth' else 'deceased'
        return (f'- value: "{pid} {ctype}, no date"\n'
                f'  id: {cid}\n  type: {ctype}\n'
                f'  persons: [{pid}]\n  roles: {{{role}: [{pid}]}}\n'
                f'  status: accepted\n  reviewed: 2026-01-01\n  confidence: low\n'
                f'  information: secondary\n  evidence: direct\n  notes: x.\n')

    def test_undated_second_death_record_keeps_the_dated_death(self):
        bm = self._bm(_married('C-0000000002', self.F, self.W, '1903'),
                      _born('C-0000000003', self.K, '1899'),
                      _died('C-0000000004', self.K, '1900'),
                      self._undated('C-0000000005', 'death', self.K))
        self.assertEqual(bm['relationship'], "father's later wife")

    def test_undated_second_birth_record_keeps_the_dated_birth(self):
        bm = self._bm(_married('C-0000000002', self.F, self.W, '1890'),
                      _died('C-0000000004', self.W, '1901-03-01'),
                      _born('C-0000000003', self.K, '1905-06-01'),
                      self._undated('C-0000000005', 'birth', self.K))
        self.assertEqual((bm['relationship'], bm['certain']), ("father's earlier wife", True))

    def test_spouse_tie_from_a_relationship_claim_reads_its_whole_date(self):
        # The pair's first spouse row comes from a relationship claim dated
        # "before 1920"; it proves nothing about when the marriage began.
        spouses = (f'- value: "{self.F} and {self.W} were husband and wife"\n'
                   f'  id: C-0000000001a\n  type: relationship\n  subtype: spouse\n'
                   f'  persons: [{self.F}, {self.W}]\n'
                   f'  roles: {{spouse: [{self.F}, {self.W}]}}\n  date: "[..1920]"\n'
                   f'  status: accepted\n  reviewed: 2026-01-01\n  confidence: high\n'
                   f'  information: primary\n  evidence: direct\n  notes: x.\n')
        bm = self._bm(spouses,
                      _born('C-0000000003', self.K, '1899'),
                      _died('C-0000000004', self.K, '1900'),
                      _died('C-0000000007', self.W, '1925-05-01'))
        self.assertNotEqual((bm['relationship'], bm['certain']), ('stepmother', True))

    def test_child_alive_long_after_marriage_is_certain(self):
        bm = self._bm(_married('C-0000000002', self.F, self.W, '1903'),
                      _born('C-0000000003', self.K, '1899'),
                      _died('C-0000000004', self.K, '1960'))
        self.assertEqual((bm['relationship'], bm['certain']), ('stepmother', True))


class RoutesTests(unittest.TestCase):
    """Cousins related two ways hear both. Two brothers who married two
    sisters have children who are double first cousins; a couple who were
    themselves cousins have grandchildren related through two lines, which
    is what explains a DNA match that looks too close."""

    # Two brothers M1, M2 (of P1 + P2) married two sisters W1, W2 (of Q1 + Q2).
    P1, P2, M1, M2 = 'P-r000000001', 'P-r000000002', 'P-r000000003', 'P-r000000004'
    Q1, Q2, W1, W2 = 'P-r000000005', 'P-r000000006', 'P-r000000007', 'P-r000000008'
    C1, C2 = 'P-r000000009', 'P-r00000000a'
    # Cousin marriage: G1 + G2 -> K1, K2; H1 + H2 -> J1, J2; J1 -> L1; J2 -> L2;
    # K1 + L1 -> C3; K2 + L2 -> C4. C3 and C4 are first cousins through
    # G1 + G2 and second cousins through H1 + H2.
    G1, G2, K1, K2 = 'P-r00000000b', 'P-r00000000c', 'P-r00000000d', 'P-r00000000e'
    H1, H2, J1, J2 = 'P-r00000000f', 'P-r00000000g', 'P-r00000000h', 'P-r00000000j'
    L1, L2, C3, C4 = 'P-r00000000k', 'P-r00000000m', 'P-r00000000n', 'P-r00000000p'

    @classmethod
    def setUpClass(cls):
        c = cls
        sexes = {c.P1: 'M', c.P2: 'F', c.M1: 'M', c.M2: 'M', c.Q1: 'M', c.Q2: 'F',
                 c.W1: 'F', c.W2: 'F', c.C1: 'F', c.C2: 'F',
                 c.G1: 'M', c.G2: 'F', c.K1: 'M', c.K2: 'M', c.H1: 'M', c.H2: 'F',
                 c.J1: 'M', c.J2: 'M', c.L1: 'F', c.L2: 'F', c.C3: 'F', c.C4: 'M'}
        names = {c.P1: 'Thomas Hartley', c.P2: 'Margaret Cole', c.M1: 'Calvin Hartley',
                 c.M2: 'Edward Hartley', c.Q1: 'John Cole', c.Q2: 'Mary Doe',
                 c.W1: 'Clara Cole', c.W2: 'Edith Cole', c.C1: 'Ethel Hartley',
                 c.C2: 'Frances Hartley',
                 c.G1: 'Grand Pa', c.G2: 'Grand Ma', c.K1: 'Kit Kay', c.K2: 'Ken Kay',
                 c.H1: 'Hal Hay', c.H2: 'Hester Hay', c.J1: 'Jay Hay', c.J2: 'Jon Hay',
                 c.L1: 'Lil Hay', c.L2: 'Lou Hay', c.C3: 'Ann Aye', c.C4: 'Bea Bee'}
        claims = (
            _child_of('C-r000000001', c.M1, [c.P1, c.P2])
            + _child_of('C-r000000002', c.M2, [c.P1, c.P2])
            + _child_of('C-r000000003', c.W1, [c.Q1, c.Q2])
            + _child_of('C-r000000004', c.W2, [c.Q1, c.Q2])
            + _child_of('C-r000000005', c.C1, [c.M1, c.W1])
            + _child_of('C-r000000006', c.C2, [c.M2, c.W2])
            + _child_of('C-r000000007', c.K1, [c.G1, c.G2])
            + _child_of('C-r000000008', c.K2, [c.G1, c.G2])
            + _child_of('C-r000000009', c.J1, [c.H1, c.H2])
            + _child_of('C-r00000000a', c.J2, [c.H1, c.H2])
            + _child_of('C-r00000000b', c.L1, [c.J1])
            + _child_of('C-r00000000c', c.L2, [c.J2])
            + _child_of('C-r00000000d', c.C3, [c.K1, c.L1])
            + _child_of('C-r00000000e', c.C4, [c.K2, c.L2])
        )
        cls.root = _write_archive(claims, sexes, names)

    def _blood(self, a, b):
        return _blood(self.root, a, b)

    def _text(self, a, b):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            relate._cmd_relate(relate.run_relate(self.root, load_fha_yaml(self.root), a, b))
        return out.getvalue()

    def test_double_first_cousins(self):
        b = self._blood(self.C1, self.C2)
        self.assertEqual(b['relationship'], 'double first cousin')
        self.assertIs(b['double'], True)
        self.assertEqual(len(b['routes']), 2)
        self.assertEqual([r['relationship'] for r in b['routes']],
                         ['first cousin', 'first cousin'])

    def test_double_text_names_both_couples(self):
        out = self._text(self.C1, self.C2)
        self.assertIn("Ethel Hartley is Frances Hartley's double first cousin", out)
        self.assertIn('common ancestors: ', out)
        self.assertIn('; and ', out)
        for name in ('Thomas Hartley', 'Margaret Cole', 'John Cole', 'Mary Doe'):
            self.assertIn(name, out)
        self.assertNotIn('and also', out)

    def test_cousins_by_two_lines_list_both_routes(self):
        b = self._blood(self.C3, self.C4)
        self.assertEqual(b['relationship'], 'first cousin')
        self.assertIs(b['double'], False)
        self.assertEqual([r['relationship'] for r in b['routes']],
                         ['first cousin', 'second cousin'])
        self.assertEqual(b['routes'][1]['common_ancestors'],
                         sorted([self.H1.lower(), self.H2.lower()]))

    def test_text_lists_second_route(self):
        out = self._text(self.C3, self.C4)
        self.assertIn("Ann Aye is Bea Bee's first cousin  (common ancestors: ", out)
        self.assertIn("and also Bea Bee's second cousin, through another line", out)
        self.assertIn('Hal Hay', out)
        self.assertIn('Hester Hay', out)

    def test_route_order_is_the_same_both_ways(self):
        there = self._blood(self.C3, self.C4)['routes']
        back = self._blood(self.C4, self.C3)['routes']
        self.assertEqual([r['common_ancestors'] for r in there],
                         [r['common_ancestors'] for r in back])

    def test_single_route_has_one_route(self):
        root = _build()
        b = _blood(root, A, B)
        self.assertEqual(len(b['routes']), 1)
        self.assertIs(b['double'], False)
        self.assertEqual(b['routes'][0]['common_ancestors'], b['common_ancestors'])

    def test_many_routes_print_three_and_point_to_json(self):
        # A deeply intermarried family can join two people five ways; he sees
        # the nearest three and is told how many more there are.
        import contextlib
        import io
        route = {'relationship': 'third cousin', 'common_ancestors': [], 'gens_a': 4,
                 'gens_b': 4, 'half': False, 'half_relationship': None}
        blood = dict(route, note=None, double=False, routes=[dict(route) for _ in range(5)])
        result = relate.Result(ok=True, exit_code=EXIT_CLEAN, data={
            'a': 'P-r000000001', 'b': 'P-r000000002', 'a_name': 'Ann Aye',
            'b_name': 'Bea Bee', 'names': {}, 'blood': blood, 'by_marriage': None,
            'any': None})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            relate._cmd_relate(result)
        text = out.getvalue()
        self.assertEqual(text.count('and also'), 2)
        self.assertIn('and 2 more routes; run with --json to see them all', text)

    def test_double_with_a_further_line_names_two_couples_then_the_third(self):
        # Double first cousins whose grandmothers were cousins: the Blood
        # line names the two grandparent couples, and the further line gets
        # its own "and also" line instead of a third couple on the first.
        import contextlib
        import io
        def route(anc, g, word):
            return {'relationship': word, 'common_ancestors': anc, 'gens_a': g,
                    'gens_b': g, 'half': False, 'half_relationship': None}
        routes = [route(['P-r000000003', 'P-r000000004'], 2, 'first cousin'),
                  route(['P-r000000005', 'P-r000000006'], 2, 'first cousin'),
                  route(['P-r000000007', 'P-r000000008'], 4, 'third cousin')]
        blood = dict(routes[0], relationship='double first cousin', note=None,
                     double=True, routes=routes)
        names = {f'P-r00000000{i}': n for i, n in
                 zip('345678', ('Thomas Hartley', 'Margaret Cole', 'John Cole',
                                'Mary Doe', 'Old Pa', 'Old Ma'))}
        result = relate.Result(ok=True, exit_code=EXIT_CLEAN, data={
            'a': 'P-r000000001', 'b': 'P-r000000002', 'a_name': 'Ethel Hartley',
            'b_name': 'Frances Hartley', 'names': names, 'blood': blood,
            'by_marriage': None, 'any': None})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            relate._cmd_relate(result)
        lines = out.getvalue().splitlines()
        blood_line = next(line for line in lines if line.startswith('Blood:'))
        self.assertIn('double first cousin', blood_line)
        self.assertIn('Mary Doe', blood_line)
        self.assertNotIn('Old Pa', blood_line)
        also = [line for line in lines if 'and also' in line]
        self.assertEqual(len(also), 1)
        self.assertIn("and also Frances Hartley's third cousin, through another line", also[0])
        self.assertIn('Old Ma', also[0])

    def test_open_half_on_another_line_is_said(self):
        # A second route through one grandparent, with the other parent
        # unrecorded, must not be stated as a plain full cousin.
        import contextlib
        import io
        first = {'relationship': 'first cousin', 'common_ancestors': [], 'gens_a': 2,
                 'gens_b': 2, 'half': False, 'half_relationship': None}
        second = {'relationship': 'second cousin', 'common_ancestors': [], 'gens_a': 3,
                  'gens_b': 3, 'half': None, 'half_relationship': 'half second cousin'}
        blood = dict(first, note=None, double=False, routes=[first, second])
        result = relate.Result(ok=True, exit_code=EXIT_CLEAN, data={
            'a': 'P-r000000001', 'b': 'P-r000000002', 'a_name': 'Ann Aye',
            'b_name': 'Bea Bee', 'names': {}, 'blood': blood, 'by_marriage': None,
            'any': None})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            relate._cmd_relate(result)
        self.assertIn("and also Bea Bee's second cousin or half second cousin, "
                      'through another line', out.getvalue())


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


class EveryoneTests(unittest.TestCase):
    """`fha relate P --all`: one list of everyone related to one person,
    blood relatives nearest first, then everyone related by marriage."""

    @classmethod
    def setUpClass(cls):
        cls.root = _build_family()

    def _all(self, who=A, **kw):
        return relate.run_relate(self.root, load_fha_yaml(self.root), who, None,
                                 everyone=True, **kw)

    def _rows(self, who=A):
        return {r['pid'].lower(): r for r in self._all(who).data['relatives']}

    def test_all_lists_blood_then_marriage(self):
        r = self._all()
        self.assertEqual(r.exit_code, EXIT_CLEAN)
        rows = {x['pid']: x for x in r.data['relatives']}
        self.assertEqual(rows[XP.lower()]['relationship'], 'father')
        self.assertEqual(rows[SIB.lower()]['relationship'], 'sister')
        self.assertEqual(rows[HALF.lower()]['relationship'], 'half-brother')
        self.assertEqual(rows[B.lower()]['relationship'], 'first cousin')
        self.assertEqual(rows[AW.lower()]['relationship'], 'wife')
        self.assertEqual(rows[AWB.lower()]['relationship'], 'brother-in-law')
        self.assertEqual(rows[YH.lower()]['relationship'], 'uncle by marriage')
        kinds = [x['kind'] for x in r.data['relatives']]
        self.assertEqual(kinds, sorted(kinds, key=lambda k: k != 'blood'))
        order = [x['pid'] for x in r.data['relatives']]
        order = [o.lower() for o in order]
        self.assertLess(order.index(XP.lower()), order.index(GF.lower()))
        self.assertLess(order.index(GF.lower()), order.index(B.lower()))
        self.assertEqual(r.data['counts']['blood'] + r.data['counts']['by_marriage'],
                         len(order))

    def test_all_excludes_unrelated_friend(self):
        root = _build()
        r = relate.run_relate(root, load_fha_yaml(root), A, None, everyone=True)
        pids = {x['pid'].lower() for x in r.data['relatives']}
        self.assertNotIn(FRIEND.lower(), pids)
        self.assertIn(B.lower(), pids)

    def test_all_never_lists_the_person(self):
        self.assertNotIn(A.lower(), self._rows())

    def test_all_with_two_people_refused(self):
        r = relate.run_relate(self.root, load_fha_yaml(self.root), A, B, everyone=True)
        self.assertEqual(r.exit_code, EXIT_ERRORS)
        self.assertIn('lists everyone related to one person', r.messages[0].text)

    def test_all_with_cm_refused(self):
        r = relate.run_relate(self.root, load_fha_yaml(self.root), A, None, '450',
                              everyone=True)
        self.assertEqual(r.exit_code, EXIT_ERRORS)
        self.assertIn('`--all` and `--cm` do not go together', r.messages[0].text)

    def test_one_person_without_all_refused(self):
        r = relate.run_relate(self.root, load_fha_yaml(self.root), A, None)
        self.assertEqual(r.exit_code, EXIT_ERRORS)
        self.assertIn('add --all', r.messages[0].text)

    def test_nobody_refused(self):
        r = relate.run_relate(self.root, load_fha_yaml(self.root), None, None)
        self.assertEqual(r.exit_code, EXIT_ERRORS)

    def test_all_by_name(self):
        r = relate.run_relate(self.root, load_fha_yaml(self.root), 'Ann Aye', None,
                              everyone=True)
        self.assertEqual(r.exit_code, EXIT_CLEAN)
        self.assertEqual(r.data['person'].lower(), A.lower())

    def test_unknown_name_refused_as_before(self):
        r = relate.run_relate(self.root, load_fha_yaml(self.root), 'Nobody Here', None,
                              everyone=True)
        self.assertEqual(r.exit_code, EXIT_FAILURE)

    def test_half_may_be_hedged_on_the_row(self):
        text = self._text()
        self.assertIn('May Aye', text)
        self.assertIn('sister (may be half-sister)', text)

    def _text(self, who=A):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            relate._cmd_relate(self._all(who))
        return out.getvalue()

    def test_text_lists_blood_then_marriage_sections(self):
        text = self._text()
        self.assertIn('Everyone related to [[', text)
        self.assertLess(text.index('Blood relatives ('), text.index('Related by marriage ('))
        self.assertIn('[[' + fmt_id_display(XP) + '|Ex Parent]]', text)

    def test_all_json_round_trip(self):
        import json
        d = json.loads(json.dumps(self._all().as_dict()))
        self.assertEqual(d['data']['person_name'], 'Ann Aye')
        self.assertEqual(set(d['data']['relatives'][0]),
                         {'pid', 'name', 'kind', 'relationship', 'half', 'half_relationship',
                          'certain', 'gens_a', 'gens_b', 'double', 'routes'})

    def test_all_obsidian_links(self):
        root = _build_family()
        (root / 'fha.yaml').write_text(
            'roots:\n  documents: documents\nlinks: obsidian\n',
            encoding='utf-8')
        r = relate.run_relate(root, load_fha_yaml(root), A, None, everyone=True)
        self.assertEqual(r.exit_code, EXIT_CLEAN)
        links = {k.lower(): v for k, v in r.data['links'].items()}
        self.assertIn(XP.lower(), links)
        self.assertTrue(links[XP.lower()].startswith('[['))
        self.assertNotIn(XP.lower(), links[XP.lower()].split('|')[0])

    def test_nobody_related_says_so(self):
        import contextlib
        import io
        root = _build()
        r = relate.run_relate(root, load_fha_yaml(root), FRIEND, None, everyone=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = relate._cmd_relate(r)
        self.assertEqual(code, EXIT_CLEAN)
        self.assertIn('Nobody in the recorded tree is related to Fred Friend yet', out.getvalue())

    def test_merged_away_record_not_listed(self):
        root = _build_family()
        conn = sqlite3.connect(str(root / '.cache' / 'index.sqlite'))
        conn.execute('UPDATE persons SET merged_into=? WHERE id=?', (SIB.lower(), B.lower()))
        conn.commit()
        conn.close()
        r = relate.run_relate(root, load_fha_yaml(root), A, None, everyone=True)
        self.assertNotIn(B.lower(), {x['pid'].lower() for x in r.data['relatives']})


# A third family for the `--all` wording and order checks.
E_A = 'P-e1e1e1e1e1'     # (M) the person asked about; married E_W
E_W = 'P-e2e2e2e2e2'     # (F) his wife
E_WF = 'P-e3e3e3e3e3'    # (M) her father, "Zed": his father-in-law
E_G = 'P-e4e4e4e4e4'     # (M) her grandfather
E_U = 'P-e5e5e5e5e5'     # (M) her uncle
E_V = 'P-e6e6e6e6e6'     # (M) her first cousin
E_C = 'P-e7e7e7e7e7'     # (M) her first cousin once removed, "Aaron"
# Two brothers married two sisters: their children are double first cousins.
D_P1, D_P2 = 'P-f1f1f1f1f1', 'P-f2f2f2f2f2'      # the brothers' parents
D_P3, D_P4 = 'P-f3f3f3f3f3', 'P-f4f4f4f4f4'      # the sisters' parents
D_M1, D_M2 = 'P-f5f5f5f5f5', 'P-f6f6f6f6f6'      # the brothers
D_F1, D_F2 = 'P-f7f7f7f7f7', 'P-f8f8f8f8f8'      # the sisters
D_K1, D_K2 = 'P-f9f9f9f9f9', 'P-g1g1g1g1g1'      # the double first cousins
# A man who married his first cousin: his brother is their son's uncle and,
# through the wife, also his first cousin once removed.
C_G = 'P-h1h1h1h1h1'
C_S1, C_S2 = 'P-h2h2h2h2h2', 'P-h3h3h3h3h3'      # siblings, children of C_G
C_H, C_HB = 'P-h4h4h4h4h4', 'P-h5h5h5h5h5'       # C_S1's sons
C_W = 'P-h6h6h6h6h6'                             # C_S2's daughter, C_H's wife
C_K = 'P-h7h7h7h7h7'                             # their son


def _build_wording() -> Path:
    claims = (
        _married('C-e000000001', E_A, E_W)
        + _child_of('C-e000000002', E_W, [E_WF])
        + _child_of('C-e000000003', E_WF, [E_G])
        + _child_of('C-e000000004', E_U, [E_G])
        + _child_of('C-e000000005', E_V, [E_U])
        + _child_of('C-e000000006', E_C, [E_V])
        + _child_of('C-f000000001', D_M1, [D_P1, D_P2])
        + _child_of('C-f000000002', D_M2, [D_P1, D_P2])
        + _child_of('C-f000000003', D_F1, [D_P3, D_P4])
        + _child_of('C-f000000004', D_F2, [D_P3, D_P4])
        + _child_of('C-f000000005', D_K1, [D_M1, D_F1])
        + _child_of('C-f000000006', D_K2, [D_M2, D_F2])
        + _child_of('C-h000000001', C_S1, [C_G])
        + _child_of('C-h000000002', C_S2, [C_G])
        + _child_of('C-h000000003', C_H, [C_S1])
        + _child_of('C-h000000004', C_HB, [C_S1])
        + _child_of('C-h000000005', C_W, [C_S2])
        + _married('C-h000000006', C_H, C_W)
        + _child_of('C-h000000007', C_K, [C_H, C_W])
    )
    people = [E_A, E_W, E_WF, E_G, E_U, E_V, E_C, D_P1, D_P2, D_P3, D_P4, D_M1, D_M2,
              D_F1, D_F2, D_K1, D_K2, C_G, C_S1, C_S2, C_H, C_HB, C_W, C_K]
    sexes = {p: 'M' for p in people}
    for p in (E_W, D_P2, D_P4, D_F1, D_F2, D_K2, C_S2, C_W):
        sexes[p] = 'F'
    names = {p: f'Person {p[2:4].upper()}' for p in people}
    names.update({E_A: 'Abe Ames', E_W: 'Wanda Ames', E_WF: 'Zed Zane',
                  E_C: 'Aaron Able', D_K1: 'Kit One', D_K2: 'Kay Two',
                  C_HB: 'Hank Bell', C_K: 'Kip Bell'})
    return _write_archive(claims, sexes, names)


class EveryoneWordingTests(unittest.TestCase):
    """The `--all` list names each tie in the words, and the order, the
    two-person answer uses."""

    @classmethod
    def setUpClass(cls):
        cls.root = _build_wording()

    def _all(self, who):
        r = relate.run_relate(self.root, load_fha_yaml(self.root), who, None, everyone=True)
        self.assertEqual(r.exit_code, EXIT_CLEAN)
        return r

    def test_father_in_law_comes_before_a_wifes_distant_cousin(self):
        # Sorting by name alone put "Aaron" (her first cousin once removed)
        # ahead of "Zed" (her father) in his reunion list.
        order = [x['pid'].lower() for x in self._all(E_A).data['relatives']]
        self.assertLess(order.index(E_WF.lower()), order.index(E_C.lower()))

    def test_double_first_cousin_on_the_row(self):
        r = self._all(D_K1)
        row = {x['pid'].lower(): x for x in r.data['relatives']}[D_K2.lower()]
        self.assertEqual(row['relationship'], 'double first cousin')
        self.assertTrue(row['double'])
        self.assertEqual(r.data['words'][row['pid']], 'double first cousin')

    def test_every_route_said_as_the_pair_answer_says_it(self):
        r = self._all(C_K)
        row = {x['pid'].lower(): x for x in r.data['relatives']}[C_HB.lower()]
        pair = relate.run_relate(self.root, load_fha_yaml(self.root), C_HB, C_K).data['blood']
        self.assertEqual(row['relationship'], pair['relationship'])
        self.assertEqual(row['routes'], pair['routes'])
        self.assertEqual(len(row['routes']), 2)
        # One parent of each sibling is recorded, so both lines may be half,
        # and the list says so as the pair answer does.
        self.assertEqual(r.data['words'][row['pid']],
                         'uncle (may be half-uncle); and also first cousin once removed or '
                         'half first cousin once removed, through another line')


if __name__ == '__main__':
    unittest.main()
