"""
test_views_reports.py - `fha views family-sheet`, the printed family group sheet.

The sheet is made to be handed to a relative who has no computer, so it follows
the shared-site rules for people and evidence (SPEC §21, "Printed reports"):
accepted claims only, nothing restricted, a living relative counted but never
named (a living parent or spouse appears only as the "Living Person" label), and
every fact carries a little number that points to the record it came from.

The synthetic family below is the Hartley example: Thomas and Margaret, his
parents, her parents (her father is still living), and five children - two
who died, one living, one of unknown status, and one who asked to be left out.
"""

import contextlib
import io
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import index as index_mod
import views
from _lib import (
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    load_fha_yaml,
)

T = 'p-aaaaaaaaaa'    # Thomas Edward Hartley, d. 1941
M = 'p-bbbbbbbbbb'    # Margaret Cole, d. 1925
TF = 'p-cccccccccc'   # John Hartley (his father)
TM = 'p-dddddddddd'   # Ann Price (his mother)
MF = 'p-eeeeeeeeee'   # her father: living
MM = 'p-ffffffffff'   # her mother: deceased
C1 = 'p-gggggggggg'   # John Hartley, 1867 - 1930
C2 = 'p-hhhhhhhhhh'   # Mary Hartley, 1869
CL = 'p-jjjjjjjjjj'   # living child
CU = 'p-kkkkkkkkkk'   # child whose living status is unknown
CR = 'p-mmmmmmmmmm'   # child who asked to be left out (restricted: by-request)
CS = 'p-nnnnnnnnnn'   # Mary Ross, wife of John
NEWSPOUSE = 'p-pppppppppp'   # Thomas's second wife (deceased)
LIVSPOUSE = 'p-qqqqqqqqqq'   # a living person recorded as a husband of nobody here

S_BIRTH_T, S_BIRTH_M, S_DEATH_T, S_DEATH_M = 's-1111111111', 's-2222222222', 's-3333333333', 's-4444444444'
S_MARR, S_PAR_T, S_PAR_M, S_KIDS, S_CENSUS = 's-5555555555', 's-6666666666', 's-7777777777', 's-8888888888', 's-9999999999'
S_DNA, S_BAPT_MISC, S_BIRTH_C1, S_DEATH_C1, S_MARR_C1 = 's-aaaaaaaaaa', 's-bbbbbbbbbb', 's-cccccccccc', 's-dddddddddd', 's-eeeeeeeeee'
S_MARR2, S_PARENT_YEARS = 's-ffffffffff', 's-gggggggggg'


_CIDS: dict[str, str] = {}


def _cid(label: str) -> str:
    """A well-formed claim id for a readable label (ids are 10 digits here)."""
    return _CIDS.setdefault(label, f'c-{len(_CIDS) + 1:010d}')


def _person(pid: str, name: str, sex: str, living: str, extra: str = '') -> str:
    return (f'---\nid: {pid}\nname: {name}\nsex: {sex}\nliving: {living}\ntier: stub\n'
            f'{extra}---\n\n# {name}\n')


def _source(sid: str, title: str, citation: str, claims: str, extra: str = '',
            people: list[str] | None = None) -> str:
    people_block = ''
    if people:
        people_block = 'people:\n' + ''.join(f'  - {p}\n' for p in people)
    return (f'---\nid: {sid}\ntitle: {title}\nsource_type: vital-record\n'
            f'citation: >\n  {citation}\n{people_block}{extra}---\n\n'
            f'## Claims\n```yaml\n{claims}```\n')


def _claim(cid: str, ctype: str, persons: list[str], date: str, *, roles: str = '',
           place: str = '', status: str = 'accepted', extra: str = '', confidence: str = 'high',
           subtype: str = '') -> str:
    out = (f'- value: "{ctype} {date}"\n  id: {_cid(cid)}\n  type: {ctype}\n'
           f'  persons: [{", ".join(persons)}]\n')
    if roles:
        out += f'  roles: {roles}\n'
    if subtype:
        out += f'  subtype: {subtype}\n'
    if date:
        out += f'  date: {date}\n'
    if place:
        out += f'  place_text: "{place}"\n'
    out += (f'  status: {status}\n  reviewed: 2026-01-01\n  confidence: {confidence}\n'
            f'  information: primary\n  evidence: direct\n  notes: x.\n{extra}')
    return out


def _rel(cid: str, child: str, parents: list[str]) -> str:
    return _claim(cid, 'relationship', [child] + parents, '',
                  roles=f'{{child: {child}, parent: [{", ".join(parents)}]}}',
                  subtype='biological', confidence='medium')


