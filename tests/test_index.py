"""
test_index.py - fha index: hypotheses/search_log parsing and indexing.

Covers the parser that extracts `## Hypotheses` and `## Research Log` entries
from a person research file's markdown body (SPEC §16) and from
notes/research-log.md (SPEC §16, multi-person/locality searches), and the
hooks that insert those rows into the hypotheses/search_log tables consumed
by report.py sections 5 and 7.

Also covers two hand-edit hardening contracts:
  - place `coords:` validation (a hand-edited empty/string/dict coords must
    degrade to NULL lat/lon with a warning, never crash the build or silently
    corrupt into lat='3'), and
  - claim persons:/roles:/place resolution through the alias map (TOOLING §3
    E004: `persons: ["[[Sam Rivera]]"]` joins to its person record; an
    unresolved name is an inert note-link, not a garbage row), identical in
    full build and incremental upsert.
"""

import argparse
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import index
from _lib import EXIT_CLEAN, EXIT_FAILURE, EXIT_WARNINGS


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


_GENERATED_TIMELINE = (
    '<!-- GENERATED by fha views timeline on 2026-06-30'
    ' - do not edit; regenerate instead -->\n\n'
    '# Timeline: Marie Hartley\n\n- 1880 - birth: born in Fairview\n'
)

# The SPEC §16 research companion, exactly as `fha person promote` scaffolds it
# (_lib.RESEARCH_TEMPLATE_FALLBACK): an `id:` and a `created:` date, and no
# other person-record field. This is why `id:` cannot be the profile test.
_SCAFFOLDED_RESEARCH = (
    '---\nid: {pid}\ncreated: 2026-06-12\n---\n\n'
    '## Research Notes\n\nWorking notes.\n'
)


def _person_md(pid: str, name: str) -> str:
    """A person record carrying the SPEC §9 required set: id, name, living."""
    return (f'---\nid: {pid}\nname: {name}\nliving: false\nsex: F\n'
            f'tier: curated\n---\n\n# {name}\n\n## Biography\n\nHer life.\n')


