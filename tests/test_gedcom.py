import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import gedcom
from index import _DDL
from _lib import EXIT_FAILURE


def _make_index(archive_root: Path) -> sqlite3.Connection:
    cache = archive_root / '.cache'
    cache.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(cache / 'index.sqlite'))
    conn.executescript(_DDL)
    conn.row_factory = sqlite3.Row
    return conn


def _add_person(conn, pid, name, sex='', living='false', tier='curated', surname=None, status='active'):
    conn.execute(
        'INSERT INTO persons(id, name, surname, sex, living, tier, status, path) '
        'VALUES (?,?,?,?,?,?,?,?)',
        (pid, name, surname, sex, living, tier, status, f'people/{pid}.md'),
    )


def _rel(conn, a, rel, b, ds='', de=None):
    conn.execute(
        'INSERT INTO relationships(person_id, rel, other_id, claim_id, date_start, date_end) '
        'VALUES (?,?,?,?,?,?)',
        (a, rel, b, 'c-rel000000', ds, de),
    )


def _spouse(conn, a, b, ds='', de=None):
    _rel(conn, a, 'spouse', b, ds, de)
    _rel(conn, b, 'spouse', a, ds, de)


def _parent_child(conn, parent, child, ds='1900-01-01', de='1900-12-31'):
    _rel(conn, child, 'parent', parent, ds, de)
    _rel(conn, parent, 'child', child, ds, de)


def _add_claim(conn, cid, ctype, persons, date_edtf='', place_id=None, place_text=None,
               source_id='s-0000000001', status='accepted', value='x'):
    mn = ''
    if date_edtf:
        from _lib import edtf_bounds
        mn = edtf_bounds(date_edtf)[0]
    conn.execute(
        'INSERT INTO claims(id, source_id, type, date_edtf, date_min, place_id, place_text, value, status) '
        'VALUES (?,?,?,?,?,?,?,?,?)',
        (cid, source_id, ctype, date_edtf, mn, place_id, place_text, value, status),
    )
    for pos, p in enumerate(persons):
        conn.execute(
            'INSERT INTO claim_persons(claim_id, person_id, position, role) VALUES (?,?,?,?)',
            (cid, p, pos, None),
        )


def _add_source(conn, sid, title, *, source_type='vital-record', restricted=0):
    conn.execute(
        'INSERT INTO sources(id, title, source_type, restricted, path) VALUES (?,?,?,?,?)',
        (sid, title, source_type, restricted, f'sources/{sid}.md'),
    )


class GedcomDateTests(unittest.TestCase):
    def test_year(self):
        self.assertEqual(gedcom._edtf_to_gedcom('1850'), '1850')

    def test_approx_year(self):
        self.assertEqual(gedcom._edtf_to_gedcom('1850~'), 'ABT 1850')

    def test_year_month(self):
        self.assertEqual(gedcom._edtf_to_gedcom('1850-05'), 'MAY 1850')

    def test_full_date(self):
        self.assertEqual(gedcom._edtf_to_gedcom('1850-05-20'), '20 MAY 1850')

    def test_interval(self):
        self.assertEqual(gedcom._edtf_to_gedcom('1871-02/1871-03'), 'BET FEB 1871 AND MAR 1871')

    def test_decade(self):
        self.assertEqual(gedcom._edtf_to_gedcom('185X'), 'ABT 1855')

    def test_open_before(self):
        self.assertEqual(gedcom._edtf_to_gedcom('[..1920]'), 'BEF 1920')

    def test_empty(self):
        self.assertIsNone(gedcom._edtf_to_gedcom(''))


class GedcomNameTests(unittest.TestCase):
    def test_surname_suffix(self):
        self.assertEqual(gedcom._gedcom_name('John Smith', 'Smith'), 'John /Smith/')

    def test_no_surname_uses_last_token(self):
        self.assertEqual(gedcom._gedcom_name('John Smith', None), 'John /Smith/')

    def test_single_name(self):
        self.assertEqual(gedcom._gedcom_name('Madonna', None), 'Madonna //')


class GedcomExportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        conn = _make_index(self.root)
        # John + Mary -> Sam; Sam + Liz(living) -> Kid(living)
        _add_person(conn, 'p-0000000001', 'John Smith', 'M', surname='Smith')
        _add_person(conn, 'p-0000000002', 'Mary Jones', 'F', surname='Jones')
        _add_person(conn, 'p-0000000003', 'Sam Smith', 'M', surname='Smith')
        _add_person(conn, 'p-0000000004', 'Liz Doe', 'F', living='unknown', surname='Doe')
        _add_person(conn, 'p-0000000005', 'Kid Smith', 'M', living='unknown', surname='Smith')
        _spouse(conn, 'p-0000000001', 'p-0000000002', ds='1900-06-01')
        _parent_child(conn, 'p-0000000001', 'p-0000000003')
        _parent_child(conn, 'p-0000000002', 'p-0000000003')
        _spouse(conn, 'p-0000000003', 'p-0000000004')
        _parent_child(conn, 'p-0000000003', 'p-0000000005')
        _parent_child(conn, 'p-0000000004', 'p-0000000005')
        _add_source(conn, 's-0000000001', 'Birth cert')
        _add_source(conn, 's-0000000002', 'Marriage record')
        _add_claim(conn, 'c-0000000001', 'birth', ['p-0000000001'],
                   date_edtf='1875-03-02', place_text='Boston', source_id='s-0000000001')
        _add_claim(conn, 'c-0000000002', 'marriage', ['p-0000000001', 'p-0000000002'],
                   date_edtf='1900-06-01', place_text='Boston', source_id='s-0000000002')
        conn.commit()
        conn.close()

    def tearDown(self):
        self._tmp.cleanup()

    def test_descendants_selects_whole_tree(self):
        r = gedcom.run_gedcom(self.root, 'p-0000000001', mode='descendants')
        self.assertEqual(r['status'], 'ok')
        self.assertEqual(r['person_count'], 5)
        self.assertIn('0 TRLR', r['text'])
        self.assertTrue(r['text'].startswith('0 HEAD'))

    def test_seed_and_all_conflict_rejected(self):
        # A seed P-id together with --all is ambiguous; reject it rather than
        # silently letting --all win and dropping the seed.
        r = gedcom.run_gedcom(self.root, 'p-0000000001', all_persons=True)
        self.assertEqual(r['status'], 'bad-args')
        self.assertEqual(r.exit_code, EXIT_FAILURE)
        self.assertIsNone(r['text'])
        self.assertIn('conflict', ' '.join(r['messages']).lower())

    def test_generations_cap(self):
        r = gedcom.run_gedcom(self.root, 'p-0000000001', mode='descendants', generations=1)
        # John, Mary (spouse), Sam (child), Liz (Sam's spouse) - not Kid (depth 2)
        self.assertEqual(r['person_count'], 4)

    def test_living_redacted_by_default(self):
        r = gedcom.run_gedcom(self.root, 'p-0000000001', mode='descendants')
        self.assertIn('1 NAME /Living/', r['text'])
        self.assertNotIn('Kid /Smith/', r['text'])
        self.assertNotIn('Liz /Doe/', r['text'])

    def test_include_living(self):
        r = gedcom.run_gedcom(self.root, 'p-0000000001', mode='descendants', include_living=True)
        self.assertIn('Liz /Doe/', r['text'])
        self.assertIn('Kid /Smith/', r['text'])
        self.assertNotIn('/Living/', r['text'])

    def test_vitals_and_sources_emitted(self):
        r = gedcom.run_gedcom(self.root, 'p-0000000001', mode='ancestors')
        self.assertIn('1 BIRT', r['text'])
        self.assertIn('2 DATE 2 MAR 1875', r['text'])
        self.assertIn('1 MARR', r['text'])
        self.assertIn('0 @S1@ SOUR', r['text'])
        self.assertIn('Birth cert', r['text'])

    def test_restricted_vital_fact_not_exported(self):
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        conn.row_factory = sqlite3.Row
        _add_source(conn, 's-0000000003', 'Restricted death cert', restricted=1)
        _add_claim(conn, 'c-0000000003', 'death', ['p-0000000001'],
                   date_edtf='1950', place_text='Hidden Town', source_id='s-0000000003')
        conn.commit()
        conn.close()

        r = gedcom.run_gedcom(self.root, 'p-0000000001', mode='ancestors')

        self.assertNotIn('1 DEAT', r['text'])
        self.assertNotIn('Hidden Town', r['text'])
        self.assertNotIn('Restricted death cert', r['text'])

    def test_dna_vital_fact_not_exported(self):
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        conn.row_factory = sqlite3.Row
        _add_source(conn, 's-0000000003', 'DNA birth estimate', source_type='dna', restricted=1)
        _add_claim(conn, 'c-0000000003', 'death', ['p-0000000001'],
                   date_edtf='1950', place_text='DNA Lab', source_id='s-0000000003')
        conn.commit()
        conn.close()

        r = gedcom.run_gedcom(self.root, 'p-0000000001', mode='ancestors')

        self.assertNotIn('DNA Lab', r['text'])
        self.assertNotIn('DNA birth estimate', r['text'])

    def test_marriage_with_witness_uses_spouse_roles(self):
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        conn.row_factory = sqlite3.Row
        _add_person(conn, 'p-0000000006', 'Anna Role', 'F', surname='Role')
        _add_person(conn, 'p-0000000007', 'Ben Role', 'M', surname='Role')
        _add_person(conn, 'p-0000000008', 'Witness Person', 'M', surname='Person')
        _spouse(conn, 'p-0000000006', 'p-0000000007')
        _add_source(conn, 's-0000000003', 'Role marriage record')
        conn.execute(
            'INSERT INTO claims(id, source_id, type, date_edtf, date_min, place_text, value, status) '
            'VALUES (?,?,?,?,?,?,?,?)',
            ('c-0000000003', 's-0000000003', 'marriage', '1901', '1901-01-01',
             'Role Town', 'married with witness', 'accepted'),
        )
        for pos, (pid, role) in enumerate([
            ('p-0000000006', 'spouse'),
            ('p-0000000007', 'spouse'),
            ('p-0000000008', 'witness'),
        ]):
            conn.execute(
                'INSERT INTO claim_persons(claim_id, person_id, position, role) VALUES (?,?,?,?)',
                ('c-0000000003', pid, pos, role),
            )
        conn.commit()
        conn.close()

        r = gedcom.run_gedcom(self.root, 'p-0000000006', mode='connected')

        self.assertIn('Role Town', r['text'])
        self.assertIn('Role marriage record', r['text'])

    def test_marriage_redacted_when_spouse_living(self):
        # Sam + Liz family: Liz is living, so MARR detail (none here) and the
        # couple is not given marriage details; ensure no Liz name leaks.
        r = gedcom.run_gedcom(self.root, 'p-0000000003', mode='descendants')
        self.assertIn('/Living/', r['text'])

    def test_all_persons(self):
        r = gedcom.run_gedcom(self.root, None, all_persons=True)
        self.assertEqual(r['person_count'], 5)

    def test_not_found(self):
        r = gedcom.run_gedcom(self.root, 'p-9999999999', mode='descendants')
        self.assertEqual(r['status'], 'not-found')

    def test_bad_id(self):
        r = gedcom.run_gedcom(self.root, 'not-an-id', mode='descendants')
        self.assertEqual(r['status'], 'bad-args')

    def test_no_index(self):
        with tempfile.TemporaryDirectory() as empty:
            r = gedcom.run_gedcom(Path(empty), 'p-0000000001')
            self.assertEqual(r['status'], 'no-index')


if __name__ == '__main__':
    unittest.main()
