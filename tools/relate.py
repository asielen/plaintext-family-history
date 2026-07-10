#!/usr/bin/env python3
"""
relate.py - fha relate: how are two people related?

  fha relate <P-A> <P-B> [--json] [--root PATH]

Answers two questions, both derived from accepted relationship claims and never
stored (a layer-3 generated view, SPEC §3 / Part IV):

  Blood:  the genealogical relationship (second cousin once removed,
          great-grandmother, brother, ...) found via the lowest common ancestor
          over GENETIC parent edges only - adoptive / step / foster / guardian
          parents are not counted into the blood answer (SPEC §12.2).
  Path:   the shortest readable chain between the two over ALL relationship edges
          (parent / child / spouse / sibling / friend / associate / neighbor),
          e.g. "your brother's friend's sister's father".

The two are reported separately because they answer different questions: the
blood degree, and the shortest social connection (which may run through people
who are not blood relatives at all).

This is a pure read query over `.cache/index.sqlite`; an absent index is a hard
error (run `fha index`), never a silent wrong answer. SPEC Part IV, TOOLING §7.

HOW IT WORKS
------------
One pass loads two graphs from the `relationships` table (joined to `claims` for
each edge's nature):
  * a GENETIC parent map (child -> {genetic parents}) for the blood answer, and
  * an undirected adjacency (every parent/child/spouse/social edge, plus derived
    sibling edges from shared parents) for the shortest-path answer.
Blood = lowest common ancestor over the genetic map, named by the classic
generation/removal formula. Path = BFS over the adjacency, rendered hop by hop.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    EXIT_CLEAN,
    EXIT_FAILURE,
    FhaConfigError,
    Result,
    fmt_id_display,
    id_type_of,
    is_genetic_parent_subtype,
    is_valid_id,
    load_fha_yaml,
    normalize_id,
    open_index_db,
    resolve_root_arg,
)

# ── CODE MAP ──────────────────────────────────────────────────────────────────
#
#  Graph loading
#    _load_graph              - persons + edges -> (people, genetic_parents, adjacency)
#
#  Blood relationship
#    _ancestors               - BFS up genetic parents -> {ancestor_pid: generations}
#    _ordinal / _times        - "first"/"second"; "once"/"twice" removal words
#    _ancestor_word / _descendant_word - parent/grandparent/... with sex
#    _blood_name              - (gens_a, gens_b, sexes) -> relationship phrase
#    _blood_answer            - lowest common ancestor + named relationship
#
#  Social path
#    _hop_label               - one edge (rel + other person's sex) -> a word
#    _path_answer             - BFS shortest path, rendered as a hop chain
#
#  Engine / interface
#    run_relate               - compute both answers, return a Result
#    _cmd_relate              - render the Result (text or --json) -> exit code
#    register / _run_relate / _standalone_main - CLI wiring
#
# ─────────────────────────────────────────────────────────────────────────────

# The relationships table carries both person<->person directions for stored
# edges; siblings are NOT stored (they are derived from shared parents below).
_SOCIAL_RELS = frozenset({'friend', 'associate', 'neighbor', 'spouse'})

_REQUIRED_TABLES = ('persons', 'relationships', 'claims')


def _load_graph(conn) -> tuple[dict, dict, dict]:
    """Return (people, genetic_parents, adjacency).

    people: pid -> {'name': str, 'sex': str}
    genetic_parents: child_pid -> set(parent_pid)   (genetic edges only; blood)
    adjacency: pid -> list of (other_pid, rel)       (all edges + derived siblings)

    The nature of each parent edge lives on its backing claim, so the edge query
    joins `relationships` to `claims` by `claim_id`; an unset/unknown/legacy
    nature counts as genetic (SPEC §12.2), so a legacy archive answers as before.
    """
    people: dict[str, dict] = {}
    for row in conn.execute('SELECT id, name, sex FROM persons'):
        people[row['id']] = {'name': row['name'] or row['id'], 'sex': (row['sex'] or '').strip()}

    genetic_parents: dict[str, set[str]] = {}
    parents_any: dict[str, set[str]] = {}
    adjacency: dict[str, list[tuple[str, str]]] = {}

    rows = conn.execute(
        '''SELECT r.person_id AS pid, r.rel AS rel, r.other_id AS other, c.subtype AS subtype
           FROM relationships r
           LEFT JOIN claims c ON r.claim_id = c.id'''
    ).fetchall()

    seen_adj: set[tuple[str, str, str]] = set()
    for row in rows:
        pid, rel, other = row['pid'], row['rel'], row['other']
        if not pid or not other:
            continue
        # Undirected adjacency for the social path (dedupe repeated edges).
        key = (pid, rel, other)
        if key not in seen_adj:
            seen_adj.add(key)
            adjacency.setdefault(pid, []).append((other, rel))
        if rel == 'parent':
            parents_any.setdefault(pid, set()).add(other)
            if is_genetic_parent_subtype(row['subtype']):
                genetic_parents.setdefault(pid, set()).add(other)

    # Derive sibling edges: two people who share at least one parent. Uses ALL
    # parent edges (a half- or adoptive sibling is still a sibling on the social
    # path); the blood answer never needs siblings (it uses ancestors directly).
    children_of: dict[str, set[str]] = {}
    for child, parents in parents_any.items():
        for p in parents:
            children_of.setdefault(p, set()).add(child)
    for kids in children_of.values():
        for a in kids:
            for b in kids:
                if a != b:
                    adjacency.setdefault(a, []).append((b, 'sibling'))

    return people, genetic_parents, adjacency


# ── Blood relationship ────────────────────────────────────────────────────────

def _ancestors(pid: str, genetic_parents: dict[str, set[str]]) -> dict[str, int]:
    """{ancestor_pid: generations above `pid`}, including pid itself at 0.

    BFS upward over genetic parent edges, recording the SHORTEST generation
    distance to each ancestor (endogamy can reach one ancestor by two paths; the
    nearer wins). A visited set guards against any cycle in malformed data."""
    dist: dict[str, int] = {pid: 0}
    queue: deque[str] = deque([pid])
    while queue:
        cur = queue.popleft()
        for parent in genetic_parents.get(cur, ()):  # noqa: SIM118 - set iteration
            if parent not in dist:
                dist[parent] = dist[cur] + 1
                queue.append(parent)
    return dist


_ORDINALS = ['zeroth', 'first', 'second', 'third', 'fourth', 'fifth', 'sixth',
             'seventh', 'eighth', 'ninth', 'tenth']
_TIMES = ['', 'once', 'twice', 'three times', 'four times', 'five times']


def _ordinal(n: int) -> str:
    if 0 <= n < len(_ORDINALS):
        return _ORDINALS[n]
    return f'{n}th'


def _times_removed(n: int) -> str:
    word = _TIMES[n] if n < len(_TIMES) else f'{n} times'
    return f'{word} removed'


def _greats(n: int) -> str:
    """`n` great-s as a prefix: 0 -> '', 1 -> 'great-', 2 -> 'great-great-', ..."""
    return 'great-' * n


def _lineal_word(gens: int, sex: str, *, up: bool) -> str:
    """A direct-line relationship `gens` steps away. up=True -> ancestor
    (parent/grandparent/...); up=False -> descendant (child/grandchild/...)."""
    s = (sex or '').upper()
    if up:
        base = {'M': 'father', 'F': 'mother'}.get(s, 'parent')
        if gens == 1:
            return base
        grand = {'M': 'grandfather', 'F': 'grandmother'}.get(s, 'grandparent')
        return _greats(gens - 2) + grand
    else:
        base = {'M': 'son', 'F': 'daughter'}.get(s, 'child')
        if gens == 1:
            return base
        grand = {'M': 'grandson', 'F': 'granddaughter'}.get(s, 'grandchild')
        return _greats(gens - 2) + grand


def _collateral_word(near: int, far: int, sex: str, *, up: bool) -> str:
    """Aunt/uncle (up) or niece/nephew (down) with grand-/great- prefixes.

    `near` is the LCA distance of the person at the shallower side (always 1
    here), `far` the deeper side. The number of greats follows the genealogical
    convention: grand-aunt at far==3, great-grand-aunt at far==4, ..."""
    s = (sex or '').upper()
    if up:
        base = {'M': 'uncle', 'F': 'aunt'}.get(s, 'aunt/uncle')
    else:
        base = {'M': 'nephew', 'F': 'niece'}.get(s, 'niece/nephew')
    extra = far - 2          # far==2 -> plain aunt/uncle; 3 -> grand-; 4 -> great-grand-
    if extra <= 0:
        return base
    if extra == 1:
        return 'grand' + base
    return _greats(extra - 1) + 'grand' + base


def _blood_name(gens_a: int, gens_b: int, sex_a: str) -> str:
    """Name A's blood relationship to B, given each one's generation distance to
    their lowest common ancestor. Phrased from A's side (so sex is A's): A is
    B's <returned phrase>."""
    if gens_a == 0 and gens_b == 0:
        return 'the same person'
    if gens_a == 0:                      # A is the common ancestor: A is B's ancestor
        return _lineal_word(gens_b, sex_a, up=True)
    if gens_b == 0:                      # B is the common ancestor: A is B's descendant
        return _lineal_word(gens_a, sex_a, up=False)
    if gens_a == 1 and gens_b == 1:
        return {'M': 'brother', 'F': 'sister'}.get((sex_a or '').upper(), 'sibling')
    if gens_a == 1:                      # A is a child of the LCA, B deeper -> A is aunt/uncle
        return _collateral_word(gens_a, gens_b, sex_a, up=True)
    if gens_b == 1:                      # B is a child of the LCA, A deeper -> A is niece/nephew
        return _collateral_word(gens_b, gens_a, sex_a, up=False)
    degree = min(gens_a, gens_b) - 1
    removal = abs(gens_a - gens_b)
    name = f'{_ordinal(degree)} cousin'
    if removal:
        name += ' ' + _times_removed(removal)
    return name


