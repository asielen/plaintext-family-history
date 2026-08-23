"""
test_lib_place_registry.py - the write-time place_text -> place_id lookup
(issue #79 point 3, "resolve at write time").

`_lib.match_place_text_to_registry` is the shared engine `fha claim`'s two
verbs (`run_claim_new`, `run_claim`) call so a claim carrying `place_text`
with no `place` can attach an already-registered place automatically,
without a separate `fha confirm place` step. It reuses the exact clustering
normalization `fha places candidates` groups unlinked place_text with
(`place_text_cluster_key`, moved out of `places.py` so both files can share
one copy - tools never import tools, TOOLING §15) - a guard here that the
two never drift apart from testing `places.py`'s own clustering tests
(`tests/test_places.py`) exercising an equivalent shape.

Covers three tiers pinned by the design the task asked for:
  - 'exact' (safe to auto-attach - claim.py's job, not this function's)
  - 'near' (surfaced, never auto-attached)
  - None (a genuine miss, or an ambiguous registry - never guesses)

Also covers `read_places_registry`'s best-effort degrade (missing file,
unparseable YAML, non-list top level, stray non-mapping rows) - the write
path this backs must never fail a claim mint because the registry file
happens to be malformed or absent.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

from _lib import (
    expand_place_abbreviations,
    match_place_text_to_registry,
    place_text_cluster_key,
    read_places_registry,
)


def _write_registry(root: Path, text: str) -> None:
    (root / 'places').mkdir(parents=True, exist_ok=True)
    (root / 'places' / 'places.yaml').write_text(text, encoding='utf-8')


class ReadPlacesRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_file_is_empty_not_an_error(self) -> None:
        self.assertEqual(read_places_registry(self.root), [])

    def test_unparseable_yaml_degrades_to_empty(self) -> None:
        _write_registry(self.root, '- id: L-aaaaaaaaaa\n  name: [unterminated\n')
        self.assertEqual(read_places_registry(self.root), [])

    def test_non_list_top_level_degrades_to_empty(self) -> None:
        _write_registry(self.root, 'not_a_list: true\n')
        self.assertEqual(read_places_registry(self.root), [])

    def test_stray_non_mapping_row_is_skipped_not_fatal(self) -> None:
        _write_registry(
            self.root,
            '- id: L-aaaaaaaaaa\n  name: Topeka\n- just a string\n- id: L-bbbbbbbbbb\n  name: Wichita\n')
        ids = [p['id'] for p in read_places_registry(self.root)]
        self.assertEqual(ids, ['L-aaaaaaaaaa', 'L-bbbbbbbbbb'])

    def test_row_with_no_id_is_skipped(self) -> None:
        _write_registry(self.root, '- name: No id here\n')
        self.assertEqual(read_places_registry(self.root), [])


class MatchPlaceTextToRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write_registry(
            self.root,
            '- id: L-baba9801fa\n'
            '  name: Topeka, Kansas\n'
            '  alt_names: ["Topeka County, Kansas"]\n')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_exact_match_on_name(self) -> None:
        m = match_place_text_to_registry(self.root, 'Topeka, Kansas')
        self.assertEqual(m['tier'], 'exact')
        self.assertEqual(m['place_id'], 'L-baba9801fa')
        self.assertEqual(m['name'], 'Topeka, Kansas')

    def test_exact_match_is_case_and_whitespace_insensitive(self) -> None:
        m = match_place_text_to_registry(self.root, '  topeka,   KANSAS  ')
        self.assertEqual(m['tier'], 'exact')
        self.assertEqual(m['place_id'], 'L-baba9801fa')

    def test_exact_match_on_alt_name(self) -> None:
        m = match_place_text_to_registry(self.root, 'Topeka County, Kansas')
        self.assertEqual(m['tier'], 'exact')
        self.assertEqual(m['place_id'], 'L-baba9801fa')
        self.assertEqual(m['name'], 'Topeka County, Kansas')

    def test_near_match_on_word_order_is_not_exact(self) -> None:
        m = match_place_text_to_registry(self.root, 'Kansas, Topeka')
        self.assertEqual(m['tier'], 'near')
        self.assertEqual(m['place_id'], 'L-baba9801fa')

    def test_near_match_on_expanded_abbreviation(self) -> None:
        m = match_place_text_to_registry(self.root, 'Topeka Co, Kansas')
        self.assertEqual(m['tier'], 'near')
        self.assertEqual(m['place_id'], 'L-baba9801fa')

    def test_genuine_miss_is_none(self) -> None:
        m = match_place_text_to_registry(self.root, 'Wichita, Kansas')
        self.assertIsNone(m['tier'])
        self.assertIsNone(m['place_id'])

    def test_empty_text_is_none(self) -> None:
        m = match_place_text_to_registry(self.root, '   ')
        self.assertIsNone(m['tier'])

    def test_no_registry_at_all_is_none(self) -> None:
        empty_tmp = tempfile.TemporaryDirectory()
        try:
            m = match_place_text_to_registry(Path(empty_tmp.name), 'Topeka, Kansas')
            self.assertIsNone(m['tier'])
            self.assertIsNone(m['place_id'])
        finally:
            empty_tmp.cleanup()

    def test_ambiguous_exact_tie_refuses_to_guess(self) -> None:
        # Two registered places share the same normalized name - a PL002
        # duplicate-name hygiene problem in its own right. Attaching to
        # either would be a coin flip, so this must report no match at all,
        # not silently pick the first one found in the file.
        _write_registry(
            self.root,
            '- id: L-aaaaaaaaaa\n  name: Springfield\n'
            '- id: L-bbbbbbbbbb\n  name: Springfield\n')
        m = match_place_text_to_registry(self.root, 'Springfield')
        self.assertIsNone(m['tier'])
        self.assertIsNone(m['place_id'])

    def test_ambiguous_near_tie_also_refuses_to_guess(self) -> None:
        _write_registry(
            self.root,
            '- id: L-aaaaaaaaaa\n  name: Topeka, Kansas\n'
            '- id: L-bbbbbbbbbb\n  name: Kansas, Topeka\n')
        # Both registered names share one token-set key, so a third string
        # with that same token set has two equally-plausible near matches.
        m = match_place_text_to_registry(self.root, 'Topeka Kansas')
        self.assertIsNone(m['tier'])

    def test_malformed_registry_degrades_to_no_match_not_a_crash(self) -> None:
        _write_registry(self.root, 'not_a_list: true\n')
        m = match_place_text_to_registry(self.root, 'Topeka, Kansas')
        self.assertIsNone(m['tier'])

    def test_place_id_that_is_not_an_l_id_is_ignored(self) -> None:
        # A hand-edited places.yaml with a malformed id: line should not
        # crash the lookup or be treated as a real place.
        _write_registry(self.root, '- id: not-an-id\n  name: Topeka, Kansas\n')
        m = match_place_text_to_registry(self.root, 'Topeka, Kansas')
        self.assertIsNone(m['tier'])


class ClusterKeySharedWithPlacesTests(unittest.TestCase):
    """Pins that the normalization moved out of places.py behaves exactly
    as places.py's own private aliases expect (see tests/test_places.py for
    the clustering-side coverage of the same behavior)."""

    def test_abbreviation_expansion(self) -> None:
        self.assertEqual(expand_place_abbreviations('st mary'), 'street mary')
        self.assertEqual(expand_place_abbreviations('shawnee co'), 'shawnee county')

    def test_cluster_key_ignores_word_order_and_punctuation(self) -> None:
        self.assertEqual(
            place_text_cluster_key('Topeka, Kansas'),
            place_text_cluster_key('Kansas, Topeka'))
        self.assertEqual(
            place_text_cluster_key('St. Mary'),
            place_text_cluster_key('St Mary'))


if __name__ == '__main__':
    unittest.main()