class _SheetBase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        (self.root / 'people' / 'stubs').mkdir(parents=True)
        (self.root / 'sources' / 'vital-record').mkdir(parents=True)
        (self.root / 'fha.yaml').write_text('site:\n  archive_name: Test Archive\n',
                                            encoding='utf-8')
        for pid, name, sex, living, extra in self.people():
            (self.root / 'people' / 'stubs' / f'x__{pid}.md').write_text(
                _person(pid, name, sex, living, extra), encoding='utf-8')
        for sid, title, citation, claims, extra, people in self.sources():
            (self.root / 'sources' / 'vital-record' / f'x_{sid}.md').write_text(
                _source(sid, title, citation, claims, extra, people), encoding='utf-8')
        index_mod.build_index(self.root, load_fha_yaml(self.root))

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def people(self):
        return [
            (T, 'Thomas Edward Hartley', 'M', 'false', ''),
            (M, 'Margaret Cole', 'F', 'false', ''),
            (TF, 'John Hartley', 'M', 'false', ''),
            (TM, 'Ann Price', 'F', 'false', ''),
            (MF, 'Walter Cole', 'M', 'true', ''),
            (MM, 'Sarah Cole', 'F', 'false', ''),
            (C1, 'John Hartley Junior', 'M', 'false', ''),
            (C2, 'Mary Hartley', 'F', 'false', ''),
            (CL, 'Lena Hartley', 'F', 'true', ''),
            (CU, 'Otto Hartley', 'M', 'unknown', ''),
            (CR, 'Hidden Hartley', 'M', 'false', 'restricted: by-request\n'),
            (CS, 'Mary Ross', 'F', 'false', ''),
            (NEWSPOUSE, 'Edith Lowe', 'F', 'false', ''),
            (LIVSPOUSE, 'Ruth Elder', 'F', 'true', ''),
        ]

    def sources(self):
        kid_claims = ''.join(
            _rel(f'kid{i}', k, [T, M]) for i, k in enumerate([C1, C2, CL, CU], 1))
        return [
            (S_BIRTH_T, '1840 birth register', 'Breton County Probate Court, Birth Register vol. 2, p. 14.',
             _claim('c-birthT0000', 'birth', [T], '1840-06-14', roles=f'{{child: {T}}}',
                    place='Fairview, Breton County, Ohio'), '', None),
            (S_DNA, 'DNA match report', 'A DNA company report.',
             _claim('c-birthT1000', 'birth', [T], '1840-06-01', roles=f'{{child: {T}}}'),
             'restricted: true\n', None),
            (S_BAPT_MISC, 'Church register 1840',
             'St. Luke church register, Fairview, 1840.',
             _claim('c-baptT0000', 'baptism', [T], '1840-07-05', roles=f'{{child: {T}}}',
                    place='Fairview') + _claim('c-burT00000', 'burial', [T], '1941-03-06',
                                                place='Elm Creek Cemetery', status='suggested'),
             '', [CL]),
            (S_DEATH_T, 'Kansas death certificate 1941', 'Kansas State Board of Health, certificate 441.',
             _claim('c-deathT0000', 'death', [T], '1941-03-03', roles=f'{{deceased: {T}}}',
                    place='Elm Creek, Kansas'), '', None),
            (S_BIRTH_M, '1842 birth register', 'Breton County Probate Court, Birth Register vol. 2, p. 30.',
             _claim('c-birthM0000', 'birth', [M], '1842', roles=f'{{child: {M}}}',
                    place='Fairview'), '', None),
            (S_DEATH_M, 'Margaret death certificate 1925', 'Kansas State Board of Health, certificate 52.',
             _claim('c-deathM0000', 'death', [M, CL], '1925-01-09',
                    roles=f'{{deceased: {M}, informant: {CL}}}', place='Elm Creek'), '', None),
            (S_MARR, 'Marriage register 1866', 'Breton County Marriage Book A, p. 3.',
             _claim('c-marr000000', 'marriage', [T, M], '1866-05-02', roles=f'{{spouse: [{T}, {M}]}}',
                    place='Fairview'), '', None),
            (S_PAR_T, 'Thomas parentage proof', 'Family Bible, Hartley.',
             _rel('c-parT000000', T, [TF, TM]), '', None),
            (S_PAR_M, 'Margaret parentage proof', 'Family Bible, Cole.',
             _rel('c-parM000000', M, [MF, MM]), '', None),
            (S_KIDS, 'Hartley family Bible', 'Hartley family Bible, births page.', kid_claims, '', None),
            # A record that names a restricted person is left out whole (the
            # site's rule), so the private child gets a record of their own.
            ('s-kkkkkkkkkk', 'Private child note', 'A private note.', _rel('kidr', CR, [T, M]), '', None),
            (S_BIRTH_C1, 'John junior birth', 'Breton County Birth Register vol. 3.',
             _claim('c-birthC1000', 'birth', [C1], '1867', roles=f'{{child: {C1}}}', place='Fairview')
             + _claim('c-birthC2000', 'birth', [C2], '1869', roles=f'{{child: {C2}}}', place='Fairview')
             + _claim('c-birthCL000', 'birth', [CL], '1901', roles=f'{{child: {CL}}}')
             + _claim('c-birthC2neg', 'birth', [C2], '1899', roles=f'{{child: {C2}}}',
                      extra='  negated: true\n'), '', None),
            (S_DEATH_C1, 'John junior death', 'Kansas death record 1930.',
             _claim('c-deathC1000', 'death', [C1], '1930', roles=f'{{deceased: {C1}}}'), '', None),
            (S_MARR_C1, 'John junior marriage', 'Breton County Marriage Book B.',
             _claim('c-marrC1000', 'marriage', [C1, CS], '1890', roles=f'{{spouse: [{C1}, {CS}]}}'),
             '', None),
            (S_PARENT_YEARS, 'Hartley parents vitals', 'Hartley Bible, deaths page.',
             _claim('c-birthTF000', 'birth', [TF], '1810', roles=f'{{child: {TF}}}')
             + _claim('c-deathTF000', 'death', [TF], '1881', roles=f'{{deceased: {TF}}}'), '', None),
        ]

    def run_sheet(self, pid=T, spouse=None, fmt='md'):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = views.run_family_sheet(self.root, pid, spouse, fmt)
        return result, out.getvalue(), err.getvalue()

    def sheet_text(self, pid=T, spouse=None, fmt='md') -> str:
        result, out, err = self.run_sheet(pid, spouse, fmt)
        self.assertEqual(result.exit_code, EXIT_CLEAN, out + err)
        return Path(result.changed[0]).read_text(encoding='utf-8')


