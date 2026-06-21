"""
test_report.py — fha report (BUILD.md M5.1-M5.3).

Unlike xref/cooccur's synthetic-sqlite-index fixtures, `fha report` rebuilds
the index from on-disk record files (it calls `index.build_index` and
`lint._run_lint_core` directly — BUILD.md M5.1's "call tool logic directly"
design), so the fixture here is a tiny real archive tree rather than a
hand-built .cache/index.sqlite.
"""

import sys
import tempfile
import unittest
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import report


_PERSON_MD = '''---
id: P-aaaaaaaaaa
name: Test Person
living: true
tier: curated
no_known_marriages: true
---

## Biography

Some text about Test Person.
'''

_SOURCE_ONE_NEEDS_REVIEW = '''---
id: S-1111111111
title: Source One
source_type: vital-record
---

## Claims
```yaml
- id: C-1111111111
  type: birth
  persons: [P-aaaaaaaaaa]
  value: Born 1900
  status: needs-review
```
'''

_SOURCE_ONE_ACCEPTED = '''---
id: S-1111111111
title: Source One
source_type: vital-record
---

## Claims
```yaml
- id: C-1111111111
  type: birth
  persons: [P-aaaaaaaaaa]
  value: Born 1900
  status: accepted
  reviewed: 2026-01-01
```
'''

_SOURCE_TWO_SUGGESTED = '''---
id: S-2222222222
title: Source Two
source_type: newspaper
---

## Claims
```yaml
- id: C-2222222222
  type: occupation
  persons: [P-aaaaaaaaaa]
  value: Worked as a clerk
  status: suggested
```
'''

_QUESTIONS_MD = '''# Open Questions (general)

## Q: When was Test Person born?
- origin: human
- status: open
- refs: [C-1111111111]
- context:
  - (human, 2026-01-01) Birth date still needs confirmation.
'''

_PERSON_NO_MARRIAGES_MD = '''---
id: P-bbbbbbbbbb
name: No Marriages Person
living: false
tier: curated
no_known_marriages: true
---

## Biography

Some text about No Marriages Person.
'''

_SOURCE_VITALS_MD = '''---
id: S-3333333333
title: Source Three
source_type: vital-record
---

## Claims
```yaml
- id: C-3333333333
  type: birth
  persons: [P-bbbbbbbbbb]
  value: Born 1900
  status: accepted
  reviewed: 2026-01-01
- id: C-3333333334
  type: death
  persons: [P-bbbbbbbbbb]
  value: Died 1970
  status: accepted
  reviewed: 2026-01-01
```
'''

_QUESTIONS_NO_MARRIAGES_MD = '''# Open Questions (general)

## Q: Is No Marriages Person fully documented?
- origin: human
- status: open
- refs: [P-bbbbbbbbbb]
- context:
  - (human, 2026-01-01) Check vitals completeness.
'''

_PERSON_PARTIAL_VITALS_MD = '''---
id: P-cccccccccc
name: Partial Birth Person
living: false
tier: curated
no_known_marriages: false
---

## Biography

Some text about Partial Birth Person.
'''

_SOURCE_BIRTH_ONLY_MD = '''---
id: S-4444444444
title: Source Four
source_type: vital-record
---

## Claims
```yaml
- id: C-4444444444
  type: birth
  persons: [P-cccccccccc]
  value: Born 1880
  status: accepted
  reviewed: 2026-01-01
```
'''

_QUESTIONS_PARTIAL_VITALS_MD = '''# Open Questions (general)

## Q: When was Partial Birth Person born?
- origin: human
- status: open
- refs: [P-cccccccccc]
- context:
  - (human, 2026-01-01) Birth date still needs confirmation.
'''


class ReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        (self.archive_root / 'people').mkdir(parents=True)
        (self.archive_root / 'sources').mkdir(parents=True)
        (self.archive_root / 'notes').mkdir(parents=True)

        (self.archive_root / 'people' / 'test__person_P-aaaaaaaaaa.md').write_text(
            _PERSON_MD, encoding='utf-8'
        )
        (self.archive_root / 'sources' / 'sourceone_S-1111111111.md').write_text(
            _SOURCE_ONE_NEEDS_REVIEW, encoding='utf-8'
        )
        (self.archive_root / 'notes' / 'questions.md').write_text(
            _QUESTIONS_MD, encoding='utf-8'
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_first_run_reports_vitals_gap_and_no_baseline_diff(self) -> None:
        result = report.run_report(self.archive_root, {}, full=True)
        self.assertEqual(result['status'], 'ok')
        md = result['markdown']
        self.assertIn('missing vital(s): birth', md)
        self.assertIn('No suggested claims awaiting review.', md)
        self.assertTrue((self.archive_root / '.cache' / 'last_report.json').exists())

    def test_accepted_claim_surfaces_as_discovery_and_closes_vitals_gap(self) -> None:
        report.run_report(self.archive_root, {}, full=True)

        # Source 2 (a suggested claim) lands on disk, and source 1's birth
        # claim flips from needs-review to accepted.
        (self.archive_root / 'sources' / 'sourcetwo_S-2222222222.md').write_text(
            _SOURCE_TWO_SUGGESTED, encoding='utf-8'
        )
        (self.archive_root / 'sources' / 'sourceone_S-1111111111.md').write_text(
            _SOURCE_ONE_ACCEPTED, encoding='utf-8'
        )

        result = report.run_report(self.archive_root, {}, full=False)
        md = result['markdown']

        self.assertIn('Claims newly accepted', md)
        self.assertIn('C-1111111111', md)
        self.assertIn('Profiles newly vital-complete', md)
        self.assertIn('Test Person', md)
        self.assertIn('No vitals gaps for curated persons.', md)
        self.assertIn('Source Two', md)
        self.assertIn('1 suggested claim(s)', md)
        self.assertIn('New sources (1):', md)
        self.assertIn('S-2222222222', md)
        self.assertIn('New claims (1):', md)
        self.assertIn('C-2222222222', md)
        self.assertIn('Changed claims (1):', md)
        # The answerable-questions proposal should cite the now-accepted claim.
        self.assertIn('now accepted', md)
        self.assertIn('answered [S-1111111111]', md)

    def test_unchanged_second_run_has_no_new_discoveries(self) -> None:
        report.run_report(self.archive_root, {}, full=True)
        result = report.run_report(self.archive_root, {}, full=False)
        md = result['markdown']
        self.assertIn('No discoveries since last session.', md)
        self.assertIn('No new sources or persons since last session.', md)

    def test_section_filter_prints_only_that_section(self) -> None:
        result = report.run_report(self.archive_root, {}, full=True, section='review-queue')
        md = result['markdown']
        self.assertIn('## 1. Review queue', md)
        self.assertNotIn('## 0. Discoveries', md)
        self.assertNotIn('## 3. Vitals gaps', md)

    def test_unknown_section_raises(self) -> None:
        with self.assertRaises(ValueError):
            report.run_report(self.archive_root, {}, section='not-a-real-section')

    def test_place_candidates_section_uses_live_places_tool(self) -> None:
        # places.py now exists (BUILD.md M6.2), so the section calls
        # places.run_candidates() instead of printing the deferral stub.
        result = report.run_report(self.archive_root, {}, full=True)
        md = result['markdown']
        self.assertNotIn('BUILD.md M6.2', md)
        self.assertIn('No recurring unlinked place-text or GPS clusters found.', md)

    def test_photo_triage_section_reports_absent_index(self) -> None:
        result = report.run_report(self.archive_root, {}, full=True)
        self.assertIn('Photo index absent', result['markdown'])

    def test_answerable_questions_skips_marriage_for_no_known_marriages_person(self) -> None:
        # lint.py's W101 rule never requires a marriage claim for a person
        # with no_known_marriages: true; the answerable-questions proposal
        # logic must mirror that or it will never propose closure for a
        # person whose vitals are already complete by lint's own standard.
        (self.archive_root / 'people' / 'nomarriages_P-bbbbbbbbbb.md').write_text(
            _PERSON_NO_MARRIAGES_MD, encoding='utf-8'
        )
        (self.archive_root / 'sources' / 'sourcethree_S-3333333333.md').write_text(
            _SOURCE_VITALS_MD, encoding='utf-8'
        )
        (self.archive_root / 'notes' / 'questions.md').write_text(
            _QUESTIONS_NO_MARRIAGES_MD, encoding='utf-8'
        )

        result = report.run_report(self.archive_root, {}, full=True)
        md = result['markdown']

        self.assertIn('Is No Marriages Person fully documented?', md)
        self.assertIn('propose: review', md)
        self.assertIn('No Marriages Person', md)

    def test_answerable_questions_proposes_closure_for_partial_vital_match(self) -> None:
        # Partial Birth Person needs birth + marriage + death (no
        # no_known_marriages, not living) but only has an accepted birth
        # claim. The open question only asks "When was X born?" — it names
        # birth specifically, so a closure proposal must fire on birth alone
        # rather than waiting on the unrelated marriage/death gaps too.
        (self.archive_root / 'people' / 'partialbirth__P-cccccccccc.md').write_text(
            _PERSON_PARTIAL_VITALS_MD, encoding='utf-8'
        )
        (self.archive_root / 'sources' / 'sourcefour_S-4444444444.md').write_text(
            _SOURCE_BIRTH_ONLY_MD, encoding='utf-8'
        )
        (self.archive_root / 'notes' / 'questions.md').write_text(
            _QUESTIONS_MD + _QUESTIONS_PARTIAL_VITALS_MD, encoding='utf-8'
        )

        result = report.run_report(self.archive_root, {}, full=True)
        md = result['markdown']

        self.assertIn('When was Partial Birth Person born?', md)
        self.assertIn('propose: review', md)
        self.assertIn('Partial Birth Person', md)
        # The proposal must cite only the matched vital (birth), not the
        # full needed set (birth, death, marriage).
        self.assertIn('accepted birth claim(s)', md)

    def test_search_log_only_marks_old_nil_searches_stale(self) -> None:
        report.run_report(self.archive_root, {}, full=True)

        db_path = self.archive_root / '.cache' / 'index.sqlite'
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(
                "INSERT INTO search_log(date, person_id, collection, result) VALUES (?,?,?,?)",
                ('2020-01-01', 'p-aaaaaaaaaa', 'County probate index', 'nil'),
            )
            conn.execute(
                "INSERT INTO search_log(date, person_id, collection, result) VALUES (?,?,?,?)",
                ('2020-01-01', 'p-aaaaaaaaaa', 'Newspaper archive', 'found S-1111111111'),
            )
            conn.commit()

            current = {'vitals_gap_person_ids': ['p-aaaaaaaaaa']}
            lines = report._section_search_log(conn, current)
        finally:
            conn.close()

        self.assertIn(
            '- Test Person [P-aaaaaaaaaa] — County probate index: worth re-running (stale nil search)',
            lines,
        )
        self.assertIn(
            '- Test Person [P-aaaaaaaaaa] — Newspaper archive: already searched 2020-01-01',
            lines,
        )


if __name__ == '__main__':
    unittest.main()
