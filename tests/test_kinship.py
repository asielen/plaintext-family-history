"""
test_kinship.py - the kinship engine in _lib, shared by `fha relate` and the
family website.

He gets the same relationship word from the command and from a person page
because both read one engine. These tests pin that engine directly: a graph
built by hand (`KinshipGraph.from_edges`) answers as `fha relate` does, the
graph loaded from an index is the same graph, an edge filter leaves rows out,
the naming words match the words `fha relate` has always used, and
`kin_relationship` gives the one-line view a page needs.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))

import _lib
from _lib import (KinshipGraph, blood_answer, kin_graph, kin_relationship,
                  kinship_path, kinship_word, open_index_db)
import test_relate as tr

WORDS = ROOT / 'tests' / 'fixtures' / 'relate-golden' / 'kinship-words.json'

# The test_relate family, in the lower-case form the index stores IDs in.
GF, GM, XP, YP, A, B, SIB, FRIEND, SID = (
    p.lower() for p in (tr.GF, tr.GM, tr.XP, tr.YP, tr.A, tr.B, tr.SIB, tr.FRIEND, tr.SID))
_SEX = {p.lower(): s for p, s in tr._SEX.items()}
_NAME = {p.lower(): n for p, n in tr._NAME.items()}


def _hand_graph() -> KinshipGraph:
    """The test_relate family, built without an index."""
    people = {pid: {'name': _NAME[pid], 'sex': _SEX[pid]} for pid in _SEX}
    parents = [
        (XP, GF, 'biological'), (XP, GM, 'biological'),
        (YP, GF, 'biological'), (YP, GM, 'biological'),
        (A, XP, 'biological'), (SIB, XP, 'biological'),
        (B, YP, 'biological'),
    ]
    return KinshipGraph.from_edges(people, parent_edges=parents,
                                   social_edges=[(A, FRIEND, 'friend')])


class KinshipGraphFromEdgesTests(unittest.TestCase):
    def setUp(self):
        self.g = _hand_graph()

    def test_first_cousins(self):
        b = blood_answer(self.g, A, B)
        self.assertEqual(b['relationship'], 'first cousin')
        self.assertEqual(set(b['common_ancestors']), {GF, GM})
        self.assertEqual((b['gens_a'], b['gens_b']), (2, 2))

    def test_grandson_and_grandfather(self):
        self.assertEqual(blood_answer(self.g, A, GF)['relationship'], 'grandson')
        self.assertEqual(blood_answer(self.g, GF, A)['relationship'], 'grandfather')

    def test_siblings_share_one_parent(self):
        b = blood_answer(self.g, A, SIB)
        self.assertEqual(b['relationship'], 'brother')
        self.assertEqual(b['common_ancestors'], [XP])

    def test_friend_is_no_blood_but_a_path(self):
        self.assertIsNone(blood_answer(self.g, A, FRIEND))
        self.assertEqual(kinship_path(self.g, SIB, FRIEND)['rendered'],
                         "your brother's friend")

    def test_ancestors_and_children(self):
        self.assertEqual(self.g.ancestors(A), {A: 0, XP: 1, GF: 2, GM: 2})
        self.assertEqual(self.g.genetic_children(XP), {A, SIB})
        self.assertEqual(self.g.genetic_children(FRIEND), set())

    def test_non_genetic_parent_is_kept_apart(self):
        people = {'P-1': {'name': 'Kid', 'sex': 'F'}, 'P-2': {'name': 'Step', 'sex': 'M'}}
        g = KinshipGraph.from_edges(people, parent_edges=[('P-1', 'P-2', 'Step')],
                                    spouse_edges=[('P-2', 'P-3', '1900', None)])
        self.assertEqual(g.genetic_parents, {})
        self.assertEqual(g.other_parents, {'P-1': {'P-2': 'step'}})
        self.assertIsNone(blood_answer(g, 'P-1', 'P-2'))
        self.assertEqual(kinship_path(g, 'P-1', 'P-2')['rendered'], 'your father')
        self.assertEqual(g.spouses['P-3'], {'P-2': ('1900', None)})


class LoadKinshipGraphParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tr._build()

    def _load(self, **kw):
        conn = open_index_db(self.root, ('persons', 'relationships', 'claims'))
        try:
            return kin_graph(conn, **kw)
        finally:
            conn.close()

    def test_index_graph_equals_hand_graph(self):
        g, h = self._load(), _hand_graph()
        self.assertEqual(g.people, h.people)
        self.assertEqual(g.genetic_parents, h.genetic_parents)
        self.assertEqual(g.other_parents, h.other_parents)
        self.assertEqual(g.spouses, h.spouses)
        # The index also carries the friend claim's subtype ('friend'); the
        # hand graph has none for a social edge, so compare who and how.
        def hops(graph):
            return {k: sorted((o, r) for o, r, _s in v) for k, v in graph.adjacency.items()}
        self.assertEqual(hops(g), hops(h))

    def test_works_without_row_factory(self):
        import sqlite3
        conn = sqlite3.connect(self.root / '.cache' / 'index.sqlite')
        try:
            self.assertEqual(kin_graph(conn).genetic_parents, _hand_graph().genetic_parents)
        finally:
            conn.close()


class EdgeFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tr._build()

    def _load(self, edge_ok):
        conn = open_index_db(self.root, ('persons', 'relationships', 'claims'))
        try:
            return kin_graph(conn, edge_ok=edge_ok)
        finally:
            conn.close()

    def test_rejected_rows_are_left_out(self):
        g = self._load(lambda r: r['rel'] != 'friend')
        self.assertIsNone(kinship_path(g, A, FRIEND))
        self.assertEqual(blood_answer(g, A, B)['relationship'], 'first cousin')

    def test_dropping_one_claim_cuts_that_parent(self):
        g = self._load(lambda r: r['claim_id'] != 'c-5555555555')   # B child of YP
        self.assertIsNone(blood_answer(g, A, B))

    def test_row_carries_the_claim_fields(self):
        seen = []
        self._load(lambda r: seen.append(r) or True)
        self.assertTrue(seen)
        for key in ('person_id', 'rel', 'other_id', 'claim_id', 'date_start',
                    'date_end', 'subtype', 'source_id', 'status'):
            self.assertIn(key, seen[0])
        parent_rows = [r for r in seen if r['rel'] == 'parent']
        self.assertTrue(all(r['status'] == 'accepted' and r['source_id'] == SID
                            and r['subtype'] == 'biological' for r in parent_rows))

    def test_no_spouses_when_spouse_rows_dropped(self):
        people = {p: {'name': p, 'sex': ''} for p in ('P-1', 'P-2')}
        g = KinshipGraph.from_edges(people, spouse_edges=[('P-1', 'P-2', None, None)])
        self.assertEqual(set(g.spouses), {'P-1', 'P-2'})


class KinshipWordsUnchangedTests(unittest.TestCase):
    """The words fha relate printed before the engine moved, recorded from
    the old relate.py for every (gens_a, gens_b) up to 7 and some far ones.
    A deliberate wording change regenerates kinship-words.json."""

    def test_every_recorded_word(self):
        table = json.loads(WORDS.read_text(encoding='utf-8'))
        self.assertEqual(sorted(table), sorted(_word_grid()))
        for key, word in table.items():
            ga, gb, *sex = key.split(' ')
            with self.subTest(key=key):
                self.assertEqual(kinship_word(int(ga), int(gb), sex[0] if sex else ''), word)


class KinRelationshipTests(unittest.TestCase):
    def setUp(self):
        self.g = _hand_graph()

    def test_grandfather_from_the_grandchild(self):
        r = kin_relationship(self.g, A, GF)    # GF is A's ...
        self.assertEqual(r, {'words': 'grandfather', 'note': None, 'kind': 'blood',
                             'path': "father's father"})

    def test_words_match_relate_blood_line(self):
        for anchor in _SEX:
            for pid in _SEX:
                if anchor == pid:
                    continue
                blood = blood_answer(self.g, pid, anchor)
                with self.subTest(anchor=anchor, pid=pid):
                    r = kin_relationship(self.g, anchor, pid)
                    self.assertEqual(r['words'], blood['relationship'] if blood else '')

    def test_unrelated_and_self_are_none(self):
        none = {'words': '', 'note': None, 'kind': 'none', 'path': ''}
        self.assertEqual(kin_relationship(self.g, A, FRIEND), none)
        self.assertEqual(kin_relationship(self.g, A, A), none)

    def test_no_name_in_words_or_path(self):
        names = set(_NAME.values())
        for anchor in _SEX:
            for pid in _SEX:
                r = kin_relationship(self.g, anchor, pid)
                for n in names:
                    self.assertNotIn(n, r['words'] + r['path'])


class RelateIsAThinCliTests(unittest.TestCase):
    def test_relate_imports_only_lib(self):
        src = (ROOT / 'tools' / 'relate.py').read_text(encoding='utf-8')
        imports = [ln.split()[1] for ln in src.splitlines()
                   if ln.startswith(('import ', 'from '))]
        local = {p.stem for p in (ROOT / 'tools').glob('*.py')}
        self.assertEqual({m for m in imports if m in local}, {'_lib'})

    def test_engine_is_not_duplicated_in_relate(self):
        import relate
        for name in ('_load_graph', '_ancestors', '_blood_name', '_blood_answer',
                     '_path_answer', '_hop_label'):
            self.assertFalse(hasattr(relate, name), name)
        self.assertIs(relate.blood_answer, _lib.blood_answer)


class KinRelationshipPathFollowsBloodTests(unittest.TestCase):
    """A page that says "first cousin" must not explain it as "friend": the
    path of a blood answer is the blood line, even when a shorter social tie
    joins the two people."""

    def _cousins_who_are_friends(self):
        people = {pid: {'name': _NAME[pid], 'sex': _SEX[pid]} for pid in _SEX}
        parents = [
            (XP, GF, 'biological'), (XP, GM, 'biological'),
            (YP, GF, 'biological'), (YP, GM, 'biological'),
            (A, XP, 'biological'), (B, YP, 'biological'),
        ]
        return KinshipGraph.from_edges(people, parent_edges=parents,
                                       social_edges=[(A, B, 'friend')])

    def test_cousin_path_is_the_blood_line(self):
        g = self._cousins_who_are_friends()
        r = kin_relationship(g, A, B)        # B is A's first cousin
        self.assertEqual(r['words'], 'first cousin')
        self.assertEqual(r['path'], "father's sister's daughter")

    def test_lineal_path_goes_straight_up(self):
        g = self._cousins_who_are_friends()
        self.assertEqual(kin_relationship(g, B, GM)['path'], "mother's mother")
        self.assertEqual(kin_relationship(g, GM, B)['path'], "daughter's daughter")

    def test_path_never_uses_a_non_genetic_tie(self):
        # A step-parent shortcut must not stand in for the blood line.
        people = {pid: {'name': _NAME[pid], 'sex': _SEX[pid]} for pid in _SEX}
        parents = [
            (XP, GF, 'biological'), (YP, GF, 'biological'),
            (A, XP, 'biological'), (B, YP, 'biological'), (B, XP, 'step'),
        ]
        g = KinshipGraph.from_edges(people, parent_edges=parents)
        r = kin_relationship(g, A, B)
        self.assertEqual(r['words'], 'first cousin')
        self.assertEqual(r['path'], "father's sister's daughter")


class SiblingOrderIsStableTests(unittest.TestCase):
    """Asked the same question twice, he gets the same answer: when two equally
    short chains join two people, which one he is shown must not depend on
    Python's per-run hash seed."""

    SCRIPT = '''
import sys
sys.path.insert(0, sys.argv[1])
from _lib import KinshipGraph, kinship_path
people = {p: {'name': p, 'sex': s} for p, s in
          (('pa', 'M'), ('pb', 'M'), ('pc', 'F'), ('px', 'M'), ('pd', 'M'), ('pe', 'F'))}
g = KinshipGraph.from_edges(
    people,
    parent_edges=[('pa', 'pd', None), ('pb', 'pd', None), ('pc', 'pd', None),
                  ('pa', 'pe', None), ('pb', 'pe', None), ('pc', 'pe', None)],
    social_edges=[('pb', 'px', 'friend'), ('pc', 'px', 'friend')])
print(kinship_path(g, 'pa', 'px')['rendered'])
'''

    def test_same_chain_under_every_hash_seed(self):
        import os
        import subprocess
        seen = set()
        for seed in range(8):
            env = dict(os.environ, PYTHONHASHSEED=str(seed))
            out = subprocess.run([sys.executable, '-c', self.SCRIPT, str(ROOT / 'tools')],
                                 env=env, capture_output=True, text=True, check=True)
            seen.add(out.stdout.strip())
        self.assertEqual(len(seen), 1, seen)