class FamilySheetContentTests(_SheetBase):
    def test_sheet_names_both_spouses_parents_marriage_and_children_with_notes(self):
        text = self.sheet_text()
        self.assertIn('# Family group sheet: Thomas Edward Hartley and Margaret Cole', text)
        self.assertIn('## Husband: Thomas Edward Hartley', text)
        self.assertIn('## Wife: Margaret Cole', text)
        self.assertIn('Born: 14 June 1840, Fairview, Breton County, Ohio', text)
        self.assertIn('Died: 3 March 1941, Elm Creek, Kansas', text)
        self.assertIn('Baptised: 5 July 1840, Fairview', text)
        self.assertIn('Father: John Hartley (1810-1881)', text)
        self.assertIn('Mother: Ann Price', text)
        self.assertIn('Married: 2 May 1866, Fairview', text)
        self.assertIn('John Hartley Junior', text)
        self.assertIn('Mary Hartley', text)
        self.assertIn('Mary Ross', text)   # John's wife, in his Married cell
        self.assertIn('1. 1840 birth register. Breton County Probate Court, Birth Register vol. 2, p. 14.', text)

    def test_every_fact_row_has_a_note_number_and_every_note_is_listed(self):
        text = self.sheet_text()
        used: set[int] = set()
        for line in text.splitlines():
            if re.match(r'^(Born|Baptised|Married|Died|Buried):', line):
                m = re.search(r'\[(\d+(?:, \d+)*)\]$', line)
                self.assertIsNotNone(m, line)
                used.update(int(n) for n in m.group(1).split(', '))
        for line in text.splitlines():
            if re.match(r'^(Father|Mother): .*\(\w* ?\d{4}', line):
                self.assertRegex(line, r'\[\d+(, \d+)*\]$')
            if line.startswith('| ') or re.match(r'^(Father|Mother): ', line):
                for m in re.finditer(r'\[(\d+(?:, \d+)*)\]', line):
                    used.update(int(n) for n in m.group(1).split(', '))
        listed = {int(m.group(1)) for m in re.finditer(r'^(\d+)\. ', text, re.M)}
        self.assertTrue(used)
        self.assertEqual(used, listed)

    def test_living_child_is_counted_not_named(self):
        for fmt in ('md', 'html'):
            text = self.sheet_text(fmt=fmt)
            self.assertNotIn('Lena Hartley', text)
            self.assertNotIn('Otto Hartley', text)
        md = self.sheet_text()
        self.assertIn('and 2 living children (not named)', md)
        self.assertIn('This sheet names', md)
        self.assertRegex(md, r'\d+ living relatives are counted but not named')

    def test_living_parent_is_the_label_and_the_living_persons_claims_are_left_out(self):
        text = self.sheet_text()
        self.assertNotIn('Walter Cole', text)
        self.assertIn('Living Person', text)

    def test_restricted_child_is_left_out_and_not_counted(self):
        text = self.sheet_text()
        self.assertNotIn('Hidden Hartley', text)
        self.assertIn('and 2 living children (not named)', text)   # CL and CU, not CR

    def test_claim_naming_a_living_person_is_left_out(self):
        text = self.sheet_text()
        margaret = text.split('## Wife: Margaret Cole')[1].split('## Marriage')[0]
        self.assertNotIn('Died:', margaret)
        self.assertIn('Born: 1842, Fairview', margaret)

    def test_source_naming_a_living_person_is_cited_to_the_shared_label(self):
        text = self.sheet_text()
        self.assertIn('Baptised: 5 July 1840, Fairview', text)
        self.assertNotIn('Church register 1840', text)
        self.assertNotIn('St. Luke', text)
        self.assertIn('A record that is not included in this report '
                      '(it names a living relative or is private).', text)

    def test_restricted_claim_and_dna_source_are_left_out(self):
        text = self.sheet_text()
        self.assertNotIn('DNA match report', text)
        self.assertNotIn('1840-06-01', text)
        self.assertNotIn('1 June 1840', text)

    def test_unaccepted_and_negated_claims_never_appear(self):
        text = self.sheet_text()
        self.assertNotIn('Buried:', text)
        self.assertNotIn('Elm Creek Cemetery', text)
        self.assertNotIn('1899', text)

    def test_vital_pick_matches_the_site(self):
        # An exact day beats a year-only claim for the same event.
        extra = _source('s-hhhhhhhhhh', 'Census age', 'US census 1880.',
                        _claim('c-birthT2000', 'birth', [T], '1840', roles=f'{{child: {T}}}'))
        (self.root / 'sources' / 'vital-record' / 'x_extra.md').write_text(extra, encoding='utf-8')
        index_mod.build_index(self.root, load_fha_yaml(self.root))
        text = self.sheet_text()
        self.assertIn('Born: 14 June 1840', text)

    def test_children_are_in_birth_order_oldest_first(self):
        text = self.sheet_text()
        self.assertLess(text.index('John Hartley Junior'), text.index('| Mary Hartley'))

    def test_second_spouse_gets_a_second_sheet_in_marriage_order(self):
        sources = _source(S_MARR2, 'Second marriage', 'Breton County Marriage Book C.',
                          _claim('c-marr200000', 'marriage', [T, NEWSPOUSE], '1930',
                                 roles=f'{{spouse: [{T}, {NEWSPOUSE}]}}'))
        (self.root / 'sources' / 'vital-record' / 'x_marr2.md').write_text(sources, encoding='utf-8')
        index_mod.build_index(self.root, load_fha_yaml(self.root))
        text = self.sheet_text()
        self.assertLess(text.index('Margaret Cole'), text.index('Edith Lowe'))
        one = self.sheet_text(spouse=NEWSPOUSE)
        self.assertIn('Edith Lowe', one)
        self.assertNotIn('Margaret Cole', one)
        self.assertNotIn('John Hartley Junior', one)

    def test_living_spouse_is_the_label_with_no_name_and_no_facts(self):
        src = _source('s-jjjjjjjjjj', 'Marriage 1895', 'Breton County Marriage Book D.',
                      _claim('c-marr300000', 'marriage', [C2, LIVSPOUSE], '1895',
                             roles=f'{{spouse: [{C2}, {LIVSPOUSE}]}}'))
        (self.root / 'sources' / 'vital-record' / 'x_marr3.md').write_text(src, encoding='utf-8')
        index_mod.build_index(self.root, load_fha_yaml(self.root))
        text = self.sheet_text(pid=C2)
        self.assertNotIn('Ruth Elder', text)
        self.assertIn('Living Person', text)
        self.assertIn('Mary Hartley', text)

    def test_a_person_with_no_spouse_gets_one_sheet(self):
        text = self.sheet_text(pid=NEWSPOUSE)
        self.assertIn('Family group sheet: Edith Lowe', text)
        self.assertIn('No spouse is recorded.', text)

    def test_parents_with_no_marriage_record_share_one_sheet(self):
        # Thomas's parents have no marriage on record, only his parentage.
        text = self.sheet_text(pid=TM)
        self.assertIn('Family group sheet: John Hartley and Ann Price', text)
        self.assertIn('| 1 | Thomas Edward Hartley', text)

    def test_rewrite_after_no_change_is_byte_identical(self):
        first = self.sheet_text()
        second = self.sheet_text()
        self.assertEqual(first, second)


