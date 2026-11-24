"""
test_dates.py - fha date: dates in other calendars (TOOLING §4b).

Known answers come from outside this code: the published calendar switch
dates (Catholic Europe 1582, Britain 1752, Russia 1918), Easter tables, and
hebcal.com for the Hebrew dates (1 Tishrei 5600 = 9 September 1839,
15 Nisan 5650 = 5 April 1890, 30 Cheshvan 5630 = 4 November 1869). The
property tests need no outside source: round trips are the identity, and
the Hebrew calendar's own rules (year lengths, the days 1 Tishrei may not
fall on) hold for every year checked. No archive is used: the command is
pure arithmetic.
"""

import datetime
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import dates
from _lib import EXIT_CLEAN, EXIT_ERRORS


def _julian_to_gregorian(y, m, d):
    return datetime.date.fromordinal(dates.julian_to_ordinal(y, m, d)).isoformat()


def _run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = dates._standalone_main(list(argv))
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


class JulianGregorianTests(unittest.TestCase):
    """The old calendar against today's, at the dates history fixed."""

    VECTORS = [
        ((1582, 10, 5), '1582-10-15'),   # the first Gregorian day
        ((1752, 9, 3), '1752-09-14'),    # Britain's switch
        ((1732, 2, 11), '1732-02-22'),
        ((1642, 12, 25), '1643-01-04'),
        ((1616, 4, 23), '1616-05-03'),
        ((1700, 2, 29), '1700-03-11'),   # a day only the old calendar has
        ((1917, 10, 25), '1917-11-07'),
        ((1918, 2, 1), '1918-02-14'),    # Russia's switch
    ]

    def test_known_switch_dates(self):
        for (y, m, d), want in self.VECTORS:
            with self.subTest(julian=(y, m, d)):
                self.assertEqual(_julian_to_gregorian(y, m, d), want)

    def test_weekdays_across_the_1582_switch(self):
        # Thursday 4 October 1582 (old) was followed by Friday 15 October (new).
        self.assertEqual(datetime.date.fromordinal(
            dates.julian_to_ordinal(1582, 10, 4)).strftime('%A'), 'Thursday')
        self.assertEqual(datetime.date(1582, 10, 15).strftime('%A'), 'Friday')

    def test_gregorian_back_to_julian(self):
        self.assertEqual(dates.ordinal_to_julian(datetime.date(1732, 2, 22).toordinal()),
                         (1732, 2, 11))

    def test_round_trip_every_day_1800_to_2100(self):
        start = datetime.date(1800, 1, 1).toordinal()
        end = datetime.date(2100, 12, 31).toordinal()
        for n in range(start, end + 1):
            self.assertEqual(dates.julian_to_ordinal(*dates.ordinal_to_julian(n)), n)

    def test_gap_grows_by_century(self):
        self.assertEqual(dates.calendar_gap_days(1700, 2, 28), 10)
        self.assertEqual(dates.calendar_gap_days(1700, 2, 29), 10)
        self.assertEqual(dates.calendar_gap_days(1700, 3, 1), 11)
        self.assertEqual(dates.calendar_gap_days(1732, 2, 11), 11)
        self.assertEqual(dates.calendar_gap_days(1880, 10, 1), 12)
        self.assertEqual(dates.calendar_gap_days(1918, 2, 1), 13)

    def test_a_day_the_old_calendar_lacks_is_refused(self):
        with self.assertRaises(ValueError):
            dates.julian_to_ordinal(1701, 2, 29)
        with self.assertRaises(ValueError):
            dates.julian_to_ordinal(1732, 2, 30)


class EasterTests(unittest.TestCase):

    def test_western_easter(self):
        for year, want in ((1818, '03-22'), (1943, '04-25'), (2000, '04-23'),
                           (2024, '03-31'), (2025, '04-20')):
            with self.subTest(year=year):
                self.assertEqual(dates.western_easter(year).isoformat(), f'{year}-{want}')

    def test_orthodox_easter(self):
        self.assertEqual(dates.orthodox_easter(1900), (1900, 4, 9))
        self.assertEqual(_julian_to_gregorian(*dates.orthodox_easter(1900)), '1900-04-22')
        self.assertEqual(_julian_to_gregorian(*dates.orthodox_easter(2024)), '2024-05-05')
        self.assertEqual(_julian_to_gregorian(*dates.orthodox_easter(2025)), '2025-04-20')


