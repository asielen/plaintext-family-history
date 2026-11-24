#!/usr/bin/env python3
"""
dates.py - fha date: dates written in another calendar (TOOLING §4b)

  fha date convert DATE --from julian|gregorian|hebrew [--json]
  fha date feast NAME YEAR [--church western|orthodox] [--json]

An old parish register says "11 Feb 1731/32", a Russian register says "on
Pokrov 1880", a tombstone says "15 Nisan 5650". Each of these is a real day,
but working out which day takes calendar arithmetic that is easy to get
wrong without any sign: the old (Julian) calendar ran 10 to 13 days behind
today's (Gregorian) one depending on the century, and Easter, which every
movable feast hangs on, follows a different rule in each church. This
command does the arithmetic so the AI drafting a claim never does it in its
head. It prints the day in today's calendar, its weekday, and the lines to
put in the claim: `value:` keeps the record's own words, `date:` is the day
in today's calendar (SPEC §11), `notes:` says which calendar the record used.

Pure computation: no archive is read or written, so it runs anywhere;
`--root` is accepted and ignored.

Every calendar goes through one shared day count, Python's proleptic
Gregorian ordinal (`datetime.date.toordinal()`, day 1 = 1 January of year 1
in today's calendar). That count is the "fixed day number" of Reingold and
Dershowitz's *Calendrical Calculations*, so the Hebrew arithmetic ported
from it needs no adjustment, and a Julian date becomes an ordinal through
the standard Julian day number formula.

CODE MAP
--------
  Calendar arithmetic
    julian_to_ordinal / ordinal_to_julian - the old calendar <-> day count
    western_easter / orthodox_easter       - Easter in each church's rule
    heb_leap ... hebrew_from_fixed         - the Hebrew calendar (ported)
  Reading what was typed
    DateRefused                            - a refusal with an example that works
    parse_civil_date                       - "11 Feb 1731/32", "1732-02-11", "Feb 1732"
    parse_hebrew_date                      - "15 Nisan 5650", "14 Adar II 5784"
    find_feast                             - exact feast-name lookup per church
  Engines (return a _lib.Result)
    run_convert                            - fha date convert
    run_feast                              - fha date feast
  Interface
    _cmd_date                              - render text or --json -> exit code
    register / _run_* / _standalone_main   - CLI wiring
"""

from __future__ import annotations

import argparse
import calendar as _stdcal
import datetime
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    EXIT_CLEAN,
    EXIT_ERRORS,
    Result,
    _MONTH_NAMES,
    configure_utf8_stdout,
    fold_accents,
)

configure_utf8_stdout()

# The proleptic Gregorian ordinal of the first and last days `datetime.date`
# can hold (1 January 1 and 31 December 9999 in today's calendar). Any
# conversion landing outside them is refused: there is no stored form for it.
_MIN_ORDINAL = datetime.date.min.toordinal()
_MAX_ORDINAL = datetime.date.max.toordinal()

# Ordinal offset between a Julian day number and a proleptic Gregorian
# ordinal: JDN 1721426 is 1 January 1 (Gregorian), ordinal 1.
_JDN_OFFSET = 1721425

# The first day of today's calendar anywhere (Catholic Europe, 15 October
# 1582). A "Gregorian" date before it is a projection back in time.
_GREGORIAN_START = datetime.date(1582, 10, 15)


def _month_name(m: int) -> str:
    return _stdcal.month_name[m]


def _long(d: datetime.date) -> str:
    """'Friday 22 February 1732': weekday, day, month name, year."""
    return f'{d.strftime("%A")} {d.day} {_month_name(d.month)} {d.year}'


def _plain(y: int, m: int, d: int) -> str:
    """'11 February 1732' for a day that may exist only in the old calendar
    (29 February 1700), which `datetime.date` cannot hold."""
    return f'{d} {_month_name(m)} {y}'


# ── Calendar arithmetic ──────────────────────────────────────────────────────
#
# Ported from igormel81/genealogy-mcp (https://github.com/igormel81/genealogy-mcp),
# files src/genealogy_mcp/dates.py (_jdn_jul, _from_jdn) and
# src/genealogy_mcp/calendars.py (western_easter, orthodox_easter, the Hebrew
# calendar functions and month aliases, and the Orthodox FIXED, MOVABLE and
# FASTS feast tables further below), at commit 2aa2cb0d. Changed here: every
# function works through the proleptic Gregorian ordinal and returns
# `datetime.date` or plain tuples; the Julian helpers validate their input;
# Easter returns a date; the Hebrew month lookup no longer folds a typed
# "Adar II" into Adar in a common year (that is refused instead); feast names
# are matched exactly (the four-letter stem match is not ported); the Western
# Ash Wednesday and Corpus Christi rows and the "św. wojciecha" alias (a
# different saint) are left out of the Orthodox table, and the Apostles' Fast
# is a fast with both ends; the French Republican calendar is not ported.
#
# MIT License
#
# Copyright (c) 2026 Igor Melnikov
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.


def julian_month_length(y: int, m: int) -> int:
    """Days in month `m` of year `y` in the old calendar, where every fourth
    year is a leap year with no century exception (so 1700 had a 29
    February that today's calendar skips)."""
    if m == 2:
        return 29 if y % 4 == 0 else 28
    return _stdcal.monthrange(2001, m)[1]


def julian_to_ordinal(y: int, m: int, d: int) -> int:
    """The day count of old-calendar date y-m-d.

    The standard Julian day number formula, shifted onto the proleptic
    Gregorian ordinal so the result can be handed to
    `datetime.date.fromordinal`. Raises ValueError for a day the old
    calendar does not have (30 February, 29 February 1701)."""
    if not 1 <= m <= 12 or not 1 <= d <= julian_month_length(y, m):
        raise ValueError(f'{y:04d}-{m:02d}-{d:02d} is not a day in the old calendar')
    a = (14 - m) // 12
    y2, m2 = y + 4800 - a, m + 12 * a - 3
    jdn = d + (153 * m2 + 2) // 5 + 365 * y2 + y2 // 4 - 32083
    return jdn - _JDN_OFFSET