class FamilySheetRefusalTests(_SheetBase):
    def test_living_subject_is_refused_and_nothing_written(self):
        result, out, err = self.run_sheet(pid=CL)
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertIn('recorded as living', err)
        self.assertIn(f'fha person set-living {CL} false', err)
        self.assertFalse((self.root / 'generated' / 'views').exists())

    def test_unknown_status_subject_is_refused(self):
        result, out, err = self.run_sheet(pid=CU)
        self.assertEqual(result.exit_code, EXIT_ERRORS)

    def test_restricted_subject_is_refused_without_naming_them(self):
        result, out, err = self.run_sheet(pid=CR)
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertIn('is marked private (restricted)', err)
        self.assertNotIn('Hidden', err + out)
        self.assertFalse((self.root / 'generated' / 'views').exists())

    def test_unknown_id_exits_one_with_a_find_hint(self):
        result, out, err = self.run_sheet(pid='p-zzzzzzzzzz')
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertIn('No person p-zzzzzzzzzz in the index.', err)

    def test_spouse_who_is_not_a_spouse_exits_two_and_lists_the_real_ones(self):
        result, out, err = self.run_sheet(spouse=CS)
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertIn('is not recorded as Thomas Edward Hartley', err)
        self.assertIn('Margaret Cole', err)

    def test_no_index_exits_three(self):
        shutil.rmtree(self.root / '.cache')
        result, out, err = self.run_sheet()
        self.assertEqual(result.exit_code, EXIT_FAILURE)


class FamilySheetHtmlTests(_SheetBase):
    def test_html_twin_has_the_report_banner_and_no_external_requests(self):
        text = self.sheet_text(fmt='html')
        self.assertTrue(text.startswith('<!-- GENERATED by fha views'))
        self.assertIn('Prepared for family. Living relatives are counted but not named, '
                      'and private records are left out.', text)
        self.assertNotIn('Private research companion', text)
        low = text.lower()
        self.assertNotIn('http://', low)
        self.assertNotIn('https://', low)
        self.assertNotIn('<link', low)
        self.assertIsNone(re.search(r'<script[^>]*\bsrc\s*=', low))
        self.assertIn('<sup class="note">', text)
        self.assertIn('<ol class="report-notes">', text)
        self.assertEqual(text.count('<style>'), 1)

    def test_html_is_written_under_generated_views(self):
        result, _out, _err = self.run_sheet(fmt='html')
        self.assertEqual(Path(result.changed[0]).parent, self.root / 'generated' / 'views')
        self.assertEqual(Path(result.changed[0]).name, f'report_family-sheet_{T}.html')

    def test_existing_views_keep_the_private_banner(self):
        html = views._render_view_html(self.root, 'timeline', 'T', '<p>x</p>')
        self.assertIn('Private research companion', html)
        self.assertNotIn('Prepared for family', html)

    def test_views_clean_removes_reports(self):
        self.run_sheet()
        self.run_sheet(fmt='html')
        folder = self.root / 'generated' / 'views'
        self.assertEqual(len(list(folder.iterdir())), 2)
        with contextlib.redirect_stdout(io.StringIO()):
            views.run_clean(self.root)
        self.assertEqual(list(folder.iterdir()), [])


