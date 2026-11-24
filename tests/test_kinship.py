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
        # The path names the kind of tie, so a step-parent never reads as a
        # father by birth.
        self.assertEqual(kinship_path(g, 'P-1', 'P-2')['rendered'], 'your stepfather')
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
    """The words fha relate prints, recorded for every (gens_a, gens_b) up to
    7 and some far ones, so no word shifts by accident. A deliberate wording
    change regenerates kinship-words.json and says which words changed."""

    def test_every_recorded_word(self):
        table = json.loads(WORDS.read_text(encoding='utf-8'))
        self.assertEqual(sorted(table), sorted(_word_grid()))
        for key, word in table.items():
            ga, gb, *sex = key.split(' ')
            with self.subTest(key=key):
                self.assertEqual(kinship_word(int(ga), int(gb), sex[0] if sex else ''), word)


class KinshipWordTableTests(unittest.TestCase):
    """Every row of the naming table (build doc 02 §3), for a man, a woman
    and someone whose sex is not recorded as M or F. The expected words are
    the ones a family says, written out here by hand, not read back from the
    code: "great-aunt", not "grandaunt"; "aunt or uncle", not "aunt/uncle"."""

    ROWS = [
        # (gens_a, gens_b, M, F, other)
        ((0, 0), 'the same person', 'the same person', 'the same person'),
        ((0, 1), 'father', 'mother', 'parent'),
        ((0, 2), 'grandfather', 'grandmother', 'grandparent'),
        ((0, 3), 'great-grandfather', 'great-grandmother', 'great-grandparent'),
        ((0, 5), 'great-great-great-grandfather', 'great-great-great-grandmother',
         'great-great-great-grandparent'),
        ((1, 0), 'son', 'daughter', 'child'),
        ((2, 0), 'grandson', 'granddaughter', 'grandchild'),
        ((4, 0), 'great-great-grandson', 'great-great-granddaughter',
         'great-great-grandchild'),
        ((1, 1), 'brother', 'sister', 'sibling'),
        ((1, 2), 'uncle', 'aunt', 'aunt or uncle'),
        ((1, 3), 'great-uncle', 'great-aunt', 'great-aunt or great-uncle'),
        ((1, 4), 'great-great-uncle', 'great-great-aunt',
         'great-great-aunt or great-great-uncle'),
        ((2, 1), 'nephew', 'niece', 'niece or nephew'),
        ((3, 1), 'great-nephew', 'great-niece', 'great-niece or great-nephew'),
        ((5, 1), 'great-great-great-nephew', 'great-great-great-niece',
         'great-great-great-niece or great-great-great-nephew'),
        ((2, 2), 'first cousin', 'first cousin', 'first cousin'),
        ((2, 3), 'first cousin once removed', 'first cousin once removed',
         'first cousin once removed'),
        ((2, 5), 'first cousin three times removed', 'first cousin three times removed',
         'first cousin three times removed'),
        ((4, 3), 'second cousin once removed', 'second cousin once removed',
         'second cousin once removed'),
        ((3, 10), 'second cousin 7 times removed', 'second cousin 7 times removed',
         'second cousin 7 times removed'),
        ((12, 12), '11th cousin', '11th cousin', '11th cousin'),
    ]

    HALF_ROWS = [
        ((1, 1), 'half-brother', 'half-sister', 'half-sibling'),
        ((1, 2), 'half-uncle', 'half-aunt', 'half-aunt or half-uncle'),
        ((1, 3), 'half great-uncle', 'half great-aunt',
         'half great-aunt or half great-uncle'),
        ((1, 4), 'half great-great-uncle', 'half great-great-aunt',
         'half great-great-aunt or half great-great-uncle'),
        ((2, 1), 'half-nephew', 'half-niece', 'half-niece or half-nephew'),
        ((3, 1), 'half great-nephew', 'half great-niece',
         'half great-niece or half great-nephew'),
        ((2, 2), 'half first cousin', 'half first cousin', 'half first cousin'),
        ((2, 3), 'half first cousin once removed', 'half first cousin once removed',
         'half first cousin once removed'),
        # A lineal tie is never half.
        ((0, 2), 'grandfather', 'grandmother', 'grandparent'),
        ((1, 0), 'son', 'daughter', 'child'),
    ]

    def _check(self, rows, **kw):
        for (ga, gb), m, f, other in rows:
            for sex, word in (('M', m), ('F', f), ('', other), ('unknown', other),
                              ('X', other), ('intersex', other)):
                with self.subTest(gens=(ga, gb), sex=sex, **kw):
                    self.assertEqual(kinship_word(ga, gb, sex, **kw), word)

    def test_full_words(self):
        self._check(self.ROWS)

    def test_half_words(self):
        self._check(self.HALF_ROWS, half=True)

    def test_double_applies_to_cousins_only(self):
        self.assertEqual(kinship_word(2, 2, 'M', double=True), 'double first cousin')
        self.assertEqual(kinship_word(3, 4, 'F', double=True),
                         'double second cousin once removed')
        self.assertEqual(kinship_word(1, 1, 'M', double=True), 'brother')