class IndexPersonSurnameSuffixTests(unittest.TestCase):
    """Surname derivation is the third site issue #53 named. Index reads
    surname from the §13 filename slug when the file has already been
    renamed (its normal, documented path - `_index_person`'s docstring), so
    fixing the filename at the two writing sites (`_lib.stub_slug_name` /
    `lint._person_filename_parts`) already fixes what the index shows for
    every MINTED record - these tests confirm that.

    They also cover the fallback this fix adds: a hand-authored, not-yet-
    minted file (SPEC §10's legal pre-machine state) has no `__` filename
    slug yet, so `surname` now falls back to splitting `name:` with the same
    suffix-aware rule, rather than staying permanently None until the next
    `fha lint --fix-ids` rename.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(index._DDL)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _index_text(self, filename: str, text: str) -> None:
        path = self.archive_root / 'people' / filename
        _write(path, text)
        index._index_person(self.conn, path, self.archive_root)

    def _surname(self, pid: str) -> str | None:
        row = self.conn.execute(
            'SELECT surname FROM persons WHERE id=?', (pid,)).fetchone()
        return row['surname'] if row else None

    def test_minted_filename_with_suffix_reads_the_correct_surname(self) -> None:
        # The already-fixed filename (dodson__roy_eugene_jr_P-…) is what
        # `fha person new`/`fha stubs`/`--fix-ids` now write for issue #53 -
        # the index must read Dodson from it, not Jr.
        self._index_text(
            'dodson__roy_eugene_jr_P-1111111111.md',
            _person_md('P-1111111111', 'Roy Eugene Dodson Jr'))
        self.assertEqual(self._surname('p-1111111111'), 'Dodson')

    def test_hand_authored_suffixed_name_falls_back_to_the_name_field(self) -> None:
        # No `__` in the stem yet (SPEC §10 pre-machine state) - the fallback
        # must apply the same suffix rule, not read "Jr" as the surname.
        self._index_text(
            'Roy Eugene Dodson Jr.md',
            '---\nid: P-2222222222\nname: Roy Eugene Dodson Jr\nliving: false\n---\n\n'
            '# Roy Eugene Dodson Jr\n')
        self.assertEqual(self._surname('p-2222222222'), 'Dodson')

    def test_hand_authored_plain_name_still_falls_back_correctly(self) -> None:
        self._index_text(
            'Roy Eugene Dodson.md',
            '---\nid: P-3333333333\nname: Roy Eugene Dodson\nliving: false\n---\n\n'
            '# Roy Eugene Dodson\n')
        self.assertEqual(self._surname('p-3333333333'), 'Dodson')

    def test_hand_authored_mononym_stays_surname_less(self) -> None:
        # A single-token name (or a suffix with nothing real to attach to)
        # must NOT get a fabricated surname out of the fallback.
        self._index_text(
            'Cher.md',
            '---\nid: P-4444444444\nname: Cher\nliving: false\n---\n\n# Cher\n')
        self.assertIsNone(self._surname('p-4444444444'))


class IndexPersonKindTests(unittest.TestCase):
    """A person file's kind comes from its CONTENT, with the filename as a hint.

    SPEC §13's person grammar is
    `{primary_sort_name}__{given_names}[_{kind}]_{P-id}.md` and underscores
    inside given names are legal - so the optional kind slot and the last
    given-name segment are one and the same slot, and the grammar cannot
    separate them. `hartley__marie_timeline_P-…` is either a generated timeline
    or the profile of Marie Timeline Hartley. Reading it as a companion writes
    no `persons` row at all, which is silent data loss: the person is gone from
    `fha find`, every view, every count, the tree, the site, GEDCOM, WikiTree
    and every packet, while her file sits untouched on disk. Frontmatter that
    carries the SPEC §9 person fields settles it; a kind-suffixed name with no
    such frontmatter is the generated companion it looks like.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(index._DDL)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _index_text(self, filename: str, text: str) -> Path:
        path = self.archive_root / 'people' / filename
        _write(path, text)
        index._index_person(self.conn, path, self.archive_root)
        return path

    def _index(self, filename: str, pid: str) -> None:
        self._index_text(filename, _person_md(pid, 'Test Person'))

    def _kind_of(self, pid: str) -> str | None:
        row = self.conn.execute(
            'SELECT kind FROM person_files WHERE person_id=?', (pid,)).fetchone()
        return row['kind'] if row else None

    def test_given_name_containing_a_kind_word_is_still_a_profile(self) -> None:
        # A real person whose given names contain a companion kind word was
        # filed as a companion, so no persons row was written at all: absent
        # from `fha find`, from every view, and from every count, with nothing
        # to say why.
        self._index('mcindoe__timeline_marie_P-dddddddddd.md', 'P-dddddddddd')
        row = self.conn.execute(
            'SELECT id FROM persons WHERE id=?', ('p-dddddddddd',)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(self._kind_of('p-dddddddddd'), 'profile')

    def test_generated_companion_is_still_a_companion(self) -> None:
        # The control that stops the content rule over-correcting: a real
        # generated view carries the GENERATED header and no frontmatter at
        # all, so it must stay a companion and must NOT mint a persons row of
        # its own beside the profile it was built from.
        self._index_text('mcindoe__marie_timeline_P-eeeeeeeeee.md', _GENERATED_TIMELINE)
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM persons').fetchone()[0], 0)
        self.assertEqual(self._kind_of('p-eeeeeeeeee'), 'timeline')

    def test_scaffolded_research_companion_is_still_a_companion(self) -> None:
        # The research companion is the reason `id:` cannot be the test: it
        # carries one. Only the other two SPEC §9 required fields (name,
        # living) mark a file as a person record.
        self._index_text('mcindoe__marie_research_P-eeeeeeeeee.md',
                         _SCAFFOLDED_RESEARCH.format(pid='P-eeeeeeeeee'))
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM persons').fetchone()[0], 0)
        self.assertEqual(self._kind_of('p-eeeeeeeeee'), 'research')

    def test_hand_edited_companion_carrying_person_frontmatter_is_a_profile(self) -> None:
        # The inverse error the same rule catches: a human who typed a real
        # person record into a file that happens to carry a companion name is
        # describing a person, and the archive must not swallow it.
        self._index('mcindoe__marie_timeline_P-ffffffffff.md', 'P-ffffffffff')
        self.assertIsNotNone(self.conn.execute(
            'SELECT id FROM persons WHERE id=?', ('p-ffffffffff',)).fetchone())
        self.assertEqual(self._kind_of('p-ffffffffff'), 'profile')

    def test_living_false_alone_still_marks_a_person_record(self) -> None:
        # `living: false` is the commonest value the field takes and is falsy
        # in Python: a truthiness test on the frontmatter would read a
        # long-dead ancestor's record as carrying nothing and lose her.
        self._index_text(
            'mcindoe__marie_timeline_P-gggggggggg.md',
            '---\nid: P-gggggggggg\nliving: false\n---\n\n# Marie\n')
        self.assertIsNotNone(self.conn.execute(
            'SELECT id FROM persons WHERE id=?', ('p-gggggggggg',)).fetchone())
        self.assertEqual(self._kind_of('p-gggggggggg'), 'profile')

    def test_sparse_profile_is_not_demoted(self) -> None:
        # Content only ever promotes a file TO a profile. A stub named as a
        # profile but carrying nothing but its id stays a profile - which is
        # what a stub is (SPEC §9, "frontmatter only, a permanent legitimate
        # state").
        self._index_text('mcindoe__marie_P-hhhhhhhhhh.md',
                         '---\nid: P-hhhhhhhhhh\n---\n\n# Marie\n')
        self.assertIsNotNone(self.conn.execute(
            'SELECT id FROM persons WHERE id=?', ('p-hhhhhhhhhh',)).fetchone())
        self.assertEqual(self._kind_of('p-hhhhhhhhhh'), 'profile')

    def test_person_record_is_never_marked_generated(self) -> None:
        # person_files.generated says "this file is machine output, regenerate
        # it". A hand-authored record whose id has not been minted yet (a legal
        # pre-machine state, SPEC §10) is not machine output.
        self._index_text('mcindoe__marie_timeline_P-jjjjjjjjjj.md',
                         '---\nname: Marie Timeline Mcindoe\nliving: false\n---\n\n# Marie\n')
        row = self.conn.execute(
            'SELECT kind, generated FROM person_files WHERE person_id=?',
            ('p-jjjjjjjjjj',)).fetchone()
        self.assertEqual(row['kind'], 'profile')
        self.assertEqual(row['generated'], 0)


class IndexPersonIdValidationTests(unittest.TestCase):
    """A `persons.id` that is not a valid Crockford ID joins to nothing.

    `claim_persons`, `relationships`, `citations` and every view key off the
    real ID, so a hand-typed `id: P-notanid` waved through by a `p-` prefix
    test produced a row that read as present in `persons` and was absent from
    every query built on it - the same invisibility as no row at all, minus the
    clue. `fha lint` E002 is what tells the human about the typo; the index
    falls back to the filename's ID, which is what the archive's existing
    `[[P-…]]` links already point at.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(index._DDL)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _index_text(self, filename: str, text: str) -> None:
        path = self.archive_root / 'people' / filename
        _write(path, text)
        index._index_person(self.conn, path, self.archive_root)

    def test_malformed_frontmatter_id_is_not_inserted(self) -> None:
        self._index_text(
            'smith__bad_id.md',
            '---\nid: P-notanid\nname: Bad Id Smith\nliving: false\n---\n\n# Bad\n')
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM persons WHERE id='p-notanid'").fetchone()[0], 0)
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM persons').fetchone()[0], 0)
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM person_files').fetchone()[0], 0)

    def test_template_placeholder_id_is_not_inserted(self) -> None:
        # `P-__________` is the shipped fill-me-in value in
        # archive-template/people/_TEMPLATE.person.md. Copied by hand and left
        # unminted it used to become a literal persons.id - and because the
        # upsert is INSERT OR REPLACE, every OTHER unminted copy in the archive
        # overwrote it in turn, so N hand-started people collapsed into one
        # unjoinable row.
        self._index_text(
            'smith__unminted.md',
            '---\nid: P-__________\nname: Unminted Smith\nliving: false\n---\n\n# U\n')
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM persons').fetchone()[0], 0)

    def test_malformed_frontmatter_id_falls_back_to_the_filename_id(self) -> None:
        # The person is not lost over a typo: the filename's ID is the one the
        # rest of the archive already links to.
        self._index_text(
            'smith__bad_P-1234567890.md',
            '---\nid: P-notanid\nname: Bad Id Smith\nliving: false\n---\n\n# Bad\n')
        ids = [r['id'] for r in self.conn.execute('SELECT id FROM persons')]
        self.assertEqual(ids, ['p-1234567890'])

    def test_a_non_md_file_under_people_is_not_a_person_record(self) -> None:
        # SPEC §13 spells every person record `.md`. `parse_filename` reads the
        # companion-kind slot for `.md` only, so any other extension came back
        # with kind=None, which the fallback turned into 'profile': a stray
        # export dropped beside the record minted a nameless persons row - and
        # since the upsert is INSERT OR REPLACE, it overwrote the real person.
        real = self.archive_root / 'people' / 'hartley__thomas_P-de957bcda1.md'
        _write(real, _person_md('P-de957bcda1', 'Thomas Edward Hartley'))
        index._index_person(self.conn, real, self.archive_root)

        stray = self.archive_root / 'people' / 'hartley__thomas_timeline_P-de957bcda1.txt'
        _write(stray, 'a plain-text dump someone left here\n')
        index._index_person(self.conn, stray, self.archive_root)

        rows = self.conn.execute('SELECT name, path FROM persons').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['name'], 'Thomas Edward Hartley')
        self.assertTrue(rows[0]['path'].endswith('.md'))
        paths = [r['path'] for r in self.conn.execute('SELECT path FROM person_files')]
        self.assertEqual(paths, [str(real.relative_to(self.archive_root))])


class PersonNamedLikeACompanionIsFindableTests(unittest.TestCase):
    """End to end: a person whose given names end in a companion kind word is
    in the index and `fha find` locates her.

    The unit tests above pin the row; this pins what the human actually does
    with it. Without the content rule these two women had no `persons` row, so
    `fha find` answered "not found" for a record sitting in the archive.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        folder = self.root / 'people' / '040 Test Couple'
        folder.mkdir(parents=True)
        (self.root / 'fha.yaml').write_text(
            'roots:\n  documents: documents\n', encoding='utf-8')
        _write(folder / 'hartley__marie_timeline_P-3kq9v8x2m1.md',
               _person_md('P-3kq9v8x2m1', 'Marie Timeline Hartley'))
        _write(folder / 'smith__anne_research_P-9x8m4k2q1v.md',
               _person_md('P-9x8m4k2q1v', 'Anne Research Smith'))
        # A curated person with her real generated companions beside her, so
        # the control travels through the full build too.
        _write(folder / 'hartley__thomas_P-de957bcda1.md',
               _person_md('P-de957bcda1', 'Thomas Edward Hartley'))
        _write(folder / 'hartley__thomas_timeline_P-de957bcda1.md', _GENERATED_TIMELINE)
        _write(folder / 'hartley__thomas_research_P-de957bcda1.md',
               _SCAFFOLDED_RESEARCH.format(pid='P-de957bcda1'))
        index.build_index(self.root, {})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _persons(self) -> dict:
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        conn.row_factory = sqlite3.Row
        try:
            return {r['id']: r['name']
                    for r in conn.execute('SELECT id, name FROM persons')}
        finally:
            conn.close()

    def _find(self, query: str) -> str:
        import find as find_mod
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            find_mod.run_find(query, self.root, {})
        return buf.getvalue()

    def test_timeline_named_profile_has_a_persons_row(self) -> None:
        self.assertEqual(self._persons().get('p-3kq9v8x2m1'), 'Marie Timeline Hartley')

    def test_research_named_profile_has_a_persons_row(self) -> None:
        self.assertEqual(self._persons().get('p-9x8m4k2q1v'), 'Anne Research Smith')

    def test_fha_find_locates_the_timeline_named_person(self) -> None:
        self.assertIn('Marie Timeline Hartley', self._find('P-3kq9v8x2m1'))

    def test_fha_find_locates_the_research_named_person(self) -> None:
        self.assertIn('Anne Research Smith', self._find('P-9x8m4k2q1v'))

    def test_generated_companions_still_mint_no_person(self) -> None:
        # Three person files for Thomas, one person. The count is the thing the
        # over-correction would break.
        self.assertEqual(sorted(self._persons()),
                         ['p-3kq9v8x2m1', 'p-9x8m4k2q1v', 'p-de957bcda1'])


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


class IndexCaptureLogTests(unittest.TestCase):
    """`.cache/capture_log.jsonl` rows must re-populate search_log on rebuild
    (a full rebuild drops and recreates the table, so a row `fha capture`
    wrote directly into index.sqlite would otherwise be lost)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(index._DDL)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_capture_log_jsonl_rows_ingested(self) -> None:
        cache = self.archive_root / '.cache'
        cache.mkdir(parents=True)
        (cache / 'capture_log.jsonl').write_text(
            json.dumps({
                'date': '2024-01-01', 'question': 'Captured page',
                'repository': 'site.test', 'collection': '', 'terms': '',
                'result': 'staged inbox/page.notes.md', 'path': 'inbox/page.notes.md',
            }) + '\n',
            encoding='utf-8',
        )

        index._index_capture_log(self.conn, self.archive_root)

        rows = self.conn.execute('SELECT * FROM search_log').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['question'], 'Captured page')
        self.assertEqual(rows[0]['path'], 'inbox/page.notes.md')
        self.assertIsNone(rows[0]['person_id'])
        self.assertIsNone(rows[0]['source_id'])

    def test_absent_capture_log_does_not_crash(self) -> None:
        index._index_capture_log(self.conn, self.archive_root)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM search_log').fetchone()[0], 0)

    def test_malformed_capture_log_line_skipped(self) -> None:
        cache = self.archive_root / '.cache'
        cache.mkdir(parents=True)
        (cache / 'capture_log.jsonl').write_text('not json\n', encoding='utf-8')
        index._index_capture_log(self.conn, self.archive_root)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM search_log').fetchone()[0], 0)


class IndexCitationsPacketOutputTests(unittest.TestCase):
    """fha packet's default out/ dir must not become a citation site, but a
    record tree's own legitimately-named 'out' subdirectory still must."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(index._DDL)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_root_level_out_dir_skipped(self) -> None:
        _write(self.archive_root / 'out' / 'packet_x' / 'profile.md', '[P-aaaaaaaaaa]\n')

        index._index_citations(self.conn, self.archive_root)

        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM citations').fetchone()[0], 0)

    def test_nested_out_directory_elsewhere_still_scanned(self) -> None:
        _write(self.archive_root / 'sources' / 'out' / 'note.md', '[P-aaaaaaaaaa]\n')

        index._index_citations(self.conn, self.archive_root)

        rows = self.conn.execute('SELECT token FROM citations').fetchall()
        self.assertEqual([r['token'] for r in rows], ['p-aaaaaaaaaa'])


class IndexPublicationOkTests(unittest.TestCase):
    """rights.publication_ok must be stored three-state: 1 (true), 0 (explicit
    false), NULL (absent). The shared exporter predicate COALESCE(publication_ok,
    1) = 0 - used by gedcom, wikitree, and site - only redacts on a stored 0, so
    folding an explicit false to NULL (the old behavior) would silently leak a
    source the human marked unpublishable."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(index._DDL)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _index(self, sid: str, rights_block: str) -> object:
        path = self.archive_root / 'sources' / 'other' / f'src_{sid}.md'
        _write(path, f'---\nid: {sid}\ntitle: Test\nsource_type: other\n{rights_block}---\n\n## Claims\n')
        index._index_source(self.conn, path, self.archive_root, {})
        return self.conn.execute(
            'SELECT publication_ok FROM sources WHERE id = ?', (sid.lower(),)
        ).fetchone()['publication_ok']

    def test_explicit_true_stored_as_one(self) -> None:
        self.assertEqual(self._index('S-aaaaaaaaaa', 'rights:\n  publication_ok: true\n'), 1)

    def test_explicit_false_stored_as_zero(self) -> None:
        self.assertEqual(self._index('S-bbbbbbbbbb', 'rights:\n  publication_ok: false\n'), 0)

    def test_absent_rights_stored_as_null(self) -> None:
        self.assertIsNone(self._index('S-cccccccccc', ''))

    def test_rights_without_publication_ok_stored_as_null(self) -> None:
        self.assertIsNone(self._index('S-dddddddddd', 'rights:\n  holder: family collection\n'))

    def test_incremental_upsert_matches_full_rebuild(self) -> None:
        # The three-state mapping must hold on the incremental path too - both
        # build_index and upsert_source go through _index_source, but verify
        # end-to-end that a publication_ok:false source stays 0 after an upsert.
        sid = 'S-eeeeeeeeee'
        path = self.archive_root / 'sources' / 'other' / f'src_{sid}.md'
        _write(path, f'---\nid: {sid}\ntitle: Test\nsource_type: other\nrights:\n  publication_ok: false\n---\n\n## Claims\n')
        index.build_index(self.archive_root, {})
        cache = self.archive_root / '.cache' / 'index.sqlite'
        conn = sqlite3.connect(str(cache))
        try:
            self.assertEqual(
                conn.execute('SELECT publication_ok FROM sources WHERE id=?', (sid.lower(),)).fetchone()[0], 0)
        finally:
            conn.close()
        index.upsert_source(self.archive_root, {}, sid.lower())
        conn = sqlite3.connect(str(cache))
        try:
            self.assertEqual(
                conn.execute('SELECT publication_ok FROM sources WHERE id=?', (sid.lower(),)).fetchone()[0], 0)
        finally:
            conn.close()


class FullRebuildClearsStaleRowsTests(unittest.TestCase):
    """A full rebuild must not leave stale hypotheses/search_log rows behind
    once an entry is removed from disk - _drop_tables already lists both
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

    def test_capture_log_row_survives_full_rebuild(self) -> None:
        # A `fha capture` run writes the row straight into index.sqlite *and*
        # to capture_log.jsonl. Simulate just the jsonl half here (the part
        # that must outlive a rebuild) and confirm build_index's drop+rebuild
        # of search_log re-ingests it rather than losing it.
        cache_dir = self.archive_root / '.cache'
        cache_dir.mkdir(parents=True)
        (cache_dir / 'capture_log.jsonl').write_text(
            json.dumps({
                'date': '2024-01-01', 'question': 'Captured page',
                'repository': 'site.test', 'collection': '', 'terms': '',
                'result': 'staged inbox/page.notes.md', 'path': 'inbox/page.notes.md',
            }) + '\n',
            encoding='utf-8',
        )

        index.build_index(self.archive_root, {})
        index.build_index(self.archive_root, {})  # a second rebuild must not duplicate-lose it

        conn = sqlite3.connect(str(cache_dir / 'index.sqlite'))
        try:
            rows = conn.execute('SELECT question, path FROM search_log').fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 'Captured page')


