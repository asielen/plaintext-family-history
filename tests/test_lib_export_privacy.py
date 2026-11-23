"""One reader for what an export must withhold (`_lib.export_privacy`).

A GEDCOM file and a printed report for a relative must agree about who is
private, so every exporter that is not the site asks `_lib` the same question:
which people, sources and claims their record files mark `restricted`, which
files could not be read to tell (withheld, and named so he can fix them), and
whether one person is kept out (living, unknown or restricted, merges
followed). These tests pin that reader on a real `fha index` build, so the
rows under test are the ones the indexer keeps.
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import index  # noqa: E402
from _lib import (  # noqa: E402
    ExportPrivacy,
    export_privacy,
    export_tie_ok,
    person_restricted,
    person_withheld,
    public_source_sql,
)

KIT = 'p-0000000001'        # deceased, public
ADA = 'p-0000000002'        # deceased, restricted: by-request
LIV = 'p-0000000003'        # living: true
UNK = 'p-0000000004'        # living: unknown
OLD = 'p-0000000005'        # a merged duplicate of KIT


def _person(pid: str, name: str, extra: str = '', living: str = 'false') -> str:
    return (f'---\nid: {pid}\nname: {name}\ntier: curated\n'
            f'living: {living}\n{extra}---\n# {name}\n')


def _source(sid: str, title: str, claims: str, extra: str = '') -> str:
    return (f'---\nid: {sid}\ntitle: {title}\nsource_type: vital-record\n{extra}'
            f'citation: "{title}."\n---\n\n## Claims\n```yaml\n{claims}```\n')


def _claim(cid: str, persons: str, extra: str = '') -> str:
    return (f'- id: {cid}\n  type: birth\n  date: 1890\n  persons: [{persons}]\n'
            f'  value: born\n  status: accepted\n{extra}')


class _Archive(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        # Registered first so it runs last, after every connection is closed.
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / 'fha.yaml').write_text('archive_name: test\n', encoding='utf-8')
        self._write('people/zabrowski__kit_P-0000000001.md',
                    _person('P-0000000001', 'Kit Zabrowski'))
        self._write('people/quillfeather__ada_P-0000000002.md',
                    _person('P-0000000002', 'Ada Quillfeather', 'restricted: by-request\n'))
        self._write('people/zabrowski__liv_P-0000000003.md',
                    _person('P-0000000003', 'Liv Zabrowski', living='true'))
        self._write('people/zabrowski__una_P-0000000004.md',
                    _person('P-0000000004', 'Una Zabrowski', living='unknown'))

    def _write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def _index(self) -> None:
        index.build_index(self.root, {})

    def _conn(self) -> sqlite3.Connection:
        if getattr(self, '_open', None) is None:
            self._open = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
            self._open.row_factory = sqlite3.Row
            self.addCleanup(self._open.close)
        return self._open

    def _privacy(self) -> ExportPrivacy:
        return export_privacy(self._conn(), self.root)

    def _lookup(self, conn):
        rows = {r['id']: r for r in conn.execute('SELECT * FROM persons')}
        return rows.get


class ExportPrivacyReaderTests(_Archive):
    def test_a_by_request_person_is_restricted_and_a_public_one_is_not(self):
        self._index()
        p = self._privacy()
        self.assertIn(ADA, p.restricted_persons)
        self.assertNotIn(KIT, p.restricted_persons)
        # Living is not `restricted`: the living rule is person_withheld's.
        self.assertNotIn(LIV, p.restricted_persons)
        self.assertEqual(p.unreadable_persons, [])

    def test_an_unreadable_person_file_is_restricted_and_listed(self):
        # The unclosed quote is the hand-edit slip that once exported a
        # by-request relative by surname: fail closed, and name the file.
        self._write('people/quillfeather__ada_P-0000000002.md',
                    '---\nid: P-0000000002\nname: "Ada Quillfeather\ntier: curated\n'
                    'living: false\n---\n# Ada\n')
        self._index()
        p = self._privacy()
        self.assertIn(ADA, p.restricted_persons)
        self.assertEqual([Path(x).as_posix() for x in p.unreadable_persons],
                         ['people/quillfeather__ada_P-0000000002.md'])

    def test_an_absent_file_reads_as_no_marker(self):
        # A file gone since the last `fha index` is staleness, not damage.
        self._index()
        (self.root / 'people' / 'quillfeather__ada_P-0000000002.md').unlink()
        p = self._privacy()
        self.assertNotIn(ADA, p.restricted_persons)
        self.assertEqual(p.unreadable_persons, [])

    def test_a_by_request_source_is_read_from_the_file(self):
        # The file is the truth, not the index row: marked by-request after
        # the last `fha index`, the source is withheld all the same.
        letter = self._write('sources/letter_S-0000000001.md', _source(
            'S-0000000001', 'Family letter', _claim('C-0000000001', 'P-0000000001')))
        self._write('sources/birth_S-0000000002.md', _source(
            'S-0000000002', 'Kit birth', _claim('C-0000000002', 'P-0000000001')))
        self._index()
        letter.write_text(letter.read_text(encoding='utf-8').replace(
            'source_type:', 'restricted: by-request\nsource_type:'), encoding='utf-8')
        conn = self._conn()
        self.assertEqual(conn.execute(
            "SELECT restricted FROM sources WHERE id = 's-0000000001'").fetchone()[0], 0)
        p = self._privacy()
        self.assertIn('s-0000000001', p.restricted_sources)
        self.assertNotIn('s-0000000002', p.restricted_sources)
        self.assertEqual(p.unreadable_sources, [])

    def test_an_unreadable_source_is_withheld_whole_and_listed(self):
        self._write('sources/birth_S-0000000001.md', _source(
            'S-0000000001', 'Kit birth', _claim('C-0000000001', 'P-0000000001')))
        self._index()
        path = self.root / 'sources' / 'birth_S-0000000001.md'
        path.write_text(path.read_text(encoding='utf-8').replace('title: Kit', 'title: "Kit'),
                        encoding='utf-8')
        p = self._privacy()
        self.assertIn('s-0000000001', p.restricted_sources)
        self.assertEqual([(sid, Path(x).as_posix()) for sid, x in p.unreadable_sources],
                         [('s-0000000001', 'sources/birth_S-0000000001.md')])
        self.assertIn('c-0000000001', p.restricted_claims)

    def test_a_claim_marked_restricted_in_its_source_is_withheld(self):
        self._write('sources/birth_S-0000000001.md', _source(
            'S-0000000001', 'Kit birth',
            _claim('C-0000000001', 'P-0000000001', '  restricted: true\n')
            + _claim('C-0000000002', 'P-0000000001')))
        self._index()
        p = self._privacy()
        self.assertEqual(p.restricted_claims, {'c-0000000001'})
        self.assertNotIn('s-0000000001', p.restricted_sources)

    def test_a_claims_block_that_will_not_parse_withholds_its_claims_not_the_source(self):
        self._write('sources/birth_S-0000000001.md', _source(
            'S-0000000001', 'Kit birth', _claim('C-0000000001', 'P-0000000001')))
        self._index()
        path = self.root / 'sources' / 'birth_S-0000000001.md'
        path.write_text(path.read_text(encoding='utf-8').replace('value: born', 'value: "born'),
                        encoding='utf-8')
        p = self._privacy()
        self.assertIn('c-0000000001', p.restricted_claims)
        self.assertNotIn('s-0000000001', p.restricted_sources)
        self.assertEqual(p.unreadable_sources, [])

    def test_a_restricted_merged_duplicate_withholds_the_person_it_was_merged_into(self):
        self._write('people/zabrowski__kit_old_P-0000000005.md', _person(
            'P-0000000005', 'Kit Zabrowsky',
            'restricted: by-request\nstatus: merged\nmerged_into: P-0000000001\n'))
        self._index()
        p = self._privacy()
        self.assertIn(OLD, p.restricted_persons)
        self.assertIn(KIT, p.restricted_persons)

    def test_public_source_sql_keeps_out_restricted_dna_and_not_for_publication(self):
        self._write('sources/a_S-0000000001.md', _source(
            'S-0000000001', 'Open', _claim('C-0000000001', 'P-0000000001')))
        self._write('sources/b_S-0000000002.md', _source(
            'S-0000000002', 'Closed', _claim('C-0000000002', 'P-0000000001'),
            extra='restricted: true\n'))
        self._write('sources/c_S-0000000003.md', _source(
            'S-0000000003', 'Not for print', _claim('C-0000000003', 'P-0000000001'),
            extra='rights:\n  publication_ok: false\n'))
        self._write('sources/d_S-0000000004.md', _source(
            'S-0000000004', 'Match list', _claim('C-0000000004', 'P-0000000001'))
            .replace('source_type: vital-record', 'source_type: dna'))
        self._index()
        conn = self._conn()
        shown = {r[0] for r in conn.execute(
            f'SELECT s.id FROM sources s WHERE {public_source_sql()}')}
        self.assertEqual(shown, {'s-0000000001'})
        aliased = {r[0] for r in conn.execute(
            f'SELECT src.id FROM sources src WHERE {public_source_sql("src")}')}
        self.assertEqual(aliased, shown)


class PersonWithheldTests(_Archive):
    def _withheld(self, pid: str) -> bool:
        conn = self._conn()
        lookup = self._lookup(conn)
        return person_withheld(conn, lookup(pid), export_privacy(conn, self.root), lookup)

    def test_living_unknown_and_restricted_people_are_withheld(self):
        self._index()
        self.assertFalse(self._withheld(KIT))
        self.assertTrue(self._withheld(ADA))
        self.assertTrue(self._withheld(LIV))
        self.assertTrue(self._withheld(UNK))

    def test_a_merged_duplicate_that_says_living_withholds_the_survivor(self):
        self._write('people/zabrowski__kit_old_P-0000000005.md', _person(
            'P-0000000005', 'Kit Zabrowsky', 'status: merged\nmerged_into: P-0000000001\n',
            living='true'))
        self._index()
        self.assertTrue(self._withheld(KIT))
        self.assertTrue(self._withheld(OLD))

    def test_an_old_id_is_held_to_the_living_person_it_was_merged_into(self):
        self._write('people/zabrowski__liv_old_P-0000000005.md', _person(
            'P-0000000005', 'Liv Old', 'status: merged\nmerged_into: P-0000000003\n'))
        self._index()
        self.assertTrue(self._withheld(OLD))

    def test_a_broken_merge_chain_is_withheld(self):
        self._write('people/zabrowski__kit_old_P-0000000005.md', _person(
            'P-0000000005', 'Kit Zabrowsky', 'status: merged\nmerged_into: P-0000000099\n'))
        self._index()
        self.assertTrue(self._withheld(OLD))


class PersonRestrictedTests(_Archive):
    """The narrower question a relationship walk asks (owner decision D16): a
    tie may pass through a living or unknown relative, never through someone
    who asked to be left out."""

    def _restricted(self, pid: str) -> bool:
        conn = self._conn()
        lookup = self._lookup(conn)
        return person_restricted(lookup(pid), export_privacy(conn, self.root), lookup)

    def test_only_a_restricted_person_counts_not_a_living_one(self):
        self._index()
        self.assertTrue(self._restricted(ADA))
        self.assertFalse(self._restricted(LIV))
        self.assertFalse(self._restricted(UNK))
        self.assertFalse(self._restricted(KIT))

    def test_an_old_id_of_a_restricted_person_is_restricted(self):
        self._write('people/quillfeather__ada_old_P-0000000005.md', _person(
            'P-0000000005', 'Ada Old', 'status: merged\nmerged_into: P-0000000002\n'))
        self._index()
        self.assertTrue(self._restricted(OLD))


class ExportTieTests(_Archive):
    """`export_tie_ok`: may a walk use this family tie (D16, SPEC §19, §21)."""

    MUM = 'p-0000000006'      # deceased mother of LIV

    def setUp(self):
        super().setUp()
        self._write('people/zabrowski__mum_P-0000000006.md',
                    _person('P-0000000006', 'Mum Zabrowski'))

    def _tie(self, child: str, parent: str, claim_id: str | None, **kw) -> bool:
        conn = self._conn()
        conn.execute('INSERT INTO relationships(person_id, rel, other_id, claim_id) '
                     'VALUES (?,?,?,?)', (child, 'parent', parent, claim_id))
        row = conn.execute('SELECT * FROM relationships WHERE person_id = ? AND other_id = ?',
                           (child, parent)).fetchone()
        lookup = self._lookup(conn)
        return export_tie_ok(conn, row, export_privacy(conn, self.root), lookup, **kw)

    def _seed(self, persons: str, claim_extra: str = '', source_extra: str = '',
              source_type: str = 'vital-record') -> None:
        self._write('sources/rel_S-0000000001.md', _source(
            'S-0000000001', 'Baptism', _claim('C-0000000001', persons, claim_extra),
            extra=source_extra).replace('source_type: vital-record',
                                        f'source_type: {source_type}'))
        self._index()

    def test_a_tie_through_a_living_person_may_be_walked(self):
        self._seed('P-0000000003, P-0000000006')
        self.assertTrue(self._tie(LIV, self.MUM, 'c-0000000001'))

    def test_a_tie_to_a_restricted_person_is_cut(self):
        self._seed('P-0000000002, P-0000000006')
        self.assertFalse(self._tie(ADA, self.MUM, 'c-0000000001'))
        self.assertFalse(self._tie(self.MUM, ADA, 'c-0000000001'))
        # The person at the end cuts it even with no claim to judge.
        self.assertFalse(self._tie(ADA, KIT, None))

    def test_a_tie_whose_claim_names_a_restricted_person_is_cut(self):
        self._seed('P-0000000003, P-0000000006, P-0000000002')
        self.assertFalse(self._tie(LIV, self.MUM, 'c-0000000001'))

    def test_a_tie_on_a_source_naming_a_restricted_person_is_cut(self):
        self._write('sources/rel_S-0000000001.md', _source(
            'S-0000000001', 'Baptism', _claim('C-0000000001', 'P-0000000003, P-0000000006')
            + _claim('C-0000000002', 'P-0000000002')))
        self._index()
        self.assertFalse(self._tie(LIV, self.MUM, 'c-0000000001'))

    def test_a_tie_resting_on_a_restricted_claim_is_cut(self):
        self._seed('P-0000000003, P-0000000006', claim_extra='  restricted: true\n')
        self.assertFalse(self._tie(LIV, self.MUM, 'c-0000000001'))

    def test_a_tie_from_a_dna_or_private_source_is_cut(self):
        self._seed('P-0000000003, P-0000000006', source_type='dna')
        self.assertFalse(self._tie(LIV, self.MUM, 'c-0000000001'))

    def test_a_tie_from_a_by_request_source_is_cut(self):
        self._seed('P-0000000003, P-0000000006', source_extra='restricted: by-request\n')
        self.assertFalse(self._tie(LIV, self.MUM, 'c-0000000001'))

    def test_a_tie_from_an_unaccepted_claim_is_cut(self):
        self._write('sources/rel_S-0000000001.md', _source(
            'S-0000000001', 'Baptism', _claim('C-0000000001', 'P-0000000003, P-0000000006')
            .replace('status: accepted', 'status: suggested')))
        self._index()
        self.assertFalse(self._tie(LIV, self.MUM, 'c-0000000001'))

    def test_a_tie_with_no_claim_is_the_callers_choice(self):
        # A hand-written relationships: entry has nothing to cite. GEDCOM and
        # the site keep it; a printed report, which cites every fact, may not.
        self._index()
        self.assertTrue(self._tie(KIT, self.MUM, None))
        self.assertFalse(self._tie(UNK, self.MUM, None, unclaimed_ok=False))


if __name__ == '__main__':
    unittest.main()