def _half_graph(half_pairs=()) -> KinshipGraph:
    """GF and GM's children XP and YP; XP's child A, YP's child B, plus Q1
    and Q2, children of GF alone, and their children QA and QB."""
    people = {p: {'name': p, 'sex': s} for p, s in
              (('gf', 'M'), ('gm', 'F'), ('xp', 'M'), ('yp', 'F'), ('a', 'M'),
               ('b', 'F'), ('q1', 'M'), ('q2', 'F'), ('qa', 'M'), ('qb', 'F'),
               ('ggf', 'M'))}
    parents = [('xp', 'gf', None), ('xp', 'gm', None), ('yp', 'gf', None),
               ('yp', 'gm', None), ('a', 'xp', None), ('b', 'yp', None),
               ('q1', 'gf', None), ('q2', 'gf', None), ('qa', 'q1', None),
               ('qb', 'q2', None), ('gf', 'ggf', None)]
    return KinshipGraph.from_edges(people, parent_edges=parents,
                                   half_sibling_pairs=half_pairs)


class BloodRoutesTests(unittest.TestCase):
    def test_ancestor_of_a_common_ancestor_is_not_a_route(self):
        # GF's father is a common ancestor of the cousins too, but he is only
        # the way to GF, not a second way they are related.
        routes = _lib.blood_routes(_half_graph(), 'a', 'b')
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]['common_ancestors'], ['gf', 'gm'])
        self.assertIs(routes[0]['half'], False)

    def test_one_shared_grandparent_leaves_half_open(self):
        r = _lib.blood_routes(_half_graph(), 'qa', 'qb')[0]
        self.assertEqual((r['relationship'], r['half'], r['half_relationship']),
                         ('first cousin', None, 'half first cousin'))

    def test_a_half_sibling_record_settles_it(self):
        g = _half_graph([frozenset({'q1', 'q2'})])
        r = _lib.blood_routes(g, 'qa', 'qb')[0]
        self.assertEqual((r['relationship'], r['half'], r['half_relationship']),
                         ('half first cousin', True, None))

    def test_no_common_ancestor_no_route(self):
        self.assertEqual(_lib.blood_routes(_half_graph(), 'a', 'ggf'), [
            {'relationship': 'great-grandson', 'common_ancestors': ['ggf'],
             'gens_a': 3, 'gens_b': 0, 'half': False, 'half_relationship': None}])
        people = {'p1': {'name': 'p1', 'sex': ''}, 'p2': {'name': 'p2', 'sex': ''}}
        self.assertEqual(_lib.blood_routes(KinshipGraph.from_edges(people), 'p1', 'p2'), [])

    def test_blood_answer_note_names_both_people(self):
        people = {p: {'name': n, 'sex': s} for p, n, s in
                  (('f', 'Thomas Hartley', 'M'), ('e', 'Ethel Hartley', 'F'),
                   ('r', 'Frances Hartley', 'F'))}
        g = KinshipGraph.from_edges(people, parent_edges=[('e', 'f', None), ('r', 'f', None)])
        self.assertEqual(blood_answer(g, 'e', 'r')['note'],
                         'Only one parent is recorded for at least one of them, so '
                         "Ethel Hartley may be Frances Hartley's half-sister.")


def _cousin_marriage_graph(order=1) -> KinshipGraph:
    """G1 + G2 -> K1, K2; H1 + H2 -> J1, J2; J1 -> L1; J2 -> L2; K1 + L1 -> c3;
    K2 + L2 -> c4. `order=-1` lists the edges backwards, so a test can show
    the route order comes from the family, not from the input."""
    people = {p: {'name': p, 'sex': 'F'} for p in
              ('g1', 'g2', 'k1', 'k2', 'h1', 'h2', 'j1', 'j2', 'l1', 'l2', 'c3', 'c4')}
    parents = [('k1', 'g1', None), ('k1', 'g2', None), ('k2', 'g1', None),
               ('k2', 'g2', None), ('j1', 'h1', None), ('j1', 'h2', None),
               ('j2', 'h1', None), ('j2', 'h2', None), ('l1', 'j1', None),
               ('l2', 'j2', None), ('c3', 'k1', None), ('c3', 'l1', None),
               ('c4', 'k2', None), ('c4', 'l2', None)]
    return KinshipGraph.from_edges(people, parent_edges=parents[::order])


