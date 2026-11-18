#!/usr/bin/env python3
"""
metrics.py - fha metrics: how well documented is each person?

  fha metrics [P-ID | --all] [--include-suggested] [--json] [--root PATH]

Answers "who still lacks a birth record, a census or a marriage record?" without
hand-written SQL. Five checks per person (TOOLING §7a):

  Birth           a birth or baptism record, or only later evidence
  Censuses        every census year they lived through in a country with a table
  Parents         both parents named by a sourced claim
  Marriage        each partner backed by a marriage record
  Death or burial on file, asked of anyone marked not living, or born over
                  130 years ago

plus archive-wide totals and the direct line's progress by generation.

A pure read over `.cache/index.sqlite`; nothing is written, and nothing it
computes is ever stored in a record (SPEC §17b - a gap a tool can compute is
not written down). The checks themselves live in `_lib` (`person_completeness`,
`archive_metrics`): the shared engine any other front door that shows
completeness (the workbench, the site) is to use, so the two cannot disagree.

By default only accepted claims count: the standard view says what the
archive has settled. `--include-suggested` counts suggested claims too, so it
shows what records have been found whether or not they are reviewed yet.

CODE MAP
--------
  _direct_line          - root_person -> {pid: Ahnentafel position}, or a note
  _generation_table     - direct line -> per-generation met/applicable counts
  run_metrics           - engine: compute everything, return a Result
  _cmd_metrics          - render a Result as text or --json -> exit code
  register / _run_metrics / _standalone_main - CLI wiring
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    COMPLETENESS_CHECK_LABELS,
    COMPLETENESS_CHECKS,
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    FhaConfigError,
    Result,
    ahnentafel_generation,
    archive_metrics,
    build_ahnentafel_map,
    completeness_context,
    completeness_summary_line,
    configure_utf8_stdout,
    fmt_id_display,
    id_type_of,
    is_valid_id,
    load_fha_yaml,
    normalize_id,
    open_index_db,
    person_completeness,
    resolve_root_arg,
    resolve_root_generation,
    root_generation_seed_position,
)

configure_utf8_stdout()

_REQUIRED_TABLES = ('persons', 'claims', 'claim_persons', 'sources', 'source_files',
                    'places', 'relationships')

_STATE_MARK = {'met': 'yes', 'partial': 'part', 'missing': 'no', 'na': '-'}


def _direct_line(conn, fha_config: dict) -> tuple[dict[str, int], str | None]:
    """{pid: Ahnentafel position} for the direct line, plus a plain note when
    it cannot be walked. The walk is `build_ahnentafel_map`, seeded the way
    every other direct-line tool seeds it (root_person at its root_generation
    slot), so the generations here match the numbered folders."""
    raw = fha_config.get('root_person')
    if not raw:
        return {}, ('No direct line: fha.yaml has no root_person. Add '
                    '`root_person: p-...` (your own person ID) to see your '
                    'ancestors by generation.')
    configured = root = normalize_id(str(raw))
    # A root_person later merged into another record is read through the
    # merge (SPEC §9): the survivor holds their claims and their parents.
    seen: set[str] = set()
    while True:
        row = conn.execute("SELECT merged_into FROM persons WHERE id=? AND status='merged'",
                           (root,)).fetchone()
        if not row or not row['merged_into']:
            break
        seen.add(root)
        root = normalize_id(str(row['merged_into']))
        if root in seen:
            return {}, (f"No direct line: fha.yaml's root_person {fmt_id_display(configured)} "
                        'is part of a merge loop (records merged into each other). Open those '
                        'records and fix their merged_into: lines, then run `fha index`.')
    if not conn.execute('SELECT 1 FROM persons WHERE id=?', (root,)).fetchone():
        via = (f', which was merged into {fmt_id_display(root)},'
               if root != configured else '')
        return {}, (f"No direct line: fha.yaml's root_person {fmt_id_display(configured)}{via} "
                    f'is not in the index. Check it with `fha find {fmt_id_display(root)}`, '
                    'or run `fha index` if you added that person recently.')
    try:
        position = root_generation_seed_position(resolve_root_generation(fha_config))
    except FhaConfigError as e:
        return {}, f'No direct line: {e}'
    return build_ahnentafel_map(conn, root, root_position=position), None


def _generation_table(people: list[dict], direct: dict[str, int]) -> list[dict]:
    """Per generation: how many people, and for each check how many meet it
    out of how many it applies to. Generation 0 is root_person."""
    rows: dict[int, dict] = {}
    for p in people:
        gen = ahnentafel_generation(direct[p['person']])
        row = rows.setdefault(gen, {'generation': gen, 'people': 0,
                                    'checks': {k: [0, 0] for k in COMPLETENESS_CHECKS}})
        row['people'] += 1
        for c in p['checks']:
            if c['state'] != 'na':
                row['checks'][c['check']][1] += 1
                if c['state'] == 'met':
                    row['checks'][c['check']][0] += 1
    return [rows[g] for g in sorted(rows)]


def run_metrics(archive_root: Path, fha_config: dict, *, person: str | None = None,
                everyone: bool = False, mode: str = 'accepted',
                this_year: int | None = None) -> Result:
    """Compute completeness for one person, the direct line, or everyone, plus
    the archive-wide totals. Read-only; returns a Result whose `data` is the
    whole answer (the text renderer and `--json` both read it)."""
    if person is not None and everyone:
        r = Result(ok=False, exit_code=EXIT_ERRORS, data={'error': 'person-and-all'})
        r.add('error', 'A person ID shows one person and --all lists everyone, so '
                       'they cannot go together. Run `fha metrics <p-id>` or '
                       '`fha metrics --all`.')
        return r
    if person is not None:
        pid = normalize_id(person)
        if not is_valid_id(pid) or id_type_of(pid) != 'P':
            r = Result(ok=False, exit_code=EXIT_ERRORS, data={'error': 'bad-id'})
            r.add('error', f'{person!r} is not a person ID. Give one like '
                           '`fha metrics p-de957bcda1`, or leave it off for the whole '
                           'direct line. `fha find "<name>"` looks an ID up by name.')
            return r

    conn = open_index_db(archive_root, _REQUIRED_TABLES)
    if conn is None:
        return Result(ok=False, exit_code=EXIT_FAILURE, data={'error': 'no-index'})
    try:
        ctx = completeness_context(conn)
        direct, direct_note = _direct_line(conn, fha_config)
        year = this_year if this_year is not None else datetime.date.today().year

        if person is not None:
            if pid not in ctx['persons']:
                merged = conn.execute(
                    "SELECT merged_into FROM persons WHERE id=? AND status='merged'",
                    (pid,)).fetchone()
                r = Result(ok=False, exit_code=EXIT_WARNINGS, data={'error': 'no-person'})
                if merged and merged['merged_into']:
                    into = fmt_id_display(str(merged['merged_into']))
                    r.add('warning', f'{fmt_id_display(pid)} was merged into {into}, whose '
                                     f'record now carries their claims. Run `fha metrics {into}`.')
                else:
                    r.add('warning', f'No person {fmt_id_display(pid)} in the index. Check the '
                                     f'ID with `fha find {fmt_id_display(pid)}`, or run '
                                     '`fha index` if the record is new.')
                return r
            one = person_completeness(ctx, pid, mode=mode, this_year=year)
            one['name'] = ctx['persons'][pid]['name']
            one['position'] = direct.get(pid)
            one['summary'] = completeness_summary_line(one)
            return Result(data={'mode': mode, 'person': one})

        if everyone:
            scope = 'everyone'
            pids = sorted(ctx['persons'], key=lambda p: (ctx['persons'][p]['name'] or '', p))
        elif direct:
            scope = 'direct-line'
            pids = sorted((p for p in direct if p in ctx['persons']), key=lambda p: direct[p])
        else:
            scope = 'curated'
            pids = sorted((p for p, r in ctx['persons'].items() if r.get('tier') == 'curated'),
                          key=lambda p: (ctx['persons'][p]['name'] or '', p))

        people = []
        for p in pids:
            one = person_completeness(ctx, p, mode=mode, this_year=year)
            one['name'] = ctx['persons'][p]['name']
            one['position'] = direct.get(p)
            one['summary'] = completeness_summary_line(one)
            people.append(one)

        in_line = [p for p in people if p['person'] in direct]
        data = {
            'mode': mode,
            'scope': scope,
            'archive': archive_metrics(conn, ctx, direct_line=direct or None),
            'direct_line_note': direct_note,
            'generations': _generation_table(in_line, direct) if in_line else [],
            'people': people,
        }
        return Result(data=data)
    finally:
        conn.close()


# ── Rendering ────────────────────────────────────────────────────────────────

def _label(p: dict) -> str:
    pos = f"#{p['position']}  " if p.get('position') else ''
    return f"{pos}{p['name']} [{fmt_id_display(p['person'])}]"


def _count(n: int, noun: str) -> str:
    return f'{n} {noun}' + ('' if n == 1 else 's')


def _mode_phrase(mode: str) -> str:
    return ('counting accepted claims only' if mode == 'accepted'
            else 'counting suggested and accepted claims (records found, reviewed or not)')


def _print_person(p: dict, mode: str) -> None:
    print(f"{_label(p)}: {p['summary']}")
    print(f'  ({_mode_phrase(mode)})')
    for c in p['checks']:
        mark = _STATE_MARK[c['state']]
        print(f"  {COMPLETENESS_CHECK_LABELS[c['check']]:<17} {mark:<5} {c['detail']}")


def _print_archive(data: dict) -> None:
    a = data['archive']
    ppl = a['people']
    line = f"{ppl['total']} people ({ppl['curated']} curated, {ppl['stubs']} stubs"
    line += f", {ppl['direct_line']} on the direct line)" if ppl['direct_line'] else ')'
    print(line)
    src = sum(a['sources_by_type'].values())
    by_type = ', '.join(f'{n} {t}' for t, n in sorted(
        a['sources_by_type'].items(), key=lambda kv: -kv[1]))
    print(f'{src} sources: {by_type}' if src else '0 sources')
    cl = a['claims_by_status']
    print(f"{sum(cl.values())} claims: " + ', '.join(f'{n} {s}' for s, n in cl.items()))
    med = a['per_curated_person']
    if med['median_sources'] is not None:
        print(f"Per curated person (median): {med['median_sources']:g} sources, "
              f"{med['median_accepted_claims']:g} accepted claims")
    st = a['searchable_text']
    if st['sources_with_files']:
        pct = round(100 * st['sources_with_text'] / st['sources_with_files'])
        print(f"Searchable text: {st['sources_with_text']} of {st['sources_with_files']} "
              f'sources with files ({pct}%); the rest are scans or recordings a search '
              'cannot read (see `fha lint` W124)')
    if a['thinnest_direct_line']:
        print('Thinnest on the direct line:')
        for t in a['thinnest_direct_line']:
            print(f"  #{t['position']}  {t['name']} [{fmt_id_display(t['person'])}]: "
                  f"{_count(t['accepted_claims'], 'accepted claim')} from "
                  f"{_count(t['sources'], 'source')}")


def _print_generations(rows: list[dict]) -> None:
    heads = [COMPLETENESS_CHECK_LABELS[k].split()[0] for k in COMPLETENESS_CHECKS]
    print('Gen  People  ' + '  '.join(f'{h:<9}' for h in heads))
    for r in rows:
        cells = []
        for k in COMPLETENESS_CHECKS:
            met, n = r['checks'][k]
            cells.append(f'{met}/{n}' if n else '-')
        print(f"{r['generation']:<4} {r['people']:<7} " + '  '.join(f'{c:<9}' for c in cells))


def _cmd_metrics(result: Result, use_json: bool = False) -> int:
    """Render a metrics Result and return its exit code. The only layer that
    prints."""
    if use_json:
        print(json.dumps(result.as_dict(), indent=2))
        return result.exit_code
    if not result.ok:
        for m in result.messages:
            prefix = 'ERROR' if m.level == 'error' else 'WARNING'
            print(f'{prefix}: {m.text}', file=sys.stderr)
        return result.exit_code

    data = result.data
    if 'person' in data:
        _print_person(data['person'], data['mode'])
        return result.exit_code

    _print_archive(data)
    print()
    if data['direct_line_note']:
        print(data['direct_line_note'])
        print()
    if data['generations']:
        print(f"Direct line by generation ({_mode_phrase(data['mode'])}; met / applies):")
        _print_generations(data['generations'])
        print()
    heading = {'direct-line': 'Direct line, closest first',
               'curated': 'Curated people',
               'everyone': 'Everyone'}[data['scope']]
    print(f'{heading}:')
    if not data['people']:
        print('  (nobody yet)')
    for p in data['people']:
        print(f"  {_label(p)}: {p['summary']}")
    print()
    print('Run `fha metrics <p-id>` for one person in detail; `--include-suggested` '
          'counts suggested claims too.')
    return result.exit_code


# ── CLI ──────────────────────────────────────────────────────────────────────

_CLI_DESCRIPTION = """\
How well documented is each person?

  fha metrics               The archive in numbers, then the direct line by generation
  fha metrics p-xxxxxxxxxx  One person's five checks, each with what is missing
  fha metrics --all         Every person, stubs included
  fha metrics --include-suggested
                            Count suggested claims too (records found, not yet reviewed)
  fha metrics --json        The same answer as machine-readable JSON