class FeastTests(unittest.TestCase):

    def _date(self, name, year, church='western'):
        result = dates.run_feast(name, year, church)
        self.assertTrue(result.ok, result.data)
        return result.data['date']

    def test_western_movable_feasts_2024(self):
        self.assertEqual(self._date('Ash Wednesday', 2024), '2024-02-14')
        self.assertEqual(self._date('Pentecost', 2024), '2024-05-19')
        self.assertEqual(self._date('Corpus Christi', 2024), '2024-05-30')

    def test_pokrov_1880_orthodox(self):
        result = dates.run_feast('Pokrov', 1880, 'orthodox')
        self.assertEqual(result.data['julian'], '1880-10-01')
        self.assertEqual(result.data['gregorian'], '1880-10-13')
        self.assertEqual(result.data['date'], '1880-10-13')
        self.assertEqual(result.data['weekday'], 'Wednesday')

    def test_michaelmas_differs_by_church(self):
        self.assertEqual(self._date('Michaelmas', 1850), '1850-09-29')
        # Orthodox: 8 November in the old calendar = 20 November in 1850.
        self.assertEqual(self._date('Michaelmas', 1850, 'orthodox'), '1850-11-20')

    def test_trinity_differs_by_church(self):
        # Western Trinity Sunday is a week after Pentecost; Orthodox Trinity is Pentecost.
        self.assertEqual(self._date('Trinity Sunday', 2024), '2024-05-26')
        self.assertEqual(self._date('Trinity', 2024, 'orthodox'), '2024-06-23')

    def test_names_are_matched_exactly_after_folding(self):
        for typed in ('St. Stephen\'s Day', 'Saint Stephen', 'st stephen', 'on St Stephen'):
            with self.subTest(typed=typed):
                self.assertEqual(self._date(typed, 1850), '1850-12-26')
        self.assertEqual(self._date('Świętego Jana', 1850), '1850-06-24')
        self.assertEqual(self._date('на Покров', 1880, 'orthodox'), '1880-10-13')
        # A near miss is not guessed.
        self.assertFalse(dates.run_feast('Michaelmass', 1850).ok)
        self.assertFalse(dates.run_feast('St Swithin', 1850).ok)

    def test_unknown_feast_lists_the_known_ones(self):
        result = dates.run_feast('St Swithin', 1850)
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertIn('Candlemas', result.data['error'])
        self.assertIn('fha date convert', result.data['error'])

    def test_feast_of_the_other_church_says_which(self):
        result = dates.run_feast('Pokrov', 1880, 'western')
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertIn('--church orthodox', result.data['example'])

    def test_orthodox_fast_gives_both_ends(self):
        result = dates.run_feast('Dormition Fast', 1880, 'orthodox')
        self.assertEqual(result.data['from']['gregorian'], '1880-08-13')
        self.assertEqual(result.data['to']['gregorian'], '1880-08-26')
        self.assertEqual(result.data['date'], '1880-08-13/1880-08-26')

    def test_orthodox_christmas_crosses_the_year(self):
        self.assertEqual(self._date('Christmas', 1900, 'orthodox'), '1901-01-07')

    def test_western_feast_before_1582_is_in_the_old_calendar(self):
        # Before 1582 every Western church kept the old calendar and the old
        # Easter rule. Published: Easter 1066 fell on 16 April (old style),
        # the year of the reform Easter 1582 on 15 April (old style).
        for year, julian in ((1066, '1066-04-16'), (1500, '1500-04-19'),
                             (1582, '1582-04-15')):
            with self.subTest(year=year):
                result = dates.run_feast('Easter', year)
                self.assertEqual(result.data['julian'], julian)
                self.assertEqual(result.data['date'], _julian_to_gregorian(
                    *(int(p) for p in julian.split('-'))))
                self.assertIn('old calendar', result.data['notes'])
        result = dates.run_feast('Michaelmas', 1550)
        self.assertEqual(result.data['julian'], '1550-09-29')
        self.assertEqual(result.data['date'], '1550-10-09')

    def test_western_feast_1583_to_1752_gives_both_readings(self):
        # England kept the old calendar and its Easter until September 1752:
        # Easter 1731 was 18 April old style there (29 April today), and
        # 25 March in lands already on today's calendar. The place decides.
        result = dates.run_feast('Easter', 1731)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertIsNone(result.data['date'])
        got = {r['calendar']: (r['julian'], r['gregorian']) for r in result.data['readings']}
        self.assertEqual(got, {'new': ('1731-03-14', '1731-03-25'),
                               'old': ('1731-04-18', '1731-04-29')})
        for r in result.data['readings']:
            self.assertEqual(r['date'], r['gregorian'])
            self.assertTrue(r['notes'])
        # Christmas 1582 is after the reform began (15 October 1582): both.
        got = {r['calendar']: r['gregorian']
               for r in dates.run_feast('Christmas', 1582).data['readings']}
        self.assertEqual(got, {'new': '1582-12-25', 'old': '1583-01-04'})
        # Easter 1752 was still old style in England; Christmas 1752 was not.
        self.assertEqual(len(dates.run_feast('Easter', 1752).data['readings']), 2)
        result = dates.run_feast('Christmas', 1752)
        self.assertEqual(result.data['date'], '1752-12-25')
        self.assertNotIn('readings', result.data)
        self.assertEqual(dates.run_feast('Michaelmas', 1850).data['cautions'], [])

    def test_two_reading_feast_prints_both(self):
        code, out, _ = _run('feast', 'Easter', '1731')
        self.assertEqual(code, EXIT_CLEAN)
        self.assertIn('29 April 1731', out)
        self.assertIn('25 March 1731', out)
        self.assertIn('England', out)

    def test_listed_feast_titles_are_accepted(self):
        # The refusal lists each feast by its title; typing a listed title works.
        for church in ('western', 'orthodox'):
            for title in dates.feast_names(church):
                with self.subTest(church=church, title=title):
                    self.assertTrue(dates.run_feast(title, 1880, church).ok)
        for typed in ("Peter's Fast", "St Peter's Fast"):
            with self.subTest(typed=typed):
                self.assertTrue(dates.run_feast(typed, 1880, 'orthodox').ok)
        self.assertEqual(self._date('Saints Peter and Paul', 1850), '1850-06-29')

    def test_whitsunday_notes_the_scottish_term_day(self):
        result = dates.run_feast('Whitsunday', 1850)
        self.assertEqual(result.data['date'], '1850-05-19')
        self.assertTrue(any('15 May' in c for c in result.data['cautions']))


