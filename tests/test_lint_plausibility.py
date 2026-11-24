"""
test_lint_plausibility.py - `fha lint`'s "dates that cannot all be true" checks.

When a GEDCOM import attaches the wrong record, or two people of one name are
merged, the archive can end up saying someone died before they were born.
Lint now says so in plain words, naming both claims and the files they sit
in. A warning against a correct record would teach the owner to ignore lint,
so every test here that expects silence matters as much as the ones that
expect a warning.

Each test builds a tiny archive in a temporary folder and runs lint's checks
over it (`_run_lint_core`), the pattern DiedButLivingW136Tests in
tests/test_lint.py uses. The base class is shared by every check in this
family (W163 onward); later checks add their own test classes here.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import lint
from _lib import load_fha_yaml

PLAUSIBILITY_CODES = ('W163', 'W164', 'W165', 'W173')


class _PlausibilityBase(unittest.TestCase):
    ADA, BEN, CARA, DAN = 'P-aaaaaaaaaa', 'P-bbbbbbbbbb', 'P-cccccccccc', 'P-dddddddddd'

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'people').mkdir()
        (self.root / 'sources' / 'notes').mkdir(parents=True)
        (self.root / 'fha.yaml').write_text('roots:\n  documents: documents\n',
                                            encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _person(self, pid: str, name: str, *, sex: str | None = 'F',
                living: str = 'false', extra: str = '') -> None:
        first, last = name.lower().split()
        sex_line = f'sex: {sex}\n' if sex is not None else ''
        (self.root / 'people' / f'{last}__{first}_{pid}.md').write_text(
            f'---\nid: {pid}\nname: {name}\n{sex_line}living: {living}\n'
            f'tier: stub\n{extra}---\n\n# {name}\n', encoding='utf-8')

    def _source(self, sid: str, claims_yaml: str) -> Path:
        path = self.root / 'sources' / 'notes' / f'rec_{sid.lower()}.md'
        path.write_text(
            f'---\nid: {sid}\ntitle: Rec\nsource_type: note\n---\n\n'
            f'## Claims\n```yaml\n{claims_yaml}```\n', encoding='utf-8')
        return path

    def _claim(self, cid: str, ctype: str, persons: list[str], *, date: str | None = None,
               roles: str = '', status: str = 'accepted', subtype: str | None = None,
               extra: str = '') -> str:
        lines = [f'- id: {cid}', f'  type: {ctype}', f'  persons: [{", ".join(persons)}]']
        if roles:
            lines.append(f'  roles: {roles}')
        if subtype is not None:
            lines.append(f'  subtype: {subtype}')
        lines.append(f'  value: "a {ctype}"')
        lines.append(f'  status: {status}')
        if status == 'accepted':
            lines.append('  reviewed: 2026-01-01')
        lines.append('  confidence: high')
        if date is not None:
            lines.append(f'  date: "{date}"')
        text = '\n'.join(lines) + '\n'
        return text + extra

    def _codes(self, code: str) -> list:
        findings, _reg = lint._run_lint_core(self.root, load_fha_yaml(self.root))
        return [f for f in findings if f.code == code]


class OnePersonDatesTests(_PlausibilityBase):
    S1, S2, S3 = 'S-1111111111', 'S-2222222222', 'S-3333333333'

    def _birth_and_death(self, born: str, died: str, pid: str | None = None) -> None:
        pid = pid or self.ADA
        self._source(self.S1, self._claim('C-1111111111', 'birth', [pid], date=born))
        self._source(self.S2, self._claim('C-2222222222', 'death', [pid], date=died))

    def test_death_before_birth_names_both_claims_and_files(self) -> None:
        self._person(self.ADA, 'Ada Hartley')
        self._birth_and_death('1871', '1850')
        found = self._codes('W163')
        self.assertEqual(len(found), 1)
        message = found[0].message
        self.assertTrue(message.startswith('Ada Hartley (p-aaaaaaaaaa) is recorded as dying 1850'),
                        message)
        self.assertIn('c-1111111111 (sources/notes/rec_s-1111111111.md)', message)
        self.assertIn('c-2222222222 (sources/notes/rec_s-2222222222.md)', message)
        self.assertIn('Both cannot be true', message)
        self.assertIn('`fha claim <c-id> --status disputed`', message)
        self.assertEqual(Path(found[0].path).name, 'hartley__ada_P-aaaaaaaaaa.md')

    def test_same_year_birth_and_death_is_silent(self) -> None:
        self._person(self.ADA, 'Ada Hartley')
        self._birth_and_death('1871', '1871')
        self.assertEqual(self._codes('W163'), [])

    def test_approximate_dates_overlap_and_stay_silent(self) -> None:
        self._person(self.ADA, 'Ada Hartley')
        self._birth_and_death('1850~', '1849')
        self.assertEqual(self._codes('W163'), [])

    def test_decade_and_open_dates_stay_silent(self) -> None:
        for born, died in (('185X', '1850'), ('[..1890]', '1850'), ('1880?', '1879')):
            with self.subTest(born=born, died=died):
                self._person(self.ADA, 'Ada Hartley')
                self._birth_and_death(born, died)
                self.assertEqual(self._codes('W163'), [])

    def test_two_birth_readings_widen_the_range(self) -> None:
        self._person(self.ADA, 'Ada Hartley')
        self._source(self.S1, self._claim('C-1111111111', 'birth', [self.ADA], date='1850')
                     + self._claim('C-1111111112', 'birth', [self.ADA], date='1870'))
        self._source(self.S2, self._claim('C-2222222222', 'death', [self.ADA], date='1860'))
        self.assertEqual(self._codes('W163'), [])

    def test_burial_before_death(self) -> None:
        self._person(self.ADA, 'Ada Hartley')
        self._source(self.S1, self._claim('C-1111111111', 'death', [self.ADA], date='1880-05-02'))
        self._source(self.S2, self._claim('C-2222222222', 'burial', [self.ADA], date='1880-04-30'))
        found = self._codes('W163')
        self.assertEqual(len(found), 1)
        self.assertIn('is recorded as buried 30 April 1880', found[0].message)
        self.assertIn('a burial is usually within days of the death', found[0].message)

    def test_baptism_before_birth(self) -> None:
        self._person(self.ADA, 'Ada Hartley')
        self._source(self.S1, self._claim('C-1111111111', 'birth', [self.ADA], date='1871-03'))
        self._source(self.S2, self._claim('C-2222222222', 'baptism', [self.ADA], date='1870'))
        found = self._codes('W163')
        self.assertEqual(len(found), 1)
        self.assertIn('is recorded as baptized 1870', found[0].message)

    def test_baptism_on_the_day_of_birth_is_silent(self) -> None:
        self._person(self.ADA, 'Ada Hartley')
        self._source(self.S1, self._claim('C-1111111111', 'birth', [self.ADA], date='1871-03-04'))
        self._source(self.S2, self._claim('C-2222222222', 'baptism', [self.ADA],
                                          date='1871-03-04'))
        self.assertEqual(self._codes('W163'), [])

    def test_burial_before_birth_when_no_death_is_recorded(self) -> None:
        self._person(self.ADA, 'Ada Hartley')
        self._source(self.S1, self._claim('C-1111111111', 'birth', [self.ADA], date='1871'))
        self._source(self.S2, self._claim('C-2222222222', 'burial', [self.ADA], date='1860'))
        found = self._codes('W163')
        self.assertEqual(len(found), 1)
        self.assertIn('is recorded as buried 1860', found[0].message)

    def test_a_mothers_birth_is_not_read_off_her_sons_record(self) -> None:
        self._person(self.ADA, 'Ada Hartley')
        self._person(self.BEN, 'Ben Hartley', sex='M')
        self._source(self.S1, self._claim(
            'C-1111111111', 'birth', [self.BEN, self.ADA], date='1900',
            roles=f'{{child: [{self.BEN}], parent: [{self.ADA}]}}'))
        self._source(self.S2, self._claim('C-2222222222', 'death', [self.ADA], date='1880'))
        self.assertEqual(self._codes('W163'), [])

    def test_suggested_and_negated_claims_are_ignored(self) -> None:
        self._person(self.ADA, 'Ada Hartley')
        self._source(self.S1, self._claim('C-1111111111', 'birth', [self.ADA], date='1871'))
        self._source(self.S2, self._claim('C-2222222222', 'death', [self.ADA], date='1850',
                                          status='suggested')
                     + self._claim('C-2222222223', 'death', [self.ADA], date='1851',
                                   extra='  negated: true\n'))
        self.assertEqual(self._codes('W163'), [])

    def test_long_life_needs_certainty(self) -> None:
        for born, died, fires in (('1800', '1921', True), ('1800', '1920', False),
                                  ('180X', '1925', False)):
            with self.subTest(born=born, died=died):
                self._person(self.ADA, 'Ada Hartley')
                self._birth_and_death(born, died)
                found = self._codes('W164')
                self.assertEqual(len(found), 1 if fires else 0, [f.message for f in found])
                if fires:
                    self.assertIn('would have lived at least 120 years', found[0].message)
                    self.assertIn('c-1111111111', found[0].message)
                    self.assertIn('c-2222222222', found[0].message)

    def test_residence_after_death_fires_and_names_the_type(self) -> None:
        self._person(self.ADA, 'Ada Hartley')
        self._source(self.S1, self._claim('C-1111111111', 'death', [self.ADA], date='1880'))
        self._source(self.S2, self._claim('C-2222222222', 'residence', [self.ADA], date='1885'))
        found = self._codes('W165')
        self.assertEqual(len(found), 1)
        self.assertIn('Ada Hartley (p-aaaaaaaaaa) died 1880', found[0].message)
        self.assertIn('but a residence record dates 1885', found[0].message)
        self.assertIn('c-2222222222 (sources/notes/rec_s-2222222222.md)', found[0].message)

    def test_an_education_claim_reads_as_a_school_record(self) -> None:
        self._person(self.ADA, 'Ada Hartley')
        self._source(self.S1, self._claim('C-1111111111', 'death', [self.ADA], date='1880'))
        self._source(self.S2, self._claim('C-2222222222', 'education', [self.ADA], date='1885'))
        (found,) = self._codes('W165')
        self.assertIn('but a school record dates 1885', found.message)

    def test_pension_and_probate_style_records_are_not_checked(self) -> None:
        self._person(self.ADA, 'Ada Hartley')
        self._source(self.S1, self._claim('C-1111111111', 'death', [self.ADA], date='1880'))
        self._source(self.S2, self._claim('C-2222222222', 'military', [self.ADA], date='1885')
                     + self._claim('C-2222222223', 'event', [self.ADA], date='1886',
                                   subtype='probate'))
        self.assertEqual(self._codes('W165'), [])

    def test_mortality_census_is_exempt(self) -> None:
        self._person(self.ADA, 'Ada Hartley')
        self._source(self.S1, self._claim('C-1111111111', 'death', [self.ADA], date='1849-11'))
        self._source(self.S2, self._claim('C-2222222222', 'census', [self.ADA], date='1850-06-01',
                                          subtype='mortality schedule'))
        self.assertEqual(self._codes('W165'), [])

    def test_mortality_census_is_exempt_however_the_subtype_is_spelled(self) -> None:
        for subtype in ('mortality_schedule', 'Mortality-Schedule', 'US mortality'):
            with self.subTest(subtype=subtype):
                self._person(self.ADA, 'Ada Hartley')
                self._source(self.S1, self._claim('C-1111111111', 'death', [self.ADA],
                                                  date='1849-11'))
                self._source(self.S2, self._claim('C-2222222222', 'census', [self.ADA],
                                                  date='1850-06-01', subtype=subtype))
                self.assertEqual(self._codes('W165'), [])

    def test_an_approximate_birth_day_does_not_fire_against_a_day_earlier_death(self) -> None:
        # An infant born "about 20 May 1850" who died 19 May 1850: both can
        # be true, so lint must not say they cannot.
        self._person(self.ADA, 'Ada Hartley')
        self._birth_and_death('1850-05-~20', '1850-05-19')
        self.assertEqual(self._codes('W163'), [])

    def test_an_approximate_day_still_fires_when_a_year_apart(self) -> None:
        self._person(self.ADA, 'Ada Hartley')
        self._birth_and_death('1850-05-~20', '1848-05-19')
        self.assertEqual(len(self._codes('W163')), 1)

    def test_a_parent_named_on_a_later_record_is_not_its_subject(self) -> None:
        self._person(self.ADA, 'Ada Hartley', sex='M')
        self._person(self.BEN, 'Ben Hartley', sex='M')
        self._source(self.S1, self._claim('C-1111111111', 'death', [self.ADA], date='1880'))
        self._source(self.S2, self._claim(
            'C-2222222222', 'residence', [self.BEN, self.ADA], date='1885',
            roles=f'{{subject: [{self.BEN}], parent: [{self.ADA}]}}'))
        self.assertEqual(self._codes('W165'), [])

    def test_a_head_of_household_is_its_subject(self) -> None:
        self._person(self.ADA, 'Ada Hartley')
        self._person(self.BEN, 'Ben Hartley', sex='M')
        self._source(self.S1, self._claim('C-1111111111', 'death', [self.ADA], date='1880'))
        self._source(self.S2, self._claim(
            'C-2222222222', 'census', [self.ADA, self.BEN], date='1890',
            roles=f'{{head: [{self.ADA}], household_member: [{self.BEN}]}}'))
        self.assertEqual(len(self._codes('W165')), 1)

    def test_merged_record_counts_for_the_survivor(self) -> None:
        self._person(self.ADA, 'Ada Hartley')
        self._person(self.BEN, 'Ada Hartlee',
                     extra=f'status: merged\nmerged_into: {self.ADA}\n')
        self._source(self.S1, self._claim('C-1111111111', 'birth', [self.ADA], date='1871'))
        self._source(self.S2, self._claim('C-2222222222', 'death', [self.BEN], date='1850'))
        found = self._codes('W163')
        self.assertEqual(len(found), 1)
        self.assertEqual(Path(found[0].path).name, 'hartley__ada_P-aaaaaaaaaa.md')

    def test_shipped_archives_carry_no_impossible_dates(self) -> None:
        for name in ('example-archive', 'quickstart-example', 'archive-template',
                     'quickstart-template'):
            with self.subTest(archive=name):
                root = ROOT / name
                findings, _reg = lint._run_lint_core(root, load_fha_yaml(root))
                self.assertEqual([f.message for f in findings
                                  if f.code in PLAUSIBILITY_CODES], [])



class ProbablyNotLivingTests(_PlausibilityBase):
    """W173: someone marked `living: unknown` (so the family site shows
    "Living Person" in their place) whose own or relatives' records say they
    would be over 110 today. Lint names them and the one command he runs;
    nothing flips the flag for him. The year is pinned to 2026."""

    S1, S2, S3, S4 = 'S-1111111111', 'S-2222222222', 'S-3333333333', 'S-4444444444'

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(lint, '_this_year', return_value=2026)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _parent_of(self, sid: str, cid: str, parent: str, child: str, *,
                   subtype: str | None = None) -> None:
        self._source(sid, self._claim(cid, 'relationship', [parent, child],
                                      roles=f'{{child: [{child}], parent: [{parent}]}}',
                                      subtype=subtype))

    def _born(self, sid: str, cid: str, pid: str, date: str) -> None:
        self._source(sid, self._claim(cid, 'birth', [pid], date=date))

    def _father_and_son(self, son_born: str, *, subtype: str | None = None) -> None:
        self._person(self.ADA, 'Thomas Hartley', sex='M', living='unknown')
        self._person(self.BEN, 'Ben Hartley', sex='M')
        self._parent_of(self.S1, 'C-1111111111', self.ADA, self.BEN, subtype=subtype)
        self._born(self.S2, 'C-2222222222', self.BEN, son_born)

    def _w173(self) -> list:
        return self._codes('W173')

    def test_father_of_a_son_born_1871_is_suggested_and_names_the_command(self) -> None:
        self._father_and_son('1871')
        found = self._w173()
        self.assertEqual(len(found), 1)
        message = found[0].message
        self.assertTrue(message.startswith(
            'Thomas Hartley (p-aaaaaaaaaa) is marked living: unknown, so the family site '
            'and shared exports treat him as living and show "Living Person" in his place.'),
            message)
        self.assertIn('His son Ben Hartley (p-bbbbbbbbbb) was born by 1871', message)
        self.assertIn('c-2222222222 (sources/notes/rec_s-2222222222.md)', message)
        self.assertIn('`fha person set-living p-aaaaaaaaaa false`', message)
        self.assertIn('`fha person set-living p-aaaaaaaaaa true`', message)
        self.assertEqual(Path(found[0].path).name, 'hartley__thomas_P-aaaaaaaaaa.md')

    def test_son_born_1920_suggests_father_born_by_1908(self) -> None:
        self._father_and_son('1920')
        found = self._w173()
        self.assertEqual(len(found), 1)
        self.assertIn('so he was born by 1908 and would be at least 117 today', found[0].message)

    def test_child_born_1925_fires_and_1930_is_silent(self) -> None:
        for year, fires in (('1925', True), ('1930', False)):
            with self.subTest(year=year):
                self._father_and_son(year)
                self.assertEqual(len(self._w173()), 1 if fires else 0)

    def test_grandchild_chain_is_followed_and_explained(self) -> None:
        # Thomas -> Ben (undated) -> Cara, born 1900: Ben born by 1888,
        # Thomas by 1876. Ben is marked false, so only Thomas is asked about.
        self._person(self.ADA, 'Thomas Hartley', sex='M', living='unknown')
        self._person(self.BEN, 'Ben Hartley', sex='M')
        self._person(self.CARA, 'Cara Hartley', sex='F')
        self._parent_of(self.S1, 'C-1111111111', self.ADA, self.BEN)
        self._parent_of(self.S2, 'C-2222222222', self.BEN, self.CARA)
        self._born(self.S3, 'C-3333333333', self.CARA, '1900')
        found = self._w173()
        self.assertEqual(len(found), 1, [f.message for f in found])
        message = found[0].message
        self.assertIn('His son Ben Hartley (p-bbbbbbbbbb) was born by 1888, going by his '
                      'daughter Cara Hartley (p-cccccccccc), born by 1900', message)
        self.assertIn('c-3333333333', message)
        self.assertIn('Nobody is a parent before 12, so he was born by 1876', message)

    def test_own_census_record_counts(self) -> None:
        self._person(self.ADA, 'Ada Hartley', living='unknown')
        self._source(self.S1, self._claim('C-1111111111', 'census', [self.ADA], date='1880'))
        found = self._w173()
        self.assertEqual(len(found), 1)
        self.assertIn('A census record names her in 1880', found[0].message)

    def test_two_disagreeing_birth_claims_naming_the_parent_read_as_one_range(self) -> None:
        # Ben's accepted births say 1900 and 1930. Merged, he is born by 1930
        # and she by 1918 (108 years): not worth asking. Read separately her
        # 1900 record would put her born by 1900 and warn on a correct tree.
        self._person(self.ADA, 'Ada Hartley', sex='F', living='unknown')
        self._person(self.BEN, 'Ben Hartley', sex='M')
        for sid, cid, date in ((self.S1, 'C-1111111111', '1900'),
                               (self.S2, 'C-2222222222', '1930')):
            self._source(sid, self._claim(
                cid, 'birth', [self.BEN, self.ADA], date=date,
                roles=f'{{child: [{self.BEN}], parent: [{self.ADA}]}}'))
        self.assertEqual(self._w173(), [])

    def test_a_note_claim_does_not_count(self) -> None:
        self._person(self.ADA, 'Ada Hartley', living='unknown')
        self._source(self.S1, self._claim('C-1111111111', 'note', [self.ADA], date='1880'))
        self.assertEqual(self._w173(), [])

    def test_mother_died_1890_suggests_her_undated_child(self) -> None:
        self._person(self.ADA, 'Ada Hartley', living='false')
        self._person(self.BEN, 'Ben Hartley', sex='M', living='unknown')
        self._parent_of(self.S1, 'C-1111111111', self.ADA, self.BEN)
        self._source(self.S2, self._claim('C-2222222222', 'death', [self.ADA], date='1890'))
        found = self._w173()
        self.assertEqual(len(found), 1)
        self.assertIn('His mother Ada Hartley (p-aaaaaaaaaa) died 1890', found[0].message)
        self.assertIn('c-2222222222', found[0].message)

    def test_provisional_birth_counts(self) -> None:
        self._person(self.ADA, 'Ada Hartley', living='unknown', extra='birth: 1840~\n')
        found = self._w173()
        self.assertEqual(len(found), 1)
        self.assertIn('Her record gives a birth estimate of about 1840', found[0].message)

    def test_provisional_death_fires(self) -> None:
        self._person(self.ADA, 'Ada Hartley', living='unknown', extra='death: 1990\n')
        found = self._w173()
        self.assertEqual(len(found), 1)
        self.assertIn('Her record gives a death estimate (1990)', found[0].message)

    def test_living_true_and_false_are_never_questioned(self) -> None:
        for living in ('true', 'false'):
            with self.subTest(living=living):
                self._person(self.ADA, 'Ada Hartley', living=living, extra='death: 1990\n')
                self._source(self.S1, self._claim('C-1111111111', 'census', [self.ADA],
                                                  date='1880'))
                self.assertEqual(self._w173(), [])

    def test_person_w136_names_is_not_repeated(self) -> None:
        self._person(self.ADA, 'Ada Hartley', living='unknown')
        self._source(self.S1, self._claim('C-1111111111', 'death', [self.ADA], date='1890')
                     + self._claim('C-1111111112', 'census', [self.ADA], date='1880'))
        self.assertEqual(len(self._codes('W136')), 1)
        self.assertEqual(self._w173(), [])

    def test_adoptive_child_does_not_age_the_parent(self) -> None:
        self._father_and_son('1871', subtype='adoptive')
        self.assertEqual(self._w173(), [])

    def test_suggested_claims_do_not_count(self) -> None:
        self._person(self.ADA, 'Ada Hartley', living='unknown')
        self._source(self.S1, self._claim('C-1111111111', 'census', [self.ADA], date='1880',
                                          status='suggested'))
        self.assertEqual(self._w173(), [])

    def test_recent_records_are_silent(self) -> None:
        # A 1950 census names her: she may well be alive at 76.
        self._person(self.ADA, 'Ada Hartley', living='unknown')
        self._source(self.S1, self._claim('C-1111111111', 'census', [self.ADA], date='1950'))
        self.assertEqual(self._w173(), [])

    def test_a_living_storyteller_is_not_aged_by_the_story(self) -> None:
        # He interviews his aunt Cara about her grandfather's 1890 death and
        # her grandfather's 1870 birth: the claims carry the event's date,
        # not the day she told it, so she is not "born by 1890".
        self._person(self.ADA, 'Will Hartley', sex='M', living='false')
        self._person(self.CARA, 'Cara Hartley', living='unknown')
        for role, ctype, date in (('narrator', 'death', '1890'),
                                  ('grandchild', 'birth', '1870'),
                                  ('informant', 'immigration', '1880')):
            with self.subTest(role=role):
                subject = {'death': 'deceased', 'birth': 'child'}.get(ctype, 'subject')
                self._source(self.S1, self._claim(
                    'C-1111111111', ctype, [self.ADA, self.CARA], date=date,
                    roles=f'{{{subject}: [{self.ADA}], {role}: [{self.CARA}]}}'))
                self.assertEqual(self._w173(), [])

    def test_a_witness_on_an_old_record_still_counts(self) -> None:
        self._person(self.ADA, 'Will Hartley', sex='M', living='false')
        self._person(self.CARA, 'Cara Hartley', living='unknown')
        self._source(self.S1, self._claim(
            'C-1111111111', 'marriage', [self.ADA, self.CARA], date='1890',
            roles=f'{{spouse: [{self.ADA}], witness: [{self.CARA}]}}'))
        self.assertEqual(len(self._w173()), 1)

    def test_a_childs_death_ages_the_undated_parent(self) -> None:
        # Ben's only record is his death in 1900, so he was born by 1900 and
        # his father Thomas by 1888.
        self._person(self.ADA, 'Thomas Hartley', sex='M', living='unknown')
        self._person(self.BEN, 'Ben Hartley', sex='M')
        self._parent_of(self.S1, 'C-1111111111', self.ADA, self.BEN)
        self._source(self.S2, self._claim('C-2222222222', 'death', [self.BEN], date='1900'))
        found = self._w173()
        self.assertEqual(len(found), 1)
        self.assertIn('His son Ben Hartley (p-bbbbbbbbbb) was born by 1900', found[0].message)
        self.assertIn('c-2222222222', found[0].message)

    def test_an_accepted_birth_is_not_outvoted_by_an_earlier_record(self) -> None:
        # Born 1950 by his birth record; a misfiled 1900 census names him.
        # Asking to mark a possibly living 76-year-old dead would put him on
        # the family site.
        self._person(self.ADA, 'Ben Hartley', sex='M', living='unknown')
        self._source(self.S1, self._claim('C-1111111111', 'birth', [self.ADA], date='1950'))
        self._source(self.S2, self._claim('C-2222222222', 'census', [self.ADA], date='1900'))
        self.assertEqual(self._w173(), [])

    def test_an_accepted_birth_is_not_outvoted_by_a_relatives_record(self) -> None:
        # Born 1950 by his birth record; a child misfiled as born 1900, or a
        # mother recorded as dying in 1890, is a conflict, not his age.
        for case in ('child', 'mother'):
            with self.subTest(case=case):
                self._person(self.BEN, 'Ben Hartley', sex='M', living='unknown')
                self._born(self.S1, 'C-1111111111', self.BEN, '1950')
                if case == 'child':
                    self._person(self.CARA, 'Cara Hartley')
                    self._parent_of(self.S2, 'C-2222222222', self.BEN, self.CARA)
                    self._born(self.S3, 'C-3333333333', self.CARA, '1900')
                else:
                    self._person(self.ADA, 'Ada Hartley')
                    self._parent_of(self.S2, 'C-2222222222', self.ADA, self.BEN)
                    self._source(self.S3, self._claim('C-3333333333', 'death', [self.ADA],
                                                      date='1890'))
                self.assertEqual(self._w173(), [])

    def test_a_provisional_death_that_is_not_a_date_is_silent(self) -> None:
        for value in ('unknown', '"?"', 'false'):
            with self.subTest(value=value):
                self._person(self.ADA, 'Ada Hartley', living='unknown',
                             extra=f'death: {value}\n')
                self.assertEqual(self._w173(), [])

    def test_lint_never_writes_living(self) -> None:
        self._father_and_son('1871')
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                lint._standalone_main(['--root', str(self.root)])
            except SystemExit:
                pass
        self.assertIn('W173', out.getvalue())
        after = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)


_GIT = shutil.which('git')


@unittest.skipUnless(_GIT, 'git is not installed')
class ChangedScopeTests(_PlausibilityBase):
    def _git(self, *args: str) -> None:
        subprocess.run([_GIT, '-c', 'user.name=Test', '-c', 'user.email=t@example.com',
                        '-c', 'commit.gpgsign=false', *args],
                       cwd=self.root, check=True, capture_output=True)

    def test_changed_scope_keeps_a_date_conflict(self) -> None:
        # Editing only the death record still shows the conflict: the source
        # names Ada, so her profile (where the warning sits) is in scope.
        self._person(self.ADA, 'Ada Hartley')
        self._source('S-1111111111', self._claim('C-1111111111', 'birth', [self.ADA],
                                                 date='1871'))
        death = self._source('S-2222222222', self._claim('C-2222222222', 'death',
                                                         [self.ADA], date='1871'))
        self._git('init', '-q')
        self._git('add', '-A')
        self._git('commit', '-q', '-m', 'start')
        death.write_text(death.read_text(encoding='utf-8').replace('"1871"', '"1850"'),
                         encoding='utf-8')
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            lint._standalone_main(['--root', str(self.root), '--changed'])
        w163 = [line for line in out.getvalue().splitlines() if line.startswith('W W163 ')]
        self.assertEqual(len(w163), 1, out.getvalue())


class LeadsToCheckTests(_PlausibilityBase):
    """Things worth a look that are not errors. They print after the findings
    and never move the exit code, because a correct record can carry each one."""

    def _margarets(self, women: int, men: int = 0, *, unknown: int = 0) -> None:
        n = 0
        for count, sex in ((women, 'F'), (men, 'M'), (unknown, 'unknown')):
            for _ in range(count):
                n += 1
                pid = f'P-{n:010d}'
                # Each gets her own surname so lint's same-name warning (W112)
                # stays out of the way of what these tests are about.
                surname = 'Hartley' + ''.join(chr(97 + int(d)) for d in str(n))
                self._person(pid, f'Margaret {surname}', sex=sex)

    def _unsexed(self, sex: str | None, name: str = 'Margaret Cole') -> str:
        pid = 'P-zzzzzzzzzz'
        self._person(pid, name, sex=sex)
        return pid

    def _leads(self) -> list[str]:
        result = lint.run_lint(self.root, load_fha_yaml(self.root))
        return result.data['leads']

    def test_sex_lead_fires_at_19_of_20_and_says_find_a_record(self) -> None:
        self._margarets(19, 1)
        pid = self._unsexed(None)
        leads = self._leads()
        self.assertEqual(len(leads), 1, leads)
        self.assertIn('19 of 20', leads[0])
        self.assertIn(f'fha person set-sex {pid.lower()} F', leads[0])
        # SPEC 9: sex is birth-assigned where a record states it, never
        # taken from name statistics, so the lead sends him to a record first.
        self.assertIn('record that states', leads[0])

    def test_sex_lead_never_prints_a_restricted_persons_name(self) -> None:
        # A restricted person's name stays off lint's informational lists; the
        # lead still helps him, by id, without the name or the given name.
        self._margarets(20)
        pid = 'P-zzzzzzzzzz'
        self._person(pid, 'Margaret Quillfeather', sex=None, extra='restricted: true\n')
        leads = self._leads()
        self.assertEqual(len(leads), 1, leads)
        self.assertNotIn('Quillfeather', leads[0])
        self.assertNotIn('Margaret', leads[0])
        self.assertIn(f'fha person set-sex {pid.lower()} F', leads[0])

    def test_sex_lead_silent_at_90_percent(self) -> None:
        self._margarets(18, 2)
        self._unsexed(None)
        self.assertEqual(self._leads(), [])

    def test_sex_lead_silent_below_five_people(self) -> None:
        self._margarets(4)
        self._unsexed(None)
        self.assertEqual(self._leads(), [])

    def test_sex_lead_silent_for_a_stated_unknown(self) -> None:
        self._margarets(20)
        self._unsexed('unknown')
        self.assertEqual(self._leads(), [])

    def test_sex_lead_fires_for_a_blank_sex(self) -> None:
        self._margarets(20)
        self._unsexed('')
        self.assertEqual(len(self._leads()), 1)

    def test_sex_lead_silent_for_a_sex_written_as_a_word(self) -> None:
        # He wrote `sex: male` (W141 asks him to spell it M); the lead must not
        # call that "no sex recorded" and steer him to the name's F.
        for written in ('male', 'Male', 'f', 'female'):
            with self.subTest(written=written):
                self._margarets(20)
                self._unsexed(written)
                self.assertEqual(self._leads(), [])

    def test_sex_lead_skips_titled_wife_names(self) -> None:
        self._margarets(20)
        self._unsexed(None, name='Mrs. Doe')
        self.assertEqual(self._leads(), [])

    def test_leads_are_left_out_with_changed(self) -> None:
        self._margarets(20)
        self._unsexed(None)
        subprocess.run([_GIT, 'init', '-q'], cwd=self.root, check=True, capture_output=True)
        subprocess.run([_GIT, '-c', 'user.name=Test', '-c', 'user.email=t@example.com',
                        '-c', 'commit.gpgsign=false', 'add', '-A'],
                       cwd=self.root, check=True, capture_output=True)
        subprocess.run([_GIT, '-c', 'user.name=Test', '-c', 'user.email=t@example.com',
                        '-c', 'commit.gpgsign=false', 'commit', '-q', '-m', 'start'],
                       cwd=self.root, check=True, capture_output=True)
        touched = next((self.root / 'people').glob('cole__*'))
        touched.write_text(touched.read_text(encoding='utf-8') + '\nA note.\n',
                           encoding='utf-8')
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            lint._standalone_main(['--root', str(self.root), '--changed'])
        self.assertNotIn('Leads to check', out.getvalue())

    def test_leads_never_move_the_exit_code(self) -> None:
        self._margarets(20)
        self._unsexed(None)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = lint._standalone_main(['--root', str(self.root)])
        self.assertIn('Leads to check', out.getvalue())
        result = lint.run_lint(self.root, load_fha_yaml(self.root))
        self.assertEqual(code, result.exit_code)
        self.assertEqual(code, lint.EXIT_CLEAN, out.getvalue())


if __name__ == '__main__':
    unittest.main()
