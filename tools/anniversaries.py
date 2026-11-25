#!/usr/bin/env python3
"""
anniversaries.py - fha calendar: what happened in the family this month?

  fha calendar [WHEN] [--week] [--include-suggested] [--json] [--root PATH]
  fha calendar --birthdays [--days N] [--json] [--root PATH]
  fha calendar --format html [--year YYYY] [--include-suggested] [--root PATH]

Births, baptisms, marriages, deaths, burials and arrivals whose recorded date
falls in a month, on a day, or in the week ahead - "On this day in 1880,
Thomas Hartley was born in Fairview" - read from the archive's own claims
(TOOLING §7b). The archive's version of a newspaper's "on this day" column.

WHEN is forgiving: nothing (this month), `today`, a month (`6`, `june`,
`Jun`), or a day (`06-14`, `6/14`, `june 14`, `14 june`).

A pure read over `.cache/index.sqlite`; nothing is written. The events
themselves come from `_lib.family_calendar_events`, the shared engine the
report, the workbench and the site's calendar panel use too. This command is
private (the owner's own terminal), so it shows everyone. Shared output must
apply SPEC §19 itself: the living and source-restricted flags each event
carries are a start, but a restricted claim or person is not flagged.

`--birthdays` is the owner's private list of living relatives' coming
birthdays ("Margaret turns 80 on 14 June, in 12 days") so he can send a card.
It prints to his terminal only; `fha site`, `fha packet`, the exports and the
workbench never read it (SPEC §21).

`--format html` writes a twelve-month wall calendar for one year to
generated/views/calendar_{year}.html: a private page to print and hang up.
It names living relatives too, so it carries the private-companion banner;
sharing goes through the site's calendar page, not this file.

The module is not called `calendar.py`: that name would shadow Python's own
`calendar` module, which `_lib` imports.

CODE MAP
--------
  parse_when            - the forgiving WHEN grammar -> [(month, day|None)]
  run_calendar          - engine: events for each requested date, as a Result
  _cmd_calendar         - render text or --json -> exit code
  run_birthdays / _cmd_birthdays - the private living-birthdays list
  run_calendar_wall     - engine + writer: the wall calendar page, as a Result
  _WALL_MARKER, _WALL_CSS - the page's frame; the month grid is _lib.wall_month_html
  register / _run_calendar / _standalone_main - CLI wiring
"""

from __future__ import annotations

import argparse
import calendar as _stdcal
import datetime
import html
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
    GeneratedFileParentMissing,
    GeneratedFileRefused,
    Result,
    archive_title,
    calendar_years_ago_phrase,
    LIVING_CUTOFF_YEARS,
    configure_utf8_stdout,
    family_calendar_events,
    family_calendar_year,
    wall_month_html,
    fmt_id_display,
    living_birthdays,
    load_fha_yaml,
    load_view_css,
    open_index_db,
    render_template,
    resolve_root_arg,
    write_generated_file,
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
                 week: bool = False, include_suggested: bool = False,
                 today: datetime.date | None = None) -> Result:
    """Family events for the requested month, day or week. Read-only."""
    day0 = today or datetime.date.today()
    dates = parse_when(when, today=day0, week=week)
    if dates is None:
        r = Result(ok=False, exit_code=EXIT_ERRORS, data={'error': 'bad-date'})
        if week and parse_when(when, today=day0) is not None:
            # A readable month with --week: the words were fine, a week just
            # needs a first day.
            r.add('error', f'A week needs a day to start from, and {when!r} is a whole '
                  'month. Try `fha calendar --week` (from today), '
                  '`fha calendar today --week` or `fha calendar june 14 --week`.')
        else:
            r.add('error', f'I could not read {when!r} as a month or a day. Give a '
                  'month or a day, e.g. `fha calendar june`, `fha calendar 6`, '
                  '`fha calendar june 14`, `fha calendar 06-14`, or `fha calendar today`.')
        return r

    conn = open_index_db(archive_root, _REQUIRED_TABLES)
    if conn is None:
        return Result(ok=False, exit_code=EXIT_FAILURE, data={'error': 'no-index'})
    statuses = ('accepted', 'suggested') if include_suggested else ('accepted',)
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
    return Result(data={'include_suggested': include_suggested, 'week': week,
                        'groups': groups})


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
    ago_text = calendar_years_ago_phrase(ev['years_ago'])
    # Under --include-suggested an unreviewed suggestion must not read as a
    # family fact.
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
    if not result.data['include_suggested']:
        print('(Accepted claims only; `--include-suggested` adds suggested ones.)')
    else:
        print('(Includes suggested claims; a line marked [suggested, not yet reviewed] '
              'rests only on suggested claims.)')
    return result.exit_code


