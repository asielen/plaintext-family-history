"""
test_repositories.py - fha repositories: where the records are (TOOLING §7c).

A Kansas Territory birth and a State of Kansas death were made by different
governments, so the verb looks each up under the right one. Each test builds a
tiny temp archive with a place registry, indexes it, and asks the helper
(`_lib.place_hierarchy_at`) or the command (`repositories.run_repositories`)
one question; the answers come from SPEC A.9 (the claim's date picks the
period, and a period counts only when it wholly covers the date) and the
ranking the doc states, never from the code's current output. Synthetic
places only; the list is a small fixture file, not the shipped catalog.

    py -3.14 -m unittest tests.test_repositories
"""

import io
import json
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
import repositories
from _lib import (
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    load_fha_yaml,
    place_hierarchy_at,
    place_hierarchy_in_year,
)

FAIRVIEW = 'l-7c1a9f4e22'
ELM = 'l-8d2b0a5f33'          # no history of its own, within Fairview
SUWALKI = 'l-9e3c1b6a44'       # Russian Empire then, Poland now
TWOROW = 'l-2a4d6f8b55'        # a wide period and a narrow one
GEORGIA = 'l-3b5e7a9c66'       # last part "Georgia": ambiguous
LOST = 'l-4c6f8b0d77'          # last part is a name nobody knows
NOHIER = 'l-5d7a9c1e88'        # no hierarchy anywhere

PLACES = [
    {'id': FAIRVIEW, 'name': 'Fairview', 'alt_names': ['Fairview City'],
     'hierarchy': 'Fairview, Breton County, Kansas, USA',
     'history': [{'period': '1858/1861',
                  'hierarchy': 'Fairview, Breton Co., Kansas Territory, USA'}]},
    {'id': ELM, 'name': 'Elm Creek', 'within': FAIRVIEW},
    {'id': SUWALKI, 'name': 'Suwalki', 'hierarchy': 'Suwalki, Poland',
     'history': [{'period': '1815/1915', 'hierarchy': 'Suwalki, Russian Empire'}]},
    {'id': TWOROW, 'name': 'Twin Fork', 'hierarchy': 'Twin Fork, Easton County, Kansas, USA',
     'history': [{'period': '1850/1870', 'hierarchy': 'Twin Fork, Kansas Territory, USA'},
                 {'period': '1858/1861', 'hierarchy': 'Twin Fork, Easton Co., Kansas Territory, USA'}]},
    {'id': GEORGIA, 'name': 'Reed Mill', 'hierarchy': 'Reed Mill, Georgia'},
    {'id': LOST, 'name': 'Cole Ridge', 'hierarchy': 'Cole Ridge, Breton Territory'},
    {'id': NOHIER, 'name': 'Hartley Bend'},
]

CATALOG = {'about': 'test', 'sources': [], 'repositories': [
    {'id': 'fs', 'name': 'Fairview Family Search', 'url': 'https://fs.example/', 'countries': ['*'],
     'years': [1500, 2000], 'records': ['vital', 'church', 'census'], 'access': 'free_account',
     'channel': ['online'], 'how': 'Open the image, not just the index.',
     'from': ['genealogy-mcp'], 'checked': '2026-11-15', 'guide': None},
    {'id': 'ks', 'name': 'Kansas State Archives', 'url': 'https://ks.example/', 'countries': ['US'],
     'years': [1850, 1990], 'records': ['vital'], 'access': 'paid', 'channel': ['by letter', 'online'],
     'how': 'Order a certified copy.', 'from': ['autoresearch-genealogy'], 'checked': '2026-05-19',
     'guide': 'usa-vital-records'},
    {'id': 'old', 'name': 'Civil War Muster Rolls', 'url': 'https://war.example/', 'countries': ['US'],
     'years': [1861, 1865], 'records': ['military'], 'access': 'free', 'channel': ['online'],
     'how': 'Rolls by regiment.', 'from': ['genealogy-mcp'], 'checked': '2026-11-15', 'guide': None},
    {'id': 'free-us', 'name': 'County Graves Project', 'url': 'https://graves.example/',
     'countries': ['US'], 'records': ['graves', 'vital'], 'access': 'free', 'channel': ['online'],
     'how': 'Search by cemetery.', 'from': ['genealogy-mcp'], 'checked': '2026-11-15', 'guide': None},
    {'id': 'ru', 'name': 'Russian State Archive', 'url': 'https://ru.example/', 'countries': ['RU'],
     'years': [1700, 1917], 'records': ['church', 'vital'], 'access': 'request',
     'channel': ['by letter'], 'how': 'Write in Russian.', 'from': ['genealogy-mcp'],
     'checked': '2026-11-15', 'guide': 'russia-ukraine'},
    {'id': 'pl', 'name': 'Polish State Archives', 'url': 'https://pl.example/', 'countries': ['PL'],
     'years': [1500, 2000], 'records': ['church', 'vital'], 'access': 'free', 'channel': ['online', 'in person'],
     'how': 'Szukaj w Archiwach.', 'from': ['genealogy-mcp'], 'checked': '2026-11-15', 'guide': 'poland'},
    {'id': 'jw', 'name': 'Jewish Records Index', 'url': 'https://jw.example/', 'countries': ['PL'],
     'records': ['vital'], 'access': 'free', 'channel': ['online'], 'how': 'Index only.',
     'from': ['genealogy-mcp'], 'checked': '2026-11-15', 'guide': 'jewish-genealogy'},
]}


