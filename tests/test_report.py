"""
test_report.py - fha report (BUILD.md M5.1-M5.3).

Unlike xref/cooccur's synthetic-sqlite-index fixtures, `fha report` rebuilds
the index from on-disk record files (it calls `index.build_index` and
`lint._run_lint_core` directly - BUILD.md M5.1's "call tool logic directly"
design), so the fixture here is a tiny real archive tree rather than a
hand-built .cache/index.sqlite.
"""

import datetime
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
        # claim. The open question only asks "When was X born?" - it names
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
            '- Test Person [P-aaaaaaaaaa] - County probate index: worth re-running (stale nil search)',
            lines,
        )
        self.assertIn(
            '- Test Person [P-aaaaaaaaaa] - Newspaper archive: already searched 2020-01-01',
            lines,
        )

    def test_search_log_calls_out_recent_unreconciled_captures(self) -> None:
        report.run_report(self.archive_root, {}, full=True)

        db_path = self.archive_root / '.cache' / 'index.sqlite'
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            today = datetime.date.today().isoformat()
            conn.execute(
                "INSERT INTO search_log(date, person_id, question, repository, result) "
                "VALUES (?, NULL, ?, ?, ?)",
                (today, 'Captured page', 'site.test', 'staged inbox/page.notes.md'),
            )
            stale = (datetime.date.today() - datetime.timedelta(days=400)).isoformat()
            conn.execute(
                "INSERT INTO search_log(date, person_id, question, repository, result) "
                "VALUES (?, NULL, ?, ?, ?)",
                (stale, 'Old captured page', 'old.test', 'staged inbox/old.notes.md'),
            )
            conn.commit()

            lines = report._section_search_log(conn, {'vitals_gap_person_ids': []})
        finally:
            conn.close()

        self.assertIn('Recently captured (not yet linked to a person):', lines)
        self.assertTrue(any('Captured page' in line for line in lines))
        self.assertFalse(any('Old captured page' in line for line in lines))

    def test_search_log_excludes_general_research_log_entries(self) -> None:
        # notes/research-log.md (SPEC §16) also logs person_id IS NULL rows for
        # general/locality searches - those aren't `fha capture` rows and must
        # not be mislabeled as "Recently captured".
        report.run_report(self.archive_root, {}, full=True)

        db_path = self.archive_root / '.cache' / 'index.sqlite'
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            today = datetime.date.today().isoformat()
            conn.execute(
                "INSERT INTO search_log(date, person_id, question, repository, result, path) "
                "VALUES (?, NULL, ?, ?, ?, ?)",
                (today, 'County land records', 'site.test', 'nil', 'notes/research-log.md'),
            )
            conn.commit()

            lines = report._section_search_log(conn, {'vitals_gap_person_ids': []})
        finally:
            conn.close()

        self.assertFalse(any('County land records' in line for line in lines))


class ReportRootGuardTests(unittest.TestCase):
    """`fha report --root <non-archive>` must refuse (exit 3) and create
    NOTHING (round-2 finding 10). Empirically, before the shared
    resolve_root_arg guard: exit 0, a healthy-empty report printed, and a
    .cache minted inside whatever folder the typo named - a permanently
    "successful" empty archive anywhere on disk."""

    def test_non_archive_root_refused_and_creates_nothing(self) -> None:
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from _lib import EXIT_FAILURE
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = report._standalone_main(['--root', tmp])
            self.assertEqual(rc, EXIT_FAILURE)
            # The empirical heart of the finding: zero files created.
            self.assertEqual(list(Path(tmp).iterdir()), [])
            self.assertIn('does not look like an archive', err.getvalue())
            self.assertIn('fha report', err.getvalue())

    def test_root_with_fha_yaml_still_reports(self) -> None:
        import io
        from contextlib import redirect_stderr, redirect_stdout
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                rc = report._standalone_main(['--root', tmp, '--full'])
            self.assertIn(rc, (0, 1, 2))     # lint-driven, never the refusal 3
            self.assertIn('# fha report', out.getvalue())


_PLACES_GOOD_COORDS = (
    '- id: L-1111111111\n  name: Millbrook\n  coords: [41.786, -73.694]\n'
)
_PLACES_BAD_COORDS = (
    '- id: L-1111111111\n  name: Millbrook\n  coords: "41.786, -73.694"\n'
)


class ReportArchiveNotesTests(unittest.TestCase):
    """Round-2 finding 16: report used to discard build_index's Result, so
    the coord warnings that ride ONLY on that Result (build collects them
    for the front door to render) were invisible on the session-start path.
    run_report now surfaces them as an archive-notes block near the top of
    the markdown and as result.messages - and, per report's documented
    exit-code contract, they stay lint-driven (printed, not exit-changing)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        (self.archive_root / 'people').mkdir()
        (self.archive_root / 'places').mkdir()
        (self.archive_root / 'people' / 'test__person_P-aaaaaaaaaa.md').write_text(
            _PERSON_MD, encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, places_yaml: str):
        (self.archive_root / 'places' / 'places.yaml').write_text(
            places_yaml, encoding='utf-8')
        return report.run_report(self.archive_root, {}, full=True)

    def test_coord_warning_lands_in_markdown_and_messages(self) -> None:
        result = self._run(_PLACES_BAD_COORDS)
        md = result['markdown']
        self.assertIn('**Archive notes from this refresh:**', md)
        self.assertIn('Millbrook', md)
        self.assertIn('coordinate pair', md)
        # Near the top: before the first section heading, where the human
        # actually looks at session start.
        self.assertLess(md.index('Archive notes'), md.index('## 0.'))
        # Structured mirror for headless consumers.
        self.assertTrue(result.messages)
        self.assertIn('Millbrook', result.messages[0].text)

    def test_clean_coords_render_no_notes_block(self) -> None:
        result = self._run(_PLACES_GOOD_COORDS)
        self.assertNotIn('Archive notes', result['markdown'])
        self.assertEqual(result.messages, [])

    def test_warnings_do_not_change_the_lint_driven_exit_code(self) -> None:
        # Same archive, only the coords line differs: the exit code must not
        # move (report's contract is the lint verdict; the note is printed).
        clean_rc = self._run(_PLACES_GOOD_COORDS).exit_code
        noted_rc = self._run(_PLACES_BAD_COORDS).exit_code
        self.assertEqual(noted_rc, clean_rc)

    def test_section_filtered_run_still_shows_notes(self) -> None:
        # Narrowing the view must never hide that a line was skipped.
        (self.archive_root / 'places' / 'places.yaml').write_text(
            _PLACES_BAD_COORDS, encoding='utf-8')
        result = report.run_report(
            self.archive_root, {}, full=True, section='review-queue')
        self.assertIn('Archive notes', result['markdown'])


if __name__ == '__main__':
    unittest.main()