# ── Living relatives' birthdays (private) ───────────────────────────────────

BIRTHDAY_CONFLICT = ('--birthdays looks ahead from today on its own; leave out the '
                     'other options. Try: fha calendar --birthdays --days 60')


def run_birthdays(archive_root: Path, *, days: int = 30,
                  today: datetime.date | None = None) -> Result:
    """Living relatives' birthdays in the next `days` days. Read-only, and for
    the owner's terminal only: nothing here may reach a shared output."""
    if not 1 <= days <= 366:
        r = Result(ok=False, exit_code=EXIT_ERRORS, data={'error': 'bad-days'})
        r.add('error', f'--days takes a number from 1 to 366, not {days}. '
              'Try: fha calendar --birthdays --days 60')
        return r
    conn = open_index_db(archive_root, _REQUIRED_TABLES)
    if conn is None:
        return Result(ok=False, exit_code=EXIT_FAILURE, data={'error': 'no-index'})
    try:
        found = living_birthdays(conn, today or datetime.date.today(), days)
    finally:
        conn.close()
    return Result(data={'days': days, **found})


def _in_days(n: int) -> str:
    return '(today)' if n == 0 else '(tomorrow)' if n == 1 else f'(in {n} days)'


def _cmd_birthdays(result: Result, use_json: bool = False) -> int:
    if use_json:
        print(json.dumps(result.as_dict(), indent=2))
        return result.exit_code
    if not result.ok:
        for m in result.messages:
            print(f'ERROR: {m.text}', file=sys.stderr)
        return result.exit_code
    d = result.data
    n = d['days']
    if not d['upcoming']:
        print(f'No birthdays of living relatives in the next {n} days.')
    else:
        print(f'Birthdays of living relatives in the next {n} days '
              '(for you only; not for sharing)')
        for e in d['upcoming']:
            when = datetime.date.fromisoformat(e['birthday'])
            note = ' (born 29 February)' if e['feb29'] else ''
            print(f"  {when.day} {_stdcal.month_name[when.month]:<9} {e['name']} "
                  f"turns {e['age']} {_in_days(e['days_away'])}{note}")
    if d['too_old']:
        who = ', '.join(f"{e['name']} [{fmt_id_display(e['id'])}]" for e in d['too_old'])
        count = len(d['too_old'])
        print(f"Left off: {count} {'person' if count == 1 else 'people'} born more "
              f"than {LIVING_CUTOFF_YEARS} years ago with no death recorded: {who}. "
              'If they have died, record it with: fha person set-living <p-id> false')
    tail = ''
    if d['inexact']:
        k = d['inexact']
        tail = (f" {k} living relative{' has' if k == 1 else 's have'} a birth date "
                'not recorded to the exact day.')
    print('Only birthdays recorded to the day appear (1946-06-14).' + tail)
    return result.exit_code


# ── The wall calendar (--format html) ────────────────────────────────────────

# The marker the views use, so `fha views clean` sweeps this file and lint
# reads it as generated.
_WALL_MARKER = '<!-- GENERATED by fha views calendar'