def _blood_answer(pid_a: str, pid_b: str, genetic_parents: dict, people: dict) -> dict | None:
    """The blood relationship via lowest common ancestor, or None if unrelated.

    Returns {relationship, common_ancestors, gens_a, gens_b}. The lowest common
    ancestor minimises max(distance) then total distance; ties (a common
    ancestral couple) are all reported."""
    anc_a = _ancestors(pid_a, genetic_parents)
    anc_b = _ancestors(pid_b, genetic_parents)
    # A person is their own ancestor at distance 0, so a direct ancestor/descendant
    # pair shares the ancestor at (0, k) - handled by the 0-distance cases in
    # _blood_name. The intersection therefore covers lineal ties as well as cousins.
    common = set(anc_a) & set(anc_b)
    if not common:
        return None
    best_key = min((max(anc_a[c], anc_b[c]), anc_a[c] + anc_b[c]) for c in common)
    lcas = sorted(
        c for c in common
        if (max(anc_a[c], anc_b[c]), anc_a[c] + anc_b[c]) == best_key
    )
    c0 = lcas[0]
    gens_a, gens_b = anc_a[c0], anc_b[c0]
    return {
        'relationship': _blood_name(gens_a, gens_b, people.get(pid_a, {}).get('sex', '')),
        'common_ancestors': lcas,
        'gens_a': gens_a,
        'gens_b': gens_b,
    }


