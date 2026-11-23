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
  * `kinship_answer` gives the nearest blood route over the genetic map (one
    common ancestor or a couple), named by generation and removal
    (`kinship_word`), with its half state and, when half is open, a note;
    and, only when there is no blood route, the tie by marriage
    (`marriage_answer`), whose step-parent date rule reads each person's
    birth and death dates (`person_life_dates`).
  * `kinship_path` is a BFS over the adjacency, rendered hop by hop.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    configure_utf8_stdout,
    EXIT_CLEAN,
    EXIT_FAILURE,
    FhaConfigError,
    Result,
    blood_answer,
    fmt_id_display,
    id_type_of,
    is_valid_id,
    kin_graph,
    kinship_answer,
    kinship_path,
    link_writer,
    load_fha_yaml,
    normalize_id,
    open_index_db,
    person_life_dates,
    person_name_refusal,
    persons_named,
    resolve_root_arg,
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

def run_relate(archive_root: Path, fha_config: dict, person_a: str, person_b: str) -> Result:
    """Compute the blood relationship, the tie by marriage and the shortest path between two
    people and return a structured Result. A pure read over the index; never
    writes. The text formatter and `--json` both render this one Result."""
    a_raw, b_raw = person_a, person_b
    pid_a, pid_b = normalize_id(person_a), normalize_id(person_b)
    for raw, pid in ((a_raw, pid_a), (b_raw, pid_b)):
        if is_valid_id(pid) and id_type_of(pid) != 'P':
            msg = (f'{raw!r} is not a person ID. Give two P-ids or full names, '
                   f'e.g. `fha relate P-de957bcda1 P-83e768cacb`.')
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
            for raw, pid in ((a_raw, pid_a), (b_raw, pid_b)):
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
            pid_a, pid_b = resolved
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
        answers = kinship_answer(graph, pid_a, pid_b,
                                 life=lambda p: person_life_dates(conn, p, vital_cache))
        path = kinship_path(graph, pid_a, pid_b)
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
    for c in (blood['common_ancestors'] if blood else []) + (
            by_marriage['via'] if by_marriage else []):
        names[c] = people.get(c, {}).get('name') or fmt_id_display(c)

    data = {
        'a': pid_a, 'b': pid_b,
        'a_name': people[pid_a]['name'], 'b_name': people[pid_b]['name'],
        'names': names,
        'blood': blood, 'by_marriage': by_marriage, 'any': path,
    }
    # With links: obsidian (SPEC §18) each name is linked by its file name, so
    # the line can be pasted into a note Obsidian follows. The id mode's
    # output is unchanged.
    links = link_writer(archive_root, fha_config)
    if links.obsidian:
        data['links'] = {pid: links.link(pid, name) for pid, name in names.items()}
    return Result(ok=True, exit_code=EXIT_CLEAN, data=data)


# Every answer line starts its words in this column ("By marriage:" and two
# spaces), so Blood, By marriage and Path read down one edge.
_LABEL = 14


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
    links = data.get('links')
    a_label = _id_name(data['a'], data['a_name'], links)
    b_label = _id_name(data['b'], data['b_name'], links)
    print(f'{a_label}  <->  {b_label}')

    names = data.get('names', {})
    blood = data.get('blood')
    if blood:
        anc = ', '.join(
            _id_name(c, names.get(c, fmt_id_display(c)), links) for c in blood['common_ancestors']
        )
        rel = blood['relationship']
        if blood['common_ancestors']:
            label = 'common ancestor' if len(blood['common_ancestors']) == 1 else 'common ancestors'
            print(f'{"Blood:":<{_LABEL}}{data["a_name"]} is {data["b_name"]}\'s {rel}  ({label}: {anc})')
        else:
            print(f'{"Blood:":<{_LABEL}}{data["a_name"]} is {data["b_name"]}\'s {rel}')
        # With a parent unrecorded he hears, in the same breath, that the
        # tie may be half, instead of a plain "sister" he would repeat.
        if blood.get('note'):
            print(f'{"":<{_LABEL}}{blood["note"]}')
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

A read-only view over accepted relationship claims: nothing is written."""


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        'relate',
        help='How are two people related? Blood degree, tie by marriage, shortest path.',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('person_a', metavar='P-A', help='First person: ID or full name')
    p.add_argument('person_b', metavar='P-B', help='Second person: ID or full name')
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
    result = run_relate(archive_root, fha_config, args.person_a, args.person_b)
    return _cmd_relate(result, use_json=getattr(args, 'use_json', False))


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha relate',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('person_a', metavar='P-A')
    parser.add_argument('person_b', metavar='P-B')
    parser.add_argument('--root', metavar='PATH')
    parser.add_argument('--json', action='store_true', dest='use_json')
    args = parser.parse_args(argv)
    return _run_relate(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