_WALL_CSS = """
.wall-month-block { margin-bottom: 2rem; }
.wall-month { width: 100%; table-layout: fixed; border-collapse: collapse; }
.wall-month th { font-size: 0.75rem; font-weight: 600; text-align: left; padding: 0.15rem 0.3rem; }
.wall-month td { height: 6.5rem; vertical-align: top; border: 1px solid var(--rule); font-size: 0.75rem; padding: 0.15rem 0.3rem; overflow: hidden; }
.wall-month .day-num { font-weight: 600; }
.wall-month .wall-line { display: block; line-height: 1.25; }
.wall-legend, .wall-more { font-size: 0.8rem; margin: 0.4rem 0; }
@media print {
  @page { size: landscape; margin: 10mm; }
  .wall-month-block { break-after: page; break-inside: avoid; margin-bottom: 0; }
  .wall-month td { height: 4rem; }
  main { padding: 0; }
  h1 { font-size: 1.3rem; margin: 0.2rem 0; }
  .view-banner { margin: 0 0 0.3rem; padding: 0.2rem 0.5rem; }
}
"""


def run_calendar_wall(archive_root: Path, fha_config: dict, year: int, *,
                      include_suggested: bool = False,
                      today: datetime.date | None = None) -> Result:
    """Write generated/views/calendar_{year}.html: twelve months of the
    family's births, marriages and deaths, to print. Private: living relatives
    are on it, under the same banner every private view carries."""
    if not 1000 <= year <= 9999:
        r = Result(ok=False, exit_code=EXIT_ERRORS, data={'error': 'bad-year'})
        r.add('error', f'{year} is not a year I can print. Give a four-digit year, e.g. '
              '`fha calendar --format html --year 2027`.')
        return r
    conn = open_index_db(archive_root, _REQUIRED_TABLES)
    if conn is None:
        r = Result(ok=False, exit_code=EXIT_FAILURE, data={'error': 'no-index'})
        r.add('error', 'There is no index yet. Run `fha index`, then try again.')
        return r
    statuses = ('accepted', 'suggested') if include_suggested else ('accepted',)
    try:
        months = family_calendar_year(conn, year, statuses)
    finally:
        conn.close()
    count = sum(len(v) for m in months for v in m['days'].values())
    count += sum(len(m['month_only']) for m in months)

    day0 = today or datetime.date.today()
    css, css_warning = load_view_css('the wall calendar')
    body = '\n'.join(wall_month_html(year, m) for m in months)
    text = render_template(
        'view.html',
        marker=f'{_WALL_MARKER} on {day0.isoformat()} - do not edit; regenerate instead -->\n',
        title=f'Family calendar {year}', subcommand='calendar',
        generator='fha calendar --format html', date=day0.isoformat(),
        archive_title=archive_title(fha_config or load_fha_yaml(archive_root)),
        css=css + _WALL_CSS, body=body, banner=None) + '\n'
    out = archive_root / 'generated' / 'views' / f'calendar_{year}.html'
    try:
        write_generated_file(out, text, _WALL_MARKER, create_parents=True)
    except (GeneratedFileRefused, GeneratedFileParentMissing):
        r = Result(ok=False, exit_code=EXIT_ERRORS, data={'error': 'not-ours'})
        r.add('error', f'generated/views/calendar_{year}.html is not a file fha made, so it '
              'was left alone. Move it, or pass a different --year.')
        return r
    r = Result(data={'path': str(out), 'year': year, 'events': count,
                     'include_suggested': include_suggested})
    if css_warning:
        r.add('warning', css_warning)
    return r


def _cmd_calendar_wall(result: Result) -> int:
    if not result.ok:
        for m in result.messages:
            print(f'ERROR: {m.text}', file=sys.stderr)
        return result.exit_code
    for m in result.messages:
        print(m.text, file=sys.stderr)
    d = result.data
    print(f"Wrote the {d['year']} family calendar: {d['path']}. Open it in a browser and "
          'print it; each month prints on its own page.')
    if d['events'] == 0:
        print('No family dates recorded to the month or day were found. A year alone, or an '
              '"about" date, cannot be placed on a calendar.')
    return result.exit_code


