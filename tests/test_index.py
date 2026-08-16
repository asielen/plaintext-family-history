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


if __name__ == '__main__':
    unittest.main()