class HebrewTests(unittest.TestCase):

    def _greg(self, text):
        result = dates.run_convert(text, 'hebrew')
        self.assertTrue(result.ok, result.data)
        return result.data['date']

    def test_known_dates(self):
        self.assertEqual(self._greg('1 Tishrei 5785'), '2024-10-03')
        self.assertEqual(self._greg('15 Nisan 5784'), '2024-04-23')
        self.assertEqual(self._greg('14 Adar II 5784'), '2024-03-24')
        self.assertEqual(self._greg('14 Adar I 5784'), '2024-02-23')
        self.assertEqual(self._greg('14 Adar 2 5784'), '2024-03-24')
        self.assertEqual(self._greg('14 Adar Sheni 5784'), '2024-03-24')
        self.assertEqual(self._greg('14 Adar Bet 5784'), '2024-03-24')
        self.assertEqual(self._greg('14 Adar Alef 5784'), '2024-02-23')

    def test_historical_dates_checked_on_hebcal(self):
        self.assertEqual(self._greg('1 Tishrei 5600'), '1839-09-09')
        self.assertEqual(self._greg('15 Nisan 5650'), '1890-04-05')
        self.assertEqual(self._greg('30 Cheshvan 5630'), '1869-11-04')

    def test_plain_adar_in_a_leap_year_gives_both(self):
        result = dates.run_convert('14 Adar 5784', 'hebrew')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertIsNone(result.data['date'])
        got = {r['hebrew']: r['gregorian'] for r in result.data['readings']}
        self.assertEqual(got, {'14 Adar I 5784': '2024-02-23',
                               '14 Adar II 5784': '2024-03-24'})

    def test_plain_thirtieth_of_adar_in_a_leap_year_is_adar_one(self):
        # Adar II always has 29 days, so a 30th of Adar in a leap year can
        # only be Adar I: one reading, not a refusal.
        result = dates.run_convert('30 Adar 5784', 'hebrew')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['date'], '2024-03-10')
        self.assertEqual(result.data['hebrew'], '30 Adar I 5784')

    def test_more_month_spellings(self):
        self.assertEqual(self._greg('30 Marheshvan 5630'), '1869-11-04')
        self.assertEqual(self._greg('14 Адар I 5784'), '2024-02-23')

    def test_plain_adar_in_a_common_year_is_one_day(self):
        self.assertEqual(self._greg('14 Adar 5785'), '2025-03-14')

    def test_second_adar_in_a_common_year_is_refused(self):
        result = dates.run_convert('14 Adar II 5785', 'hebrew')
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertIn('14 Adar 5785', result.data['error'])

    def test_day_past_month_end_is_refused(self):
        self.assertEqual(dates.run_convert('30 Iyar 5784', 'hebrew').exit_code, EXIT_ERRORS)

    def test_round_trip_every_day_1800_to_2100(self):
        start = datetime.date(1800, 1, 1).toordinal()
        end = datetime.date(2100, 12, 31).toordinal()
        for n in range(start, end + 1):
            self.assertEqual(dates.fixed_from_hebrew(*dates.hebrew_from_fixed(n)), n)

    def test_new_year_never_on_sunday_wednesday_or_friday(self):
        for y in range(5500, 5901):
            wd = datetime.date.fromordinal(dates.heb_new_year(y)).strftime('%A')
            self.assertNotIn(wd, ('Sunday', 'Wednesday', 'Friday'), y)

    def test_year_lengths_are_legal(self):
        for y in range(5500, 5901):
            length = dates.heb_new_year(y + 1) - dates.heb_new_year(y)
            self.assertIn(length, (353, 354, 355, 383, 384, 385), y)
            self.assertEqual(length > 380, dates.heb_leap(y), y)


