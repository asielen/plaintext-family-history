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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import lint
from _lib import load_fha_yaml

PLAUSIBILITY_CODES = ('W163', 'W164', 'W165', 'W166', 'W167', 'W168', 'W169', 'W170')


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


class FamilyDatesTests(_PlausibilityBase):
    """W166-W170: dates across a family that cannot all be true."""

    S1, S2, S3 = 'S-1111111111', 'S-2222222222', 'S-3333333333'

    def _tie(self, cid: str, parent: str, child: str, subtype: str | None = None) -> str:
        return self._claim(cid, 'relationship', [parent, child], subtype=subtype,
                           roles=f'{{parent: [{parent}], child: [{child}]}}')

    def test_born_after_mother_died_names_the_tie_claim(self) -> None:
        # The plan's sentence: Margaret born 1871, her mother died 1869.
        self._person(self.ADA, 'Ann Cole')
        self._person(self.BEN, 'Margaret Cole')
        self._source(self.S1, self._claim('C-1111111111', 'birth', [self.BEN], date='1871'))
        self._source(self.S2, self._claim('C-2222222222', 'death', [self.ADA], date='1869'))
        self._source(self.S3, self._tie('C-3333333333', self.ADA, self.BEN))
        (found,) = self._codes('W167')
        message = found.message
        self.assertTrue(message.startswith(
            'Margaret Cole (p-bbbbbbbbbb) is recorded as born 1871 (c-1111111111 '
            '(sources/notes/rec_s-1111111111.md)), but her mother Ann Cole (p-aaaaaaaaaa) '
            'died 1869 (c-2222222222 (sources/notes/rec_s-2222222222.md))'), message)
        self.assertIn('Both cannot be true', message)
        self.assertIn('The mother tie comes from c-3333333333 '
                      '(sources/notes/rec_s-3333333333.md)', message)
        self.assertEqual(Path(found.path).name, 'cole__margaret_P-bbbbbbbbbb.md')

    def test_adoptive_mother_dead_before_birth_is_silent(self) -> None:
        self._person(self.ADA, 'Ann Cole')
        self._person(self.BEN, 'Margaret Cole')
        self._source(self.S1, self._claim('C-1111111111', 'birth', [self.BEN], date='1871'))
        self._source(self.S2, self._claim('C-2222222222', 'death', [self.ADA], date='1869'))
        self._source(self.S3, self._tie('C-3333333333', self.ADA, self.BEN, subtype='adoptive'))
        self.assertEqual(self._codes('W167'), [])

    def test_parent_tie_from_a_birth_claims_roles_counts(self) -> None:
        self._person(self.ADA, 'Ann Cole')
        self._person(self.BEN, 'Margaret Cole')
        self._source(self.S1, self._claim(
            'C-1111111111', 'birth', [self.BEN, self.ADA], date='1871',
            roles=f'{{child: [{self.BEN}], parent: [{self.ADA}]}}'))
        self._source(self.S2, self._claim('C-2222222222', 'birth', [self.ADA], date='1880'))
        (found,) = self._codes('W168')
        self.assertIn('before her mother Ann Cole (p-aaaaaaaaaa) was born, 1880', found.message)
        self.assertIn('The parent tie (c-1111111111 (sources/notes/rec_s-1111111111.md))',
                      found.message)

    def test_mother_too_old_says_her_age(self) -> None:
        self._person(self.ADA, 'Ann Cole')
        self._person(self.BEN, 'Ben Cole', sex='M')
        self._source(self.S1, self._claim('C-1111111111', 'birth', [self.BEN], date='1870'))
        self._source(self.S2, self._claim('C-2222222222', 'birth', [self.ADA], date='1800'))
        self._source(self.S3, self._tie('C-3333333333', self.ADA, self.BEN))
        (found,) = self._codes('W168')
        self.assertTrue(found.message.startswith(
            'Ann Cole (p-aaaaaaaaaa) was at least 69 when her son Ben Cole (p-bbbbbbbbbb) '
            'was born 1870'), found.message)
        self.assertIn('older than a mother can be', found.message)
        self.assertEqual(Path(found.path).name, 'cole__ben_P-bbbbbbbbbb.md')

    def test_marriage_after_spouse_died(self) -> None:
        self._person(self.ADA, 'Ada Hartley')
        self._person(self.BEN, 'Ben Hartley', sex='M')
        self._source(self.S1, self._claim('C-1111111111', 'death', [self.BEN], date='1860'))
        self._source(self.S2, self._claim('C-2222222222', 'marriage', [self.ADA, self.BEN],
                                          date='1865'))
        (found,) = self._codes('W166')
        self.assertTrue(found.message.startswith(
            'Ben Hartley (p-bbbbbbbbbb) died 1860 (c-1111111111 '
            '(sources/notes/rec_s-1111111111.md)), but is recorded as marrying Ada Hartley '
            '(p-aaaaaaaaaa) 1865'), found.message)
        self.assertEqual(Path(found.path).name, 'hartley__ben_P-bbbbbbbbbb.md')

    def test_divorce_after_spouse_died_says_divorcing(self) -> None:
        self._person(self.ADA, 'Ada Hartley')
        self._person(self.BEN, 'Ben Hartley', sex='M')
        self._source(self.S1, self._claim('C-1111111111', 'death', [self.BEN], date='1860'))
        self._source(self.S2, self._claim('C-2222222222', 'divorce', [self.ADA, self.BEN],
                                          date='1865'))
        (found,) = self._codes('W166')
        self.assertIn('but is recorded as divorcing Ada Hartley', found.message)

    def test_marriage_parents_named_on_the_licence_are_not_spouses(self) -> None:
        self._person(self.ADA, 'Ada Hartley')
        self._person(self.BEN, 'Ben Hartley', sex='M')
        self._person(self.CARA, 'Carl Moss', sex='M')
        self._source(self.S1, self._claim('C-1111111111', 'death', [self.CARA], date='1850'))
        self._source(self.S2, self._claim(
            'C-2222222222', 'marriage', [self.ADA, self.BEN, self.CARA], date='1865',
            roles=f'{{spouse: [{self.ADA}, {self.BEN}], parent: [{self.CARA}]}}'))
        self.assertEqual(self._codes('W166'), [])

    def test_married_young(self) -> None:
        self._person(self.ADA, 'Ada Hartley')
        self._person(self.BEN, 'Ben Hartley', sex='M')
        self._source(self.S1, self._claim('C-1111111111', 'birth', [self.ADA], date='1860'))
        self._source(self.S2, self._claim('C-2222222222', 'marriage', [self.ADA, self.BEN],
                                          date='1872'))
        (found,) = self._codes('W166')
        self.assertTrue(found.message.startswith(
            'Ada Hartley (p-aaaaaaaaaa) was at most 12 at the marriage to Ben Hartley'),
            found.message)

    def test_siblings_too_close(self) -> None:
        self._person(self.ADA, 'Ada Hartley')
        self._person(self.BEN, 'Ben Hartley', sex='M')
        self._person(self.CARA, 'Cara Hartley')
        self._source(self.S1, self._claim('C-1111111111', 'birth', [self.BEN], date='1850-01')
                     + self._claim('C-1111111112', 'birth', [self.CARA], date='1850-06'))
        self._source(self.S2, self._tie('C-2222222221', self.ADA, self.BEN)
                     + self._tie('C-2222222222', self.ADA, self.CARA))
        (found,) = self._codes('W169')
        self.assertTrue(found.message.startswith(
            'Cara Hartley (p-cccccccccc) (June 1850'), found.message)
        self.assertIn('children of Ada Hartley (p-aaaaaaaaaa), born between 121 and 180 '
                      'days apart', found.message)
        self.assertEqual(Path(found.path).name, 'hartley__cara_P-cccccccccc.md')

    def test_own_ancestor_loop_is_reported_once(self) -> None:
        self._person(self.ADA, 'Ada Hartley')
        self._person(self.BEN, 'Ben Hartley', sex='M')
        self._source(self.S1, self._tie('C-1111111111', self.ADA, self.BEN)
                     + self._tie('C-1111111112', self.BEN, self.ADA))
        (found,) = self._codes('W170')
        self.assertTrue(found.message.startswith(
            'Ada Hartley (p-aaaaaaaaaa) is recorded as her own ancestor: Ada Hartley '
            '(p-aaaaaaaaaa) is the parent of Ben Hartley (p-bbbbbbbbbb), Ben Hartley '
            '(p-bbbbbbbbbb) is the parent of Ada Hartley (p-aaaaaaaaaa).'), found.message)
        self.assertIn('c-1111111111 (sources/notes/rec_s-1111111111.md)', found.message)
        self.assertIn('c-1111111112', found.message)


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


if __name__ == '__main__':
    unittest.main()
