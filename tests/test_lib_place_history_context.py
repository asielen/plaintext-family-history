"""`_lib.place_history_context`: the background lines a person's timeline gets
when a place they lived at changed government during their stay."""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))

from _lib import (  # noqa: E402
    insert_context_rows, place_history_context, place_history_phrase, place_visits_from_rows,
)


class _R:
    """A timeline row stand-in: own rows are sqlite rows, not dicts."""
    def __init__(self, d):
        self.d = d

    def __getitem__(self, k):
        return self.d[k]


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

    def test_change_year_written_in_both_periods(self):
        # SPEC A.9's own style: the Territory to 1861, the state from 1861.
        # The state line says 1861, and no second Territory line appears.
        self.conn.execute('DELETE FROM place_history')
        for p, a, b, h in (('1855/1861', '1855-01-01', '1861-12-31', 'Fairview, Kansas Territory'),
                           ('1861/1889', '1861-01-01', '1889-12-31', 'Fairview, Breton Co., Kansas, USA')):
            self.conn.execute('INSERT INTO place_history VALUES (?,?,?,?,?)', ('l-1', p, a, b, h))
        rows = place_history_context(self.conn, {'l-1': ('1850-01-01', '1900-12-31')})
        self.assertEqual([place_history_phrase(r) for r in rows],
                         ['From 1855, Fairview was in Kansas Territory.',
                          'From 1861, Fairview was in Breton Co., Kansas, USA.'])

    def test_open_ended_records_do_not_stretch_the_stay(self):
        def row(dmin, dmax, typ='residence'):
            return {'place_id': 'l-1', 'type': typ, 'date_min': dmin, 'date_max': dmax}
        # "from 1883 on" and "by 1895" each count as the one known year.
        self.assertEqual(place_visits_from_rows([row('1883-01-01', '9999-12-31')]),
                         {'l-1': ('1883-01-01', '1883-01-01')})
        self.assertEqual(place_visits_from_rows([row('0001-01-01', '1895-12-31')]),
                         {'l-1': ('1895-12-31', '1895-12-31')})

    def test_a_burial_back_home_is_not_a_stay(self):
        visits = place_visits_from_rows([
            {'place_id': 'l-1', 'type': 'birth', 'date_min': '1856-01-01', 'date_max': '1856-12-31'},
            {'place_id': 'l-2', 'type': 'burial', 'date_min': '1952-01-01', 'date_max': '1952-12-31'}])
        self.assertEqual(visits, {'l-1': ('1856-01-01', '1856-12-31')})
        self.assertEqual(place_history_context(self.conn, visits), [])

    def test_a_by_year_record_keeps_its_place_in_order(self):
        own = [{'date_min': '1858-01-01', 'date_max': '1858-12-31'},
               {'date_min': '0001-01-01', 'date_max': '1875-12-31'}]
        ctx = {'date_iso': '1862-01-01'}
        out = insert_context_rows([_R(r) for r in own], [ctx])
        self.assertIs(out[1], ctx)

    def test_phrase(self):
        r = {'year': 1861, 'name': 'Fairview', 'hierarchy': 'Fairview, Breton Co., Kansas, USA'}
        self.assertEqual(place_history_phrase(r), 'From 1861, Fairview was in Breton Co., Kansas, USA.')
        r['hierarchy'] = 'Kansas Territory'
        self.assertEqual(place_history_phrase(r), 'From 1861, this place was recorded as Kansas Territory.')


if __name__ == '__main__':
    unittest.main()