class SeveralRoutesTests(unittest.TestCase):
    """Two people related more than one way get every route, nearest first,
    and "double" only for two full routes at the same cousin degree."""

    def test_routes_sorted_nearest_first(self):
        for order in (1, -1):
            with self.subTest(order=order):
                routes = _lib.blood_routes(_cousin_marriage_graph(order), 'c3', 'c4')
                self.assertEqual([(r['relationship'], r['common_ancestors']) for r in routes],
                                 [('first cousin', ['g1', 'g2']),
                                  ('second cousin', ['h1', 'h2'])])

    def test_couple_grouping_needs_shared_child(self):
        # g1 and g2 are each a grandparent of both cousins, at the same
        # distance, but through different children: two separate lines, not
        # one couple. With the other parents unrecorded neither line is
        # certainly full, so this is not "double".
        people = {p: {'name': p, 'sex': 'M'} for p in
                  ('g1', 'g2', 'x1', 'x2', 'y1', 'y2', 'a', 'b')}
        parents = [('x1', 'g1', None), ('x2', 'g1', None), ('y1', 'g2', None),
                   ('y2', 'g2', None), ('a', 'x1', None), ('a', 'y1', None),
                   ('b', 'x2', None), ('b', 'y2', None)]
        g = KinshipGraph.from_edges(people, parent_edges=parents)
        answer = blood_answer(g, 'a', 'b')
        self.assertEqual([r['common_ancestors'] for r in answer['routes']], [['g1'], ['g2']])
        self.assertIs(answer['double'], False)
        self.assertEqual(answer['relationship'], 'first cousin')

    def test_double_needs_two_full_routes_at_one_degree(self):
        people = {p: {'name': p, 'sex': 'F'} for p in
                  ('p1', 'p2', 'q1', 'q2', 'm1', 'm2', 'w1', 'w2', 'c1', 'c2')}
        parents = [('m1', 'p1', None), ('m1', 'p2', None), ('m2', 'p1', None),
                   ('m2', 'p2', None), ('w1', 'q1', None), ('w1', 'q2', None),
                   ('w2', 'q1', None), ('w2', 'q2', None), ('c1', 'm1', None),
                   ('c1', 'w1', None), ('c2', 'm2', None), ('c2', 'w2', None)]
        g = KinshipGraph.from_edges(people, parent_edges=parents)
        answer = blood_answer(g, 'c1', 'c2')
        self.assertEqual((answer['relationship'], answer['double']),
                         ('double first cousin', True))
        # The nearest route still names its own couple; the routes list keeps
        # each line's own word.
        self.assertEqual(answer['common_ancestors'], ['p1', 'p2'])
        self.assertEqual([r['relationship'] for r in answer['routes']],
                         ['first cousin', 'first cousin'])
        # Two routes at different degrees are listed, never merged.
        cm = blood_answer(_cousin_marriage_graph(), 'c3', 'c4')
        self.assertEqual((cm['relationship'], cm['double']), ('first cousin', False))
        # Double siblings do not exist: two lines meeting in one couple is one route.
        sib = blood_answer(g, 'm1', 'm2')
        self.assertEqual((sib['relationship'], sib['double'], len(sib['routes'])),
                         ('sister', False, 1))

    def test_site_label_says_double(self):
        # The family website's line reads the same engine.
        people = {p: {'name': p, 'sex': 'F'} for p in
                  ('p1', 'p2', 'q1', 'q2', 'm1', 'm2', 'w1', 'w2', 'c1', 'c2')}
        parents = [('m1', 'p1', None), ('m1', 'p2', None), ('m2', 'p1', None),
                   ('m2', 'p2', None), ('w1', 'q1', None), ('w1', 'q2', None),
                   ('w2', 'q1', None), ('w2', 'q2', None), ('c1', 'm1', None),
                   ('c1', 'w1', None), ('c2', 'm2', None), ('c2', 'w2', None)]
        g = KinshipGraph.from_edges(people, parent_edges=parents)
        self.assertEqual(kin_relationship(g, 'c1', 'c2')['words'], 'double first cousin')


