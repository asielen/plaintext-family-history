import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import cooccur
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


def _add_source(conn, sid, title, source_type):
    conn.execute("INSERT INTO sources(id, title, source_type, path) VALUES (?,?,?,?)",
                 (sid, title, source_type, f'{sid}.md'))


def _link_source_people(conn, sid, *pids):
    for pid in pids:
        conn.execute("INSERT INTO source_people(source_id, person_id) VALUES (?,?)", (sid, pid))


class CooccurPersonTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = _make_index(self.archive_root)
        _add_person(self.conn, 'p-aaaaaaaaaa', 'Alice')
        _add_person(self.conn, 'p-bbbbbbbbbb', 'Bob')
        _add_source(self.conn, 's-1111111111', 'Census', 'census')
        _add_source(self.conn, 's-2222222222', 'Newspaper', 'newspaper')

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_pair_below_threshold_excluded(self) -> None:
        _link_source_people(self.conn, 's-1111111111', 'p-aaaaaaaaaa', 'p-bbbbbbbbbb')
        self.conn.commit()

        result = cooccur.run_cooccur(self.archive_root, threshold=2)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['person_pairs'], [])

    def test_pair_meeting_threshold_included_with_variety(self) -> None:
        _link_source_people(self.conn, 's-1111111111', 'p-aaaaaaaaaa', 'p-bbbbbbbbbb')
        _link_source_people(self.conn, 's-2222222222', 'p-aaaaaaaaaa', 'p-bbbbbbbbbb')
        self.conn.commit()

        result = cooccur.run_cooccur(self.archive_root, threshold=2)
        self.assertEqual(len(result['person_pairs']), 1)
        pair = result['person_pairs'][0]
        self.assertEqual(pair['source_count'], 2)
        self.assertEqual(pair['variety'], 2)

    def test_existing_relationship_excludes_pair(self) -> None:
        _link_source_people(self.conn, 's-1111111111', 'p-aaaaaaaaaa', 'p-bbbbbbbbbb')
        _link_source_people(self.conn, 's-2222222222', 'p-aaaaaaaaaa', 'p-bbbbbbbbbb')
        self.conn.execute(
            "INSERT INTO relationships(person_id, rel, other_id) VALUES ('p-aaaaaaaaaa','spouse','p-bbbbbbbbbb')"
        )
        self.conn.commit()

        result = cooccur.run_cooccur(self.archive_root, threshold=2)
        self.assertEqual(result['person_pairs'], [])

    def test_dismissed_tombstone_excludes_pair(self) -> None:
        _link_source_people(self.conn, 's-1111111111', 'p-aaaaaaaaaa', 'p-bbbbbbbbbb')
        _link_source_people(self.conn, 's-2222222222', 'p-aaaaaaaaaa', 'p-bbbbbbbbbb')
        self.conn.commit()

        dismissed_path = self.archive_root / '.cache' / 'cooccur_dismissed.json'
        dismissed_path.write_text(json.dumps({
            'pairs': [['p-aaaaaaaaaa', 'p-bbbbbbbbbb']],
            'generated': '2026-06-19',
        }), encoding='utf-8')

        result = cooccur.run_cooccur(self.archive_root, threshold=2)
        self.assertEqual(result['person_pairs'], [])

    def test_missing_tombstone_is_not_an_error(self) -> None:
        _link_source_people(self.conn, 's-1111111111', 'p-aaaaaaaaaa', 'p-bbbbbbbbbb')
        _link_source_people(self.conn, 's-2222222222', 'p-aaaaaaaaaa', 'p-bbbbbbbbbb')
        self.conn.commit()

        self.assertFalse((self.archive_root / '.cache' / 'cooccur_dismissed.json').exists())
        result = cooccur.run_cooccur(self.archive_root, threshold=2)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(result['person_pairs']), 1)

    def test_claim_participants_without_source_people_still_pair(self) -> None:
        # Two people named only via claim_persons (no source_people frontmatter
        # list) on two different sources should still be detected as a
        # co-occurring pair — source_people and claim_persons are unioned.
        for cid, sid in (('c-aaaaaaaaaa', 's-1111111111'), ('c-bbbbbbbbbb', 's-2222222222')):
            self.conn.execute(
                "INSERT INTO claims(id, source_id, type, value, status) VALUES (?,?,?,?,?)",
                (cid, sid, 'residence', 'lived together', 'accepted'),
            )
            for pos, pid in enumerate(('p-aaaaaaaaaa', 'p-bbbbbbbbbb')):
                self.conn.execute(
                    'INSERT INTO claim_persons(claim_id, person_id, position, role) VALUES (?,?,?,?)',
                    (cid, pid, pos, None),
                )
        self.conn.commit()

        result = cooccur.run_cooccur(self.archive_root, threshold=2)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(result['person_pairs']), 1)
        pair = result['person_pairs'][0]
        self.assertEqual(pair['source_count'], 2)

    def test_missing_required_table_returns_failed_status(self) -> None:
        self.conn.execute('DROP TABLE relationships')
        self.conn.commit()

        result = cooccur.run_cooccur(self.archive_root, threshold=2)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['person_pairs'], [])
        self.assertEqual(result['org_groups'], [])

    def test_missing_required_column_returns_failed_status(self) -> None:
        # All required tables exist (table probe passes) but claims is missing
        # a column _org_recurrence's query selects — must surface the
        # documented incompatible-schema message rather than an uncaught
        # OperationalError.
        self.conn.execute('ALTER TABLE claims RENAME TO claims_old')
        self.conn.execute(
            '''CREATE TABLE claims(
                 id TEXT PRIMARY KEY, source_id TEXT NOT NULL, type TEXT NOT NULL,
                 value TEXT NOT NULL, status TEXT NOT NULL
               )'''
        )
        self.conn.commit()

        result = cooccur.run_cooccur(self.archive_root, threshold=2)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['person_pairs'], [])
        self.assertEqual(result['org_groups'], [])


class CooccurOrgTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self._tmp.name)
        self.conn = _make_index(self.archive_root)
        _add_person(self.conn, 'p-aaaaaaaaaa', 'Alice')
        _add_person(self.conn, 'p-bbbbbbbbbb', 'Bob')
        _add_source(self.conn, 's-1111111111', 'Census', 'census')
        _add_source(self.conn, 's-2222222222', 'Obituary', 'newspaper')

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _insert_claim(self, cid, sid, ctype, value, persons, *, subtype=None):
        self.conn.execute(
            "INSERT INTO claims(id, source_id, type, subtype, value, status) VALUES (?,?,?,?,?,'accepted')",
            (cid, sid, ctype, subtype, value),
        )
        for pos, pid in enumerate(persons):
            self.conn.execute(
                'INSERT INTO claim_persons(claim_id, person_id, position, role) VALUES (?,?,?,?)',
                (cid, pid, pos, None),
            )

    def test_recurring_employer_across_two_people_is_a_hub(self) -> None:
        self._insert_claim('c-aaaaaaaaaa', 's-1111111111', 'occupation',
                            'Plains Junction Railroad', ['p-aaaaaaaaaa'])
        self._insert_claim('c-bbbbbbbbbb', 's-2222222222', 'occupation',
                            'Plains Junction Railroad', ['p-bbbbbbbbbb'])
        self.conn.commit()

        result = cooccur.run_cooccur(self.archive_root, threshold=2)
        self.assertEqual(len(result['org_groups']), 1)
        group = result['org_groups'][0]
        self.assertEqual(group['label'], 'Plains Junction Railroad')
        self.assertEqual(group['category'], 'occupation')
        self.assertEqual(group['person_count'], 2)

    def test_single_person_single_source_not_a_hub(self) -> None:
        self._insert_claim('c-aaaaaaaaaa', 's-1111111111', 'occupation',
                            'Plains Junction Railroad', ['p-aaaaaaaaaa'])
        self.conn.commit()

        result = cooccur.run_cooccur(self.archive_root, threshold=2)
        self.assertEqual(result['org_groups'], [])

    def test_same_label_in_different_categories_does_not_collapse(self) -> None:
        self._insert_claim('c-aaaaaaaaaa', 's-1111111111', 'occupation',
                            'Grand Army Hall', ['p-aaaaaaaaaa'])
        self._insert_claim('c-bbbbbbbbbb', 's-2222222222', 'military',
                            'Grand Army Hall', ['p-bbbbbbbbbb'])
        self.conn.commit()

        result = cooccur.run_cooccur(self.archive_root, threshold=2)
        self.assertEqual(result['org_groups'], [])

    def test_membership_style_event_is_included_as_hub(self) -> None:
        self._insert_claim('c-aaaaaaaaaa', 's-1111111111', 'event',
                            'Odd Fellows Lodge', ['p-aaaaaaaaaa'], subtype='membership')
        self._insert_claim('c-bbbbbbbbbb', 's-2222222222', 'event',
                            'Odd Fellows Lodge', ['p-bbbbbbbbbb'], subtype='membership')
        self.conn.commit()

        result = cooccur.run_cooccur(self.archive_root, threshold=2)
        self.assertEqual(len(result['org_groups']), 1)
        self.assertEqual(result['org_groups'][0]['category'], 'membership')

    def test_non_membership_event_is_not_an_org_hub(self) -> None:
        self._insert_claim('c-aaaaaaaaaa', 's-1111111111', 'event',
                            'County fair', ['p-aaaaaaaaaa'])
        self._insert_claim('c-bbbbbbbbbb', 's-2222222222', 'event',
                            'County fair', ['p-bbbbbbbbbb'])
        self.conn.commit()

        result = cooccur.run_cooccur(self.archive_root, threshold=2)
        self.assertEqual(result['org_groups'], [])


if __name__ == '__main__':
    unittest.main()