def ordinal_to_julian(n: int) -> tuple[int, int, int]:
    """The old-calendar (year, month, day) of day count `n`: the inverse of
    `julian_to_ordinal`, as a tuple because the old calendar has days
    (29 February 1700) that `datetime.date` cannot hold."""
    c = n + _JDN_OFFSET + 32082
    d = (4 * c + 3) // 1461
    e = c - 1461 * d // 4
    m = (5 * e + 2) // 153
    day = e - (153 * m + 2) // 5 + 1
    month = m + 3 - 12 * (m // 10)
    year = d - 4800 + m // 10
    return year, month, day


def _gregorian_formula_ordinal(y: int, m: int, d: int) -> int:
    """The day count today's calendar would give y-m-d, computed by formula
    so an old-calendar-only day (29 February 1700) still has a value. Used
    only to measure how far the two calendars stood apart on that date."""
    a = (14 - m) // 12
    y2, m2 = y + 4800 - a, m + 12 * a - 3
    jdn = d + (153 * m2 + 2) // 5 + 365 * y2 + y2 // 4 - y2 // 100 + y2 // 400 - 32045
    return jdn - _JDN_OFFSET


def calendar_gap_days(y: int, m: int, d: int) -> int:
    """How many days the old calendar ran behind today's on old-calendar
    date y-m-d: 10 from 1582 to 28 February 1700, 11 to 1800, 12 to 1900,
    13 after. The jump falls on 1 March of a century year because only the
    old calendar keeps 29 February in it."""
    return julian_to_ordinal(y, m, d) - _gregorian_formula_ordinal(y, m, d)


def western_easter(year: int) -> datetime.date:
    """Easter Sunday as the Catholic and Protestant churches keep it, in
    today's calendar: the anonymous Gregorian algorithm (Meeus, Jones,
    Butcher). Every Western movable feast is counted from it."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = (h + l_ - 7 * m + 114) % 31 + 1
    return datetime.date(year, month, day)


def orthodox_easter(year: int) -> tuple[int, int, int]:
    """Orthodox Easter (Pascha) as an old-calendar (year, month, day):
    Meeus's Julian algorithm. The Orthodox church still counts its movable
    feasts in the old calendar, so this is the date a register gives."""
    a, b, c = year % 4, year % 7, year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month = (d + e + 114) // 31
    day = (d + e + 114) % 31 + 1
    return year, month, day


# The Hebrew calendar, from Calendrical Calculations. Months are numbered from
# Nisan (1) to Adar II (13); the year itself starts at 1 Tishrei (month 7).
# A leap year (7 in every 19) has a 13th month: Adar I is month 12 and Adar II
# month 13. In a common year month 12 is plain Adar.
HEBREW_EPOCH = -1373427   # the day count of 1 Tishrei of year 1
HEB_MONTHS = {1: 'Nisan', 2: 'Iyar', 3: 'Sivan', 4: 'Tammuz', 5: 'Av', 6: 'Elul',
              7: 'Tishrei', 8: 'Cheshvan', 9: 'Kislev', 10: 'Tevet', 11: 'Shevat',
              12: 'Adar', 13: 'Adar II'}
HEB_ALIASES = {
    'nissan': 1, 'iyyar': 2, 'tamuz': 4, 'ab': 5, 'tishri': 7, 'heshvan': 8,
    'marcheshvan': 8, 'cheshvan': 8, 'kislew': 9, 'teves': 10, 'tebet': 10,
    'shvat': 11, "sh'vat": 11, 'shebat': 11,
    'adar i': 12, 'adar 1': 12, 'adar rishon': 12,
    'adar ii': 13, 'adar 2': 13, 'adar sheni': 13, 'veadar': 13,
    'нисан': 1, 'ияр': 2, 'сиван': 3, 'таммуз': 4, 'тамуз': 4, 'ав': 5, 'элул': 6,
    'тишрей': 7, 'хешван': 8, 'мархешван': 8, 'кислев': 9, 'тевет': 10, 'шват': 11,
    'адар': 12, 'адар ii': 13, 'адар бет': 13,
}
# The spellings that say "first Adar" outright; plain "Adar" in a leap year
# could be either one and is never guessed.
_FIRST_ADAR_WORDS = {'adar i', 'adar 1', 'adar rishon'}


def heb_leap(y: int) -> bool:
    """True when Hebrew year `y` has two Adars (years 3, 6, 8, 11, 14, 17
    and 19 of each 19-year cycle)."""
    return (7 * y + 1) % 19 < 7


def _elapsed(y: int) -> int:
    """Days from the epoch to the molad (mean new moon) of Tishrei of year
    `y`, pushed one day when the arithmetic would put 1 Tishrei on a
    Sunday, Wednesday or Friday."""
    months = (235 * y - 234) // 19
    parts = 12084 + 13753 * months
    days = 29 * months + parts // 25920
    return days + 1 if (3 * (days + 1)) % 7 < 3 else days


def _delay(y: int) -> int:
    """The extra one or two days that keep every year a legal length (353
    to 355 days, or 383 to 385 in a leap year)."""
    ny0, ny1, ny2 = _elapsed(y - 1), _elapsed(y), _elapsed(y + 1)
    return 2 if ny2 - ny1 == 356 else 1 if ny1 - ny0 == 382 else 0


def heb_new_year(y: int) -> int:
    """The day count of 1 Tishrei (Rosh Hashanah) of Hebrew year `y`."""
    return HEBREW_EPOCH + _elapsed(y) + _delay(y)


def heb_month_length(m: int, y: int) -> int:
    """Days in Hebrew month `m` of year `y`. Cheshvan and Kislev vary with
    the year's length; Adar has 29 days in a common year and 30 as Adar I."""
    days = heb_new_year(y + 1) - heb_new_year(y)
    if (m in (2, 4, 6, 10, 13) or (m == 12 and not heb_leap(y))
            or (m == 8 and days % 10 != 5) or (m == 9 and days % 10 == 3)):
        return 29
    return 30


def _months(y: int) -> list[int]:
    """The months of Hebrew year `y` in order, from Tishrei."""
    last = 13 if heb_leap(y) else 12
    return list(range(7, last + 1)) + list(range(1, 7))


def fixed_from_hebrew(y: int, m: int, d: int) -> int:
    """The day count of Hebrew date d / month m / year y. Raises ValueError
    for a month the year lacks (Adar II in a common year) or a day past the
    month's end."""
    if m not in _months(y):
        raise ValueError(f'Hebrew year {y} has no month {HEB_MONTHS.get(m, m)}')
    if not 1 <= d <= heb_month_length(m, y):
        raise ValueError(f'{HEB_MONTHS[m]} {y} has {heb_month_length(m, y)} days')
    rd = heb_new_year(y)
    for mm in _months(y):
        if mm == m:
            return rd + d - 1
        rd += heb_month_length(mm, y)
    raise AssertionError('unreachable: m is one of _months(y)')


def hebrew_from_fixed(rd: int) -> tuple[int, int, int]:
    """The Hebrew (year, month, day) of day count `rd`."""
    y = (rd - HEBREW_EPOCH) * 98496 // 35975351
    while heb_new_year(y + 1) <= rd:
        y += 1
    while heb_new_year(y) > rd:
        y -= 1
    start = heb_new_year(y)
    for m in _months(y):
        n = heb_month_length(m, y)
        if rd < start + n:
            return y, m, rd - start + 1
        start += n
    raise AssertionError('unreachable: the year holds rd')


def heb_month_label(m: int, y: int) -> str:
    """The month's name as a reader expects it: month 12 is "Adar I" in a
    leap year and plain "Adar" otherwise."""
    if m == 12 and heb_leap(y):
        return 'Adar I'
    return HEB_MONTHS[m]


def hebrew_text(rd: int) -> str:
    """'15 Nisan 5650' for day count `rd`."""
    y, m, d = hebrew_from_fixed(rd)
    return f'{d} {heb_month_label(m, y)} {y}'


# ── Reading what was typed ───────────────────────────────────────────────────

class DateRefused(ValueError):
    """Input the command cannot read, with an example of input it can.

    Every refusal carries its example so the person (or the AI) sees what
    to type next, never a bare "invalid date"."""

    def __init__(self, text: str, example: str):
        super().__init__(text)
        self.text = text
        self.example = example


_CIVIL_EXAMPLE = ('fha date convert "11 Feb 1731/32" --from julian  '
                  '(also 1732-02-11, "February 11, 1732", "Feb 1732" or 1732)')
_HEBREW_EXAMPLE = 'fha date convert "15 Nisan 5650" --from hebrew  (also "14 Adar II 5784")'


def _read_year(text: str) -> tuple[int, str]:
    """A year, or a double year "1731/32" / "1731/2" read as the later
    year. Returns (year, the year as it should be shown back).

    A double year is how England and its colonies wrote a January to March
    date before 1752, when the year still began on 25 March: "1731/32" is
    the year 1731 to the reckoning of the day and 1732 to ours. The second
    part must be the last one or two digits of the next year, so a typo is
    refused rather than read as some other year."""
    m = re.fullmatch(r'(\d{1,4})(?:/(\d{1,2}))?', text)
    if not m:
        raise DateInputError
    year = int(m.group(1))
    if m.group(2) is None:
        return year, m.group(1)
    tail = m.group(2)
    nxt = year + 1
    if int(tail) != nxt % (10 ** len(tail)):
        raise DateRefused(
            f'"{text}" is not a double year: the part after the slash must be '
            f'the last digits of the next year ({year}/{nxt % 100:02d} or '
            f'{year}/{nxt % 10}).',
            _CIVIL_EXAMPLE)
    return nxt, text


class DateInputError(Exception):
    """Internal: this pattern did not match; try the next one."""


def _month_number(word: str) -> int | None:
    return _MONTH_NAMES.get(word.lower().rstrip('.'))


def parse_civil_date(text: str) -> tuple[int, int | None, int | None, str]:
    """Read a date in the old or today's calendar.

    Returns (year, month or None, day or None, how to show it back). The
    forms are the plain ones `_lib.normalize_date` reads (1732-02-11,
    1732-02, 1732, "11 Feb 1732", "February 11, 1732", "Feb 1732"), plus a
    double year in any of them ("11 Feb 1731/32"). The day is checked
    against the month by the calling calendar, not here, because the two
    calendars disagree about 29 February in a century year.

    Hedged or ranged dates ("about 1732", "1731-1733") are refused: the
    conversion is for one day or month the record names, and the hedge
    belongs in the claim, around the converted date."""
    raw = re.sub(r'\s+', ' ', (text or '').strip()).strip('.,')
    if not raw:
        raise DateRefused('No date was given.', _CIVIL_EXAMPLE)
    low = re.sub(r'(\d)(st|nd|rd|th)\b', r'\1', raw.lower())
    low = re.sub(r'\bof\b ', '', low).replace(',', ' ')
    low = re.sub(r'\s+', ' ', low).strip()

    iso = re.fullmatch(r'(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?', low)
    if iso:
        y = int(iso.group(1))
        mo = int(iso.group(2)) if iso.group(2) else None
        d = int(iso.group(3)) if iso.group(3) else None
        if mo is not None and not 1 <= mo <= 12:
            raise DateRefused(f'"{raw}" has no month {mo}: months run 01 to 12.',
                              _CIVIL_EXAMPLE)
        if d is not None:
            shown = _plain(y, mo, d)
        elif mo is not None:
            shown = f'{_month_name(mo)} {y}'
        else:
            shown = str(y)
        return y, mo, d, shown

    patterns = (
        (r'(\d{1,2}) ([a-z]+\.?) (\S+)', ('d', 'm', 'y')),   # 11 feb 1731/32
        (r'([a-z]+\.?) (\d{1,2}) (\S+)', ('m', 'd', 'y')),   # february 11 1732
        (r'([a-z]+\.?) (\S+)', ('m', 'y')),                  # feb 1732
        (r'(\d{1,4}/\d{1,2})', ('y',)),                      # 1731/32
    )
    for pattern, order in patterns:
        m = re.fullmatch(pattern, low)
        if not m:
            continue
        parts = dict(zip(order, m.groups()))
        mo = _month_number(parts['m']) if 'm' in parts else None
        if 'm' in parts and mo is None:
            continue
        try:
            y, y_shown = _read_year(parts['y'])
        except DateInputError:
            continue
        d = int(parts['d']) if 'd' in parts else None
        if d is not None:
            shown = f'{d} {_month_name(mo)} {y_shown}'
        elif mo is not None:
            shown = f'{_month_name(mo)} {y_shown}'
        else:
            shown = y_shown
        return y, mo, d, shown
    raise DateRefused(
        f'"{raw}" is not a date I can read. Give one day, month or year, '
        'without "about" or a range (put those in the claim around the '
        'converted date).',
        _CIVIL_EXAMPLE)


def _heb_month(word: str) -> int | None:
    n = re.sub(r'\s+', ' ', word.strip().lower().replace('ё', 'е'))
    for k, v in HEB_MONTHS.items():
        if v.lower() == n:
            return k
    return HEB_ALIASES.get(n)


def parse_hebrew_date(text: str) -> tuple[int, int, int, bool]:
    """Read "15 Nisan 5650" into (year, month, day, plain_adar).

    `plain_adar` is True when the month was typed as plain "Adar": in a
    leap year that could be Adar I or Adar II, and the caller shows both
    rather than guessing. A month with a number in it ("Adar 2") is read
    whole. Only a full day is read; a Hebrew month or year alone has no
    single day in today's calendar to give."""
    raw = re.sub(r'\s+', ' ', (text or '').strip()).strip('.,')
    m = re.fullmatch(r'(\d{1,2}) (.+?) (\d{4,5})', raw)
    if not m:
        raise DateRefused(f'"{raw}" is not a Hebrew date I can read: give the '
                          'day, the month and the year.', _HEBREW_EXAMPLE)
    month = _heb_month(m.group(2))
    if month is None:
        names = ', '.join(HEB_MONTHS[k] for k in (7, 8, 9, 10, 11, 12, 13, 1, 2, 3, 4, 5, 6))
        raise DateRefused(f'"{m.group(2)}" is not a Hebrew month I know. The months '
                          f'are {names} (and Adar I in a leap year).', _HEBREW_EXAMPLE)
    plain = m.group(2).strip().lower() in ('adar', 'адар')
    return int(m.group(3)), month, int(m.group(1)), plain


# ── Feasts ───────────────────────────────────────────────────────────────────
#
# Each row: key -> (the name shown, the day, the spellings people write).
# The day is (month, day) for a fixed feast and a day offset from that
# church's Easter for a movable one. Western fixed days are in today's
# calendar; Orthodox ones are in the old calendar, as the church keeps them.

WESTERN_FIXED = {
    'christmas': ('Christmas', (12, 25), ('christmas', 'christmas day', 'boze narodzenie')),
    'st_stephen': ("St Stephen's Day (Boxing Day)", (12, 26),
                   ('st stephen', 'boxing day', 'swietego szczepana')),
    'epiphany': ('Epiphany (Three Kings)', (1, 6),
                 ('epiphany', 'three kings', 'twelfth day', 'trzech kroli')),
    'candlemas': ('Candlemas (Presentation of the Lord)', (2, 2),
                  ('candlemas', 'presentation', 'purification', 'matki boskiej gromnicznej')),
    'lady_day': ('Lady Day (the Annunciation)', (3, 25),
                 ('lady day', 'annunciation', 'zwiastowanie')),
    'st_george': ("St George's Day", (4, 23), ('st george',)),
    'midsummer': ('Midsummer (St John the Baptist)', (6, 24),
                  ('midsummer', 'st john the baptist', 'st john baptist',
                   'nativity of st john', 'swietego jana')),
    'peter_paul': ('Sts Peter and Paul', (6, 29),
                   ('sts peter and paul', 'st peter and st paul', 'peter and paul',
                    'st peter', 'piotra i pawla')),
    'lammas': ('Lammas', (8, 1), ('lammas',)),
    'assumption': ('The Assumption', (8, 15), ('assumption', 'wniebowziecie')),
    'nativity_mary': ('Nativity of Mary', (9, 8),
                      ('nativity of mary', 'nativity of the virgin',
                       'nativity of the blessed virgin mary')),
    'michaelmas': ('Michaelmas (St Michael)', (9, 29),
                   ('michaelmas', 'st michael', 'swietego michala')),
    'all_saints': ("All Saints' Day", (11, 1),
                   ('all saints', 'all hallows', 'wszystkich swietych')),
    'all_souls': ("All Souls' Day", (11, 2), ('all souls', 'zaduszki')),
    'martinmas': ('Martinmas (St Martin)', (11, 11),
                  ('martinmas', 'st martin', 'swietego marcina')),
    'st_andrew': ("St Andrew's Day", (11, 30), ('st andrew',)),
    'st_nicholas': ("St Nicholas' Day", (12, 6),
                    ('st nicholas', 'mikolaja', 'swietego mikolaja')),
}
WESTERN_MOVABLE = {
    'septuagesima': ('Septuagesima Sunday', -63, ('septuagesima',)),
    'shrove_tuesday': ('Shrove Tuesday', -47, ('shrove tuesday', 'pancake day', 'mardi gras')),
    'ash_wednesday': ('Ash Wednesday', -46, ('ash wednesday', 'popielec')),
    'palm_sunday': ('Palm Sunday', -7, ('palm sunday', 'niedziela palmowa')),
    'maundy_thursday': ('Maundy Thursday', -3, ('maundy thursday', 'wielki czwartek')),
    'good_friday': ('Good Friday', -2, ('good friday', 'wielki piatek')),
    'easter': ('Easter Sunday', 0, ('easter', 'easter sunday', 'easter day', 'wielkanoc')),
    'easter_monday': ('Easter Monday', 1, ('easter monday',)),
    'low_sunday': ('Low Sunday', 7, ('low sunday',)),
    'rogation_sunday': ('Rogation Sunday', 35, ('rogation sunday',)),
    'ascension': ('Ascension Day', 39, ('ascension', 'wniebowstapienie')),
    'pentecost': ('Pentecost (Whitsunday)', 49,
                  ('pentecost', 'whitsunday', 'whit sunday', 'whitsun', 'zielone swiatki')),
    'whit_monday': ('Whit Monday', 50, ('whit monday',)),
    'trinity_sunday': ('Trinity Sunday', 56, ('trinity sunday', 'trinity')),
    'corpus_christi': ('Corpus Christi', 60, ('corpus christi', 'boze cialo')),
}

# Ported from genealogy-mcp calendars.py (see the notice above): old-calendar
# month and day, or days from Orthodox Easter.
ORTHODOX_FIXED = {
    'christmas': ('Christmas', (12, 25),
                  ('рождество', 'рождество христово', 'christmas', 'boże narodzenie', 'crăciun')),
    'epiphany': ('Epiphany / Theophany', (1, 6),
                 ('крещение', 'богоявление', 'epiphany', 'theophany', 'trzech króli', 'boboteaza')),
    'presentation': ('Presentation (Candlemas)', (2, 2),
                     ('сретение', 'candlemas', 'presentation', 'matki boskiej gromnicznej')),
    'annunciation': ('Annunciation', (3, 25),
                     ('благовещение', 'annunciation', 'zwiastowanie', 'buna vestire')),
    'st_george_spring': ('St George (spring)', (4, 23),
                         ('егорий вешний', 'юрьев день вешний', 'георгий победоносец', 'st george')),
    'st_nicholas_spring': ('St Nicholas (spring)', (5, 9),
                           ('никола вешний', 'николай вешний', 'никола летний')),
    'nativity_john': ('Nativity of John the Baptist', (6, 24),
                      ('рождество иоанна предтечи', 'иван купала', 'иванов день', 'midsummer',
                       'st john baptist', 'st john the baptist', 'świętego jana', 'sânziene')),
    'peter_paul': ('Sts Peter and Paul', (6, 29),
                   ('петров день', 'петра и павла', 'petrov den', 'peter and paul',
                    'sts peter and paul', 'piotra i pawła')),
    'elijah': ('St Elijah', (7, 20), ('ильин день', 'илья пророк', 'elijah')),
    'transfiguration': ('Transfiguration', (8, 6),
                        ('преображение', 'яблочный спас', 'spas', 'transfiguration')),
    'dormition': ('Dormition / Assumption', (8, 15),
                  ('успение', 'dormition', 'assumption', 'wniebowzięcie', 'adormirea')),
    'beheading_john': ('Beheading of John the Baptist', (8, 29), ('усекновение', 'иван постный')),
    'nativity_theotokos': ('Nativity of the Theotokos', (9, 8),
                           ('рождество богородицы', 'богородичен день', 'nativity of mary',
                            'narodzenie najświętszej', 'sfânta maria mică')),
    'exaltation': ('Exaltation of the Cross', (9, 14),
                   ('воздвижение', 'exaltation of the cross', 'podwyższenie krzyża', 'ziua crucii')),
    'protection': ('Protection of the Theotokos (Pokrov)', (10, 1),
                   ('покров', 'покров пресвятой богородицы', 'pokrov', 'protection of the theotokos')),
    'kazan_icon': ('Kazan Icon of the Mother of God', (10, 22), ('казанская', 'казанской иконы')),
    'dmitry': ('St Demetrius', (10, 26), ('дмитриев день', 'димитрий солунский', 'dimitrie')),
    'cosmas_damian': ('Sts Cosmas and Damian', (11, 1),
                      ('кузьминки', 'кузьма и демьян', 'козьма и дамиан')),
    'michael': ('Archangel Michael', (11, 8),
                ('михайлов день', 'архангела михаила', 'michaelmas', 'st michael', 'sf. mihail')),
    'entry_temple': ('Entry of the Theotokos into the Temple', (11, 21),
                     ('введение', 'введение во храм', 'entry into the temple',
                      'presentation of mary', 'intrarea în biserică')),
    'st_george_autumn': ("St George (autumn), Yuri's Day", (11, 26),
                         ('юрьев день', 'егорий осенний', 'егорий зимний')),
    'st_nicholas_winter': ('St Nicholas', (12, 6),
                           ('никола зимний', 'николин день', 'николай чудотворец',
                            'st nicholas', 'mikołaja', 'sf. nicolae')),
}
ORTHODOX_MOVABLE = {
    'forgiveness_sunday': ('Forgiveness Sunday (end of Maslenitsa)', -49,
                           ('прощёное воскресенье', 'масленица', 'cheesefare sunday',
                            'forgiveness sunday')),
    'clean_monday': ('Clean Monday, start of Great Lent', -48,
                     ('великий пост', 'чистый понедельник', 'clean monday',
                      'начало великого поста')),
    'lazarus_saturday': ('Lazarus Saturday', -8, ('лазарева суббота', 'lazarus saturday')),
    'palm_sunday': ('Palm Sunday', -7,
                    ('вербное воскресенье', 'вход господень в иерусалим', 'palm sunday',
                     'niedziela palmowa', 'florii')),
    'good_friday': ('Good Friday', -2,
                    ('страстная пятница', 'великая пятница', 'good friday', 'wielki piątek')),
    'easter': ('Easter (Pascha)', 0,
               ('пасха', 'светлое воскресенье', 'easter', 'pascha', 'wielkanoc', 'paște')),
    'radonitsa': ('Radonitsa (day of the dead)', 9, ('радоница', 'radonitsa')),
    'ascension': ('Ascension', 39, ('вознесение', 'ascension', 'wniebowstąpienie', 'înălțarea')),
    'trinity': ('Pentecost / Trinity', 49,
                ('троица', 'троицын день', 'пятидесятница', 'pentecost', 'trinity',
                 'whitsun', 'zielone świątki', 'rusalii')),
    'spirit_day': ('Whit Monday / Spirit Day', 50, ('духов день', 'whit monday', 'spirit day')),
    'all_saints_orthodox': ('All Saints Sunday', 56, ('неделя всех святых', 'all saints')),
}
# Fasts: (name, first day, last day, spellings). A fixed end is an
# old-calendar (month, day); the Apostles' Fast starts a set number of days
# after Easter (an int) and ends the day before Sts Peter and Paul.
ORTHODOX_FASTS = {
    'apostles_fast': ("Apostles' (Peter's) Fast", 57, (6, 28),
                      ('петров пост', 'апостольский пост', "apostles' fast", 'peters fast')),
    'dormition_fast': ('Dormition Fast', (8, 1), (8, 14), ('успенский пост', 'dormition fast')),
    'nativity_fast': ("Nativity (Philip's) Fast", (11, 15), (12, 24),
                      ('филиппов пост', 'рождественский пост', 'nativity fast', 'advent')),
}

_CHURCH_TABLES = {
    'western': (WESTERN_FIXED, WESTERN_MOVABLE, {}),
    'orthodox': (ORTHODOX_FIXED, ORTHODOX_MOVABLE, ORTHODOX_FASTS),
}

# One leading preposition a record writes before a feast ("on Pokrov", "na
# Pokrov", "w Wielkanoc"), in English, Polish and Russian.
_LEADING_WORD = re.compile(r'^(?:on|at|in|na|w|we|в|во|на|the|feast of|feast of the)\s+')


def feast_key(name: str) -> str:
    """The comparison form of a feast name: accents folded, lower case,
    "Saint"/"St." as "st", "'s" and a closing "Day" dropped, one leading
    preposition removed. The same folding is applied to the table's
    spellings, so "St. Stephen's Day", "Saint Stephen" and "st stephen"
    meet, and "Świętego Jana" meets "swietego jana"."""
    n = fold_accents(str(name or '')).lower().replace('ё', 'е')
    n = re.sub(r"['’]s\b", '', n).replace('.', ' ').replace("'", '').replace('’', '')
    n = re.sub(r'\s+', ' ', n).strip()
    while (stripped := _LEADING_WORD.sub('', n)) != n:     # "on the feast of St Martin"
        n = stripped
    n = re.sub(r'^saint\b', 'st', n)
    n = re.sub(r' day$', '', n)
    return n.strip()


def find_feast(name: str, church: str) -> tuple[str, str, tuple] | None:
    """Look a feast up by exact name in one church's tables.

    Returns (kind, key, row) with kind 'fixed', 'movable' or 'fast', or
    None. Exact match only: a fuzzy match would pick the nearest name on
    a typo and give a confident wrong day, which is worse than asking."""
    want = feast_key(name)
    if not want:
        return None
    for kind, table in zip(('fixed', 'movable', 'fast'), _CHURCH_TABLES[church]):
        for key, row in table.items():
            if want == key.replace('_', ' ') or any(want == feast_key(w) for w in row[-1]):
                return kind, key, row
    return None


def feast_names(church: str) -> list[str]:
    """Every feast name one church's tables know, in table order."""
    return [row[0] for table in _CHURCH_TABLES[church] for row in table.values()]


# ── Engines ──────────────────────────────────────────────────────────────────

def _refused(result_data: dict, err: DateRefused) -> Result:
    data = dict(result_data)
    data.update({'error': err.text, 'example': err.example})
    result = Result(ok=False, exit_code=EXIT_ERRORS, data=data)
    result.add('error', err.text, next_step=f'Try: {err.example}')
    return result


def _date_of(ordinal: int) -> datetime.date:
    if not _MIN_ORDINAL <= ordinal <= _MAX_ORDINAL:
        raise DateRefused('That date falls outside the years 1 to 9999, which is '
                          'all this command can convert.', _CIVIL_EXAMPLE)
    return datetime.date.fromordinal(ordinal)


def _iso_tuple(y: int, m: int, d: int) -> str:
    return f'{y:04d}-{m:02d}-{d:02d}'


def _old_style_caution(y: int, mo: int | None, d: int | None, double_year: bool) -> list[str]:
    """The one caution for an old-calendar date in January to March before
    1752 with a single year: it may belong to the next year."""
    if double_year or mo is None or y >= 1752:
        return []
    if mo < 3 or (mo == 3 and (d is None or d <= 24)):
        return ['In England and its colonies before 1752 the year began on 25 March, '
                'so a date in January to March may belong to the next year. If the '
                'record shows two years (1731/32), type both.']
    return []


_BEFORE_GREGORIAN = ('Today\'s calendar began on 15 October 1582; a date before it is '
                     'that calendar counted backwards, which no record of the time used. '
                     'A record from then is almost certainly in the old calendar: try '
                     '--from julian.')


def run_convert(text: str, from_calendar: str) -> Result:
    """Convert one date from the old (Julian), today's (Gregorian) or the
    Hebrew calendar. The engine of `fha date convert`.

    `data` carries the doc's JSON shape: input, from, julian, gregorian,
    hebrew, weekday, difference_days, date (the claim's `date:`, in today's
    calendar), notes (the claim's `notes:` line) and cautions. A month
    alone converts to the span of days it covers, for information; its
    `date:` stays the month as written, since converting it would only move
    its edges. A year alone needs no conversion. Plain "Adar" in a Hebrew
    leap year gives two readings (`readings`) and no single `date:`."""
    base = {'input': text, 'from': from_calendar}
    try:
        if from_calendar == 'hebrew':
            return _convert_hebrew(text, base)
        return _convert_civil(text, from_calendar, base)
    except DateRefused as err:
        return _refused(base, err)


def _convert_civil(text: str, cal: str, base: dict) -> Result:
    y, mo, d, shown = parse_civil_date(text)
    double_year = '/' in shown
    if not 1 <= y <= 9999:
        raise DateRefused(f'Year {y} is outside 1 to 9999.', _CIVIL_EXAMPLE)
    data = dict(base, shown=shown, julian=None, gregorian=None, hebrew=None,
                weekday=None, difference_days=None, precision='day', cautions=[])
    old = cal == 'julian'
    cal_words = 'the old calendar (Julian, "Old Style")' if old else "today's calendar (Gregorian)"
    data['calendar_words'] = cal_words
    if old:
        data['cautions'] += _old_style_caution(y, mo, d, double_year)

    if mo is None:
        data.update(precision='year', date=str(y),
                    notes=(f'The record gives the year only ({shown}); no conversion is needed.'
                           if not double_year else
                           f'The record gives the double year {shown}; date: is the later year.'))
        if old:
            data['julian'] = str(y)
        else:
            data['gregorian'] = str(y)
        return Result(data=data)

    if d is None:
        last = julian_month_length(y, mo) if old else _stdcal.monthrange(y, mo)[1]
        if old:
            first_o, last_o = julian_to_ordinal(y, mo, 1), julian_to_ordinal(y, mo, last)
            span = f'{_date_of(first_o).isoformat()}/{_date_of(last_o).isoformat()}'
            data.update(julian=f'{y:04d}-{mo:02d}', gregorian=span)
        else:
            first_o = datetime.date(y, mo, 1).toordinal()
            last_o = datetime.date(y, mo, last).toordinal()
            jf, jl = ordinal_to_julian(first_o), ordinal_to_julian(last_o)
            data.update(gregorian=f'{y:04d}-{mo:02d}',
                        julian=f'{_iso_tuple(*jf)}/{_iso_tuple(*jl)}')
        data.update(precision='month', date=f'{y:04d}-{mo:02d}',
                    notes=(f'The record gives the month only ({shown}'
                           + (', old calendar' if old else '') + '); date: keeps the month '
                           'as written.'))
        return Result(data=data)

    if old:
        try:
            ordinal = julian_to_ordinal(y, mo, d)
        except ValueError:
            raise DateRefused(
                f'{_month_name(mo)} {y} had {julian_month_length(y, mo)} days in the old '
                f'calendar, so day {d} does not exist.', _CIVIL_EXAMPLE) from None
        g = _date_of(ordinal)
        data.update(julian=_iso_tuple(y, mo, d), gregorian=g.isoformat(),
                    difference_days=calendar_gap_days(y, mo, d))
        data['notes'] = (f'The record uses the old (Julian) calendar: {_civil_value(text)}. '
                         "date: is the same day in today's calendar.")
    else:
        try:
            g = datetime.date(y, mo, d)
        except ValueError:
            raise DateRefused(
                f'{_month_name(mo)} {y} has {_stdcal.monthrange(y, mo)[1]} days in today\'s '
                f'calendar, so day {d} does not exist.', _CIVIL_EXAMPLE) from None
        ordinal = g.toordinal()
        jy, jm, jd = ordinal_to_julian(ordinal)
        data.update(julian=_iso_tuple(jy, jm, jd), gregorian=g.isoformat(),
                    difference_days=calendar_gap_days(jy, jm, jd))
        data['notes'] = None
        if g < _GREGORIAN_START:
            data['cautions'].append(_BEFORE_GREGORIAN)
    data['weekday'] = g.strftime('%A')
    data['date'] = g.isoformat()
    try:
        data['hebrew'] = hebrew_text(ordinal)
    except (ValueError, AssertionError):
        data['hebrew'] = None
    return Result(data=data)


def _civil_value(text: str) -> str:
    return re.sub(r'\s+', ' ', text.strip())


def _hebrew_reading(y: int, m: int, d: int) -> dict:
    """One Hebrew date's equivalents, or DateRefused when the day is not in
    that month or the year lacks the month."""
    if m not in _months(y):
        raise DateRefused(
            f'Hebrew year {y} is not a leap year, so it has only one Adar. Type it as '
            f'plain Adar ("{d} Adar {y}").', _HEBREW_EXAMPLE)
    length = heb_month_length(m, y)
    if not 1 <= d <= length:
        raise DateRefused(f'{heb_month_label(m, y)} {y} has {length} days, so day {d} '
                          'does not exist.', _HEBREW_EXAMPLE)
    g = _date_of(fixed_from_hebrew(y, m, d))
    jy, jm, jd = ordinal_to_julian(g.toordinal())
    return {'hebrew': f'{d} {heb_month_label(m, y)} {y}', 'gregorian': g.isoformat(),
            'julian': _iso_tuple(jy, jm, jd), 'weekday': g.strftime('%A')}


_SUNSET = ('A Hebrew day begins at sunset: an event "on the night of" may fall on the '
           'day before.')


def _convert_hebrew(text: str, base: dict) -> Result:
    y, m, d, plain_adar = parse_hebrew_date(text)
    data = dict(base, julian=None, gregorian=None, hebrew=None, weekday=None,
                difference_days=None, precision='day', cautions=[_SUNSET])
    if m == 12 and heb_leap(y) and plain_adar:
        first, second = _hebrew_reading(y, 12, d), _hebrew_reading(y, 13, d)
        data.update(readings=[first, second], date=None,
                    notes=None)
        data['cautions'].insert(0, (
            f'{y} is a Hebrew leap year with two Adars: plain "Adar" can mean Adar I '
            f'(the extra month) or Adar II (the one holidays like Purim follow). The '
            'record or its context decides; both readings are shown.'))
        return Result(data=data)
    if m in (12, 13) and not heb_leap(y) and not plain_adar:
        raise DateRefused(
            f'Hebrew year {y} is not a leap year, so it has only one Adar. Type it as '
            f'plain Adar ("{d} Adar {y}").', _HEBREW_EXAMPLE)
    reading = _hebrew_reading(y, m, d)
    data.update(reading, date=reading['gregorian'],
                notes=(f'The record uses the Hebrew calendar: {reading["hebrew"]}. '
                       "date: is the same day in today's calendar."))
    return Result(data=data)


_WESTERN_PRE_1752 = ('Before 1752 England and its colonies (and before 1700 most Protestant '
                     'German lands) still kept the old calendar, so the feast fell on a '
                     'different day there. This answer is for a place already using '
                     'today\'s calendar; for an English record of that time, look the '
                     'feast\'s old-calendar day up and convert it with --from julian.')
_SCOTTISH_WHITSUNDAY = ('In Scottish legal papers (leases, rents, removals) "Whitsunday" '
                        'can mean the term day, 15 May, not the church feast.')


def run_feast(name: str, year: int, church: str = 'western') -> Result:
    """The day of a church feast or fast in one year. The engine of
    `fha date feast`.

    A Western feast is computed in today's calendar (its fixed day, or its
    distance from Western Easter). An Orthodox one is computed in the old
    calendar, as the church keeps it, and then converted. A fast spans two
    days, given as `from` and `to`, and its `date:` is the EDTF interval
    between them. An unknown name is refused with the church's list; a name
    the other church knows says to switch `--church`."""
    base = {'input': name, 'year': year, 'church': church}
    try:
        if not 1 <= year <= 9998:
            raise DateRefused(f'Year {year} is outside 1 to 9998.',
                              'fha date feast Michaelmas 1850')
        found = find_feast(name, church)
        if found is None:
            other = 'orthodox' if church == 'western' else 'western'
            if find_feast(name, other) is not None:
                raise DateRefused(
                    f'"{name}" is a feast of the {other.capitalize()} church in my lists, '
                    f'not the {church.capitalize()} one.',
                    f'fha date feast "{name}" {year} --church {other}')
            raise DateRefused(
                f'I don\'t know a feast called "{name}". The ones I know '
                f'({church.capitalize()} church): {", ".join(feast_names(church))}. '
                'Look the feast\'s day up, then convert it with `fha date convert`.',
                f'fha date convert "29 Sep {year}" --from '
                + ('gregorian' if church == 'western' else 'julian'))
        kind, key, row = found
        return _feast_result(base, kind, key, row, year, church)
    except DateRefused as err:
        return _refused(base, err)


def _feast_day(spec, year: int, church: str) -> tuple[int, int, int, int]:
    """(ordinal, y, m, d) of one feast day; y-m-d in the church's own calendar."""
    if isinstance(spec, int):
        if church == 'western':
            day = western_easter(year) + datetime.timedelta(days=spec)
            return day.toordinal(), day.year, day.month, day.day
        ordinal = julian_to_ordinal(*orthodox_easter(year)) + spec
        return (ordinal, *ordinal_to_julian(ordinal))
    m, d = spec
    if church == 'western':
        day = datetime.date(year, m, d)
        return day.toordinal(), year, m, d
    return julian_to_ordinal(year, m, d), year, m, d


def _feast_result(base: dict, kind: str, key: str, row: tuple, year: int, church: str) -> Result:
    title = row[0]
    old = church == 'orthodox'
    data = dict(base, feast=title, key=key, cautions=[])
    days = [row[1]] if kind != 'fast' else [row[1], row[2]]
    readings = []
    for spec in days:
        ordinal, y, m, d = _feast_day(spec, year, church)
        g = _date_of(ordinal)
        jy, jm, jd = ordinal_to_julian(ordinal)
        readings.append({'julian': _iso_tuple(jy, jm, jd), 'gregorian': g.isoformat(),
                         'weekday': g.strftime('%A'), 'own': (y, m, d)})
    cal_note = 'Julian calendar' if old else "today's calendar"
    # The notes line names the feast as the record did ("Pokrov"), so it
    # reads against the record; the title line above gives the full name.
    said = re.sub(r'\s+', ' ', str(base['input']).strip()) or title
    if kind == 'fast':
        first, last = readings
        data['from'] = {k: first[k] for k in ('julian', 'gregorian', 'weekday')}
        data['to'] = {k: last[k] for k in ('julian', 'gregorian', 'weekday')}
        data['date'] = f'{first["gregorian"]}/{last["gregorian"]}'
        fy, fm, fd = first['own']
        ly, lm, ld = last['own']
        data['notes'] = (f'{said} {year} in the record ({fd} {_month_name(fm)} to {ld} '
                         f'{_month_name(lm)}, {cal_note}); date: is that span in '
                         "today's calendar.")
    else:
        r = readings[0]
        data.update(julian=r['julian'], gregorian=r['gregorian'], weekday=r['weekday'],
                    date=r['gregorian'])
        y, m, d = r['own']
        if old:
            data['notes'] = (f'Feast of {said} {year} in the record ({d} {_month_name(m)}, '
                             "Julian calendar); date: is the same day in today's calendar.")
        else:
            data['notes'] = f'{said} {year} in the record; date: is that day.'
    data['own_days'] = [list(r['own']) for r in readings]
    if not old and year < 1752:
        data['cautions'].append(_WESTERN_PRE_1752)
    if not old and key == 'pentecost':
        data['cautions'].append(_SCOTTISH_WHITSUNDAY)
    return Result(data=data)


# ── Interface ────────────────────────────────────────────────────────────────

def _print_claim_block(value: str, date: str | None, notes: str | None) -> None:
    print()
    print('For a claim:')
    print(f'  value:  keep the date as the record writes it ("{value}")')
    if date:
        print(f'  date:   {date}')
    if notes:
        print(f'  notes:  "{notes}"')


def _render_convert(data: dict) -> None:
    cautions = data.get('cautions') or []
    if data['from'] == 'hebrew':
        if data.get('readings'):
            print(f'{data["input"].strip()} in the Hebrew calendar could be either of these:')
            for r in data['readings']:
                g = datetime.date.fromisoformat(r['gregorian'])
                print(f'  {r["hebrew"]} is {_long(g)} in today\'s calendar.')
            for c in cautions:
                print(c)
            print()
            print('For a claim: keep the record\'s words in value:, and once you know which '
                  'Adar it means, run this again with "Adar I" or "Adar II".')
            return
        g = datetime.date.fromisoformat(data['gregorian'])
        print(f'{data["hebrew"]} in the Hebrew calendar is {_long(g)} in today\'s calendar.')
        for c in cautions:
            print(c)
        _print_claim_block(_civil_value(data['input']), data['date'], data['notes'])
        return

    shown, old = data['shown'], data['from'] == 'julian'
    if data['precision'] == 'year':
        print(f'{shown} is a year alone: it needs no conversion.')
        if '/' in shown:
            print(f'A double year is read as the later year: {data["date"]}.')
        for c in cautions:
            print(c)
        _print_claim_block(_civil_value(data['input']), data['date'], data['notes'])
        return
    if data['precision'] == 'month':
        span = data['gregorian'] if old else data['julian']
        a, b = span.split('/')
        other = "today's calendar" if old else 'the old (Julian) calendar'
        print(f'{shown} in {data["calendar_words"]} ran from {a} to {b} in {other}.')
        print('A month alone is kept as the record gives it: converting it would only '
              'move its first and last days.')
        for c in cautions:
            print(c)
        _print_claim_block(_civil_value(data['input']), data['date'], data['notes'])
        return

    g = datetime.date.fromisoformat(data['gregorian'])
    if old:
        print(f'{shown} in the old calendar (Julian, "Old Style")')
        print(f'is {_long(g)} in today\'s calendar (Gregorian).')
        print(f'The old calendar ran {data["difference_days"]} days behind at that time.')
    else:
        jy, jm, jd = (int(p) for p in data['julian'].split('-'))
        print(f'{_long(g)} in today\'s calendar (Gregorian)')
        print(f'was {_plain(jy, jm, jd)} in the old calendar (Julian, "Old Style"), '
              f'{data["difference_days"]} days behind.')
    if data.get('hebrew'):
        print(f'In the Hebrew calendar: {data["hebrew"]}.')
    for c in cautions:
        print(c)
    if old:
        _print_claim_block(_civil_value(data['input']), data['date'], data['notes'])
    else:
        print()
        print(f'For a claim: date: {data["date"]} (already in today\'s calendar).')


def _render_feast(data: dict) -> None:
    church = data['church'].capitalize()
    old = data['church'] == 'orthodox'
    print(f'{data["feast"]}, {data["year"]}, {church} church:')
    if 'from' in data:
        for word, r in (('from', data['from']), ('to', data['to'])):
            g = datetime.date.fromisoformat(r['gregorian'])
            if old:
                jy, jm, jd = (int(p) for p in r['julian'].split('-'))
                print(f'  {word} {_plain(jy, jm, jd)} in the Julian calendar the register used'
                      f' = {_long(g)} in today\'s calendar')
            else:
                print(f'  {word} {_long(g)}')
    elif old:
        jy, jm, jd = (int(p) for p in data['julian'].split('-'))
        g = datetime.date.fromisoformat(data['gregorian'])
        print(f'  {_plain(jy, jm, jd)} in the Julian calendar the register used')
        print(f'  = {_long(g)} in today\'s calendar.')
    else:
        g = datetime.date.fromisoformat(data['gregorian'])
        jy, jm, jd = (int(p) for p in data['julian'].split('-'))
        print(f'  {_long(g)} in today\'s calendar')
        print(f'  ({_plain(jy, jm, jd)} in the old Julian calendar).')
    for c in data['cautions']:
        print(c)
    print()
    print(f'For a claim: date: {data["date"]}, and in notes: "{data["notes"]}"')


def _cmd_date(result: Result, verb: str, use_json: bool = False) -> int:
    """Render one `fha date` result; the only layer that prints."""
    if use_json:
        data = {k: v for k, v in result.data.items()
                if k not in ('shown', 'calendar_words', 'own_days', 'key')}
        print(json.dumps(data, indent=2, ensure_ascii=False))
        if not result.ok:
            for m in result.messages:
                print(f'ERROR: {m.text}', file=sys.stderr)
        return result.exit_code
    if not result.ok:
        for m in result.messages:
            print(f'ERROR: {m.text}', file=sys.stderr)
            if m.next_step:
                print(m.next_step, file=sys.stderr)
        return result.exit_code
    if verb == 'convert':
        _render_convert(result.data)
    else:
        _render_feast(result.data)
    return result.exit_code


# ── CLI ──────────────────────────────────────────────────────────────────────

_CLI_DESCRIPTION = """\
Dates written in another calendar: the old (Julian) calendar, a church feast,
or the Hebrew calendar. Prints the same day in today's calendar, its weekday,
and the lines to put in a claim.

  fha date convert "11 Feb 1731/32" --from julian
  fha date convert 1918-02-14 --from gregorian
  fha date convert "15 Nisan 5650" --from hebrew
  fha date feast Michaelmas 1850
  fha date feast Pokrov 1880 --church orthodox

Pure arithmetic: nothing in the archive is read or written."""


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument('--json', action='store_true', dest='use_json',
                   help='Machine-readable JSON output (one object)')
    p.add_argument('--root', metavar='PATH',
                   help='Accepted and ignored: this command needs no archive')


def _add_arguments(p: argparse.ArgumentParser) -> None:
    verbs = p.add_subparsers(dest='date_verb', metavar='{convert,feast}')
    verbs.required = True
    c = verbs.add_parser(
        'convert', help='Convert a date from the old, today\'s or the Hebrew calendar',
        description='Convert one date. Julian or Gregorian: 1732-02-11, "11 Feb 1732", '
                    '"February 11, 1732", "Feb 1732", 1732, and a double year such as '
                    '"11 Feb 1731/32". Hebrew: "15 Nisan 5650", "14 Adar II 5784".')
    c.add_argument('date', metavar='DATE', help='The date as the record writes it')
    c.add_argument('--from', dest='from_calendar', required=True,
                   choices=('julian', 'gregorian', 'hebrew'),
                   help='The calendar the record uses: julian (the old calendar, '
                        '"Old Style"), gregorian (today\'s) or hebrew')
    _add_common(c)
    c.set_defaults(func=_run_convert)
    f = verbs.add_parser(
        'feast', help='The day a church feast or fast fell on in one year',
        description='The day a church feast or fast fell on in one year. Names are '
                    'matched exactly (accents and case aside): "Michaelmas", "St '
                    'Stephen", "Easter Monday", "Pokrov". An unknown name lists the '
                    'ones known.')
    f.add_argument('name', metavar='NAME', help='The feast, as the record names it')
    f.add_argument('year', metavar='YEAR', type=int, help='The year, e.g. 1850')
    f.add_argument('--church', choices=('western', 'orthodox'), default='western',
                   help='western (Catholic and Protestant, the default) or orthodox '
                        '(kept in the old calendar)')
    _add_common(f)
    f.set_defaults(func=_run_feast)


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        'date',
        help='Convert a date from the old calendar, a church feast or the Hebrew calendar',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(p)


def _run_convert(args: argparse.Namespace) -> int:
    return _cmd_date(run_convert(args.date, args.from_calendar), 'convert', args.use_json)


def _run_feast(args: argparse.Namespace) -> int:
    return _cmd_date(run_feast(args.name, args.year, args.church), 'feast', args.use_json)


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha date', description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    _add_arguments(parser)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