class PlaceCoordsTests(unittest.TestCase):
    """Hand-edited `coords:` must never kill or corrupt the index (bug: an
    empty `coords:` key crashed every `fha index`/`fha report` with a
    len(None) TypeError; a string value '39.8, -95.6' silently indexed as
    lat='3', lon='9'; a dict raised KeyError). Every bad shape stores NULLs
    plus one warning that names the place and the expected shape, and the
    build completes on the warnings exit path."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'places').mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _build(self, places_yaml: str):
        (self.root / 'places' / 'places.yaml').write_text(places_yaml, encoding='utf-8')
        result = index.build_index(self.root, {})
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute('SELECT id, lat, lon FROM places ORDER BY id').fetchall()
        finally:
            conn.close()
        return result, rows

    def test_valid_coords_index_as_floats(self) -> None:
        result, rows = self._build(
            '- id: L-1111111111\n  name: Millbrook\n  coords: [41.786, -73.694]\n')
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]['lat'], 41.786)
        self.assertAlmostEqual(rows[0]['lon'], -73.694)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.messages, [])

    def test_numeric_string_coords_still_index(self) -> None:
        _, rows = self._build(
            '- id: L-1111111111\n  name: Millbrook\n  coords: ["41.786", "-73.694"]\n')
        self.assertAlmostEqual(rows[0]['lat'], 41.786)
        self.assertAlmostEqual(rows[0]['lon'], -73.694)

    def test_absent_coords_is_silent_null(self) -> None:
        result, rows = self._build('- id: L-1111111111\n  name: Millbrook\n')
        self.assertIsNone(rows[0]['lat'])
        self.assertIsNone(rows[0]['lon'])
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.messages, [])

    def test_place_notes_land_in_text_search(self) -> None:
        # P2 codex finding (round 4, PR #31): text hits come only from
        # notes_fts, so an `fha places note` entry was undiscoverable by
        # search the moment it was written. Each place's notes get an fts
        # row under the registry's own path.
        self._build(
            '- id: L-1111111111\n  name: Millbrook\n'
            '  notes: |\n    Platted by the millwright cooperative in 1858.\n')
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT path FROM notes_fts WHERE notes_fts MATCH 'millwright'").fetchall()
        finally:
            conn.close()
        self.assertEqual([r['path'] for r in rows], ['places/places.yaml'])

    def test_place_note_text_hits_dedupe_to_one_registry_result(self) -> None:
        # Final-review finding (PR #31): every place's notes row shares the
        # path places/places.yaml, and the CLI text search appended each FTS
        # row as its own hit - a word appearing in two places' notes printed
        # the registry twice and then suppressed the honest file-scan hit.
        # One physical file, one hit.
        self._build(
            '- id: L-1111111111\n  name: Millbrook\n'
            '  notes: |\n    Platted by the millwright cooperative in 1858.\n'
            '- id: L-2222222222\n  name: Sawville\n'
            '  notes: |\n    The millwright families moved here in 1870.\n')
        from contextlib import redirect_stdout
        from tools import find as find_mod
        buf = io.StringIO()
        with redirect_stdout(buf):
            find_mod.run_find('millwright', self.root, {}, text_mode=True)
        self.assertEqual(buf.getvalue().count('places/places.yaml'), 1)

    def _assert_bad_shape(self, coords_line: str) -> None:
        result, rows = self._build(
            f'- id: L-1111111111\n  name: Millbrook\n{coords_line}')
        self.assertEqual(len(rows), 1, coords_line)
        self.assertIsNone(rows[0]['lat'], coords_line)
        self.assertIsNone(rows[0]['lon'], coords_line)
        # One warning naming the place and the expected shape reaches the
        # Result, and the build lands on the documented warnings exit (1).
        self.assertEqual(result.exit_code, EXIT_WARNINGS, coords_line)
        warning_texts = [m.text for m in result.messages]
        self.assertEqual(len(warning_texts), 1, coords_line)
        self.assertIn('Millbrook', warning_texts[0])
        self.assertIn('coords: [39.8, -95.6]', warning_texts[0])
        self.assertIn('fha index', warning_texts[0])

    def test_empty_coords_key_warns_and_stores_null(self) -> None:
        self._assert_bad_shape('  coords:\n')

    def test_string_coords_warn_never_corrupt(self) -> None:
        self._assert_bad_shape('  coords: "39.8, -95.6"\n')

    def test_dict_coords_warn(self) -> None:
        self._assert_bad_shape('  coords: {lat: 39.8, lon: -95.6}\n')

    def test_single_entry_coords_warn(self) -> None:
        self._assert_bad_shape('  coords: [39.8]\n')

    def test_non_numeric_pair_warns(self) -> None:
        self._assert_bad_shape('  coords: [north, south]\n')

    def _assert_out_of_range(self, coords_line: str) -> None:
        # Numeric but off the globe (a missing decimal, a swapped pair, or a
        # non-finite value): degrade to NULL coords + one range warning, never a
        # silently-stored bad pin.
        result, rows = self._build(
            f'- id: L-1111111111\n  name: Millbrook\n{coords_line}')
        self.assertEqual(len(rows), 1, coords_line)
        self.assertIsNone(rows[0]['lat'], coords_line)
        self.assertIsNone(rows[0]['lon'], coords_line)
        self.assertEqual(result.exit_code, EXIT_WARNINGS, coords_line)
        warning_texts = [m.text for m in result.messages]
        self.assertEqual(len(warning_texts), 1, coords_line)
        self.assertIn('Millbrook', warning_texts[0])
        self.assertIn('out of range', warning_texts[0])

    def test_missing_decimal_latitude_warns(self) -> None:
        self._assert_out_of_range('  coords: [398, -95.6]\n')   # 39.8 minus its dot

    def test_swapped_out_of_range_longitude_warns(self) -> None:
        self._assert_out_of_range('  coords: [0, 200]\n')

    def test_non_finite_coords_warn(self) -> None:
        self._assert_out_of_range('  coords: ["nan", "1000"]\n')


_RESOLUTION_PERSON = '''---
id: P-aaaaaaaaaa
name: Samuel Rivera
living: false
aliases: [P-aaaaaaaaaa, Sam Rivera]
---

# Samuel Rivera
'''

_RESOLUTION_SOURCE = '''---
id: S-1111111111
title: Birth certificate
source_type: vital-record
---

## Claims
```yaml
- id: C-1111111111
  value: "Sam born 1985"
  type: birth
  persons: ["[[P-aaaaaaaaaa|Sam]]"]
  status: accepted
  reviewed: 2026-01-01
  confidence: high
  place: "[[L-1111111111]]"
  corroborates: ["[[C-2222222222]]"]

- id: C-3333333333
  value: "Sam is the son of ..."
  type: relationship
  persons: ["[[Sam Rivera]]"]
  roles: {child: "[[Sam Rivera]]"}
  status: accepted
  reviewed: 2026-01-01
  confidence: high

- id: C-4444444444
  value: "an ambiguous witness"
  type: note
  persons: ["[[Pat Smith]]"]
  status: suggested
  confidence: low

- id: C-5555555555
  value: "a place by name"
  type: residence
  persons: [P-aaaaaaaaaa]
  status: suggested
  confidence: low
  place: Millbrook
```
'''

_AMBIGUOUS_PERSON = '''---
id: {pid}
name: Pat Smith
living: false
---

# Pat Smith
'''


class ClaimPersonResolutionTests(unittest.TestCase):
    """Claim persons:/roles:/place references resolve through the alias map
    the same way source frontmatter people: does (TOOLING §3 E004): wrapped
    IDs unwrap, unambiguous names land on their P-id, ambiguous or unknown
    names are inert (no row, no garbage). And CRITICALLY: the incremental
    upsert produces the exact rows the full rebuild does."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write(self.root / 'people' / 'rivera__samuel_P-aaaaaaaaaa.md', _RESOLUTION_PERSON)
        _write(self.root / 'people' / 'smith__pat_P-bbbbbbbbbb.md',
               _AMBIGUOUS_PERSON.format(pid='P-bbbbbbbbbb'))
        _write(self.root / 'people' / 'smith__pat_P-cccccccccc.md',
               _AMBIGUOUS_PERSON.format(pid='P-cccccccccc'))
        _write(self.root / 'sources' / 'birth_S-1111111111.md', _RESOLUTION_SOURCE)
        _write(self.root / 'places' / 'places.yaml',
               '- id: L-1111111111\n  name: Millbrook\n  coords: [41.786, -73.694]\n')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _snapshot(self) -> dict:
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        conn.row_factory = sqlite3.Row
        try:
            return {
                'claim_persons': sorted(
                    tuple(r) for r in conn.execute(
                        'SELECT claim_id, person_id, position, role FROM claim_persons')),
                'claim_links': sorted(
                    tuple(r) for r in conn.execute(
                        'SELECT claim_id, rel, target_id FROM claim_links')),
                'places': {
                    r['id']: (r['place_id'], r['place_text']) for r in conn.execute(
                        'SELECT id, place_id, place_text FROM claims')},
            }
        finally:
            conn.close()

    def test_full_build_resolves_wrapped_ids_names_roles_places(self) -> None:
        index.build_index(self.root, {})
        snap = self._snapshot()

        # Wrapped bare ID `[[P-…|Sam]]` → the bare id.
        self.assertIn(('c-1111111111', 'p-aaaaaaaaaa', 0, None), snap['claim_persons'])
        # Name link `[[Sam Rivera]]` (an unambiguous person alias) → its P-id,
        # with the role resolved through the same map.
        self.assertIn(('c-3333333333', 'p-aaaaaaaaaa', 0, 'child'), snap['claim_persons'])
        # Ambiguous `[[Pat Smith]]` (two records) → inert: NO row, no garbage.
        c4 = [t for t in snap['claim_persons'] if t[0] == 'c-4444444444']
        self.assertEqual(c4, [])
        # No literal bracket garbage anywhere.
        self.assertFalse([t for t in snap['claim_persons'] if '[[' in t[1]])
        # Wrapped `[[C-…]]` corroborates target → bare c-id.
        self.assertIn(('c-1111111111', 'corroborates', 'c-2222222222'), snap['claim_links'])
        # place: wrapped L-id and registered place NAME both land on the L-id.
        self.assertEqual(snap['places']['c-1111111111'][0], 'l-1111111111')
        self.assertEqual(snap['places']['c-5555555555'][0], 'l-1111111111')

    def test_upsert_source_matches_full_build(self) -> None:
        # The symmetry contract (TOOLING §2): any discrepancy between the
        # incremental and full states is a bug in incremental, by definition.
        index.build_index(self.root, {})
        full = self._snapshot()
        status = index.upsert_source(self.root, {}, 's-1111111111')
        self.assertEqual(status, 'indexed')
        self.assertEqual(self._snapshot(), full)


