"""`_lib.place_history_context`: the background lines a person's timeline gets
when a place they lived at changed government during their stay."""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))

from _lib import place_history_context, place_history_phrase  # noqa: E402


class PlaceHistoryContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(':memory:')
        self.conn.executescript(
            'CREATE TABLE places(id TEXT, name TEXT, hierarchy TEXT, within TEXT);'
            'CREATE TABLE place_history(place_id TEXT, period_edtf TEXT, date_min TEXT,'
            ' date_max TEXT, hierarchy TEXT);')
        self.conn.execute("INSERT INTO places VALUES ('l-1','Fairview','Fairview, Breton Co., Kansas, USA',NULL)")
        self.conn.execute("INSERT INTO places VALUES ('l-2','Fairview Cemetery','Fairview Cemetery, Fairview',"
                          "'l-1')")
        self.conn.execute("INSERT INTO place_history VALUES ('l-1','1858/1861','1858-01-01','1861-12-31',"
                           "'Fairview, Kansas Territory')")

    def test_start_and_present_hierarchy_rows(self):
        rows = place_history_context(self.conn, {'l-1': ('1856-01-01', '1875-12-31')})
        self.assertEqual([(r['date_iso'], r['hierarchy']) for r in rows],
                         [('1858-01-01', 'Fairview, Kansas Territory'),
                          ('1862-01-01', 'Fairview, Breton Co., Kansas, USA')])

    def test_only_inside_the_stay(self):
        rows = place_history_context(self.conn, {'l-1': ('1859-01-01', '1860-12-31')})
        self.assertEqual(rows, [])

    def test_a_house_inside_the_place_counts_as_a_visit_to_it(self):
        rows = place_history_context(self.conn, {'l-2': ('1856-01-01', '1875-12-31')})
        self.assertEqual({r['place_id'] for r in rows}, {'l-1'})

    def test_open_ended_entries_make_no_row(self):
        self.conn.execute("INSERT INTO place_history VALUES ('l-1','..','0001-01-01','9999-12-31','X')")
        rows = place_history_context(self.conn, {'l-1': ('0100-01-01', '1900-12-31')})
        self.assertFalse([r for r in rows if r['date_iso'] in ('0001-01-01',)])

    def test_phrase(self):
        r = {'year': 1861, 'name': 'Fairview', 'hierarchy': 'Fairview, Breton Co., Kansas, USA'}
        self.assertEqual(place_history_phrase(r), 'From 1861, Fairview was in Breton Co., Kansas, USA.')
        r['hierarchy'] = 'Kansas Territory'
        self.assertEqual(place_history_phrase(r), 'From 1861, this place was recorded as Kansas Territory.')


if __name__ == '__main__':
    unittest.main()