The five checks: a birth record; every census they lived through; both parents
named by a sourced claim; a marriage record for each partner; and, for anyone
marked not living or born over 130 years ago, a death or burial. Read-only:
nothing is written."""


def _add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument('person', nargs='?', metavar='P-ID',
                   help='One person to show in detail (default: the direct line)')
    p.add_argument('--all', action='store_true', dest='everyone',
                   help='List every person, stubs included (not with a P-ID)')
    p.add_argument('--include-suggested', action='store_true', dest='include_suggested',
                   help='Count suggested claims as well as accepted ones')
    p.add_argument('--json', action='store_true', dest='use_json',
                   help='Machine-readable JSON output')
    p.add_argument('--root', metavar='PATH', help='Archive root (overrides auto-detection)')


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        'metrics',
        help='How well documented is each person? Completeness checks + archive totals.',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(p)
    p.set_defaults(func=_run_metrics)


def _run_metrics(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE
    result = run_metrics(
        archive_root, fha_config, person=args.person, everyone=args.everyone,
        mode='include-suggested' if args.include_suggested else 'accepted')
    return _cmd_metrics(result, use_json=args.use_json)


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha metrics', description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    _add_arguments(parser)
    return _run_metrics(parser.parse_args(argv))


if __name__ == '__main__':
    sys.exit(_standalone_main())
