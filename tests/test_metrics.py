"""
test_metrics.py - fha metrics: person completeness checks and archive totals
(TOOLING §7a, issue #343).

Each test builds a tiny temp archive, indexes it, and asks the shared engine
(`_lib.person_completeness`) or the command (`metrics.run_metrics`) one
question. Expected answers come from the rules TOOLING §7a states - a birth
record is a vital-record source; census years run from birth (or the first
record in that country) to death; a marriage record is a vital-record
marriage claim; a death is asked of anyone not living, and of anyone of
unknown status only past 130 years - never from the code's current output.
Temp fixtures throughout; never the real archive or example-archive.
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
import lint
import metrics
from _lib import (
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    completeness_context,
    completeness_summary_line,
    fmt_id_display,
    load_fha_yaml,
    person_completeness,
)

YEAR = 2026

KID = 'P-aaaaaaaaaa'
DAD = 'P-bbbbbbbbbb'
MOM = 'P-cccccccccc'
STEP = 'P-dddddddddd'
OTHER = 'P-eeeeeeeeee'

US = 'L-aaaaaaaaaa'      # Fairview, Kansas, USA
CA = 'L-bbbbbbbbbb'      # Toronto, Ontario, Canada
PL = 'L-cccccccccc'      # Suwalki, Poland
TOWN = 'L-dddddddddd'    # no hierarchy of its own, within US


class _Archive:
    """A throwaway archive: people, sources with claims, a place registry."""

    def __init__(self, root_person: str | None = None):
        self.root = Path(tempfile.mkdtemp())
        (self.root / 'people' / 'stubs').mkdir(parents=True)
        (self.root / 'places').mkdir()
        cfg = 'roots:\n  documents: documents\n'
        if root_person:
            cfg += f'root_person: {root_person}\n'
        (self.root / 'fha.yaml').write_text(cfg, encoding='utf-8')
        (self.root / 'places' / 'places.yaml').write_text(yaml.safe_dump([
            {'id': US, 'name': 'Fairview', 'hierarchy': 'Fairview, Breton County, Kansas, USA'},
            {'id': CA, 'name': 'Toronto', 'hierarchy': 'Toronto, Ontario, Canada'},
            {'id': PL, 'name': 'Suwalki', 'hierarchy': 'Suwalki, Poland'},
            {'id': TOWN, 'name': 'Elm Creek', 'within': US},
        ], sort_keys=False), encoding='utf-8')
        self._n = 0

    def person(self, pid: str, name: str, **fields) -> None:
        meta = {'id': pid, 'name': name, 'living': 'unknown', 'tier': 'stub', **fields}
        body = '---\n' + yaml.safe_dump(meta, sort_keys=False) + f'---\n\n# {name}\n'
        (self.root / 'people' / 'stubs' / f'p__{pid[2:].lower()}_{pid}.md').write_text(
            body, encoding='utf-8')

    def source(self, source_type: str, claims: list[dict], *, files=None,
               source_date=None, places=None) -> str:
        self._n += 1
        sid = f'S-{self._n:010d}'
        folder = self.root / 'sources' / source_type
        folder.mkdir(parents=True, exist_ok=True)
        meta = {'id': sid, 'title': f'{source_type} {self._n}', 'source_type': source_type}
        if source_date:
            meta['source_date'] = source_date
        if places:
            meta['places'] = list(places)
        if files:
            meta['files'] = files
        full = []
        for i, c in enumerate(claims):
            c = dict(c)
            c.setdefault('id', f'C-{self._n:05d}{i:05d}')
            c.setdefault('status', 'accepted')
            if c['status'] == 'accepted':
                c.setdefault('reviewed', '2026-01-01')
            c.setdefault('confidence', 'medium')
            c.setdefault('value', c['type'])
            full.append(c)
        text = ('---\n' + yaml.safe_dump(meta, sort_keys=False) + '---\n\n## Claims\n```yaml\n'
                + yaml.safe_dump(full, sort_keys=False) + '```\n')
        (folder / f'src_{sid}.md').write_text(text, encoding='utf-8')
        return sid

    def build(self) -> dict:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            index_mod.build_index(self.root, load_fha_yaml(self.root))
        conn = sqlite3.connect(self.root / '.cache' / 'index.sqlite')
        conn.row_factory = sqlite3.Row
        try:
            return completeness_context(conn)
        finally:
            conn.close()

    def check(self, pid: str, name: str, mode: str = 'accepted') -> dict:
        ctx = self.build()
        result = person_completeness(ctx, pid, mode=mode, this_year=YEAR)
        return next(c for c in result['checks'] if c['check'] == name)

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def _birth(pid, date, place=None, **extra):
    c = {'type': 'birth', 'persons': [pid], 'date': date, **extra}
    if place:
        c['place'] = place
    return c


class _Base(unittest.TestCase):
    def setUp(self):
        self.a = _Archive()
        for pid, name in ((KID, 'Kid'), (DAD, 'Dad'), (MOM, 'Mom'),
                          (STEP, 'Step'), (OTHER, 'Other')):
            self.a.person(pid, name)

    def tearDown(self):
        self.a.cleanup()


class BirthCheckTests(_Base):
    def test_vital_record_birth_is_met(self):
        self.a.source('vital-record', [_birth(KID, '1880-03-02')])
        self.assertEqual(self.a.check(KID, 'birth')['state'], 'met')

    def test_birth_from_census_age_is_partial(self):
        self.a.source('census', [_birth(KID, '1880~')])
        self.assertEqual(self.a.check(KID, 'birth')['state'], 'partial')

    def test_secondary_information_on_a_vital_record_is_partial(self):
        # A death certificate's birth date: a vital record, but secondary.
        self.a.source('vital-record', [_birth(KID, '1880', information='secondary')])
        self.assertEqual(self.a.check(KID, 'birth')['state'], 'partial')

    def test_mother_named_on_sons_birth_record_gets_no_birth(self):
        self.a.source('vital-record', [{
            'type': 'birth', 'persons': [KID, MOM], 'date': '1880',
            'roles': {'child': KID, 'parent': [MOM]}}])
        self.assertEqual(self.a.check(MOM, 'birth')['state'], 'missing')
        self.assertEqual(self.a.check(KID, 'birth')['state'], 'met')

    def test_birth_read_whatever_role_the_person_carries(self):
        # Two people named, the subject under `child:` - still his own birth.
        self.a.source('vital-record', [{
            'type': 'birth', 'persons': [OTHER, KID], 'date': '1880',
            'roles': {'informant': OTHER, 'child': KID}}])
        self.assertEqual(self.a.check(KID, 'birth')['state'], 'met')

    def test_a_death_certificates_birth_date_is_not_a_birth_record(self):
        # The usual hand-typed shape: no information: field at all.
        self.a.source('vital-record', [
            {'type': 'death', 'persons': [KID], 'date': '1940-02-01'},
            _birth(KID, '1872')])
        self.assertEqual(self.a.check(KID, 'birth')['state'], 'partial')

    def test_another_persons_death_on_the_same_register_leaves_a_birth_record(self):
        # A family register: the child's birth, and the father's death, unroled.
        self.a.source('vital-record', [
            _birth(KID, '1872'),
            {'type': 'death', 'persons': [DAD], 'date': '1890-02-01'}])
        self.assertEqual(self.a.check(KID, 'birth')['state'], 'met')

    def test_a_rejected_burial_on_the_register_leaves_a_birth_record(self):
        self.a.source('vital-record', [
            {'type': 'baptism', 'persons': [KID], 'date': '1880-04-01'},
            {'type': 'burial', 'persons': [KID], 'date': '1881-01-01',
             'status': 'rejected', 'reviewed': '2026-01-01'}])
        self.assertEqual(self.a.check(KID, 'birth')['state'], 'met')

    def test_an_infants_baptism_and_burial_on_one_register_is_a_birth_record(self):
        # An infant baptised in 1880 and buried in 1881, both on the parish
        # register: the baptism entry is the record of the event.
        self.a.source('vital-record', [
            {'type': 'baptism', 'persons': [KID], 'date': '1880-04-01'},
            {'type': 'burial', 'persons': [KID], 'date': '1881-01-01'}])
        self.assertEqual(self.a.check(KID, 'birth')['state'], 'met')

    def test_an_adults_baptism_decades_before_the_burial_names_the_fix(self):
        # Baptism read off the same register decades before the burial: a
        # later reading, so under-counted, and the detail names the fix.
        self.a.source('vital-record', [
            {'type': 'baptism', 'persons': [KID], 'date': '1880-04-01'},
            {'type': 'burial', 'persons': [KID], 'date': '1941-01-01'}])
        c = self.a.check(KID, 'birth')
        self.assertEqual(c['state'], 'partial')
        self.assertIn('information: primary', c['detail'])

    def test_an_undated_burial_beside_a_baptism_stays_a_later_reading(self):
        self.a.source('vital-record', [
            {'type': 'baptism', 'persons': [KID], 'date': '1880-04-01'},
            {'type': 'burial', 'persons': [KID]}])
        self.assertEqual(self.a.check(KID, 'birth')['state'], 'partial')

    def test_a_birth_claim_beside_an_infants_burial_is_still_a_later_reading(self):
        # "Aged 3 months" on a burial entry gives a birth year, not a birth
        # record: only a baptism entry is its own record.
        self.a.source('vital-record', [
            _birth(KID, '1880'),
            {'type': 'burial', 'persons': [KID], 'date': '1881-01-01'}])
        self.assertEqual(self.a.check(KID, 'birth')['state'], 'partial')

    def test_information_primary_overrides_the_death_record_rule(self):
        self.a.source('vital-record', [
            {'type': 'death', 'persons': [KID], 'date': '1940-02-01'},
            _birth(KID, '1872', information='primary')])
        self.assertEqual(self.a.check(KID, 'birth')['state'], 'met')

    def test_suggested_birth_counts_only_when_suggested_are_included(self):
        self.a.source('vital-record', [_birth(KID, '1880', status='suggested')])
        self.assertEqual(self.a.check(KID, 'birth', 'accepted')['state'], 'missing')
        self.assertEqual(self.a.check(KID, 'birth', 'include-suggested')['state'], 'met')

    def test_rejected_and_negated_claims_never_count(self):
        self.a.source('vital-record', [
            _birth(KID, '1880', status='rejected', reviewed='2026-01-01'),
            _birth(KID, '1881', negated=True, evidence='negative')])
        self.assertEqual(self.a.check(KID, 'birth', 'include-suggested')['state'], 'missing')


class CensusCheckTests(_Base):
    def test_born_in_us_expects_every_census_to_death(self):
        self.a.source('vital-record', [_birth(KID, '1872', US)])
        self.a.source('vital-record', [{'type': 'death', 'persons': [KID], 'date': '1915'}])
        self.a.source('census', [{'type': 'census', 'persons': [KID], 'date': '1880-06'}])
        c = self.a.check(KID, 'census')
        # 1890 is an `extra` year (the schedules burned): never reported missing.
        self.assertEqual(c['expected'], [1880, 1900, 1910])
        self.assertEqual(c['found'], [1880])
        self.assertEqual(c['missing'], [1900, 1910])
        self.assertEqual(c['state'], 'partial')

    def test_residence_on_a_census_source_counts_as_that_census(self):
        self.a.source('vital-record', [_birth(KID, '1872', US),
                                       {'type': 'death', 'persons': [KID], 'date': '1889'}])
        self.a.source('census', [{'type': 'residence', 'persons': [KID], 'date': '1880-06',
                                  'place': US}])
        c = self.a.check(KID, 'census')
        self.assertEqual((c['expected'], c['state']), ([1880], 'met'))

    def test_residence_on_a_non_census_source_does_not(self):
        self.a.source('vital-record', [_birth(KID, '1872', US),
                                       {'type': 'death', 'persons': [KID], 'date': '1889'}])
        self.a.source('directory', [{'type': 'residence', 'persons': [KID], 'date': '1880'}])
        self.assertEqual(self.a.check(KID, 'census')['found'], [])

    def test_no_census_year_asked_before_their_first_record_there(self):
        self.a.source('vital-record', [_birth(KID, '1850', PL)])
        self.a.source('other', [{'type': 'immigration', 'persons': [KID], 'date': '1885',
                                 'place': US}])
        self.a.source('vital-record', [{'type': 'death', 'persons': [KID], 'date': '1912'}])
        self.assertEqual(self.a.check(KID, 'census')['expected'], [1900, 1910])

    def test_census_birth_claim_is_not_a_census_year(self):
        # The census gave his age; the birth year it implies is not a census.
        self.a.source('vital-record', [_birth(KID, '1872', US),
                                       {'type': 'death', 'persons': [KID], 'date': '1889'}])
        self.a.source('census', [_birth(KID, '1880~')])
        self.assertEqual(self.a.check(KID, 'census')['found'], [])

    def test_undated_claims_do_not_hide_real_years(self):
        self.a.source('other', [{'type': 'occupation', 'persons': [KID], 'place': US}])
        self.a.source('vital-record', [_birth(KID, '1872', US),
                                       {'type': 'death', 'persons': [KID], 'date': '1889'}])
        self.assertEqual(self.a.check(KID, 'census')['expected'], [1880])

    def test_country_reached_through_within(self):
        self.a.source('vital-record', [_birth(KID, '1872', TOWN),
                                       {'type': 'death', 'persons': [KID], 'date': '1889'}])
        self.assertEqual(self.a.check(KID, 'census')['countries'], ['United States'])

    def test_canada_uses_its_own_years(self):
        self.a.source('vital-record', [_birth(KID, '1870', CA),
                                       {'type': 'death', 'persons': [KID], 'date': '1895'}])
        self.assertEqual(self.a.check(KID, 'census')['expected'], [1871, 1881, 1891])

    def test_no_registered_place_is_reported_not_guessed(self):
        self.a.source('vital-record', [_birth(KID, '1872')])
        c = self.a.check(KID, 'census')
        self.assertEqual(c['state'], 'missing')
        self.assertIn("can't tell which country", c['detail'])

    def test_cant_tell_which_country_names_the_next_step(self):
        self.a.source('vital-record', [_birth(KID, '1872')])
        ctx = self.a.build()
        r = person_completeness(ctx, KID, this_year=YEAR)
        census = next(c for c in r['checks'] if c['check'] == 'census')
        self.assertIn('fha report --section place-candidates', census['detail'])
        self.assertIn('register the place', completeness_summary_line(r))

    def test_a_census_with_a_subject_still_places_and_dates_the_head(self):
        # A census entry written with subject: beside head: - the head was
        # enumerated too, so the entry places him and counts as his year.
        self.a.source('vital-record', [_birth(DAD, '1850'),
                                       {'type': 'death', 'persons': [DAD], 'date': '1885'}])
        self.a.source('census', [{'type': 'census', 'persons': [KID, DAD], 'date': '1880',
                                  'place': US, 'roles': {'subject': KID, 'head': DAD}}])
        c = self.a.check(DAD, 'census')
        self.assertEqual(c['countries'], ['United States'])
        self.assertIn(1880, c['found'])

    def test_no_death_year_is_never_met(self):
        # Every census up to the last record is on file, but with no death
        # year the later ones may still be out there.
        self.a.source('vital-record', [_birth(KID, '1872', US)])
        self.a.source('census', [{'type': 'census', 'persons': [KID], 'date': f'{y}', 'place': US}
                                 for y in (1880, 1900, 1910, 1920)])
        c = self.a.check(KID, 'census')
        self.assertEqual(c['state'], 'partial')
        self.assertEqual(c['missing'], [])
        self.assertEqual(c['unchecked_after'], 1920)
        self.assertIn('no death year', c['detail'])
        self.assertIn('1920', c['detail'])

    def test_a_birth_record_alone_is_not_nothing_to_find(self):
        self.a.source('vital-record', [_birth(KID, '1872', US)])
        c = self.a.check(KID, 'census')
        self.assertEqual((c['state'], c['expected']), ('partial', []))
        self.assertIn('no death year', c['detail'])

    def test_born_after_the_last_census_year_does_not_apply(self):
        self.a.source('vital-record', [_birth(KID, '1960', US)])
        self.assertEqual(self.a.check(KID, 'census')['state'], 'na')

    def test_country_with_no_table_does_not_apply(self):
        self.a.source('vital-record', [_birth(KID, '1872', PL),
                                       {'type': 'death', 'persons': [KID], 'date': '1940',
                                        'place': PL}])
        self.assertEqual(self.a.check(KID, 'census')['state'], 'na')

    def test_a_move_abroad_shows_as_a_gap_not_a_complete(self):
        # No migration is modelled: born in Canada, so Canada's years run to
        # death; the years spent in the U.S. stay visible as Canadian gaps.
        self.a.source('vital-record', [_birth(KID, '1850', CA)])
        self.a.source('census', [{'type': 'census', 'persons': [KID], 'date': f'{y}', 'place': CA}
                                 for y in (1851, 1861, 1871, 1881)])
        self.a.source('other', [{'type': 'immigration', 'persons': [KID], 'date': '1885',
                                 'place': US}])
        self.a.source('census', [{'type': 'census', 'persons': [KID], 'date': f'{y}', 'place': US}
                                 for y in (1900, 1910, 1920)])
        self.a.source('vital-record', [{'type': 'death', 'persons': [KID], 'date': '1920-12-01',
                                        'place': US}])
        c = self.a.check(KID, 'census')
        self.assertEqual(c['state'], 'partial')
        self.assertEqual(c['missing'], [1891, 1901, 1911])

    def test_a_death_abroad_does_not_hide_missing_censuses_at_home(self):
        # No immigration claim: nothing says when they left, so the gaps stay.
        self.a.source('vital-record', [_birth(KID, '1850', US)])
        self.a.source('census', [{'type': 'census', 'persons': [KID], 'date': '1850', 'place': US}])
        self.a.source('vital-record', [{'type': 'death', 'persons': [KID], 'date': '1900',
                                        'place': CA}])
        c = self.a.check(KID, 'census')
        self.assertEqual(c['state'], 'partial')
        self.assertIn(1880, c['missing'])

    def test_a_sons_occupation_does_not_place_his_mother(self):
        self.a.source('vital-record', [_birth(MOM, '1850', CA),
                                       {'type': 'death', 'persons': [MOM], 'date': '1895',
                                        'place': CA}])
        self.a.source('directory', [{'type': 'occupation', 'persons': [KID, MOM],
                                     'date': '1880', 'place': US,
                                     'roles': {'subject': KID, 'parent': MOM}}])
        self.assertEqual(self.a.check(MOM, 'census')['countries'], ['Canada'])

    def test_a_census_inside_an_uncertain_death_window_still_counts(self):
        self.a.source('vital-record', [_birth(KID, '1870', US),
                                       {'type': 'death', 'persons': [KID], 'date': '1920/1940'}])
        self.a.source('census', [{'type': 'census', 'persons': [KID], 'date': '1930', 'place': US}])
        c = self.a.check(KID, 'census')
        self.assertIn(1930, c['extras'])            # found, inside the death window
        self.assertNotIn(1940, c['missing'])        # maybe dead by then: never asked
        self.assertIn(1910, c['missing'])           # surely alive: still asked

    def test_an_adult_baptism_is_not_the_birth_year(self):
        self.a.person(KID, 'Kid', birth='1870')
        self.a.source('vital-record', [
            {'type': 'baptism', 'persons': [KID], 'date': '1900', 'place': US},
            {'type': 'death', 'persons': [KID], 'date': '1905'}])
        self.assertEqual(self.a.check(KID, 'marriage')['state'], 'missing')
        # The baptism is not a birthplace: with no birth place known, U.S.
        # years start at their first U.S. record, not at the birth year.
        self.assertEqual(self.a.check(KID, 'census')['expected'], [1900])

    def test_a_baptism_abroad_is_not_a_birthplace(self):
        self.a.source('vital-record', [_birth(KID, '1870', US)])
        self.a.source('vital-record', [{'type': 'baptism', 'persons': [KID], 'date': '1900',
                                        'place': CA},
                                       {'type': 'death', 'persons': [KID], 'date': '1905'}])
        c = self.a.check(KID, 'census')
        self.assertFalse([y for y in c['expected'] if y in (1871, 1881, 1891)])

    def test_died_before_1880_does_not_demand_the_1880_census(self):
        self.a.source('vital-record', [_birth(KID, '1870', US),
                                       {'type': 'death', 'persons': [KID], 'date': '[..1880]'}])
        c = self.a.check(KID, 'census')
        self.assertNotIn(1880, c['missing'])
        self.assertIsNone(c['unchecked_after'])        # a death year is known
        self.assertNotIn('no death year', c['detail'])

    def test_a_provisional_died_before_date_does_not_demand_the_census_either(self):
        self.a.person(KID, 'Kid', death='[..1880]')
        self.a.source('vital-record', [_birth(KID, '1870', US)])
        c = self.a.check(KID, 'census')
        self.assertNotIn(1880, c['missing'])
        self.assertNotIn('no death year', c['detail'])

    def test_an_undated_record_at_a_registered_place_asks_for_a_date(self):
        self.a.person(KID, 'Kid', birth='1870', death='1905')
        self.a.source('other', [{'type': 'residence', 'persons': [KID], 'place': US}])
        c = self.a.check(KID, 'census')
        self.assertEqual(c['countries'], ['United States'])
        self.assertIn('add a date', c['detail'])
        self.assertNotIn('register', c['detail'])

    def test_a_foreign_census_does_not_fill_a_home_census_year(self):
        uk = 'L-eeeeeeeeee'
        reg = self.a.root / 'places' / 'places.yaml'
        reg.write_text(reg.read_text(encoding='utf-8')
                       + f'- id: {uk}\n  name: London\n  hierarchy: London, United Kingdom\n',
                       encoding='utf-8')
        self.a.source('vital-record', [_birth(KID, '1870', CA),
                                       {'type': 'death', 'persons': [KID], 'date': '1905'}])
        self.a.source('census', [{'type': 'census', 'persons': [KID], 'date': '1881', 'place': uk}])
        c = self.a.check(KID, 'census')
        self.assertIn(1881, c['missing'])

    def test_an_undated_second_country_keeps_the_check_open(self):
        self.a.source('vital-record', [_birth(KID, '1870', US),
                                       {'type': 'death', 'persons': [KID], 'date': '1905'}])
        self.a.source('census', [{'type': 'census', 'persons': [KID], 'date': f'{y}', 'place': US}
                                 for y in (1880, 1900)])
        self.a.source('other', [{'type': 'residence', 'persons': [KID], 'place': CA}])
        c = self.a.check(KID, 'census')
        self.assertEqual(c['state'], 'partial')
        self.assertIn('Canada', c['countries'])
        self.assertIn('Canada have no date', c['detail'])

    def test_the_summary_line_names_an_undated_country(self):
        self.a.person(KID, 'Kid', birth='1870', death='1905')
        self.a.source('other', [{'type': 'residence', 'persons': [KID], 'place': US}])
        ctx = self.a.build()
        line = completeness_summary_line(person_completeness(ctx, KID, this_year=YEAR))
        self.assertIn('census years unknown (date a record)', line)
        self.assertNotIn('; ;', line)

    def test_an_open_ended_census_date_does_not_crash(self):
        self.a.source('vital-record', [_birth(KID, '1870', US)])
        self.a.source('census', [{'type': 'census', 'persons': [KID], 'date': '[1883..]',
                                  'place': US}])
        self.assertIn(self.a.check(KID, 'census')['state'], ('missing', 'partial'))

    def test_a_mention_on_a_sons_death_record_does_not_stretch_a_life(self):
        self.a.source('census', [{'type': 'census', 'persons': [DAD], 'date': f'{y}', 'place': US}
                                 for y in (1850, 1860)])
        self.a.source('vital-record', [_birth(DAD, '1820')])
        self.a.source('vital-record', [{
            'type': 'death', 'persons': [KID, DAD], 'date': '1950-03-01', 'place': US,
            'roles': {'deceased': KID, 'parent': DAD}}])
        c = self.a.check(DAD, 'census')
        self.assertEqual(c['expected'], [1850, 1860])

    def test_an_informant_on_someone_elses_census_is_not_enumerated(self):
        # SPEC 8.3: an informant is named on the record, not a party to it.
        self.a.source('vital-record', [_birth(MOM, '1850', US),
                                       {'type': 'death', 'persons': [MOM], 'date': '1889'}])
        self.a.source('census', [{'type': 'residence', 'persons': [KID, MOM], 'date': '1880-06',
                                  'place': US, 'roles': {'subject': KID, 'informant': MOM}}])
        c = self.a.check(MOM, 'census')
        self.assertNotIn(1880, c['found'])
        self.assertIn(1880, c['missing'])

    def test_an_informant_mention_does_not_add_a_country(self):
        self.a.source('vital-record', [_birth(MOM, '1850', CA),
                                       {'type': 'death', 'persons': [MOM], 'date': '1895',
                                        'place': CA}])
        self.a.source('census', [{'type': 'residence', 'persons': [KID, MOM], 'date': '1880-06',
                                  'place': US, 'roles': {'subject': KID, 'informant': MOM}}])
        c = self.a.check(MOM, 'census')
        self.assertEqual(c['countries'], ['Canada'])
        self.assertNotIn(1880, c['expected'])

    def test_an_informant_who_is_also_household_member_is_enumerated(self):
        self.a.source('vital-record', [_birth(MOM, '1850', US),
                                       {'type': 'death', 'persons': [MOM], 'date': '1889'}])
        self.a.source('census', [{'type': 'residence', 'persons': [KID, MOM], 'date': '1880-06',
                                  'place': US,
                                  'roles': {'head': KID, 'household_member': MOM,
                                            'informant': MOM}}])
        self.assertIn(1880, self.a.check(MOM, 'census')['found'])

    def test_unplaced_census_year_on_file_widens_the_window(self):
        # Placed in the U.S. only from 1880, but an 1850 census is on file.
        self.a.source('census', [{'type': 'residence', 'persons': [KID], 'date': '1850-08-14'}])
        self.a.source('census', [{'type': 'residence', 'persons': [KID], 'date': '1880-06',
                                  'place': US}])
        c = self.a.check(KID, 'census')
        self.assertEqual(c['expected'], [1850, 1860, 1870, 1880])
        self.assertEqual(c['missing'], [1860, 1870])

    def _register(self, lid: str, name: str, hierarchy: str) -> None:
        reg = self.a.root / 'places' / 'places.yaml'
        reg.write_text(reg.read_text(encoding='utf-8')
                       + f'- id: {lid}\n  name: {name}\n  hierarchy: {hierarchy}\n',
                       encoding='utf-8')

    def test_a_place_ending_at_a_state_counts_toward_its_country(self):
        # "Elm Creek, Kansas" stops at the state: still a U.S. census.
        ks = 'L-ffffffffff'
        self._register(ks, 'Elm Grove', 'Elm Grove, Kansas')
        self.a.source('vital-record', [_birth(KID, '1872', US),
                                       {'type': 'death', 'persons': [KID], 'date': '1889'}])
        self.a.source('census', [{'type': 'census', 'persons': [KID], 'date': '1880',
                                  'place': ks}])
        c = self.a.check(KID, 'census')
        self.assertEqual((c['expected'], c['found'], c['state']), ([1880], [1880], 'met'))

    def test_a_place_ending_at_a_province_counts_toward_canada(self):
        on = 'L-ffffffffff'
        self._register(on, 'Elm Grove', 'Elm Grove, Ontario')
        self.a.source('vital-record', [_birth(KID, '1872', on),
                                       {'type': 'death', 'persons': [KID], 'date': '1889'}])
        self.a.source('census', [{'type': 'census', 'persons': [KID], 'date': '1881',
                                  'place': on}])
        c = self.a.check(KID, 'census')
        self.assertEqual((c['countries'], c['expected'], c['state']),
                         (['Canada'], [1881], 'met'))

    def test_georgia_the_country_never_fills_a_us_census_year(self):
        # Georgia is a U.S. state and a country: not read as either, so a
        # census there never fills the U.S. year.
        ge = 'L-ffffffffff'
        self._register(ge, 'Tbilisi', 'Tbilisi, Georgia')
        self.a.source('vital-record', [_birth(KID, '1872', US),
                                       {'type': 'death', 'persons': [KID], 'date': '1889'}])
        self.a.source('census', [{'type': 'census', 'persons': [KID], 'date': '1880',
                                  'place': ge}])
        c = self.a.check(KID, 'census')
        self.assertEqual((c['missing'], c['state']), ([1880], 'missing'))

    def test_an_exact_death_outranks_an_approximate_one(self):
        # Died 19 Jan 1941 by the certificate; a recollection says "funeral
        # about 1940" (read as 1939 to 1941). The exact date decides, so the
        # 1940 census is still asked, and the detail says why.
        self.a.source('vital-record', [_birth(KID, '1900', US),
                                       {'type': 'death', 'persons': [KID],
                                        'date': '1941-01-19'}])
        self.a.source('other', [{'type': 'burial', 'persons': [KID], 'date': '1940~'}])
        c = self.a.check(KID, 'census')
        self.assertIn(1940, c['expected'])
        self.assertIn(1940, c['missing'])
        self.assertIn('1941-01-19', c['detail'])
        self.assertIn('1940~', c['detail'])

    def test_an_approximate_death_alone_still_opens_a_window(self):
        # Only the vague date: 1940 may fall after the death, so it is not
        # asked (the window rule is unchanged).
        self.a.source('vital-record', [_birth(KID, '1900', US)])
        self.a.source('other', [{'type': 'burial', 'persons': [KID], 'date': '1940~'}])
        c = self.a.check(KID, 'census')
        self.assertNotIn(1940, c['expected'])

    def test_an_occupation_from_a_census_counts_as_that_census(self):
        self.a.source('vital-record', [_birth(KID, '1872', US),
                                       {'type': 'death', 'persons': [KID], 'date': '1905'}])
        self.a.source('census', [{'type': 'census', 'persons': [KID], 'date': '1880',
                                  'place': US}])
        self.a.source('census', [{'type': 'occupation', 'persons': [KID], 'date': '1900',
                                  'place': US}], source_date='1900-06-01')
        c = self.a.check(KID, 'census')
        self.assertEqual((c['found'], c['state']), ([1880, 1900], 'met'))

    def test_an_event_from_a_census_takes_the_sources_place(self):
        self.a.source('vital-record', [_birth(KID, '1872', US),
                                       {'type': 'death', 'persons': [KID], 'date': '1885'}])
        self.a.source('census', [{'type': 'event', 'persons': [KID], 'date': '1880'}],
                      source_date='1880', places=[US])
        c = self.a.check(KID, 'census')
        self.assertEqual((c['found'], c['state']), ([1880], 'met'))

    def test_a_census_claim_off_the_census_year_never_counts(self):
        # The 1900 census gives an immigration year of 1890 (an extra year):
        # that is not the 1890 census. An undated census source proves no
        # year, and an unplaced claim on an unplaced source no country.
        self.a.source('vital-record', [_birth(KID, '1872', US),
                                       {'type': 'death', 'persons': [KID], 'date': '1905'}])
        self.a.source('census', [{'type': 'event', 'persons': [KID], 'date': '1890',
                                  'place': US}], source_date='1900')
        self.a.source('census', [{'type': 'occupation', 'persons': [KID], 'date': '1880',
                                  'place': US}])
        self.a.source('census', [{'type': 'occupation', 'persons': [KID], 'date': '1900'}],
                      source_date='1900')
        c = self.a.check(KID, 'census')
        self.assertEqual(c['found'], [])
        self.assertNotIn(1890, c['extras'])
        self.assertEqual(c['state'], 'missing')


class ParentsCheckTests(_Base):
    def _rel(self, child, parents, **extra):
        return {'type': 'relationship', 'persons': [child, *parents],
                'roles': {'child': child, 'parent': list(parents)}, **extra}

    def test_both_parents_from_a_birth_record(self):
        self.a.source('vital-record', [{
            'type': 'birth', 'persons': [KID, DAD, MOM], 'date': '1880',
            'roles': {'child': KID, 'parent': [DAD, MOM]}}])
        c = self.a.check(KID, 'parents')
        self.assertEqual(c['state'], 'met')
        self.assertEqual(c['from_birth_record'], sorted([DAD.lower(), MOM.lower()]))

    def test_a_stepfather_does_not_complete_the_parents(self):
        self.a.source('vital-record', [{
            'type': 'birth', 'persons': [KID, MOM], 'date': '1880',
            'roles': {'child': KID, 'parent': [MOM]}}])
        self.a.source('census', [self._rel(KID, [STEP], subtype='step')])
        c = self.a.check(KID, 'parents')
        self.assertEqual(c['state'], 'partial')
        self.assertEqual(c['other_parents'], [STEP.lower()])
        self.assertIn('step', c['detail'])

    def test_three_birth_parents_are_a_conflict_not_complete(self):
        self.a.source('census', [self._rel(KID, [DAD, MOM])])
        self.a.source('other', [self._rel(KID, [OTHER])])
        c = self.a.check(KID, 'parents')
        self.assertEqual(c['state'], 'partial')
        self.assertIn('3 birth parents named', c['detail'])
        self.assertIn('review them', c['detail'])

    def test_one_parent_is_partial(self):
        self.a.source('census', [self._rel(KID, [DAD])])
        c = self.a.check(KID, 'parents')
        self.assertEqual(c['state'], 'partial')
        self.assertIn('not a birth record', c['detail'])

    def test_parents_from_a_marriage_certificate_are_not_from_a_birth_record(self):
        # A vital record, but the child's marriage record, not a birth record.
        self.a.source('vital-record', [self._rel(KID, [DAD, MOM])])
        c = self.a.check(KID, 'parents')
        self.assertEqual(c['state'], 'met')
        self.assertEqual(c['from_birth_record'], [])
        self.assertIn('not a birth record', c['detail'])

    def test_unsourced_hypothesis_parent_does_not_count(self):
        self.a.person(KID, 'Kid', relationships=[
            {'to': f'[[{DAD}]]', 'type': 'parent', 'status': 'hypothesis'}])
        self.assertEqual(self.a.check(KID, 'parents')['state'], 'missing')

    def test_suggested_parent_counts_only_when_suggested_are_included(self):
        self.a.source('census', [self._rel(KID, [DAD, MOM], status='suggested')])
        self.assertEqual(self.a.check(KID, 'parents', 'accepted')['state'], 'missing')
        self.assertEqual(self.a.check(KID, 'parents', 'include-suggested')['state'], 'met')


class MarriageCheckTests(_Base):
    def _marriage(self, a, b, **extra):
        return {'type': 'marriage', 'persons': [a, b], 'date': '1870',
                'roles': {'spouse': [a, b]}, **extra}

    def test_marriage_record_is_met(self):
        self.a.source('vital-record', [self._marriage(DAD, MOM)])
        self.assertEqual(self.a.check(DAD, 'marriage')['state'], 'met')

    def test_newspaper_marriage_notice_is_not_a_marriage_record(self):
        self.a.source('newspaper', [self._marriage(DAD, MOM)])
        c = self.a.check(DAD, 'marriage')
        self.assertEqual(c['state'], 'missing')
        self.assertEqual(c['partners'], [MOM.lower()])

    def test_parents_of_a_child_with_no_marriage_claim_are_flagged(self):
        self.a.source('census', [{
            'type': 'relationship', 'persons': [KID, DAD, MOM],
            'roles': {'child': KID, 'parent': [DAD, MOM]}}])
        c = self.a.check(DAD, 'marriage')
        self.assertEqual((c['state'], c['partners']), ('missing', [MOM.lower()]))

    def test_a_childs_step_parent_is_not_a_partner(self):
        self.a.source('vital-record', [self._marriage(DAD, MOM)])
        self.a.source('census', [
            {'type': 'relationship', 'persons': [KID, DAD, MOM],
             'roles': {'child': KID, 'parent': [DAD, MOM]}},
            {'type': 'relationship', 'subtype': 'step', 'persons': [KID, STEP],
             'roles': {'child': KID, 'parent': [STEP]}}])
        c = self.a.check(DAD, 'marriage')
        self.assertEqual((c['state'], c['partners']), ('met', [MOM.lower()]))

    def test_an_adoptive_parent_is_not_the_birth_parents_partner(self):
        self.a.source('vital-record', [{
            'type': 'birth', 'persons': [KID, DAD, MOM], 'date': '1880',
            'roles': {'child': KID, 'parent': [DAD, MOM]}}])
        self.a.source('other', [{
            'type': 'relationship', 'subtype': 'adoptive', 'persons': [KID, STEP],
            'roles': {'child': KID, 'parent': [STEP]}}])
        self.assertEqual(self.a.check(STEP, 'marriage')['partners'], [])
        self.assertEqual(self.a.check(DAD, 'marriage')['partners'], [MOM.lower()])

    def test_a_one_person_confirmed_absence_counts_as_complete(self):
        # SPEC A.2's ordinary shape: "no marriage found", naming only them.
        self.a.source('proof-argument', [{
            'type': 'marriage', 'persons': [OTHER], 'value': 'no marriage found',
            'negated': True, 'evidence': 'negative'}])
        c = self.a.check(OTHER, 'marriage')
        self.assertEqual((c['state'], c['detail']), ('met', 'never married (confirmed)'))

    def test_a_marriage_read_off_a_death_certificate_is_not_a_marriage_record(self):
        # Both spouses get the same answer about the same marriage.
        self.a.source('vital-record', [
            {'type': 'death', 'persons': [DAD], 'date': '1920-01-01'},
            self._marriage(DAD, MOM)])
        self.assertEqual(self.a.check(DAD, 'marriage')['state'], 'missing')
        self.assertEqual(self.a.check(MOM, 'marriage')['state'], 'missing')

    def test_did_not_marry_someone_is_not_never_married(self):
        self.a.source('vital-record', [self._marriage(KID, MOM)])
        self.a.source('proof-argument', [{
            'type': 'marriage', 'persons': [KID, OTHER], 'negated': True,
            'evidence': 'negative', 'roles': {'spouse': [KID, OTHER]}}])
        c = self.a.check(KID, 'marriage')
        self.assertEqual((c['state'], c['partners']), ('met', [MOM.lower()]))
        self.assertNotIn('never married', c['detail'])

    def test_a_provisional_death_on_the_record_still_counts_as_died_young(self):
        self.a.person(KID, 'Kid', death='1882')
        self.a.source('vital-record', [_birth(KID, '1880')])
        self.assertEqual(self.a.check(KID, 'marriage')['state'], 'na')

    def test_a_divorce_reveals_a_partner_whose_marriage_record_is_missing(self):
        self.a.source('vital-record', [self._marriage(DAD, MOM)])
        self.a.source('other', [{'type': 'divorce', 'persons': [DAD, OTHER], 'date': '1920',
                                 'roles': {'spouse': [DAD, OTHER]}}])
        c = self.a.check(DAD, 'marriage')
        self.assertEqual(c['state'], 'partial')
        self.assertEqual(c['partners'], sorted([MOM.lower(), OTHER.lower()]))

    def test_born_before_1880_and_an_adult_baptism_is_not_died_young(self):
        self.a.source('vital-record', [_birth(KID, '[..1880]'),
                                       {'type': 'baptism', 'persons': [KID], 'date': '1900'},
                                       {'type': 'death', 'persons': [KID], 'date': '1905'}])
        self.assertEqual(self.a.check(KID, 'marriage')['state'], 'missing')

    def test_a_wide_death_range_is_not_died_young(self):
        self.a.source('vital-record', [_birth(KID, '1895'),
                                       {'type': 'death', 'persons': [KID], 'date': '1900/1960'}])
        self.assertEqual(self.a.check(KID, 'marriage')['state'], 'missing')

    def test_a_child_who_died_young_is_not_asked_for_a_marriage(self):
        self.a.source('vital-record', [_birth(KID, '1880-03-01'),
                                       {'type': 'death', 'persons': [KID], 'date': '1882-05-01'}])
        c = self.a.check(KID, 'marriage')
        self.assertEqual(c['state'], 'na')
        self.assertIn('died young', c['detail'])

    def test_a_sourced_spouse_overrides_a_no_known_marriages_flag(self):
        # SPEC A.2: a later contradicting claim supersedes the convenience flag.
        self.a.person(DAD, 'Dad', no_known_marriages=True)
        self.a.source('census', [{'type': 'relationship', 'persons': [DAD, MOM],
                                  'roles': {'spouse': [DAD, MOM]}}])
        c = self.a.check(DAD, 'marriage')
        self.assertEqual((c['state'], c['partners']), ('missing', [MOM.lower()]))
        self.assertIn('remove the flag', c['detail'])

    def test_no_known_marriages_flag_counts_as_complete(self):
        self.a.person(OTHER, 'Other', no_known_marriages=True)
        self.assertEqual(self.a.check(OTHER, 'marriage')['state'], 'met')


class EndOfLifeCheckTests(_Base):
    def test_born_over_130_years_ago_without_death_is_missing(self):
        self.a.source('vital-record', [_birth(KID, '1880')])
        self.assertEqual(self.a.check(KID, 'end-of-life')['state'], 'missing')

    def test_born_under_130_years_ago_is_not_asked(self):
        self.a.source('vital-record', [_birth(KID, '1920')])
        self.assertEqual(self.a.check(KID, 'end-of-life')['state'], 'na')

    def test_an_uncertain_birth_uses_its_latest_year(self):
        # 189X could be 1899: under 130 in 2026, so not asked.
        self.a.source('vital-record', [_birth(KID, '189X')])
        self.assertEqual(self.a.check(KID, 'end-of-life')['state'], 'na')

    def test_an_uncertain_provisional_birth_uses_its_latest_year(self):
        self.a.person(KID, 'Kid', birth='1890/1910')
        self.assertEqual(self.a.check(KID, 'end-of-life')['state'], 'na')

    def test_a_provisional_birth_beats_an_adult_baptism_for_the_death_check(self):
        self.a.person(KID, 'Kid', birth='1870')
        self.a.source('vital-record', [{'type': 'baptism', 'persons': [KID], 'date': '1900'}])
        self.assertEqual(self.a.check(KID, 'end-of-life')['state'], 'missing')

    def test_born_before_1880_keeps_its_known_end(self):
        self.a.source('vital-record', [_birth(KID, '[..1880]')])
        self.assertEqual(self.a.check(KID, 'end-of-life')['state'], 'missing')

    def test_a_provisional_born_before_date_keeps_its_known_end(self):
        self.a.person(KID, 'Kid', birth='[..1880]')
        self.assertEqual(self.a.check(KID, 'end-of-life')['state'], 'missing')

    def test_burial_counts(self):
        self.a.source('vital-record', [_birth(KID, '1880')])
        self.a.source('other', [{'type': 'burial', 'persons': [KID], 'date': '1950'}])
        self.assertEqual(self.a.check(KID, 'end-of-life')['state'], 'met')

    def test_earliest_record_stands_in_for_an_unknown_birth(self):
        self.a.source('census', [{'type': 'residence', 'persons': [KID], 'date': '1850'}])
        self.assertEqual(self.a.check(KID, 'end-of-life')['state'], 'missing')

    def test_an_early_record_still_decides_beside_a_later_one(self):
        # Birth unknown: each record is a separate fact they were born by,
        # so the 1850 census decides even beside a 1950 record.
        self.a.source('census', [{'type': 'residence', 'persons': [KID], 'date': '1850'}])
        self.a.source('other', [{'type': 'residence', 'persons': [KID], 'date': '1950'}])
        self.assertEqual(self.a.check(KID, 'end-of-life')['state'], 'missing')

    def test_disagreeing_births_use_the_later_one(self):
        # Born 1890 or 1900: they may be 126, so not yet asked for a death.
        self.a.source('vital-record', [_birth(KID, '1890')])
        self.a.source('census', [_birth(KID, '1900')])
        self.assertEqual(self.a.check(KID, 'end-of-life')['state'], 'na')

    def test_disagreeing_births_both_past_130_years_still_ask(self):
        self.a.source('vital-record', [_birth(KID, '1880')])
        self.a.source('census', [_birth(KID, '1890')])
        self.assertEqual(self.a.check(KID, 'end-of-life')['state'], 'missing')

    def test_provisional_birth_estimate_is_read(self):
        self.a.person(KID, 'Kid', birth='1870~')
        self.assertEqual(self.a.check(KID, 'end-of-life')['state'], 'missing')

    def test_not_living_owes_a_death_whatever_their_age(self):
        # SPEC 8.2 / A.2: living: false means a death record is owed.
        self.a.person(KID, 'Kid', living='false')
        self.a.source('vital-record', [_birth(KID, '1950')])
        self.assertEqual(self.a.check(KID, 'end-of-life')['state'], 'missing')

    def test_marked_living_but_very_old_says_check_the_record(self):
        self.a.person(KID, 'Kid', living='true')
        self.a.source('vital-record', [_birth(KID, '1850')])
        c = self.a.check(KID, 'end-of-life')
        self.assertEqual(c['state'], 'missing')
        self.assertIn('marked living but born over 130 years ago', c['detail'])

    def test_marked_living_and_young_is_not_asked(self):
        self.a.person(KID, 'Kid', living='true')
        self.a.source('vital-record', [_birth(KID, '1950')])
        self.assertEqual(self.a.check(KID, 'end-of-life')['state'], 'na')

    def test_metrics_and_lint_w101_agree_on_a_not_living_person(self):
        self.a.person(KID, 'Kid', living='false', tier='curated')
        self.a.source('vital-record', [_birth(KID, '1950')])
        eol = self.a.check(KID, 'end-of-life')
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            findings, _ = lint._run_lint_core(self.a.root, load_fha_yaml(self.a.root))
        w101 = [f for f in findings if f.code == 'W101' and KID.lower() in f.message]
        self.assertTrue(w101 and 'death' in w101[0].message, findings)
        self.assertEqual(eol['state'], 'missing')


class ScoreTests(_Base):
    def test_not_applicable_checks_leave_the_score(self):
        self.a.source('vital-record', [_birth(KID, '1950', US)])
        ctx = self.a.build()
        r = person_completeness(ctx, KID, mode='accepted', this_year=YEAR)
        # Birth met; census (born 1950, death unknown) asks 1950; parents and
        # marriage missing; end of life not asked.
        self.assertEqual(r['applicable'], 4)
        self.assertTrue(completeness_summary_line(r).startswith(f"{r['met']} of 4:"))

    def test_unknown_mode_refused(self):
        ctx = self.a.build()
        with self.assertRaises(ValueError):
            person_completeness(ctx, KID, mode='everything')


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.a = _Archive(root_person=KID)
        self.a.person(KID, 'Kid')
        self.a.person(DAD, 'Dad', sex='M')
        self.a.person(MOM, 'Mom', sex='F')
        self.a.source('vital-record', [
            {'type': 'birth', 'persons': [KID, DAD, MOM], 'date': '1900', 'place': US,
             'roles': {'child': KID, 'parent': [DAD, MOM]}}],
            files=[{'file': 'documents/vital-record/b.txt'}])
        self.a.source('census', [{'type': 'census', 'persons': [DAD], 'date': '1880'}],
                      files=[{'file': 'documents/census/c.jpg'}])
        self.a.build()

    def tearDown(self):
        self.a.cleanup()

    def _run(self, **kw):
        return metrics.run_metrics(self.a.root, load_fha_yaml(self.a.root), this_year=YEAR, **kw)

    def test_direct_line_by_generation(self):
        r = self._run()
        self.assertEqual(r.exit_code, EXIT_CLEAN)
        self.assertEqual(r.data['scope'], 'direct-line')
        self.assertEqual([p['position'] for p in r.data['people']], [1, 2, 3])
        gens = {g['generation']: g for g in r.data['generations']}
        self.assertEqual(gens[0]['checks']['parents'], [1, 1])
        self.assertEqual(gens[1]['people'], 2)

    def test_archive_totals_and_text_coverage(self):
        a = self._run().data['archive']
        self.assertEqual(a['people']['direct_line'], 3)
        self.assertEqual(a['sources_by_type'], {'census': 1, 'vital-record': 1})
        self.assertEqual(a['searchable_text'], {'sources_with_files': 2, 'sources_with_text': 1})

    def test_a_source_naming_someone_in_people_counts_without_claims(self):
        # A photo filed about Kid, nothing extracted yet: still one of his sources.
        folder = self.a.root / 'sources' / 'photos'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'photo_S-0000000099.md').write_text(
            f'---\nid: S-0000000099\ntitle: Kid at school\nsource_type: photo\n'
            f'people: ["[[{KID}]]"]\n---\n', encoding='utf-8')
        self.a.build()
        thin = {t['person']: t['sources'] for t in
                self._run().data['archive']['thinnest_direct_line']}
        self.assertEqual(thin[KID.lower()], 2)   # the birth record and the photo

    def test_one_person(self):
        r = self._run(person=KID.lower())
        self.assertEqual(r.data['person']['person'], KID.lower())
        self.assertEqual(r.data['person']['position'], 1)

    def test_a_person_with_all_is_refused_with_exit_2(self):
        r = self._run(person=KID.lower(), everyone=True)
        self.assertEqual(r.exit_code, EXIT_ERRORS)
        self.assertIn('--all', r.messages[0].text)

    def test_include_suggested_flag_selects_the_mode(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = metrics._standalone_main(['--include-suggested', '--json',
                                             '--root', str(self.a.root)])
        self.assertEqual(code, EXIT_CLEAN)
        self.assertEqual(json.loads(out.getvalue())['data']['mode'], 'include-suggested')

    def test_bad_id_is_refused_with_exit_2(self):
        self.assertEqual(self._run(person='p-zz').exit_code, EXIT_ERRORS)

    def test_another_kind_of_id_is_refused_with_exit_2(self):
        r = self._run(person='S-0000000001')
        self.assertEqual(r.exit_code, EXIT_ERRORS)
        self.assertIn('not a person ID', r.messages[0].text)

    def test_a_full_name_resolves_to_the_one_person_carrying_it(self):
        r = self._run(person='dad')
        self.assertEqual(r.exit_code, EXIT_CLEAN)
        self.assertEqual(r.data['person']['person'], DAD.lower())

    def test_a_name_nobody_carries_points_to_find_with_exit_2(self):
        r = self._run(person='Nobody Here')
        self.assertEqual(r.exit_code, EXIT_ERRORS)
        self.assertIn('fha find "Nobody Here"', r.messages[0].text)

    def test_a_shared_name_is_refused_listing_both_with_exit_2(self):
        self.a.person(OTHER, 'Dad', sex='M')
        self.a.build()
        r = self._run(person='Dad')
        self.assertEqual(r.exit_code, EXIT_ERRORS)
        self.assertEqual(r.data['error'], 'ambiguous-name')
        self.assertEqual(sorted(r.data['candidates']), sorted([DAD.lower(), OTHER.lower()]))
        self.assertIn(fmt_id_display(OTHER.lower()), r.messages[0].text)

    def test_unknown_person_is_not_found_exit_1(self):
        self.assertEqual(self._run(person='p-0000000000').exit_code, EXIT_WARNINGS)

    def test_a_merged_root_person_is_read_through_the_merge(self):
        # root_person (KID) is merged into OTHER; OTHER inherits KID's parents.
        self.a.person(OTHER, 'Kid Survivor', tier='curated')
        self.a.person(KID, 'Kid', status='merged', merged_into=OTHER)
        self.a.build()
        r = self._run()
        self.assertEqual(r.data['scope'], 'direct-line')
        self.assertEqual([p['person'] for p in r.data['people']][:1], [OTHER.lower()])
        self.assertEqual(len(r.data['people']), 3)

    def test_a_root_merged_into_a_missing_record_names_both_ids(self):
        self.a.person(KID, 'Kid', status='merged', merged_into='P-9999999999')
        self.a.build()
        note = self._run().data['direct_line_note']
        self.assertIn(KID.lower(), note)
        self.assertIn('p-9999999999', note)

    def test_a_merge_loop_at_the_root_is_explained(self):
        self.a.person(KID, 'Kid', status='merged', merged_into=OTHER)
        self.a.person(OTHER, 'Other', status='merged', merged_into=KID)
        self.a.build()
        self.assertIn('merge loop', self._run().data['direct_line_note'])

    def test_a_merged_person_names_the_survivor(self):
        self.a.person(OTHER, 'Kid Dup', status='merged', merged_into=KID)
        self.a.build()
        r = self._run(person=OTHER.lower())
        self.assertEqual(r.exit_code, EXIT_WARNINGS)
        self.assertIn(f'fha metrics {KID.lower()}', r.messages[0].text)

    def test_missing_index_exit_3(self):
        (self.a.root / '.cache' / 'index.sqlite').unlink()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(self._run().exit_code, EXIT_FAILURE)

    def test_no_root_person_lists_curated_people_and_says_why(self):
        (self.a.root / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
        r = self._run()
        self.assertEqual(r.data['scope'], 'curated')
        self.assertIn('root_person', r.data['direct_line_note'])

    def test_json_output_is_only_json(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = metrics._cmd_metrics(self._run(), use_json=True)
        self.assertEqual(code, EXIT_CLEAN)
        self.assertEqual(json.loads(out.getvalue())['data']['mode'], 'accepted')

    def test_text_output_renders(self):
        out = io.StringIO()
        with redirect_stdout(out):
            metrics._cmd_metrics(self._run())
        self.assertIn('Direct line by generation', out.getvalue())

    def test_writes_nothing(self):
        before = sorted(p for p in self.a.root.rglob('*') if '.cache' not in p.parts)
        stamps = {p: p.stat().st_mtime_ns for p in before if p.is_file()}
        self._run()
        after = sorted(p for p in self.a.root.rglob('*') if '.cache' not in p.parts)
        self.assertEqual(before, after)
        self.assertEqual(stamps, {p: p.stat().st_mtime_ns for p in after if p.is_file()})


if __name__ == '__main__':
    unittest.main()
