"""
test_relationship_subtypes.py - relationship claims and entries that read
wrong to the owner even though every line is legal YAML (SPEC §8.2, §8.3, §9;
lint W158, W159, W160, and W115's nature message).

Covers:
  * W158: a role, record or kinship word in a relationship claim's subtype,
    on any status, while the SPEC §8.2 natures (including the ones that
    double as roles, like `employer`) and other free text stay quiet;
  * `half` is a sibling nature and `half-sibling` reads as the same word, so
    an entry and a claim written the two ways agree;
  * W159: an accepted relationship claim naming a parent and no child, a
    child and no parent, or one person alone as spouse or sibling;
  * W115 points at the claim, not the record, when the claim's word is one
    W158 reports;
  * W160 and `fha lint --fix-duplicates`: a bare entry beside a sourced one
    for the same tie is reported and removed (dry-run first); an entry with a
    note, another key or a comment is reported for removal by hand and never
    touched; a guess or an entry of another nature is not a duplicate; the
    needs-sourcing list leaves the duplicate to W160.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))

import lint
from _lib import (
    claim_relationship_nature, drop_claimless_duplicates,
    relationship_entry_nature, subtype_misuse,
)
from test_lint_relationships import (
    CHILD, CLAIM, PARENT, SOURCE, _build, _codes, _person, _rel_source,
)

OTHER = 'P-cccccccccc'


def _claim_source(claim_body: str, sid: str = SOURCE) -> tuple[str, str]:
    """A source holding one hand-written claim (YAML lines, list-item first)."""
    text = ('---\n'
            f'id: {sid}\n'
            'title: Claim source\n'
            'source_type: other\n'
            '---\n\n'
            '## Claims\n\n```yaml\n'
            f'{claim_body}'
            '```\n')
    return f'sources/notes/{sid.lower()}.md', text


def _rel_claim(roles: str, *, subtype: str = '', status: str = 'accepted',
               persons: str = f'[{CHILD}, {PARENT}]') -> str:
    lines = ['- value: "a tie"', f'  id: {CLAIM}', '  type: relationship']
    if subtype:
        lines.append(f'  subtype: {subtype}')
    lines += [f'  persons: {persons}', '  roles:']
    lines += [f'    {r}' for r in roles.strip().split('\n')]
    lines += [f'  status: {status}', '  reviewed: 2026-01-01', '  notes: test.']
    return '\n'.join(lines) + '\n'


def _people(child_rel: str = '', parent_rel: str = '') -> dict[str, str]:
    c_rel, c_text = _person(CHILD, 'kid', 'ann', relationships=child_rel)
    p_rel, p_text = _person(PARENT, 'kid', 'bob', relationships=parent_rel)
    o_rel, o_text = _person(OTHER, 'kid', 'cara')
    return {c_rel: c_text, p_rel: p_text, o_rel: o_text}


def _lint(files: dict[str, str]):
    findings, _registry = lint._run_lint_core(_build(files), {})
    return findings


class SubtypeMisuseWordsTests(unittest.TestCase):
    def test_role_record_and_kinship_words_are_named(self):
        for word, kind in (('mother', 'role'), ('Child', 'role'), ('spouse', 'role'),
                           ('witness', 'role'), ('sibling', 'role'),
                           ('household_member', 'role'), ('household member', 'role'),
                           ('survived-by', 'record'), ('Survived by', 'record'),
                           ('nickname', 'record'),
                           ('cousin', 'kinship'), ('uncle-nephew', 'kinship'),
                           ('grandparent-grandchild', 'kinship'),
                           ('sibling-in-law', 'kinship'), ('extended-family', 'kinship')):
            self.assertEqual(subtype_misuse(word), kind, word)

    def test_natures_and_free_text_are_left_alone(self):
        for word in ('biological', 'adoptive', 'half', 'half-sibling', 'employer',
                     'friend', 'neighbor', 'enslaver', 'member-of', 'godparent',
                     'common-law', '', None):
            self.assertIsNone(subtype_misuse(word), word)

    def test_half_sibling_reads_as_half(self):
        self.assertEqual(relationship_entry_nature({'type': 'sibling',
                                                    'subtype': 'Half-Sibling'}), 'half')
        self.assertEqual(claim_relationship_nature({'subtype': 'half-sibling'}), 'half')


class W158Tests(unittest.TestCase):
    def test_role_word_subtype_is_reported_on_the_claim_whatever_its_status(self):
        for status in ('accepted', 'suggested'):
            rel, text = _claim_source(_rel_claim(
                f'child: {CHILD}\nparent: {PARENT}', subtype='mother', status=status))
            (w,) = _codes(_lint({**_people(), rel: text}), 'W158')
            self.assertTrue(str(w.path).endswith(rel.split('/')[-1]))
            self.assertIn('`subtype: mother`', w.message)
            self.assertIn('roles:', w.message)

    def test_nature_subtype_is_quiet(self):
        rel, text = _claim_source(_rel_claim(
            f'child: {CHILD}\nparent: {PARENT}', subtype='adoptive'))
        self.assertEqual(_codes(_lint({**_people(), rel: text}), 'W158'), [])

    def test_kinship_word_message_names_a_descriptive_role(self):
        rel, text = _claim_source(_rel_claim(
            f'grandparent: {PARENT}\ngrandchild: {CHILD}', subtype='grandparent-grandchild'))
        (w,) = _codes(_lint({**_people(), rel: text}), 'W158')
        self.assertIn('`grandchild:`', w.message)


class W159Tests(unittest.TestCase):
    def _w159(self, roles: str, **kw):
        rel, text = _claim_source(_rel_claim(roles, **kw))
        return _codes(_lint({**_people(), rel: text}), 'W159')

    def test_a_parent_with_no_child(self):
        (w,) = self._w159(f'mother: {PARENT}')
        self.assertIn('names a parent (under `mother:`) and no child', w.message)

    def test_a_child_with_no_parent(self):
        (w,) = self._w159(f'child: {CHILD}')
        self.assertIn('no parent', w.message)

    def test_one_spouse_or_one_sibling(self):
        (w,) = self._w159(f'spouse: [{PARENT}]')
        self.assertIn('`spouse: [p-…, p-…]`', w.message)
        (w,) = self._w159(f'sibling: [{CHILD}]', subtype='half')
        self.assertIn('`sibling:`', w.message)

    def test_whole_maps_and_unaccepted_claims_are_quiet(self):
        self.assertEqual(self._w159(f'child: {CHILD}\nfather: {PARENT}'), [])
        self.assertEqual(self._w159(f'sibling: [{CHILD}, {PARENT}]', subtype='half'), [])
        self.assertEqual(self._w159(f'spouse: [{CHILD}, {PARENT}]'), [])
        self.assertEqual(self._w159(f'mother: {PARENT}', status='suggested'), [])

    def test_a_misspelt_key_is_left_to_w126(self):
        rel, text = _claim_source(_rel_claim(f'child: {CHILD}\nparnet: {PARENT}'))
        findings = _lint({**_people(), rel: text})
        self.assertEqual(_codes(findings, 'W159'), [])
        self.assertTrue(_codes(findings, 'W126'))


class W115NatureMessageTests(unittest.TestCase):
    def _entry(self, subtype: str) -> str:
        return ('relationships:\n'
                f'  - to: "[[{PARENT}|Bob Kid]]"\n'
                '    type: parent\n'
                f'    subtype: {subtype}\n'
                f'    claims: ["[[{CLAIM}]]"]\n')

    def test_a_bad_claim_word_sends_the_owner_to_the_claim(self):
        rel, text = _rel_source(SOURCE, CLAIM, CHILD, PARENT, subtype='survived-by')
        findings = _lint({**_people(child_rel=self._entry('biological')), rel: text})
        msgs = [f.message for f in _codes(findings, 'W115') if 'survived-by' in f.message]
        self.assertEqual(len(msgs), 1)
        self.assertIn('Fix the claim in', msgs[0])
        self.assertIn('W158', msgs[0])

    def test_an_ordinary_mismatch_keeps_its_wording(self):
        rel, text = _rel_source(SOURCE, CLAIM, CHILD, PARENT, subtype='adoptive')
        findings = _lint({**_people(child_rel=self._entry('biological')), rel: text})
        msgs = [f.message for f in _codes(findings, 'W115') if 'adoptive' in f.message]
        self.assertEqual(len(msgs), 1)
        self.assertIn('make the nature match', msgs[0])

    def test_half_and_half_sibling_agree(self):
        claim = _rel_claim(f'sibling: [{CHILD}, {PARENT}]', subtype='half-sibling')
        rel, text = _claim_source(claim)
        entry = ('relationships:\n'
                 f'  - to: "[[{PARENT}|Bob Kid]]"\n'
                 '    type: sibling\n'
                 '    subtype: half\n'
                 f'    claims: ["[[{CLAIM}]]"]\n')
        findings = _lint({**_people(child_rel=entry), rel: text})
        self.assertEqual([f for f in _codes(findings, 'W115') if 'subtype' in f.message], [])
        self.assertEqual(_codes(findings, 'W158'), [])


SOURCED = (f'  - to: "[[{PARENT}|Bob Kid]]"\n'
           '    type: parent\n'
           f'    claims: ["[[{CLAIM}]]"]\n')
BARE = (f'  - to: "[[{PARENT}|Bob Kid]]"\n'
        '    type: parent\n')


def _front(entries: str, newline: str = '\n') -> str:
    text = (f'---\nid: {CHILD}\nname: Ann Kid\nliving: false\ntier: stub\n'
            f'relationships:\n{entries}---\n\n# Ann Kid\n')
    return text.replace('\n', newline)


class DropClaimlessDuplicatesTests(unittest.TestCase):
    def test_bare_entry_goes_and_the_rest_is_kept_byte_for_byte(self):
        for entries in (BARE + SOURCED, SOURCED + BARE):
            for nl in ('\n', '\r\n'):
                new, dropped, by_hand = drop_claimless_duplicates(_front(entries, nl))
                self.assertEqual((len(dropped), by_hand), (1, []))
                self.assertEqual(new, _front(SOURCED, nl))

    def test_an_entry_with_a_note_another_key_or_a_comment_is_never_removed(self):
        for extra in ('    note: Aunt May says so\n', '    since: 1920\n',
                      '    # from the family bible\n'):
            text = _front(SOURCED + BARE + extra)
            new, dropped, by_hand = drop_claimless_duplicates(text)
            self.assertEqual((new, dropped, len(by_hand)), (text, [], 1), extra)
        commented = BARE.replace('type: parent', 'type: parent   # Grandma said')
        text = _front(SOURCED + commented)
        self.assertEqual(drop_claimless_duplicates(text)[0], text)

    def test_a_guess_or_another_nature_is_not_a_duplicate(self):
        for other in (BARE + '    status: hypothesis\n', BARE + '    subtype: adoptive\n'):
            text = _front(SOURCED + other)
            self.assertEqual(drop_claimless_duplicates(text), (text, [], []))

    def test_a_quoted_hash_is_not_a_comment(self):
        quoted = BARE.replace('Bob Kid', 'Bob #2 Kid')
        new, dropped, _ = drop_claimless_duplicates(_front(SOURCED + quoted))
        self.assertEqual(len(dropped), 1)


class W160Tests(unittest.TestCase):
    def _archive(self, entries: str):
        rel, text = _rel_source(SOURCE, CLAIM, CHILD, PARENT)
        c_rel, _ = _person(CHILD, 'kid', 'ann')
        files = _people()
        files[c_rel] = _front(entries)
        files[rel] = text
        return _build(files), c_rel

    def test_reported_then_removed_after_a_dry_run(self):
        root, c_rel = self._archive(SOURCED + BARE)
        findings, _ = lint._run_lint_core(root, {})
        (w,) = _codes(findings, 'W160')
        self.assertIn('--fix-duplicates', w.message)
        before = (root / c_rel).read_bytes()
        r = lint.run_lint(root, {}, fix_duplicates=True, dry_run=True)
        self.assertEqual((root / c_rel).read_bytes(), before)
        self.assertTrue(any('dry-run: would remove' in p for p in r.data['progress']))
        r = lint.run_lint(root, {}, fix_duplicates=True)
        self.assertTrue(any(p.startswith('Removed the duplicate parent entry')
                            for p in r.data['progress']))
        self.assertEqual((root / c_rel).read_text(encoding='utf-8'), _front(SOURCED))
        findings, _ = lint._run_lint_core(root, {})
        self.assertEqual(_codes(findings, 'W160'), [])

    def test_an_entry_holding_a_note_is_reported_for_removal_by_hand(self):
        root, c_rel = self._archive(SOURCED + BARE + '    note: Aunt May says so\n')
        findings, _ = lint._run_lint_core(root, {})
        (w,) = _codes(findings, 'W160')
        self.assertIn('remove it by hand', w.message)
        self.assertNotIn('--fix-duplicates', w.message)
        before = (root / c_rel).read_bytes()
        lint.run_lint(root, {}, fix_duplicates=True)
        self.assertEqual((root / c_rel).read_bytes(), before)

    def test_the_needs_sourcing_list_leaves_the_duplicate_to_w160(self):
        root, _ = self._archive(SOURCED + BARE)
        r = lint.run_lint(root, {})
        self.assertFalse([b for b in r.data['backlog'] if 'Bob Kid' in b])

    def test_a_guess_beside_the_sourced_entry_is_w155_not_w160(self):
        root, _ = self._archive(SOURCED + BARE + '    status: hypothesis\n')
        findings, _ = lint._run_lint_core(root, {})
        self.assertEqual(_codes(findings, 'W160'), [])
        self.assertTrue(_codes(findings, 'W155'))


if __name__ == '__main__':
    unittest.main()
