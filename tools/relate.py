#!/usr/bin/env python3
"""
relate.py - fha relate: how are two people related?

  fha relate <P-A> <P-B> [--json] [--root PATH]

Answers three questions, all derived from accepted relationship claims and
never stored (a layer-3 generated view, SPEC §3 / Part IV):

  Blood:        the genealogical relationship (second cousin once removed,
                great-grandmother, half-brother, ...) found via the lowest
                common ancestor over GENETIC parent edges only - adoptive /
                step / foster / guardian parents are not counted into the
                blood answer (SPEC §12.2). When a parent on the connecting
                line is unrecorded, a second line says the tie may be half.
                Two people related more than one way hear every line
                ("double first cousin", or "and also ... second cousin,
                through another line").
  By marriage:  only when there is no blood answer: wife, brother-in-law,
                stepmother, adoptive father, uncle by marriage. A step tie
                the dates cannot settle gets a second line saying so.
  Path:         the shortest readable chain between the two over ALL
                relationship edges (parent / child / spouse / sibling /
                friend / associate / neighbor), e.g. "your brother's
                friend's sister's father".

They are reported separately because they answer different questions: the
blood degree, the family tie through a marriage or an adoption, and the
shortest social connection (which may run through people who are not
relatives at all).

This is a pure read query over `.cache/index.sqlite`; an absent index is a hard
error (run `fha index`), never a silent wrong answer. SPEC Part IV, TOOLING §7.

HOW IT WORKS
------------
The engine lives in `_lib`'s kinship section, shared with `fha site`, so the
command and the family website always use the same words. This file is the
command around it:
  * `kin_graph` loads one graph from the `relationships` table (joined to
    `claims` for each edge's nature): a GENETIC parent map for the blood
    answer, and an undirected adjacency (every parent/child/spouse/social
    edge, plus derived sibling edges from shared parents) for the path.
  * `kinship_answer` gives every blood route over the genetic map (one
    common ancestor or a couple), nearest first, named by generation and
    removal (`kinship_word`), with its half state, the double word for two
    full routes at one cousin degree and, when half is open, a note;
    and, only when there is no blood route, the tie by marriage
    (`marriage_answer`), whose step-parent date rule reads each person's
    birth and death dates (`person_life_dates`).
  * `kinship_path` is a BFS over the adjacency, rendered hop by hop.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    configure_utf8_stdout,
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    FhaConfigError,
    Result,
    blood_answer,
    blood_routes,
    cm_fits,
    expected_shared_cm,
    fmt_id_display,
    id_type_of,
    is_valid_id,
    kin_graph,
    kinship_relatives,
    kinship_answer,
    kinship_path,
    link_writer,
    load_fha_yaml,
    normalize_id,
    open_index_db,
    parse_cm,
    person_life_dates,
    person_name_refusal,
    persons_named,
    resolve_root_arg,
    shared_cm_table,
)

configure_utf8_stdout()

# ── CODE MAP ──────────────────────────────────────────────────────────────────
#
#  The engine is _lib's "Kinship" section (kin_graph, kinship_answer,
#  blood_answer, marriage_answer, kinship_path, kinship_word); nothing in this
#  file names a relationship.
#
#  Engine / interface
#    run_relate               - resolve the two people, ask _lib for the
#                               answers, return a Result
#    _cmd_relate              - render the Result (text or --json) -> exit code
#    register / _run_relate / _standalone_main - CLI wiring
#
# ─────────────────────────────────────────────────────────────────────────────

_REQUIRED_TABLES = ('persons', 'relationships', 'claims', 'claim_persons', 'aliases')


# ── Engine / interface ────────────────────────────────────────────────────────

_EXAMPLE_ONE = 'fha relate P-de957bcda1 --all'
_EXAMPLE_TWO = 'fha relate P-de957bcda1 P-83e768cacb'


def run_relate(archive_root: Path, fha_config: dict, person_a: str | None,
               person_b: str | None = None, cm: str | None = None, *,
               everyone: bool = False) -> Result:
    """Compute the blood relationship, the tie by marriage and the shortest path between two
    people and return a structured Result; with `everyone`, list everyone
    related to one person instead. A pure read over the index; never
    writes. The text formatter and `--json` both render this one Result.

    `cm` is the shared DNA a match page shows. With two people it says whether
    that amount fits their recorded tie; with no people it is the lookup alone
    (no index needed)."""
    if everyone and cm is not None:
        return _usage_error(archive_root,
                            '`--all` and `--cm` do not go together. To list everyone related '
                            f'to one person, for example `{_EXAMPLE_ONE}`.')
    if everyone and person_b:
        return _usage_error(archive_root,
                            '`--all` lists everyone related to one person. Give one name or ID, '
                            f'for example `{_EXAMPLE_ONE}`.')
    if everyone and not person_a:
        return _usage_error(archive_root,
                            '`--all` lists everyone related to one person. Give one name or ID, '
                            f'for example `{_EXAMPLE_ONE}`.')
    cm_value = None
    if cm is not None:
        cm_value = parse_cm(cm)
        if cm_value is None:
            return _usage_error(archive_root, '--cm needs a number of centimorgans, for example '
                                '--cm 450 (from the match page: "Shared DNA: 450 cM").')
        if person_a and not person_b:
            return _usage_error(archive_root, '--cm goes with two people (to check their tie) '
                                'or with none (to list what an amount fits), not one.')
    if not person_a and not person_b:
        if cm_value is None:
            return _usage_error(archive_root,
                                f'Give two people to see how they are related, for example '
                                f'`{_EXAMPLE_TWO}`, one person and `--all` to list everyone '
                                f'related to them, for example `{_EXAMPLE_ONE}`, or an amount '
                                'of shared DNA, for example `fha relate --cm 450`.')
        return _cm_lookup(cm_value)
    if not everyone and not person_b:
        return _usage_error(archive_root,
                            f'Give two people to see how they are related, for example '
                            f'`{_EXAMPLE_TWO}`, or add --all to list everyone related to this one.')
    a_raw, b_raw = person_a, person_b or person_a
    pid_a, pid_b = normalize_id(a_raw), normalize_id(b_raw)
    for raw, pid in ((a_raw, pid_a), (b_raw, pid_b)):
        if is_valid_id(pid) and id_type_of(pid) != 'P':
            msg = (f'{raw!r} is not a person ID. Give two P-ids or full names, '
                   f'e.g. `{_EXAMPLE_TWO}`.')
            r = Result(ok=False, exit_code=EXIT_FAILURE, data={'error': 'bad-id'})
            r.add('error', msg, path=archive_root)
            return r

    conn = open_index_db(archive_root, _REQUIRED_TABLES)
    if conn is None:
        # open_index_db already printed the cause + the `fha index` next step.
        return Result(ok=False, exit_code=EXIT_FAILURE, data={'error': 'no-index'})

    # The connection stays open until the answers are computed: the
    # step-parent date rule reads birth and death dates from it.
    try:
        try:
            # A name: one person carrying it is the person meant; two or
            # more, or nobody, is refused with the choices and the next step.
            resolved = []
            for raw, pid in (((a_raw, pid_a),) if everyone else ((a_raw, pid_a), (b_raw, pid_b))):
                if not is_valid_id(pid):
                    matches = persons_named(conn, raw)
                    if len(matches) != 1:
                        r = Result(ok=False, exit_code=EXIT_FAILURE,
                                   data={'error': 'ambiguous-name' if matches else 'bad-id',
                                         'candidates': [m['id'] for m in matches]})
                        r.add('error', person_name_refusal(raw, matches, '`fha relate`'),
                              path=archive_root)
                        return r
                    pid = normalize_id(matches[0]['id'])
                resolved.append(pid)
            pid_a = resolved[0]
            pid_b = pid_a if everyone else resolved[1]
            graph = kin_graph(conn)
        except Exception:
            r = Result(ok=False, exit_code=EXIT_FAILURE, data={'error': 'bad-schema'})
            r.add('error',
                  '.cache/index.sqlite is unreadable or has an incompatible schema. '
                  'Run `fha index` to rebuild it.', path=archive_root)
            return r

        people = graph.people
        for raw, pid in ((a_raw, pid_a), (b_raw, pid_b)):
            if pid not in people:
                r = Result(ok=False, exit_code=EXIT_FAILURE, data={'error': 'no-person'})
                r.add('error',
                      f'No person {fmt_id_display(pid)} in the index. '
                      f'Run `fha find {fmt_id_display(pid)}` to check the ID, or `fha index` to rebuild.',
                      path=archive_root)
                return r

        vital_cache: dict = {}
        if everyone:
            return _everyone_result(archive_root, fha_config, conn, graph, pid_a, vital_cache)
        answers = kinship_answer(graph, pid_a, pid_b,
                                 life=lambda p: person_life_dates(conn, p, vital_cache))
        path = kinship_path(graph, pid_a, pid_b)
        n_routes = len(blood_routes(graph, pid_a, pid_b))
    finally:
        try:
            conn.close()
        except Exception:
            pass
    blood, by_marriage = answers['blood'], answers['by_marriage']

    # Display names for every person the renderer will name (the pair, any
    # common ancestors, and the people a tie by marriage runs through), so
    # the text output reads with names, not bare IDs.
    names = {pid_a: people[pid_a]['name'], pid_b: people[pid_b]['name']}
    route_ancestors = [c for r in (blood['routes'] if blood else [])
                       for c in r['common_ancestors']]
    for c in route_ancestors + (by_marriage['via'] if by_marriage else []):
        names[c] = people.get(c, {}).get('name') or fmt_id_display(c)

    data = {
        'a': pid_a, 'b': pid_b,
        'a_name': people[pid_a]['name'], 'b_name': people[pid_b]['name'],
        'names': names,
        'blood': blood, 'by_marriage': by_marriage, 'any': path,
        'dna': _dna_answer(blood, n_routes, cm_value),
    }
    # With links: obsidian (SPEC §18) each name is linked by its file name, so
    # the line can be pasted into a note Obsidian follows. The id mode's
    # output is unchanged.
    links = link_writer(archive_root, fha_config)
    if links.obsidian:
        data['links'] = {pid: links.link(pid, name) for pid, name in names.items()}
    return Result(ok=True, exit_code=EXIT_CLEAN, data=data)


def _everyone_result(archive_root: Path, fha_config: dict, conn, graph, pid: str,
                     vital_cache: dict) -> Result:
    """The list behind `fha relate P --all`: everyone related to `pid`, blood
    first, then by marriage. A record merged into another is left out, since
    the survivor already stands for him."""
    try:
        merged = {row[0] for row in conn.execute(
            'SELECT id FROM persons WHERE merged_into IS NOT NULL')}
    except sqlite3.Error:
        merged = set()
    rows = kinship_relatives(graph, pid, skip=merged,
                             life=lambda p: person_life_dates(conn, p, vital_cache))
    keys = ('pid', 'name', 'kind', 'relationship', 'half', 'half_relationship', 'certain',
            'gens_a', 'gens_b', 'double', 'routes')
    data = {
        'person': pid, 'person_name': graph.people[pid]['name'],
        'relatives': [{k: r[k] for k in keys} for r in rows],
        'counts': {'blood': sum(1 for r in rows if r['kind'] == 'blood'),
                   'by_marriage': sum(1 for r in rows if r['kind'] != 'blood')},
        # What each line says, hedge included ("sister (may be half-sister)").
        'words': {r['pid']: _row_words(r) for r in rows},
    }
    links = link_writer(archive_root, fha_config)
    if links.obsidian:
        names = {pid: data['person_name']}
        names.update({r['pid']: r['name'] for r in rows})
        data['links'] = {p: links.link(p, n) for p, n in names.items()}
    return Result(ok=True, exit_code=EXIT_CLEAN, data=data)


def _row_words(row: dict) -> str:
    """One row's relationship in the words the two-person answer uses: the
    double word, "may be" the half word when a parent is missing, every
    further line "through another line", and a step tie the dates cannot
    settle marked so."""
    words = row['relationship']
    if row['half'] is None and row.get('half_relationship'):
        words += f" (may be {row['half_relationship']})"
    if not row['certain']:
        words += ' (dates not all recorded)'
    routes = row.get('routes') or []
    extra = routes[2 if row.get('double') else 1:]
    for r in extra[:_MAX_EXTRA_ROUTES]:
        words += f'; and also {_route_word(r)}, through another line'
    more = len(extra) - _MAX_EXTRA_ROUTES
    if more > 0:
        words += f"; and {more} more {'route' if more == 1 else 'routes'}"
    return words


def _cmd_relate_all(result: Result) -> int:
    """Render the reunion list: blood relatives nearest first, then those
    related by marriage."""
    data = result.data
    links = data.get('links')
    relatives = data['relatives']
    if not relatives:
        print(f"Nobody in the recorded tree is related to {data['person_name']} yet. "
              'Record a parent, child or spouse (a relationship claim, or `fha person relate` '
              'for a guess), then run `fha index`.')
        return result.exit_code
    who = _id_name(data['person'], data['person_name'], links)
    print(f'Everyone related to {who} in the recorded tree: {len(relatives)} people')
    for title, kind, tail in (('Blood relatives', 'blood', ', nearest first'),
                              ('Related by marriage', 'by-marriage', '')):
        group = [r for r in relatives if r['kind'] == kind]
        if not group:
            continue
        print(f'\n{title} ({len(group)}){tail}')
        label = [_id_name(r['pid'], r['name'], links) for r in group]
        width = max(len(x) for x in label) + 2
        for text, r in zip(label, group):
            print(f'  {text:<{width}}{data["words"][r["pid"]]}')
    return result.exit_code
# ── shared DNA (The Shared cM Project figures, _lib's shared_cm_table) ────────
# SPEC §21: DNA evidence is restricted. This reads no DNA source and stores
# nothing: the amount he types lives only in this command's output.

def _usage_error(archive_root: Path, text: str) -> Result:
    r = Result(ok=False, exit_code=EXIT_ERRORS, data={'error': 'usage'})
    r.add('error', text, path=archive_root)
    return r


def _fig(n: float) -> str:
    """450.0 reads "450", 1397 reads "1,397", 72.5 stays "72.5"."""
    return f'{int(n):,}' if float(n) == int(n) else f'{n:,}'


def _cm_row(row: dict) -> dict:
    return {'key': row['key'], 'label': row['label'], 'average_cm': row['average'],
            'low_cm': row['low'], 'high_cm': row['high']}


def _usual(row: dict) -> str:
    return f'nearly all share between {_fig(row["low_cm"])} and {_fig(row["high_cm"])} cM'


_CM_DOCUMENTS = 'Documents decide which.'
_CM_TOO_SMALL = 'Matches under 20 cM are often false or too distant to place.'
_CM_TOO_BIG = ('That is more than a parent and child share. It usually means the same '
               'person tested twice, or identical twins.')
_CM_MISSING = ("The shared-DNA table is missing from this archive's tools. "
               'Run `fha update-tools` to restore it.')
_CM_NO_ROW = 'The Shared cM Project chart gives no figure for this relationship.'
_CM_NO_HALF_ROW = ('The chart gives no figure for the {form} form of this tie, so that '
                   'reading cannot be checked.')


_CM_ROUTES = ('They are related by more than one route, which can mean more DNA than one '
              'tie shares; the chart gives no figure for that.')


def _routes_high(blood: dict, table: dict) -> float:
    """The most the recorded routes could share together: each route's
    highest usual figure (its half reading too when it may be half), added
    up, and never more than a parent and child share."""
    total = 0
    for route in blood.get('routes') or [blood]:
        halves = [False, True] if route['half'] is None else [bool(route['half'])]
        highs = [r['high'] for h in halves
                 if (r := expected_shared_cm(route['gens_a'], route['gens_b'], h, table))]
        total += max(highs, default=0)
    return min(total, _cm_ceiling(table))


def _cm_ceiling(table: dict) -> float:
    """The most any relationship in the chart shares (a parent and child)."""
    return max(r['high'] for r in table['rows'])


def _cm_lookup(cm: float) -> Result:
    """`fha relate --cm N` with no people: every relationship N cM fits, the
    typical amount nearest first. Several answers on purpose; DNA alone
    never settles which."""
    table = shared_cm_table()
    data = {'cm': cm, 'fits': [], 'note': None, 'credit': table.get('credit')}
    if not table:
        data['note'] = _CM_MISSING
    elif cm > _cm_ceiling(table):
        data['note'] = _CM_TOO_BIG
    else:
        data['fits'] = [_cm_row(r) for r in cm_fits(cm, table)]
        if cm < 20:
            data['note'] = _CM_TOO_SMALL
    return Result(ok=True, exit_code=EXIT_CLEAN, data=data)


def _dna_answer(blood: dict | None, n_routes: int, cm: float | None) -> dict | None:
    """The DNA part of a two-person answer: what the recorded blood tie
    usually shares and, with `cm`, whether that amount fits it. None with no
    blood tie (a tie by marriage shares no DNA by itself). Both rows when the
    tie may be half, so he never repeats one guess.

    `fits_recorded` is None, not False, when the chart has no figure for the
    tie (or for one reading of a tie that may be half) and the amount fits no
    reading it does have: a gap in the chart is not evidence against his tree."""
    if not blood or not (blood['gens_a'] or blood['gens_b']):
        return None       # no tie, or the same person twice
    table = shared_cm_table()
    dna: dict = {'expected': [], 'more_than_one_route': False, 'note': None,
                 'credit': table.get('credit')}
    if cm is not None:
        dna.update(cm=cm, fits_recorded=None, fits=[])
    if not table:
        dna['note'] = _CM_MISSING
        return dna
    halves = [False, True] if blood['half'] is None else [bool(blood['half'])]
    found = {h: expected_shared_cm(blood['gens_a'], blood['gens_b'], h, table) for h in halves}
    rows = [r for r in found.values() if r]
    unlisted = [h for h, r in found.items() if not r]
    dna['expected'] = [_cm_row(r) for r in rows]
    dna['more_than_one_route'] = n_routes > 1
    if not rows:
        dna['note'] = _CM_NO_ROW
    elif unlisted:
        dna['note'] = _CM_NO_HALF_ROW.format(form='half' if unlisted[0] else 'full')
    if cm is not None:
        if any(r['low'] <= cm <= r['high'] for r in rows):
            dna['fits_recorded'] = True
        elif unlisted:
            dna['fits_recorded'] = None
        elif n_routes > 1 and max(r['high'] for r in rows) < cm <= _routes_high(blood, table):
            # The chart has no figure for a tie joined twice (double
            # cousins): an amount the routes together can reach is not a
            # misfit. A distant second route adds little, so far too much
            # DNA for the nearest route still is one.
            dna['fits_recorded'] = None
            dna['note'] = _CM_ROUTES
        else:
            dna['fits_recorded'] = False
        here = {r['key'] for r in rows}
        dna['fits'] = [_cm_row(f) for f in cm_fits(cm, table) if f['key'] not in here]
    return dna


def _also_fits(fits: list[dict], cm: float, shown: int = 5) -> str:
    """The first few other relationships an amount fits, saying how many more
    there are, so a cut list never reads as the whole list."""
    text = ', '.join(f['label'] for f in fits[:shown])
    if len(fits) > shown:
        text += (f', and {len(fits) - shown} more (`fha relate --cm {_fig(cm)}` '
                 f'lists them all)')
    return text


def _misfit_causes(rows: list[dict], cm: float) -> str:
    """What could explain an amount outside the recorded tie, matched to which
    way it misses: a half relationship only means LESS DNA, so it is offered
    only for a full collateral tie with too little, never for half-siblings,
    for a grandparent, or for too much."""
    if cm > max(r['high_cm'] for r in rows):
        return ('A closer tie, another route between them the tree does not show, or a '
                'wrong parent in the tree could explain it.')
    table = {r['key']: r for r in shared_cm_table().get('rows') or []}
    full = [table[r['key']] for r in rows if r['key'] in table and not table[r['key']]['half']]
    if (cm < min(r['low_cm'] for r in rows) and len(full) == len(rows)
            and any(t['near'] >= 1 and expected_shared_cm(t['near'], t['far'], True)
                    for t in full)):
        return ('A half relationship, a wrong or missing parent in the tree, or a '
                'different tie could explain it.')
    return 'A wrong or missing parent in the tree, or a different tie could explain it.'


def _dna_lines(dna: dict) -> list[str]:
    """The DNA line and the lines under it, without the label column."""
    rows, cm = dna['expected'], dna.get('cm')
    lines: list[str] = []
    routes_said = False
    if not rows and dna.get('note'):
        lines.append(dna['note'])
    if cm is None:
        if len(rows) == 1:
            r = rows[0]
            lines.append(f'{r["label"]} share about {_fig(r["average_cm"])} cM on average; '
                         f'{_usual(r)}.')
        elif rows:
            first, second = rows
            lines.append(
                f'{first["label"]} share about {_fig(first["average_cm"])} cM on average '
                f'(nearly all {_fig(first["low_cm"])} to {_fig(first["high_cm"])} cM); '
                f'{second["label"]} about {_fig(second["average_cm"])} cM '
                f'(nearly all {_fig(second["low_cm"])} to {_fig(second["high_cm"])} cM).')
        if rows and dna.get('note'):
            lines.append(dna['note'])
    elif dna['note'] != _CM_MISSING:
        also = _also_fits(dna['fits'], cm)
        if dna['fits_recorded']:
            hit = [r for r in rows if r['low_cm'] <= cm <= r['high_cm']]
            lines.append(f'{_fig(cm)} cM fits {" and ".join(r["label"] for r in hit)} '
                         f'(nearly all share between {_fig(min(r["low_cm"] for r in hit))} '
                         f'and {_fig(max(r["high_cm"] for r in hit))} cM).')
            lines.append(f'It also fits: {also}. {_CM_DOCUMENTS}' if also else _CM_DOCUMENTS)
        elif rows:
            lines.append(f'{_fig(cm)} cM is outside what {" or ".join(r["label"] for r in rows)} '
                         f'usually share ('
                         + '; '.join(_usual(r) for r in rows) + ').')
            if dna.get('note'):
                lines.append(dna['note'])
            if dna['note'] == _CM_ROUTES:
                if also:
                    lines.append(f'It fits: {also}.')
                routes_said = True
            else:
                lines.append((f'It fits: {also}. ' if also else '') + _misfit_causes(rows, cm))
        else:
            documents = _CM_DOCUMENTS if len(dna['fits']) > 1 else 'Documents decide.'
            lines.append(f'{_fig(cm)} cM fits: {also}. {documents}' if also
                         else f'{_fig(cm)} cM fits no relationship in the chart.')
        table = shared_cm_table()
        if cm < 20:
            lines.append(_CM_TOO_SMALL)
        elif table and cm > _cm_ceiling(table):
            lines.append(_CM_TOO_BIG)
    if dna['more_than_one_route'] and not routes_said:
        lines.append('Because they are related by more than one route, they may share more '
                     'than this.')
    if dna.get('credit'):
        lines.append(f'Figures: {dna["credit"]}.')
    return lines


def _print_cm_lookup(data: dict) -> None:
    cm = data['cm']
    fits = data['fits']
    if not fits:
        print(f'{_fig(cm)} cM of shared DNA: {data["note"] or "fits no relationship in the chart."}')
    else:
        if len(fits) == 1:
            print(f'{_fig(cm)} cM of shared DNA fits one relationship in the chart:')
        else:
            print(f'{_fig(cm)} cM of shared DNA fits several relationships. '
                  f'Closest typical amount first:')
        for f in fits:
            print(f'  {f["label"]}: typical {_fig(f["average_cm"])} cM, '
                  f'nearly all {_fig(f["low_cm"])} to {_fig(f["high_cm"])} cM')
        print('DNA alone cannot settle a relationship; documents decide. If the two '
              'families married into each other more than once, people can share more DNA '
              'than these figures suggest.')
        if data.get('note'):
            print(data['note'])
    if data.get('credit'):
        print(f'Figures: {data["credit"]}.')


# Every answer line starts its words in this column ("By marriage:" and two
# spaces), so Blood, By marriage and Path read down one edge.
_LABEL = 14

# The Blood line and at most two "and also" lines before the rest are only
# counted: enough to show a cousin marriage, few enough to read.
_MAX_EXTRA_ROUTES = 2


def _route_word(route: dict) -> str:
    """One further blood line's word, "second cousin or half second cousin"
    when that line may be half. The pair answer and the `--all` list both
    say a further line this way."""
    word = route['relationship']
    if route.get('half') is None and route.get('half_relationship'):
        word = f'{word} or {route["half_relationship"]}'
    return word


def _id_name(pid: str, name: str, links: dict[str, str] | None = None) -> str:
    if links and pid in links:
        return links[pid]
    return f'[[{fmt_id_display(pid)}|{name}]]'


def _cmd_relate(result: Result, use_json: bool = False) -> int:
    """Render a relate Result and return the exit code. The only layer that
    prints; the Blood, By marriage and Path lines of TOOLING §7, their
    labels padded to one column so the answers line up."""
    if use_json:
        print(json.dumps(result.as_dict(), indent=2))
        return result.exit_code

    if not result.ok:
        for m in result.messages:
            print(f'ERROR: {m.text}', file=sys.stderr)
        return result.exit_code

    data = result.data
    if 'relatives' in data:
        return _cmd_relate_all(result)
    if 'a' not in data:       # `--cm` alone: the lookup, no people
        _print_cm_lookup(data)
        return result.exit_code
    links = data.get('links')
    a_label = _id_name(data['a'], data['a_name'], links)
    b_label = _id_name(data['b'], data['b_name'], links)
    print(f'{a_label}  <->  {b_label}')

    names = data.get('names', {})
    blood = data.get('blood')
    if blood:
        routes = blood.get('routes') or [blood]

        def ancestors(group: list[str]) -> str:
            return ', '.join(_id_name(c, names.get(c, fmt_id_display(c)), links) for c in group)

        # Double first cousins descend from both couples, so both are named;
        # any further, more distant line gets its own "and also" line below.
        shown = 2 if blood.get('double') else 1
        groups = ([r['common_ancestors'] for r in routes[:shown]] if blood.get('double')
                  else [blood['common_ancestors']])
        groups = [g for g in groups if g]
        rel = blood['relationship']
        if groups:
            label = 'common ancestor' if sum(map(len, groups)) == 1 else 'common ancestors'
            anc = '; and '.join(ancestors(g) for g in groups)
            print(f'{"Blood:":<{_LABEL}}{data["a_name"]} is {data["b_name"]}\'s {rel}  ({label}: {anc})')
        else:
            print(f'{"Blood:":<{_LABEL}}{data["a_name"]} is {data["b_name"]}\'s {rel}')
        # With a parent unrecorded he hears, in the same breath, that the
        # tie may be half, instead of a plain "sister" he would repeat.
        if blood.get('note'):
            print(f'{"":<{_LABEL}}{blood["note"]}')
        # Cousins whose grandparents were cousins hear every line, nearest
        # first, so a DNA match that looks too close has its explanation. A
        # line through one ancestor whose other parent is unrecorded says it
        # may be half, as the first line's note does.
        extra = routes[shown:]
        for r in extra[:_MAX_EXTRA_ROUTES]:
            line = (f'{"":<{_LABEL}}and also {data["b_name"]}\'s {_route_word(r)}, '
                    'through another line')
            if r['common_ancestors']:
                label = 'common ancestor' if len(r['common_ancestors']) == 1 else 'common ancestors'
                line += f'  ({label}: {ancestors(r["common_ancestors"])})'
            print(line)
        more = len(extra) - _MAX_EXTRA_ROUTES
        if more > 0:
            noun = 'route' if more == 1 else 'routes'
            print(f'{"":<{_LABEL}}and {more} more {noun}; run with --json to see them all')
    else:
        print(f'{"Blood:":<{_LABEL}}no blood relationship found in the recorded tree')

    # A wife's brother reads "brother-in-law"; a stepmother the dates cannot
    # settle carries a second line saying so, so he never repeats a guess.
    bm = data.get('by_marriage')
    if bm:
        line = f'{"By marriage:":<{_LABEL}}{data["a_name"]} is {data["b_name"]}\'s {bm["relationship"]}'
        if bm.get('explain'):
            line += f'  ({bm["explain"]})'
        print(line)
        if bm.get('hedge'):
            print(f'{"":<{_LABEL}}{bm["hedge"]}')

    # How much DNA such relatives usually share and, with --cm, whether the
    # amount a match shows fits the recorded tie. Several answers, never odds.
    dna = data.get('dna')
    if dna:
        for i, text in enumerate(_dna_lines(dna)):
            print(f'{"DNA:" if i == 0 else "":<{_LABEL}}{text}')

    path = data.get('any')
    if path and path.get('path'):
        print(f'{"Path:":<{_LABEL}}{path["rendered"]}')
    elif path and not path.get('path'):
        print(f'{"Path:":<{_LABEL}}the same person')
    else:
        print(f'{"Path:":<{_LABEL}}no recorded connection between them')
    return result.exit_code


# ── CLI ───────────────────────────────────────────────────────────────────────

# User-facing --help text (the module docstring stays developer-facing).
_CLI_DESCRIPTION = """\
Show how two people are related.

  fha relate P-A P-B          Blood degree, the tie by marriage (brother-in-law,
                              stepmother) and the shortest social path
  fha relate "Ada Hart" P-B   Either person by full name instead of ID (a name
                              two people share is listed so you can pick one)
  fha relate P-A P-B --json   Same answer, as machine-readable JSON
  fha relate P-A --all        Everyone related to one person: blood relatives
                              nearest first, then those related by marriage
  fha relate P-A P-B --cm 450 Does 450 cM of shared DNA fit their recorded tie?
  fha relate --cm 450         Which relationships 450 cM fits (no people needed)

