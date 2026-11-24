"""
test_compare.py - fha compare: every record about one fact, side by side
(TOOLING §7d).

Each test builds a small temp archive of Hartley family records, indexes it,
and asks `compare.run_compare` (or the CLI renderer) one question. Expected
answers come from the rules TOOLING §7d states, never from the code's current
output. Temp fixtures only; fictional people.

    py -3.14 -m unittest tests.test_compare
"""

import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import compare
import index as index_mod
from _lib import EXIT_CLEAN, EXIT_ERRORS, EXIT_FAILURE, EXIT_WARNINGS, load_fha_yaml

TOM = 'p-aaaaaaaaaa'
ANN = 'p-bbbbbbbbbb'   # Tom's mother
BOB = 'p-cccccccccc'   # a neighbour who gave the information
TWIN = 'p-dddddddddd'  # another Thomas Hartley

SITE_RECORD = '''---
id: {sid}
title: {title}
source_type: {source_type}
{extra}repository: {repo}
citation: >
  {citation}
people:
  - "[[{pid}|Tom]]"
files:
  - file: documents/{sid}.json
    role: site-data
---

## Claims
```yaml
{claims}
```
'''


class _Archive:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / 'people' / 'stubs').mkdir(parents=True)
        (self.root / 'places').mkdir()
        (self.root / 'documents').mkdir()
        (self.root / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
        (self.root / 'places' / 'places.yaml').write_text('[]\n', encoding='utf-8')
        self._n = 0
        for pid, name in ((TOM, 'Thomas Hartley'), (ANN, 'Ann Hartley'),
                          (BOB, 'Bob Neighbour'), (TWIN, 'Thomas Hartley')):
            self.person(pid, name)

    def person(self, pid, name):
        meta = {'id': pid, 'name': name, 'living': 'unknown', 'tier': 'stub'}
        (self.root / 'people' / 'stubs' / f'p__{pid[2:]}_{pid}.md').write_text(
            '---\n' + yaml.safe_dump(meta, sort_keys=False) + f'---\n\n# {name}\n', encoding='utf-8')

    def source(self, claims, *, title=None, source_type='census', source_date=None, **meta_extra):
        self._n += 1
        sid = f's-{self._n:010d}'
        meta = {'id': sid, 'title': title or f'Record {self._n}', 'source_type': source_type}
        if source_date:
            meta['source_date'] = source_date
        meta.update(meta_extra)
        full = []
        for i, c in enumerate(claims):
            c = dict(c)
            c.setdefault('id', f'c-{self._n:05d}{i:05d}')
            c.setdefault('status', 'accepted')
            if c['status'] == 'accepted':
                c.setdefault('reviewed', '2026-01-01')
            c.setdefault('confidence', 'medium')
            c.setdefault('value', c['type'])
            full.append(c)
        folder = self.root / 'sources' / source_type
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f'rec_{sid}.md').write_text(
            '---\n' + yaml.safe_dump(meta, sort_keys=False) + '---\n\n## Claims\n```yaml\n'
            + yaml.safe_dump(full, sort_keys=False) + '```\n', encoding='utf-8')
        return sid

    def site_source(self, site, coll, *, name='Thomas Hartley'):
        """One census entry as the capture add-on files it, with its site data."""
        self._n += 1
        sid = f's-{self._n:010d}'
        record = ({'url': 'https://www.familysearch.org/ark:/61903/1:1:ABCD-123', 'collectionId': coll}
                  if site == 'fs' else {'dbId': coll, 'recordId': '4321'})
        record['name'] = name
        (self.root / 'documents' / f'{sid}.json').write_text(json.dumps({'record': record}), encoding='utf-8')
        if site == 'fs':
            citation, repo = ('"United States Census, 1880," database, FamilySearch '
                              '(https://www.familysearch.org/ark:/61903/1:1:ABCD-123)'), 'FamilySearch'
        else:
            citation, repo = f'Ancestry.com (https://www.ancestry.com/search/collections/{coll}/records/4321)', 'Ancestry.com'
        claims = [{'id': f'c-{self._n:05d}00000', 'type': 'birth', 'persons': [TOM], 'date': '1840~',
                   'value': 'born about 1840', 'status': 'accepted', 'reviewed': '2026-01-01'}]
        folder = self.root / 'sources' / 'census'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f'rec_{sid}.md').write_text(SITE_RECORD.format(
            sid=sid, title='Thomas Hartley 1880', source_type='census',
            extra="source_date: '1880-06-04'\n", repo=repo, citation=citation, pid=TOM,
            claims=yaml.safe_dump(claims, sort_keys=False)), encoding='utf-8')
        return sid

    def build(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            index_mod.build_index(self.root, load_fha_yaml(self.root))

    def run(self, person=TOM, **kw):
        self.build()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return compare.run_compare(self.root, load_fha_yaml(self.root), person=person, **kw)

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def _birth(date, *, pid=TOM, **extra):
    return {'type': 'birth', 'persons': [pid], 'date': date, 'value': f'born {date}', **extra}


class _Base(unittest.TestCase):
    def setUp(self):
        self.a = _Archive()
        self.addCleanup(self.a.cleanup)

    def rows(self, **kw):
        result = self.a.run(fact='birth', **kw)
        self.assertTrue(result.ok, [m.text for m in result.messages])
        return result.data['rows']


class WhichClaimsCountTests(_Base):
    def test_a_sons_birth_never_shows_under_his_mothers_own_birth(self):
        self.a.source([{'type': 'birth', 'persons': [TOM, ANN], 'date': '1840-03-01',
                        'roles': {'child': TOM, 'parent': [ANN]}}], source_type='vital-record')
        self.a.source([_birth('1815', pid=ANN)], source_type='vital-record')
        mother = self.a.run(person=ANN, fact='birth')
        self.assertEqual([r['date_edtf'] for r in mother.data['rows']], ['1815'])
        son = self.a.run(person=TOM, fact='birth')
        self.assertEqual([r['date_edtf'] for r in son.data['rows']], ['1840-03-01'])

    def test_baptism_counts_with_birth(self):
        self.a.source([_birth('1840-03-01')], source_type='vital-record')
        self.a.source([{'type': 'baptism', 'persons': [TOM], 'date': '1840-04-01'}], source_type='church')
        self.assertEqual(len(self.rows()), 2)

    def test_rejected_claims_are_left_out_unless_asked_for(self):
        self.a.source([_birth('1840'), _birth('1838', status='rejected', reviewed='2026-01-01')])
        self.assertEqual([r['date_edtf'] for r in self.rows()], ['1840'])
        both = self.rows(include_rejected=True)
        self.assertEqual(sorted(r['date_edtf'] for r in both), ['1838', '1840'])

    def test_accepted_only_leaves_out_suggested(self):
        self.a.source([_birth('1840'), _birth('1838', status='suggested')])
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual([r['date_edtf'] for r in self.rows(accepted_only=True)], ['1840'])

    def test_a_parked_claim_is_marked(self):
        self.a.source([_birth('1838', status='suggested', reviewed='2026-02-02')])
        self.assertTrue(self.rows()[0]['parked'])


class RowFieldTests(_Base):
    def test_recorded_after_years_comes_from_the_source_and_claim_dates(self):
        self.a.source([_birth('1840')], source_date='1880')
        span = self.rows()[0]['recorded_after_years']
        self.assertEqual(span, {'min': 40, 'max': 40})

    def test_recorded_after_is_null_when_either_side_is_undated(self):
        self.a.source([_birth('1840')])                      # no source date
        self.a.source([{'type': 'birth', 'persons': [TOM], 'value': 'born'}], source_date='1880')
        self.assertEqual([r['recorded_after_years'] for r in self.rows()], [None, None])

    def test_informants_are_named_from_their_role(self):
        self.a.source([{'type': 'death', 'persons': [TOM, BOB], 'date': '1900-01-01',
                        'roles': {'deceased': TOM, 'informant': [BOB]}}], source_type='vital-record')
        row = self.a.run(fact='death').data['rows'][0]
        self.assertEqual(row['informants'], ['Bob Neighbour'])

    def test_information_and_evidence_come_through(self):
        self.a.source([_birth('1840', information='primary', evidence='direct')])
        row = self.rows()[0]
        self.assertEqual((row['information'], row['evidence'], row['status']),
                         ('primary', 'direct', 'accepted'))

    def test_claim_links_name_the_row_they_point_at(self):
        self.a.source([_birth('1840', id='c-1a2b3c4d5e', corroborates=['c-2b3c4d5e6f'])])
        self.a.source([_birth('1840', id='c-2b3c4d5e6f')])
        rows = self.rows()
        one = next(r for r in rows if r['claim_id'] == 'c-1a2b3c4d5e')
        two = next(r for r in rows if r['claim_id'] == 'c-2b3c4d5e6f')
        self.assertEqual(one['links'][0]['row'], two['n'])


class SameOriginTests(_Base):
    def test_two_claims_on_one_source_are_one_origin(self):
        self.a.source([_birth('1840'), _birth('1841')])
        rows = self.rows()
        self.assertEqual(rows[0]['same_origin_as'], [2])
        self.assertEqual(rows[1]['same_origin_as'], [1])

    def test_one_record_saved_from_two_sites_is_one_origin(self):
        self.a.site_source('ancestry', '6742')
        self.a.site_source('fs', '1417683')
        rows = self.rows()
        self.assertEqual([r['same_origin_as'] for r in rows], [[2], [1]])
        self.assertIn('one record saved from two sites', rows[0]['origin_notes'])

    def test_two_records_from_one_site_are_not_one_origin(self):
        self.a.site_source('fs', '1417683')
        self.a.site_source('fs', '1417683')
        self.assertEqual([r['same_origin_as'] for r in self.rows()], [[], []])

    def test_claims_from_an_imported_tree_are_labelled_someones_tree(self):
        self.a.source([_birth('1840')], source_type='other', subtype='gedcom', title='Tree A')
        self.a.source([_birth('1841')], source_type='other', subtype='gedcom', title='Tree B')
        self.a.source([_birth('1842')], source_type='census')
        rows = self.rows()
        self.assertEqual(rows[0]['origin_notes'], ["someone's tree"])
        self.assertEqual(rows[2]['same_origin_as'], [])

    def test_a_website_tree_is_labelled_someones_tree(self):
        self.a.source([_birth('1840')], source_type='website', title='Hartley family tree (online)')
        self.a.source([_birth('1841')], source_type='website', title='Hartley family tree 2')
        self.assertIn("someone's tree", self.rows()[0]['origin_notes'])


class SummaryAndExitTests(_Base):
    def test_no_fact_lists_only_facts_whose_records_disagree(self):
        self.a.source([_birth('1838')], source_date='1880')
        self.a.source([_birth('1840')], source_date='1900')
        self.a.source([{'type': 'death', 'persons': [TOM], 'date': '1900'}])
        self.a.source([{'type': 'death', 'persons': [TOM], 'date': '1900'}])
        d = self.a.run().data
        self.assertEqual([f['fact'] for f in d['disagreeing']], ['birth'])
        self.assertEqual((d['disagreeing'][0]['first_year'], d['disagreeing'][0]['last_year']), (1838, 1840))

    def test_approximate_dates_that_overlap_do_not_disagree(self):
        self.a.source([_birth('1840~')])
        self.a.source([_birth('1841')])
        self.assertEqual(self.a.run().data['disagreeing'], [])

    def test_different_places_disagree_even_with_the_same_year(self):
        self.a.source([_birth('1840', place_text='Easton, New York')])
        self.a.source([_birth('1840', place_text='Boston, Massachusetts')])
        self.assertEqual([f['fact'] for f in self.a.run().data['disagreeing']], ['birth'])

    def test_no_claims_for_a_fact_is_exit_one_and_names_the_facts_that_exist(self):
        self.a.source([_birth('1840')])
        r = self.a.run(fact='death')
        self.assertEqual(r.exit_code, EXIT_WARNINGS)
        self.assertEqual(r.data['facts'], ['birth'])
        self.assertIn('birth', r.messages[0].text)

    def test_an_ambiguous_name_is_exit_two_with_both_ids(self):
        self.a.source([_birth('1840')])
        r = self.a.run(person='Thomas Hartley', fact='birth')
        self.assertEqual(r.exit_code, EXIT_ERRORS)
        self.assertIn('p-aaaaaaaaaa', r.messages[0].text)
        self.assertIn('p-dddddddddd', r.messages[0].text)

    def test_a_name_that_is_one_person_works(self):
        self.a.source([_birth('1840')])
        r = self.a.run(person='Ann Hartley', fact='birth')
        self.assertEqual(r.data['person'], ANN)

    def test_no_index_is_exit_three(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            r = compare.run_compare(self.a.root, {}, person=TOM, fact='birth')
        self.assertEqual(r.exit_code, EXIT_FAILURE)


class OutputTests(_Base):
    def _text(self, **kw):
        result = self.a.run(**kw)
        out = io.StringIO()
        with redirect_stdout(out):
            code = compare._cmd_compare(result)
        return code, out.getvalue()

    def test_the_text_table_says_who_told_it_and_how_long_after(self):
        self.a.source([{'type': 'birth', 'persons': [TOM, BOB], 'date': '1840', 'place_text': 'Easton, New York',
                        'roles': {'child': TOM, 'informant': [BOB]}, 'information': 'primary'}],
                      title='1880 census', source_date='1880')
        code, text = self._text(fact='birth')
        self.assertEqual(code, EXIT_CLEAN)
        self.assertIn('Birth of Thomas Hartley [p-aaaaaaaaaa]: 1 record', text)
        self.assertIn('1880 census (1880): about 40 years after. Told by: Bob Neighbour.', text)
        self.assertIn('firsthand', text)
        self.assertIn('Same origin: none found.', text)

    def test_json_has_the_documented_keys(self):
        self.a.source([_birth('1840')], source_date='1880')
        result = self.a.run(fact='birth')
        out = io.StringIO()
        with redirect_stdout(out):
            compare._cmd_compare(result, use_json=True)
        row = json.loads(out.getvalue())['data']['rows'][0]
        for key in ('n', 'value', 'date_edtf', 'place_text', 'place_id', 'source_id', 'source_title',
                    'source_type', 'source_class', 'source_date', 'repository', 'path',
                    'recorded_after_years', 'informants', 'information', 'evidence', 'status',
                    'reviewed', 'confidence', 'same_origin_as', 'links', 'anchor'):
            self.assertIn(key, row)

    def test_it_changes_nothing_on_disk(self):
        self.a.source([_birth('1838')], source_date='1880')
        self.a.source([_birth('1840')])
        self.a.build()

        def snapshot():
            h = {}
            for p in sorted(self.a.root.rglob('*')):
                if p.is_file() and '.cache' not in p.parts:
                    h[str(p)] = (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            return h
        before = snapshot()
        self._text()
        self._text(fact='birth')
        self.assertEqual(before, snapshot())


if __name__ == '__main__':
    unittest.main()
