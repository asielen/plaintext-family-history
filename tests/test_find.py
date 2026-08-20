import argparse
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import find
from _lib import EXIT_CLEAN, EXIT_ERRORS, EXIT_FAILURE, EXIT_WARNINGS
from index import _DDL


def _make_index(archive_root: Path) -> sqlite3.Connection:
    cache = archive_root / '.cache'
    cache.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(cache / 'index.sqlite'))
    conn.executescript(_DDL)
    conn.row_factory = sqlite3.Row
    return conn


def _add_person(conn, pid, name):
    conn.execute("INSERT INTO persons(id, name, living, tier, path) VALUES (?,?,?,?,?)",
                 (pid, name, 'false', 'curated', f'{pid}.md'))


def _add_source(conn, sid, title, source_type=None, repository=None):
    conn.execute("INSERT INTO sources(id, title, source_type, repository, path) VALUES (?,?,?,?,?)",
                 (sid, title, source_type, repository, f'{sid}.md'))


def _add_claim(conn, cid, sid, ctype, value, persons, *, status='accepted',
                date_edtf=None, date_min=None, date_max=None,
                place_id=None, place_text=None, subtype=None, hypothesis=None):
    conn.execute(
        '''INSERT INTO claims(id, source_id, type, subtype, value, status, date_edtf,
           date_min, date_max, place_id, place_text, hypothesis)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
        (cid, sid, ctype, subtype, value, status, date_edtf, date_min, date_max,
         place_id, place_text, hypothesis),
    )
    for pos, pid in enumerate(persons):
        conn.execute(
            'INSERT INTO claim_persons(claim_id, person_id, position, role) VALUES (?,?,?,?)',
            (cid, pid, pos, None),
        )


def _add_alias(conn, alias, canonical_id, kind='name'):
    conn.execute(
        'INSERT INTO aliases(alias, canonical_id, kind) VALUES (?,?,?)',
        (alias.lower(), canonical_id, kind),
    )


def _run(func, *args, **kwargs):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = func(*args, **kwargs)
    return rc, buf.getvalue()


class RelatedPersonTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = _make_index(self.archive_root)
        _add_person(self.conn, 'p-aaaaaaaaaa', 'Alice')
        _add_person(self.conn, 'p-bbbbbbbbbb', 'Bob')
        _add_person(self.conn, 'p-cccccccccc', 'Carol')
        _add_source(self.conn, 's-1111111111', 'Census')
        _add_source(self.conn, 's-2222222222', 'Obituary')

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_relationship_edge_with_source_count(self) -> None:
        _add_claim(self.conn, 'c-1111111111', 's-1111111111', 'relationship',
                    'Alice is spouse of Bob', ['p-aaaaaaaaaa', 'p-bbbbbbbbbb'])
        self.conn.execute(
            "INSERT INTO relationships(person_id, rel, other_id, claim_id) "
            "VALUES ('p-aaaaaaaaaa','spouse','p-bbbbbbbbbb','c-1111111111')"
        )
        self.conn.commit()

        rc, out = _run(find.run_related, 'p-aaaaaaaaaa', None, self.archive_root, {})
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('spouse: Bob [p-bbbbbbbbbb] - 1 source(s)', out)

    def test_cooccurrence_excludes_existing_relationship(self) -> None:
        # Alice/Bob share two sources and already have a relationship edge -
        # should appear under relationships, not co-occurrence.
        _add_claim(self.conn, 'c-1111111111', 's-1111111111', 'residence', 'lived together',
                    ['p-aaaaaaaaaa', 'p-bbbbbbbbbb'])
        _add_claim(self.conn, 'c-2222222222', 's-2222222222', 'residence', 'lived together',
                    ['p-aaaaaaaaaa', 'p-bbbbbbbbbb'])
        self.conn.execute(
            "INSERT INTO relationships(person_id, rel, other_id, claim_id) "
            "VALUES ('p-aaaaaaaaaa','spouse','p-bbbbbbbbbb','c-1111111111')"
        )
        # Alice/Carol share a source with no edge - should show as co-occurring.
        _add_claim(self.conn, 'c-3333333333', 's-1111111111', 'residence', 'lived together',
                    ['p-aaaaaaaaaa', 'p-cccccccccc'])
        self.conn.commit()

        rc, out = _run(find.run_related, 'p-aaaaaaaaaa', None, self.archive_root, {})
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertNotIn('Bob', out.split('co-occurring persons')[1])
        self.assertIn('Carol [p-cccccccccc] - 1 shared source(s)', out)

    def test_places_ranked_by_frequency(self) -> None:
        _add_claim(self.conn, 'c-1111111111', 's-1111111111', 'residence', 'lived in Topeka',
                    ['p-aaaaaaaaaa'], place_text='Topeka, Kansas')
        _add_claim(self.conn, 'c-2222222222', 's-2222222222', 'residence', 'lived in Topeka',
                    ['p-aaaaaaaaaa'], place_text='Topeka, Kansas')
        self.conn.commit()

        rc, out = _run(find.run_related, 'p-aaaaaaaaaa', None, self.archive_root, {})
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('Topeka, Kansas - 2 claim(s)', out)

    def test_shared_affiliation_hub(self) -> None:
        _add_claim(self.conn, 'c-1111111111', 's-1111111111', 'occupation',
                    'bookkeeper, Plains Junction Railroad', ['p-aaaaaaaaaa'])
        _add_claim(self.conn, 'c-2222222222', 's-2222222222', 'occupation',
                    'conductor, Plains Junction Railroad', ['p-bbbbbbbbbb'])
        self.conn.commit()

        rc, out = _run(find.run_related, 'p-aaaaaaaaaa', None, self.archive_root, {})
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('Plains Junction Railroad [occupation] - also: Bob [p-bbbbbbbbbb]', out)

    def test_date_filter_narrows_relationships_and_places(self) -> None:
        _add_claim(self.conn, 'c-1111111111', 's-1111111111', 'relationship',
                    'Alice child of Bob', ['p-aaaaaaaaaa', 'p-bbbbbbbbbb'],
                    subtype='child-of', date_edtf='1900', date_min='1900-01-01', date_max='1900-12-31')
        # date_start/date_end mirror what _derive_relationships actually
        # writes for a child-of edge: the originating claim's own bounds.
        self.conn.execute(
            "INSERT INTO relationships(person_id, rel, other_id, claim_id, date_start, date_end) "
            "VALUES ('p-aaaaaaaaaa','parent','p-bbbbbbbbbb','c-1111111111','1900-01-01','1900-12-31')"
        )
        self.conn.commit()

        rc, out = _run(find.run_related, 'p-aaaaaaaaaa', '1850', self.archive_root, {})
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('relationships: none', out)

        rc, out = _run(find.run_related, 'p-aaaaaaaaaa', '1900', self.archive_root, {})
        self.assertIn('parent: Bob [p-bbbbbbbbbb] - 1 source(s)', out)

    def test_date_filter_uses_relationship_validity_not_claim_bounds(self) -> None:
        # Married in 1850 (the marriage claim's own bounds are just that
        # year), still married in 1865 - date_end stays NULL (open-ended)
        # because no divorce/death claim ended it. A --date query for 1865
        # must still find the spouse edge even though it falls outside the
        # marriage claim's own narrow date_min/date_max.
        _add_claim(self.conn, 'c-1111111111', 's-1111111111', 'marriage',
                    'Alice married Bob', ['p-aaaaaaaaaa', 'p-bbbbbbbbbb'],
                    date_edtf='1850', date_min='1850-01-01', date_max='1850-12-31')
        self.conn.execute(
            "INSERT INTO relationships(person_id, rel, other_id, claim_id, date_start, date_end) "
            "VALUES ('p-aaaaaaaaaa','spouse','p-bbbbbbbbbb','c-1111111111','1850-01-01',NULL)"
        )
        self.conn.commit()

        rc, out = _run(find.run_related, 'p-aaaaaaaaaa', '1865', self.archive_root, {})
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('spouse: Bob [p-bbbbbbbbbb] - 1 source(s)', out)

        rc, out = _run(find.run_related, 'p-aaaaaaaaaa', '1840', self.archive_root, {})
        self.assertIn('relationships: none', out)

    def test_unknown_person_returns_warning(self) -> None:
        rc, out = _run(find.run_related, 'p-zzzzzzzzzz', None, self.archive_root, {})
        self.assertEqual(rc, EXIT_WARNINGS)
        self.assertIn('not found in index', out)


class RelatedPlaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = _make_index(self.archive_root)
        _add_person(self.conn, 'p-aaaaaaaaaa', 'Alice')
        _add_source(self.conn, 's-1111111111', 'Census')
        self.conn.execute(
            "INSERT INTO places(id, name, lat, lon) VALUES ('l-1111111111', 'Fairview', 39.0, -95.0)"
        )
        self.conn.execute(
            "INSERT INTO places(id, name, within) VALUES ('l-2222222222', 'Fairview Cemetery', 'l-1111111111')"
        )

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_claims_people_and_micro_places(self) -> None:
        _add_claim(self.conn, 'c-1111111111', 's-1111111111', 'residence', 'lived in Fairview',
                    ['p-aaaaaaaaaa'], place_id='l-1111111111')
        self.conn.commit()

        rc, out = _run(find.run_related, 'l-1111111111', None, self.archive_root, {})
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('c-1111111111', out)
        self.assertIn('Alice [p-aaaaaaaaaa] - 1 claim(s)', out)
        self.assertIn('Fairview Cemetery [l-2222222222]', out)

    def test_unknown_place_returns_warning(self) -> None:
        rc, out = _run(find.run_related, 'l-9999999999', None, self.archive_root, {})
        self.assertEqual(rc, EXIT_WARNINGS)


class RelatedSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = _make_index(self.archive_root)
        _add_person(self.conn, 'p-aaaaaaaaaa', 'Alice')
        _add_source(self.conn, 's-1111111111', 'Census', repository='County Archive')
        _add_source(self.conn, 's-2222222222', 'Obituary', repository='County Archive')

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_claims_persons_and_sibling_by_repository(self) -> None:
        _add_claim(self.conn, 'c-1111111111', 's-1111111111', 'residence', 'lived there',
                    ['p-aaaaaaaaaa'])
        self.conn.commit()

        rc, out = _run(find.run_related, 's-1111111111', None, self.archive_root, {})
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('Alice [p-aaaaaaaaaa]', out)
        self.assertIn('s-2222222222', out)

    def test_corroborating_source_via_claim_links(self) -> None:
        _add_claim(self.conn, 'c-1111111111', 's-1111111111', 'birth', 'born 1900',
                    ['p-aaaaaaaaaa'])
        _add_claim(self.conn, 'c-2222222222', 's-2222222222', 'birth', 'born 1900',
                    ['p-aaaaaaaaaa'])
        self.conn.execute(
            "INSERT INTO claim_links(claim_id, rel, target_id) VALUES "
            "('c-1111111111','corroborates','c-2222222222')"
        )
        self.conn.commit()

        rc, out = _run(find.run_related, 's-1111111111', None, self.archive_root, {})
        self.assertIn('corroborates: s-2222222222', out)

        rc, out = _run(find.run_related, 's-2222222222', None, self.archive_root, {})
        self.assertIn('corroborated-by: s-1111111111', out)


class RelatedClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = _make_index(self.archive_root)
        _add_person(self.conn, 'p-aaaaaaaaaa', 'Alice')
        _add_source(self.conn, 's-1111111111', 'Census')
        _add_source(self.conn, 's-2222222222', 'Obituary')

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_sibling_claims_same_person_and_type(self) -> None:
        _add_claim(self.conn, 'c-1111111111', 's-1111111111', 'residence', 'lived in Topeka',
                    ['p-aaaaaaaaaa'])
        _add_claim(self.conn, 'c-2222222222', 's-2222222222', 'residence', 'lived in Wichita',
                    ['p-aaaaaaaaaa'])
        self.conn.commit()

        rc, out = _run(find.run_related, 'c-1111111111', None, self.archive_root, {})
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('sibling claims (same person + type) (1):', out)
        self.assertIn('c-2222222222', out)

    def test_unknown_claim_returns_warning(self) -> None:
        rc, out = _run(find.run_related, 'c-9999999999', None, self.archive_root, {})
        self.assertEqual(rc, EXIT_WARNINGS)


class RelatedHypothesisTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = _make_index(self.archive_root)
        _add_person(self.conn, 'p-aaaaaaaaaa', 'Alice')
        _add_source(self.conn, 's-1111111111', 'Census')

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_no_table_row_derives_from_referencing_claims(self) -> None:
        _add_claim(self.conn, 'c-1111111111', 's-1111111111', 'note', 'maybe related',
                    ['p-aaaaaaaaaa'], status='suggested', hypothesis='h-1111111111')
        self.conn.commit()

        rc, out = _run(find.run_related, 'h-1111111111', None, self.archive_root, {})
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('no hypotheses-table row', out)
        self.assertIn('Alice [p-aaaaaaaaaa]', out)
        self.assertIn('c-1111111111', out)

    def test_no_row_and_no_claims_returns_warning(self) -> None:
        rc, out = _run(find.run_related, 'h-9999999999', None, self.archive_root, {})
        self.assertEqual(rc, EXIT_WARNINGS)


class RelatedDateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = _make_index(self.archive_root)
        _add_person(self.conn, 'p-aaaaaaaaaa', 'Alice')
        _add_person(self.conn, 'p-bbbbbbbbbb', 'Bob')
        _add_source(self.conn, 's-1111111111', 'Census')

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_standalone_time_slice(self) -> None:
        _add_claim(self.conn, 'c-1111111111', 's-1111111111', 'residence', 'lived there',
                    ['p-aaaaaaaaaa', 'p-bbbbbbbbbb'], place_text='Topeka',
                    date_edtf='1880', date_min='1880-01-01', date_max='1880-12-31')
        self.conn.commit()

        rc, out = _run(find.run_related, None, '1880', self.archive_root, {})
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('Active in 1880: 1 claims, 2 people, 1 sources', out)
        self.assertIn('Alice [p-aaaaaaaaaa]', out)
        self.assertIn('Topeka', out)

    def test_undated_claim_counts_as_unbounded_not_excluded(self) -> None:
        # index.py stores an undated claim's date_min/date_max as '' (see
        # _overlap_clause's docstring in find.py) rather than NULL or the
        # unbounded edtf_bounds() sentinel. A naive `date_max >= ?` filter
        # would treat '' as the smallest possible value and wrongly drop
        # every undated claim from every --date query.
        _add_claim(self.conn, 'c-1111111111', 's-1111111111', 'residence', 'lived there',
                    ['p-aaaaaaaaaa'], date_min='', date_max='')
        self.conn.commit()

        rc, out = _run(find.run_related, None, '1900', self.archive_root, {})
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('Active in 1900: 1 claims, 1 people, 1 sources', out)

    def test_no_claims_in_range(self) -> None:
        _add_claim(self.conn, 'c-1111111111', 's-1111111111', 'residence', 'lived there',
                    ['p-aaaaaaaaaa'], date_edtf='1880', date_min='1880-01-01', date_max='1880-12-31')
        self.conn.commit()

        rc, out = _run(find.run_related, None, '1950', self.archive_root, {})
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('Active in 1950: 0 claims, 0 people, 0 sources', out)


class RelatedValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = _make_index(self.archive_root)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_invalid_id_rejected(self) -> None:
        rc, out = _run(find.run_related, 'X-bad', None, self.archive_root, {})
        self.assertEqual(rc, EXIT_FAILURE)

    def test_invalid_edtf_rejected(self) -> None:
        rc, out = _run(find.run_related, None, 'not-a-date', self.archive_root, {})
        self.assertEqual(rc, EXIT_FAILURE)

    def test_neither_id_nor_date_rejected(self) -> None:
        rc, out = _run(find.run_related, None, None, self.archive_root, {})
        self.assertEqual(rc, EXIT_FAILURE)

    def test_absent_index_returns_failure(self) -> None:
        empty_root = Path(tempfile.mkdtemp())
        try:
            rc, out = _run(find.run_related, 'p-aaaaaaaaaa', None, empty_root, {})
            self.assertEqual(rc, EXIT_FAILURE)
        finally:
            import shutil
            shutil.rmtree(empty_root, ignore_errors=True)


class RunFindDispatchTests(unittest.TestCase):
    """CLI-layer dispatch: --related with/without an ID, --date alone, and the
    flag-not-given sentinel that keeps bare lookups and --text working."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        # _run_find (the CLI bridge these tests drive via --root) refuses a
        # root without fha.yaml, so the synthetic archive must carry one.
        (self.archive_root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        self.conn = _make_index(self.archive_root)
        _add_person(self.conn, 'p-aaaaaaaaaa', 'Alice')
        _add_source(self.conn, 's-1111111111', 'Census')
        _add_claim(self.conn, 'c-1111111111', 's-1111111111', 'residence', 'lived there',
                    ['p-aaaaaaaaaa'])
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_related_with_id_routes_to_run_related(self) -> None:
        rc, out = _run(
            find.run_find, None, self.archive_root, {},
            related_id='p-aaaaaaaaaa', related_requested=True,
        )
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn("p-aaaaaaaaaa's world", out)

    def test_related_requested_with_no_id_is_standalone_date(self) -> None:
        rc, out = _run(
            find.run_find, None, self.archive_root, {},
            related_id=None, related_requested=True, date_filter='1850/2100',
        )
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('Active in 1850/2100', out)

    def test_bare_id_lookup_unaffected(self) -> None:
        rc, out = _run(find.run_find, 'p-aaaaaaaaaa', self.archive_root, {})
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('p-aaaaaaaaaa  [Alice]', out)

    def _parse(self, argv: list[str]) -> argparse.Namespace:
        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers()
        find.register(subs)
        args = parser.parse_args(['find', *argv])
        args.root = str(self.archive_root)
        return args

    def test_date_without_related_is_rejected_not_silently_dropped(self) -> None:
        # `fha find P-id --date EDTF` (no --related) has no defined meaning -
        # it must not silently discard the ID and run the standalone
        # --related --date time-slice instead.
        args = self._parse(['p-aaaaaaaaaa', '--date', '1900'])
        rc, out = _run(find._run_find, args)
        self.assertEqual(rc, EXIT_FAILURE)

    def test_related_with_date_still_works_via_cli(self) -> None:
        args = self._parse(['--related', 'p-aaaaaaaaaa', '--date', '1900'])
        rc, out = _run(find._run_find, args)
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn("p-aaaaaaaaaa's world", out)

    def test_date_with_text_is_rejected_not_silently_dropped(self) -> None:
        # `fha find --text "X" --date 1900` has no defined meaning - --date
        # is only documented for --related. Used to silently route through
        # the --text branch and discard the date; should now error out.
        args = self._parse(['--text', 'lived', '--date', '1900'])
        rc, out = _run(find._run_find, args)
        self.assertEqual(rc, EXIT_FAILURE)

    def test_related_with_date_then_id_treats_positional_as_related_id(self) -> None:
        # `fha find --related --date 1900 P-…` parses as --related-no-value
        # + --date 1900 + positional 'P-…'. Used to silently drop the P-id
        # and run the standalone date slice; should now route the P-id to
        # the related-person neighborhood.
        args = self._parse(['--related', '--date', '1900', 'p-aaaaaaaaaa'])
        rc, out = _run(find._run_find, args)
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn("p-aaaaaaaaaa's world", out)
        self.assertNotIn('Active in', out)


class PersonPlacesStatusTests(unittest.TestCase):
    """Covers the _person_places status-filter fix: `suggested`/`rejected`
    placed claims must not be silently promoted into a person's place
    ranking, matching the gating on co-occurrence and shared affiliations."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = _make_index(self.archive_root)
        _add_person(self.conn, 'p-aaaaaaaaaa', 'Alice')
        _add_source(self.conn, 's-1111111111', 'Census')

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_suggested_and_rejected_places_excluded_from_ranking(self) -> None:
        _add_claim(self.conn, 'c-1111111111', 's-1111111111', 'residence',
                    'lived in Topeka', ['p-aaaaaaaaaa'],
                    place_text='Topeka, Kansas')  # accepted
        _add_claim(self.conn, 'c-2222222222', 's-2222222222', 'residence',
                    'maybe Wichita', ['p-aaaaaaaaaa'],
                    place_text='Wichita, Kansas', status='suggested')
        _add_claim(self.conn, 'c-3333333333', 's-1111111111', 'residence',
                    'not Lawrence', ['p-aaaaaaaaaa'],
                    place_text='Lawrence, Kansas', status='rejected')
        self.conn.commit()

        rc, out = _run(find.run_related, 'p-aaaaaaaaaa', None, self.archive_root, {})
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('Topeka, Kansas - 1 claim(s)', out)
        self.assertNotIn('Wichita', out)
        self.assertNotIn('Lawrence', out)


class SharedAffiliationDateTests(unittest.TestCase):
    """Covers the _person_org_hubs `--date` fix: hubs from a 1950 membership
    must not appear when slicing the 1880 neighborhood."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = _make_index(self.archive_root)
        _add_person(self.conn, 'p-aaaaaaaaaa', 'Alice')
        _add_person(self.conn, 'p-bbbbbbbbbb', 'Bob')
        _add_source(self.conn, 's-1111111111', 'Census')
        _add_source(self.conn, 's-2222222222', 'Obituary')

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_date_filter_excludes_out_of_range_hubs(self) -> None:
        _add_claim(self.conn, 'c-1111111111', 's-1111111111', 'occupation',
                    'bookkeeper, Plains Junction Railroad', ['p-aaaaaaaaaa'],
                    date_edtf='1880', date_min='1880-01-01', date_max='1880-12-31')
        _add_claim(self.conn, 'c-2222222222', 's-2222222222', 'occupation',
                    'conductor, Plains Junction Railroad', ['p-bbbbbbbbbb'],
                    date_edtf='1880', date_min='1880-01-01', date_max='1880-12-31')
        _add_claim(self.conn, 'c-3333333333', 's-1111111111', 'event',
                    'Elks Lodge', ['p-aaaaaaaaaa'], subtype='membership',
                    date_edtf='1950', date_min='1950-01-01', date_max='1950-12-31')
        _add_claim(self.conn, 'c-4444444444', 's-2222222222', 'event',
                    'Elks Lodge', ['p-bbbbbbbbbb'], subtype='membership',
                    date_edtf='1950', date_min='1950-01-01', date_max='1950-12-31')
        self.conn.commit()

        rc, out = _run(find.run_related, 'p-aaaaaaaaaa', '1880', self.archive_root, {})
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('Plains Junction Railroad', out)
        self.assertNotIn('Elks Lodge', out)


class PhotoIndexFreshnessInRelatedTests(unittest.TestCase):
    """Covers the photoindex_status gating in _print_person_photos and
    _print_place_photos: a stale photos.sqlite must be reported as stale
    rather than queried as if its rows were current."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = _make_index(self.archive_root)
        _add_person(self.conn, 'p-aaaaaaaaaa', 'Alice')
        _add_source(self.conn, 's-1111111111', 'Census')
        self.conn.execute(
            "INSERT INTO places(id, name, lat, lon) VALUES ('l-1111111111', 'Fairview', 39.0, -95.0)"
        )
        _add_claim(self.conn, 'c-1111111111', 's-1111111111', 'residence',
                    'lived there', ['p-aaaaaaaaaa'], place_id='l-1111111111')
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _make_stale_photo_db(self) -> None:
        photos_root = self.archive_root / 'photos'
        photos_root.mkdir()
        cache = self.archive_root / '.cache'
        cache.mkdir(exist_ok=True)
        # Build the minimal schema the queries touch so we'd surface rows
        # if the freshness gate were absent - the test then asserts we do not.
        pconn = sqlite3.connect(str(cache / 'photos.sqlite'))
        pconn.executescript(
            '''
            PRAGMA user_version=1;
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta(key, value) VALUES ('schema_version', '1');
            CREATE TABLE photos(path TEXT PRIMARY KEY, group_id INTEGER,
                                gps_lat REAL, gps_lon REAL);
            CREATE TABLE photo_groups(group_id INTEGER PRIMARY KEY, primary_path TEXT);
            CREATE TABLE photo_people(path TEXT, person_ref TEXT);
            CREATE TABLE photo_face_regions(path TEXT);
            CREATE TABLE photo_keywords(path TEXT);
            CREATE VIRTUAL TABLE photo_fts USING fts5(path, name, caption);
            INSERT INTO photo_groups VALUES (1, 'photos/old.jpg');
            INSERT INTO photos VALUES ('photos/old.jpg', 1, 39.0, -95.0);
            INSERT INTO photo_people VALUES ('photos/old.jpg', 'p-aaaaaaaaaa');
            '''
        )
        pconn.commit()
        pconn.close()
        # A photo file newer than the index → photoindex_status reports 'stale'.
        new_photo = photos_root / 'new.jpg'
        new_photo.write_bytes(b'x')
        import os, time
        future = time.time() + 60
        os.utime(new_photo, (future, future))

    def test_person_photos_reports_stale_instead_of_old_rows(self) -> None:
        self._make_stale_photo_db()
        rc, out = _run(find.run_related, 'p-aaaaaaaaaa', None,
                       self.archive_root, {'photos': {'root': 'photos'}})
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('photos: photo index is stale', out)
        self.assertNotIn('photos/old.jpg', out)

    def test_place_photos_reports_stale_instead_of_old_rows(self) -> None:
        self._make_stale_photo_db()
        rc, out = _run(find.run_related, 'l-1111111111', None,
                       self.archive_root, {'photos': {'root': 'photos'}})
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('photos: photo index is stale', out)
        self.assertNotIn('photos/old.jpg', out)


class PersonSourceCountDateTests(unittest.TestCase):
    """Covers the _person_source_count `--date` fix: dated person slices
    must not count sources whose only claim about the person falls outside
    the window."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = _make_index(self.archive_root)
        _add_person(self.conn, 'p-aaaaaaaaaa', 'Alice')
        _add_source(self.conn, 's-1111111111', 'Census 1880')
        _add_source(self.conn, 's-2222222222', 'Obituary 1950')

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_dated_slice_counts_only_in_window_sources(self) -> None:
        _add_claim(self.conn, 'c-1111111111', 's-1111111111', 'residence', 'lived',
                    ['p-aaaaaaaaaa'], date_edtf='1880',
                    date_min='1880-01-01', date_max='1880-12-31')
        _add_claim(self.conn, 'c-2222222222', 's-2222222222', 'event', 'died',
                    ['p-aaaaaaaaaa'], date_edtf='1950',
                    date_min='1950-01-01', date_max='1950-12-31')
        self.conn.commit()

        rc, out = _run(find.run_related, 'p-aaaaaaaaaa', None, self.archive_root, {})
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('sources: 2', out)

        rc, out = _run(find.run_related, 'p-aaaaaaaaaa', '1880', self.archive_root, {})
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('sources: 1', out)


class RelatedSourceDateTests(unittest.TestCase):
    """Covers the _related_source date-narrowing fixes:
    - source_people frontmatter rows excluded when date_bounds is set
    - sibling sources filtered to claim-backed in-window sources only"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = _make_index(self.archive_root)
        _add_person(self.conn, 'p-aaaaaaaaaa', 'Alice')
        _add_person(self.conn, 'p-bbbbbbbbbb', 'Bob')
        _add_source(self.conn, 's-1111111111', 'Selected 1880')
        _add_source(self.conn, 's-2222222222', 'Sibling 1880')
        _add_source(self.conn, 's-3333333333', 'Sibling 1950')

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_source_people_skipped_in_dated_slice(self) -> None:
        # Bob is only on s-1111111111 via frontmatter source_people - no
        # in-window claim ties him to it. A dated slice must not list him.
        _add_claim(self.conn, 'c-1111111111', 's-1111111111', 'residence', 'lived',
                    ['p-aaaaaaaaaa'], date_edtf='1880',
                    date_min='1880-01-01', date_max='1880-12-31')
        self.conn.execute(
            "INSERT INTO source_people(source_id, person_id) VALUES "
            "('s-1111111111', 'p-bbbbbbbbbb')"
        )
        self.conn.commit()

        rc, out = _run(find.run_related, 's-1111111111', '1880', self.archive_root, {})
        self.assertEqual(rc, EXIT_CLEAN)
        # Alice (in-window claim) yes; Bob (frontmatter-only) no.
        self.assertIn('Alice [p-aaaaaaaaaa]', out)
        self.assertNotIn('Bob', out)

    def test_sibling_sources_filtered_by_date(self) -> None:
        # Selected source has Alice in 1880. Sibling 1880 has Alice in 1880.
        # Sibling 1950 has Alice in 1950. A 1880 slice must drop Sibling 1950.
        _add_claim(self.conn, 'c-1111111111', 's-1111111111', 'residence', 'in selected',
                    ['p-aaaaaaaaaa'], date_edtf='1880',
                    date_min='1880-01-01', date_max='1880-12-31')
        _add_claim(self.conn, 'c-2222222222', 's-2222222222', 'residence', 'in sibling 1880',
                    ['p-aaaaaaaaaa'], date_edtf='1880',
                    date_min='1880-01-01', date_max='1880-12-31')
        _add_claim(self.conn, 'c-3333333333', 's-3333333333', 'event', 'in sibling 1950',
                    ['p-aaaaaaaaaa'], date_edtf='1950',
                    date_min='1950-01-01', date_max='1950-12-31')
        self.conn.commit()

        rc, out = _run(find.run_related, 's-1111111111', '1880', self.archive_root, {})
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('s-2222222222', out)
        self.assertNotIn('s-3333333333', out)


class RelatedPhotoDateTests(unittest.TestCase):
    """Covers _print_person_photos / _print_place_photos date filtering:
    when `--date` is given, photos whose own EDTF falls outside the window
    must not appear in the neighborhood."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = _make_index(self.archive_root)
        _add_person(self.conn, 'p-aaaaaaaaaa', 'Alice')
        self.conn.execute(
            "INSERT INTO places(id, name, lat, lon) VALUES ('l-1111111111', 'Fairview', 39.0, -95.0)"
        )
        self.conn.commit()
        self._make_fresh_photo_db()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _make_fresh_photo_db(self) -> None:
        cache = self.archive_root / '.cache'
        cache.mkdir(exist_ok=True)
        pconn = sqlite3.connect(str(cache / 'photos.sqlite'))
        pconn.executescript(
            '''
            PRAGMA user_version=1;
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta(key, value) VALUES ('schema_version', '1');
            CREATE TABLE photos(path TEXT PRIMARY KEY, group_id INTEGER,
                                gps_lat REAL, gps_lon REAL, edtf TEXT);
            CREATE TABLE photo_groups(group_id INTEGER PRIMARY KEY,
                                       primary_path TEXT, edtf_resolved TEXT);
            CREATE TABLE photo_people(path TEXT, person_ref TEXT);
            CREATE TABLE photo_face_regions(path TEXT);
            CREATE TABLE photo_keywords(path TEXT);
            CREATE VIRTUAL TABLE photo_fts USING fts5(path, name, caption);
            INSERT INTO photo_groups VALUES (1, 'photos/p1880.jpg', '1880');
            INSERT INTO photo_groups VALUES (2, 'photos/p1950.jpg', '1950');
            INSERT INTO photos VALUES ('photos/p1880.jpg', 1, 39.0, -95.0, '1880');
            INSERT INTO photos VALUES ('photos/p1950.jpg', 2, 39.0, -95.0, '1950');
            INSERT INTO photo_people VALUES ('photos/p1880.jpg', 'p-aaaaaaaaaa');
            INSERT INTO photo_people VALUES ('photos/p1950.jpg', 'p-aaaaaaaaaa');
            '''
        )
        pconn.commit()
        pconn.close()

    def test_person_photos_filtered_by_date_window(self) -> None:
        rc, out = _run(find.run_related, 'p-aaaaaaaaaa', '1880',
                       self.archive_root, {'photos': {'root': 'photos'}})
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('photos/p1880.jpg', out)
        self.assertNotIn('photos/p1950.jpg', out)

    def test_place_photos_filtered_by_date_window(self) -> None:
        rc, out = _run(find.run_related, 'l-1111111111', '1880',
                       self.archive_root, {'photos': {'root': 'photos'}})
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('photos/p1880.jpg', out)
        self.assertNotIn('photos/p1950.jpg', out)


class RelatedPhotoMissingTests(unittest.TestCase):
    """Covers reconcile's 'MISSING:' catalog keys in the person and place
    neighborhoods: a group whose primary vanished must be listed by a member
    that is still on disk, and a group with no live member at all must say so
    in plain words instead of printing the internal key."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = _make_index(self.archive_root)
        _add_person(self.conn, 'p-aaaaaaaaaa', 'Alice')
        self.conn.execute(
            "INSERT INTO places(id, name, lat, lon) VALUES ('l-1111111111', 'Fairview', 39.0, -95.0)"
        )
        self.conn.commit()
        self._make_photo_db()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _make_photo_db(self) -> None:
        cache = self.archive_root / '.cache'
        cache.mkdir(exist_ok=True)
        pconn = sqlite3.connect(str(cache / 'photos.sqlite'))
        pconn.executescript(
            '''
            PRAGMA user_version=1;
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta(key, value) VALUES ('schema_version', '1');
            CREATE TABLE photos(path TEXT PRIMARY KEY, group_id INTEGER,
                                gps_lat REAL, gps_lon REAL, edtf TEXT);
            CREATE TABLE photo_groups(group_id INTEGER PRIMARY KEY,
                                       primary_path TEXT, edtf_resolved TEXT);
            CREATE TABLE photo_people(path TEXT, person_ref TEXT);
            CREATE TABLE photo_face_regions(path TEXT);
            CREATE TABLE photo_keywords(path TEXT);
            CREATE VIRTUAL TABLE photo_fts USING fts5(path, name, caption);
            -- Group 1: the front scan vanished, the back scan is still here.
            INSERT INTO photo_groups VALUES (1, 'MISSING:photos/front.jpg', '1880');
            INSERT INTO photos VALUES ('MISSING:photos/front.jpg', 1, 39.0, -95.0, '1880');
            INSERT INTO photos VALUES ('photos/front-back.jpg', 1, 39.0, -95.0, '1880');
            INSERT INTO photo_people VALUES ('MISSING:photos/front.jpg', 'p-aaaaaaaaaa');
            -- Group 2: every member is gone.
            INSERT INTO photo_groups VALUES (2, 'MISSING:photos/gone.jpg', '1890');
            INSERT INTO photos VALUES ('MISSING:photos/gone.jpg', 2, 39.0, -95.0, '1890');
            INSERT INTO photo_people VALUES ('MISSING:photos/gone.jpg', 'p-aaaaaaaaaa');
            '''
        )
        pconn.commit()
        pconn.close()

    def test_person_photos_prefer_live_member_and_flag_vanished_group(self) -> None:
        rc, out = _run(find.run_related, 'p-aaaaaaaaaa', None,
                       self.archive_root, {'photos': {'root': 'photos'}})
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertNotIn('MISSING:', out)
        self.assertIn('photos/front-back.jpg', out)
        self.assertIn('photos/gone.jpg   (not on disk)', out)
        self.assertIn('fha photoindex reconcile', out)

    def test_place_photos_prefer_live_member_and_flag_vanished_group(self) -> None:
        rc, out = _run(find.run_related, 'l-1111111111', None,
                       self.archive_root, {'photos': {'root': 'photos'}})
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertNotIn('MISSING:', out)
        self.assertIn('photos/front-back.jpg', out)
        self.assertIn('photos/gone.jpg   (not on disk)', out)
        self.assertIn('fha photoindex reconcile', out)

    def test_caption_search_shows_the_path_the_photo_had(self) -> None:
        # A caption stays searchable after its file leaves the disk (that is
        # what reconcile's kept row is for), so the hit is right - but the
        # human is given the path the photo had, marked, not the internal key.
        cache = self.archive_root / '.cache'
        pconn = sqlite3.connect(str(cache / 'photos.sqlite'))
        pconn.executescript(
            "INSERT INTO photo_fts(path, name, caption) VALUES "
            "('MISSING:photos/gone.jpg', '', 'Alice at the county fair');"
        )
        pconn.commit()
        pconn.close()
        future = time.time() + 600
        os.utime(cache / 'photos.sqlite', (future, future))

        with unittest.mock.patch.object(find, 'photoindex_status',
                                        return_value=('fresh', 0.0)):
            rc, out = _run(find._find_text, 'fair', self.archive_root, {}, None)
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('[photo] photos/gone.jpg (not on disk)', out)
        self.assertNotIn('MISSING:', out)

    def test_person_photo_count_names_the_files_that_are_gone(self) -> None:
        # `fha find P-id` prints one photo count; it must not promise two
        # files the human then cannot find.
        rc, out = _run(find._find_person, 'p-aaaaaaaaaa', self.conn,
                       self.archive_root, {'photos': {'root': 'photos'}})
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('photos: 2 (2 not on disk - run fha photoindex reconcile)', out)


class RelatedSchemaFailureTests(unittest.TestCase):
    """Covers the run_related sqlite3.OperationalError guard: a partial /
    incompatible schema (table exists but a column the query uses doesn't)
    must surface the documented unreadable-index error and exit 3 instead
    of tracebacking out of dispatch."""

    def test_missing_relationships_date_start_returns_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp)
            cache = archive_root / '.cache'
            cache.mkdir()
            conn = sqlite3.connect(str(cache / 'index.sqlite'))
            from index import _DDL
            conn.executescript(_DDL)
            # Drop+recreate `relationships` without date_start/date_end so
            # the related-person query's _overlap_clause raises mid-dispatch.
            conn.executescript(
                '''
                DROP TABLE relationships;
                CREATE TABLE relationships(
                    person_id TEXT, rel TEXT, other_id TEXT, claim_id TEXT
                );
                INSERT INTO persons(id, name, living, tier, path)
                  VALUES ('p-aaaaaaaaaa', 'Alice', 'false', 'curated', 'p.md');
                '''
            )
            conn.commit()
            conn.close()

            buf = io.StringIO()
            from contextlib import redirect_stderr
            with redirect_stdout(io.StringIO()), redirect_stderr(buf):
                rc = find.run_related('p-aaaaaaaaaa', '1900', archive_root, {})
            self.assertEqual(rc, EXIT_FAILURE)
            self.assertIn('unreadable or has an incompatible schema', buf.getvalue())


class OpenIndexDbFailureTests(unittest.TestCase):
    """Covers the _lib.open_index_db and find._open_index connect-failure
    fixes: a non-file at `.cache/index.sqlite` (e.g. a directory) used to
    crash before reaching the try block; should now return None cleanly so
    the documented behavior (unreadable-index error for --related, silent
    tree-scan fallback for bare ID/--text) still holds."""

    def test_directory_at_index_path_returns_none(self) -> None:
        from _lib import open_index_db
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.cache' / 'index.sqlite').mkdir(parents=True)
            buf = io.StringIO()
            from contextlib import redirect_stderr
            with redirect_stderr(buf):
                conn = open_index_db(root, ('persons',))
            self.assertIsNone(conn)
            self.assertIn('unreadable', buf.getvalue())

    def test_find_open_index_directory_returns_none_silently(self) -> None:
        # find._open_index is the bare-ID/--text fallback path: should
        # degrade silently to a tree scan instead of tracebacking out.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.cache' / 'index.sqlite').mkdir(parents=True)
            self.assertIsNone(find._open_index(root))


class FindSourceRestrictedLabelTests(unittest.TestCase):
    """`fha find <S-id>` must show the [restricted] label for a TYPED
    `restricted:` value (`dna`, `by-request`) - the strongest markers. The
    label reads the index's restricted column, which the old narrow idiom in
    index.py stored as 0 for typed values, so the label silently vanished for
    exactly the sources it mattered most for. End-to-end: real record files,
    real build_index, real find output."""

    _SOURCE = ('---\nid: {sid}\ntitle: {title}\nsource_type: other\n'
               '{line}---\n\n## Claims\n')

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for sid, title, line in (
            ('S-1111111111', 'DNA match list', 'restricted: dna\n'),
            ('S-2222222222', 'Private letter', 'restricted: by-request\n'),
            ('S-3333333333', 'Plain census', ''),
        ):
            path = self.root / 'sources' / 'other' / f'src_{sid}.md'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                self._SOURCE.format(sid=sid, title=title, line=line),
                encoding='utf-8',
            )
        import index
        index.build_index(self.root, {})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_typed_restricted_values_show_label(self) -> None:
        for sid in ('s-1111111111', 's-2222222222'):
            rc, out = _run(find.run_find, sid, self.root, {})
            self.assertEqual(rc, EXIT_CLEAN, sid)
            self.assertIn('[restricted]', out, sid)

    def test_unrestricted_source_has_no_label(self) -> None:
        rc, out = _run(find.run_find, 's-3333333333', self.root, {})
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertNotIn('[restricted]', out)


class RunFindRootGuardTests(unittest.TestCase):
    """`fha find --root <non-archive>` must refuse (exit 3) with a message
    naming the fix, mirroring `fha index --root`. Without the guard a typo'd
    --root scanned an arbitrary folder and reported a false "not found in
    archive tree" - a dead end that reads as "the record doesn't exist"."""

    def test_non_archive_root_refused(self) -> None:
        from contextlib import redirect_stderr
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = find._standalone_main(['p-aaaaaaaaaa', '--root', tmp])
            self.assertEqual(rc, EXIT_FAILURE)
            self.assertIn('fha.yaml', err.getvalue())
            self.assertIn('--root', err.getvalue())

    def test_root_with_fha_yaml_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
            conn = _make_index(root)
            _add_person(conn, 'p-aaaaaaaaaa', 'Alice')
            conn.commit()
            conn.close()
            rc, out = _run(find._standalone_main, ['p-aaaaaaaaaa', '--root', tmp])
            self.assertEqual(rc, EXIT_CLEAN)
            self.assertIn('p-aaaaaaaaaa  [Alice]', out)


# ── --json (plan 17): the reference-resolver backend ────────────────────────

class SearchJsonHitShapeTests(unittest.TestCase):
    """search_json's per-hit shape and per-type label/detail formatting."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = _make_index(self.archive_root)
        _add_person(self.conn, 'p-aaaaaaaaaa', 'Thomas Hartley')
        self.conn.execute(
            "UPDATE persons SET surname='Hartley', birth='1840~', death='1923', tier='stub' "
            "WHERE id='p-aaaaaaaaaa'"
        )
        _add_alias(self.conn, 'p-aaaaaaaaaa', 'p-aaaaaaaaaa', 'id')
        _add_alias(self.conn, 'Thomas Hartley', 'p-aaaaaaaaaa', 'name')

        _add_source(self.conn, 's-1111111111', 'Hartley Census', source_type='census')
        self.conn.execute("UPDATE sources SET date_edtf='1900' WHERE id='s-1111111111'")
        _add_alias(self.conn, 's-1111111111', 's-1111111111', 'id')

        self.conn.execute(
            "INSERT INTO places(id, name, hierarchy) VALUES "
            "('l-1111111111', 'Fairview', 'Kansas, USA')"
        )
        _add_alias(self.conn, 'l-1111111111', 'l-1111111111', 'id')
        _add_alias(self.conn, 'Fairview', 'l-1111111111', 'name')
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_every_hit_has_the_four_documented_keys(self) -> None:
        results = find.search_json(self.archive_root, {}, 'Hartley')
        self.assertTrue(results)
        for r in results:
            self.assertEqual(set(r.keys()), {'id', 'type', 'label', 'detail'})

    def test_person_detail_carries_vitals_and_tier(self) -> None:
        results = find.search_json(self.archive_root, {}, 'P-aaaaaaaaaa')
        self.assertEqual(len(results), 1)
        hit = results[0]
        self.assertEqual(hit['type'], 'person')
        self.assertEqual(hit['label'], 'Thomas Hartley')
        self.assertEqual(hit['detail'], '1840~ - 1923 · stub')

    def test_source_detail_carries_type_and_date(self) -> None:
        results = find.search_json(self.archive_root, {}, 'S-1111111111')
        self.assertEqual(len(results), 1)
        hit = results[0]
        self.assertEqual(hit['type'], 'source')
        self.assertEqual(hit['label'], 'Hartley Census')
        self.assertEqual(hit['detail'], 'census · 1900')

    def test_place_detail_carries_hierarchy(self) -> None:
        results = find.search_json(self.archive_root, {}, 'L-1111111111')
        self.assertEqual(len(results), 1)
        hit = results[0]
        self.assertEqual(hit['type'], 'place')
        self.assertEqual(hit['label'], 'Fairview')
        self.assertEqual(hit['detail'], 'Kansas, USA')


class SearchJsonPlaceAltNameTests(unittest.TestCase):
    """The place_names alt-name match in `_ranked_search` used to run one
    extra `SELECT ... FROM places WHERE id = ?` per matching row - an N+1
    now replaced by a single JOIN. Covers the case the old per-row code was
    most likely to get wrong: MULTIPLE alt-name hits resolving to their own
    (different) place, plus a stale alt_name row whose place no longer
    exists."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = _make_index(self.archive_root)
        self.conn.execute(
            "INSERT INTO places(id, name, hierarchy) VALUES "
            "('l-1111111111', 'Fairview', 'Kansas, USA')"
        )
        self.conn.execute(
            "INSERT INTO places(id, name, hierarchy) VALUES "
            "('l-2222222222', 'Fairview Township', 'Brown County, Kansas, USA')"
        )
        self.conn.execute(
            "INSERT INTO place_names(place_id, alt_name) VALUES "
            "('l-1111111111', 'Fairview Corners')"
        )
        self.conn.execute(
            "INSERT INTO place_names(place_id, alt_name) VALUES "
            "('l-2222222222', 'Fairview Corners Township')"
        )
        # A stale alt_name row for a place that no longer exists - the JOIN
        # must silently drop it, exactly like the old per-row lookup's
        # `if place_row is None: continue`.
        self.conn.execute(
            "INSERT INTO place_names(place_id, alt_name) VALUES "
            "('l-9999999999', 'Fairview Corners Ghost')"
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_multiple_alt_name_matches_resolve_to_their_own_place(self) -> None:
        results = find.search_json(self.archive_root, {}, 'Fairview Corners')
        by_id = {r['id']: r for r in results}
        self.assertEqual(set(by_id), {'l-1111111111', 'l-2222222222'})
        self.assertEqual(by_id['l-1111111111']['label'], 'Fairview')
        self.assertEqual(by_id['l-1111111111']['detail'], 'Kansas, USA')
        self.assertEqual(by_id['l-2222222222']['label'], 'Fairview Township')
        self.assertEqual(by_id['l-2222222222']['detail'], 'Brown County, Kansas, USA')

    def test_alt_name_row_for_a_deleted_place_is_dropped_not_crashed(self) -> None:
        results = find.search_json(self.archive_root, {}, 'Fairview Corners Ghost')
        self.assertEqual(results, [])


class SearchJsonIdLookupTests(unittest.TestCase):
    """A bare valid ID short-circuits straight to that record - the
    reference-resolver's 'I already have an ID' path (plan-17 point 1)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = _make_index(self.archive_root)
        _add_person(self.conn, 'p-aaaaaaaaaa', 'Alice Hartley')
        _add_source(self.conn, 's-1111111111', 'Alice Hartley Census')
        _add_claim(self.conn, 'c-1111111111', 's-1111111111', 'birth', 'born 1900',
                    ['p-aaaaaaaaaa'], status='accepted')
        self.conn.execute(
            "INSERT INTO hypotheses(id, person_id, hypothesis, status) VALUES "
            "('h-1111111111', 'p-aaaaaaaaaa', 'maybe born in Fairview', 'open')"
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_person_id_returns_one_result(self) -> None:
        results = find.search_json(self.archive_root, {}, 'p-aaaaaaaaaa')
        self.assertEqual([r['id'] for r in results], ['p-aaaaaaaaaa'])
        self.assertEqual(results[0]['type'], 'person')

    def test_id_lookup_is_case_insensitive(self) -> None:
        results = find.search_json(self.archive_root, {}, 'P-AAAAAAAAAA')
        self.assertEqual([r['id'] for r in results], ['p-aaaaaaaaaa'])

    def test_claim_id_resolves_to_the_claim_itself_not_its_source(self) -> None:
        # A cited C-id's only ALIAS row points at its owning source (see
        # index.py's _register_cited_claim_aliases) - the bare-ID lookup must
        # still resolve to the claim, not silently redirect to the source.
        results = find.search_json(self.archive_root, {}, 'c-1111111111')
        self.assertEqual([r['id'] for r in results], ['c-1111111111'])
        self.assertEqual(results[0]['type'], 'claim')
        self.assertIn('born 1900', results[0]['label'])

    def test_hypothesis_id_resolves(self) -> None:
        results = find.search_json(self.archive_root, {}, 'h-1111111111')
        self.assertEqual([r['id'] for r in results], ['h-1111111111'])
        self.assertEqual(results[0]['type'], 'hypothesis')

    def test_unknown_id_returns_empty(self) -> None:
        results = find.search_json(self.archive_root, {}, 'p-zzzzzzzzzz')
        self.assertEqual(results, [])

    def test_id_lookup_respects_kind_filter(self) -> None:
        results = find.search_json(self.archive_root, {}, 'p-aaaaaaaaaa', kinds=['source'])
        self.assertEqual(results, [])


class SearchJsonRankingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = _make_index(self.archive_root)
        # Exact, prefix, and substring title matches for the same query -
        # deliberately all sources, so the tier ladder is isolated from any
        # cross-type tie-break.
        _add_source(self.conn, 's-1111111111', 'Kansas', source_type='book')
        _add_source(self.conn, 's-2222222222', 'Kansas City Directory', source_type='directory')
        _add_source(self.conn, 's-3333333333', 'State of Kansas Records', source_type='book')
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_exact_ranks_before_prefix_before_substring(self) -> None:
        results = find.search_json(self.archive_root, {}, 'Kansas')
        ids = [r['id'] for r in results]
        self.assertEqual(ids, ['s-1111111111', 's-2222222222', 's-3333333333'])

    def test_no_match_returns_empty_list(self) -> None:
        results = find.search_json(self.archive_root, {}, 'zzzznotfound')
        self.assertEqual(results, [])

    def test_blank_query_returns_empty_list(self) -> None:
        self.assertEqual(find.search_json(self.archive_root, {}, ''), [])
        self.assertEqual(find.search_json(self.archive_root, {}, '   '), [])


class SearchJsonDedupeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = _make_index(self.archive_root)
        _add_person(self.conn, 'p-aaaaaaaaaa', 'Margaret Cole')
        self.conn.execute("UPDATE persons SET surname='Cole' WHERE id='p-aaaaaaaaaa'")
        # Registered as an alias (name) AND directly matchable via
        # persons.surname - both paths must collapse to one hit.
        _add_alias(self.conn, 'Margaret Cole', 'p-aaaaaaaaaa', 'name')
        _add_alias(self.conn, 'p-aaaaaaaaaa', 'p-aaaaaaaaaa', 'id')
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_alias_and_direct_field_match_collapse_to_one_hit(self) -> None:
        results = find.search_json(self.archive_root, {}, 'Cole')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['id'], 'p-aaaaaaaaaa')


class SearchJsonKindAndLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = _make_index(self.archive_root)
        _add_person(self.conn, 'p-aaaaaaaaaa', 'Topeka Smith')
        _add_source(self.conn, 's-1111111111', 'Topeka Directory', source_type='directory')
        self.conn.execute("INSERT INTO places(id, name) VALUES ('l-1111111111', 'Topeka')")
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_unfiltered_search_spans_multiple_kinds(self) -> None:
        results = find.search_json(self.archive_root, {}, 'Topeka')
        types = {r['type'] for r in results}
        self.assertTrue({'person', 'source', 'place'} <= types)

    def test_kind_filter_restricts_to_one_type(self) -> None:
        results = find.search_json(self.archive_root, {}, 'Topeka', kinds=['place'])
        self.assertTrue(results)
        self.assertTrue(all(r['type'] == 'place' for r in results))

    def test_limit_caps_result_count(self) -> None:
        results = find.search_json(self.archive_root, {}, 'Topeka', limit=1)
        self.assertEqual(len(results), 1)

    def test_zero_limit_returns_empty(self) -> None:
        results = find.search_json(self.archive_root, {}, 'Topeka', limit=0)
        self.assertEqual(results, [])


class SearchJsonTextHitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = _make_index(self.archive_root)
        self.conn.execute(
            "INSERT INTO notes_fts(path, content) VALUES "
            "('notes/questions.md', 'Was Grandpa Hartley really born in Fairview?')"
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_fts_hit_is_typed_text_with_path_as_id_and_detail(self) -> None:
        results = find.search_json(self.archive_root, {}, 'Fairview')
        text_hits = [r for r in results if r['type'] == 'text']
        self.assertEqual(len(text_hits), 1)
        hit = text_hits[0]
        self.assertEqual(hit['id'], 'notes/questions.md')
        self.assertEqual(hit['detail'], 'notes/questions.md')
        self.assertIn('Fairview', hit['label'])

    def test_malformed_fts_query_degrades_instead_of_raising(self) -> None:
        # An unbalanced quote is not valid FTS5 MATCH syntax - the search
        # must degrade to no text hits, not raise out of search_json.
        results = find.search_json(self.archive_root, {}, '"unterminated')
        self.assertEqual(results, [])


class RunFindJsonResultTests(unittest.TestCase):
    """run_find_json's Result contract, and its missing-index behavior -
    plan-17 point 2 requires the exact same plain message + exit code
    _related_dispatch's open_index_db call already gives --related."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = _make_index(self.archive_root)
        _add_person(self.conn, 'p-aaaaaaaaaa', 'Alice')
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_result_carries_results_key_and_clean_exit(self) -> None:
        result = find.run_find_json(self.archive_root, {}, query='Alice')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertTrue(result.ok)
        self.assertIn('results', result.data)
        self.assertEqual(result.data['results'][0]['id'], 'p-aaaaaaaaaa')

    def test_missing_index_matches_related_contract(self) -> None:
        empty_root = Path(tempfile.mkdtemp())
        try:
            related_result = find.run_related('p-aaaaaaaaaa', None, empty_root, {})
            json_result = find.run_find_json(empty_root, {}, query='Alice')
            self.assertEqual(json_result.exit_code, related_result.exit_code)
            self.assertEqual(json_result.exit_code, EXIT_FAILURE)
            self.assertFalse(json_result.ok)
            self.assertNotIn('results', json_result.data)
        finally:
            import shutil
            shutil.rmtree(empty_root, ignore_errors=True)


class JsonCliTests(unittest.TestCase):
    """The CLI layer: `fha find --json` prints exactly one JSON document and
    nothing else on stdout, and the --json/--related/--kind guard rails."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        (self.archive_root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        self.conn = _make_index(self.archive_root)
        _add_person(self.conn, 'p-aaaaaaaaaa', 'Alice')
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _parse(self, argv: list[str]) -> argparse.Namespace:
        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers()
        find.register(subs)
        args = parser.parse_args(['find', *argv])
        args.root = str(self.archive_root)
        return args

    def test_json_emits_one_parseable_document_and_nothing_else(self) -> None:
        # The documented shape (TOOLING.md §4a, tools/README.md) is the bare
        # hit list `[{id, type, label, detail}, ...]` - not Result.data's own
        # `{"results": [...]}` wrapper (P2 codex finding, round 5, PR #30).
        args = self._parse(['Alice', '--json'])
        rc, out = _run(find._run_find, args)
        self.assertEqual(rc, EXIT_CLEAN)
        lines = out.splitlines()
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertIsInstance(payload, list)
        self.assertEqual(payload[0]['id'], 'p-aaaaaaaaaa')

    def test_json_with_text_flag(self) -> None:
        args = self._parse(['--text', 'Alice', '--json'])
        rc, out = _run(find._run_find, args)
        self.assertEqual(rc, EXIT_CLEAN)
        payload = json.loads(out.strip())
        self.assertTrue(payload)

    def test_json_with_kind_and_limit(self) -> None:
        args = self._parse(['Alice', '--json', '--kind', 'person', '--limit', '5'])
        rc, out = _run(find._run_find, args)
        self.assertEqual(rc, EXIT_CLEAN)
        payload = json.loads(out.strip())
        self.assertTrue(payload)
        self.assertTrue(all(r['type'] == 'person' for r in payload))

    def test_json_with_unrecognised_kind_is_rejected(self) -> None:
        args = self._parse(['Alice', '--json', '--kind', 'nonsense'])
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = find._run_find(args)
        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('nonsense', err.getvalue())

    def test_json_with_no_query_is_refused_not_a_false_empty_success(self) -> None:
        # P2 codex finding (PR #30): a bare `fha find --json` (no positional
        # query, no --text) used to fall through with json_query == '' and
        # print a real, parseable `{"results": []}` document at exit 0 - a
        # mistyped automation call looked exactly like a genuine search that
        # found nothing. The non-JSON branch already refuses this shape;
        # --json must match it rather than silently "succeed" empty.
        args = self._parse(['--json'])
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = find._run_find(args)
        self.assertEqual(rc, EXIT_FAILURE)
        self.assertEqual(out.getvalue(), '')   # no JSON document printed
        self.assertIn('--json', err.getvalue())

    def test_json_with_empty_text_flag_is_also_refused(self) -> None:
        args = self._parse(['--text', '', '--json'])
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = find._run_find(args)
        self.assertEqual(rc, EXIT_FAILURE)
        self.assertEqual(out.getvalue(), '')

    def test_json_with_related_is_refused_naming_alternatives(self) -> None:
        args = self._parse(['--related', 'p-aaaaaaaaaa', '--json'])
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = find._run_find(args)
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertIn('--related', err.getvalue())
        self.assertIn('--text', err.getvalue())

    def test_json_on_missing_index_prints_plain_message_not_json(self) -> None:
        self.conn.close()
        (self.archive_root / '.cache' / 'index.sqlite').unlink()
        args = self._parse(['Alice', '--json'])
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = find._run_find(args)
        self.assertEqual(rc, EXIT_FAILURE)
        self.assertEqual(out.getvalue(), '')
        self.assertIn('fha index', err.getvalue())


class SearchJsonPhotoSourceKindTests(unittest.TestCase):
    """kind 'photo-source': only sources that actually own photo assets
    (source_type photo, or any photo-suffixed file in source_files) - the
    workbench's set-profile-photo picker. Hits still carry type 'source'."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = _make_index(self.archive_root)
        # A census with a PDF only, a photo-typed source, and an 'other'
        # source whose files include a scan (a photo by extension).
        _add_source(self.conn, 's-1111111111', 'Hartley census page', source_type='census')
        self.conn.execute(
            "INSERT INTO source_files(source_id, path) VALUES ('s-1111111111', 'documents/census/page_s-1111111111.pdf')")
        _add_source(self.conn, 's-2222222222', 'Hartley family portrait', source_type='photo')
        _add_source(self.conn, 's-3333333333', 'Hartley bible flyleaf', source_type='other')
        self.conn.execute(
            "INSERT INTO source_files(source_id, path) VALUES ('s-3333333333', 'photos/1900/flyleaf.JPG')")
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_filters_to_sources_with_photo_assets(self) -> None:
        results = find.search_json(self.archive_root, {}, 'Hartley',
                                   kinds=['photo-source'])
        ids = {r['id'] for r in results}
        self.assertEqual(ids, {'s-2222222222', 's-3333333333'})
        self.assertTrue(all(r['type'] == 'source' for r in results))

    def test_plain_source_kind_still_returns_everything(self) -> None:
        results = find.search_json(self.archive_root, {}, 'Hartley', kinds=['source'])
        self.assertEqual(len(results), 3)

    def test_pasted_bare_id_resolves_even_without_photos(self) -> None:
        # An explicit S-id is an explicit pick - the picker's own note says a
        # typed id always works, photo assets or not.
        results = find.search_json(self.archive_root, {}, 's-1111111111',
                                   kinds=['photo-source'])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['id'], 's-1111111111')

    def test_photo_extension_match_is_case_insensitive(self) -> None:
        results = find.search_json(self.archive_root, {}, 'flyleaf',
                                   kinds=['photo-source'])
        self.assertEqual([r['id'] for r in results], ['s-3333333333'])

    def test_alias_hits_participate_and_filter_by_photo_ownership(self) -> None:
        # P2 codex finding (round 1, PR #31): 'photo-source' used to re-run
        # only the title search, skipping the alias pass entirely - an
        # alias/stem that found the source under kind 'source' returned
        # nothing here even when the source owned a photo. It is a FILTER on
        # the normal source search, not a separate narrower search.
        _add_alias(self.conn, 'fam-portrait-1900', 's-2222222222')
        _add_alias(self.conn, 'census-stem-1900', 's-1111111111')
        self.conn.commit()
        hits = find.search_json(self.archive_root, {}, 'fam-portrait-1900',
                                kinds=['photo-source'])
        self.assertEqual([r['id'] for r in hits], ['s-2222222222'])
        self.assertEqual(hits[0]['type'], 'source')
        # The same alias mechanism still applies the photo filter: a
        # photo-less source found by alias under 'source' is dropped here.
        self.assertEqual(
            [r['id'] for r in find.search_json(self.archive_root, {},
                                               'census-stem-1900', kinds=['source'])],
            ['s-1111111111'])
        self.assertEqual(
            find.search_json(self.archive_root, {}, 'census-stem-1900',
                             kinds=['photo-source']),
            [])


class _ClosedPipe(io.StringIO):
    """A stdout whose reader has gone away, exactly as `| head` leaves it."""

    def write(self, s):    # noqa: D102 - stands in for a real pipe
        raise BrokenPipeError(32, 'Broken pipe')


class BrokenPipeTests(unittest.TestCase):
    """`fha find P-… | head` is ordinary use, not an archive problem.

    `head` closes the pipe as soon as it has its lines, so the next print
    raises BrokenPipeError. It used to travel to fha.py's catch-all and print
    `ERROR: something went wrong: [Errno 32] Broken pipe` with a `fha doctor`
    next step - blaming the archive for the shell doing its job.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        (self.archive_root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        self.conn = _make_index(self.archive_root)
        _add_person(self.conn, 'p-aaaaaaaaaa', 'Alice')
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _args(self, argv: list[str]) -> argparse.Namespace:
        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers()
        find.register(subs)
        args = parser.parse_args(argv)
        args.root = str(self.archive_root)
        return args

    def test_find_exits_quietly_when_the_reader_closes_the_pipe(self) -> None:
        args = self._args(['find', 'p-aaaaaaaaaa'])
        with redirect_stdout(_ClosedPipe()):
            rc = find._run_find(args)
        self.assertEqual(rc, EXIT_CLEAN)

    def test_search_exits_quietly_too(self) -> None:
        args = self._args(['search', 'alice'])
        with redirect_stdout(_ClosedPipe()):
            rc = find._run_search(args)
        self.assertEqual(rc, EXIT_CLEAN)


def _scandir_denying(unreadable: Path):
    """An os.scandir stand-in that refuses to list `unreadable`.

    One level below `os.walk` - and below pathlib's `rglob`, which reaches the
    disk the same way - so the same injection reproduces the pre-fix
    behaviour (the folder reads as empty) and exercises the post-fix `onerror`
    seam. chmod is no use: CI runs as root, and Windows has no equivalent.
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


class ScanFallbackCoverageTests(unittest.TestCase):
    """A search that looked in less than the whole archive must say so.

    Nothing here is deleted or certified, so an unreadable folder is no reason
    to refuse - but "not found in archive tree" over a folder nobody could
    open is a wrong answer in the one direction the human cannot check, and
    the fallback said nothing at all."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        (self.archive_root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        self.shut = self.archive_root / 'people' / 'stubs'
        self.shut.mkdir(parents=True)
        (self.shut / 'webb__nancy_P-cccccccccc.md').write_text(
            '---\nid: P-cccccccccc\nname: Nancy Webb\n---\n\n# Nancy Webb\n',
            encoding='utf-8')
        (self.archive_root / 'notes').mkdir()
        (self.archive_root / 'notes' / 'log.md').write_text(
            '# Log\n\nNothing about her here.\n', encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _scan(self, fn, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = fn(*args)
        return code, out.getvalue(), err.getvalue()

    def test_a_not_found_over_a_shut_folder_is_qualified(self) -> None:
        with unittest.mock.patch('os.scandir', new=_scandir_denying(self.shut)):
            code, out, err = self._scan(
                find._find_by_scan, 'P-cccccccccc', self.archive_root)
        self.assertEqual(code, EXIT_WARNINGS)
        self.assertIn('not found in archive tree', out)
        self.assertIn('does not mean it is not in your archive', err)
        self.assertIn('people/stubs', err)

    def test_a_clean_scan_says_nothing_extra(self) -> None:
        code, out, err = self._scan(
            find._find_by_scan, 'P-cccccccccc', self.archive_root)
        self.assertEqual(code, EXIT_CLEAN)
        self.assertIn('found in 1 file', out)
        self.assertNotIn('could not be opened', err)

    def test_text_search_says_there_may_be_more(self) -> None:
        with unittest.mock.patch('os.scandir', new=_scandir_denying(self.shut)):
            code, out, err = self._scan(
                find._find_text, 'Nancy', self.archive_root, {}, None)
        self.assertEqual(code, EXIT_WARNINGS)
        self.assertIn('No results', out)
        self.assertIn('could not be opened', err)
        self.assertIn('people/stubs', err)


# Two valid Crockford S-ids (SPEC §10) for the coverage fixtures below.
_MUTE_SID = 's-2h4k6m8p0r'      # a scan: nothing of it is in the archive as text
_SPOKEN_SID = 's-3j5n7q9s1t'    # the same evidence, typed out beside the original


class TextSearchCoverageNoteTests(unittest.TestCase):
    """A text search must say how much of the archive it could actually read.

    A source whose files are all scans, photographs or PDFs puts nothing into
    the archive as text. Search it and you get nothing back - which looks
    exactly like searching it and finding nothing, and that is how a null
    result gets read as a fact about the family rather than a fact about the
    corpus. It has already cost a person her surname: the name was searched
    for, found only in one claim's value, judged invented, and struck, while it
    sat in plain handwriting on a 22-page image-only scan (#46).

    The note therefore prints on a HIT as readily as on a miss. Three results
    drawn from a corpus half of which nobody could read are as misleading as
    none, and worse in practice, because hits feel like confirmation."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        (self.archive_root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        self.conn = _make_index(self.archive_root)
        _add_source(self.conn, _MUTE_SID, 'Hand-drawn family chart')
        self._add_file(_MUTE_SID, 'documents/charts/chart_S-2h4k6m8p0r.pdf', 'page-1')
        _add_source(self.conn, _SPOKEN_SID, 'Farm interview')
        self._add_file(
            _SPOKEN_SID,
            'documents/interviews/farm-transcript_S-3j5n7q9s1t.md', 'transcript')
        self.conn.execute(
            'INSERT INTO transcripts_fts(source_id, path, content) VALUES (?,?,?)',
            (_SPOKEN_SID, 'documents/interviews/farm-transcript_S-3j5n7q9s1t.md',
             'Rose Harkness kept the west field.'))
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _add_file(self, sid: str, path: str, role: str, exists: int = 1) -> None:
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role, derived, '
            'exists_on_disk, in_inventory) VALUES (?,?,?,0,?,1)',
            (sid, path, role, exists))

    def _search(self, query: str, *, indexed: bool = True):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = find._find_text(
                query, self.archive_root, {}, self.conn if indexed else None)
        return code, buf.getvalue()

    def test_a_miss_names_the_sources_it_could_not_read(self) -> None:
        code, out = self._search('Harkness-and-nobody-else')
        self.assertEqual(code, EXIT_CLEAN)
        self.assertIn('No results', out)
        self.assertIn('no searchable text for 1 of its 2 sources', out)
        self.assertIn('could not look everywhere', out)

    def test_a_hit_carries_the_same_caveat(self) -> None:
        # The general case, not just the null one: a search that returned
        # something out of a corpus it could only half read is exactly as
        # misleading as one that returned nothing.
        code, out = self._search('Harkness')
        self.assertEqual(code, EXIT_CLEAN)
        self.assertIn('Found 1 result(s)', out)
        self.assertIn('no searchable text for 1 of its 2 sources', out)
        self.assertIn('not the whole picture', out)

    def test_the_note_names_only_commands_that_exist(self) -> None:
        # `fha source transcribe` does not exist. Naming it in shipped output
        # would be a promise this program cannot keep, so the next steps are
        # the extract verb (for a PDF with its own text layer) and reading the
        # file. See the write-up in TOOLING §4a.
        _, out = self._search('nothing-matches-this')
        self.assertIn('fha source extract', out)
        self.assertNotIn('fha source transcribe', out)

    def test_an_archive_that_can_be_read_throughout_says_nothing(self) -> None:
        self.conn.execute('DELETE FROM source_files WHERE source_id=?', (_MUTE_SID,))
        self.conn.execute('DELETE FROM sources WHERE id=?', (_MUTE_SID,))
        self.conn.commit()
        _, out = self._search('Harkness')
        self.assertNotIn('no searchable text', out)

    def test_a_promised_transcript_that_is_not_on_disk_is_not_text(self) -> None:
        # A `files:` line naming a transcript nobody synced is exactly as
        # unsearchable as no transcript at all, so it must not buy coverage.
        self.conn.execute('DELETE FROM transcripts_fts')
        self.conn.execute(
            'UPDATE source_files SET exists_on_disk=0 WHERE source_id=?',
            (_SPOKEN_SID,))
        self.conn.commit()
        _, out = self._search('anything')
        self.assertIn('no searchable text for 2 of its 2 sources', out)

    def test_a_source_with_no_files_is_not_counted_as_unreadable(self) -> None:
        # An online record with nothing attached has no evidence to transcribe;
        # counting it would inflate the number and teach the reader to ignore it.
        _add_source(self.conn, 's-4k6m8p0r2t', 'A citation with no attachment')
        self.conn.commit()
        _, out = self._search('anything')
        self.assertIn('no searchable text for 1 of its 3 sources', out)

    def test_without_an_index_the_question_is_declared_unanswered(self) -> None:
        # Silence on coverage is the failure mode; a scan-only search reads even
        # less than an indexed one, so it must not imply it read everything.
        _, out = self._search('anything', indexed=False)
        self.assertIn('could not check which sources have no searchable text', out)
        self.assertIn('fha index', out)


# Four transcripts of the same kind of evidence, in the four states the
# transcribe-source marker contract defines.
_DRAFTED_SID = 's-5m7p9r1t3v'    # a machine read the scan; nobody has checked it
_CHECKED_SID = 's-6n8q0s2v4w'    # a human compared it to the image
_TYPED_SID = 's-7p0r2t4w6x'      # a human typed it, or extract dumped it: no marker
_BROKEN_SID = 's-8q1s3v5x7y'     # its marker is damaged and cannot be read


class UncheckedTranscriptHitTests(unittest.TestCase):
    """A hit that came from an unchecked machine reading says so, on its line.

    #46 closed the hole where a search could not see an image-only source and a
    null result was read as a finding. This is the same hole facing the other
    way: a transcript a model produced and nobody checked against the image is
    searchable and indistinguishable from evidence, and it does not fail
    quietly - it returns confident hits, which nobody re-examines. By the
    reasoning in AGENTS.md's "You cannot conclude absence from a search", that
    is a coverage claim too.

    The mark is per RESULT, not per search: one search can return a transcript a
    human has compared to the scan and a transcript nobody has ever looked at,
    and the difference between those two matters at the line. Which is also why
    `unmarked` and `verified` must stay silent - flag every transcript and the
    flag stops meaning anything."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        (self.archive_root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        self.conn = _make_index(self.archive_root)

        page = '[Page 1]\nHartlee kept the {field} field.\n'
        self.paths = {}
        for sid, name, body in (
            (_DRAFTED_SID, 'drafted', page.format(field='west')
             + '\n<!-- AI-DRAFT 2026-08-16 a-model - transcript of chart.jpg, '
               'pages 1-1; not yet checked against the image by a human -->\n'),
            (_CHECKED_SID, 'checked', page.format(field='east')
             + '\n<!-- AI-ACCEPTED 2026-08-16 a-model - transcript of '
               'chart.jpg (accepted 2026-08-20) -->\n'),
            (_TYPED_SID, 'typed', page.format(field='north')),
            (_BROKEN_SID, 'broken', page.format(field='south')
             + '\n<!-- AI-DRAFT 2026-08-16 a-model\n'),
        ):
            _add_source(self.conn, sid, f'A {name} transcript')
            rel = f'documents/charts/{name}-transcript_{sid.upper()}.md'
            self.paths[name] = rel
            self.conn.execute(
                'INSERT INTO transcripts_fts(source_id, path, content) '
                'VALUES (?,?,?)', (sid, rel, body))

        # The other surface: a claim value. `fha index` puts a source record's
        # whole body into notes_fts, `## Claims` block included, so this is what
        # a claim-value hit looks like to the search. It is deliberately the
        # record of the source whose transcript IS unreviewed - the same source,
        # a different surface - because that is the pair an implementation which
        # marked the search (or the source) instead of the hit would get wrong.
        self.paths['record'] = f'sources/other/drafted_{_DRAFTED_SID.upper()}.md'
        self.conn.execute(
            'INSERT INTO notes_fts(path, content) VALUES (?,?)',
            (self.paths['record'],
             '## Claims\n\n- id: C-aaaaaaaaaa\n  type: name\n'
             '  value: Hartlee\n  status: accepted\n'))
        # And a person profile carrying AI-DRAFT biography prose. The SAME
        # marker word, in a file that is not a transcript of anything: nothing
        # here was read off a picture, so calling it an unchecked transcript
        # would be a false statement about where the words came from.
        self.paths['profile'] = 'people/stubs/hartlee__rose_P-aaaaaaaaaa.md'
        self.conn.execute(
            'INSERT INTO notes_fts(path, content) VALUES (?,?)',
            (self.paths['profile'],
             '## Biography\n\nRose Hartlee farmed the west field.\n'
             '<!-- AI-DRAFT 2026-08-16 a-model - biography from 2 claims -->\n'))
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _search(self, query: str):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = find._find_text(query, self.archive_root, {}, self.conn)
        return code, buf.getvalue()

    @staticmethod
    def _marked(out: str, rel: str) -> bool:
        """Is the result line for `rel` carrying the unchecked label?"""
        for line in out.splitlines():
            if line.strip().startswith(rel):
                return '[unchecked AI transcript]' in line
        raise AssertionError(f'{rel} is not in the results:\n{out}')

    def test_an_unreviewed_transcript_hit_is_marked(self) -> None:
        code, out = self._search('Hartlee')
        self.assertEqual(code, EXIT_CLEAN)
        self.assertTrue(self._marked(out, self.paths['drafted']))

    def test_a_verified_transcript_hit_is_silent(self) -> None:
        # A human compared this one to the image. Marking it would say the
        # opposite of what is true.
        _, out = self._search('Hartlee')
        self.assertFalse(self._marked(out, self.paths['checked']))

    def test_an_unmarked_transcript_hit_is_silent(self) -> None:
        # `unmarked` is not `unreviewed`. A human's typing and an `fha source
        # extract` dump of a PDF's own text layer both carry no marker, and
        # they are most of the transcripts in an archive: flag them and the
        # reader learns to skip the flag, which is how the ones that matter get
        # read as evidence.
        _, out = self._search('Hartlee')
        self.assertFalse(self._marked(out, self.paths['typed']))

    def test_a_damaged_marker_is_marked_failing_closed(self) -> None:
        # Its marker has no closing `-->`, so draft cannot be told from checked.
        # Treated as unchecked, exactly as _lib.strip_unaccepted_drafts
        # withholds rather than guesses.
        _, out = self._search('Hartlee')
        self.assertTrue(self._marked(out, self.paths['broken']))

    def test_a_record_body_hit_is_not_a_transcript_hit(self) -> None:
        # The match here is on what someone wrote down in a claim value, not on
        # a machine's reading of a picture.
        _, out = self._search('Hartlee')
        self.assertFalse(self._marked(out, self.paths['record']))

    def test_a_drafted_biography_hit_is_not_a_transcript_hit(self) -> None:
        # The surface decides, not the marker word. An AI-drafted biography
        # carries the identical `<!-- AI-DRAFT ... -->` comment - it is the same
        # marker pair - but nobody read a picture to write it, and the profile
        # is not a transcript of anything. Marking it would say something false
        # about where the words came from, and put the label on a large share of
        # ordinary profile hits.
        _, out = self._search('Hartlee')
        self.assertFalse(self._marked(out, self.paths['profile']))

    def test_the_mark_explains_itself_once(self) -> None:
        _, out = self._search('Hartlee')
        self.assertIn('the image is the evidence', out)
        self.assertIn('Nobody has checked them against the image', out)
        # Once for the whole result list, not once per hit: two of these five
        # results are marked, and repeating the paragraph between them would
        # bury the results it is warning about.
        self.assertEqual(out.count('Nobody has checked them against the image'), 1)

    def test_nothing_is_said_when_every_hit_is_trustworthy(self) -> None:
        # Only the checked and the unmarked transcripts match this one.
        self.conn.execute('DELETE FROM transcripts_fts WHERE source_id IN (?,?)',
                          (_DRAFTED_SID, _BROKEN_SID))
        self.conn.commit()
        _, out = self._search('Hartlee')
        self.assertIn('Found 4 result(s)', out)
        self.assertNotIn('unchecked AI transcript', out)

    def test_the_regex_pass_marks_what_fts_did_not_match(self) -> None:
        # The two halves must agree. FTS matches whole tokens and the fallback
        # regex matches substrings, so a transcript really can be found only by
        # the second pass - and printing it unmarked there would make the label
        # depend on which pass happened to find it.
        body = ('[Page 1]\nHartlee kept the west field.\n'
                '\n<!-- AI-DRAFT 2026-08-16 a-model - pages 1-1 -->\n')
        rel = self.paths['drafted']
        on_disk = self.archive_root / rel
        on_disk.parent.mkdir(parents=True, exist_ok=True)
        on_disk.write_text(body, encoding='utf-8')
        self.conn.execute('UPDATE transcripts_fts SET content=? WHERE source_id=?',
                          (body, _DRAFTED_SID))
        self.conn.commit()
        _, out = self._search('artlee')          # a substring, not a token
        self.assertTrue(self._marked(out, rel))

    def test_an_external_documents_root_is_marked_too(self) -> None:
        # The index keys a transcript by its alias-form path ('documents/…');
        # the scan pass keys a file in an EXTERNAL documents root by its
        # absolute path, because it is not under the archive root at all. An
        # archive whose documents live on another drive must get the same
        # answer as one that keeps them inside.
        outside = Path(self._tmp.name + '-docs')
        outside.mkdir()
        self.addCleanup(shutil.rmtree, outside, True)
        body = ('[Page 1]\nHartlee kept the west field.\n'
                '\n<!-- AI-DRAFT 2026-08-16 a-model - pages 1-1 -->\n')
        (outside / 'charts').mkdir()
        on_disk = outside / 'charts' / Path(self.paths['drafted']).name
        on_disk.write_text(body, encoding='utf-8')
        self.conn.execute('UPDATE transcripts_fts SET content=? WHERE source_id=?',
                          (body, _DRAFTED_SID))
        self.conn.commit()
        buf = io.StringIO()
        with redirect_stdout(buf):
            find._find_text('artlee', self.archive_root,
                            {'roots': {'documents': str(outside)}}, self.conn)
        self.assertTrue(self._marked(buf.getvalue(), str(on_disk)))


class JsonCoverageNoteTests(unittest.TestCase):
    """`fha find --json` says what it could not read - on stderr, never stdout.

    The human search has said this since #46. The JSON surface had not, and its
    reader is usually another model - the exact reader whose null-result
    reasoning cost a person her surname. So the caveat has to reach it.

    It reaches it on STDERR because stdout is a parsed contract: a bare hit
    array, and anything already reading it must keep reading exactly what it
    read before. Nothing on stdout changes here at all."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        (self.archive_root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        self.conn = _make_index(self.archive_root)
        # One source the archive holds no words for (a scan), and one that was
        # typed out - the D14 fixture, seen through the JSON door.
        _add_source(self.conn, _MUTE_SID, 'Hand-drawn family chart')
        self._add_file(_MUTE_SID, 'documents/charts/chart_S-2h4k6m8p0r.pdf', 'page-1')
        _add_source(self.conn, _SPOKEN_SID, 'Farm interview')
        self._add_file(
            _SPOKEN_SID,
            'documents/interviews/farm-transcript_S-3j5n7q9s1t.md', 'transcript')
        self.conn.execute(
            'INSERT INTO transcripts_fts(source_id, path, content) VALUES (?,?,?)',
            (_SPOKEN_SID, 'documents/interviews/farm-transcript_S-3j5n7q9s1t.md',
             'Rose Harkness kept the west field.'))
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _add_file(self, sid: str, path: str, role: str) -> None:
        self.conn.execute(
            'INSERT INTO source_files(source_id, path, role, derived, '
            'exists_on_disk, in_inventory) VALUES (?,?,?,0,1,1)', (sid, path, role))

    def _parse(self, argv: list[str]) -> argparse.Namespace:
        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers()
        find.register(subs)
        args = parser.parse_args(['find', *argv])
        args.root = str(self.archive_root)
        return args

    def _json_cli(self, query: str):
        """Run `fha find --json <query>` and return (rc, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = find._run_find(self._parse([query, '--json']))
        return rc, out.getvalue(), err.getvalue()

    def test_the_caveat_reaches_the_json_caller_on_stderr(self) -> None:
        rc, out, err = self._json_cli('Harkness')
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertTrue(json.loads(out))
        self.assertIn('no searchable text for 1 of its 2 sources', err)
        self.assertIn('not the whole picture', err)

    def test_stdout_stays_the_bare_array_while_the_note_fires(self) -> None:
        # The pin: whatever else this command says, stdout is one JSON document
        # and nothing else. A consumer that pipes stdout into a parser must not
        # be able to tell that the note exists.
        _rc, out, err = self._json_cli('Harkness')
        self.assertTrue(err.strip())              # the note really did fire
        self.assertEqual(len(out.splitlines()), 1)
        payload = json.loads(out)
        self.assertEqual(out, json.dumps(payload) + '\n')
        for hit in payload:
            self.assertLessEqual({'id', 'type', 'label', 'detail'}, set(hit))

    def test_a_miss_carries_the_caveat_and_still_prints_an_empty_array(self) -> None:
        # The #46 shape exactly: nothing came back. stdout is still the array a
        # caller parses, and the reason the nothing might not mean anything is
        # on stderr beside it.
        rc, out, err = self._json_cli('Harkness-and-nobody-else')
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual(out, '[]\n')
        self.assertIn('no searchable text for 1 of its 2 sources', err)
        self.assertIn('could not look everywhere', err)

    def test_an_archive_with_nothing_to_caveat_is_silent(self) -> None:
        # Silence is correct when there is nothing to say. A warning that fires
        # on every search is a warning nobody reads.
        self.conn.execute('DELETE FROM source_files WHERE source_id=?', (_MUTE_SID,))
        self.conn.execute('DELETE FROM sources WHERE id=?', (_MUTE_SID,))
        self.conn.commit()
        rc, out, err = self._json_cli('Harkness')
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertTrue(json.loads(out))
        self.assertEqual(err, '')

    def test_the_note_is_the_same_sentence_the_human_search_prints(self) -> None:
        # One composer, not two: if these ever drift, the two surfaces are
        # making different claims about the same archive.
        _rc, _out, err = self._json_cli('Harkness')
        self.assertEqual(
            err.strip(),
            find._searchable_text_note(self.conn, found_something=True))

    def test_the_engine_hands_callers_the_same_note(self) -> None:
        # serve reads it through here rather than composing its own.
        hits, note = find.search_json_with_coverage(
            self.archive_root, {}, 'Harkness')
        self.assertTrue(hits)
        self.assertIn('no searchable text for 1 of its 2 sources', note)
        self.assertEqual(hits, find.search_json(self.archive_root, {}, 'Harkness'))

    def test_a_kind_filtered_search_is_not_charged_for_the_count(self) -> None:
        # A --kind lookup is "which record do you mean", not "what does this
        # archive say" - nobody shows the caveat there, and the count is two
        # indexed scans the caller will throw away. The assertion is that the
        # work does not happen, not merely that the answer is dropped: a
        # response that omits the note while still counting is the bug.
        calls = []
        real = find._count_sources_without_text
        find._count_sources_without_text = lambda conn: (calls.append(1), real(conn))[1]
        try:
            filtered, note = find.search_json_with_coverage(
                self.archive_root, {}, 'Harkness', kinds=['source'])
            self.assertEqual(calls, [])
            self.assertIsNone(note)
            # And the search itself is untouched: same hits as the unfiltered
            # call, minus the kinds it filtered out.
            self.assertEqual(
                filtered,
                [h for h in find.search_json(self.archive_root, {}, 'Harkness')
                 if h['type'] == 'source'])
            self.assertEqual(len(calls), 1)   # that unfiltered call DID count
        finally:
            find._count_sources_without_text = real


class JsonUncheckedKeyTests(unittest.TestCase):
    """A JSON hit whose words nobody has checked against the image says so.

    The same D15 rule the CLI prints as `[unchecked AI transcript]`, carried to
    the machine surface as one additive key. Present ONLY when true: a key that
    is there-or-not-there cannot be misread as "checked", which is a claim
    nobody has made about an unmarked transcript, a record body or a caption.

    THE SURFACE DECIDES, NOT THE MARKER WORD - so an AI-DRAFT biography, which
    carries the identical marker comment, is never keyed."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        (self.archive_root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        self.conn = _make_index(self.archive_root)

        page = '[Page 1]\nHartlee kept the {field} field.\n'
        self.paths = {}
        for sid, name, body in (
            (_DRAFTED_SID, 'drafted', page.format(field='west')
             + '\n<!-- AI-DRAFT 2026-08-16 a-model - transcript of chart.jpg, '
               'pages 1-1; not yet checked against the image by a human -->\n'),
            (_CHECKED_SID, 'checked', page.format(field='east')
             + '\n<!-- AI-ACCEPTED 2026-08-16 a-model - transcript of '
               'chart.jpg (accepted 2026-08-20) -->\n'),
            (_TYPED_SID, 'typed', page.format(field='north')),
            (_BROKEN_SID, 'broken', page.format(field='south')
             + '\n<!-- AI-DRAFT 2026-08-16 a-model\n'),
        ):
            _add_source(self.conn, sid, f'A {name} transcript')
            rel = f'documents/charts/{name}-transcript_{sid.upper()}.md'
            self.paths[name] = rel
            self.conn.execute(
                'INSERT INTO transcripts_fts(source_id, path, content) '
                'VALUES (?,?,?)', (sid, rel, body))

        # A claim value in a source record's body, and an AI-drafted biography
        # in a person profile: both reach the ranked search through notes_fts,
        # and neither is anybody's reading of a picture.
        self.paths['record'] = f'sources/other/drafted_{_DRAFTED_SID.upper()}.md'
        self.conn.execute(
            'INSERT INTO notes_fts(path, content) VALUES (?,?)',
            (self.paths['record'],
             '## Claims\n\n- id: C-aaaaaaaaaa\n  type: name\n'
             '  value: Hartlee\n  status: accepted\n'))
        self.paths['profile'] = 'people/stubs/hartlee__rose_P-aaaaaaaaaa.md'
        self.conn.execute(
            'INSERT INTO notes_fts(path, content) VALUES (?,?)',
            (self.paths['profile'],
             '## Biography\n\nRose Hartlee farmed the west field.\n'
             '<!-- AI-DRAFT 2026-08-16 a-model - biography from 2 claims -->\n'))
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _hits(self, query: str = 'Hartlee') -> dict:
        return {h['id']: h for h in
                find.search_json(self.archive_root, {}, query, limit=50)}

    def test_an_unreviewed_transcript_hit_carries_the_key(self) -> None:
        self.assertIs(self._hits()[self.paths['drafted']].get('unchecked'), True)

    def test_a_damaged_marker_carries_it_too_failing_closed(self) -> None:
        # Draft can no longer be told from checked, so it is treated as
        # unchecked - the posture _lib.strip_unaccepted_drafts already takes.
        self.assertIs(self._hits()[self.paths['broken']].get('unchecked'), True)

    def test_a_verified_transcript_hit_has_no_key_at_all(self) -> None:
        # Not `"unchecked": false` - absent. A human compared this one to the
        # image, and the key's job is to warn, not to certify.
        self.assertNotIn('unchecked', self._hits()[self.paths['checked']])

    def test_an_unmarked_transcript_hit_has_no_key(self) -> None:
        # `fha source extract`'s dump of a PDF's own text layer, and anything a
        # human typed. Most transcripts are these; keying them would train a
        # reader to ignore the key.
        self.assertNotIn('unchecked', self._hits()[self.paths['typed']])

    def test_a_claim_value_hit_has_no_key(self) -> None:
        self.assertNotIn('unchecked', self._hits()[self.paths['record']])

    def test_a_drafted_biography_hit_has_no_key(self) -> None:
        # The identical `<!-- AI-DRAFT … -->` comment on a file that is not a
        # transcript of anything. The surface decides.
        self.assertNotIn('unchecked', self._hits()[self.paths['profile']])

    def test_record_hits_keep_exactly_the_four_documented_keys(self) -> None:
        # The back-compat pin: nothing that is not an unchecked transcript hit
        # gains anything at all.
        _add_person(self.conn, 'p-aaaaaaaaaa', 'Rose Hartlee')
        _add_alias(self.conn, 'Rose Hartlee', 'p-aaaaaaaaaa', 'name')
        self.conn.commit()
        for hit in find.search_json(self.archive_root, {}, 'Rose Hartlee'):
            self.assertEqual(set(hit), {'id', 'type', 'label', 'detail'})

    def test_the_cli_json_document_carries_the_key(self) -> None:
        # End to end through the printed document, not just the engine.
        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers()
        find.register(subs)
        args = parser.parse_args(['find', 'Hartlee', '--json', '--limit', '50'])
        args.root = str(self.archive_root)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = find._run_find(args)
        self.assertEqual(rc, EXIT_CLEAN)
        by_id = {h['id']: h for h in json.loads(out.getvalue())}
        self.assertIs(by_id[self.paths['drafted']].get('unchecked'), True)
        self.assertNotIn('unchecked', by_id[self.paths['checked']])


if __name__ == '__main__':
    unittest.main()
