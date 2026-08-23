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

Also covers `read_places_registry`'s `(rows, error)` contract (Codex review,
PR #150): a missing file degrades to an empty registry with `error=None`
(the ordinary case), while a file that EXISTS but is unparseable - bad YAML,
a non-list top level - reports a distinguishable `error` string rather than
looking identical to "the registry is empty" or "genuinely no match". A
stray non-mapping row inside an otherwise-valid list is not a registry-level
error and stays silent, same as before. The write path this backs
(`match_place_text_to_registry`, forwarding `registry_error`) must never
fail a claim mint/edit because the registry file happens to be malformed or
absent - only warn honestly about which of the two happened.
"""

import sys
import tempfile
import unittest
from pathlib import Path

import yaml

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
        rows, error = read_places_registry(self.root)
        self.assertEqual(rows, [])
        self.assertIsNone(error)

    def test_unparseable_yaml_reports_a_distinguishable_error(self) -> None:
        # A malformed-but-EXISTING places.yaml (e.g. mid hand-edit) must not
        # look identical to a missing/empty registry (Codex review, PR #150) -
        # rows stays [] either way, but error is now set so the write path
        # can tell the two apart and say WHY nothing matched.
        _write_registry(self.root, '- id: L-aaaaaaaaaa\n  name: [unterminated\n')
        rows, error = read_places_registry(self.root)
        self.assertEqual(rows, [])
        self.assertIsNotNone(error)
        self.assertIn('YAML', error)

    def test_non_list_top_level_reports_a_distinguishable_error(self) -> None:
        _write_registry(self.root, 'not_a_list: true\n')
        rows, error = read_places_registry(self.root)
        self.assertEqual(rows, [])
        self.assertIsNotNone(error)

    def test_comment_only_seed_file_is_a_valid_empty_registry_not_malformed(self) -> None:
        # Codex review, PR #150 follow-up: archive-template/places/places.yaml
        # - the file every freshly-installed archive starts with - is ALL
        # comments (SPEC §15's seed state). `yaml.safe_load` on comment-only
        # text returns None, not [] or a list, so the non-list rejection
        # above used to misclassify this shipped, valid, empty-to-start seed
        # file as a malformed registry - `fha claim new --place-text` on a
        # brand-new archive, before its first place is ever registered,
        # would then emit a bogus "malformed registry" repair warning on
        # totally correct data. `data is None` must degrade to an ordinary
        # empty registry (error stays None), the same as a missing file,
        # BEFORE the non-list check runs.
        seed_path = ROOT / 'archive-template' / 'places' / 'places.yaml'
        seed_text = seed_path.read_text(encoding='utf-8')
        self.assertIsNone(yaml.safe_load(seed_text))   # pin the assumption this guards
        _write_registry(self.root, seed_text)
        rows, error = read_places_registry(self.root)
        self.assertEqual(rows, [])
        self.assertIsNone(error)

    def test_stray_non_mapping_row_is_skipped_not_fatal(self) -> None:
        _write_registry(
            self.root,
            '- id: L-aaaaaaaaaa\n  name: Topeka\n- just a string\n- id: L-bbbbbbbbbb\n  name: Wichita\n')
        rows, error = read_places_registry(self.root)
        self.assertEqual([p['id'] for p in rows], ['L-aaaaaaaaaa', 'L-bbbbbbbbbb'])
        self.assertIsNone(error)   # the file itself parsed fine - one entry was just junk

    def test_row_with_no_id_is_skipped(self) -> None:
        _write_registry(self.root, '- name: No id here\n')
        rows, error = read_places_registry(self.root)
        self.assertEqual(rows, [])
        self.assertIsNone(error)


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
        self.assertIsNone(m['registry_error'])   # a real miss, not a broken registry

    def test_empty_text_is_none(self) -> None:
        m = match_place_text_to_registry(self.root, '   ')
        self.assertIsNone(m['tier'])
        self.assertIsNone(m['registry_error'])

    def test_no_registry_at_all_is_none(self) -> None:
        empty_tmp = tempfile.TemporaryDirectory()
        try:
            m = match_place_text_to_registry(Path(empty_tmp.name), 'Topeka, Kansas')
            self.assertIsNone(m['tier'])
            self.assertIsNone(m['place_id'])
            self.assertIsNone(m['registry_error'])   # missing file is a normal empty registry
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
        # PR #150 review: the mint/edit must still succeed with no match
        # (never a hard refusal) - but the reason now travels with the
        # result instead of looking like an ordinary miss.
        self.assertIsNotNone(m['registry_error'])

    def test_unreadable_yaml_registry_error_is_a_plain_repair_pointer(self) -> None:
        _write_registry(self.root, '- id: L-aaaaaaaaaa\n  name: [unterminated\n')
        m = match_place_text_to_registry(self.root, 'Topeka, Kansas')
        self.assertIsNone(m['tier'])
        self.assertIsNotNone(m['registry_error'])
        self.assertIn('YAML', m['registry_error'])

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
