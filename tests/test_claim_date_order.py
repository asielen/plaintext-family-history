"""
test_claim_date_order.py - claims on the same date keep one order every build.

`_lib.claim_date_order_sql` orders claims by date for every timeline, page,
packet and export. Two claims with the same date (two births both "about
1840") used to come back in whatever order SQLite's query plan produced, so
the example site's committed showcase swapped them between builds and the CI
drift check failed about half the time. The helper now ends with keys taken
from the records - the source's id, then the claim's order in its file - so
the order does not depend on the filesystem's listing order or on whether a
source was reindexed on its own.
"""

import io
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import index as index_mod
from _lib import claim_date_order_sql, load_fha_yaml


def _memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE claims(id TEXT, source_id TEXT, date_edtf TEXT, '
                 'date_min TEXT, date_max TEXT)')
    conn.execute('CREATE TABLE claim_persons(claim_id TEXT, person_id TEXT)')
    # Inserted in an order that matches neither the source ids nor the claim ids.
    rows = [('c-z', 's-b', '1840~', '1839-01-01', '1841-12-31'),
            ('c-m', 's-a', '1840~', '1839-01-01', '1841-12-31'),
            ('c-a', 's-b', '1840~', '1839-01-01', '1841-12-31'),
            ('c-early', 's-c', '1830', '1830-01-01', '1830-12-31'),
            ('c-undated', 's-a', '', '', '')]
    conn.executemany('INSERT INTO claims VALUES (?,?,?,?,?)', rows)
    for cid, *_rest in rows:
        conn.execute('INSERT INTO claim_persons VALUES (?, ?)', (cid, 'p-1'))
        conn.execute('INSERT INTO claim_persons VALUES (?, ?)', (cid, 'p-2'))
    return conn


# Same date: by source id (s-a before s-b), then each source's own claim order.
EXPECTED = ['c-early', 'c-m', 'c-z', 'c-a', 'c-undated']


class HelperTests(unittest.TestCase):
    def test_same_date_claims_sort_by_source_then_file_order(self):
        got = [r[0] for r in _memory_db().execute(
            f'SELECT id FROM claims c ORDER BY {claim_date_order_sql("c")}')]
        self.assertEqual(got, EXPECTED)

    def test_same_order_through_a_distinct_join(self):
        # The shape the site's timeline query uses: DISTINCT over a join that
        # lists each claim twice, which sorts through a temporary table.
        got = [r[0] for r in _memory_db().execute(
            'SELECT DISTINCT c.id FROM claim_persons cp JOIN claims c ON cp.claim_id = c.id '
            f'ORDER BY {claim_date_order_sql("c")}')]
        self.assertEqual(got, EXPECTED)


class RebuildStableTests(unittest.TestCase):
    """The order comes from the records, not from how the index was built."""

    PID = 'p-aaaaaaaaaa'

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / 'people' / 'stubs').mkdir(parents=True)
        (self.root / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
        meta = {'id': self.PID, 'name': 'Kid', 'living': 'false', 'tier': 'stub'}
        (self.root / 'people' / 'stubs' / f'p__kid_{self.PID}.md').write_text(
            '---\n' + yaml.safe_dump(meta) + '---\n\n# Kid\n', encoding='utf-8')
        # Two sources, each with two claims of the same date; the source filed
        # first on disk has the LATER id, so walk order and id order disagree.
        for sid, cids, folder in (('s-bbbbbbbbbb', ['c-bbbbbbbbb1', 'c-bbbbbbbbb2'], 'a-first'),
                                  ('s-aaaaaaaaaa', ['c-aaaaaaaaa1', 'c-aaaaaaaaa2'], 'z-last')):
            claims = [{'value': f'residence {c}', 'id': c, 'type': 'residence',
                       'persons': [self.PID], 'date': '1840~', 'status': 'accepted',
                       'reviewed': '2026-01-01', 'confidence': 'medium'} for c in cids]
            d = self.root / 'sources' / folder
            d.mkdir(parents=True)
            (d / f'src_{sid}.md').write_text(
                '---\n' + yaml.safe_dump({'id': sid, 'title': sid, 'source_type': 'other'})
                + '---\n\n## Claims\n```yaml\n' + yaml.safe_dump(claims, sort_keys=False)
                + '```\n', encoding='utf-8')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _order(self) -> list[str]:
        conn = sqlite3.connect(self.root / '.cache' / 'index.sqlite')
        try:
            return [r[0] for r in conn.execute(
                'SELECT DISTINCT c.id FROM claim_persons cp JOIN claims c ON cp.claim_id = c.id '
                f'WHERE cp.person_id = ? ORDER BY {claim_date_order_sql("c")}', (self.PID,))]
        finally:
            conn.close()

    def test_full_rebuild_and_one_source_reindex_agree(self):
        quiet = (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()))
        cfg = load_fha_yaml(self.root)
        with quiet[0], quiet[1]:
            index_mod.build_index(self.root, cfg)
        full = self._order()
        self.assertEqual(full, ['c-aaaaaaaaa1', 'c-aaaaaaaaa2', 'c-bbbbbbbbb1', 'c-bbbbbbbbb2'])
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            index_mod.upsert_source(self.root, cfg, 's-aaaaaaaaaa')
        self.assertEqual(self._order(), full)


if __name__ == '__main__':
    unittest.main()
