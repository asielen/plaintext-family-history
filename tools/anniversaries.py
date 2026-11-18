#!/usr/bin/env python3
"""
anniversaries.py - fha calendar: what happened in the family this month?

  fha calendar [WHEN] [--week] [--found] [--json] [--root PATH]

Births, baptisms, marriages, deaths, burials and arrivals whose recorded date
falls in a month, on a day, or in the week ahead - "On this day in 1880,
Thomas Hartley was born in Fairview" - read from the archive's own claims
(TOOLING §7b). The archive's version of a newspaper's "on this day" column.

WHEN is forgiving: nothing (this month), `today`, a month (`6`, `june`,
`Jun`), or a day (`06-14`, `6/14`, `june 14`, `14 june`).

A pure read over `.cache/index.sqlite`; nothing is written. The events
themselves come from `_lib.family_calendar_events`, the one engine the
report, workbench and site panels are to share. This command is private (the owner's own
terminal), so it shows everyone; shared output applies the living and
restricted rules itself from the flags each event carries.

The module is not called `calendar.py`: that name would shadow Python's own
`calendar` module, which `_lib` imports.

CODE MAP
--------
  parse_when            - the forgiving WHEN grammar -> [(month, day|None)]
  run_calendar          - engine: events for each requested date, as a Result
  _cmd_calendar         - render text or --json -> exit code
  register / _run_calendar / _standalone_main - CLI wiring
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
    EXIT_FAILURE,
    FhaConfigError,
    Result,
    configure_utf8_stdout,
    family_calendar_events,
    fmt_id_display,
    load_fha_yaml,
    open_index_db,
    resolve_root_arg,
)

configure_utf8_stdout()

_REQUIRED_TABLES = ('persons', 'claims', 'claim_persons', 'sources', 'places')

_MONTHS: dict[str, int] = {}
for _n in range(1, 13):
    _MONTHS[_stdcal.month_name[_n].lower()] = _n
    _MONTHS[_stdcal.month_abbr[_n].lower()] = _n
_MONTHS['sept'] = 9


def _month_of(word: str) -> int | None:
    w = word.strip().lower().rstrip('.')
    if w.isdigit():
        n = int(w)
        return n if 1 <= n <= 12 else None
    return _MONTHS.get(w)


def parse_when(when: str | None, *, today: datetime.date, week: bool = False
               ) -> list[tuple[int, int | None]] | None:
    """The dates a request covers, as [(month, day-or-None)], or None when
    the words cannot be read. `week` turns a day (today by default) into it
    and the six days after it. A month with `week` is refused (None): a week
    needs a starting day."""
    text = re.sub(r'(\d+)(st|nd|rd|th)\b', r'\1', (when or '').strip().lower())
    start: tuple[int, int | None]
    if not text:
        start = (today.month, today.day) if week else (today.month, None)
    elif text == 'today':
        start = (today.month, today.day)
    else:
        parts = [p for p in re.split(r'[\s/\-.,]+', text) if p]
        if len(parts) == 1:
            m = _month_of(parts[0])
            if m is None:
                return None
            start = (m, None)
        elif len(parts) == 2:
            a, b = parts
            if _month_of(a) is not None and b.isdigit() and not a.isdigit():
                m, d = _month_of(a), int(b)        # june 14
            elif _month_of(b) is not None and a.isdigit() and not b.isdigit():
                m, d = _month_of(b), int(a)        # 14 june
            elif a.isdigit() and b.isdigit():
                m, d = int(a), int(b)              # 06-14, 6/14 (month first)
            else:
                return None
            if m is None or not 1 <= m <= 12:
                return None
            try:
                datetime.date(2000, m, d)          # a leap year, so 02-29 is real
            except ValueError:
                return None
            start = (m, d)
        else:
            return None
    if not week:
        return [start]
    if start[1] is None:
        return None
    # The week runs on this year's calendar, so a February week in a non-leap
    # year has no 29th. Only a week asked to START on February 29 in a
    # non-leap year falls back to a leap year to have a first day at all.
    try:
        first = datetime.date(today.year, start[0], start[1])
    except ValueError:
        first = datetime.date(2000, start[0], start[1])
    return [((first + datetime.timedelta(days=i)).month,
             (first + datetime.timedelta(days=i)).day) for i in range(7)]


def run_calendar(archive_root: Path, fha_config: dict, *, when: str | None = None,
                 week: bool = False, found: bool = False,
                 today: datetime.date | None = None) -> Result:
    """Family events for the requested month, day or week. Read-only."""
    day0 = today or datetime.date.today()
    dates = parse_when(when, today=day0, week=week)
    if dates is None:
        r = Result(ok=False, exit_code=EXIT_ERRORS, data={'error': 'bad-date'})
        hint = ('--week needs a day to start from, e.g. `fha calendar today --week` or '
                '`fha calendar june 14 --week`.' if week and when else
                'Give a month or a day, e.g. `fha calendar june`, `fha calendar 6`, '
                '`fha calendar june 14`, `fha calendar 06-14`, or `fha calendar today`.')
        r.add('error', f'I could not read {when!r} as a month or a day. {hint}')
        return r

    conn = open_index_db(archive_root, _REQUIRED_TABLES)
    if conn is None:
        return Result(ok=False, exit_code=EXIT_FAILURE, data={'error': 'no-index'})
    statuses = ('accepted', 'suggested') if found else ('accepted',)
    try:
        groups = []
        for month, day in dates:
            # A week that crosses New Year counts its January days against
            # next year: 2 January 1880 is 147 years ago in the week of
            # 28 December 2026, not 146.
            year = day0.year + (1 if week and (month, day) < dates[0] else 0)
            groups.append({
                'month': month, 'day': day,
                'events': family_calendar_events(conn, month=month, day=day,
                                                 statuses=statuses, this_year=year),
            })
    finally:
        conn.close()
    return Result(data={'found': found, 'week': week, 'groups': groups})


# ── Rendering ────────────────────────────────────────────────────────────────

def _heading(month: int, day: int | None) -> str:
    name = _stdcal.month_name[month]
    return f'{day} {name}' if day else name


def _people_phrase(people: list[dict]) -> str:
    names = [f"{p['name']} [{fmt_id_display(p['id'])}]" for p in people]
    return names[0] if len(names) == 1 else ', '.join(names[:-1]) + ' and ' + names[-1]


def _event_line(ev: dict) -> str:
    when = (f"{ev['day']} {_stdcal.month_name[ev['month']]} {ev['year']}" if ev['day']
            else f"{_stdcal.month_name[ev['month']]} {ev['year']}")
    place = f", {ev['place']}" if ev['place'] else ''
    ago = ev['years_ago']
    ago_text = f'{ago} years ago' if ago != 1 else '1 year ago'
    # Under --found an unreviewed suggestion must not read as a family fact.
    mark = '' if ev.get('accepted', True) else '  [suggested, not yet reviewed]'
    return (f"  {when:<20} {ev['word']:<9} {_people_phrase(ev['people'])}{place} "
            f"({ago_text}){mark}")


def _cmd_calendar(result: Result, use_json: bool = False) -> int:
    if use_json:
        print(json.dumps(result.as_dict(), indent=2))
        return result.exit_code
    if not result.ok:
        for m in result.messages:
            print(f'ERROR: {m.text}', file=sys.stderr)
        return result.exit_code

    groups = result.data['groups']
    total = sum(len(g['events']) for g in groups)
    if result.data['week']:
        first, last = groups[0], groups[-1]
        print(f"The week of {_heading(first['month'], first['day'])} to "
              f"{_heading(last['month'], last['day'])} in the family: "
              f"{total} event{'s' if total != 1 else ''}")
    for g in groups:
        if not result.data['week']:
            print(f"{_heading(g['month'], g['day'])} in the family: "
                  f"{len(g['events'])} event{'s' if len(g['events']) != 1 else ''}")
        for ev in g['events']:
            print(_event_line(ev))
    if total == 0:
        print('  Nothing on file for these dates. Only dates recorded to the month '
              'or day appear here (1880-06 or 1880-06-14); a year alone, or an '
              '"about", cannot be placed on a calendar.')
    if not result.data['found']:
        print('(Accepted claims only; `--found` adds suggested ones.)')
    else:
        print('(Includes suggested claims; a line marked [suggested] rests on no '
              'accepted claim yet.)')
    return result.exit_code


# ── CLI ──────────────────────────────────────────────────────────────────────

_CLI_DESCRIPTION = """\
What happened in the family this month, or on this day?

  fha calendar                 This month: births, marriages, deaths, arrivals
  fha calendar june            Any month, by name or number
  fha calendar june 14         One day (also 06-14, 6/14, 14 june)
  fha calendar today           Today's anniversaries
  fha calendar --week          Today and the six days after it
  fha calendar --found         Count suggested claims too
  fha calendar --json          The same answer as machine-readable JSON

Only dates recorded to the month or day appear. Read-only: nothing is written."""


def _add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument('when', nargs='*', metavar='WHEN',
                   help='A month or a day (default: this month)')
    p.add_argument('--week', action='store_true',
                   help='The week starting on the day given (default: today)')
    p.add_argument('--found', action='store_true',
                   help='Count suggested claims as well as accepted ones')
    p.add_argument('--json', action='store_true', dest='use_json',
                   help='Machine-readable JSON output')
    p.add_argument('--root', metavar='PATH', help='Archive root (overrides auto-detection)')


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        'calendar',
        help='What happened in the family this month, or on this day?',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(p)
    p.set_defaults(func=_run_calendar)


def _run_calendar(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE
    result = run_calendar(archive_root, fha_config, when=' '.join(args.when) or None,
                          week=args.week, found=args.found)
    return _cmd_calendar(result, use_json=args.use_json)


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha calendar', description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    _add_arguments(parser)
    return _run_calendar(parser.parse_args(argv))


if __name__ == '__main__':
    sys.exit(_standalone_main())
