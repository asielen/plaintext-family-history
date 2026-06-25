"""
test_lint.py — fha lint forgiving-input behavior (PR 05).

Covers the "forgiving, not fussy" rule (AGENTS.md → "Who you serve"): a human
who hand-edits a claim and writes a loose date ("circa 1870", "1870s") or types
a place name into the `place:` field should be understood, not hard-rejected.
Only a genuinely unreadable date is a hard E014 error, and even then with a
plain, example-bearing message.

Like test_report.py, this builds a tiny real archive tree and calls lint's tool
logic directly (`_run_lint_core`) rather than going through the CLI, so the
checks run over a fresh in-memory registry with no prior `fha index`.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import lint
from _lib import normalize_date


_PERSON_MD = '''---
id: P-1111111111
name: Jane Doe
living: false
---

## Biography

Some text.
'''


def _source_md(claim_date: str, place_line: str = '') -> str:
    """A one-claim source whose claim date / place can be parameterised."""
    place = f'  {place_line}\n' if place_line else ''
    return (
        '---\n'
        'id: S-1111111111\n'
        'title: Test source\n'
        'source_type: other\n'
        '---\n\n'
        '## Claims\n\n'
        '```yaml\n'
        '- value: a fact\n'
        '  id: C-1111111111\n'
        '  type: birth\n'
        '  persons: [P-1111111111]\n'
        f'  date: {claim_date}\n'
        f'{place}'
        '  status: suggested\n'
        '```\n'
    )


class NormalizeDateTests(unittest.TestCase):
    """Unit tests for the loose-date translator that the lint checks rely on."""

    def test_approximate_forms_map_to_tilde(self) -> None:
        for raw in ('circa 1870', 'ca 1870', 'c. 1870', 'abt 1870',
                    'about 1870', 'around 1870', '~1870', 'est 1870'):
            self.assertEqual(normalize_date(raw), '1870~', raw)

    def test_decade_forms_map_to_x(self) -> None:
        self.assertEqual(normalize_date('1870s'), '187X')
        self.assertEqual(normalize_date("1870's"), '187X')
        self.assertEqual(normalize_date('187x'), '187X')

    def test_uncertain_and_before_and_interval(self) -> None:
        self.assertEqual(normalize_date('maybe 1900'), '1900?')
        self.assertEqual(normalize_date('before 1920'), '[..1920]')
        self.assertEqual(normalize_date('by 1920'), '[..1920]')
        self.assertEqual(normalize_date('between 1870 and 1875'), '1870/1875')
        self.assertEqual(normalize_date('1870-1875'), '1870/1875')

    def test_month_name_forms(self) -> None:
        self.assertEqual(normalize_date('June 1923'), '1923-06')
        self.assertEqual(normalize_date('Jun. 1923'), '1923-06')
        self.assertEqual(normalize_date('June 14, 1923'), '1923-06-14')
        self.assertEqual(normalize_date('the 14th of June 1923'), '1923-06-14')
        self.assertEqual(normalize_date('about June 1923'), '1923-06~')

    def test_already_canonical_passes_through_unchanged(self) -> None:
        for canon in ('1870', '1870~', '187X', '1850-05', '1850-05-20',
                      '[..1920]', '1871-02/1871-03'):
            self.assertEqual(normalize_date(canon), canon, canon)

    def test_genuinely_unparseable_returns_none(self) -> None:
        for raw in ('the day after never', 'garbage', '', '   ', None):
            self.assertIsNone(normalize_date(raw), repr(raw))


class LintForgivingDateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'people' / 'stubs').mkdir(parents=True)
        (self.root / 'sources' / 'notes').mkdir(parents=True)
        (self.root / 'fha.yaml').write_text('root_person: P-1111111111\n', encoding='utf-8')
        (self.root / 'people' / 'stubs' / 'doe__jane_P-1111111111.md').write_text(
            _PERSON_MD, encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _lint(self, claim_date: str, place_line: str = '') -> list:
        (self.root / 'sources' / 'notes' / 'test_S-1111111111.md').write_text(
            _source_md(claim_date, place_line), encoding='utf-8')
        findings, _ = lint._run_lint_core(self.root, {})
        return findings

    def _codes_for(self, findings, substring: str) -> list:
        return [f.code for f in findings if substring in f.message]

    def test_loose_date_warns_does_not_error(self) -> None:
        findings = self._lint('circa 1870')
        date_findings = [f for f in findings if 'date' in f.message and "'circa 1870'" in f.message]
        self.assertTrue(date_findings)
        self.assertTrue(all(f.severity == 'W' and f.code == 'W109' for f in date_findings))
        self.assertFalse(any(f.code == 'E014' for f in findings))
        # The suggestion names the canonical form and its plain meaning.
        self.assertIn("'1870~'", date_findings[0].message)
        self.assertIn('about 1870', date_findings[0].message)

    def test_decade_date_warns_with_x_form(self) -> None:
        findings = self._lint('1870s')
        msgs = [f.message for f in findings if f.code == 'W109' and "'1870s'" in f.message]
        self.assertTrue(msgs)
        self.assertIn("'187X'", msgs[0])
        self.assertIn('the 1870s', msgs[0])

    def test_broken_date_is_single_plain_error(self) -> None:
        findings = self._lint('the day after never')
        e014 = [f for f in findings if f.code == 'E014']
        self.assertEqual(len(e014), 1)
        msg = e014[0].message
        self.assertIn('the day after never', msg)
        # Plain, example-bearing — no bare jargon, names accepted shapes.
        self.assertIn('1880', msg)
        self.assertNotIn('EDTF', msg)

    def test_freetext_place_warns_points_to_place_text(self) -> None:
        findings = self._lint('1870', place_line='place: Fairview, Ohio')
        place_w = [f for f in findings
                   if f.code == 'W109' and 'Fairview, Ohio' in f.message]
        self.assertTrue(place_w)
        self.assertIn('place_text', place_w[0].message)
        # A typed place name is never a hard error.
        self.assertFalse(any('Fairview, Ohio' in f.message and f.severity == 'E'
                             for f in findings))

    def test_unregistered_l_id_place_still_errors(self) -> None:
        # A well-formed L-id that resolves to nothing is a broken link, not a
        # forgiving case — integrity matters, so it stays E004.
        findings = self._lint('1870', place_line='place: L-cccccccccc')
        e004 = [f for f in findings if f.code == 'E004' and 'L-cccccccccc' in f.message]
        self.assertTrue(e004)


class LintControlledVocabularyTests(unittest.TestCase):
    """E010 confidence presence + E019 status/confidence value checks (SPEC §8.1/§8.5),
    and the SPEC §9 MERGED-INTO tombstone filename grammar."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'people' / 'stubs').mkdir(parents=True)
        (self.root / 'sources' / 'notes').mkdir(parents=True)
        (self.root / 'fha.yaml').write_text('root_person: P-1111111111\n', encoding='utf-8')
        (self.root / 'people' / 'stubs' / 'doe__jane_P-1111111111.md').write_text(
            _PERSON_MD, encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _lint_claim(self, *, status: str = 'accepted', confidence: str | None = 'high') -> list:
        src = (
            '---\nid: S-1111111111\ntitle: Test source\nsource_type: other\n---\n\n'
            '## Claims\n\n```yaml\n'
            '- value: a fact\n  id: C-1111111111\n  type: birth\n'
            '  persons: [P-1111111111]\n  date: 1870\n'
            f'  status: {status}\n'
            + (f'  confidence: {confidence}\n' if confidence is not None else '')
            + ('  reviewed: 2020-01-01\n' if status == 'accepted' else '')
            + '```\n'
        )
        (self.root / 'sources' / 'notes' / 'test_S-1111111111.md').write_text(src, encoding='utf-8')
        findings, _ = lint._run_lint_core(self.root, {})
        return findings

    def test_valid_status_and_confidence_clean(self) -> None:
        findings = self._lint_claim(status='accepted', confidence='high')
        self.assertFalse([f for f in findings if f.code == 'E019'])
        self.assertFalse([f for f in findings if f.code == 'E010' and 'confidence' in f.message])

    def test_missing_confidence_is_e010(self) -> None:
        findings = self._lint_claim(confidence=None)
        self.assertTrue([f for f in findings if f.code == 'E010' and 'confidence' in f.message])

    def test_invalid_status_is_e019(self) -> None:
        findings = self._lint_claim(status='acccepted')
        e019 = [f for f in findings if f.code == 'E019' and 'status' in f.message]
        self.assertTrue(e019)
        self.assertIn('acccepted', e019[0].message)

    def test_invalid_confidence_is_e019(self) -> None:
        findings = self._lint_claim(confidence='very-high')
        self.assertTrue([f for f in findings if f.code == 'E019' and 'confidence' in f.message])

    def test_merged_into_tombstone_filename_matches_grammar(self) -> None:
        self.assertTrue(lint._PERSON_FILENAME_RE.fullmatch(
            'MERGED-INTO-P-de957bcda1__hartley__thomas_P-1234567890'))
        self.assertTrue(lint._PERSON_FILENAME_RE.fullmatch('cole__margaret_P-4d5e6f7g8h'))
        self.assertFalse(lint._PERSON_FILENAME_RE.fullmatch('notaperson'))


if __name__ == '__main__':
    unittest.main()