_ALIAS_CLASH_PERSON = '''---
id: P-aaaaaaaaaa
name: Ken Smith
living: false
---

# Ken Smith
'''

_ALIAS_CLASH_SOURCE_A = '''---
id: S-1111111111
title: Census page
source_type: census
people: ["[[Ken Smith]]"]
---

## Claims
```yaml
- id: C-1111111111
  value: "Ken Smith, farmer"
  type: occupation
  persons: ["[[Ken Smith]]"]
  status: accepted
  reviewed: 2026-01-01
```
'''

# The clashing record: a DIFFERENT source hand-aliased with the person's name.
_ALIAS_CLASH_SOURCE_B = '''---
id: S-2222222222
title: Folder of Ken Smith papers
source_type: other
aliases: [Ken Smith]
---

## Claims
'''


class UpsertAliasUniverseParityTests(unittest.TestCase):
    """Round-2 finding 8 (the r3a repro): full build and upsert must resolve
    claim/frontmatter names through the SAME alias universe (persons+places).

    The full build snapshots its map before any source is indexed; the upsert
    used to read the whole aliases table, where another source's hand alias
    'Ken Smith' clashed the person 'Ken Smith' out of the clash-aware map -
    so `fha index --source S-A` silently dropped the claim_persons and
    source_people rows the full build keeps, breaking the row-for-row
    equivalence contract. The ('P','L') filter in _resolve_map_from_aliases
    makes both maps identical by construction."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write(self.root / 'people' / 'smith__ken_P-aaaaaaaaaa.md', _ALIAS_CLASH_PERSON)
        _write(self.root / 'sources' / 'census_S-1111111111.md', _ALIAS_CLASH_SOURCE_A)
        _write(self.root / 'sources' / 'papers_S-2222222222.md', _ALIAS_CLASH_SOURCE_B)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _rows(self) -> dict:
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        try:
            return {
                'claim_persons': sorted(tuple(r) for r in conn.execute(
                    'SELECT claim_id, person_id, position, role FROM claim_persons')),
                'source_people': sorted(tuple(r) for r in conn.execute(
                    'SELECT source_id, person_id FROM source_people')),
            }
        finally:
            conn.close()

    def test_other_sources_alias_cannot_drop_rows_on_upsert(self) -> None:
        index.build_index(self.root, {})
        full = self._rows()
        # The full build resolves the name; prove the fixture actually
        # exercises the clash (a person row exists to lose).
        self.assertIn(('c-1111111111', 'p-aaaaaaaaaa', 0, None), full['claim_persons'])
        self.assertIn(('s-1111111111', 'p-aaaaaaaaaa'), full['source_people'])

        status = index.upsert_source(self.root, {}, 's-1111111111')
        self.assertEqual(status, 'indexed')
        self.assertEqual(self._rows(), full)

    def test_same_source_own_alias_boundary_still_works(self) -> None:
        # Boundary case: the upserted source ITSELF is aliased with the
        # person's name. Its own alias rows are deleted before the map is
        # built (full build never saw them either), so the name still
        # resolves to the person in both paths.
        _write(self.root / 'sources' / 'census_S-1111111111.md',
               _ALIAS_CLASH_SOURCE_A.replace(
                   'source_type: census\n',
                   'source_type: census\naliases: [Ken Smith]\n'))
        index.build_index(self.root, {})
        full = self._rows()
        self.assertIn(('c-1111111111', 'p-aaaaaaaaaa', 0, None), full['claim_persons'])
        status = index.upsert_source(self.root, {}, 's-1111111111')
        self.assertEqual(status, 'indexed')
        self.assertEqual(self._rows(), full)

    def test_citation_map_still_resolves_source_stems(self) -> None:
        # The scope guard's counterpart: the CITATION scan keeps the full
        # alias universe on purpose - a prose `[[Ken Smith]]` note-link to
        # the aliased source... is a clash here (person + source share the
        # string), but an unambiguous source stem must keep resolving.
        _write(self.root / 'sources' / 'papers_S-2222222222.md',
               _ALIAS_CLASH_SOURCE_B.replace('aliases: [Ken Smith]',
                                             'aliases: [ken-papers]')
               + '\nSee also [[ken-papers]].\n')
        index.build_index(self.root, {})
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        try:
            cites = list(conn.execute(
                "SELECT token FROM citations WHERE token='s-2222222222'"))
        finally:
            conn.close()
        self.assertTrue(cites, 'source stem citation should resolve via the full map')


class SourceRestrictedTests(unittest.TestCase):
    """The sources.restricted column must store 1 for ANY truthy `restricted:`
    value. The marker is open (SPEC §19): the typed values (`dna`,
    `by-request`) are the STRONGEST privacy markers - `by-request` never opens
    under any export flag - and the old narrow `in (True, 'true')` idiom
    flattened exactly those to 0 (unrestricted) in every SQL prefilter built
    on the column. Absent and explicit-false stay 0. The incremental upsert
    must agree with the full rebuild (TOOLING §2: any discrepancy is a bug in
    incremental, by definition)."""

    # restricted-line → expected column value. Keys are also used to build
    # distinct S-ids/paths, one source per case.
    CASES = [
        ('restricted: dna\n', 1),
        ('restricted: by-request\n', 1),
        ('restricted: true\n', 1),
        ('', 0),                       # absent → unrestricted
        ('restricted: false\n', 0),    # explicit false → unrestricted
    ]

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.sids = []
        for i, (line, _expected) in enumerate(self.CASES):
            sid = f's-{str(i) * 10}'
            self.sids.append(sid)
            _write(
                self.root / 'sources' / 'other' / f'src_{sid}.md',
                f'---\nid: {sid.upper()}\ntitle: Test {i}\n'
                f'source_type: other\n{line}---\n\n## Claims\n',
            )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _restricted_column(self) -> dict:
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        try:
            return dict(conn.execute('SELECT id, restricted FROM sources'))
        finally:
            conn.close()

    def test_full_build_stores_typed_values_as_restricted(self) -> None:
        index.build_index(self.root, {})
        got = self._restricted_column()
        for sid, (line, expected) in zip(self.sids, self.CASES):
            self.assertEqual(got[sid], expected, f'{line!r} on {sid}')

    def test_upsert_matches_full_build(self) -> None:
        # Upsert every source after the full build; the column must be
        # byte-identical to the full-rebuild state (both flow through
        # _index_source, but prove it end-to-end).
        index.build_index(self.root, {})
        full = self._restricted_column()
        for sid in self.sids:
            self.assertEqual(index.upsert_source(self.root, {}, sid), 'indexed')
        self.assertEqual(self._restricted_column(), full)


_EXTRACT_SID = 'S-7a7a7a7a7a'
_EXTRACT_SOURCE = '''---
id: {sid}
title: County History
source_type: book
files:
  - file: documents/book/county-history_{low}.pdf
    role: primary
  - file: documents/book/county-history-extracted-text_{sid}.md
    role: extracted-text
    derived: true
---

## Notes
A fat county history.
'''
_EXTRACT_DUMP = '''# Extracted text - County History [{sid}]

[Page 1]
Ferdinand Hartley arrived in Marsh Creek in 1854.

[Page 2]
(no text layer on this page - read it with vision)
'''


class ExtractedTextIndexingTests(unittest.TestCase):
    """`fha source extract` promises `fha index` makes the dumped PDF text
    searchable. That promise is kept only if BOTH index paths feed a
    `role: extracted-text` companion's body into transcripts_fts - the full
    rebuild AND the incremental upsert, since JSON/workbench search reads text
    hits from that table. This is the symmetry contract (TOOLING §2): if the
    two paths disagree, incremental is wrong by definition."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        low = _EXTRACT_SID.lower()
        _write(self.root / 'sources' / 'book' / f'county-history_{low}.md',
               _EXTRACT_SOURCE.format(sid=_EXTRACT_SID, low=low))
        # The dump companion lives beside the PDF under documents/; only its
        # text body is indexed (the PDF itself is never read by the indexer).
        _write(self.root / 'documents' / 'book'
               / f'county-history-extracted-text_{_EXTRACT_SID}.md',
               _EXTRACT_DUMP.format(sid=_EXTRACT_SID))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _transcript_rows(self) -> list:
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        try:
            return sorted(tuple(r) for r in conn.execute(
                'SELECT source_id, path FROM transcripts_fts'))
        finally:
            conn.close()

    def _matches(self, term: str) -> list:
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        try:
            return [r[0] for r in conn.execute(
                'SELECT source_id FROM transcripts_fts WHERE content MATCH ?', (term,))]
        finally:
            conn.close()

    def test_full_build_populates_transcripts_fts(self) -> None:
        index.build_index(self.root, {})
        rows = self._transcript_rows()
        self.assertEqual(rows, [(
            's-7a7a7a7a7a',
            f'documents/book/county-history-extracted-text_{_EXTRACT_SID}.md')])
        # The dumped text is searchable by a word from inside a page.
        self.assertEqual(self._matches('Marsh'), ['s-7a7a7a7a7a'])

    def test_malformed_utf8_companion_does_not_empty_the_index(self) -> None:
        # A non-UTF-8 extracted-text companion (hand-edited or corrupted) must NOT
        # abort the build. UnicodeDecodeError is a ValueError, not an OSError, so
        # without the guard it escapes and, on a full rebuild, rolls back over an
        # already-dropped index - leaving an empty current-schema cache that later
        # readers accept as fresh. The malformed dump is skipped; the source and
        # the rest of the index survive.
        dump = (self.root / 'documents' / 'book'
                / f'county-history-extracted-text_{_EXTRACT_SID}.md')
        dump.write_bytes(b'\xff\xfe not valid utf-8 \x80\x81')
        index.build_index(self.root, {})            # must not raise
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        try:
            n_sources = conn.execute('SELECT COUNT(*) FROM sources').fetchone()[0]
            n_transcripts = conn.execute(
                'SELECT COUNT(*) FROM transcripts_fts').fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n_sources, 1, 'the source is still indexed (index not emptied)')
        self.assertEqual(n_transcripts, 0, 'the malformed dump was skipped')

    def test_extracted_text_is_found_through_the_json_ranked_search(self) -> None:
        # The finding: transcripts_fts is populated, but the JSON/workbench
        # backend (find._ranked_search, behind search_json + serve.py) queried
        # only notes_fts - so `fha find --json` could not find extracted PDF
        # text even after `fha index`. It must query transcripts_fts too.
        import find
        index.build_index(self.root, {})
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        conn.row_factory = sqlite3.Row
        try:
            hits = find._ranked_search(conn, 'Marsh', ['text'], 20)
        finally:
            conn.close()
        text_hits = [h for h in hits if h['type'] == 'text']
        self.assertTrue(text_hits, hits)
        self.assertTrue(any('extracted-text' in h['detail'] for h in text_hits),
                        text_hits)

    def test_upsert_matches_full_build_and_never_duplicates(self) -> None:
        index.build_index(self.root, {})
        full = self._transcript_rows()
        status = index.upsert_source(self.root, {}, _EXTRACT_SID.lower())
        self.assertEqual(status, 'indexed')
        # Symmetric with the full build - and the upsert's DELETE-first step
        # means re-running it never leaves a duplicate transcript row.
        self.assertEqual(self._transcript_rows(), full)
        self.assertEqual(self._matches('Marsh'), ['s-7a7a7a7a7a'])