class FamilySheetReviewFixTests(_SheetBase):
    """Review round 1 of the family sheet: what a relative reading the printout
    would have been told wrongly, or not told at all."""

    def add_source(self, name: str, sid: str, title: str, citation: str, claims: str) -> None:
        (self.root / 'sources' / 'vital-record' / f'x_{name}.md').write_text(
            _source(sid, title, citation, claims), encoding='utf-8')

    def add_person(self, pid: str, name: str, sex: str, living: str) -> None:
        (self.root / 'people' / 'stubs' / f'x__{pid}.md').write_text(
            _person(pid, name, sex, living), encoding='utf-8')

    def reindex(self) -> None:
        index_mod.build_index(self.root, load_fha_yaml(self.root))

    def line(self, text: str, start: str) -> str:
        return next(ln for ln in text.splitlines() if ln.startswith(start))

    def test_parent_years_keep_about_unconfirmed_and_ranges(self):
        # His cousin must not read an exact year the records do not give.
        self.add_source('annyears', 's-nnnnnnnnnn', 'Price vitals', 'Price family Bible.',
                        _claim('c-birthTM000', 'birth', [TM], '1815~', roles=f'{{child: {TM}}}')
                        + _claim('c-deathTM000', 'death', [TM], '1890?', roles=f'{{deceased: {TM}}}')
                        + _claim('c-birthMM000', 'birth', [MM], '1818/1820', roles=f'{{child: {MM}}}'))
        self.reindex()
        text = self.sheet_text()
        mother = self.line(text, 'Mother: Ann Price')
        self.assertIn('about 1815', mother)
        self.assertIn('1890 (unconfirmed)', mother)
        self.assertNotIn('(1815-1890)', mother)
        self.assertIn('1818 to 1820', self.line(text, 'Mother: Sarah Cole'))
        self.assertIn('Father: John Hartley (1810-1881)', text)   # plain years stay short

    def notes(self, text: str) -> dict[int, str]:
        return {int(m.group(1)): m.group(2)
                for m in re.finditer(r'^(\d+)\. (.*)$', text, re.M)}

    def cited_titles(self, text: str, line: str) -> list[str]:
        notes = self.notes(text)
        nums = [int(n) for m in re.finditer(r'\[(\d+(?:, \d+)*)\]', line)
                for n in m.group(1).split(', ')]
        return [notes[n] for n in nums]

    def test_parent_child_and_married_lines_cite_the_record_of_the_tie(self):
        # A relative can look up where "Ann Price was his mother" comes from.
        text = self.sheet_text()
        mother = self.line(text, 'Mother: Ann Price')
        self.assertTrue(any(t.startswith('Thomas parentage proof')
                            for t in self.cited_titles(text, mother)), mother)
        father = self.line(text, 'Father: John Hartley')
        self.assertTrue(any(t.startswith('Thomas parentage proof')
                            for t in self.cited_titles(text, father)), father)
        row = self.line(text, '| 1 | John Hartley Junior')
        name_cell = row.split('|')[2]
        # The family Bible also names the living children, so it is the shared note.
        self.assertEqual(self.cited_titles(text, name_cell),
                         ['A record that is not included in this report '
                          '(it names a living relative or is private).'], row)
        married_cell = row.split('|')[5]
        self.assertTrue(any(t.startswith('John junior marriage')
                            for t in self.cited_titles(text, married_cell)), row)

    def test_adoptive_parent_is_listed_after_the_birth_parents_and_labelled(self):
        adp = 'p-rrrrrrrrrr'
        self.add_person(adp, 'George Lowe', 'M', 'false')
        self.add_source('adopt', 's-xxxxxxxxxx', 'Adoption decree', 'Breton County Orphans Court.',
                        _claim('c-adopt00000', 'relationship', [T, adp], '',
                               roles=f'{{child: {T}, parent: [{adp}]}}', subtype='adoptive'))
        self.reindex()
        text = self.sheet_text()
        husband = text.split('## Wife:')[0]
        self.assertIn('Father (adoptive): George Lowe', husband)
        self.assertLess(husband.index('Father: John Hartley'), husband.index('George Lowe'))
        # From the adoptive father's side, Thomas is his (adopted) child.
        self.assertIn('Thomas Edward Hartley (adopted)', self.sheet_text(pid=adp))

    def test_child_of_another_recorded_parent_is_not_put_under_this_couple(self):
        # The sheet must not tell his cousin that Margaret was Zeke's mother.
        jane, zeke, young = 'p-ssssssssss', 'p-tttttttttt', 'p-wwwwwwwwww'
        self.add_person(jane, 'Jane Doe', 'F', 'false')
        self.add_person(zeke, 'Zeke Hartley', 'M', 'false')
        self.add_person(young, 'Young Hartley', 'M', 'false')
        self.add_source('zeke', 's-pppppppppp', 'Zeke birth', 'Breton County Birth Register vol. 4.',
                        _rel('c-parZ000000', zeke, [T, jane]))
        self.add_source('young', 's-qqqqqqqqqq', 'Young birth', 'Breton County Birth Register vol. 5.',
                        _rel('c-parY000000', young, [T]))
        zoe = 'p-yyyyyyyyyy'
        self.add_person(zoe, 'Zoe Hartley', 'F', 'true')
        self.add_source('zoe', 's-rrrrrrrrrr', 'Zoe birth', 'Breton County Birth Register vol. 6.',
                        _rel('c-parZoe0000', zoe, [T, jane]))
        self.reindex()
        text = self.sheet_text()
        margaret_sheet = text.split('Jane Doe')[0]
        self.assertNotIn('Zeke Hartley', margaret_sheet)
        self.assertIn('Young Hartley', margaret_sheet)   # no other parent recorded: still listed
        # Jane and Thomas, with no marriage on record, still get their own
        # sheet, so Zeke is not lost and living Zoe is counted, not named.
        self.assertIn('## Family group sheet: Thomas Edward Hartley and Jane Doe', text)
        jane_sheet = text.split('Family group sheet: Thomas Edward Hartley and Jane Doe')[1]
        self.assertIn('Zeke Hartley', jane_sheet)
        self.assertNotIn('Zoe Hartley', text)
        self.assertIn('1 living child (not named)', jane_sheet)
        self.assertNotIn('Young Hartley', jane_sheet)
        # From Jane's side the same couple is one sheet.
        self.assertIn('Zeke Hartley', self.sheet_text(pid=jane))
        # --spouse accepts the other parent of his children.
        self.assertIn('Zeke Hartley', self.sheet_text(spouse=jane))

    def break_file(self, pid: str) -> None:
        path = self.root / 'people' / 'stubs' / f'x__{pid}.md'
        path.write_text(path.read_text(encoding='utf-8').replace(
            'tier: stub\n', 'tier: stub\nnotes: [unclosed\n'), encoding='utf-8')
        self.reindex()

    def test_unreadable_person_file_is_named_with_the_fix(self):
        # Sarah's file has a typo; her line vanishing with no word would leave
        # him wondering why Margaret's mother is missing.
        self.break_file(MM)
        result, out, err = self.run_sheet()
        # Written, but exit 1 as fha gedcom does, so a script sees the gap.
        self.assertEqual(result.exit_code, EXIT_WARNINGS, err)
        self.assertTrue(Path(result.changed[0]).exists())
        self.assertIn(f'x__{MM}.md', err)
        self.assertIn('fha index', err)

    def test_unreadable_subject_file_is_not_called_private(self):
        self.break_file(T)
        result, out, err = self.run_sheet()
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertNotIn('is marked private', err)
        self.assertIn(f'x__{T}.md', err)
        self.assertIn('Nothing was written', err)
        self.assertFalse((self.root / 'generated' / 'views').exists())

    def test_living_refusal_reads_right_with_no_sex_recorded(self):
        nosex = 'p-vvvvvvvvvv'
        self.add_person(nosex, 'Pat Hartley', 'U', 'true')
        self.reindex()
        result, out, err = self.run_sheet(pid=nosex)
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertIn('If they have died', err)
        self.assertNotIn('they has', err)


    def test_step_and_adopted_childrens_other_parents_are_never_his_partners(self):
        # An adopted girl's birth mother, or Margaret's first husband, must
        # never be printed beside Thomas as a couple.
        jack, sam, bella, ada = 'p-ssssssssss', 'p-tttttttttt', 'p-wwwwwwwwww', 'p-yyyyyyyyyy'
        self.add_person(jack, 'Jack Miller', 'M', 'false')
        self.add_person(sam, 'Sam Miller', 'M', 'false')
        self.add_person(bella, 'Bella Birth', 'F', 'false')
        self.add_person(ada, 'Ada Hartley', 'F', 'false')
        self.add_source('sam', 's-pppppppppp', 'Sam birth', 'Breton County Birth Register vol. 7.',
                        _rel('c-parSam0000', sam, [M, jack])
                        + _claim('c-stepSam000', 'relationship', [sam, T], '',
                                 roles=f'{{child: {sam}, parent: [{T}]}}', subtype='step'))
        self.add_source('ada', 's-qqqqqqqqqq', 'Ada adoption', 'Breton County Orphans Court.',
                        _rel('c-parAda0000', ada, [bella])
                        + _claim('c-adoptAda00', 'relationship', [ada, T, M], '',
                                 roles=f'{{child: {ada}, parent: [{T}, {M}]}}', subtype='adoptive'))
        self.reindex()
        text = self.sheet_text()
        self.assertNotIn('Jack Miller', text)
        self.assertNotIn('Bella Birth', text)
        self.assertEqual(text.count('Sam Miller'), 1)
        self.assertEqual(text.count('Ada Hartley'), 1)

    def test_child_adopted_by_him_alone_stays_on_his_sheet_marked_as_his(self):
        # Ada's birth mother is recorded and Thomas adopted her on his own:
        # no other sheet of his lists her, so she must not vanish, and her
        # row must not suggest Margaret is her mother.
        bella, ada = 'p-wwwwwwwwww', 'p-yyyyyyyyyy'
        self.add_person(bella, 'Bella Birth', 'F', 'false')
        self.add_person(ada, 'Ada Hartley', 'F', 'false')
        self.add_source('ada', 's-qqqqqqqqqq', 'Ada adoption', 'Breton County Orphans Court.',
                        _rel('c-parAda0000', ada, [bella])
                        + _claim('c-adoptAda00', 'relationship', [ada, T], '',
                                 roles=f'{{child: {ada}, parent: [{T}]}}', subtype='adoptive'))
        self.reindex()
        text = self.sheet_text()
        self.assertEqual(text.count('Ada Hartley'), 1)
        self.assertIn('Ada Hartley (adopted child of Thomas Edward Hartley)', text)
        self.assertNotIn('Bella Birth', text)

    def test_his_child_whose_other_parent_is_private_stays_on_his_sheet(self):
        # Ada's mother is a private person on a separate record: Ada must not
        # vanish from Thomas's report, and Margaret must not be implied.
        ada = 'p-yyyyyyyyyy'
        self.add_person(ada, 'Ada Hartley', 'F', 'false')
        self.add_source('ada', 's-qqqqqqqqqq', 'Ada birth', 'Breton County Birth Register vol. 9.',
                        _rel('c-parAda0000', ada, [T]))
        self.add_source('adamom', 's-rrrrrrrrrr', 'Ada mother', 'A private letter.',
                        _rel('c-momAda0000', ada, [CR]))
        self.reindex()
        text = self.sheet_text()
        self.assertEqual(text.count('Ada Hartley'), 1)
        self.assertIn('Ada Hartley (child of Thomas Edward Hartley)', text)
        self.assertNotIn('Hidden Hartley', text)

    def test_no_spouse_sheet_never_calls_her_a_wife(self):
        text = self.sheet_text(pid=NEWSPOUSE)
        self.assertNotIn('Wife:', text)
        self.assertNotIn('Husband:', text)

    def test_parents_never_married_are_father_and_mother_not_husband_and_wife(self):
        jane, zeke = 'p-ssssssssss', 'p-tttttttttt'
        self.add_person(jane, 'Jane Doe', 'F', 'false')
        self.add_person(zeke, 'Zeke Hartley', 'M', 'false')
        self.add_source('zeke', 's-pppppppppp', 'Zeke birth', 'Breton County Birth Register vol. 4.',
                        _rel('c-parZ000000', zeke, [T, jane]))
        self.reindex()
        text = self.sheet_text(spouse=jane)
        self.assertIn('## Mother: Jane Doe', text)
        self.assertIn('## Father: Thomas Edward Hartley', text)
        self.assertNotIn('Wife:', text)
        self.assertNotIn('Husband:', text)
        self.assertIn('## Husband: Thomas Edward Hartley', self.sheet_text(spouse=M))