class HalfSiblingRecordsTests(unittest.TestCase):
    """A source saying two people are half-siblings is read from the index,
    and the site's edge filter can leave it out like any other tie."""

    @classmethod
    def setUpClass(cls):
        cls.root = tr._build_family()

    def _load(self, **kw):
        conn = open_index_db(self.root, ('persons', 'relationships', 'claims',
                                         'claim_persons'))
        try:
            return kin_graph(conn, **kw)
        finally:
            conn.close()

    def test_half_sibling_claims_are_loaded(self):
        g = self._load()
        self.assertEqual(g.half_sibling_pairs,
                         {frozenset({tr.PA.lower(), tr.PB.lower()}),
                          frozenset({tr.Q1.lower(), tr.Q2.lower()})})

    def test_edge_filter_sees_and_can_drop_them(self):
        seen = []

        def edge_ok(row):
            seen.append(row)
            return row['claim_id'] != 'c-a00000000a'
        g = self._load(edge_ok=edge_ok)
        self.assertEqual(g.half_sibling_pairs, {frozenset({tr.Q1.lower(), tr.Q2.lower()})})
        sib_rows = [r for r in seen if r['rel'] == 'sibling']
        self.assertTrue(sib_rows)
        for key in ('person_id', 'other_id', 'claim_id', 'source_id', 'status', 'subtype'):
            self.assertIn(key, sib_rows[0])
        self.assertEqual(sib_rows[0]['status'], 'accepted')


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

    def test_open_half_keeps_the_plain_word_with_a_note(self):
        # A page says "brother" and leaves the caveat to its note.
        r = kin_relationship(self.g, SIB, A)     # A and SIB share only XP
        self.assertEqual((r['words'], r['note']), ('brother', 'other parent not recorded'))
        self.assertIsNone(kin_relationship(self.g, B, A)['note'])

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


def _mg(sexes: dict[str, str], parents=(), spouses=()) -> KinshipGraph:
    """A small hand graph: `sexes` pid -> sex; names are the pid capitalised."""
    people = {p: {'name': p.capitalize(), 'sex': s} for p, s in sexes.items()}
    return KinshipGraph.from_edges(
        people, parent_edges=[(c, p, k) for c, p, k in parents],
        spouse_edges=[(a, b, start, end) for a, b, start, end in spouses])


SEXES = (('M', 0), ('F', 1), ('', 2))


def _pick(words, sex):
    return words[{'M': 0, 'F': 1, '': 2}[sex]]


