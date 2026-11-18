"""
test_claim_date_order.py - claims on the same date keep one order every build.

`_lib.claim_date_order_sql` orders claims by date for every timeline, page,
packet and export. Two claims with the same date (two births both "about
1840") used to come back in whatever order SQLite's query plan produced, so
the example site's committed showcase swapped them between builds and the CI
drift check failed about half the time. The helper now ends with the order
`fha index` wrote the claims (`rowid`, the archive's file order). These tests
pin that contract; the original flake depends on the query plan and cannot be
forced on demand.
"""

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

from _lib import claim_date_order_sql


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE claims(id TEXT, date_edtf TEXT, date_min TEXT, date_max TEXT)')
    conn.execute('CREATE TABLE claim_persons(claim_id TEXT, person_id TEXT)')
    # Written in this order by "the index"; ids deliberately sort the other way.
    rows = [('c-z', '1840~', '1839-01-01', '1841-12-31'),
            ('c-m', '1840~', '1839-01-01', '1841-12-31'),
            ('c-a', '1840~', '1839-01-01', '1841-12-31'),
            ('c-early', '1830', '1830-01-01', '1830-12-31'),
            ('c-undated', '', '', '')]
    conn.executemany('INSERT INTO claims VALUES (?,?,?,?)', rows)
    for cid, *_rest in rows:
        conn.execute('INSERT INTO claim_persons VALUES (?, ?)', (cid, 'p-1'))
        conn.execute('INSERT INTO claim_persons VALUES (?, ?)', (cid, 'p-2'))
    return conn


class ClaimDateOrderTests(unittest.TestCase):
    def test_same_date_claims_keep_index_order(self):
        conn = _db()
        got = [r[0] for r in conn.execute(
            f'SELECT id FROM claims c ORDER BY {claim_date_order_sql("c")}')]
        self.assertEqual(got, ['c-early', 'c-z', 'c-m', 'c-a', 'c-undated'])

    def test_same_order_through_a_distinct_join(self):
        # The shape the site's timeline query uses: DISTINCT over a join that
        # lists each claim twice, which sorts through a temporary table.
        conn = _db()
        got = [r[0] for r in conn.execute(
            'SELECT DISTINCT c.id FROM claim_persons cp JOIN claims c ON cp.claim_id = c.id '
            f'ORDER BY {claim_date_order_sql("c")}')]
        self.assertEqual(got, ['c-early', 'c-z', 'c-m', 'c-a', 'c-undated'])

    def test_the_last_key_is_the_index_write_order(self):
        self.assertTrue(claim_date_order_sql('c').endswith('c.rowid ASC'))
        self.assertTrue(claim_date_order_sql('').endswith(' rowid ASC'))


if __name__ == '__main__':
    unittest.main()