class ConvertTests(unittest.TestCase):

    def test_double_year_reads_as_the_later_year(self):
        result = dates.run_convert('11 Feb 1731/32', 'julian')
        self.assertEqual(result.data['julian'], '1732-02-11')
        self.assertEqual(result.data['gregorian'], '1732-02-22')
        self.assertEqual(result.data['weekday'], 'Friday')
        self.assertEqual(result.data['difference_days'], 11)
        self.assertEqual(result.data['date'], '1732-02-22')
        self.assertEqual(result.data['cautions'], [])
        self.assertIn('11 Feb 1731/32', result.data['notes'])

    def test_double_year_short_forms(self):
        for typed in ('11 Feb 1731/2', '11 February 1731/32', 'February 11, 1731/32'):
            with self.subTest(typed=typed):
                self.assertEqual(dates.run_convert(typed, 'julian').data['date'], '1732-02-22')

    def test_double_year_after_march_is_refused(self):
        # A double year covers 1 January to 24 March only; "3 Sep 1731/32"
        # must not quietly become September 1732.
        for typed in ('3 Sep 1731/32', '25 Mar 1731/32', '11 Jun 1731/2'):
            with self.subTest(typed=typed):
                result = dates.run_convert(typed, 'julian')
                self.assertEqual(result.exit_code, EXIT_ERRORS)
                self.assertIn('24 March', result.data['error'])
        self.assertEqual(dates.run_convert('24 Mar 1731/32', 'julian').data['julian'],
                         '1732-03-24')

    def test_double_year_written_in_full(self):
        self.assertEqual(dates.run_convert('1 Jan 1699/1700', 'julian').data['julian'],
                         '1700-01-01')
        self.assertEqual(dates.run_convert('1 Jan 1799/1800', 'julian').data['julian'],
                         '1800-01-01')

    def test_bad_double_year_is_refused(self):
        result = dates.run_convert('11 Feb 1731/35', 'julian')
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertIn('1731/32', result.data['error'])

    def test_input_forms(self):
        for typed in ('1732-02-11', '11 Feb 1732', 'February 11, 1732', '11th of February 1732'):
            with self.subTest(typed=typed):
                self.assertEqual(dates.run_convert(typed, 'julian').data['date'], '1732-02-22')

    def test_january_to_march_before_1752_single_year_is_cautioned(self):
        result = dates.run_convert('11 Feb 1732', 'julian')
        self.assertEqual(len(result.data['cautions']), 1)
        self.assertIn('25 March', result.data['cautions'][0])
        self.assertEqual(dates.run_convert('25 Mar 1732', 'julian').data['cautions'], [])
        self.assertEqual(dates.run_convert('11 Feb 1760', 'julian').data['cautions'], [])

    def test_month_alone_keeps_the_month(self):
        result = dates.run_convert('Feb 1731', 'julian')
        self.assertEqual(result.data['precision'], 'month')
        self.assertEqual(result.data['date'], '1731-02')
        self.assertEqual(result.data['gregorian'], '1731-02-12/1731-03-11')

    def test_year_alone_needs_no_conversion(self):
        result = dates.run_convert('1731', 'julian')
        self.assertEqual(result.data['precision'], 'year')
        self.assertEqual(result.data['date'], '1731')

    def test_gregorian_to_julian(self):
        result = dates.run_convert('1732-02-22', 'gregorian')
        self.assertEqual(result.data['julian'], '1732-02-11')
        self.assertEqual(result.data['date'], '1732-02-22')

    def test_refusals_exit_2_with_an_example(self):
        for typed, cal in (('30 Feb 1732', 'julian'), ('29 Feb 1701', 'julian'),
                           ('31 Apr 1900', 'gregorian'), ('not a date', 'julian'),
                           ('about 1732', 'julian'), ('', 'julian'),
                           ('11 Feb 32', 'julian'),          # never the year 32
                           ('1 Smarch 5650', 'hebrew'), ('Nisan 5650', 'hebrew')):
            with self.subTest(typed=typed):
                result = dates.run_convert(typed, cal)
                self.assertEqual(result.exit_code, EXIT_ERRORS)
                self.assertIn('fha date convert', result.data['example'])

    def test_julian_leap_day_1700_converts(self):
        result = dates.run_convert('1700-02-29', 'julian')
        self.assertEqual(result.data['date'], '1700-03-11')