class MarriageAnswerTests(unittest.TestCase):
    """Every row of the words-by-marriage table (build doc 02 §3), for a man,
    a woman and someone whose sex is not recorded: "x is y's ___"."""

    def _word(self, g, a, b, years=None):
        bm = _lib.marriage_answer(g, a, b, life=(years or {}).get)
        return bm['relationship'] if bm else None

    def test_spouse(self):
        for sex, _i in SEXES:
            g = _mg({'x': sex, 'y': 'F'}, spouses=[('x', 'y', '1900', None)])
            with self.subTest(sex=sex):
                self.assertEqual(self._word(g, 'x', 'y'),
                                 _pick(('husband', 'wife', 'spouse'), sex))

    def test_recorded_parent_kinds(self):
        table = {
            'step': (('stepfather', 'stepmother', 'step-parent'),
                     ('stepson', 'stepdaughter', 'stepchild')),
            'adoptive': (('adoptive father', 'adoptive mother', 'adoptive parent'),
                         ('adopted son', 'adopted daughter', 'adopted child')),
            'foster': (('foster father', 'foster mother', 'foster parent'),
                       ('foster son', 'foster daughter', 'foster child')),
            'guardian': (('guardian',) * 3, ('ward',) * 3),
            'surrogate-gestational': (('gestational surrogate',) * 3,
                                      ('child carried as a surrogate',) * 3),
            'social': (('social father', 'social mother', 'social parent'),
                       ('social son', 'social daughter', 'social child')),
        }
        for kind, (parent_words, child_words) in table.items():
            for sex, _i in SEXES:
                as_parent = _mg({'x': sex, 'c': 'F'}, parents=[('c', 'x', kind)])
                as_child = _mg({'x': sex, 'p': 'M'}, parents=[('x', 'p', kind)])
                with self.subTest(kind=kind, sex=sex):
                    self.assertEqual(self._word(as_parent, 'x', 'c'), _pick(parent_words, sex))
                    self.assertEqual(self._word(as_child, 'x', 'p'), _pick(child_words, sex))
                    bm = _lib.marriage_answer(as_parent, 'x', 'c')
                    self.assertEqual((bm['kind'], bm['certain'], bm['explain']),
                                     (kind, True, None))

    def test_siblings_through_a_parent_not_by_birth(self):
        for kind, words in (('adoptive', ('adoptive brother', 'adoptive sister', 'adoptive sibling')),
                            ('foster', ('foster brother', 'foster sister', 'foster sibling')),
                            ('step', ('stepbrother', 'stepsister', 'stepsibling'))):
            for sex, _i in SEXES:
                g = _mg({'x': sex, 'k': 'F', 'p': 'M'},
                        parents=[('k', 'p', 'biological'), ('x', 'p', kind)])
                with self.subTest(kind=kind, sex=sex):
                    self.assertEqual(self._word(g, 'x', 'k'), _pick(words, sex))
                    self.assertIsNone(blood_answer(g, 'x', 'k'))

    def test_derived_step_parent_by_the_dates(self):
        # y's father f married x; y was born in 1900.
        def graph(sex, start, end):
            return _mg({'x': sex, 'f': 'M', 'y': 'F'}, parents=[('y', 'f', 'biological')],
                       spouses=[('x', 'f', start, end)])
        for sex, _i in SEXES:
            word = _pick(('stepfather', 'stepmother', 'step-parent'), sex)
            with self.subTest(sex=sex):
                bm = _lib.marriage_answer(graph(sex, '1905', None), 'x', 'y',
                                          life={'y': 1900}.get)
                self.assertEqual((bm['relationship'], bm['kind'], bm['certain']),
                                 (word, 'step', True))
                # Married before the birth, still married after it.
                bm = _lib.marriage_answer(graph(sex, '1890', '1920'), 'x', 'y',
                                          life={'y': 1900}.get)
                self.assertEqual((bm['relationship'], bm['certain']), (word, True))
                # The marriage ended before the birth: not a step-parent.
                bm = _lib.marriage_answer(graph(sex, '1880', '1885'), 'x', 'y',
                                          life={'y': 1900}.get)
                spouse = _pick(('husband', 'wife', 'spouse'), sex)
                self.assertEqual(bm['relationship'], f"father's earlier {spouse}")
                self.assertEqual(bm['kind'], 'by-marriage')
                # Married before the birth, no end recorded: hedged.
                bm = _lib.marriage_answer(graph(sex, '1890', None), 'x', 'y',
                                          life={'y': 1900}.get)
                self.assertEqual((bm['relationship'], bm['certain']), (word, False))
                self.assertIn('X may instead have been', bm['hedge'])
                # No dates at all, or no birth year: hedged.
                bm = _lib.marriage_answer(graph(sex, None, None), 'x', 'y',
                                          life={'y': 1900}.get)
                self.assertIs(bm['certain'], False)
                bm = _lib.marriage_answer(graph(sex, '1905', None), 'x', 'y')
                self.assertIs(bm['certain'], False)

    def test_step_rule_reads_every_date_as_a_range(self):
        # y's father f married x. A vague date settles only what every day
        # inside it would; a child who died before the marriage has no
        # step-parent; a marriage that surely ended before the birth is not one.
        def word(start, end, born, died=None):
            g = _mg({'x': 'F', 'f': 'M', 'y': 'M'}, parents=[('y', 'f', 'biological')],
                    spouses=[('x', 'f', start, end)])
            bm = _lib.marriage_answer(g, 'x', 'y', life={'y': {'born': born, 'died': died}}.get)
            return bm['relationship'], bm['certain']
        cases = [
            (('1905', None, '1899'), ('stepmother', True)),
            (('1905', None, '[..1910]'), ('stepmother', False)),
            (('1905', None, '190X'), ('stepmother', False)),
            (('1903', None, '1903'), ('stepmother', False)),          # same year
            (('1890', '1903-01-15', '1903-12-01'), ("father's earlier wife", True)),
            (('1890', '190X', '1904'), ('stepmother', False)),        # end is a decade
            (('1890', '1903-01-01/1903-12-31', '1903-06-01'), ('stepmother', False)),
            (('1903', None, '1899', '1900'), ("father's later wife", True)),
            (('1903', None, '1899', '190X'), ('stepmother', False)),
            (('1903', None, '1899', '1960'), ('stepmother', True)),
            (('190X', None, '1899', '1960'), ('stepmother', True)),
            (('190X', None, '1899', '1905'), ('stepmother', False)),
            # An index lower bound with no upper bound ("before 1920") is no date.
            (('0001-01-01', '1925', '1899', '1900'), ('stepmother', False)),
        ]
        for args, want in cases:
            with self.subTest(args=args):
                self.assertEqual(word(*args), want)

    def test_stepchild_and_step_sibling_who_died_before_the_marriage(self):
        # y married s in 1903; s's son x died in 1900.
        g = _mg({'x': 'M', 's': 'F', 'y': 'M'}, parents=[('x', 's', 'biological')],
                spouses=[('y', 's', '1903', None)])
        bm = _lib.marriage_answer(g, 'x', 'y', life={'x': {'born': '1899', 'died': '1900'}}.get)
        self.assertEqual(bm['relationship'], "later wife's son")
        self.assertEqual(bm['explain'], "died before S's marriage to Y")
        # q's son x died before q married p, father of y: no stepbrother.
        g = _mg({'x': 'M', 'q': 'F', 'p': 'M', 'y': 'F'},
                parents=[('x', 'q', 'biological'), ('y', 'p', 'biological')],
                spouses=[('p', 'q', '1910', None)])
        bm = _lib.marriage_answer(g, 'x', 'y', life={
            'x': {'born': '1899', 'died': '1900'}, 'y': {'born': '1904'}}.get)
        self.assertTrue(bm is None or 'step' not in bm['relationship'])

    def test_derived_stepchild_by_the_dates(self):
        # y married s; x is s's child.
        def graph(sex, start, end):
            return _mg({'x': sex, 's': 'F', 'y': 'M'}, parents=[('x', 's', 'biological')],
                       spouses=[('y', 's', start, end)])
        for sex, _i in SEXES:
            word = _pick(('stepson', 'stepdaughter', 'stepchild'), sex)
            with self.subTest(sex=sex):
                bm = _lib.marriage_answer(graph(sex, '1905', None), 'x', 'y',
                                          life={'x': 1900}.get)
                self.assertEqual((bm['relationship'], bm['certain']), (word, True))
                self.assertEqual(bm['explain'], f"Y's wife's {_pick(('son', 'daughter', 'child'), sex)}")
                bm = _lib.marriage_answer(graph(sex, '1880', '1885'), 'x', 'y',
                                          life={'x': 1900}.get)
                self.assertNotIn('step', bm['relationship'])
                bm = _lib.marriage_answer(graph(sex, None, None), 'x', 'y',
                                          life={'x': 1900}.get)
                self.assertEqual((bm['relationship'], bm['certain']), (word, False))
                self.assertIn('X may instead have been born after', bm['hedge'])

    def test_step_sibling(self):
        # x is q's child; q married p; p is y's parent; they share no parent.
        for sex, _i in SEXES:
            g = _mg({'x': sex, 'q': 'F', 'p': 'M', 'y': 'F'},
                    parents=[('x', 'q', 'biological'), ('y', 'p', 'biological')],
                    spouses=[('p', 'q', '1910', None)])
            with self.subTest(sex=sex):
                bm = _lib.marriage_answer(g, 'x', 'y', life={'x': 1902, 'y': 1904}.get)
                self.assertEqual(bm['relationship'],
                                 _pick(('stepbrother', 'stepsister', 'stepsibling'), sex))
                self.assertIs(bm['certain'], True)
                self.assertEqual(bm['via'], ['p', 'q'])
                hedged = _lib.marriage_answer(g, 'x', 'y', life={'x': 1902}.get)
                self.assertIs(hedged['certain'], False)

    def test_sibling_in_law_three_ways(self):
        words = ('brother-in-law', 'sister-in-law', 'sibling-in-law')
        for sex, _i in SEXES:
            # x married y's sister s.
            married_sister = _mg({'x': sex, 'y': 'M', 's': 'F', 'p': 'M'},
                                 parents=[('y', 'p', None), ('s', 'p', None)],
                                 spouses=[('x', 's', None, None)])
            # x is the sibling of y's wife w.
            wifes_sibling = _mg({'x': sex, 'y': 'M', 'w': 'F', 'p': 'M'},
                                parents=[('x', 'p', None), ('w', 'p', None)],
                                spouses=[('y', 'w', None, None)])
            # x married r, the sister of y's wife w.
            co_in_law = _mg({'x': sex, 'y': 'M', 'w': 'F', 'r': 'F', 'p': 'M'},
                            parents=[('r', 'p', None), ('w', 'p', None)],
                            spouses=[('y', 'w', None, None), ('x', 'r', None, None)])
            with self.subTest(sex=sex):
                self.assertEqual(self._word(married_sister, 'x', 'y'), _pick(words, sex))
                self.assertEqual(self._word(wifes_sibling, 'x', 'y'), _pick(words, sex))
                bm = _lib.marriage_answer(co_in_law, 'x', 'y')
                self.assertEqual(bm['relationship'], _pick(words, sex))
                self.assertEqual(bm['explain'], "Y's wife's sister's "
                                 + _pick(('husband', 'wife', 'spouse'), sex))
                self.assertEqual(bm['via'], ['w', 'r'])

    def test_parent_and_grandparent_in_law(self):
        for sex, _i in SEXES:
            # y's wife w; w's parent m; m's parent g; g's parent gg.
            people = {'y': 'M', 'w': 'F', 'm': 'F', 'g': 'M', 'gg': 'M'}
            chain = [('w', 'm', None), ('m', 'g', None), ('g', 'gg', None)]
            for x in ('m', 'g', 'gg'):
                people_x = dict(people, **{x: sex})
                g = _mg(people_x, parents=chain, spouses=[('y', 'w', None, None)])
                want = {'m': ('father-in-law', 'mother-in-law', 'parent-in-law'),
                        'g': ('grandfather-in-law', 'grandmother-in-law', 'grandparent-in-law'),
                        'gg': ('great-grandfather-in-law', 'great-grandmother-in-law',
                               'great-grandparent-in-law')}[x]
                with self.subTest(sex=sex, x=x):
                    self.assertEqual(self._word(g, x, 'y'), _pick(want, sex))

    def test_child_and_grandchild_in_law(self):
        for sex, _i in SEXES:
            # y's daughter d, granddaughter gd, great-granddaughter ggd.
            chain = [('d', 'y', None), ('gd', 'd', None), ('ggd', 'gd', None)]
            for target, want in (('d', ('son-in-law', 'daughter-in-law', 'child-in-law')),
                                 ('gd', ('grandson-in-law', 'granddaughter-in-law',
                                         'grandchild-in-law')),
                                 ('ggd', ('great-grandson-in-law', 'great-granddaughter-in-law',
                                          'great-grandchild-in-law'))):
                g = _mg({'x': sex, 'y': 'M', 'd': 'F', 'gd': 'F', 'ggd': 'F'},
                        parents=chain, spouses=[('x', target, None, None)])
                with self.subTest(sex=sex, target=target):
                    self.assertEqual(self._word(g, 'x', 'y'), _pick(want, sex))

    def test_by_marriage(self):
        for sex, _i in SEXES:
            # gp -> aunt t and parent f -> y; x married the aunt.
            g = _mg({'x': sex, 'y': 'F', 't': 'F', 'f': 'M', 'gp': 'M'},
                    parents=[('t', 'gp', None), ('f', 'gp', None), ('y', 'f', None)],
                    spouses=[('x', 't', None, None)])
            with self.subTest(sex=sex):
                bm = _lib.marriage_answer(g, 'x', 'y')
                self.assertEqual(bm['relationship'],
                                 _pick(('uncle', 'aunt', 'aunt or uncle'), sex) + ' by marriage')
                self.assertEqual(bm['explain'], "Y's aunt's " + _pick(('husband', 'wife', 'spouse'), sex))
            # x is the niece of y's wife w.
            g = _mg({'x': sex, 'y': 'M', 'w': 'F', 'b': 'M', 'gp': 'M'},
                    parents=[('w', 'gp', None), ('b', 'gp', None), ('x', 'b', None)],
                    spouses=[('y', 'w', None, None)])
            with self.subTest(sex=sex, way='niece'):
                self.assertEqual(self._word(g, 'x', 'y'),
                                 _pick(('nephew', 'niece', 'niece or nephew'), sex) + ' by marriage')

    def test_grandparents_spouse_and_spouses_grandchild_are_chains(self):
        g = _mg({'x': 'F', 'y': 'M', 'f': 'M', 'gp': 'M'},
                parents=[('y', 'f', None), ('f', 'gp', None)],
                spouses=[('x', 'gp', None, None)])
        self.assertEqual(self._word(g, 'x', 'y'), "grandfather's wife")
        # The word is already the chain; it is not printed twice.
        self.assertIsNone(_lib.marriage_answer(g, 'x', 'y')['explain'])
        g = _mg({'x': 'M', 'y': 'M', 'w': 'F', 'c': 'F'},
                parents=[('c', 'w', None), ('x', 'c', None)],
                spouses=[('y', 'w', None, None)])
        self.assertEqual(self._word(g, 'x', 'y'), "wife's grandson")

    def test_blood_wins(self):
        # y's father f married x, who is also y's mother: blood, so no step word.
        g = _mg({'x': 'F', 'f': 'M', 'y': 'F'},
                parents=[('y', 'f', None), ('y', 'x', None)],
                spouses=[('x', 'f', None, None)])
        self.assertIsNone(_lib.marriage_answer(g, 'x', 'y'))
        self.assertEqual(_lib.kinship_answer(g, 'x', 'y')['by_marriage'], None)
        # A cousin who married into the family stays a cousin.
        g = _mg({'x': 'M', 'y': 'F', 'a': 'M', 'b': 'M', 'gp': 'M', 's': 'M'},
                parents=[('a', 'gp', None), ('b', 'gp', None), ('x', 'a', None),
                         ('y', 'b', None), ('s', 'b', None)],
                spouses=[('x', 's', None, None)])
        answer = _lib.kinship_answer(g, 'x', 'y')
        self.assertEqual(answer['blood']['relationship'], 'first cousin')
        self.assertIsNone(answer['by_marriage'])

    def test_adoptive_and_step_parents_make_no_blood_tie(self):
        # gp -> f (birth); f adopts c; f's stepchild s. Neither c nor s is
        # blood kin of f, of gp, or of f's birth child k.
        g = _mg({'gp': 'M', 'f': 'M', 'c': 'F', 's': 'M', 'k': 'F'},
                parents=[('f', 'gp', None), ('c', 'f', 'adoptive'), ('s', 'f', 'step'),
                         ('k', 'f', None)])
        for x in ('c', 's'):
            for y in ('f', 'gp', 'k'):
                with self.subTest(x=x, y=y):
                    self.assertIsNone(blood_answer(g, x, y))
                    self.assertIsNone(blood_answer(g, y, x))
        self.assertEqual(self._word(g, 's', 'k'), 'stepbrother')

    def test_recorded_step_tie_beats_the_dates(self):
        # x is recorded as y's stepmother and also married y's father before
        # y's birth with no end date: the record says step, so no hedge.
        g = _mg({'x': 'F', 'f': 'M', 'y': 'F'},
                parents=[('y', 'f', None), ('y', 'x', 'step')],
                spouses=[('x', 'f', '1890', None)])
        bm = _lib.marriage_answer(g, 'x', 'y', life={'y': 1900}.get)
        self.assertEqual((bm['relationship'], bm['certain'], bm['explain']),
                         ('stepmother', True, None))

    def test_nothing_ties_them(self):
        g = _mg({'x': 'M', 'y': 'F'})
        self.assertIsNone(_lib.marriage_answer(g, 'x', 'y'))
        self.assertIsNone(_lib.marriage_answer(g, 'x', 'x'))