# ── CLI ──────────────────────────────────────────────────────────────────────

_CLI_DESCRIPTION = """\
What happened in the family this month, or on this day?

  fha calendar                 This month: births, marriages, deaths, arrivals
  fha calendar june            Any month, by name or number
  fha calendar june 14         One day (also 06-14, 6/14, 14 june)
  fha calendar today           Today's anniversaries
  fha calendar --week          Today and the six days after it
  fha calendar --include-suggested
                               Count suggested claims too, each marked
  fha calendar --json          The same answer as machine-readable JSON
  fha calendar --birthdays     Living relatives' birthdays in the next 30 days
                               (private: for you only, never in a shared copy)
  fha calendar --birthdays --days 60
  fha calendar --format html   A printable wall calendar for this year
  fha calendar --format html --year 2027
                               The same, for another year

Only dates recorded to the month or day appear. The text views write nothing;
the wall calendar writes one private page under generated/views/ and names
living relatives, so keep it to yourself."""


def _add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument('when', nargs='*', metavar='WHEN',
                   help='A month or a day (default: this month)')
    p.add_argument('--week', action='store_true',
                   help='The week starting on the day given (default: today)')
    p.add_argument('--include-suggested', action='store_true',
                   help='Count suggested claims as well as accepted ones; each '
                        'line resting only on suggested claims is marked')
    p.add_argument('--birthdays', action='store_true',
                   help="Living relatives' coming birthdays (private; for you only)")
    p.add_argument('--days', type=int, default=None, metavar='N',
                   help='With --birthdays: how many days ahead (1 to 366, default 30)')
    p.add_argument('--json', action='store_true', dest='use_json',
                   help='Machine-readable JSON output')
    p.add_argument('--format', choices=('text', 'html'), default='text',
                   help='text (default), or html for a printable twelve-month wall '
                        'calendar under generated/views/')
    p.add_argument('--year', type=int, metavar='YYYY',
                   help='With --format html: the year to print (default: this year)')
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
    if args.birthdays:
        if (args.when or args.week or args.include_suggested
                or args.format == 'html' or args.year is not None):
            print(f'ERROR: {BIRTHDAY_CONFLICT}', file=sys.stderr)
            return EXIT_ERRORS
        return _cmd_birthdays(run_birthdays(archive_root, days=30 if args.days is None
                                            else args.days), use_json=args.use_json)
    if args.days is not None:
        print('ERROR: --days goes with --birthdays. Try: fha calendar --birthdays '
              '--days 60', file=sys.stderr)
        return EXIT_ERRORS
    if args.format == 'html':
        if args.when or args.week or args.use_json:
            print('ERROR: The wall calendar always shows the whole year, so leave out the '
                  'month or day. Try: fha calendar --format html --year 2027',
                  file=sys.stderr)
            return EXIT_ERRORS
        year = args.year if args.year is not None else datetime.date.today().year
        return _cmd_calendar_wall(run_calendar_wall(
            archive_root, fha_config, year, include_suggested=args.include_suggested))
    if args.year is not None:
        print('ERROR: --year only works with --format html, the printable wall calendar. '
              'Try: fha calendar --format html --year 2027', file=sys.stderr)
        return EXIT_ERRORS
    result = run_calendar(archive_root, fha_config, when=' '.join(args.when) or None,
                          week=args.week, include_suggested=args.include_suggested)
    return _cmd_calendar(result, use_json=args.use_json)


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha calendar', description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    _add_arguments(parser)
    return _run_calendar(parser.parse_args(argv))


if __name__ == '__main__':
    sys.exit(_standalone_main())