class RunIndexRootGuardTests(unittest.TestCase):
    """`fha index --root <non-archive>` must refuse (exit 3) and create
    NOTHING. Without the guard it globbed missing dirs, minted an empty
    .cache/index.sqlite inside ANY folder, and printed "Index rebuilt" with
    exit 0 - a typo'd --root produced a permanently-"successful" empty
    archive. A --root that does carry fha.yaml builds exactly as before."""

    def test_non_archive_root_refused_and_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = index._standalone_main(['--root', tmp])
            self.assertEqual(rc, EXIT_FAILURE)
            self.assertFalse((Path(tmp) / '.cache').exists())
            # Nothing else materialized either - the folder is untouched.
            self.assertEqual(list(Path(tmp).iterdir()), [])
            # The message names the cause (no fha.yaml) and the fix (--root
            # at the folder that contains it) - the next-step rule.
            self.assertIn('fha.yaml', err.getvalue())
            self.assertIn('--root', err.getvalue())

    def test_incremental_source_against_non_archive_also_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = index._standalone_main(
                    ['--root', tmp, '--source', 'S-1111111111'])
            self.assertEqual(rc, EXIT_FAILURE)
            self.assertFalse((Path(tmp) / '.cache').exists())
            self.assertIn('fha.yaml', err.getvalue())

    def test_root_with_fha_yaml_builds_as_before(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
            _write(root / 'sources' / 'other' / 'src_S-1111111111.md',
                   '---\nid: S-1111111111\ntitle: Test\nsource_type: other\n---\n\n## Claims\n')
            with redirect_stdout(io.StringIO()):
                rc = index._standalone_main(['--root', tmp])
            self.assertEqual(rc, EXIT_CLEAN)
            db = root / '.cache' / 'index.sqlite'
            self.assertTrue(db.is_file())
            conn = sqlite3.connect(str(db))
            try:
                self.assertEqual(
                    conn.execute('SELECT COUNT(*) FROM sources').fetchone()[0], 1)
            finally:
                conn.close()


_NEGATED_HUSBAND = '''---
id: P-hhhhhhhhhh
name: Henry Fowler
living: false
aliases: [P-hhhhhhhhhh, Henry Fowler]
---

# Henry Fowler
'''

_NEGATED_WIFE = '''---
id: P-wwwwwwwwww
name: Wilma Grant
living: false
aliases: [P-wwwwwwwwww, Wilma Grant]
---

# Wilma Grant
'''

# One source, two marriage claims: a real (positive) marriage between Henry and
# a third person, and an accepted but negated "they did NOT marry" claim between
# Henry and Wilma (SPEC §8.6: a researched negative). Only the positive one may
# mint spouse edges.
_NEGATED_MARRIAGE_SOURCE = '''---
id: S-9999999999
title: Marriage research
source_type: other
---

## Claims
```yaml
- id: C-1010101010
  value: "Henry married Rose, 1901"
  type: marriage
  persons: ["[[Henry Fowler]]", "[[Rose Kemp]]"]
  status: accepted
  reviewed: 2026-01-01
  date: 1901

- id: C-2020202020
  value: "Researched: Henry and Wilma did NOT marry"
  type: marriage
  persons: ["[[Henry Fowler]]", "[[Wilma Grant]]"]
  status: accepted
  reviewed: 2026-01-01
  negated: true
  evidence: negative
```
'''

_NEGATED_THIRD = '''---
id: P-rrrrrrrrrr
name: Rose Kemp
living: false
aliases: [P-rrrrrrrrrr, Rose Kemp]
---

# Rose Kemp
'''


class NegatedRelationshipDerivationTests(unittest.TestCase):
    """A negated relationship-bearing claim asserts the ABSENCE of a bond
    (SPEC §8.6: "we researched and it did NOT happen"). Even when accepted it
    must never become a graph edge, or a confirmed "these two did not marry"
    would mint phantom spouse edges that drive family views, relation answers,
    and Ahnentafel placement. The positive marriage in the same source must
    still derive normally, so the exclusion is scoped to `negated` alone."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write(self.root / 'people' / 'fowler__henry_P-hhhhhhhhhh.md', _NEGATED_HUSBAND)
        _write(self.root / 'people' / 'grant__wilma_P-wwwwwwwwww.md', _NEGATED_WIFE)
        _write(self.root / 'people' / 'kemp__rose_P-rrrrrrrrrr.md', _NEGATED_THIRD)
        _write(self.root / 'sources' / 'marriage_S-9999999999.md',
               _NEGATED_MARRIAGE_SOURCE)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _spouse_pairs(self) -> set:
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        try:
            return {
                tuple(sorted((p, o)))
                for p, o in conn.execute(
                    "SELECT person_id, other_id FROM relationships WHERE rel = 'spouse'")
            }
        finally:
            conn.close()

    def test_full_build_excludes_negated_marriage(self) -> None:
        index.build_index(self.root, {})
        pairs = self._spouse_pairs()
        # The positive marriage derives reciprocal spouse edges.
        self.assertIn(('p-hhhhhhhhhh', 'p-rrrrrrrrrr'), pairs)
        # The negated marriage produces NO spouse edge in either direction.
        self.assertNotIn(('p-hhhhhhhhhh', 'p-wwwwwwwwww'), pairs)
        self.assertFalse(
            any('p-wwwwwwwwww' in pair for pair in pairs),
            'negated marriage must not touch the relationships table')

    def test_upsert_source_matches_full_build(self) -> None:
        # Negation must be honored on the incremental path too, or `fha index
        # --source` would silently reintroduce the phantom edge (TOOLING §2).
        index.build_index(self.root, {})
        full = self._spouse_pairs()
        status = index.upsert_source(self.root, {}, 's-9999999999')
        self.assertEqual(status, 'indexed')
        self.assertEqual(self._spouse_pairs(), full)


def _scandir_denying(unreadable: Path):
    """An os.scandir stand-in that refuses to list `unreadable`.

    The fault goes in at `os.scandir` because `os.walk` resolves it at call
    time on every supported Python - that is what makes the `onerror` seam
    observable here. chmod cannot produce this: CI runs as root, which ignores
    mode bits, and Windows has no equivalent.

    What this deliberately does NOT rely on: that pathlib's `rglob` reaches the
    disk the same way. It does on 3.11/3.12/3.14, but NOT on the 3.10 floor
    (pathlib routes through an accessor object that bound `os.scandir` at
    import time, so a later patch is invisible) and not on 3.13. So the
    injection does not reproduce the pre-fix `rglob` behaviour on every version
    we support - a regression back to `rglob` is still caught everywhere, but
    on the floor it is caught by the warning going missing rather than by the
    folder reading as empty.
    """
    real_scandir = os.scandir
    target = unreadable.resolve()

    def scandir(path='.'):
        try:
            denied = Path(path).resolve() == target
        except (TypeError, ValueError, OSError):
            denied = False
        if denied:
            err = PermissionError(13, 'Permission denied')
            err.filename = str(path)
            raise err
        return real_scandir(path)

    return scandir


class UnreadableFolderBuildTests(unittest.TestCase):
    """A rebuild that could not read a folder must not report a clean build.

    `build_index` DROPS every table and refills them, so a folder that will
    not list does not merely go unindexed - the persons, claims and citations
    filed there disappear from search, timelines and exports. The rows are
    rebuildable (the index is a cache), but a build that says nothing has
    told the human nothing, and `fha report` prints these messages at session
    start where he actually looks."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.folder = self.root / 'people' / '040 Hartley'
        self.folder.mkdir(parents=True)
        (self.root / 'fha.yaml').write_text(
            'roots:\n  documents: documents\n', encoding='utf-8')
        (self.folder / 'hartley__cur_P-aaaaaaaaaa.md').write_text(
            '---\nid: P-aaaaaaaaaa\nname: Cur Hartley\nliving: false\n'
            'tier: curated\n---\n\n## Biography\n\nx\n',
            encoding='utf-8')

    def test_build_warns_and_exits_1_naming_the_folder(self) -> None:
        clean = index.build_index(self.root, {})
        self.assertEqual(clean.exit_code, EXIT_CLEAN)
        self.assertEqual(clean['persons'], 1)

        with unittest.mock.patch('os.scandir', new=_scandir_denying(self.folder)):
            result = index.build_index(self.root, {})

        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result['persons'], 0)      # the folder went unread
        self.assertEqual(result['unreadable_dirs'], ['people/040 Hartley'])
        texts = [m.text for m in result.messages]
        self.assertTrue(any('could not be opened' in t for t in texts), texts)
        self.assertTrue(any('fha index' in t for t in texts), texts)
        # Report output can be committed, so it carries no local absolute path.
        self.assertFalse(any(str(self.root) in t for t in texts), texts)


_TRANSCRIPT_SID = 'S-6b6b6b6b6b'
_TRANSCRIPT_SOURCE = '''---
id: {sid}
title: Hand-drawn Harkness family chart
source_type: other
files:
  - file: documents/charts/harkness-chart_{low}.jpg
    role: front
  - file: documents/charts/harkness-chart-transcript_{sid}.md
    role: transcript
---

## Notes
Twenty-two pages of scan, and one page of somebody's patience.
'''
_TRANSCRIPT_TEXT = '''# Transcript - hand-drawn family chart [{sid}]

Rose Harkness, married 1871, mother of the bride.
'''