class KinRelationshipByMarriageTests(unittest.TestCase):
    """A page reads a tie by marriage as one short label with its kind, and a
    step tie the dates cannot settle carries the dates note."""

    def test_kinds_and_notes(self):
        g = _mg({'x': 'F', 'f': 'M', 'y': 'F', 'w': 'M', 'b': 'M', 'gp': 'M', 'c': 'F'},
                parents=[('y', 'f', None), ('b', 'gp', None), ('y', 'gp', None),
                         ('c', 'x', 'adoptive')],
                spouses=[('x', 'f', None, None), ('w', 'y', None, None)])
        self.assertEqual(kin_relationship(g, 'y', 'x'),
                         {'words': 'stepmother', 'note': 'marriage dates not recorded',
                          'kind': 'step', 'path': "father's wife"})
        self.assertEqual(kin_relationship(g, 'y', 'x', life={'y': 1900}.get)['note'],
                         'marriage dates not recorded')
        self.assertEqual(kin_relationship(g, 'y', 'w'),
                         {'words': 'husband', 'note': None, 'kind': 'marriage', 'path': 'husband'})
        self.assertEqual(kin_relationship(g, 'b', 'w')['kind'], 'marriage')
        self.assertEqual(kin_relationship(g, 'b', 'w')['words'], 'brother-in-law')
        self.assertEqual(kin_relationship(g, 'c', 'x'),
                         {'words': 'adoptive mother', 'note': None, 'kind': 'social',
                          'path': 'adoptive mother'})

    def test_words_match_relate_for_the_family_fixture(self):
        # kin_relationship's words are what fha relate's Blood or By marriage
        # line says, for every pair in the test_relate family (relate prints
        # kinship_answer; test_relate checks run_relate gives it).
        root = tr._build_family()
        conn = open_index_db(root, ('persons', 'relationships', 'claims'))
        try:
            g = kin_graph(conn)
            cache: dict = {}
            years = {p: _lib.person_life_dates(conn, p, cache) for p in g.people}
        finally:
            conn.close()
        pids = sorted(g.people)
        for a in pids:
            for b in pids:
                if a == b:
                    continue
                data = _lib.kinship_answer(g, a, b, life=years.get)
                tie = data['blood'] or data['by_marriage']
                with self.subTest(a=a, b=b):
                    self.assertEqual(kin_relationship(g, b, a, life=years.get)['words'],
                                     tie['relationship'] if tie else '')

    def test_life_dates_read_own_birth_and_death_only(self):
        root = tr._build_family()
        conn = open_index_db(root, ('persons', 'claims', 'claim_persons'))
        try:
            self.assertEqual(_lib.person_life_dates(conn, tr.A.lower(), {}),
                             {'born': ('1900-01-01', '1900-12-31'), 'died': None})
            self.assertEqual(_lib.person_life_dates(conn, tr.XP.lower(), {}),
                             {'born': None, 'died': None})
            self.assertEqual(_lib.person_life_dates(conn, tr.XW.lower(), {})['died'],
                             ('1910-01-01', '1910-12-31'))
        finally:
            conn.close()


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