class _Archive:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / 'people' / 'stubs').mkdir(parents=True)
        (self.root / 'places').mkdir()
        (self.root / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
        (self.root / 'places' / 'places.yaml').write_text(
            yaml.safe_dump(PLACES, sort_keys=False, allow_unicode=True), encoding='utf-8')
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            index_mod.build_index(self.root, load_fha_yaml(self.root))
        self.catalog = self.root / 'catalog.json'
        self.catalog.write_text(json.dumps(CATALOG), encoding='utf-8')

    def conn(self):
        return sqlite3.connect(self.root / '.cache' / 'index.sqlite')

    def run(self, **kw):
        with redirect_stderr(io.StringIO()):
            return repositories.run_repositories(self.root, catalog_path=self.catalog, **kw)

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class PlaceHierarchyAt(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a = _Archive()
        cls.conn = cls.a.conn()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.a.cleanup()

    def test_period_covering_the_year_names_the_territory(self):
        self.assertEqual(place_hierarchy_in_year(self.conn, FAIRVIEW, 1860),
                         ('Fairview, Breton Co., Kansas Territory, USA', '1858/1861'))

    def test_year_outside_every_period_gives_nothing(self):
        self.assertEqual(place_hierarchy_in_year(self.conn, FAIRVIEW, 1911), (None, None))

    def test_no_year_gives_nothing(self):
        self.assertEqual(place_hierarchy_in_year(self.conn, FAIRVIEW, None), (None, None))

    def test_a_range_must_sit_wholly_inside_the_period(self):
        # A 1855/1865 claim is not inside 1858/1861 (SPEC A.9).
        self.assertEqual(place_hierarchy_at(self.conn, FAIRVIEW, '1855', '1865'), (None, None))
        self.assertEqual(place_hierarchy_at(self.conn, FAIRVIEW, '1859-03-01', '1860-12-31'),
                         ('Fairview, Breton Co., Kansas Territory, USA', '1858/1861'))

    def test_undated_range_gives_nothing(self):
        self.assertEqual(place_hierarchy_at(self.conn, FAIRVIEW, '0001-01-01', '9999-12-31'),
                         (None, None))

    def test_micro_place_inherits_the_containing_places_history(self):
        self.assertEqual(place_hierarchy_in_year(self.conn, ELM, 1860)[1], '1858/1861')

    def test_narrowest_covering_period_wins(self):
        self.assertEqual(place_hierarchy_in_year(self.conn, TWOROW, 1860),
                         ('Twin Fork, Easton Co., Kansas Territory, USA', '1858/1861'))
        self.assertEqual(place_hierarchy_in_year(self.conn, TWOROW, 1865),
                         ('Twin Fork, Kansas Territory, USA', '1850/1870'))


class CountryNames(unittest.TestCase):
    def codes(self, h):
        return repositories.hierarchy_countries(h)[0]

    def test_territory_and_usa(self):
        self.assertEqual(self.codes('Fairview, Breton Co., Kansas Territory, USA'), ('US',))

    def test_territory_alone_is_found_through_its_state(self):
        self.assertEqual(self.codes('Fairview, Kansas Territory'), ('US',))

    def test_a_state_alone_stands_for_its_country(self):
        self.assertEqual(self.codes('Fairview, Kansas'), ('US',))

    def test_georgia_alone_is_not_guessed(self):
        self.assertIsNone(self.codes('Reed Mill, Georgia'))

    def test_empires_and_kingdoms(self):
        self.assertEqual(self.codes('Suwalki, Russian Empire'), ('RU',))
        self.assertEqual(self.codes('Berlin, Kingdom of Prussia'), ('DE',))
        self.assertEqual(self.codes('Brno, Austria-Hungary'), ('AT', 'HU'))
        self.assertEqual(self.codes('Smyrna, Ottoman Empire'), ('TR',))

    def test_a_name_the_tool_does_not_know_is_reported(self):
        codes, bad = repositories.hierarchy_countries('Cole Ridge, Breton Territory')
        self.assertIsNone(codes)
        self.assertEqual(bad, 'Breton Territory')


class Engine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a = _Archive()

    @classmethod
    def tearDownClass(cls):
        cls.a.cleanup()

    def names(self, r):
        return [x['name'] for x in r.data['repositories']]

    def test_birth_in_the_territory_year_reads_the_government_of_the_day(self):
        r = self.a.run(place=FAIRVIEW, year=1860, record='birth')
        self.assertEqual(r.exit_code, EXIT_CLEAN)
        self.assertIn('Kansas Territory', r.data['then']['hierarchy'])
        self.assertEqual(r.data['then']['period'], '1858/1861')
        self.assertEqual(r.data['then']['countries'], ['US'])

    def test_text_place_name_and_alt_name_resolve(self):
        for text in ('Fairview', 'fairview city'):
            r = self.a.run(place=text, year=1911, record='death')
            self.assertEqual(r.exit_code, EXIT_CLEAN, text)
            self.assertEqual(r.data['place']['id'], FAIRVIEW)

    def test_year_filters_out_rows_that_do_not_cover_it(self):
        r = self.a.run(place=FAIRVIEW, year=1911, record='military')
        self.assertEqual(r.exit_code, EXIT_WARNINGS)  # muster rolls end in 1865
        r = self.a.run(country='US', year=1863, record='military')
        self.assertEqual(self.names(r), ['Civil War Muster Rolls'])

    def test_record_kind_filters(self):
        r = self.a.run(country='US', record='death')
        self.assertNotIn('Civil War Muster Rolls', self.names(r))
        self.assertIn('County Graves Project', self.names(r))

    def test_country_rows_before_worldwide_and_cheaper_first(self):
        r = self.a.run(country='US', year=1911, record='death')
        # Year-known rows first, then unknown; country rows before the
        # worldwide row; free before paid.
        self.assertEqual(self.names(r), ['Kansas State Archives', 'County Graves Project',
                                         'Fairview Family Search'])

    def test_access_free_keeps_free_and_free_with_account(self):
        r = self.a.run(country='US', year=1911, record='death', access='free')
        self.assertEqual(self.names(r), ['County Graves Project', 'Fairview Family Search'])

    def test_then_and_now_label_who_each_row_serves(self):
        r = self.a.run(place=SUWALKI, year=1880, record='birth')
        serves = {x['id']: x['serves'] for x in r.data['repositories']}
        self.assertEqual(serves['ru'], ['then'])
        self.assertEqual(serves['pl'], ['now'])
        self.assertEqual(serves['fs'], ['then', 'now'])
        self.assertEqual(r.data['then']['countries'], ['RU'])
        self.assertEqual(r.data['now']['countries'], ['PL'])

    def test_topic_rows_only_with_the_topic(self):
        self.assertNotIn('Jewish Records Index', self.names(self.a.run(country='PL', record='birth')))
        r = self.a.run(country='PL', record='birth', topic='jewish')
        self.assertEqual(self.names(r), ['Jewish Records Index'])

    def test_limit(self):
        r = self.a.run(country='US', limit=2)
        self.assertEqual(len(r.data['repositories']), 2)
        self.assertGreater(r.data['total'], 2)

    def test_country_by_two_letter_code(self):
        self.assertEqual(self.a.run(country='pl').exit_code, EXIT_CLEAN)

    def test_guide_paths_are_never_absolute(self):
        r = self.a.run(country='US', year=1911, record='death')
        for p in r.data['guide_paths']:
            self.assertFalse(Path(p).is_absolute(), p)
            self.assertTrue(p.startswith(('tools/', '.fha/tools/')), p)

    def test_exit_codes(self):
        self.assertEqual(self.a.run(country='US', record='military', year=1990).exit_code, EXIT_WARNINGS)
        self.assertEqual(self.a.run(country='Atlantis').exit_code, EXIT_ERRORS)
        self.assertEqual(self.a.run().exit_code, EXIT_ERRORS)
        self.assertEqual(self.a.run(place='Nowhere Town').exit_code, EXIT_ERRORS)
        self.assertEqual(self.a.run(place=GEORGIA).exit_code, EXIT_ERRORS)
        self.assertEqual(self.a.run(place=NOHIER).exit_code, EXIT_ERRORS)
        missing = repositories.run_repositories(
            self.a.root, country='US', catalog_path=self.a.root / 'nope.json')
        self.assertEqual(missing.exit_code, EXIT_FAILURE)

    def test_unknown_country_says_the_fix(self):
        r = self.a.run(place=LOST)
        self.assertEqual(r.exit_code, EXIT_ERRORS)
        self.assertIn("I don't know which country 'Breton Territory' is", r.messages[0].text)
        self.assertIn('--country', r.messages[0].text)

    def test_two_places_with_one_name_are_refused_with_each_command(self):
        a = _Archive()
        try:
            places = PLACES + [{'id': 'l-6e8b0d2f99', 'name': 'Fairview',
                                'hierarchy': 'Fairview, Easton County, Ohio, USA'}]
            (a.root / 'places' / 'places.yaml').write_text(yaml.safe_dump(places), encoding='utf-8')
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                index_mod.build_index(a.root, load_fha_yaml(a.root))
            r = a.run(place='Fairview')
            self.assertEqual(r.exit_code, EXIT_ERRORS)
            self.assertIn(f'fha repositories --place {FAIRVIEW}', r.messages[0].text)
            self.assertIn('fha repositories --place l-6e8b0d2f99', r.messages[0].text)
        finally:
            a.cleanup()

    def test_no_index_is_a_tool_failure(self):
        empty = Path(tempfile.mkdtemp())
        try:
            (empty / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
            with redirect_stderr(io.StringIO()):
                r = repositories.run_repositories(empty, place=FAIRVIEW, catalog_path=self.a.catalog)
            self.assertEqual(r.exit_code, EXIT_FAILURE)
        finally:
            shutil.rmtree(empty, ignore_errors=True)


class Output(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a = _Archive()

    @classmethod
    def tearDownClass(cls):
        cls.a.cleanup()

    def render(self, **kw):
        r = self.a.run(**kw)
        out = io.StringIO()
        with redirect_stdout(out):
            code = repositories._cmd_repositories(r)
        return code, out.getvalue()

    def test_every_row_carries_cost_reach_and_check_this(self):
        code, text = self.render(place=FAIRVIEW, year=1911, record='death')
        self.assertEqual(code, EXIT_CLEAN)
        self.assertIn('Death records for Fairview, Breton County, Kansas, USA in 1911', text)
        self.assertIn('no dated history covers 1911, so this is its present name', text)
        self.assertIn('Kansas State Archives: paid, by letter or online', text)
        self.assertIn('Fairview Family Search: free with an account, online', text)
        self.assertEqual(text.count('(check this)'), 3)
        self.assertIn('Listed by genealogy-mcp as of 2026-11-15 (check this)', text)
        self.assertIn('check before you go or send money.', text)
        self.assertIn('More on what records exist there: ', text)

    def test_territory_year_names_the_period(self):
        _, text = self.render(place=FAIRVIEW, year=1860, record='birth')
        self.assertIn('Kansas Territory', text)
        self.assertIn('1858/1861', text)

    def test_different_governments_label_each_row(self):
        _, text = self.render(place=SUWALKI, year=1880, record='birth')
        self.assertIn('For: records made by the government of the day', text)
        self.assertIn('For: where the records are likely kept today', text)

    def test_no_absolute_path_in_output(self):
        _, text = self.render(place=FAIRVIEW, year=1911, record='death')
        self.assertNotIn(str(ROOT), text)
        self.assertNotIn(str(self.a.root), text)
        self.assertNotRegex(text, r'(?<![A-Za-z])[A-Za-z]:[\\/]')

    def test_json_shape(self):
        r = self.a.run(place=FAIRVIEW, year=1860, record='birth')
        out = io.StringIO()
        with redirect_stdout(out):
            repositories._cmd_repositories(r, use_json=True)
        d = json.loads(out.getvalue())['data']
        self.assertEqual(set(['place', 'year', 'then', 'now', 'repositories', 'guides']) - set(d), set())
        self.assertEqual(d['then']['period'], '1858/1861')
        row = d['repositories'][0]
        for k in ('serves', 'access_words', 'channel_words'):
            self.assertIn(k, row)

    def test_nothing_matched_prints_the_broader_command_to_stderr(self):
        r = self.a.run(country='US', record='military', year=1990)
        err = io.StringIO()
        with redirect_stderr(err):
            code = repositories._cmd_repositories(r)
        self.assertEqual(code, EXIT_WARNINGS)
        self.assertIn('drop `--record`', err.getvalue())


class ShippedCatalog(unittest.TestCase):
    def test_fairview_1860_birth_against_the_real_list(self):
        a = _Archive()
        try:
            with redirect_stderr(io.StringIO()):
                r = repositories.run_repositories(a.root, place=FAIRVIEW, year=1860, record='birth')
            self.assertEqual(r.exit_code, EXIT_CLEAN)
            self.assertIn('Kansas Territory', r.data['then']['hierarchy'])
        finally:
            a.cleanup()


if __name__ == '__main__':
    unittest.main()