class TranscriptCompanionIndexingTests(unittest.TestCase):
    """A `role: transcript` companion is text about the evidence exactly as an
    `extracted-text` dump is, and must reach transcripts_fts the same way (#46).

    Until it did, an archive could hold a full, careful transcript of a scan and
    still answer `fha find --text` as though the scan were mute - the index
    loaded only `fha source extract`'s own dumps, so every transcript written by
    hand, by the transcribe-audio skill, or by the transcribe-source skill
    reading a picture and typing it out stayed outside the searchable surface.
    Both index paths are checked, because a contract honoured by the full
    rebuild and not by the incremental upsert is a contract that fails in
    ordinary use."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        low = _TRANSCRIPT_SID.lower()
        _write(self.root / 'sources' / 'other' / f'harkness-chart_{low}.md',
               _TRANSCRIPT_SOURCE.format(sid=_TRANSCRIPT_SID, low=low))
        _write(self.root / 'documents' / 'charts'
               / f'harkness-chart-transcript_{_TRANSCRIPT_SID}.md',
               _TRANSCRIPT_TEXT.format(sid=_TRANSCRIPT_SID))
        # The scan itself: never read by the indexer, present so the source
        # looks like the real thing (an image plus one page of typing).
        _write(self.root / 'documents' / 'charts' / f'harkness-chart_{low}.jpg',
               'not really a jpeg')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _rows(self) -> list:
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        try:
            return sorted(tuple(r) for r in conn.execute(
                'SELECT source_id, path FROM transcripts_fts'))
        finally:
            conn.close()

    def test_full_build_loads_the_transcript_companion(self) -> None:
        index.build_index(self.root, {})
        self.assertEqual(self._rows(), [(
            's-6b6b6b6b6b',
            f'documents/charts/harkness-chart-transcript_{_TRANSCRIPT_SID}.md')])

    def test_upsert_matches_the_full_build(self) -> None:
        index.build_index(self.root, {})
        full = self._rows()
        self.assertEqual(
            index.upsert_source(self.root, {}, _TRANSCRIPT_SID.lower()), 'indexed')
        self.assertEqual(self._rows(), full)

    def test_the_name_on_the_chart_comes_back_from_a_text_search(self) -> None:
        # The round trip the issue asks for: a name written only on a scan,
        # typed out into a transcript companion, is returned by `fha find
        # --text` - and by the JSON/workbench backend, which reads text hits
        # from transcripts_fts alone and so had no other way to see it.
        import find
        index.build_index(self.root, {})
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        conn.row_factory = sqlite3.Row
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = find._find_text('Harkness', self.root, {}, conn)
            out = buf.getvalue()
            hits = find._ranked_search(conn, 'Harkness', ['text'], 20)
        finally:
            conn.close()
        self.assertEqual(code, EXIT_CLEAN)
        self.assertIn('harkness-chart-transcript', out)
        self.assertTrue(any('transcript' in h['detail'] for h in hits), hits)


# ── Marriage / divorce role scoping ──────────────────────────────────────────
#
# A marriage certificate names the couple AND both sets of parents - six people
# is the ordinary shape of a Vermont-style certificate, and listing all six in
# `persons:` is correct (`persons:` is "who the claim is about", SPEC §8.3).
# Only the two people in `roles: spouse:` married each other. Expanding
# `persons:` into a complete graph turns a father-in-law into a spouse.
#
# The rule these tests pin (TOOLING §197 - `type: marriage` yields reciprocal
# spouse edges, `date_end` backfilled from a divorce claim BETWEEN THE PAIR):
#   - `roles: spouse:` names the couple whenever it is present;
#   - exactly two persons and no roles map falls back to those two;
#   - more than two persons and no usable roles map emits NOTHING. Silence is
#     recoverable, a false marriage is not.

_MDR_HUS = 'P-h1h1h1h1h1'
_MDR_WIF = 'P-w2w2w2w2w2'
_MDR_HFA = 'P-f3f3f3f3f3'
_MDR_HMO = 'P-m4m4m4m4m4'
_MDR_WFA = 'P-f5f5f5f5f5'
_MDR_WMO = 'P-m6m6m6m6m6'

_MDR_ALL = [_MDR_HUS, _MDR_WIF, _MDR_HFA, _MDR_HMO, _MDR_WFA, _MDR_WMO]

_MDR_NAMES = {
    _MDR_HUS: 'Amos Prentice',
    _MDR_WIF: 'Clara Denby',
    _MDR_HFA: 'Reuben Prentice',
    _MDR_HMO: 'Hannah Prentice',
    _MDR_WFA: 'Silas Denby',
    _MDR_WMO: 'Martha Denby',
}


def _mdr_person(pid: str) -> str:
    name = _MDR_NAMES[pid]
    return (f'---\nid: {pid}\nname: {name}\nliving: false\n'
            f'aliases: [{pid}, {name}]\n---\n\n# {name}\n')


def _mdr_claim(cid: str, ctype: str, persons: list, date: str,
               spouse_roles: list = None, other_roles: dict = None) -> str:
    """One accepted marriage/divorce claim, with or without a `roles:` map.

    `other_roles` adds non-spouse role keys ({'parent': [P-…]}) so a claim can
    say outright that somebody it names was NOT a party to the marriage."""
    text = (f'- id: {cid}\n'
            f'  value: "{ctype} record"\n'
            f'  type: {ctype}\n'
            f'  persons: [{", ".join(persons)}]\n'
            f'  status: accepted\n'
            f'  reviewed: 2026-01-01\n'
            f'  date: {date}\n')
    if spouse_roles is not None or other_roles:
        text += '  roles:\n'
        if spouse_roles is not None:
            text += f'    spouse: [{", ".join(spouse_roles)}]\n'
        for role_name, who in (other_roles or {}).items():
            text += f'    {role_name}: [{", ".join(who)}]\n'
    return text


class MarriageRoleScopingTests(unittest.TestCase):
    """A marriage claim naming the couple plus both sets of parents must derive
    exactly one spouse pair - the couple - not a complete graph of all six."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for pid in _MDR_ALL:
            _write(self.root / 'people' / f'p_{pid}.md', _mdr_person(pid))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_source(self, claims: str) -> None:
        _write(self.root / 'sources' / 'marriage_S-7777777777.md',
               '---\nid: S-7777777777\ntitle: Marriage record\n'
               'source_type: vital-record\n---\n\n'
               f'## Claims\n```yaml\n{claims}```\n')

    def _spouse_edges(self) -> set:
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        try:
            return {
                (p, o) for p, o in conn.execute(
                    "SELECT person_id, other_id FROM relationships WHERE rel = 'spouse'")
            }
        finally:
            conn.close()

    def test_six_person_marriage_with_roles_derives_only_the_couple(self) -> None:
        # The reporter's actual claim shape: a certificate naming the couple and
        # both sets of parents. Six people, one marriage.
        self._write_source(_mdr_claim(
            'C-1111111111', 'marriage', _MDR_ALL, '1890',
            spouse_roles=[_MDR_HUS, _MDR_WIF]))
        index.build_index(self.root, {})
        edges = self._spouse_edges()
        self.assertEqual(
            edges,
            {(_MDR_HUS.lower(), _MDR_WIF.lower()),
             (_MDR_WIF.lower(), _MDR_HUS.lower())},
            'a six-person marriage certificate must derive exactly two directed '
            'spouse edges, between the couple named in roles: spouse:')

    def test_six_person_marriage_without_roles_emits_nothing(self) -> None:
        # No roles map and more than two people: the tool cannot know who
        # married whom, so it must not guess. Silence is recoverable; a false
        # marriage edge read out of `fha relate` is not. W125 tells the human.
        self._write_source(_mdr_claim(
            'C-1111111111', 'marriage', _MDR_ALL, '1890'))
        index.build_index(self.root, {})
        self.assertEqual(self._spouse_edges(), set(),
                         'more than two persons with no roles: map must emit no '
                         'spouse edges at all, never a guessed pairing')

    def test_two_person_marriage_without_roles_still_derives(self) -> None:
        # The overwhelmingly common case - it must not regress.
        self._write_source(_mdr_claim(
            'C-1111111111', 'marriage', [_MDR_HUS, _MDR_WIF], '1890'))
        index.build_index(self.root, {})
        self.assertEqual(
            self._spouse_edges(),
            {(_MDR_HUS.lower(), _MDR_WIF.lower()),
             (_MDR_WIF.lower(), _MDR_HUS.lower())})

    def test_partial_roles_map_falls_back_to_the_two_named_people(self) -> None:
        # A roles: map answers "who married whom" only when it names a couple.
        # One typo'd id, one spouse left out of persons:, one alias that stopped
        # resolving - each leaves a SINGLE resolvable spouse, which is not an
        # answer. Two people are named, so the ordinary two-person rule applies
        # exactly as it would with no roles: map at all. Treating a thin roles
        # map as authoritative would silently drop the edge from an ordinary
        # two-person marriage, and W125 could never catch it (it only speaks
        # above two people).
        self._write_source(_mdr_claim(
            'C-1111111111', 'marriage', [_MDR_HUS, _MDR_WIF], '1890',
            spouse_roles=[_MDR_HUS, 'P-zzzzzzzzzz']))
        index.build_index(self.root, {})
        self.assertEqual(
            self._spouse_edges(),
            {(_MDR_HUS.lower(), _MDR_WIF.lower()),
             (_MDR_WIF.lower(), _MDR_HUS.lower())},
            'a roles: map naming one resolvable spouse has not said who the '
            'couple were, so a two-person claim must still derive its pair')

    def test_roles_map_resolving_to_nobody_falls_back_to_the_two_named(self) -> None:
        # The same state one step further along: every id in the roles: map is
        # broken, so it resolves to nobody at all. Still two people named, still
        # the ordinary claim.
        self._write_source(_mdr_claim(
            'C-1111111111', 'marriage', [_MDR_HUS, _MDR_WIF], '1890',
            spouse_roles=['P-zzzzzzzzzz', 'P-yyyyyyyyyy']))
        index.build_index(self.root, {})
        self.assertEqual(
            self._spouse_edges(),
            {(_MDR_HUS.lower(), _MDR_WIF.lower()),
             (_MDR_WIF.lower(), _MDR_HUS.lower())})

    def test_six_person_marriage_with_one_id_roles_map_emits_nothing(self) -> None:
        # The fallback must not reach past two people. A thin roles: map on a
        # six-person certificate leaves the tool exactly where a missing one
        # does - unable to tell the couple from their parents - so it stays
        # silent and W125 does the talking.
        self._write_source(_mdr_claim(
            'C-1111111111', 'marriage', _MDR_ALL, '1890',
            spouse_roles=[_MDR_HUS]))
        index.build_index(self.root, {})
        self.assertEqual(self._spouse_edges(), set())

    def test_serial_roles_map_of_three_still_pairs_all_three(self) -> None:
        # A roles: map naming three or more spouses has answered the question -
        # serial marriages recorded on one claim - and keeps its full pairing.
        self._write_source(_mdr_claim(
            'C-1111111111', 'marriage', _MDR_ALL, '1890',
            spouse_roles=[_MDR_HUS, _MDR_WIF, _MDR_HMO]))
        index.build_index(self.root, {})
        trio = [_MDR_HUS.lower(), _MDR_WIF.lower(), _MDR_HMO.lower()]
        expected = {(a, b) for a in trio for b in trio if a != b}
        self.assertEqual(self._spouse_edges(), expected)

    def test_legacy_spouse_of_relationship_is_scoped_too(self) -> None:
        # The third path into the spouse graph: `relationship` +
        # `subtype: spouse-of`, whose roles: fallback had the same unguarded
        # shape. A roles: map that resolves no spouse (the list shorthand
        # lint's own E015 message suggests) must not pair six people off.
        claims = (
            '- id: C-4444444444\n'
            '  value: "spouses"\n'
            '  type: relationship\n'
            '  subtype: spouse-of\n'
            f'  persons: [{", ".join(_MDR_ALL)}]\n'
            '  roles: [spouse, spouse]\n'
            '  status: accepted\n'
            '  reviewed: 2026-01-01\n'
            '  date: 1890\n'
        )
        self._write_source(claims)
        index.build_index(self.root, {})
        self.assertEqual(self._spouse_edges(), set())

    def test_duplicate_persons_entry_never_marries_someone_to_himself(self) -> None:
        # `persons:` accepts a bare P-id and a name-link, and nothing stops both
        # from landing on the same person - `claim_persons` has no UNIQUE
        # constraint and stores one row per entry. Two rows for Amos meant two
        # "spouses", and the pairing loop married him to himself: a spouse edge
        # from a person to themselves, silent in lint (W125 cannot speak, there
        # is only one distinct person) and read back as fact by `fha relate`,
        # the family charts and the GEDCOM export.
        self._write_source(_mdr_claim(
            'C-1111111111', 'marriage',
            [_MDR_HUS, f'"[[{_MDR_NAMES[_MDR_HUS]}]]"'], '1890'))
        index.build_index(self.root, {})
        edges = self._spouse_edges()
        self.assertEqual(
            [e for e in edges if e[0] == e[1]], [],
            'nobody is married to themselves - a duplicate persons: entry must '
            'never become a spouse edge')
        # One distinct person is not a couple, so the claim derives nothing.
        self.assertEqual(edges, set())

    def test_duplicate_persons_entry_beside_a_real_spouse_derives_the_pair(self) -> None:
        # The same duplicate on an otherwise ordinary claim. Counted as three
        # entries the claim overshot the two-person fallback and derived
        # nothing; counted as the two PEOPLE it actually names, it is the
        # commonest shape there is and must derive its pair.
        self._write_source(_mdr_claim(
            'C-1111111111', 'marriage',
            [_MDR_HUS, f'"[[{_MDR_NAMES[_MDR_HUS]}]]"', _MDR_WIF], '1890'))
        index.build_index(self.root, {})
        self.assertEqual(
            self._spouse_edges(),
            {(_MDR_HUS.lower(), _MDR_WIF.lower()),
             (_MDR_WIF.lower(), _MDR_HUS.lower())})

    def test_explicit_non_spouse_role_is_never_married(self) -> None:
        # The two-person fallback exists for a claim that says nothing about
        # who married whom. This claim SAYS: one spouse, one parent. Pairing
        # them anyway contradicts the claim in its own words and marries a man
        # to his own father - the exact corruption the roles: scoping was added
        # to prevent, reached through the fallback instead.
        self._write_source(_mdr_claim(
            'C-1111111111', 'marriage', [_MDR_HUS, _MDR_HFA], '1890',
            spouse_roles=[_MDR_HUS], other_roles={'parent': [_MDR_HFA]}))
        index.build_index(self.root, {})
        self.assertEqual(
            self._spouse_edges(), set(),
            'a claim that calls someone a parent must never have them married '
            'to the person it calls a spouse')

    def test_insert_site_refuses_a_self_edge_even_when_handed_duplicates(self) -> None:
        # Belt and braces. `spouse_parties` dedupes, so this state is already
        # unreachable through it - but the insert sites are what actually write
        # the tree, and a self-marriage must be impossible there too, whatever
        # a future caller hands them.
        self._write_source(_mdr_claim(
            'C-1111111111', 'marriage', [_MDR_HUS, _MDR_WIF], '1890'))
        with unittest.mock.patch.object(
                index, 'spouse_parties',
                lambda _rows: [_MDR_HUS.lower(), _MDR_HUS.lower()]):
            index.build_index(self.root, {})
        self.assertEqual(self._spouse_edges(), set())

    def test_upsert_source_matches_full_build(self) -> None:
        # The incremental path re-derives relationships too; a fix applied to
        # build_index only would let `fha index --source` restore the false
        # edges (the full-rebuild / upsert symmetry pair).
        self._write_source(_mdr_claim(
            'C-1111111111', 'marriage', _MDR_ALL, '1890',
            spouse_roles=[_MDR_HUS, _MDR_WIF]))
        index.build_index(self.root, {})
        full = self._spouse_edges()
        status = index.upsert_source(self.root, {}, 's-7777777777')
        self.assertEqual(status, 'indexed')
        self.assertEqual(self._spouse_edges(), full)