A read-only view over accepted relationship claims: nothing is written."""


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        'relate',
        help='How are two people related? Blood degree, tie by marriage, shortest path.',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('person_a', metavar='P-A', nargs='?', help='First person: ID or full name')
    p.add_argument('person_b', metavar='P-B', nargs='?', help='Second person: ID or full name')
    p.add_argument('--all', action='store_true', dest='everyone',
                   help='List everyone related to one person, by blood or by marriage')
    p.add_argument('--cm', metavar='CM',
                   help='Shared DNA in centimorgans from a match; shows the relationships it fits')
    p.add_argument('--root', metavar='PATH', help='Archive root (overrides auto-detection)')
    p.add_argument('--json', action='store_true', dest='use_json',
                   help='Machine-readable JSON output')
    p.set_defaults(func=_run_relate)


def _run_relate(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE
    result = run_relate(archive_root, fha_config, args.person_a, args.person_b,
                        cm=getattr(args, 'cm', None),
                        everyone=getattr(args, 'everyone', False))
    return _cmd_relate(result, use_json=getattr(args, 'use_json', False))


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha relate',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('person_a', metavar='P-A', nargs='?')
    parser.add_argument('person_b', metavar='P-B', nargs='?')
    parser.add_argument('--all', action='store_true', dest='everyone')
    parser.add_argument('--cm', metavar='CM')
    parser.add_argument('--root', metavar='PATH')
    parser.add_argument('--json', action='store_true', dest='use_json')
    args = parser.parse_args(argv)
    return _run_relate(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
