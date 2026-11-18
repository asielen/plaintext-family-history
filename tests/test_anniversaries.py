"""
test_anniversaries.py - fha calendar: family events by month and day
(TOOLING §7b).

The engine (`_lib.family_calendar_events`) and the command
(`anniversaries.run_calendar`) against tiny temp archives. Expected answers
come from the rules TOOLING §7b states: only dates recorded to the month or
day are placed; a vital event belongs to its subject, never to the parent or
spouse a record also names; one event per person, type and date however many
sources record it; negated and unaccepted claims stay off by default. Temp
fixtures throughout; never the real archive or example-archive.
"""

import datetime
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

import anniversaries
import index as index_mod
from _lib import (
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    calendar_date_parts,
    family_calendar_events,
    load_fha_yaml,
)

KID = 'p-aaaaaaaaaa'
MOM = 'p-bbbbbbbbbb'
DAD = 'p-cccccccccc'
TODAY = datetime.date(2026, 6, 14)


class _Archive:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / 'people' / 'stubs').mkdir(parents=True)
        (self.root / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
        for pid, name, living in ((KID, 'Kid', 'false'), (MOM, 'Mom', 'false'),
                                  (DAD, 'Dad', 'true')):
            meta = {'id': pid, 'name': name, 'living': living, 'tier': 'stub'}
            (self.root / 'people' / 'stubs' / f'p__{pid[2:]}_{pid}.md').write_text(
                '---\n' + yaml.safe_dump(meta) + f'---\n\n# {name}\n', encoding='utf-8')
        self._n = 0

    def source(self, claims: list[dict], **meta) -> str:
        self._n += 1
        sid = f's-{self._n:010d}'
        folder = self.root / 'sources' / 'vital-record'
        folder.mkdir(parents=True, exist_ok=True)
        full = []
        for i, c in enumerate(claims):
            c = {'value': c['type'], 'id': f'c-{self._n:05d}{i:05d}', 'status': 'accepted',
                 'reviewed': '2026-01-01', 'confidence': 'high', **c}
            if c['status'] != 'accepted' and c.get('reviewed') == '2026-01-01':
                c.pop('reviewed')
            full.append(c)
        head = {'id': sid, 'title': f'record {self._n}', 'source_type': 'vital-record', **meta}
        (folder / f'r_{sid}.md').write_text(
            '---\n' + yaml.safe_dump(head) + '---\n\n## Claims\n```yaml\n'
            + yaml.safe_dump(full, sort_keys=False) + '```\n', encoding='utf-8')
        return sid

    def build(self) -> None:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            index_mod.build_index(self.root, load_fha_yaml(self.root))

    def events(self, month, day=None, statuses=('accepted',)):
        self.build()
        conn = sqlite3.connect(self.root / '.cache' / 'index.sqlite')
        conn.row_factory = sqlite3.Row
        try:
            return family_calendar_events(conn, month=month, day=day, statuses=statuses,
                                          this_year=2026)
        finally:
            conn.close()


class DatePartsTests(unittest.TestCase):
    def test_month_and_day_forms(self):
        self.assertEqual(calendar_date_parts('1880-06-14'), (1880, 6, 14))
        self.assertEqual(calendar_date_parts('1880-06'), (1880, 6, None))

    def test_unsure_dates_are_not_placed(self):
        for d in ('1880', '1880-06~', '1880-06-14?', '1880/1881', '188X', '', None,
                  '1880-13', '1880-02-30'):
            self.assertIsNone(calendar_date_parts(d), d)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.a = _Archive()

    def tearDown(self):
        shutil.rmtree(self.a.root, ignore_errors=True)

    def test_birth_on_its_day(self):
        self.a.source([{'type': 'birth', 'persons': [KID], 'date': '1880-06-14'}])
        ev = self.a.events(6, 14)
        self.assertEqual([(e['word'], e['year'], e['years_ago']) for e in ev],
                         [('Born', 1880, 146)])

    def test_month_view_includes_month_only_dates_day_view_does_not(self):
        self.a.source([{'type': 'death', 'persons': [KID], 'date': '1930-06'}])
        self.assertEqual(len(self.a.events(6)), 1)
        self.assertEqual(self.a.events(6, 1), [])

    def test_parent_named_on_a_birth_record_is_not_born_that_day(self):
        self.a.source([{'type': 'birth', 'persons': [KID, MOM], 'date': '1880-06-14',
                        'roles': {'child': KID, 'parent': [MOM]}}])
        (ev,) = self.a.events(6)
        self.assertEqual([p['id'] for p in ev['people']], [KID])

    def test_marriage_names_the_couple(self):
        self.a.source([{'type': 'marriage', 'persons': [MOM, DAD, KID], 'date': '1870-06-01',
                        'roles': {'spouse': [MOM, DAD], 'witness': [KID]}}])
        (ev,) = self.a.events(6)
        self.assertEqual(sorted(p['id'] for p in ev['people']), [MOM, DAD])

    def test_same_event_from_two_sources_is_one_event(self):
        self.a.source([{'type': 'birth', 'persons': [KID], 'date': '1880-06-14'}])
        self.a.source([{'type': 'birth', 'persons': [KID], 'date': '1880-06-14'}],
                      restricted=True)
        (ev,) = self.a.events(6)
        self.assertEqual(len(ev['claims']), 2)
        self.assertEqual(len(ev['sources']), 2)
        # One restricted source behind it is enough to keep it off shared output.
        self.assertTrue(ev['source_restricted'])

    def test_suggested_and_negated_claims(self):
        self.a.source([
            {'type': 'birth', 'persons': [KID], 'date': '1880-06-14', 'status': 'suggested'},
            {'type': 'death', 'persons': [MOM], 'date': '1900-06-02', 'negated': True,
             'evidence': 'negative'}])
        self.assertEqual(self.a.events(6), [])
        found = self.a.events(6, statuses=('accepted', 'suggested'))
        self.assertEqual([e['type'] for e in found], ['birth'])

    def test_include_suggested_marks_suggested_only_events(self):
        self.a.source([
            {'type': 'birth', 'persons': [KID], 'date': '1880-06-14'},
            {'type': 'death', 'persons': [MOM], 'date': '1900-06-02', 'status': 'suggested'}])
        events = self.a.events(6, statuses=('accepted', 'suggested'))
        self.assertEqual({e['type']: e['accepted'] for e in events},
                         {'birth': True, 'death': False})

    def test_an_accepted_copy_makes_the_event_accepted(self):
        self.a.source([{'type': 'birth', 'persons': [KID], 'date': '1880-06-14',
                        'status': 'suggested'}])
        self.a.source([{'type': 'birth', 'persons': [KID], 'date': '1880-06-14'}])
        (ev,) = self.a.events(6, statuses=('accepted', 'suggested'))
        self.assertTrue(ev['accepted'])

    def test_living_flag_travels_with_each_person(self):
        self.a.source([{'type': 'birth', 'persons': [DAD], 'date': '1960-06-03'},
                       {'type': 'birth', 'persons': [KID], 'date': '1880-06-04'}])
        living = {e['people'][0]['id']: e['people'][0]['living'] for e in self.a.events(6)}
        self.assertEqual(living, {DAD: True, KID: False})

    def test_living_follows_a_merge_as_exports_read_it(self):
        # A record merged into Kid says living: true; the survivor counts as living.
        meta = {'id': 'p-dddddddddd', 'name': 'Kid Dup', 'living': 'true', 'tier': 'stub',
                'status': 'merged', 'merged_into': KID}
        (self.a.root / 'people' / 'stubs' / 'p__dup_p-dddddddddd.md').write_text(
            '---\n' + yaml.safe_dump(meta) + '---\n\n# Kid Dup\n', encoding='utf-8')
        self.a.source([{'type': 'birth', 'persons': [KID], 'date': '1990-06-14'}])
        (ev,) = self.a.events(6)
        self.assertIs(ev['people'][0]['living'], True)

    def test_month_only_record_is_left_off_beside_its_dated_record(self):
        # Nothing from the month-only record joins the dated line.
        self.a.source([{'type': 'birth', 'persons': [KID], 'date': '1880-06'}])
        dated = self.a.source([{'type': 'birth', 'persons': [KID], 'date': '1880-06-14'}])
        (ev,) = self.a.events(6)
        self.assertEqual((ev['day'], ev['sources']), (14, [dated]))

    def test_month_only_record_of_someone_else_stays(self):
        self.a.source([{'type': 'birth', 'persons': [MOM], 'date': '1880-06'}])
        self.a.source([{'type': 'birth', 'persons': [KID], 'date': '1880-06-14'}])
        self.assertEqual(len(self.a.events(6)), 2)

    def test_a_suggested_line_carries_no_accepted_claim(self):
        # The [suggested] marker says the line rests only on suggested claims;
        # an accepted month-only record must not be folded in behind it.
        self.a.source([{'type': 'birth', 'persons': [KID], 'date': '1880-06'}])
        self.a.source([{'type': 'birth', 'persons': [KID], 'date': '1880-06-14',
                        'status': 'suggested'}])
        events = self.a.events(6, statuses=('accepted', 'suggested'))
        conn = sqlite3.connect(self.a.root / '.cache' / 'index.sqlite')
        try:
            status = dict(conn.execute('SELECT id, status FROM claims'))
        finally:
            conn.close()
        self.assertTrue(events)
        for ev in events:
            self.assertEqual(ev['accepted'],
                             any(status[c] == 'accepted' for c in ev['claims']), ev)

    def test_an_accepted_month_does_not_make_a_suggested_day_accepted(self):
        self.a.source([{'type': 'birth', 'persons': [KID], 'date': '1880-06'}])
        self.a.source([{'type': 'birth', 'persons': [KID], 'date': '1880-06-14',
                        'status': 'suggested'}])
        (ev,) = self.a.events(6, statuses=('accepted', 'suggested'))
        self.assertEqual((ev['day'], ev['accepted']), (14, False))

    def test_an_accepted_event_shows_no_unreviewed_place(self):
        self.a.source([{'type': 'birth', 'persons': [KID], 'date': '1880-06-14'}])
        self.a.source([{'type': 'birth', 'persons': [KID], 'date': '1880-06-14',
                        'status': 'suggested', 'place_text': 'Unreviewed Town'}])
        (ev,) = self.a.events(6, statuses=('accepted', 'suggested'))
        self.assertEqual((ev['accepted'], ev['place']), (True, None))

    def test_a_month_only_suggestion_lends_no_place_to_an_accepted_day(self):
        self.a.source([{'type': 'birth', 'persons': [KID], 'date': '1880-06-14'}])
        self.a.source([{'type': 'birth', 'persons': [KID], 'date': '1880-06',
                        'status': 'suggested', 'place_text': 'Unreviewed Town'}])
        (ev,) = self.a.events(6, statuses=('accepted', 'suggested'))
        self.assertEqual(ev['place'], None)

    def test_a_suggested_only_event_keeps_its_place(self):
        self.a.source([{'type': 'birth', 'persons': [KID], 'date': '1880-06-14',
                        'status': 'suggested', 'place_text': 'Elm Creek'}])
        (ev,) = self.a.events(6, statuses=('accepted', 'suggested'))
        self.assertEqual((ev['accepted'], ev['place']), (False, 'Elm Creek'))

    def test_month_only_record_is_left_off_beside_two_different_days(self):
        self.a.source([{'type': 'birth', 'persons': [KID], 'date': '1880-06'}])
        self.a.source([{'type': 'birth', 'persons': [KID], 'date': '1880-06-14'}])
        self.a.source([{'type': 'birth', 'persons': [KID], 'date': '1880-06-15'}])
        self.assertEqual([e['day'] for e in self.a.events(6)], [14, 15])

    def test_a_restricted_claim_is_listed_but_not_flagged(self):
        # The index records `restricted:` only on sources, so a restricted
        # claim reaches the calendar unflagged; a shared front door must apply
        # SPEC 19 itself (TOOLING 7b, Privacy).
        self.a.source([{'type': 'birth', 'persons': [KID], 'date': '1880-06-14',
                        'restricted': True}])
        (ev,) = self.a.events(6)
        self.assertFalse(ev['source_restricted'])

    def test_arrival_of_a_household_names_the_subjects(self):
        self.a.source([{'type': 'immigration', 'persons': [MOM, KID], 'date': '1905-06-20',
                        'roles': {'subject': [MOM, KID]}}])
        (ev,) = self.a.events(6)
        self.assertEqual((ev['word'], sorted(p['id'] for p in ev['people'])),
                         ('Arrived', sorted([MOM, KID])))

    def test_arrival_leaves_off_a_relative_with_a_role(self):
        self.a.source([{'type': 'immigration', 'persons': [MOM, KID], 'date': '1905-06-20',
                        'roles': {'relative': [KID]}}])
        (ev,) = self.a.events(6)
        self.assertEqual([p['id'] for p in ev['people']], [MOM])

    def test_arrival_of_an_unroled_emigrant_with_a_sponsor(self):
        self.a.source([{'type': 'immigration', 'persons': [MOM, DAD], 'date': '1905-06-20',
                        'roles': {'sponsor': [DAD]}}])
        (ev,) = self.a.events(6)
        self.assertEqual([p['id'] for p in ev['people']], [MOM])

    def test_arrival_where_everyone_has_a_role_but_none_is_subject_is_no_event(self):
        self.a.source([{'type': 'immigration', 'persons': [MOM, DAD], 'date': '1905-06-20',
                        'roles': {'sponsor': [DAD], 'relative': [MOM]}}])
        self.assertEqual(self.a.events(6), [])

    def test_arrival_with_no_roles_is_everyone(self):
        self.a.source([{'type': 'immigration', 'persons': [MOM, KID], 'date': '1905-06-20'}])
        (ev,) = self.a.events(6)
        self.assertEqual(sorted(p['id'] for p in ev['people']), sorted([MOM, KID]))

    def test_ordered_by_day_then_year(self):
        self.a.source([{'type': 'birth', 'persons': [KID], 'date': '1900-06-20'},
                       {'type': 'death', 'persons': [MOM], 'date': '1890-06-20'},
                       {'type': 'birth', 'persons': [MOM], 'date': '1850-06-25'}])
        self.assertEqual([(e['day'], e['year']) for e in self.a.events(6)],
                         [(20, 1890), (20, 1900), (25, 1850)])


class ParseWhenTests(unittest.TestCase):
    def p(self, s, week=False):
        return anniversaries.parse_when(s, today=TODAY, week=week)

    def test_forms(self):
        self.assertEqual(self.p(None), [(6, None)])
        self.assertEqual(self.p('today'), [(6, 14)])
        for s in ('june', 'Jun', '6', '06'):
            self.assertEqual(self.p(s), [(6, None)], s)
        for s in ('june 14', '14 june', '06-14', '6/14', 'June 14th', '14th of june'.replace(' of', '')):
            self.assertEqual(self.p(s), [(6, 14)], s)
        self.assertEqual(self.p('february 29'), [(2, 29)])

    def test_unreadable(self):
        for s in ('13', 'smarch', '13th', '02-30', 'june 14 1880'):
            self.assertIsNone(self.p(s), s)

    def test_week_follows_this_years_february(self):
        # 2026 is not a leap year: no 29 February, and the week ends 3 March.
        week = anniversaries.parse_when(None, today=datetime.date(2026, 2, 25), week=True)
        self.assertNotIn((2, 29), week)
        self.assertEqual(week[-1], (3, 3))
        leap = anniversaries.parse_when(None, today=datetime.date(2028, 2, 25), week=True)
        self.assertIn((2, 29), leap)

    def test_week_crosses_the_month(self):
        self.assertEqual(self.p('june 28', week=True)[-1], (7, 4))
        self.assertEqual(len(self.p(None, week=True)), 7)
        self.assertIsNone(self.p('june', week=True))


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.a = _Archive()
        self.a.source([{'type': 'birth', 'persons': [KID], 'date': '1880-06-14'}])
        self.a.build()

    def tearDown(self):
        shutil.rmtree(self.a.root, ignore_errors=True)

    def run_(self, **kw):
        return anniversaries.run_calendar(self.a.root, {}, today=TODAY, **kw)

    def test_today(self):
        r = self.run_(when='today')
        self.assertEqual(r.exit_code, EXIT_CLEAN)
        self.assertEqual(len(r.data['groups'][0]['events']), 1)

    def test_week_across_new_year_counts_january_against_next_year(self):
        self.a.source([{'type': 'birth', 'persons': [MOM], 'date': '1880-01-02'}])
        self.a.build()
        r = anniversaries.run_calendar(self.a.root, {}, week=True,
                                       today=datetime.date(2026, 12, 28))
        (ev,) = [e for g in r.data['groups'] for e in g['events'] if e['month'] == 1]
        self.assertEqual(ev['years_ago'], 147)

    def test_week_has_seven_days(self):
        self.assertEqual(len(self.run_(week=True).data['groups']), 7)

    def test_bad_date_exit_2_names_examples(self):
        r = self.run_(when='smarch')
        self.assertEqual(r.exit_code, EXIT_ERRORS)
        self.assertIn('fha calendar june', r.messages[0].text)

    def test_a_month_with_week_asks_for_a_day_not_a_readable_month(self):
        r = self.run_(when='june', week=True)
        self.assertEqual(r.exit_code, EXIT_ERRORS)
        text = r.messages[0].text
        self.assertNotIn('could not read', text)
        self.assertIn('needs a day to start from', text)
        self.assertIn('fha calendar june 14 --week', text)

    def test_unreadable_words_with_week_still_say_unreadable(self):
        r = self.run_(when='smarch', week=True)
        self.assertIn('could not read', r.messages[0].text)

    def test_years_ago_reads_this_year_and_flags_a_future_date(self):
        self.a.source([{'type': 'birth', 'persons': [MOM], 'date': '2026-06-01'},
                       {'type': 'birth', 'persons': [DAD], 'date': '2030-06-02'}])
        self.a.build()
        out = io.StringIO()
        with redirect_stdout(out):
            anniversaries._cmd_calendar(self.run_())
        lines = out.getvalue().splitlines()
        (now,) = [ln for ln in lines if '2026' in ln and 'Born' in ln]
        (future,) = [ln for ln in lines if '2030' in ln]
        self.assertIn('(this year)', now)
        self.assertIn('(dated in the future - check the record)', future)
        self.assertNotIn('years ago', future)

    def test_no_index_exit_3(self):
        (self.a.root / '.cache' / 'index.sqlite').unlink()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_().exit_code, EXIT_FAILURE)

    def test_text_and_json(self):
        out = io.StringIO()
        with redirect_stdout(out):
            anniversaries._cmd_calendar(self.run_())
        self.assertIn('Born', out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            anniversaries._cmd_calendar(self.run_(), use_json=True)
        self.assertEqual(json.loads(out.getvalue())['data']['groups'][0]['month'], 6)

    def test_include_suggested_text_labels_a_suggested_line(self):
        self.a.source([{'type': 'death', 'persons': [MOM], 'date': '1900-06-02',
                        'status': 'suggested'}])
        self.a.build()
        out = io.StringIO()
        with redirect_stdout(out):
            anniversaries._cmd_calendar(self.run_(include_suggested=True))
        lines = [ln for ln in out.getvalue().splitlines() if 'Died' in ln or 'Born' in ln]
        self.assertTrue(any('[suggested' in ln for ln in lines if 'Died' in ln))
        self.assertFalse(any('[suggested' in ln for ln in lines if 'Born' in ln))

    def test_include_suggested_footer_agrees_with_the_marked_line(self):
        # An accepted "June 1900" beside an unreviewed "2 June 1900": the
        # day's line is marked, and the footer's promise that a marked line
        # rests only on suggested claims holds for it.
        self.a.source([{'type': 'death', 'persons': [MOM], 'date': '1900-06'}])
        self.a.source([{'type': 'death', 'persons': [MOM], 'date': '1900-06-02',
                        'status': 'suggested'}])
        self.a.build()
        r = self.run_(include_suggested=True)
        (ev,) = [e for e in r.data['groups'][0]['events'] if e['type'] == 'death']
        conn = sqlite3.connect(self.a.root / '.cache' / 'index.sqlite')
        try:
            statuses = {conn.execute('SELECT status FROM claims WHERE id=?', (c,)).fetchone()[0]
                        for c in ev['claims']}
        finally:
            conn.close()
        self.assertEqual(statuses, {'suggested'})
        out = io.StringIO()
        with redirect_stdout(out):
            anniversaries._cmd_calendar(r)
        text = out.getvalue()
        (died,) = [ln for ln in text.splitlines() if 'Died' in ln]
        self.assertIn('[suggested, not yet reviewed]', died)
        self.assertIn('rests only on suggested claims', text)

    def test_cli_flag_is_include_suggested(self):
        self.a.source([{'type': 'death', 'persons': [MOM], 'date': '1900-06-02',
                        'status': 'suggested'}])
        self.a.build()
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            anniversaries._standalone_main(['june', '--include-suggested',
                                            '--root', str(self.a.root)])
        self.assertIn('Died', out.getvalue())

    def test_empty_month_explains_what_can_be_placed(self):
        out = io.StringIO()
        with redirect_stdout(out):
            anniversaries._cmd_calendar(self.run_(when='january'))
        self.assertIn('a year alone', out.getvalue())


if __name__ == '__main__':
    unittest.main()