if __name__ == '__main__':
    unittest.main()


TFF = 'p-rrrrrrrrrr'   # Henry Hartley, John's father
TFM = 'p-ssssssssss'   # Ruth Hartley, John's mother
WF = 'p-tttttttttt'    # Amos Cole, father of the living Walter
HID = 'p-uuuuuuuuuu'   # a great-grandfather behind a restricted record
S_PAR_TF, S_PAR_W, S_MARR_TF, S_PAR_HID = 's-hhhhhhhhhh', 's-iiiiiiiiii', 's-jjjjjjjjjj', 's-kkkkkkkkk1'


class AhnentafelTests(_SheetBase):
    """`fha views ahnentafel`: his ancestors numbered the way genealogy books
    print them (1 him, 2 his father, 3 his mother, 4 his father's father...),
    each fact with a note, living relatives never named."""

    def people(self):
        return super().people() + [
            (TFF, 'Henry Hartley', 'M', 'false', ''),
            (TFM, 'Ruth Hartley', 'F', 'false', ''),
            (WF, 'Amos Cole', 'M', 'false', ''),
            (HID, 'Silas Hartley', 'M', 'false', ''),
        ]

    def sources(self):
        return super().sources() + [
            (S_PAR_TF, 'John parentage proof', 'Hartley Bible, John page.',
             _rel('c-parTF00000', TF, [TFF, TFM]), '', None),
            (S_PAR_W, 'Walter parentage proof', 'Cole Bible.',
             _rel('c-parW000000', MF, [WF]), '', None),
            (S_MARR_TF, 'Hartley marriage 1838', 'Breton County Marriage Book A, p. 1.',
             _claim('c-marrTF0000', 'marriage', [TF, TM], '1838-04-10',
                    roles=f'{{spouse: [{TF}, {TM}]}}', place='Fairview'), '', None),
            (S_PAR_HID, 'DNA parentage', 'A DNA company report.',
             _rel('c-parHID0000', TFF, [HID]), 'restricted: true\n', None),
        ]

    def run_ahn(self, pid=T, generations=6, fmt='md'):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = views.run_ahnentafel(self.root, pid, generations, fmt)
        return result, out.getvalue(), err.getvalue()

    def text(self, pid=T, generations=6, fmt='md') -> str:
        result, out, err = self.run_ahn(pid, generations, fmt)
        self.assertEqual(result.exit_code, EXIT_CLEAN, out + err)
        return Path(result.changed[0]).read_text(encoding='utf-8')

    def test_numbers_follow_the_father_mother_slots(self):
        text = self.text()
        self.assertIn('# Ancestors of Thomas Edward Hartley', text)
        self.assertRegex(text, r'(?m)^\*\*1\.\*\* Thomas Edward Hartley$')
        self.assertRegex(text, r'(?m)^\*\*2\.\*\* John Hartley \[\d+\]$')
        self.assertRegex(text, r'(?m)^\*\*3\.\*\* Ann Price \[\d+\]$')
        self.assertRegex(text, r'(?m)^\*\*4\.\*\* Henry Hartley \[\d+\]$')
        self.assertRegex(text, r'(?m)^\*\*5\.\*\* Ruth Hartley \[\d+\]$')
        self.assertIn('## Generation 1', text)
        self.assertIn('## Generation 2: parents', text)
        self.assertIn('## Generation 3: grandparents', text)

    def test_facts_carry_notes_and_the_marriage_prints_once(self):
        text = self.text()
        self.assertIn('- Born: 14 June 1840, Fairview, Breton County, Ohio [', text)
        self.assertIn('- Died: 3 March 1941, Elm Creek, Kansas [', text)
        self.assertEqual(text.count('Married: Ann Price, 10 April 1838, Fairview ['), 1)
        self.assertNotIn('Married: John Hartley', text)
        self.assertIn('Married: Margaret Cole, 2 May 1866, Fairview [', text)

    def test_a_restricted_record_cuts_the_branch_above_it(self):
        text = self.text()
        self.assertNotIn('Silas Hartley', text)
        self.assertNotIn('DNA', text)

    def test_a_living_parent_is_a_label_and_her_parents_still_print(self):
        text = self.text(M)
        self.assertRegex(text, r'(?m)^\*\*2\.\*\* A living relative \(not named\) \[\d+\]$')
        self.assertNotIn('Walter', text)
        self.assertRegex(text, r'(?m)^\*\*3\.\*\* Sarah Cole \[\d+\]$')
        self.assertRegex(text, r'(?m)^\*\*4\.\*\* Amos Cole \[\d+\]$')
        self.assertIn('1 living relative is counted but not named', text)

    def test_a_living_subject_prints_as_the_label_and_the_line_continues(self):
        text = self.text(MF)
        self.assertRegex(text, r'(?m)^\*\*1\.\*\* A living relative \(not named\)$')
        self.assertIn('# Ancestors of a living relative', text)
        self.assertRegex(text, r'(?m)^\*\*2\.\*\* Amos Cole \[\d+\]$')
        self.assertNotIn('Walter', text)

    def test_nobody_shown_is_refused(self):
        result, _out, err = self.run_ahn(LIVSPOUSE)
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertIn('Everyone this report would list is living or private, '
                      'so there is nothing to print.', err)
        self.assertEqual(result.changed, [])

    def test_restricted_subject_is_refused_without_a_name(self):
        result, _out, err = self.run_ahn(CR)
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertNotIn('Hidden', err)

    def test_generations_stops_at_the_requested_depth(self):
        text = self.text(T, 2)
        self.assertIn('Ann Price', text)
        self.assertNotIn('Henry Hartley', text)
        self.assertNotIn('Generation 3', text)

    def test_too_many_generations_is_clamped_with_a_note(self):
        result, out, _err = self.run_ahn(T, 13)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertIn('Showing 12 generations, the most a printed report goes to.', out)

    def test_notes_are_listed_and_html_renders_people_not_a_notes_list(self):
        text = self.text(fmt='html')
        self.assertIn('Ancestors of Thomas Edward Hartley', text)
        self.assertEqual(text.count('<ol class="report-notes">'), 1)
        self.assertIn('<sup class="note">', text)
        self.assertIn('Henry Hartley', text)
        self.assertNotIn('<li value="4">Henry', text)

    def test_rewrite_is_byte_identical(self):
        first = self.text()
        second = self.text()
        self.assertEqual(first, second)

    def test_a_person_met_twice_is_not_expanded_again(self):
        # Ann Price is also made the mother of John: she sits at two numbers.
        (self.root / 'sources' / 'vital-record' / 'x_loop.md').write_text(
            _source('s-loop000001', 'Loop proof', 'Bible.',
                    _rel('c-parTFloop0', TF, [TFF, TM])), encoding='utf-8')
        index_mod.build_index(self.root, load_fha_yaml(self.root))
        text = self.text()
        self.assertIn('(also no. 3)', text)

    def test_numbers_survive_a_markdown_preview(self):
        # A preview renumbers a markdown list with gaps (4, 6, 7 shows as
        # 1, 2, 3), so a person line must not be a list item.
        text = self.text()
        people_part = text.split('## Notes')[0]
        self.assertNotRegex(people_part, r'(?m)^\d+\. ')
        self.assertNotRegex(people_part, r'(?m)^ {1,3}\S')

    def test_an_unreadable_file_is_named_before_nothing_to_print(self):
        # Amos's file has a typo, which cuts off the only deceased line above
        # the living Walter: he must be told which file, not just "private".
        path = self.root / 'people' / 'stubs' / f'x__{WF}.md'
        path.write_text(path.read_text(encoding='utf-8').replace(
            'tier: stub\n', 'tier: stub\nnotes: [unclosed\n'), encoding='utf-8')
        index_mod.build_index(self.root, load_fha_yaml(self.root))
        result, _out, err = self.run_ahn(MF)
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertIn(f'x__{WF}.md', err)
        self.assertIn('fha index', err)
        self.assertIn('nothing to print', err)

    def test_a_second_marriage_met_at_a_second_number_still_prints(self):
        # George's son by Alice married George's daughter by Betty: George is
        # no. 4 beside Alice and no. 6 beside Betty, and both weddings print.
        gg, ga, gb, fr, ma, sam = ('p-gggggggg01', 'p-gggggggg02', 'p-gggggggg03',
                                   'p-gggggggg04', 'p-gggggggg05', 'p-gggggggg06')
        for pid, name, sex in ((gg, 'George Grand', 'M'), (ga, 'Alice First', 'F'),
                               (gb, 'Betty Second', 'F'), (fr, 'Fred Grand', 'M'),
                               (ma, 'Mary Grand', 'F'), (sam, 'Sam Grand', 'M')):
            (self.root / 'people' / 'stubs' / f'x__{pid}.md').write_text(
                _person(pid, name, sex, 'false'), encoding='utf-8')
        claims = (_rel('c-grFred0001', fr, [gg, ga]) + _rel('c-grMary0001', ma, [gg, gb])
                  + _rel('c-grSam00001', sam, [fr, ma])
                  + _claim('c-grMarA0001', 'marriage', [gg, ga], '1880',
                           roles=f'{{spouse: [{gg}, {ga}]}}', place='Fairview')
                  + _claim('c-grMarB0001', 'marriage', [gg, gb], '1890',
                           roles=f'{{spouse: [{gg}, {gb}]}}', place='Fairview'))
        (self.root / 'sources' / 'vital-record' / 'x_grand.md').write_text(
            _source('s-grand00001', 'Grand family Bible', 'Bible.', claims), encoding='utf-8')
        index_mod.build_index(self.root, load_fha_yaml(self.root))
        text = self.text(sam)
        self.assertIn('(also no. 4)', text)
        self.assertEqual(text.count('Married: Alice First, 1880'), 1)
        self.assertEqual(text.count('Married: Betty Second, 1890'), 1)
