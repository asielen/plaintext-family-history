"""
test_places_city_countries.py - a country that is its own capital city gets
that city's coordinates (TOOLING §10).

"Born in Luxembourg" names a country and a city at once. `fha places
geocode` gives a lone country name the coordinates of its capital only when
the capital (GeoNames `PPLC` in that country) has the same name after
folding: Luxembourg, Monaco and the like. Every other lone country name
stays a general place with no coordinates: "Wales" never becomes a village
called Wales, and "Georgia" stays the country. Population never decides, and
the match still waits in the one review table for the owner's yes.

The gazetteer here is synthetic and shaped like the GeoNames export.
"""

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))

import places
from test_places import _add_place, _dump_lines, _make_index
from _lib import index_manifest_path, record_path_manifest, write_path_manifest

_COUNTRY_INFO = (
    '#ISO\tISO3\tISO-Numeric\tfips\tCountry\tCapital\n'
    'LU\tLUX\t442\tLU\tLuxembourg\tLuxembourg\n'
    'MC\tMCO\t492\tMN\tMonaco\tMonaco\n'
    'SM\tSMR\t674\tSM\tSan Marino\tSan Marino\n'
    'VA\tVAT\t336\tVT\tVatican\tVatican City\n'
    'KW\tKWT\t414\tKU\tKuwait\tKuwait City\n'
    'GB\tGBR\t826\tUK\tUnited Kingdom\tLondon\n'
    'GE\tGEO\t268\tGG\tGeorgia\tTbilisi\n'
    'PL\tPOL\t616\tPL\tPoland\tWarsaw\n'
)

# (gid, name, ascii, alts, lat, lon, fclass, fcode, admin1, admin2, admin3, pop)
_FEATURES = {
    'LU': [
        (400, 'Grand Duchy of Luxembourg', 'Grand Duchy of Luxembourg', 'Luxembourg',
         49.75, 6.16667, 'A', 'PCLI', '00', '', '', 600000),
        (401, 'Luxembourg', 'Luxembourg', '', 49.7, 6.1, 'A', 'ADM1', 'LU', '', '', 180000),
        (402, 'Luxembourg', 'Luxembourg', '', 49.61167, 6.13, 'P', 'PPLC', 'LU', '', '', 115000),
        # A larger namesake is never chosen over the capital: population
        # does not decide.
        (403, 'Luxembourg', 'Luxembourg', '', 49.9, 6.0, 'P', 'PPL', 'LU', '', '', 900000),
    ],
    'MC': [
        (500, 'Principality of Monaco', 'Principality of Monaco', 'Monaco',
         43.73, 7.42, 'A', 'PCLI', '00', '', '', 38000),
        (501, 'Monaco', 'Monaco', '', 43.73333, 7.41667, 'P', 'PPLC', '00', '', '', 32000),
    ],
    # The capital countryInfo names is not in the dump under that name.
    'SM': [
        (600, 'Republic of San Marino', 'Republic of San Marino', 'San Marino',
         43.94, 12.46, 'A', 'PCLI', '00', '', '', 33000),
        (601, 'Citta di San Marino', 'Citta di San Marino', 'San Marino',
         43.93667, 12.44639, 'P', 'PPLC', '07', '', '', 4000),
    ],
    'VA': [
        (800, 'Vatican City', 'Vatican City', 'Holy See', 41.90268, 12.45414,
         'P', 'PPLC', '00', '', '', 830),
    ],
    'KW': [
        (700, 'State of Kuwait', 'State of Kuwait', 'Kuwait', 29.5, 47.75,
         'A', 'PCLI', '00', '', '', 4000000),
        (701, 'Kuwait City', 'Kuwait City', 'Kuwait', 29.36972, 47.97833,
         'P', 'PPLC', '02', '', '', 60000),
    ],
}


def _write_gazetteer(gdir: Path, countries=('LU', 'MC', 'SM', 'VA', 'KW')) -> None:
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / 'countryInfo.txt').write_text(_COUNTRY_INFO, encoding='utf-8')
    (gdir / 'admin1CodesASCII.txt').write_text('LU.LU\tLuxembourg\tLuxembourg\t1\n',
                                               encoding='utf-8')
    (gdir / 'admin2Codes.txt').write_text('', encoding='utf-8')
    for cc in countries:
        with zipfile.ZipFile(gdir / f'{cc}.zip', 'w') as zf:
            zf.writestr(f'{cc}.txt', _dump_lines(_FEATURES[cc], cc))


def _row(name, hierarchy=None):
    return {'name': name, 'hierarchy': hierarchy}


class MatchTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write_gazetteer(self.root / '.cache' / 'geonames')
        rows = [_row(n) for n in ('Luxembourg', 'Monaco', 'San Marino', 'Vatican City',
                                  'Kuwait')]
        self.gaz, notes = places._prepare_gazetteer(
            self.root, rows, offline=True, progress=lambda _l: None)
        self.assertIsNotNone(self.gaz, notes)

    def tearDown(self):
        self.gaz.conn.close()
        self._tmp.cleanup()

    def test_a_country_that_is_its_own_capital_gets_the_capital(self):
        for name, coords in (('Luxembourg', (49.61167, 6.13)),
                             ('Monaco', (43.73333, 7.41667)),
                             ('Vatican City', (41.90268, 12.45414)),
                             ('luxembourg', (49.61167, 6.13))):
            with self.subTest(name=name):
                m = places._match_place(name, None, self.gaz)
                self.assertEqual(m.status, 'hit')
                self.assertEqual((m.hit.lat, m.hit.lon), coords)
                self.assertEqual(m.hit.fcode, 'PPLC')

    def test_other_lone_country_names_stay_general(self):
        # Wales is a country name GeoNames files under the United Kingdom,
        # whose capital is London; Georgia's is Tbilisi; Kuwait's capital is
        # Kuwait City, which is not the same name.
        for name in ('Wales', 'Georgia', 'Poland', 'Kuwait', 'United Kingdom'):
            with self.subTest(name=name):
                self.assertEqual(places._match_place(name, None, self.gaz).status, 'general')

    def test_a_capital_by_another_name_in_the_data_stays_general(self):
        # countryInfo says San Marino; the data's capital is spelled
        # otherwise and lists San Marino only among its other names.
        self.assertEqual(places._match_place('San Marino', None, self.gaz).status, 'general')

    def test_a_city_country_with_a_hierarchy_is_matched_as_before(self):
        m = places._match_place('Luxembourg', 'Luxembourg, Georgia', self.gaz)
        self.assertNotEqual(m.status, 'hit')


class DownloadTests(unittest.TestCase):

    def test_a_city_country_needs_its_data_and_a_general_name_needs_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_gazetteer(root / '.cache' / 'geonames', countries=())
            gaz, notes = places._prepare_gazetteer(
                root, [_row('Luxembourg'), _row('Wales'), _row('Georgia')],
                offline=True, progress=lambda _l: None)
            try:
                text = ' '.join(notes)
                self.assertIn('Luxembourg', text)
                self.assertNotIn('United Kingdom', text)
                self.assertNotIn('Georgia', text)
            finally:
                if gaz is not None:
                    gaz.conn.close()


_REGISTRY = (
    '- id: L-7c1a9f4e22\n'
    '  name: Luxembourg\n'
    '- id: L-8d2b0a5f33\n'
    '  name: Wales\n'
)


class ReviewTableTests(unittest.TestCase):
    """The capital is proposed in the one table and written only on a yes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'places').mkdir()
        self.conn = _make_index(self.root)
        self.yaml_path = self.root / 'places' / 'places.yaml'
        self.yaml_path.write_text(_REGISTRY, encoding='utf-8')
        _add_place(self.conn, 'l-7c1a9f4e22', 'Luxembourg')
        _add_place(self.conn, 'l-8d2b0a5f33', 'Wales')
        self.conn.commit()
        import os
        os.utime(self.root / '.cache' / 'index.sqlite')
        write_path_manifest(index_manifest_path(self.root), record_path_manifest(self.root))
        _write_gazetteer(self.root / '.cache' / 'geonames')

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _run(self, answer):
        prompts = []

        def confirm(prompt):
            prompts.append(prompt)
            return answer
        result = places.run_geocode(self.root, {}, offline=True, confirm=confirm)
        return result, prompts

    def test_no_writes_nothing_and_yes_writes_the_capital(self):
        import yaml
        result, prompts = self._run(False)
        self.assertEqual(len(prompts), 1)
        self.assertIn('l-7c1a9f4e22', prompts[0])
        self.assertNotIn('l-8d2b0a5f33', prompts[0])
        self.assertEqual(self.yaml_path.read_text(encoding='utf-8'), _REGISTRY)
        result, _ = self._run(True)
        self.assertEqual(result['written'], 1)
        data = {e['id']: e for e in yaml.safe_load(self.yaml_path.read_text(encoding='utf-8'))}
        self.assertEqual(data['L-7c1a9f4e22']['coords'], [49.61167, 6.13])
        self.assertNotIn('coords', data['L-8d2b0a5f33'])


if __name__ == '__main__':
    unittest.main()
