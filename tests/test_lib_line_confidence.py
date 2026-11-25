"""
test_lib_line_confidence.py - how strongly each person in a tree is tied to
the person the tree is drawn around.

When the only thing linking Grandpa Thomas to his parents is a family story,
everyone above Thomas rests on that one story, however good their own records
are. `_lib.line_confidence` works that out for every chart: each person gets
the weakest rated parent or child tie on their best route back to the seed,
so a chart can fade the weak part of the tree (chart plan 08-6, drawn by 08-7).
"""

from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))

from _lib import line_confidence, stronger_confidence, weaker_confidence  # noqa: E402


class StrongerConfidenceTests(unittest.TestCase):
    def test_the_better_word_wins_and_unrated_loses_to_any_word(self):
        self.assertEqual(stronger_confidence('low', 'high'), 'high')
        self.assertEqual(stronger_confidence('medium', 'low'), 'medium')
        self.assertEqual(stronger_confidence(None, 'low'), 'low')
        self.assertEqual(stronger_confidence('low', None), 'low')
        self.assertIsNone(stronger_confidence(None, None))

    def test_a_word_outside_the_scale_counts_as_unrated(self):
        self.assertEqual(stronger_confidence('certain', 'low'), 'low')

    def test_weaker_ignores_an_unrated_tie(self):
        # An unrated tie says nothing about strength, so it never weakens.
        self.assertEqual(weaker_confidence('high', 'low'), 'low')
        self.assertEqual(weaker_confidence(None, 'medium'), 'medium')
        self.assertEqual(weaker_confidence('high', None), 'high')
        self.assertIsNone(weaker_confidence(None, None))


class LineConfidenceTests(unittest.TestCase):
    def test_the_seed_alone_has_no_rated_line(self):
        self.assertEqual(line_confidence('kit', []), {'kit': None})

    def test_a_weak_tie_weakens_everyone_beyond_it(self):
        # Kit's father rests on a family story; his own father is on a
        # certificate, but is still reached only through the story.
        got = line_confidence('kit', [('kit', 'alan', 'parent', 'low'),
                                      ('alan', 'silas', 'parent', 'high')])
        self.assertEqual(got, {'kit': None, 'alan': 'low', 'silas': 'low'})

    def test_the_better_of_two_routes_counts(self):
        # Chart case C3: one grandparent reached through the father (weak) and
        # through the mother (strong). The strong route decides.
        edges = [('kit', 'alan', 'parent', 'low'), ('alan', 'calvin', 'parent', 'high'),
                 ('kit', 'ruth', 'parent', 'high'), ('ruth', 'calvin', 'parent', 'medium')]
        got = line_confidence('kit', edges)
        self.assertEqual(got['calvin'], 'medium')
        # Alan is Calvin's son on a certificate, so he is reached through
        # Ruth's side too, and that route is better than his own weak tie.
        self.assertEqual(got['alan'], 'medium')

    def test_a_spouse_takes_the_line_of_the_person_they_married(self):
        edges = [('calvin', 'alan', 'child', 'low'),
                 ('alan', 'ethel', 'spouse', None),
                 ('calvin', 'ruth', 'child', 'high'),
                 ('ruth', 'hugh', 'spouse', 'low')]
        got = line_confidence('calvin', edges)
        self.assertEqual(got['ethel'], 'low')
        # A spouse tie is never rated: a low-confidence marriage does not
        # weaken the husband.
        self.assertEqual(got['hugh'], 'high')

    def test_an_unrated_tie_does_not_weaken(self):
        got = line_confidence('kit', [('kit', 'alan', 'parent', None),
                                      ('alan', 'silas', 'parent', 'medium')])
        self.assertEqual(got, {'kit': None, 'alan': None, 'silas': 'medium'})

    def test_a_child_reached_through_a_weak_tie_is_weak_too(self):
        got = line_confidence('calvin', [('calvin', 'alan', 'child', 'low'),
                                         ('alan', 'wren', 'child', 'high')])
        self.assertEqual(got['wren'], 'low')

    def test_a_person_who_is_their_own_ancestor_ends_the_search(self):
        # Chart case C12: A's parent is B, B's parent is C, C's parent is A.
        got = line_confidence('a', [('a', 'b', 'parent', 'high'), ('b', 'c', 'parent', 'low'),
                                    ('c', 'a', 'parent', 'medium')])
        self.assertEqual(got, {'a': None, 'b': 'high', 'c': 'medium'})

    def test_the_answer_does_not_depend_on_edge_order(self):
        edges = [('kit', 'alan', 'parent', 'low'), ('alan', 'calvin', 'parent', 'high'),
                 ('kit', 'ruth', 'parent', 'high'), ('ruth', 'calvin', 'parent', 'medium'),
                 ('calvin', 'edith', 'spouse', None), ('ruth', 'hugh', 'spouse', 'low'),
                 ('hugh', 'mae', 'child', None), ('mae', 'kit', 'parent', 'low')]
        want = line_confidence('kit', edges)
        rng = random.Random(0)
        for _ in range(20):
            shuffled = list(edges)
            rng.shuffle(shuffled)
            got = line_confidence('kit', shuffled)
            self.assertEqual(got, want)
            self.assertEqual(list(got), list(want))   # same key order too

    def test_a_leaf_tie_gives_the_leaf_a_line_but_is_no_route_onward(self):
        # Thomas's son Samuel (high); Samuel's son Gus rests on a family
        # story (low); Gus's mother Ann married in and is on his birth record.
        edges = [('tom', 'sam', 'child', 'high'), ('sam', 'gus', 'child', 'low'),
                 ('sam', 'ann', 'spouse', None)]
        got = line_confidence('tom', edges, leaf_ties=[('ann', 'gus', 'high')])
        self.assertEqual(got['gus'], 'low')
        self.assertEqual(got['ann'], 'high')     # from her husband, the better route
        # Unmarried: her line is the weaker of her son's and her tie to him.
        got = line_confidence('tom', edges[:2], leaf_ties=[('ann', 'gus', 'high')])
        self.assertEqual(got, {'ann': 'low', 'gus': 'low', 'sam': 'high', 'tom': None})
        # A leaf tied to someone the seed cannot reach is left out.
        self.assertNotIn('ann', line_confidence('tom', [], leaf_ties=[('ann', 'gus', 'high')]))

    def test_people_the_seed_cannot_reach_are_left_out(self):
        got = line_confidence('kit', [('alan', 'silas', 'parent', 'high')])
        self.assertEqual(got, {'kit': None})


if __name__ == '__main__':
    unittest.main()