# ── Social path ─────────────────────────────────────────────────────────────

def _hop_label(rel: str, other_sex: str) -> str:
    """Render one edge as the word for the person it reaches (sex-aware)."""
    s = (other_sex or '').upper()
    table = {
        'parent': {'M': 'father', 'F': 'mother', '': 'parent'},
        'child': {'M': 'son', 'F': 'daughter', '': 'child'},
        'spouse': {'M': 'husband', 'F': 'wife', '': 'spouse'},
        'sibling': {'M': 'brother', 'F': 'sister', '': 'sibling'},
    }
    if rel in table:
        return table[rel].get(s, table[rel][''])
    return rel          # friend / associate / neighbor render as themselves


def _path_answer(pid_a: str, pid_b: str, adjacency: dict, people: dict) -> dict | None:
    """Shortest path A -> B over all edges, rendered as a chain of hops.

    BFS (shortest by hop count); the first path found is returned. Returns
    {path: [{from, to, rel, label}], rendered} or None if disconnected."""
    if pid_a == pid_b:
        return {'path': [], 'rendered': 'the same person'}
    prev: dict[str, tuple[str, str]] = {}   # node -> (came_from, rel)
    visited = {pid_a}
    queue: deque[str] = deque([pid_a])
    while queue:
        cur = queue.popleft()
        if cur == pid_b:
            break
        for other, rel in adjacency.get(cur, ()):  # noqa: SIM118
            if other not in visited:
                visited.add(other)
                prev[other] = (cur, rel)
                queue.append(other)
    if pid_b not in prev:
        return None
    # Reconstruct from B back to A.
    chain: list[tuple[str, str, str]] = []
    node = pid_b
    while node != pid_a:
        came_from, rel = prev[node]
        chain.append((came_from, node, rel))
        node = came_from
    chain.reverse()
    path = [
        {'from': a, 'to': b, 'rel': rel, 'label': _hop_label(rel, people.get(b, {}).get('sex', ''))}
        for (a, b, rel) in chain
    ]
    rendered = 'your ' + "'s ".join(h['label'] for h in path)
    return {'path': path, 'rendered': rendered}


# ── Engine / interface ────────────────────────────────────────────────────────