class AnyRowFactoryTests(unittest.TestCase):
    """kin_graph promises to work whatever row_factory a caller set."""

    @classmethod
    def setUpClass(cls):
        cls.root = tr._build()

    def test_dict_row_factory(self):
        import sqlite3
        conn = sqlite3.connect(self.root / '.cache' / 'index.sqlite')
        try:
            plain = kin_graph(conn)
            conn.row_factory = lambda cur, row: {d[0]: v for d, v in zip(cur.description, row)}
            as_dicts = kin_graph(conn)
        finally:
            conn.close()
        self.assertEqual(as_dicts.people, plain.people)
        self.assertEqual(as_dicts.genetic_parents, plain.genetic_parents)


def _word_grid() -> dict[str, str]:
    """Every (gens_a, gens_b) up to 7 for five sex spellings, plus far pairs."""
    grid = {f'{a} {b} {s}': kinship_word(a, b, s)
            for a in range(8) for b in range(8) for s in ('M', 'F', '', 'unknown', 'm')}
    for a, b in ((2, 14), (14, 2), (12, 13), (1, 9), (9, 1), (0, 12), (12, 0)):
        for s in ('M', 'F', ''):
            grid[f'{a} {b} {s}'] = kinship_word(a, b, s)
    return grid


if __name__ == '__main__':
    if '--write' in sys.argv:
        # Only when a word is MEANT to change; say which in the commit.
        WORDS.write_text(json.dumps(_word_grid(), indent=1, sort_keys=True) + '\n',
                         encoding='utf-8', newline='\n')
        print(f'wrote {WORDS}')
    else:
        unittest.main()