class DivorceRoleScopingTests(unittest.TestCase):
    """A divorce claim ends a spouse edge rather than minting one, so the same
    unscoped pair loop corrupts differently: it closes OTHER people's real
    marriages. A divorce record naming the couple plus both sets of parents
    pairs each parent couple with itself - and those two are genuinely married,
    so the UPDATE lands and their marriage is recorded as ending on the
    couple's divorce date."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for pid in _MDR_ALL:
            _write(self.root / 'people' / f'p_{pid}.md', _mdr_person(pid))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_source(self, divorce_claim: str) -> None:
        # Two plain two-person marriages (the couple, and the husband's
        # parents), then the divorce under test. Both marriages derive
        # correctly through the two-person fallback, so anything wrong in the
        # result is the divorce branch alone.
        claims = (
            _mdr_claim('C-1111111111', 'marriage', [_MDR_HUS, _MDR_WIF], '1890')
            + _mdr_claim('C-2222222222', 'marriage', [_MDR_HFA, _MDR_HMO], '1860')
            + divorce_claim
        )
        _write(self.root / 'sources' / 'divorce_S-8888888888.md',
               '---\nid: S-8888888888\ntitle: Divorce record\n'
               'source_type: vital-record\n---\n\n'
               f'## Claims\n```yaml\n{claims}```\n')

    def _date_end(self, a: str, b: str):
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        try:
            row = conn.execute(
                "SELECT date_end FROM relationships WHERE rel = 'spouse' "
                'AND person_id = ? AND other_id = ?',
                (a.lower(), b.lower())).fetchone()
            self.assertIsNotNone(row, f'no spouse edge {a} -> {b}')
            return row[0]
        finally:
            conn.close()

    def test_six_person_divorce_with_roles_ends_only_the_couple(self) -> None:
        self._write_source(_mdr_claim(
            'C-3333333333', 'divorce', _MDR_ALL, '1900',
            spouse_roles=[_MDR_HUS, _MDR_WIF]))
        index.build_index(self.root, {})
        self.assertIsNotNone(self._date_end(_MDR_HUS, _MDR_WIF),
                             "the couple's own marriage must be closed by their divorce")
        self.assertIsNone(
            self._date_end(_MDR_HFA, _MDR_HMO),
            "the husband's parents were merely named on the certificate - their "
            'marriage must not be recorded as ending on their child\'s divorce date')
        self.assertIsNone(self._date_end(_MDR_HMO, _MDR_HFA))

    def test_six_person_divorce_without_roles_ends_nothing(self) -> None:
        self._write_source(_mdr_claim(
            'C-3333333333', 'divorce', _MDR_ALL, '1900'))
        index.build_index(self.root, {})
        self.assertIsNone(self._date_end(_MDR_HUS, _MDR_WIF),
                          'with no roles: map and six people the tool cannot know '
                          'whose marriage ended, so it must close none')
        self.assertIsNone(self._date_end(_MDR_HFA, _MDR_HMO))

    def test_two_person_divorce_with_partial_roles_still_ends_the_marriage(self) -> None:
        # The divorce branch reads the same rule, so it inherits the same
        # regression: a decree whose roles: map resolves to one person must not
        # leave a two-person divorce unable to close its own marriage.
        self._write_source(_mdr_claim(
            'C-3333333333', 'divorce', [_MDR_HUS, _MDR_WIF], '1900',
            spouse_roles=[_MDR_HUS, 'P-zzzzzzzzzz']))
        index.build_index(self.root, {})
        self.assertIsNotNone(
            self._date_end(_MDR_HUS, _MDR_WIF),
            'a roles: map naming one resolvable spouse has not said whose '
            'marriage ended, so the two named people still apply')
        self.assertIsNone(self._date_end(_MDR_HFA, _MDR_HMO))

    def test_two_person_divorce_without_roles_still_ends_the_marriage(self) -> None:
        self._write_source(_mdr_claim(
            'C-3333333333', 'divorce', [_MDR_HUS, _MDR_WIF], '1900'))
        index.build_index(self.root, {})
        self.assertIsNotNone(self._date_end(_MDR_HUS, _MDR_WIF))
        self.assertIsNone(self._date_end(_MDR_HFA, _MDR_HMO))

    def test_two_person_divorce_with_an_explicit_parent_role_ends_nothing(self) -> None:
        # The two-person fallback on the ending side. A decree naming the
        # husband and his father, calling one a spouse and the other a parent,
        # says plainly that these two were not the marriage - so closing an
        # edge between them (his parents' marriage, in the general case) is a
        # real marriage recorded as ending on the wrong date and by the wrong
        # decree.
        self._write_source(_mdr_claim(
            'C-3333333333', 'divorce', [_MDR_HFA, _MDR_HMO], '1900',
            spouse_roles=[_MDR_HFA], other_roles={'parent': [_MDR_HMO]}))
        index.build_index(self.root, {})
        self.assertIsNone(
            self._date_end(_MDR_HFA, _MDR_HMO),
            'a decree that calls one of its two people a parent has not said '
            "whose marriage ended, so it must not close anybody's")
        self.assertIsNone(self._date_end(_MDR_HMO, _MDR_HFA))


# ── Unreadable records (#62) ──────────────────────────────────────────────────

_U62_PERSON_A = 'P-aaaaaaaaaa'
_U62_PERSON_B = 'P-bbbbbbbbbb'
_U62_SID = 'S-1111111111'

_U62_SOURCE_HEAD = """---
id: {sid}
title: Marriage notice
source_type: newspaper
---

## Claims