def run_relate(archive_root: Path, fha_config: dict, person_a: str, person_b: str) -> Result:
    """Compute the blood relationship and the shortest social path between two
    people and return a structured Result. A pure read over the index; never
    writes. The text formatter and `--json` both render this one Result."""
    a_raw, b_raw = person_a, person_b
    pid_a, pid_b = normalize_id(person_a), normalize_id(person_b)
    for raw, pid in ((a_raw, pid_a), (b_raw, pid_b)):
        if not is_valid_id(pid) or id_type_of(pid) != 'P':
            msg = (f'{raw!r} is not a person ID. Give two P-ids, '
                   f'e.g. `fha relate P-de957bcda1 P-83e768cacb`.')
            r = Result(ok=False, exit_code=EXIT_FAILURE, data={'error': 'bad-id'})
            r.add('error', msg, path=archive_root)
            return r

    conn = open_index_db(archive_root, _REQUIRED_TABLES)
    if conn is None:
        # open_index_db already printed the cause + the `fha index` next step.
        return Result(ok=False, exit_code=EXIT_FAILURE, data={'error': 'no-index'})

    try:
        people, genetic_parents, adjacency = _load_graph(conn)
    except Exception:
        conn.close()
        r = Result(ok=False, exit_code=EXIT_FAILURE, data={'error': 'bad-schema'})
        r.add('error',
              '.cache/index.sqlite is unreadable or has an incompatible schema. '
              'Run `fha index` to rebuild it.', path=archive_root)
        return r
    finally:
        try:
            conn.close()
        except Exception:
            pass

    for raw, pid in ((a_raw, pid_a), (b_raw, pid_b)):
        if pid not in people:
            r = Result(ok=False, exit_code=EXIT_FAILURE, data={'error': 'no-person'})
            r.add('error',
                  f'No person {fmt_id_display(pid)} in the index. '
                  f'Run `fha find {fmt_id_display(pid)}` to check the ID, or `fha index` to rebuild.',
                  path=archive_root)
            return r

    blood = _blood_answer(pid_a, pid_b, genetic_parents, people)
    path = _path_answer(pid_a, pid_b, adjacency, people)

    # Display names for every person the renderer will name (the pair, plus any
    # common ancestors), so the text output reads with names, not bare IDs.
    names = {pid_a: people[pid_a]['name'], pid_b: people[pid_b]['name']}
    if blood:
        for c in blood['common_ancestors']:
            names[c] = people.get(c, {}).get('name') or fmt_id_display(c)

    return Result(
        ok=True,
        exit_code=EXIT_CLEAN,
        data={
            'a': pid_a, 'b': pid_b,
            'a_name': people[pid_a]['name'], 'b_name': people[pid_b]['name'],
            'names': names,
            'blood': blood, 'any': path,
        },
    )


def _id_name(pid: str, name: str) -> str:
    return f'[[{fmt_id_display(pid)}|{name}]]'


def _cmd_relate(result: Result, use_json: bool = False) -> int:
    """Render a relate Result and return the exit code. The only layer that
    prints; mirrors the blood/path two-line shape from TOOLING §7."""
    if use_json:
        print(json.dumps(result.as_dict(), indent=2))
        return result.exit_code

    if not result.ok:
        for m in result.messages:
            print(f'ERROR: {m.text}', file=sys.stderr)
        return result.exit_code

    data = result.data
    a_label = _id_name(data['a'], data['a_name'])
    b_label = _id_name(data['b'], data['b_name'])
    print(f'{a_label}  <->  {b_label}')

    names = data.get('names', {})
    blood = data.get('blood')
    if blood:
        anc = ', '.join(
            _id_name(c, names.get(c, fmt_id_display(c))) for c in blood['common_ancestors']
        )
        rel = blood['relationship']
        if blood['common_ancestors']:
            label = 'common ancestor' if len(blood['common_ancestors']) == 1 else 'common ancestors'
            print(f'Blood:  {data["a_name"]} is {data["b_name"]}\'s {rel}  ({label}: {anc})')
        else:
            print(f'Blood:  {data["a_name"]} is {data["b_name"]}\'s {rel}')
    else:
        print('Blood:  no blood relationship found in the recorded tree')

    path = data.get('any')
    if path and path.get('path'):
        print(f'Path:   {path["rendered"]}')
    elif path and not path.get('path'):
        print('Path:   the same person')
    else:
        print('Path:   no recorded connection between them')
    return result.exit_code


# ── CLI ───────────────────────────────────────────────────────────────────────

# User-facing --help text (the module docstring stays developer-facing).
_CLI_DESCRIPTION = """\
Show how two people are related.

  fha relate P-A P-B          Blood degree plus the shortest social path
  fha relate P-A P-B --json   Same answer, as machine-readable JSON

A read-only view over accepted relationship claims: nothing is written."""


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        'relate',
        help='How are two people related? Blood degree + shortest social path.',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('person_a', metavar='P-A', help='First person ID')
    p.add_argument('person_b', metavar='P-B', help='Second person ID')
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
