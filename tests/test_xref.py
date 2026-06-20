import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import xref
from index import _DDL


def _make_index(archive_root: Path) -> sqlite3.Connection:
    """Build a synthetic .cache/index.sqlite with the real schema."""
    cache = archive_root / '.cache'
    cache.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(cache / 'index.sqlite'))
    conn.executescript(_DDL)
    conn.row_factory = sqlite3.Row
    return conn


def _insert_claim(conn, cid, source_id, ctype, value, *, date_edtf=None,
                   place_text=None, place_id=None, subtype=None, negated=0,
                   status='accepted', persons=(), roles=None):
    conn.execute(
        '''INSERT INTO claims(id, source_id, type, subtype, date_edtf, place_id,
                               place_text, value, status, negated)
           VALUES (?,?,?,?,?,?,?,?,?,?)''',
        (cid, source_id, ctype, subtype, date_edtf, place_id, place_text, value,
         status, negated),
    )
    roles = roles or {}
    for pos, pid in enumerate(persons):
        conn.execute(
            'INSERT INTO claim_persons(claim_id, person_id, position, role) VALUES (?,?,?,?)',
            (cid, pid, pos, roles.get(pid)),
        )


class XrefTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = _make_index(self.archive_root)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _seed_persons_sources(self):
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-aaaaaaaaaa','Test Person','false','curated','x.md')")
        self.conn.execute("INSERT INTO sources(id, title, path) VALUES "
                           "('s-1111111111','Source One','a.md')")
        self.conn.execute("INSERT INTO sources(id, title, path) VALUES "
                           "('s-2222222222','Source Two','b.md')")

    def test_overlapping_dates_corroborate(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'birth',
                       'born about 1840', date_edtf='1840~', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'birth',
                       'born 1840-03-02', date_edtf='1840-03-02', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(result['groups']), 1)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]['kind'], 'corroborates')

    def test_non_overlapping_dates_contradict(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'birth',
                       'born 1840', date_edtf='1840', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'birth',
                       'born 1900', date_edtf='1900', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'contradicts')

    def test_overlapping_vital_dates_but_different_place_contradicts(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'birth',
                       'born 1840 in New York', date_edtf='1840~',
                       place_text='New York', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'birth',
                       'born 1840 in Ohio', date_edtf='1840~',
                       place_text='Ohio', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'contradicts')

    def test_overlapping_vital_value_places_contradict_without_place_text(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'birth',
                       'born in New York', date_edtf='1840~', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'birth',
                       'born in Ohio', date_edtf='1840~', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'contradicts')

    def test_overlapping_vital_wording_without_place_still_corroborates(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'birth',
                       'born about 1840', date_edtf='1840~', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'birth',
                       'born 1840-03-02', date_edtf='1840-03-02', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'corroborates')

    def test_same_source_pair_excluded(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'birth',
                       'born 1840', date_edtf='1840', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-1111111111', 'birth',
                       'born 1840 again', date_edtf='1840', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['groups'], [])

    def test_already_linked_pair_excluded(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'birth',
                       'born 1840', date_edtf='1840', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'birth',
                       'born 1840 also', date_edtf='1840', persons=['p-aaaaaaaaaa'])
        self.conn.execute(
            "INSERT INTO claim_links(claim_id, rel, target_id) VALUES ('c-aaaaaaaaaa','corroborates','c-bbbbbbbbbb')"
        )
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['groups'], [])

    def test_different_type_not_paired(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'birth',
                       'born 1840', date_edtf='1840', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'occupation',
                       'worked as a clerk', date_edtf='1840', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['groups'], [])

    def test_non_overlapping_substantive_dates_not_paired(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'residence',
                       'lived in Topeka', date_edtf='1880', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'residence',
                       'lived in Wichita', date_edtf='1900', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['groups'], [])

    def test_matching_place_id_corroborates_despite_place_text_wording(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'birth',
                       'born 1840', date_edtf='1840', place_id='l-aaaaaaaaaa',
                       place_text='Fairview, Breton County, Kansas', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'birth',
                       'born 1840', date_edtf='1840', place_id='l-aaaaaaaaaa',
                       place_text='Fairview City, Breton, Kansas', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'corroborates')

    def test_differing_place_id_contradicts_even_with_similar_text(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'birth',
                       'born 1840', date_edtf='1840', place_id='l-aaaaaaaaaa',
                       place_text='Fairview, Kansas', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'birth',
                       'born 1840', date_edtf='1840', place_id='l-bbbbbbbbbb',
                       place_text='Fairview, Kansas', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'contradicts')

    def test_negation_polarity_mismatch_contradicts_regardless_of_dates(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'marriage',
                       'married 1870', date_edtf='1870', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'marriage',
                       'never married', date_edtf=None, negated=1, persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'contradicts')

    def test_relationship_claims_with_different_roles_not_paired(self) -> None:
        self._seed_persons_sources()
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-bbbbbbbbbb','Parent','false','curated','y.md')")
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-cccccccccc','Child','false','curated','z.md')")
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'relationship',
                       'child of parent', subtype='child-of',
                       persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb'],
                       roles={'p-aaaaaaaaaa': 'child', 'p-bbbbbbbbbb': 'parent'})
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'relationship',
                       'parent of child', subtype='child-of',
                       persons=['p-aaaaaaaaaa', 'p-cccccccccc'],
                       roles={'p-aaaaaaaaaa': 'parent', 'p-cccccccccc': 'child'})
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['groups'], [])

    def test_absent_index_returns_failed_status(self) -> None:
        self.conn.close()
        empty_root = Path(tempfile.mkdtemp())
        try:
            result = xref.run_xref(empty_root)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['groups'], [])
        finally:
            import shutil
            shutil.rmtree(empty_root, ignore_errors=True)

    def test_non_overlapping_burial_dates_contradict(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'burial',
                       'buried 1840', date_edtf='1840', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'burial',
                       'buried 1900', date_edtf='1900', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'contradicts')

    def test_overlapping_burial_value_places_contradict_without_place_text(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'burial',
                       'buried in Springfield', date_edtf='1840~', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'burial',
                       'buried in Fairview', date_edtf='1840~', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'contradicts')

    def test_negated_substantive_claim_not_overlapping_positive_not_paired(self) -> None:
        # A negated 1880 residence and a positive 1900 residence for the same
        # person are not in tension — they describe different periods, so
        # the non-overlap exclusion for recurring types should still apply
        # even when polarity differs.
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'residence',
                       'did not reside in Topeka', date_edtf='1880', negated=1,
                       persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'residence',
                       'resided in Topeka', date_edtf='1900', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['groups'], [])

    def test_negated_substantive_claim_overlapping_positive_contradicts(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'residence',
                       'did not reside in Topeka', date_edtf='1880', negated=1,
                       persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'residence',
                       'resided in Topeka', date_edtf='1880', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'contradicts')

    def test_negated_substantive_claim_different_place_not_paired(self) -> None:
        # A negated residence claim about one place and a positive residence
        # claim about a different place, same year, aren't in conflict —
        # both can be true at once.
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'residence',
                       'did not reside in Topeka', date_edtf='1880', negated=1,
                       place_text='Topeka', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'residence',
                       'resided in Boston', date_edtf='1880',
                       place_text='Boston', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['groups'], [])

    def test_negated_substantive_claim_same_place_text_contradicts(self) -> None:
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'residence',
                       'did not reside there', date_edtf='1880', negated=1,
                       place_text='Topeka', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'residence',
                       'resided there', date_edtf='1880',
                       place_text='Topeka', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'contradicts')

    def test_marriage_to_different_spouse_not_paired(self) -> None:
        # Marriage claims always share the literal role "spouse" for both
        # parties, so role can't distinguish one marriage from another for
        # the same person — the counterpart must. A first-marriage claim and
        # a second-marriage claim (different spouse) shouldn't be compared.
        self._seed_persons_sources()
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-bbbbbbbbbb','Spouse One','false','curated','y.md')")
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-cccccccccc','Spouse Two','false','curated','z.md')")
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'marriage',
                       'married Spouse One', date_edtf='1870',
                       persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb'],
                       roles={'p-aaaaaaaaaa': 'spouse', 'p-bbbbbbbbbb': 'spouse'})
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'marriage',
                       'married Spouse Two', date_edtf='1870',
                       persons=['p-aaaaaaaaaa', 'p-cccccccccc'],
                       roles={'p-aaaaaaaaaa': 'spouse', 'p-cccccccccc': 'spouse'})
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['groups'], [])

    def test_marriage_to_same_spouse_corroborates(self) -> None:
        self._seed_persons_sources()
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-bbbbbbbbbb','Spouse','false','curated','y.md')")
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'marriage',
                       'married Spouse', date_edtf='1870',
                       persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb'],
                       roles={'p-aaaaaaaaaa': 'spouse', 'p-bbbbbbbbbb': 'spouse'})
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'marriage',
                       'married Spouse', date_edtf='1870',
                       persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb'],
                       roles={'p-aaaaaaaaaa': 'spouse', 'p-bbbbbbbbbb': 'spouse'})
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'corroborates')

    def test_negated_marriage_with_no_spouse_named_contradicts_positive_marriage(self) -> None:
        # "Never married" proof claims name no spouse, so they can't be
        # bucketed by counterpart the way a normal marriage claim is — they
        # have to be compared against every marriage claim for this person.
        self._seed_persons_sources()
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-bbbbbbbbbb','Spouse','false','curated','y.md')")
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'marriage',
                       'never married', date_edtf='1870', negated=1,
                       persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'marriage',
                       'married Spouse', date_edtf='1870',
                       persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb'],
                       roles={'p-aaaaaaaaaa': 'spouse', 'p-bbbbbbbbbb': 'spouse'})
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'contradicts')

    def test_divorce_from_different_spouse_not_paired(self) -> None:
        self._seed_persons_sources()
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-bbbbbbbbbb','Spouse One','false','curated','y.md')")
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-cccccccccc','Spouse Two','false','curated','z.md')")
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'divorce',
                       'divorced Spouse One', date_edtf='1870',
                       persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb'],
                       roles={'p-aaaaaaaaaa': 'spouse', 'p-bbbbbbbbbb': 'spouse'})
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'divorce',
                       'divorced Spouse Two', date_edtf='1870',
                       persons=['p-aaaaaaaaaa', 'p-cccccccccc'],
                       roles={'p-aaaaaaaaaa': 'spouse', 'p-cccccccccc': 'spouse'})
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['groups'], [])

    def test_vital_place_phrase_excludes_trailing_date_clause(self) -> None:
        # "born in Springfield in 1840" should compare as place "Springfield",
        # not "Springfield in 1840" — the date belongs to date_edtf, not the
        # place phrase, so this isn't a place mismatch with bare "Springfield".
        self._seed_persons_sources()
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'birth',
                       'born in Springfield in 1840', date_edtf='1840', persons=['p-aaaaaaaaaa'])
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'birth',
                       'born in Springfield', date_edtf='1840', persons=['p-aaaaaaaaaa'])
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(pairs[0]['kind'], 'corroborates')

    def test_relationship_claim_bundling_extra_counterpart_still_pairs(self) -> None:
        # One source bundles two children under one parent claim
        # (roles: parent: [P2, P3]); another source only names one of them.
        # The bundled claim should still be compared against the claim that
        # names just the shared counterpart, since both assert the same
        # parent-of-P2 edge.
        self._seed_persons_sources()
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-bbbbbbbbbb','Child Two','false','curated','y.md')")
        self.conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES "
                           "('p-cccccccccc','Child Three','false','curated','z.md')")
        _insert_claim(self.conn, 'c-aaaaaaaaaa', 's-1111111111', 'relationship',
                       'parent of two children', subtype='child-of',
                       persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb', 'p-cccccccccc'],
                       roles={'p-aaaaaaaaaa': 'parent', 'p-bbbbbbbbbb': 'child',
                              'p-cccccccccc': 'child'})
        _insert_claim(self.conn, 'c-bbbbbbbbbb', 's-2222222222', 'relationship',
                       'parent of one child', subtype='child-of',
                       persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb'],
                       roles={'p-aaaaaaaaaa': 'parent', 'p-bbbbbbbbbb': 'child'})
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        pairs = result['groups'][0]['pairs']
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]['kind'], 'corroborates')

    def test_missing_required_column_returns_failed_status(self) -> None:
        # A cache built against an older claims schema has all the required
        # tables (so the table probe passes) but is missing a column xref's
        # query selects — this must surface the documented incompatible-
        # schema message rather than an uncaught OperationalError.
        self.conn.execute('ALTER TABLE claims RENAME TO claims_old')
        self.conn.execute(
            '''CREATE TABLE claims(
                 id TEXT PRIMARY KEY, source_id TEXT NOT NULL, type TEXT NOT NULL,
                 subtype TEXT, date_edtf TEXT, place_text TEXT, value TEXT NOT NULL,
                 status TEXT NOT NULL, negated INTEGER DEFAULT 0
               )'''
        )
        self.conn.commit()

        result = xref.run_xref(self.archive_root)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['groups'], [])


if __name__ == '__main__':
    unittest.main()