```yaml
"""

_U62_GOOD_CLAIMS = """- id: C-1111111111
  type: marriage
  persons: [P-aaaaaaaaaa, P-bbbbbbbbbb]
  roles:
    spouse: [P-aaaaaaaaaa, P-bbbbbbbbbb]
  value: married 1871
  date: "1871"
  status: accepted
  reviewed: 2026-01-01
- id: C-2222222222
  type: residence
  persons: [P-aaaaaaaaaa]
  value: lived in Kansas
  status: accepted
  reviewed: 2026-01-01
```
"""

# The same two claims, with one line indented a space too far.  This is the
# ordinary way a hand-edit breaks a claims block: YAML refuses the whole list,
# so BOTH claims are lost, not only the mistyped one.
_U62_BROKEN_CLAIMS = _U62_GOOD_CLAIMS.replace(
    '  persons: [P-aaaaaaaaaa]\n', '   persons: [P-aaaaaaaaaa]\n')


class UnreadableRecordBuildTests(unittest.TestCase):
    """A claims block the build cannot parse must not vanish in silence (#62).

    `read_record` reports malformed YAML through `parse_errors` and hands back
    `claims: []`, so before this the block was simply dropped: `fha index`
    printed nothing and exited 0 while the claims - and every relationship edge
    derived from them - were gone from the index.  A field report of this cost
    16% of an archive's spouse edges (166 -> 140) with the tools reporting
    success throughout.

    The drop is correct and stays: YAML that will not parse cannot be indexed,
    and guessing at it would be worse than losing it.  What these tests pin is
    that the build SAYS so - naming the file, what the loss costs, and the way
    back - and ends on the documented warnings exit (TOOLING §1: 1 = warnings
    only) rather than a clean 0.  Warning and not error because `fha index`
    already exits 1 for the strictly worse case of a folder it could not open
    at all, and because the index it built is usable: the repair is in the
    human's own file, and `fha lint` (E010) is the command that names the spot.
    """

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        (self.root / 'fha.yaml').write_text(
            'roots:\n  documents: documents\n', encoding='utf-8')
        (self.root / 'sources').mkdir()
        (self.root / 'people').mkdir()
        for pid, name in ((_U62_PERSON_A, 'Alice Hartley'),
                          (_U62_PERSON_B, 'Ben Hartley')):
            (self.root / 'people' / f'hartley__x_{pid}.md').write_text(
                f'---\nid: {pid}\nname: {name}\nliving: false\n'
                f'tier: curated\n---\n\n## Biography\n\nx\n',
                encoding='utf-8')
        self.source_path = self.root / 'sources' / f'notice_{_U62_SID}.md'

    def _write_source(self, claims_yaml: str) -> None:
        self.source_path.write_text(
            _U62_SOURCE_HEAD.format(sid=_U62_SID) + claims_yaml,
            encoding='utf-8')

    def _counts(self) -> tuple[int, int]:
        """(claims, spouse edges) as the index holds them - the two numbers
        issue #62 measured going 1 -> 0 and 2 -> 0."""
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        try:
            claims = conn.execute('SELECT count(*) FROM claims').fetchone()[0]
            spouse = conn.execute(
                "SELECT count(*) FROM relationships WHERE rel='spouse'"
            ).fetchone()[0]
        finally:
            conn.close()
        return claims, spouse

    def test_well_formed_claims_build_clean_and_silent(self) -> None:
        # The control, and the guard against new noise: an archive with nothing
        # wrong still exits 0 with an empty message list.
        self._write_source(_U62_GOOD_CLAIMS)
        result = index.build_index(self.root, {})
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual([m.text for m in result.messages], [])
        self.assertEqual(result['unreadable_records'], [])
        self.assertEqual(self._counts(), (2, 2))

    def test_malformed_claims_block_is_reported_and_exits_1(self) -> None:
        self._write_source(_U62_GOOD_CLAIMS)
        index.build_index(self.root, {})
        self.assertEqual(self._counts(), (2, 2))

        self._write_source(_U62_BROKEN_CLAIMS)
        result = index.build_index(self.root, {})

        # The claims are still dropped - that half was never the bug.
        self.assertEqual(self._counts(), (0, 0))
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result['unreadable_records'],
                         [f'sources/notice_{_U62_SID}.md'])
        self.assertEqual(len(result.messages), 1)
        msg = result.messages[0]
        self.assertEqual(msg.level, 'warning')
        # read_record's own finding code, passed through rather than reinvented,
        # so index and lint never name one broken file two different ways.
        self.assertEqual(msg.code, 'E010')
        self.assertEqual(msg.path, f'sources/notice_{_U62_SID}.md')
        self.assertIn(f'notice_{_U62_SID}.md', msg.text)
        self.assertIn('claims could not be read', msg.text)
        self.assertIn('fha index', msg.text)      # the way back
        self.assertIn('E010', msg.text)           # where to see the same spot
        # Index output can end up in a committed `fha report`, so it carries no
        # local absolute path.
        self.assertNotIn(str(self.root), msg.text)
        # `fha report` writes these messages into a markdown bullet list, and
        # the worked example inside the parser's explanation is itself a YAML
        # list. It stays inside its own bullet only while every continuation
        # line is indented; unindented it reads as four more findings.
        for line in msg.text.splitlines()[1:]:
            self.assertTrue(line.startswith('  '), repr(line))

    def test_malformed_frontmatter_loses_the_whole_source_and_says_which(self) -> None:
        # The frontmatter half of the same silence: with no readable `id:` the
        # source never reaches the index at all, so the warning must not say
        # that only its claims were lost.
        self.source_path.write_text(
            f'---\nid: {_U62_SID}\n  title: Marriage notice\n---\n',
            encoding='utf-8')
        result = index.build_index(self.root, {})
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result['sources'], 1)   # walked, but not indexed
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        try:
            self.assertEqual(
                conn.execute('SELECT count(*) FROM sources').fetchone()[0], 0)
        finally:
            conn.close()
        self.assertIn('not in the index at all', result.messages[0].text)

    def test_malformed_person_frontmatter_is_reported(self) -> None:
        # Symmetry: `_index_person` reads through the same `read_record`, and a
        # person whose frontmatter will not parse keeps its filename P-id and
        # indexes with name 'unknown' - present in the table, blank in every
        # query built on it.  That is worse than absent, and was equally silent.
        self._write_source(_U62_GOOD_CLAIMS)
        (self.root / 'people' / f'hartley__x_{_U62_PERSON_A}.md').write_text(
            f'---\nid: {_U62_PERSON_A}\nname: Alice Hartley\n living: false\n---\n',
            encoding='utf-8')
        result = index.build_index(self.root, {})
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result['unreadable_records'],
                         [f'people/hartley__x_{_U62_PERSON_A}.md'])
        self.assertIn('frontmatter could not be read', result.messages[0].text)

    def test_upsert_reports_the_same_block_as_the_full_rebuild(self) -> None:
        # Full rebuild and incremental upsert must answer identically: the
        # upsert DELETEs the source's claim rows first, so it can lose a claims
        # block exactly as silently.
        self._write_source(_U62_GOOD_CLAIMS)
        index.build_index(self.root, {})
        self.assertEqual(self._counts(), (2, 2))

        self._write_source(_U62_BROKEN_CLAIMS)
        collected: list[tuple[str, str, str]] = []
        status = index.upsert_source(
            self.root, {}, _U62_SID.lower(),
            index._parse_error_recorder(collected, self.root),
        )
        self.assertEqual(status, 'indexed')
        self.assertEqual(self._counts(), (0, 0))
        self.assertEqual(len(collected), 1)
        rel, code, text = collected[0]
        self.assertEqual(rel, f'sources/notice_{_U62_SID}.md')
        self.assertEqual(code, 'E010')
        self.assertIn('claims could not be read', text)

    def test_cli_prints_the_warning_and_returns_1(self) -> None:
        self._write_source(_U62_BROKEN_CLAIMS)
        args = argparse.Namespace(root=str(self.root), source=None, verbose=False)
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = index._run_index(args)
        self.assertEqual(code, EXIT_WARNINGS)
        self.assertIn('WARNING:', err.getvalue())
        self.assertIn(f'notice_{_U62_SID}.md', err.getvalue())

    def test_cli_source_mode_prints_the_warning_and_returns_1(self) -> None:
        self._write_source(_U62_GOOD_CLAIMS)
        index.build_index(self.root, {})
        self._write_source(_U62_BROKEN_CLAIMS)
        args = argparse.Namespace(
            root=str(self.root), source=_U62_SID, verbose=False)
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = index._run_index(args)
        self.assertEqual(code, EXIT_WARNINGS)
        self.assertIn(f'notice_{_U62_SID}.md', err.getvalue())

    def test_cli_stays_clean_and_quiet_on_a_good_archive(self) -> None:
        self._write_source(_U62_GOOD_CLAIMS)
        args = argparse.Namespace(root=str(self.root), source=None, verbose=False)
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = index._run_index(args)
        self.assertEqual(code, EXIT_CLEAN)
        self.assertEqual(err.getvalue(), '')

if __name__ == '__main__':
    unittest.main()


class UndecodableFileBuildTests(unittest.TestCase):
    """A file that is not UTF-8 must not take the whole index down (#62 class).

    Sibling of `UnreadableRecordBuildTests`, found by sweeping for the same
    shape.  Three reads in this module - each note, `notes/research-log.md`,
    and `.cache/capture_log.jsonl` - guarded only `except OSError`.
    `UnicodeDecodeError` is a **ValueError**, not an OSError, so a file saved in
    the machine's own codepage sailed straight past the guard and out of
    `build_index`: an unhandled traceback, and NOTHING indexed at all.  Strictly
    worse than the silent drop #62 was filed for, and reachable by writing one
    note in a Windows editor - cp1252 is its default, and the names this archive
    is full of (Krakow, Muller, nee) are exactly the bytes that differ.

    That the codebase already knew is what makes it a miss rather than an
    oversight: the `dump_text` read in this same file catches
    `UnicodeDecodeError` by name.  Three siblings never got the same treatment.

    Pinned here: the build survives, the rest of the archive still indexes, the
    file is named with what its loss costs, and the run ends on the documented
    warnings exit rather than 0 or a crash.  The file is never rewritten - it is
    the human's, and it is not damaged, only encoded differently.
    """

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        (self.root / 'fha.yaml').write_text(
            'roots:\n  documents: documents\n', encoding='utf-8')
        (self.root / 'notes').mkdir()
        (self.root / 'sources').mkdir()
        (self.root / 'people').mkdir()

    def _cp1252(self, rel: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes('# Note\n\nGrandma in Krak\u00f3w, n\u00e9e M\u00fcller.\n'.encode('cp1252'))
        return p

    def test_a_cp1252_note_does_not_crash_the_build(self) -> None:
        self._cp1252('notes/research.md')
        result = index.build_index(self.root, {'roots': {'documents': 'documents'}})
        self.assertEqual(1, result.exit_code,
                         'a file that will not decode is a warning, not a clean run')

    def test_the_rest_of_the_archive_still_indexes(self) -> None:
        self._cp1252('notes/research.md')
        (self.root / 'notes' / 'good.md').write_text(
            '# Good\n\nfindable words here\n', encoding='utf-8')
        index.build_index(self.root, {'roots': {'documents': 'documents'}})
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        try:
            rows = conn.execute(
                "SELECT count(*) FROM notes_fts WHERE content LIKE '%findable%'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(1, rows,
                         'one undecodable note must not cost the notes that DO decode')

    def test_the_message_names_the_file_and_the_loss(self) -> None:
        self._cp1252('notes/research.md')
        result = index.build_index(self.root, {'roots': {'documents': 'documents'}})
        text = '\n'.join(m.text for m in result.messages)
        self.assertIn('notes/research.md', text)
        self.assertIn('UTF-8', text)
        self.assertNotIn(str(self.root), text,
                         'a local absolute path has no business in a committed report')

    def test_an_undecodable_research_log_is_reported(self) -> None:
        self._cp1252('notes/research-log.md')
        result = index.build_index(self.root, {'roots': {'documents': 'documents'}})
        self.assertEqual(1, result.exit_code)
        self.assertIn('research-log.md',
                      '\n'.join(m.text for m in result.messages))

    def test_an_undecodable_capture_log_is_reported(self) -> None:
        self._cp1252('.cache/capture_log.jsonl')
        result = index.build_index(self.root, {'roots': {'documents': 'documents'}})
        self.assertEqual(1, result.exit_code)
        self.assertIn('capture_log.jsonl',
                      '\n'.join(m.text for m in result.messages))

    def test_the_file_is_never_rewritten(self) -> None:
        p = self._cp1252('notes/research.md')
        before = p.read_bytes()
        index.build_index(self.root, {'roots': {'documents': 'documents'}})
        self.assertEqual(before, p.read_bytes(),
                         'the note is the human\'s and is not damaged - never touch it')

    def test_a_clean_archive_still_exits_zero(self) -> None:
        (self.root / 'notes' / 'fine.md').write_text('# Fine\n\nwords\n', encoding='utf-8')
        result = index.build_index(self.root, {'roots': {'documents': 'documents'}})
        self.assertEqual(0, result.exit_code, 'no new noise on a clean archive')
