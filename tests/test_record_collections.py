"""One record set on several sites: the collection map and `fha lint` W162.

The map (tools/record_collections.json) is WikiTree Sourcer's, rebuilt by
build_record_collections.py, which reads Sourcer's file as text. W162 reports
two filed sources that are one record saved from Ancestry and FamilySearch.
Fictional Hartley family data only.

    py -3.14 -m unittest tests.test_record_collections
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT))

import build_record_collections as brc  # noqa: E402
import lint  # noqa: E402
from _lib import (  # noqa: E402
    record_collection_group, record_collection_index, source_site_collection)

SAMPLE_MJS = '''/* MIT License ... */
import { CD } from "./country_data.mjs";
const cols = ["name", "age"];
const RecordCollectionData = [
  // the whole index, on FamilySearch
  {
    wtsId: "HartleyBirthReg",
    title: "Kansas, Birth Index",
    country: "United States",
    sites: { fs: { id: "1001", dates: { from: 1880, to: 1920 } }, mh: { id: "20" } },
  },
  {
    wtsId: "HartleyBirthRegEarly",
    partOf: ["HartleyBirthReg"],
    title: 'Kansas, Birth Index 1880-1899',
    country: "United States",
    householdTableColumns: cols,
    sites: { ancestry: { id: "2002", altId: "ksbirth" } },
  },
  {
    wtsId: "UsCensus1880",
    title: "1880 United States Federal Census",
    country: "United States",
    dates: { year: 1880, exactDate: "1 Jun 1880" },
    sites: { ancestry: { id: "6742" }, fs: { id: "1417683" } },
  },
];
const RC = { find(id) { return RecordCollectionData.find((c) => c.wtsId === id); } };
export { RC };
'''


class BuilderTests(unittest.TestCase):
    """build_record_collections.py reads Sourcer's array as text, never runs it."""

    def test_the_array_is_read_and_slimmed_and_the_code_after_it_is_never_read(self):
        data = brc.build(SAMPLE_MJS, 'abc1234')
        self.assertEqual(data['source']['commit'], 'abc1234')
        self.assertIn('Copyright (c) 2020 Robert M Pavey', data['licence'])
        self.assertEqual(data['collections'], [
            {'id': 'HartleyBirthReg', 'title': 'Kansas, Birth Index', 'country': 'United States',
             'sites': {'fs': ['1001'], 'mh': ['20']}},
            {'id': 'HartleyBirthRegEarly', 'title': 'Kansas, Birth Index 1880-1899', 'country': 'United States',
             'partOf': ['HartleyBirthReg'], 'sites': {'ancestry': ['2002', 'ksbirth']}},
            {'id': 'UsCensus1880', 'title': '1880 United States Federal Census', 'country': 'United States',
             'year': 1880, 'sites': {'ancestry': ['6742'], 'fs': ['1417683']}},
        ])

    def test_a_form_it_does_not_know_stops_the_build_with_its_line(self):
        bad = SAMPLE_MJS.replace('title: "Kansas, Birth Index",', 'title: makeTitle("Kansas"),')
        with self.assertRaises(brc.ReadError) as cm:
            brc.build(bad, 'abc1234')
        self.assertIn('line', str(cm.exception))
        with self.assertRaises(brc.ReadError):
            brc.build('const Other = [];', 'abc1234')

    def test_the_shipped_map_carries_the_notice_and_the_censuses(self):
        data = json.loads((ROOT / 'tools' / 'record_collections.json').read_text(encoding='utf-8'))
        self.assertIn('Robert M Pavey', data['licence'])
        self.assertGreater(len(data['collections']), 100)
        anc = record_collection_group('ancestry', '8054')
        self.assertEqual(anc, record_collection_group('fs', '1401638'))
        self.assertEqual(anc[2], 1850)


def _index(tmp: Path) -> dict:
    path = tmp / 'map.json'
    path.write_text(json.dumps(brc.build(SAMPLE_MJS, 'abc1234')), encoding='utf-8')
    return record_collection_index(path)


class LookupTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_sites_part_of_a_set_belongs_to_the_sets_group(self):
        idx = _index(self.tmp)
        self.assertEqual(record_collection_group('ancestry', '2002', idx)[0], 'HartleyBirthReg')
        self.assertEqual(record_collection_group('ancestry', 'ksbirth', idx)[0], 'HartleyBirthReg')
        self.assertEqual(record_collection_group('fs', '1001', idx)[0], 'HartleyBirthReg')
        self.assertIsNone(record_collection_group('fs', '9999', idx))

    def test_a_sources_collection_comes_from_its_record_address_or_its_site_data(self):
        none = lambda p: None  # noqa: E731
        for url in ('https://www.ancestry.com/search/collections/6742/records/4321?tid=1',
                    'https://www.ancestry.com/discoveryui-content/view/4321:6742',
                    'https://www.ancestry.com/interactive/6742/img_0042/4321'):
            self.assertEqual(source_site_collection({'citation': 'Ancestry (' + url + ')'}, none), ('ancestry', '6742'))
        fs = {'files': [{'file': 'documents/census/record-data.json', 'role': 'site-data'}]}
        data = {'record': {'url': 'https://www.familysearch.org/ark:/61903/1:1:ABCD-123', 'collectionId': '1417683'}}
        self.assertEqual(source_site_collection(fs, lambda p: data), ('fs', '1417683'))
        # A record merged from both sites is no longer one site's copy.
        both = dict(fs, citation='https://www.ancestry.com/search/collections/6742/records/4321')
        self.assertIsNone(source_site_collection(both, lambda p: data))
        self.assertIsNone(source_site_collection({'citation': 'A family Bible'}, none))


SOURCE = '''---
id: {sid}
title: {title}
source_type: census
{date}repository: {repo}
citation: >
  {citation}
people:
{people}
files:{files}
---

## Notes
A record.
'''

P1 = 'p-0a1b2c3d4e'
P2 = 'p-1b2c3d4e5f'


class SameRecordTwoSitesTests(unittest.TestCase):
    """W162: one record saved from two sites, reported, never merged."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
        (self.root / 'people').mkdir()
        (self.root / 'sources' / 'census').mkdir(parents=True)

    def _source(self, sid, *, site, coll, people=(P1,), date="'1880-06-04'"):
        files = ''
        if site == 'fs':
            rel = f'documents/census/record-data_{sid.upper()}.json'
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text(json.dumps({'record': {
                'url': 'https://www.familysearch.org/ark:/61903/1:1:ABCD-123', 'collectionId': coll}}), encoding='utf-8')
            files = f'\n  - file: {rel}\n    role: site-data'
            citation = '"United States Census, 1880," database, FamilySearch (https://www.familysearch.org/ark:/61903/1:1:ABCD-123)'
            repo = 'FamilySearch'
        else:
            citation = f'Ancestry.com (https://www.ancestry.com/search/collections/{coll}/records/4321)'
            repo = 'Ancestry.com'
        (self.root / 'sources' / 'census' / f'calvin-hartley_{sid}.md').write_text(SOURCE.format(
            sid=sid, title='Calvin Hartley 1880', date=f'source_date: {date}\n' if date else '', repo=repo,
            citation=citation, people=''.join(f'  - "[[{p}|Someone]]"\n' for p in people), files=files),
            encoding='utf-8')

    def _w162(self):
        findings, _ = lint._run_lint_core(self.root, {})
        return [f for f in findings if f.code == 'W162']

    def test_one_census_entry_saved_from_ancestry_and_familysearch_is_reported(self):
        self._source('s-0a1b2c3d4e', site='ancestry', coll='6742')
        self._source('s-1b2c3d4e5f', site='fs', coll='1417683')
        found = self._w162()
        self.assertEqual(len(found), 1, [f.message for f in found])
        self.assertEqual(found[0].severity, 'W')
        msg = found[0].message
        self.assertIn('s-0a1b2c3d4e (Ancestry collection 6742)', msg)
        self.assertIn('s-1b2c3d4e5f (FamilySearch collection 1417683)', msg)
        self.assertIn('1880 United States Federal Census', msg)
        self.assertIn('fha source merge', msg)

    def test_a_census_source_with_no_date_takes_the_census_year(self):
        self._source('s-0a1b2c3d4e', site='ancestry', coll='6742', date=None)
        self._source('s-1b2c3d4e5f', site='fs', coll='1417683')
        self.assertEqual(len(self._w162()), 1)

    def test_two_records_from_one_site_are_never_paired(self):
        # Household members' records of one census page are kept apart on purpose.
        self._source('s-0a1b2c3d4e', site='fs', coll='1417683')
        self._source('s-1b2c3d4e5f', site='fs', coll='1417683')
        self.assertEqual(self._w162(), [])

    def test_no_person_in_common_different_dates_or_an_unmapped_collection_say_nothing(self):
        self._source('s-0a1b2c3d4e', site='ancestry', coll='6742')
        self._source('s-1b2c3d4e5f', site='fs', coll='1417683', people=(P2,))
        self.assertEqual(self._w162(), [])
        self._source('s-1b2c3d4e5f', site='fs', coll='1417683', date="'1880-06-05'")
        self.assertEqual(self._w162(), [])
        self._source('s-1b2c3d4e5f', site='fs', coll='9999999')
        self.assertEqual(self._w162(), [])


if __name__ == '__main__':
    unittest.main()
