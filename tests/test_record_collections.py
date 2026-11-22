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
        # A string escape JSON does not share, and a partOf that is no list.
        with self.assertRaises(brc.ReadError):
            brc.build(SAMPLE_MJS.replace('"Kansas, Birth Index",', '"Kansas\\x41",'), 'abc1234')
        with self.assertRaises(brc.ReadError):
            brc.build(SAMPLE_MJS.replace('partOf: ["HartleyBirthReg"]', 'partOf: "HartleyBirthReg"'), 'abc1234')

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

    def test_a_site_number_two_record_sets_share_is_left_unmapped(self):
        # FamilySearch keeps one province's births, marriages and deaths under
        # one number: it cannot say which set a record is from.
        text = SAMPLE_MJS.replace('sites: { ancestry: { id: "6742" }, fs: { id: "1417683" } },',
                                  'sites: { ancestry: { id: "6742" }, fs: { id: "1001" } },')
        path = self.tmp / 'map2.json'
        path.write_text(json.dumps(brc.build(text, 'abc1234')), encoding='utf-8')
        idx = record_collection_index(path)
        self.assertIsNone(record_collection_group('fs', '1001', idx))
        self.assertEqual(record_collection_group('ancestry', '6742', idx)[0], 'UsCensus1880')

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

    def _source(self, sid, *, site, coll, people=(P1,), date="'1880-06-04'", name='Calvin Hartley', age=None):
        # Both sites' captures carry the site's data, naming whose entry it is.
        rel = f'documents/census/record-data_{sid.upper()}.json'
        (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
        record = ({'url': 'https://www.familysearch.org/ark:/61903/1:1:ABCD-123', 'collectionId': coll}
                  if site == 'fs' else {'dbId': coll, 'recordId': '4321'})
        if name:
            record['name'] = name
        data = {'record': record}
        if age is not None and site == 'fs':
            record['fsId'] = 'ABCD-123'
            data['gedcomx'] = {'persons': [
                {'identifiers': {'http://gedcomx.org/Persistent': ['https://familysearch.org/ark:/61903/1:1:ABCD-999']},
                 'fields': [{'type': 'http://familysearch.org/types/fields/Age', 'values': [{'text': '70'}]}]},
                {'identifiers': {'http://gedcomx.org/Persistent': ['https://familysearch.org/ark:/61903/1:1:ABCD-123']},
                 'fields': [{'type': 'http://familysearch.org/types/fields/Age', 'values': [{'labelId': 'PR_AGE', 'text': str(age)}]}]},
            ]}
        elif age is not None:
            data['fields'] = [{'label': 'Name', 'value': name}, {'label': 'Age in 1880', 'value': str(age)}]
        (self.root / rel).write_text(json.dumps(data), encoding='utf-8')
        files = f'\n  - file: {rel}\n    role: site-data'
        if site == 'fs':
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
        # The exact command, both ids filled in.
        self.assertIn('`fha source merge s-0a1b2c3d4e s-1b2c3d4e5f --dry-run`', msg)

    def test_the_same_subject_matches_whatever_case_or_punctuation_the_sites_use(self):
        self._source('s-0a1b2c3d4e', site='ancestry', coll='6742', name='CALVIN HARTLEY')
        self._source('s-1b2c3d4e5f', site='fs', coll='1417683', name='Calvin  Hartley.')
        self.assertEqual(len(self._w162()), 1)

    def test_a_husbands_and_a_wifes_entries_on_one_page_are_two_records(self):
        # Both list the whole household, so they name the same people; whose
        # entry each is differs, and that is what makes them two records.
        self._source('s-0a1b2c3d4e', site='ancestry', coll='6742', people=(P1, P2), name='Calvin Hartley')
        self._source('s-1b2c3d4e5f', site='fs', coll='1417683', people=(P1, P2), name='Margaret Hartley')
        self.assertEqual(self._w162(), [])

    def test_a_source_holding_a_whole_pages_entries_pairs_with_a_copy_of_one(self):
        # One source per census page, its members' entries folded into it.
        self._source('s-0a1b2c3d4e', site='ancestry', coll='6742', name='Calvin Hartley')
        self._source('s-1b2c3d4e5f', site='fs', coll='1417683', name='Margaret Hartley')
        more = self.root / 'documents' / 'census' / 'record-data-2_S-1B2C3D4E5F.json'
        more.write_text(json.dumps({'record': {'url': 'https://www.familysearch.org/ark:/61903/1:1:ABCD-124',
                                               'collectionId': '1417683', 'name': 'Calvin Hartley'}}), encoding='utf-8')
        path = next((self.root / 'sources' / 'census').glob('*_s-1b2c3d4e5f.md'))
        path.write_text(path.read_text(encoding='utf-8').replace(
            '    role: site-data',
            '    role: site-data\n  - file: documents/census/record-data-2_S-1B2C3D4E5F.json\n    role: site-data', 1),
            encoding='utf-8')
        self.assertEqual(len(self._w162()), 1)

    def test_a_son_named_for_his_father_is_told_apart_by_age(self):
        # Same name, same household, same page: the age each record gives
        # its own subject tells the father's entry from the son's.
        self._source('s-0a1b2c3d4e', site='ancestry', coll='6742', people=(P1, P2), age=52)
        self._source('s-1b2c3d4e5f', site='fs', coll='1417683', people=(P1, P2), age=24)
        self.assertEqual(self._w162(), [])
        self._source('s-1b2c3d4e5f', site='fs', coll='1417683', people=(P1, P2), age=52)
        self.assertEqual(len(self._w162()), 1)

    def test_a_copy_whose_site_data_names_no_subject_is_never_paired(self):
        self._source('s-0a1b2c3d4e', site='ancestry', coll='6742', name=None)
        self._source('s-1b2c3d4e5f', site='fs', coll='1417683')
        self.assertEqual(self._w162(), [])

    def test_a_people_field_that_is_not_a_list_does_not_stop_lint(self):
        self._source('s-0a1b2c3d4e', site='ancestry', coll='6742')
        self._source('s-1b2c3d4e5f', site='fs', coll='1417683')
        path = next((self.root / 'sources' / 'census').glob('*_s-1b2c3d4e5f.md'))
        path.write_text(path.read_text(encoding='utf-8').replace(
            'people:\n  - "[[p-0a1b2c3d4e|Someone]]"\n', 'people: 5\n'), encoding='utf-8')
        self.assertEqual(self._w162(), [])

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
