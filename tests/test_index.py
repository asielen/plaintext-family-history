"""
test_index.py — fha index: hypotheses/search_log parsing and indexing.

Covers the parser that extracts `## Hypotheses` and `## Research Log` entries
from a person research file's markdown body (SPEC §16) and from
notes/research-log.md (SPEC §16, multi-person/locality searches), and the
hooks that insert those rows into the hypotheses/search_log tables consumed
by report.py sections 5 and 7.
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import index


_RESEARCH_MD_WELL_FORMED = '''---
id: P-aaaaaaaaaa
created: 2026-06-12
---

## Research Notes
Some notes.

## Open Questions
*(none yet)*

## Hypotheses

- id: H-1111111111
  hypothesis: "Family arrived by 1869"
  basis: "railroad boom drew settlers"
  verify: "1870 census"
  origin: agent
  status: open

- id: H-2222222222
  hypothesis: "Second guess"
  basis: "weak basis"
  verify: "county land records"
  origin: human
  status: "verified → C-3333333333"

## Research Log

- date: 2026-06-12
  question: "[H-1111111111] Family arrival in town"
  repository: example collection
  collection: "1870 census"
  terms: "Smith, town, 1870"
  result: nil

- date: 2026-06-14
  question: "parentage of Jane Doe"
  repository: example collection
  collection: "vitals"
  terms: "Jane Doe"
  result: "found [S-4444444444]"
'''

_RESEARCH_MD_NO_SECTIONS = '''---
id: P-bbbbbbbbbb
created: 2026-06-12
---

## Research Notes
Nothing else here.
'''

_NOTES_RESEARCH_LOG_MD = '''# Research Log (general)

- date: 2026-06-10
  question: "Hartley surname origin in county records"
  repository: example collection
  collection: "county land records"
  terms: "Hartley, county"
  result: nil

- date: 2026-06-11
  question: "[P-aaaaaaaaaa] specific person mention"
  repository: example collection
  collection: "newspapers"
  terms: "Smith"
  result: nil
'''


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


class ParseMdListBlocksTests(unittest.TestCase):
    def test_well_formed_indented_entries_parse(self) -> None:
        section = index._extract_section_body(_RESEARCH_MD_WELL_FORMED, 'Hypotheses')
        entries = index._parse_md_list_blocks(section)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]['id'], 'H-1111111111')
        self.assertEqual(entries[0]['status'], 'open')
        self.assertIn('railroad', entries[0]['basis'])

    def test_blank_line_terminates_entry(self) -> None:
        section = index._extract_section_body(_RESEARCH_MD_WELL_FORMED, 'Research Log')
        entries = index._parse_md_list_blocks(section)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]['date'], '2026-06-12')
        self.assertEqual(entries[1]['date'], '2026-06-14')

    def test_missing_section_returns_empty(self) -> None:
        section = index._extract_section_body(_RESEARCH_MD_NO_SECTIONS, 'Hypotheses')
        self.assertEqual(section.strip(), '')
        self.assertEqual(index._parse_md_list_blocks(section), [])


class IndexPersonResearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(index._DDL)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_hypotheses_and_search_log_rows_inserted(self) -> None:
        path = self.archive_root / 'people' / 'smith__test_research_P-aaaaaaaaaa.md'
        _write(path, _RESEARCH_MD_WELL_FORMED)

        index._index_person(self.conn, path, self.archive_root)

        hyps = self.conn.execute(
            'SELECT * FROM hypotheses ORDER BY id'
        ).fetchall()
        self.assertEqual(len(hyps), 2)
        self.assertEqual(hyps[0]['id'], 'h-1111111111')
        self.assertEqual(hyps[0]['person_id'], 'p-aaaaaaaaaa')
        self.assertEqual(hyps[0]['status'], 'open')
        self.assertIsNone(hyps[0]['verified_claim'])

        # Second hypothesis is verified -> C-3333333333; the C-id must be
        # extracted into verified_claim even though it's embedded in prose.
        self.assertEqual(hyps[1]['id'], 'h-2222222222')
        self.assertEqual(hyps[1]['verified_claim'], 'c-3333333333')

        logs = self.conn.execute(
            'SELECT * FROM search_log ORDER BY date'
        ).fetchall()
        self.assertEqual(len(logs), 2)
        self.assertEqual(logs[0]['person_id'], 'p-aaaaaaaaaa')
        self.assertEqual(logs[0]['result'], 'nil')
        self.assertIsNone(logs[0]['source_id'])

        # "found [S-4444444444]" must yield an extracted source_id.
        self.assertEqual(logs[1]['source_id'], 's-4444444444')
        self.assertIn(
            str(path.relative_to(self.archive_root)),
            (logs[1]['path'], logs[0]['path']),
        )

    def test_research_file_without_sections_inserts_nothing(self) -> None:
        path = self.archive_root / 'people' / 'jones__test_research_P-bbbbbbbbbb.md'
        _write(path, _RESEARCH_MD_NO_SECTIONS)

        index._index_person(self.conn, path, self.archive_root)

        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM hypotheses').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM search_log').fetchone()[0], 0)

    def test_profile_kind_file_does_not_index_hypotheses(self) -> None:
        # A plain profile (not a *_research_* file) should never feed these
        # tables even if its body happens to contain a matching heading.
        path = self.archive_root / 'people' / 'jones__test_P-cccccccccc.md'
        _write(path, _RESEARCH_MD_WELL_FORMED.replace('P-aaaaaaaaaa', 'P-cccccccccc'))

        index._index_person(self.conn, path, self.archive_root)

        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM hypotheses').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM search_log').fetchone()[0], 0)


class IndexNotesResearchLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(index._DDL)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_notes_research_log_indexed_without_person_scoping(self) -> None:
        _write(self.archive_root / 'notes' / 'research-log.md', _NOTES_RESEARCH_LOG_MD)

        index._index_notes(self.conn, self.archive_root)

        logs = self.conn.execute('SELECT * FROM search_log ORDER BY date').fetchall()
        self.assertEqual(len(logs), 2)
        # No explicit person reference -> person_id stays null.
        self.assertIsNone(logs[0]['person_id'])
        # Second entry's question explicitly references a P-id -> picked up.
        self.assertEqual(logs[1]['person_id'], 'p-aaaaaaaaaa')

    def test_absent_research_log_file_does_not_crash(self) -> None:
        (self.archive_root / 'notes').mkdir(parents=True)
        # No research-log.md present.
        index._index_notes(self.conn, self.archive_root)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM search_log').fetchone()[0], 0)


class FullRebuildClearsStaleRowsTests(unittest.TestCase):
    """A full rebuild must not leave stale hypotheses/search_log rows behind
    once an entry is removed from disk — _drop_tables already lists both
    tables, so build_index's drop+rebuild sequence should already cover this;
    this test exercises it end-to-end rather than just trusting the DDL list."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        (self.archive_root / 'people').mkdir(parents=True)
        (self.archive_root / 'sources').mkdir(parents=True)
        (self.archive_root / 'notes').mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_removed_hypothesis_disappears_after_rebuild(self) -> None:
        path = self.archive_root / 'people' / 'smith__test_research_P-aaaaaaaaaa.md'
        _write(path, _RESEARCH_MD_WELL_FORMED)

        index.build_index(self.archive_root, {})

        cache = self.archive_root / '.cache' / 'index.sqlite'
        conn = sqlite3.connect(str(cache))
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM hypotheses').fetchone()[0], 2)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM search_log').fetchone()[0], 2)
        finally:
            conn.close()

        _write(path, _RESEARCH_MD_NO_SECTIONS.replace('P-bbbbbbbbbb', 'P-aaaaaaaaaa'))
        index.build_index(self.archive_root, {})

        conn = sqlite3.connect(str(cache))
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM hypotheses').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM search_log').fetchone()[0], 0)
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
