"""
test_metrics.py - fha metrics: person completeness checks and archive totals
(TOOLING §7a, issue #343).

Each test builds a tiny temp archive, indexes it, and asks the shared engine
(`_lib.person_completeness`) or the command (`metrics.run_metrics`) one
question. Expected answers come from the rules TOOLING §7a states - a birth
record is a vital-record source; census years run from birth or arrival to
death; a marriage record is a vital-record marriage claim; a death is asked
for only past 130 years - never from the code's current output. Temp fixtures
throughout; never the real archive or example-archive.
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
import metrics
from _lib import (
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    completeness_context,
    completeness_summary_line,
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
        meta = {'id': pid, 'name': name, 'living': 'false', 'tier': 'stub', **fields}
        body = '---\n' + yaml.safe_dump(meta, sort_keys=False) + f'---\n\n# {name}\n'
        (self.root / 'people' / 'stubs' / f'p__{pid[2:].lower()}_{pid}.md').write_text(
            body, encoding='utf-8')

    def source(self, source_type: str, claims: list[dict], *, files=None) -> str:
        self._n += 1
        sid = f'S-{self._n:010d}'
        folder = self.root / 'sources' / source_type
        folder.mkdir(parents=True, exist_ok=True)
        meta = {'id': sid, 'title': f'{source_type} {self._n}', 'source_type': source_type}
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

    def test_information_primary_overrides_the_death_record_rule(self):
        self.a.source('vital-record', [
            {'type': 'death', 'persons': [KID], 'date': '1940-02-01'},
            _birth(KID, '1872', information='primary')])
        self.assertEqual(self.a.check(KID, 'birth')['state'], 'met')

    def test_suggested_birth_counts_only_in_found_mode(self):
        self.a.source('vital-record', [_birth(KID, '1880', status='suggested')])
        self.assertEqual(self.a.check(KID, 'birth', 'accepted')['state'], 'missing')
        self.assertEqual(self.a.check(KID, 'birth', 'found')['state'], 'met')

    def test_rejected_and_negated_claims_never_count(self):
        self.a.source('vital-record', [
            _birth(KID, '1880', status='rejected', reviewed='2026-01-01'),
            _birth(KID, '1881', negated=True, evidence='negative')])
        self.assertEqual(self.a.check(KID, 'birth', 'found')['state'], 'missing')


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

    def test_no_census_year_asked_before_immigration(self):
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

    def test_country_with_no_table_does_not_apply(self):
        self.a.source('vital-record', [_birth(KID, '1872', PL),
                                       {'type': 'death', 'persons': [KID], 'date': '1940',
                                        'place': PL}])
        self.assertEqual(self.a.check(KID, 'census')['state'], 'na')

    def test_no_years_asked_after_moving_to_another_country(self):
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
        self.assertEqual((c['state'], c['missing']), ('met', []))

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

    def test_suggested_parent_counts_only_in_found_mode(self):
        self.a.source('census', [self._rel(KID, [DAD, MOM], status='suggested')])
        self.assertEqual(self.a.check(KID, 'parents', 'accepted')['state'], 'missing')
        self.assertEqual(self.a.check(KID, 'parents', 'found')['state'], 'met')


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
        self.a.source('vital-record', [
            {'type': 'death', 'persons': [DAD], 'date': '1920-01-01'},
            self._marriage(DAD, MOM)])
        self.assertEqual(self.a.check(DAD, 'marriage')['state'], 'missing')

    def test_a_child_who_died_young_is_not_asked_for_a_marriage(self):
        self.a.source('vital-record', [_birth(KID, '1880-03-01'),
                                       {'type': 'death', 'persons': [KID], 'date': '1882-05-01'}])
        c = self.a.check(KID, 'marriage')
        self.assertEqual(c['state'], 'na')
        self.assertIn('died young', c['detail'])

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

    def test_burial_counts(self):
        self.a.source('vital-record', [_birth(KID, '1880')])
        self.a.source('other', [{'type': 'burial', 'persons': [KID], 'date': '1950'}])
        self.assertEqual(self.a.check(KID, 'end-of-life')['state'], 'met')

    def test_earliest_record_stands_in_for_an_unknown_birth(self):
        self.a.source('census', [{'type': 'residence', 'persons': [KID], 'date': '1850'}])
        self.assertEqual(self.a.check(KID, 'end-of-life')['state'], 'missing')

    def test_provisional_birth_estimate_is_read(self):
        self.a.person(KID, 'Kid', birth='1870~')
        self.assertEqual(self.a.check(KID, 'end-of-life')['state'], 'missing')


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

    def test_one_person(self):
        r = self._run(person=KID.lower())
        self.assertEqual(r.data['person']['person'], KID.lower())
        self.assertEqual(r.data['person']['position'], 1)

    def test_bad_id_is_refused_with_exit_2(self):
        self.assertEqual(self._run(person='p-zz').exit_code, EXIT_ERRORS)

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