class CliTests(unittest.TestCase):

    def test_convert_text(self):
        code, out, _ = _run('convert', '11 Feb 1731/32', '--from', 'julian')
        self.assertEqual(code, EXIT_CLEAN)
        self.assertIn('is Friday 22 February 1732', out)
        self.assertIn('date:   1732-02-22', out)
        self.assertIn('ran 11 days behind', out)

    def test_json_prints_one_object(self):
        code, out, _ = _run('convert', '11 Feb 1731/32', '--from', 'julian', '--json')
        self.assertEqual(code, EXIT_CLEAN)
        data = json.loads(out)
        for key in ('input', 'from', 'julian', 'gregorian', 'hebrew', 'weekday',
                    'difference_days', 'date', 'notes', 'cautions'):
            self.assertIn(key, data)
        self.assertEqual(data['from'], 'julian')

    def test_feast_json(self):
        code, out, _ = _run('feast', 'Pokrov', '1880', '--church', 'orthodox', '--json')
        self.assertEqual(code, EXIT_CLEAN)
        data = json.loads(out)
        self.assertEqual((data['feast'], data['church'], data['date']),
                         ('Protection of the Theotokos (Pokrov)', 'orthodox', '1880-10-13'))

    def test_refusal_exits_2_with_example_on_stderr(self):
        code, out, err = _run('convert', '30 Feb 1732', '--from', 'julian')
        self.assertEqual(code, EXIT_ERRORS)
        self.assertEqual(out, '')
        self.assertIn('ERROR:', err)
        self.assertIn('Try: fha date convert', err)
        code, out, err = _run('feast', 'St Swithin', '1850', '--json')
        self.assertEqual(code, EXIT_ERRORS)
        self.assertIn('error', json.loads(out))

    def test_usage_error_exits_2(self):
        code, _, _ = _run('convert', '1732')
        self.assertEqual(code, 2)

    def test_runs_outside_an_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, str(ROOT / 'tools' / 'fha.py'), 'date', 'feast',
                 'Michaelmas', '1850', '--root', tmp],
                cwd=tmp, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(proc.returncode, EXIT_CLEAN, proc.stderr)
        self.assertIn('29 September 1850', proc.stdout)


if __name__ == '__main__':
    unittest.main()
