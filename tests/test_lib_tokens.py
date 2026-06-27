"""
test_lib_tokens.py — the `[[ ]]` citation-token grammar in _lib.

The token grammar is the single chokepoint every consumer (index, find,
wikitree, site, packet, report, lint) resolves through, so these tests pin the
contract it must hold across the consumer sweep:

  - all five ID types (P/S/C/L/H) resolve to the same lowercased ID whether they
    are written `[[ID]]`, `[[ID|display]]`, `[[ID#fragment]]`, or legacy `[ID]`;
  - a `|display` alias and an Obsidian `#fragment` are surfaced separately and
    NEVER change the resolved ID;
  - `extract_token_ids` yields each token's ID once, in document order, whatever
    the bracket count or trimmings;
  - the historical `TOKEN_RE.findall(...)` / `m.group(1)` shape is preserved (one
    capturing group, the ID) so the unswept consumers keep working unchanged.

L and H are tested explicitly because they are rare in prose and therefore the
two it is easiest to silently break.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

from _lib import (
    LEGACY_TOKEN_RE,
    TOKEN_RE,
    extract_token_ids,
    extract_tokens,
)


class TokenGrammarTests(unittest.TestCase):

    def test_all_five_types_canonical_double_bracket(self):
        """[[P]], [[S]], [[C]], [[L]], [[H]] each resolve to their lowercased ID."""
        cases = {
            '[[P-aaaaaaaaaa]]': 'p-aaaaaaaaaa',
            '[[S-bbbbbbbbbb]]': 's-bbbbbbbbbb',
            '[[C-cccccccccc]]': 'c-cccccccccc',
            '[[L-dddddddddd]]': 'l-dddddddddd',
            '[[H-eeeeeeeeee]]': 'h-eeeeeeeeee',
        }
        for token, expected in cases.items():
            self.assertEqual(extract_token_ids(token), [expected], token)

    def test_display_alias_does_not_change_id(self):
        toks = extract_tokens('[[P-aaaaaaaaaa|Margaret Cole]]')
        self.assertEqual(len(toks), 1)
        cid, display, fragment, _span = toks[0]
        self.assertEqual(cid, 'p-aaaaaaaaaa')
        self.assertEqual(display, 'Margaret Cole')
        self.assertIsNone(fragment)

    def test_heading_fragment_is_parse_only(self):
        toks = extract_tokens('[[S-bbbbbbbbbb#Claims]]')
        cid, display, fragment, _span = toks[0]
        self.assertEqual(cid, 's-bbbbbbbbbb')
        self.assertIsNone(display)
        self.assertEqual(fragment, 'Claims')
        # The resolved ID list never carries the fragment.
        self.assertEqual(extract_token_ids('[[S-bbbbbbbbbb#Claims]]'), ['s-bbbbbbbbbb'])

    def test_block_fragment_with_display(self):
        toks = extract_tokens('[[C-cccccccccc#^x|note]]')
        cid, display, fragment, _span = toks[0]
        self.assertEqual(cid, 'c-cccccccccc')
        self.assertEqual(display, 'note')
        self.assertEqual(fragment, '^x')

    def test_legacy_single_bracket_still_resolves(self):
        self.assertEqual(extract_token_ids('[S-bbbbbbbbbb]'), ['s-bbbbbbbbbb'])
        toks = extract_tokens('[L-dddddddddd]')
        cid, display, fragment, _span = toks[0]
        self.assertEqual(cid, 'l-dddddddddd')
        self.assertIsNone(display)
        self.assertIsNone(fragment)

    def test_case_insensitive_ids_lowercased(self):
        self.assertEqual(extract_token_ids('[[H-EEEEEEEEEE]]'), ['h-eeeeeeeeee'])
        self.assertEqual(extract_token_ids('[s-BBBBBBBBBB]'), ['s-bbbbbbbbbb'])

    def test_double_bracket_not_double_counted(self):
        """[[S-…]] is one token, not also the inner [S-…]."""
        self.assertEqual(extract_token_ids('[[S-bbbbbbbbbb]]'), ['s-bbbbbbbbbb'])

    def test_mixed_paragraph_yields_each_id_once(self):
        para = (
            'Margaret [[P-aaaaaaaaaa|Margaret Cole]] appears on the census '
            '[[S-bbbbbbbbbb#Claims]], corroborated by [[C-cccccccccc#^x|a note]], '
            'in [[L-dddddddddd|Fairview]]; the household legend is [[H-eeeeeeeeee]], '
            'and a hand-typed legacy cite [S-ffffffffff] survives.'
        )
        self.assertEqual(
            extract_token_ids(para),
            [
                'p-aaaaaaaaaa',
                's-bbbbbbbbbb',
                'c-cccccccccc',
                'l-dddddddddd',
                'h-eeeeeeeeee',
                's-ffffffffff',
            ],
        )

    def test_spans_round_trip_to_source_text(self):
        text = 'see [[S-bbbbbbbbbb|the census]] now'
        (_cid, display, _frag, (start, end)), = extract_tokens(text)
        self.assertEqual(text[start:end], '[[S-bbbbbbbbbb|the census]]')
        self.assertEqual(display, 'the census')

    def test_display_whitespace_is_trimmed(self):
        (_cid, display, _frag, _span), = extract_tokens('[[P-aaaaaaaaaa|  Margaret  ]]')
        self.assertEqual(display, 'Margaret')

    def test_token_re_single_group_contract(self):
        """findall / group(1) still yield the bare ID — the unswept-consumer shape."""
        text = '[[S-bbbbbbbbbb|Display]] and [P-aaaaaaaaaa] and [[H-eeeeeeeeee#h]]'
        self.assertEqual(
            TOKEN_RE.findall(text),
            ['S-bbbbbbbbbb', 'P-aaaaaaaaaa', 'H-eeeeeeeeee'],
        )
        first = TOKEN_RE.search('[[L-dddddddddd|Fairview]]')
        self.assertEqual(first.group(1), 'L-dddddddddd')

    def test_legacy_re_isolates_single_brackets_only(self):
        """LEGACY_TOKEN_RE finds [ID] but never the inner brackets of [[ID]]."""
        text = 'canonical [[S-bbbbbbbbbb]] beside legacy [P-aaaaaaaaaa]'
        self.assertEqual(LEGACY_TOKEN_RE.findall(text), ['P-aaaaaaaaaa'])

    def test_non_ids_are_not_tokens(self):
        # A bracketed word that is not a valid ID is not a citation token.
        self.assertEqual(extract_token_ids('[[not-an-id]] and [todo]'), [])


if __name__ == '__main__':
    unittest.main()
