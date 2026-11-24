"""
test_connected_facts.py - the "Connected facts" section of `fha views
draft-queue` (TOOLING §14b) and the `_lib.connected_facts` engine behind it.

When someone drafts Margaret's biography they are handed, with sources, the
facts that tie her life to her family: she lost her parents young, she shared
a birthday with her brother, three of her children died before her. These
tests build small real archives (people, sources, claims, an index) and check
each rule at its edge: the day before a 16th birthday against the day itself,
twins one day apart against two, an accepted death against a parked one.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import index as index_mod
import views
from _lib import connected_facts, load_fha_yaml, open_index_db

ME = 'p-m000000001'
FATHER = 'p-f000000001'
MOTHER = 'p-n000000001'
SIB = 'p-b000000001'
SPOUSE = 'p-h000000001'
KID1 = 'p-k000000001'
KID2 = 'p-k000000002'
KID3 = 'p-k000000003'
PLACE = 'l-c000000001'


def _claim(cid: str, ctype: str, persons: list[str], roles: dict[str, list[str]] | None = None,
           date: str | None = None, *, status: str = 'accepted', place: str | None = None,
           subtype: str | None = None, negated: bool = False) -> str:
    out = [f'- value: "{ctype} {cid}"', f'  id: {cid}', f'  type: {ctype}',
           f'  persons: [{", ".join(persons)}]']
    if roles:
        out.append('  roles:')
        out += [f'    {role}: [{", ".join(who)}]' for role, who in roles.items()]
    if date:
        out.append(f'  date: {date}')
    if place:
        out.append(f'  place: {place}')
    if subtype:
        out.append(f'  subtype: {subtype}')
    if negated:
        out.append('  negated: true')
    out += [f'  status: {status}', '  reviewed: 2026-01-01', '  confidence: medium',
            '  information: primary', '  evidence: direct', '  notes: x.', '']
    return '\n'.join(out)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        for d in ('people/stubs', 'people/040 Margaret Cole', 'sources/census', 'places'):
            (self.root / d).mkdir(parents=True)
        (self.root / 'fha.yaml').write_text(
            'site:\n  archive_name: Test Archive\nroots:\n  documents: documents\n',
            encoding='utf-8')
        (self.root / 'places' / 'places.yaml').write_text(
            f'- id: {PLACE}\n  name: Riverside Cemetery\n', encoding='utf-8')
        self.people: dict[str, tuple[str, str, str]] = {}
        self.sources: dict[str, list[str]] = {}
        self.person(ME, 'Margaret Cole', 'F')

    def person(self, pid: str, name: str, sex: str = 'F', living: str = 'false') -> None:
        self.people[pid] = (name, sex, living)

    def claims(self, sid: str, *blocks: str) -> None:
        self.sources.setdefault(sid, []).extend(blocks)

    def build(self, profile_body: str = '') -> None:
        for pid, (name, sex, living) in self.people.items():
            curated = pid == ME
            folder = self.root / 'people' / ('040 Margaret Cole' if curated else 'stubs')
            tier = 'curated' if curated else 'stub'
            (folder / f'cole__{name.split()[0].lower()}_{pid}.md').write_text(
                f'---\nid: {pid}\nname: {name}\nsex: {sex}\nliving: {living}\ntier: {tier}\n---\n\n'
                f'# {name}\n\n## Biography\n\n{profile_body if curated else "Prose."}\n',
                encoding='utf-8')
        for sid, blocks in self.sources.items():
            (self.root / 'sources' / 'census' / f'record-{sid}.md').write_text(
                f'---\nid: {sid}\ntitle: Record {sid}\nsource_type: census\n---\n\n'
                '## Claims\n```yaml\n' + '\n'.join(blocks) + '```\n', encoding='utf-8')
        index_mod.build_index(self.root, load_fha_yaml(self.root))

    def facts(self, kind: str | None = None) -> list[dict]:
        conn = open_index_db(self.root, ('persons', 'claims'))
        try:
            found = connected_facts(conn, ME)
        finally:
            conn.close()
        return [f for f in found if kind is None or f['kind'] == kind]

    def parents(self, subtype: str | None = None) -> str:
        return _claim('c-r000000001', 'relationship', [ME, FATHER, MOTHER],
                      {'child': [ME], 'parent': [FATHER, MOTHER]}, subtype=subtype or 'biological')

    def draft_queue(self, fmt: str = 'md') -> str:
        res = views.run_draft_queue(self.root, person_id=ME, fmt=fmt)
        return Path(res.changed[0]).read_text(encoding='utf-8')


class OrphanedTests(_Base):
    def _family(self, father_death: str, mother_death: str | None, birth: str = '1871-05-10') -> None:
        self.person(FATHER, 'Caleb Cole', 'M')
        self.person(MOTHER, 'Chastina Cole', 'F')
        blocks = [_claim('c-b000000001', 'birth', [ME], {'child': [ME]}, birth), self.parents()]
        self.claims('s-b000000001', *blocks)
        self.claims('s-d000000001', _claim('c-d000000001', 'death', [FATHER],
                                           {'deceased': [FATHER]}, father_death))
        if mother_death:
            self.claims('s-d000000002', _claim('c-d000000002', 'death', [MOTHER],
                                               {'deceased': [MOTHER]}, mother_death))

    def test_both_parents_dead_before_sixteen(self):
        self._family('1877', '1883-01')
        self.build()
        (fact,) = self.facts('orphaned')
        self.assertEqual(fact['sources'], ['s-b000000001', 's-d000000001', 's-d000000002'])
        line = fact['line_md']
        self.assertTrue(line.startswith('Orphaned by 11:'), line)
        self.assertIn('her father [[p-f000000001|Caleb Cole]] died 1877 [[s-d000000001]]', line)
        self.assertIn('her mother [[p-n000000001|Chastina Cole]] 1883-01 [[s-d000000002]]', line)
        self.assertIn('(born 1871-05-10 [[s-b000000001]])', line)
        self.assertEqual(self.facts('lost_parent'), [])

    def test_day_before_sixteenth_birthday_counts_the_day_itself_does_not(self):
        self._family('1877', '1887-05-09')
        self.build()
        self.assertEqual(len(self.facts('orphaned')), 1)
        self.setUp()
        self._family('1877', '1887-05-10')
        self.build()
        self.assertEqual(self.facts('orphaned'), [])
        (fact,) = self.facts('lost_parent')
        self.assertIn('[[p-f000000001|Caleb Cole]]', fact['line_md'])

    def test_one_parent_lost_says_which_and_how_old(self):
        self._family('1877', None, birth='1871')
        self.build()
        (fact,) = self.facts('lost_parent')
        self.assertTrue(fact['line_md'].startswith(
            'Lost her father [[p-f000000001|Caleb Cole]] at 5 or 6: died 1877 [[s-d000000001]]'),
            fact['line_md'])
        self.assertEqual(self.facts('orphaned'), [])

    def test_parent_dead_after_sixteen_gives_nothing(self):
        self._family('1890', '1895')
        self.build()
        self.assertEqual(self.facts(), [])

    def test_suggested_parent_death_does_not_count(self):
        self._family('1877', None)
        self.claims('s-d000000009', _claim('c-d000000009', 'death', [MOTHER], {'deceased': [MOTHER]},
                                           '1880', status='suggested'))
        self.build()
        self.assertEqual(self.facts('orphaned'), [])
        self.assertEqual(len(self.facts('lost_parent')), 1)

    def test_step_parent_is_not_a_lost_parent(self):
        self.person(FATHER, 'Caleb Cole', 'M')
        self.claims('s-b000000001', _claim('c-b000000001', 'birth', [ME], {'child': [ME]}, '1871'),
                    _claim('c-r000000001', 'relationship', [ME, FATHER],
                           {'child': [ME], 'parent': [FATHER]}, subtype='step'))
        self.claims('s-d000000001', _claim('c-d000000001', 'death', [FATHER],
                                           {'deceased': [FATHER]}, '1875'))
        self.build()
        self.assertEqual(self.facts(), [])

    def test_loose_birth_gives_no_age_but_still_a_fact(self):
        self._family('1877', None, birth='1871~')
        self.build()
        (fact,) = self.facts('lost_parent')
        self.assertIn('at under 16:', fact['line_md'])


class WidowedTests(_Base):
    def _couple(self, spouse_death: str = '1923') -> None:
        self.person(SPOUSE, 'Thomas Cole', 'M')
        self.claims('s-m000000001',
                    _claim('c-m000000001', 'marriage', [ME, SPOUSE], {'spouse': [ME, SPOUSE]}, '1893'),
                    _claim('c-b000000001', 'birth', [ME], {'child': [ME]}, '1871-03-02'))
        self.claims('s-d000000001', _claim('c-d000000001', 'death', [SPOUSE],
                                           {'deceased': [SPOUSE]}, spouse_death))

    def test_widowed_with_her_age(self):
        self._couple()
        self.claims('s-c000000001', _claim('c-c000000001', 'census', [ME], None, '1930'))
        self.build()
        (fact,) = self.facts('widowed')
        self.assertTrue(fact['line_md'].startswith('Widowed at 51 or 52: [[p-h000000001|Thomas Cole]] '
                                                   'died 1923 [[s-d000000001]]'), fact['line_md'])

    def test_not_widowed_when_she_died_first(self):
        self._couple()
        self.claims('s-d000000002', _claim('c-d000000002', 'death', [ME], {'deceased': [ME]}, '1920'))
        self.build()
        self.assertEqual(self.facts('widowed'), [])

    def test_not_widowed_after_a_divorce(self):
        self._couple()
        self.claims('s-c000000001', _claim('c-c000000001', 'census', [ME], None, '1930'),
                    _claim('c-v000000001', 'divorce', [ME, SPOUSE], {'spouse': [ME, SPOUSE]}, '1905'))
        self.build()
        self.assertEqual(self.facts('widowed'), [])

    def test_no_later_record_for_her_says_nothing(self):
        self._couple()
        self.build()
        self.assertEqual(self.facts('widowed'), [])


class SiblingDayTests(_Base):
    def _pair(self, mine: str, theirs: str, ctype: str = 'birth', half: bool = False) -> None:
        self.person(FATHER, 'Caleb Cole', 'M')
        self.person(MOTHER, 'Chastina Cole', 'F')
        self.person(SIB, 'James Cole', 'M')
        role = 'child'
        sib_parents = [FATHER] if half else [FATHER, MOTHER]
        self.claims('s-r000000001',
                    _claim('c-r000000001', 'relationship', [ME, FATHER, MOTHER],
                           {'child': [ME], 'parent': [FATHER, MOTHER]}, subtype='biological'),
                    _claim('c-r000000002', 'relationship', [SIB] + sib_parents,
                           {'child': [SIB], 'parent': sib_parents}, subtype='biological'))
        self.claims('s-a000000001', _claim('c-a000000001', ctype, [ME], {role: [ME]}, mine))
        self.claims('s-a000000002', _claim('c-a000000002', ctype, [SIB], {role: [SIB]}, theirs))

    def test_half_brother_is_called_half(self):
        self._pair('1875-03-02', '1875-03-02', half=True)
        self.build()
        (fact,) = self.facts('same_birthday')
        self.assertIn('her half-brother [[p-b000000001|James Cole]]', fact['line_md'])
        self.assertNotIn('twins', fact['line_md'])

    def test_day_level_guess_never_matches(self):
        self._pair('1875-03-02~', '1875-03-02')
        self.build()
        self.assertEqual(self.facts('same_birthday'), [])

    def test_same_birthday(self):
        self._pair('1875-03-02', '1875-03-02')
        self.build()
        (fact,) = self.facts('same_birthday')
        self.assertEqual(fact['line_md'],
                         'Born the same day as her brother [[p-b000000001|James Cole]] (twins?): '
                         '1875-03-02 [[s-a000000001]] [[s-a000000002]].')
        self.assertEqual(fact['sources'], ['s-a000000001', 's-a000000002'])

    def test_one_day_apart_is_twins_across_midnight_two_is_not(self):
        self._pair('1875-03-02', '1875-03-03')
        self.build()
        (fact,) = self.facts('same_birthday')
        self.assertIn('Born within a day of her brother', fact['line_md'])
        self.setUp()
        self._pair('1875-03-02', '1875-03-04')
        self.build()
        self.assertEqual(self.facts('same_birthday'), [])

    def test_loose_birth_dates_never_match(self):
        self._pair('1875-03', '1875-03')
        self.build()
        self.assertEqual(self.facts('same_birthday'), [])

    def test_same_day_baptism(self):
        self._pair('1880-06-12', '1880-06-12', ctype='baptism')
        self.build()
        (fact,) = self.facts('same_day_baptism')
        self.assertIn('Baptised the same day as her brother [[p-b000000001|James Cole]], 1880-06-12',
                      fact['line_md'])

    def test_baptisms_a_day_apart_are_not_the_same_day(self):
        self._pair('1880-06-12', '1880-06-13', ctype='baptism')
        self.build()
        self.assertEqual(self.facts('same_day_baptism'), [])


class BuriedTogetherTests(_Base):
    def _couple(self, hers: str, his: str) -> None:
        self.person(SPOUSE, 'Thomas Cole', 'M')
        self.claims('s-m000000001',
                    _claim('c-m000000001', 'marriage', [ME, SPOUSE], {'spouse': [ME, SPOUSE]}, '1893'))
        self.claims('s-u000000001', _claim('c-u000000001', 'burial', [ME], {'deceased': [ME]}, '1930',
                                           place=hers))
        self.claims('s-u000000002', _claim('c-u000000002', 'burial', [SPOUSE], {'deceased': [SPOUSE]},
                                           '1923', place=his))

    def test_same_registered_place(self):
        self._couple(PLACE, PLACE)
        self.build()
        (fact,) = self.facts('buried_with_spouse')
        self.assertEqual(fact['line_md'],
                         'Buried in the same place as her husband [[p-h000000001|Thomas Cole]]: '
                         '[[l-c000000001|Riverside Cemetery]] [[s-u000000001]] [[s-u000000002]].')

    def test_free_text_places_never_match(self):
        self.person(SPOUSE, 'Thomas Cole', 'M')
        self.claims('s-m000000001',
                    _claim('c-m000000001', 'marriage', [ME, SPOUSE], {'spouse': [ME, SPOUSE]}, '1893'),
                    _claim('c-u000000001', 'burial', [ME], {'deceased': [ME]}, '1930'),
                    _claim('c-u000000002', 'burial', [SPOUSE], {'deceased': [SPOUSE]}, '1923'))
        self.build()
        self.assertEqual(self.facts('buried_with_spouse'), [])


class ChildrenAtDeathTests(_Base):
    def _children(self, kid3_living: str = 'true') -> None:
        self.person(SPOUSE, 'Thomas Cole', 'M')
        self.person(KID1, 'James Cole', 'M')
        self.person(KID2, 'Ethel Cole', 'F')
        self.person(KID3, 'Private Child', 'F', living=kid3_living)
        kids = [_claim(f'c-r00000000{i}', 'relationship', [kid, ME, SPOUSE],
                       {'child': [kid], 'parent': [ME, SPOUSE]}, subtype='biological')
                for i, kid in enumerate((KID1, KID2, KID3), start=1)]
        self.claims('s-r000000001', *kids)
        self.claims('s-d000000001', _claim('c-d000000001', 'death', [ME], {'deceased': [ME]}, '1923'))
        self.claims('s-d000000002', _claim('c-d000000002', 'death', [KID1], {'deceased': [KID1]}, '1880'))
        self.claims('s-d000000003', _claim('c-d000000003', 'death', [KID2], {'deceased': [KID2]}, '1930'))

    def test_counts_before_after_and_unplaced(self):
        self._children()
        self.build()
        (fact,) = self.facts('children_at_death')
        line = fact['line_md']
        self.assertTrue(line.startswith('At her death in 1923 [[s-d000000001]], 1 of her 3 recorded '
                                        'children had died before her ([[p-k000000001|James Cole]] '
                                        '1880 [[s-d000000002]]); 1 outlived her ([[p-k000000002|Ethel '
                                        'Cole]] 1930 [[s-d000000003]]); 1 has no death recorded'), line)
        self.assertEqual(fact['sources'], ['s-d000000001', 's-d000000002', 's-d000000003'])

    def test_a_living_child_is_counted_never_named_or_cited(self):
        self._children()
        self.claims('s-d000000004', _claim('c-d000000004', 'death', [KID3], {'deceased': [KID3]}, '1900'))
        self.build()
        (fact,) = self.facts('children_at_death')
        self.assertNotIn('Private Child', fact['line_md'])
        self.assertNotIn('p-k000000003', fact['line_md'])
        self.assertNotIn('s-d000000004', fact['line_md'])
        self.assertNotIn('s-d000000004', fact['sources'])
        self.assertIn('1 living or unknown, not named here', fact['line_md'])
        self.assertIn('2 of her 3 recorded children had died before her', fact['line_md'])

    def test_no_death_for_the_person_gives_no_line(self):
        self._children()
        self.sources.pop('s-d000000001')
        self.build()
        self.assertEqual(self.facts('children_at_death'), [])


class DraftQueueSectionTests(_Base):
    def _orphan(self) -> None:
        self.person(FATHER, 'Caleb Cole', 'M')
        self.claims('s-b000000001', _claim('c-b000000001', 'birth', [ME], {'child': [ME]}, '1871'),
                    self.parents())
        self.claims('s-d000000001', _claim('c-d000000001', 'death', [FATHER],
                                           {'deceased': [FATHER]}, '1877'))

    def test_section_lists_the_fact_with_every_source(self):
        self._orphan()
        self.build()
        text = self.draft_queue()
        self.assertIn('## Connected facts', text)
        self.assertIn('- Lost her father [[p-f000000001|Caleb Cole]] at 5 or 6: died 1877 '
                      '[[s-d000000001]] (born 1871 [[s-b000000001]]).', text)

    def test_html_twin_carries_the_same_line(self):
        self._orphan()
        self.build()
        text = self.draft_queue('html')
        self.assertIn('<h2>Connected facts</h2>', text)
        self.assertIn('Lost her father', text)
        self.assertIn('s-d000000001', text)

    def test_fact_whose_sources_are_all_cited_is_left_out(self):
        self._orphan()
        self.build('She was born [[s-b000000001]]; her father died [[s-d000000001]].')
        text = self.draft_queue()
        self.assertNotIn('Lost her father', text)
        self.assertNotIn('## Connected facts', text)

    def test_one_uncited_source_keeps_the_whole_fact(self):
        self._orphan()
        self.build('She was born [[s-b000000001]].')
        self.assertIn('Lost her father', self.draft_queue())

    def test_section_appears_when_every_claim_is_cited_but_a_fact_source_is_not(self):
        # Claims are cited by their source token; the fact needs BOTH sources
        # cited to drop, so citing one leaves the queue's own list empty of
        # that source while the fact still shows.
        self._orphan()
        self.build('Born [[s-b000000001]].')
        text = self.draft_queue()
        self.assertIn('## Connected facts', text)

    def test_empty_queue_with_no_facts_is_unchanged(self):
        self.claims('s-b000000001', _claim('c-b000000001', 'birth', [ME], {'child': [ME]}, '1871'))
        self.build('Born [[s-b000000001]].')
        text = self.draft_queue()
        self.assertIn('All accepted claims are cited in the profile.', text)
        self.assertNotIn('Connected facts', text)


if __name__ == '__main__':
    unittest.main()
