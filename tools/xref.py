#!/usr/bin/env python3
"""
xref.py - fha xref: cross-reference pass over the claim index.

  fha xref [--root PATH]

Read-only candidate-suggestion tool (TOOLING §14a). Does not write to the
archive - it only prints candidate pairs for a human (or a future skill
layer) to confirm. Confirmation, link-writing, and question-spawning are out
of scope for this tool.

ALGORITHM
---------
For every person, group their accepted/needs-review claims by claim `type`
(relationship claims are further split by `subtype` - the *nature* of the bond,
e.g. biological vs. adoptive - this person's `role`, and the other person(s)
named in the claim, since a person can be e.g. a child in one relationship claim
and a parent in another, and a biological and an adoptive parent of the same
child are co-valid edges rather than a contradiction). Within each group,
every pair of claims from *different* sources that isn't already linked via
`claim_links` is a candidate:

  - negation polarity differs (`negated`)          -> contradiction candidate,
                                                        regardless of dates
  - bounds don't overlap, vital type                -> contradiction candidate
  - bounds don't overlap, substantive type           -> not a candidate
    (residence, occupation, ... recur by design, §8.2; non-overlapping dates
    are expected, not a conflict)
  - bounds overlap                                  -> corroboration candidate
  - vital type AND bounds overlap AND both claims     -> also a contradiction
    carry a `place_id`/`place_text` that disagree        candidate (incompatible
                                                          value), even though the
                                                          dates don't conflict

Place comparison prefers structured `place_id` when both claims have one;
it falls back to normalized `place_text`, then to a place phrase parsed out
of free-prose `value`, since `value` itself is not reliably comparable
across claims. A claim with no `date_edtf` gets the unbounded
`('0001-01-01', '9999-12-31')` bounds from `edtf_bounds`, so an undated claim
always overlaps rather than being treated as conflicting.

MARRIAGE/DIVORCE BUCKETING (#63)
---------------------------------
`persons:` on a marriage/divorce claim is who the claim is ABOUT, not a
couple list (SPEC §8.3) - a certificate routinely names both sets of
parents alongside the couple. Bucketing by "every other named person"
(as this file once did) keys a six-person certificate by all five
bystanders, so it never matches a plain two-person claim of the *same*
marriage keyed by just the one spouse - two records of one marriage that
never get compared, and the tool's silence reads as "no contradictions"
rather than "couldn't tell". `_lib.spouse_parties` is the shared, already-
correct rule for "who does this claim say married whom" (also used by
`fha index`'s spouse edges and `fha gedcom`'s FAM grouping); every
marriage/divorce claim is read through it once, up front, before any
per-person bucketing:

  - `negated: true` (SPEC §8.6, a real "researched, did not happen") is
    always compared against every marriage/divorce claim this person has
    - checked on the claim's own `negated` field, never inferred from an
    empty party set - narrowed further to one counterpart's claims when
    `spouse_parties` did resolve a specific ex-partner.
  - A claim naming fewer than two people at all has no counterpart to be
    ambiguous ABOUT, negated or not - unchanged from before #63, it takes
    the same broad every-claim-of-this-type path a negation does.
  - A resolved couple (`spouse_parties` non-empty) buckets narrowly by that
    couple, same as any other counterpart-keyed group.
  - An unresolved, non-negated claim naming 2+ people (a roles-less
    certificate, or a `roles:` map that names fewer than two spouses) is
    AMBIGUOUS: comparing it against everything fabricates contradictions
    against unrelated marriages (a groom's own certificate "contradicting"
    his parents' wedding); comparing it against nothing is the original
    bug. It is excluded from every comparison bucket and listed instead in
    the Result's `unscoped` list, so the human can see it and add a
    `roles: spouse:` map to enable comparison.
  - A person named on the claim but not among its resolved parties (a
    witness, an informant, a parent on a certificate) never enters that
    claim into their own marriage/divorce bucket at all - the claim asserts
    nothing about their marital status.

CODE MAP
--------
  DB / root helpers - open_index_db, resolve_root_arg, both shared via _lib.py

  Classification
    _place_from_vital_value     - vital-claim place extraction (uses _lib.normalize_place_text)
    _classify_pair              - corroborates/contradicts for one claim pair
    run_xref                    - group claims by person+type, pair, classify
                                   (marriage/divorce grouping uses _lib.spouse_parties,
                                   see "MARRIAGE/DIVORCE BUCKETING" above)

  CLI
    _fmt_claim                  - display formatting (uses _lib.fmt_id_display)
    _cmd_xref, register, _standalone_main
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    EXIT_CLEAN,
    EXIT_FAILURE,
    Result,
    edtf_bounds,
    fmt_id_display,
    normalize_place_text,
    open_index_db,
    resolve_root_arg,
    spouse_parties,
)

_VITAL_TYPES = {'birth', 'death', 'marriage', 'baptism', 'burial'}

_REQUIRED_TABLES = ('persons', 'claims', 'sources', 'claim_persons', 'claim_links')


# ── Core query ────────────────────────────────────────────────────────────────


def _place_from_vital_value(text: str | None) -> str:
    """
    Extract a conservative place phrase from a vital claim value.

    Vital `value` is free prose, so comparing whole strings would turn harmless
    wording differences into contradictions. The stable conflict signal is a
    place-like phrase introduced by common vital wording ("born in ...",
    "birthplace: ...", etc.); if no such phrase is present, the value is not
    used for contradiction classification.
    """
    if not text:
        return ''
    # The place capture stops before a trailing date/preposition clause
    # ("born in Springfield in 1840" -> "Springfield", not "Springfield in
    # 1840") as well as at sentence punctuation, since the date belongs to
    # the structured `date_edtf` field, not the place comparison.
    patterns = (
        r'\b(?:born|died|married|buried|baptized|baptised)\s+(?:in|at)\s+'
        r'([^.;\n]+?)(?:\s+(?:in|on|circa|c\.)\s+\d|[.;\n]|$)',
        r'\b(?:birthplace|deathplace|marriage place|burial place|baptism place|place)\s*:\s*'
        r'([^.;\n]+?)(?:\s+(?:in|on|circa|c\.)\s+\d|[.;\n]|$)',
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return normalize_place_text(match.group(1))
    return ''


def _classify_pair(a: dict, b: dict) -> str | None:
    """
    Return 'corroborates', 'contradicts', or None (not a comparable pair) for a
    same-person, same-type pair.
    """
    a_min, a_max = edtf_bounds(a['date_edtf'])
    b_min, b_max = edtf_bounds(b['date_edtf'])
    bounds_overlap = a_min <= b_max and b_min <= a_max

    if not bounds_overlap:
        if a['type'] in _VITAL_TYPES:
            return 'contradicts'
        # Substantive types (residence, occupation, ...) are recurring by
        # design (§8.2) - non-overlapping dates are expected, not a conflict.
        return None

    if bool(a['negated']) != bool(b['negated']):
        # One claim asserts the fact happened, the other confirms it never
        # did, for the same place in time - that's a genuine conflict. (Vital
        # types always reach here: an undated negated claim gets unbounded
        # bounds, so it overlaps any dated positive claim of the same type.)
        # For repeatable substantive types (residence, occupation, ...) the
        # absence and the presence have to be about the *same* place - a
        # negated "did not reside in Topeka" doesn't conflict with a positive
        # "resided in Boston" the same year, since both can be true at once.
        if a['type'] not in _VITAL_TYPES:
            if a['place_id'] and b['place_id']:
                if a['place_id'] != b['place_id']:
                    return None
            else:
                place_a = normalize_place_text(a['place_text'])
                place_b = normalize_place_text(b['place_text'])
                if place_a and place_b and place_a != place_b:
                    return None
        return 'contradicts'

    if a['type'] in _VITAL_TYPES:
        if a['place_id'] and b['place_id']:
            if a['place_id'] != b['place_id']:
                return 'contradicts'
        else:
            place_a = normalize_place_text(a['place_text']) or _place_from_vital_value(a['value'])
            place_b = normalize_place_text(b['place_text']) or _place_from_vital_value(b['value'])
            if place_a and place_b and place_a != place_b:
                return 'contradicts'

    return 'corroborates'


def run_xref(archive_root: Path) -> Result:
    """
    Find corroboration/contradiction candidate claim pairs.

    Returns a `Result` whose `data` is {'status': 'ok'|'failed', 'groups':
    [{'person_id', 'person_name', 'pairs': [{'kind', 'claim_a', 'claim_b'}, …]},
    …], 'unscoped': [{'claim', 'persons'}, …]}.  Result exposes dict-style
    access (_lib.py), so callers and tests keep reading `result['status']` /
    `result['groups']` unchanged.

    Each claim dict embedded in a pair (or in `unscoped`) carries: id,
    source_id, source_title, type, date_edtf, place_text, value.

    `unscoped` is the secondary "here's what I couldn't do" list (matching
    `run_index`'s `unreadable_records`/warnings shape) - marriage/divorce
    claims naming 2+ people where `_lib.spouse_parties` could not tell the
    couple from everyone else named on the claim, so they were left out of
    every comparison bucket in `groups` rather than risking a fabricated
    contradiction (#63; see the module docstring's "MARRIAGE/DIVORCE
    BUCKETING" section). Each entry's `persons` is the display names of
    everyone the claim names, in claim order.
    """
    conn = open_index_db(archive_root, _REQUIRED_TABLES)
    if conn is None:
        return Result(ok=False, exit_code=EXIT_FAILURE,
                      data={'status': 'failed', 'groups': [], 'unscoped': []})

    try:
        data = _run_xref_queries(conn)
        return Result(exit_code=EXIT_CLEAN, data=data)
    except sqlite3.OperationalError:
        print(
            'ERROR: .cache/index.sqlite is unreadable or has an incompatible schema. '
            'Run `fha index` to rebuild.',
            file=sys.stderr,
        )
        return Result(ok=False, exit_code=EXIT_FAILURE,
                      data={'status': 'failed', 'groups': [], 'unscoped': []})
    finally:
        conn.close()


def _run_xref_queries(conn: sqlite3.Connection) -> dict:
    claims_by_id = {
        row['id']: dict(row)
        for row in conn.execute(
            '''
            SELECT id, source_id, type, subtype, date_edtf, place_id, place_text,
                   value, negated
            FROM claims
            WHERE status IN ('accepted', 'needs-review')
            '''
        )
    }
    source_titles = {
        row['id']: row['title'] for row in conn.execute('SELECT id, title FROM sources')
    }
    for claim in claims_by_id.values():
        claim['source_title'] = source_titles.get(claim['source_id'], claim['source_id'])

    claims_by_person: dict[str, list[str]] = {}
    claim_persons: dict[str, list[str]] = {}
    claim_role: dict[tuple[str, str], str] = {}
    # ORDER BY position: _lib.spouse_parties reads "the claim's people paired
    # with their role, in the claim's own order" - it only matters for its
    # first-occurrence-wins duplicate handling, but that is the contract the
    # rest of the codebase (fha index, fha gedcom) already relies on, so xref
    # reads claim_persons the same way rather than trusting sqlite's
    # unspecified default row order.
    for row in conn.execute(
        'SELECT claim_id, person_id, role FROM claim_persons ORDER BY position'
    ):
        if row['claim_id'] not in claims_by_id:
            continue
        claims_by_person.setdefault(row['person_id'], []).append(row['claim_id'])
        claim_persons.setdefault(row['claim_id'], []).append(row['person_id'])
        claim_role[(row['claim_id'], row['person_id'])] = row['role']

    linked_pairs: set[frozenset[str]] = set()
    for row in conn.execute('SELECT claim_id, target_id FROM claim_links'):
        linked_pairs.add(frozenset((row['claim_id'], row['target_id'])))

    person_names = {row['id']: row['name'] for row in conn.execute('SELECT id, name FROM persons')}

    _COUNTERPART_VITAL_TYPES = {'marriage', 'divorce'}

    # Who each marriage/divorce claim actually names as the couple, read
    # through the shared _lib.spouse_parties rule (see the module docstring's
    # "MARRIAGE/DIVORCE BUCKETING" section for the full reasoning behind
    # #63's fix). Computed once per claim, up front - who a claim names as a
    # couple does not depend on whose bucket is being built, and every
    # person named on the claim needs the same answer.
    #
    # `unscoped_claim_ids` collects the AMBIGUOUS claims: 2+ distinct people
    # named, not negated, and spouse_parties still could not tell the couple
    # from everyone else on the record (a roles-less certificate, or a
    # roles: map naming fewer than two spouses). That is deliberately a
    # narrower test than "spouse_parties resolved nothing" - a claim naming
    # fewer than two people has no counterpart to be ambiguous ABOUT (it
    # takes the same broad every-claim-of-this-type path a negation does,
    # preserved below exactly as before #63), and a genuine negation (SPEC
    # §8.6) is a real, on-purpose absence, not an unscoped certificate - so
    # it is read off the claim's own `negated` field, never inferred from an
    # empty party set. Only the true "certificate could name any of several
    # people" case is ambiguous: comparing it against everything fabricates
    # contradictions against unrelated marriages (the #63 repro: a groom's
    # own certificate "contradicting" his parents' wedding); comparing it
    # against nothing is the original #63 bug (two records of one marriage
    # that never get compared). Excluded from every bucket below and
    # reported back in the Result's `unscoped` list instead.
    claim_parties: dict[str, list[str]] = {}
    unscoped_claim_ids: set[str] = set()
    for cid, claim in claims_by_id.items():
        if claim['type'] not in _COUNTERPART_VITAL_TYPES:
            continue
        persons_with_roles = [
            (pid, claim_role.get((cid, pid))) for pid in claim_persons.get(cid, [])
        ]
        parties = spouse_parties(persons_with_roles)
        claim_parties[cid] = parties
        named = set(claim_persons.get(cid, []))
        if not parties and not claim['negated'] and len(named) >= 2:
            unscoped_claim_ids.add(cid)

    groups = []
    for person_id, claim_ids in sorted(claims_by_person.items()):
        by_group: dict[tuple, list[str]] = {}
        # Marriage/divorce claims with no specific counterpart identified -
        # a genuine negation naming no ex-partner, or (preserved from before
        # #63) a claim naming fewer than two people at all - compared
        # against every claim of that type for this person instead of just
        # one counterpart's bucket. Ambiguous claims do NOT land here (see
        # unscoped_claim_ids above); they are excluded from this loop
        # entirely.
        no_counterpart: dict[str, list[str]] = {}
        all_of_type: dict[str, list[str]] = {}
        for cid in claim_ids:
            claim = claims_by_id[cid]
            if claim['type'] == 'relationship':
                # A person can be e.g. a child in one relationship claim and a
                # parent in another - only pair claims with the same subtype
                # (nature) and this person's role, so a biological and an
                # adoptive parent of the same child never read as a contradiction.
                # A claim can bundle several
                # counterparts at once (e.g. roles: parent: [P2, P3]), so it's
                # bucketed once per individual counterpart rather than once
                # per whole counterpart set - otherwise a claim naming {P2, P3}
                # would never compare against one naming only {P2}.
                role = claim_role.get((cid, person_id))
                others = [p for p in claim_persons.get(cid, []) if p != person_id]
                for other in others:
                    key = (claim['type'], claim['subtype'], role, other)
                    by_group.setdefault(key, []).append(cid)
            elif claim['type'] in _COUNTERPART_VITAL_TYPES:
                parties = claim_parties.get(cid, [])
                if parties and person_id not in parties:
                    # Named on the claim (a witness, an informant, a parent
                    # on a certificate) but not one of the people
                    # spouse_parties actually names as married - this claim
                    # asserts nothing about THIS person's own marital
                    # status, so it does not enter their marriage/divorce
                    # bucket at all (#63's second open question). Without
                    # this skip, a certificate would still land in the
                    # parents' buckets too and could be compared against
                    # claims about their own, unrelated marriage - the same
                    # fabrication risk the ambiguous case guards against,
                    # just reached from the other direction.
                    continue
                if cid in unscoped_claim_ids:
                    # Ambiguous - already collected above, reported via
                    # `unscoped` instead of being forced into a bucket here.
                    continue
                # Everything else (a resolved couple, a genuine negation
                # with or without a specific ex-partner, or - unchanged from
                # before #63 - a claim naming fewer than two people at all)
                # buckets by counterpart the same way: narrow when
                # spouse_parties named one, broad (no_counterpart, compared
                # against every claim of this type) when it didn't.
                all_of_type.setdefault(claim['type'], []).append(cid)
                others = frozenset(p for p in parties if p != person_id)
                if others:
                    key = (claim['type'], others)
                    by_group.setdefault(key, []).append(cid)
                else:
                    no_counterpart.setdefault(claim['type'], []).append(cid)
            else:
                key = (claim['type'],)
                by_group.setdefault(key, []).append(cid)

        pairs = []
        seen_pairs: set[frozenset[str]] = set()

        def _try_pair(cid_a: str, cid_b: str) -> None:
            pair_key = frozenset((cid_a, cid_b))
            if pair_key in seen_pairs:
                return
            seen_pairs.add(pair_key)
            claim_a, claim_b = claims_by_id[cid_a], claims_by_id[cid_b]
            if claim_a['source_id'] == claim_b['source_id']:
                return
            if pair_key in linked_pairs:
                return
            kind = _classify_pair(claim_a, claim_b)
            if kind is None:
                return
            pairs.append({
                'kind': kind,
                'claim_a': claim_a,
                'claim_b': claim_b,
            })

        for ids in by_group.values():
            ids = sorted(set(ids))
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    # A relationship claim can land in more than one
                    # per-counterpart bucket; _try_pair dedupes via seen_pairs.
                    _try_pair(ids[i], ids[j])

        for ctype, no_ids in no_counterpart.items():
            for cid_a in no_ids:
                for cid_b in all_of_type.get(ctype, []):
                    if cid_a == cid_b:
                        continue
                    _try_pair(cid_a, cid_b)

        if pairs:
            pairs.sort(key=lambda p: (p['claim_a']['type'], p['claim_a']['id'], p['claim_b']['id']))
            groups.append({
                'person_id': person_id,
                'person_name': person_names.get(person_id, person_id),
                'pairs': pairs,
            })

    groups.sort(key=lambda g: g['person_name'] or '')

    # Sorted by claim id for stable output - unscoped_claim_ids was built as
    # a set while walking claims_by_id, whose order sqlite does not
    # guarantee.
    unscoped = [
        {
            'claim': claims_by_id[cid],
            'persons': [
                person_names.get(pid, pid) for pid in claim_persons.get(cid, [])
            ],
        }
        for cid in sorted(unscoped_claim_ids)
    ]

    return {'status': 'ok', 'groups': groups, 'unscoped': unscoped}


# ── CLI ───────────────────────────────────────────────────────────────────────

def _fmt_claim(c: dict) -> str:
    date_label = c['date_edtf'] or '(no date)'
    place = f"  @ {c['place_text']}" if c.get('place_text') else ''
    return (
        f"{fmt_id_display(c['id'])}  [{c['source_title']} / {fmt_id_display(c['source_id'])}]  "
        f"{date_label}{place} - {c['value']}"
    )


def _cmd_xref(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE

    result = run_xref(archive_root)
    if result['status'] == 'failed':
        return EXIT_FAILURE

    groups = result['groups']
    unscoped = result.get('unscoped') or []

    if not groups and not unscoped:
        print('No candidate pairs found.')
        return EXIT_CLEAN

    if groups:
        total = sum(len(g['pairs']) for g in groups)
        print(f'Found {total} candidate pair(s) across {len(groups)} person(s):')
        for group in groups:
            print(f"\n{group['person_name']}  [{fmt_id_display(group['person_id'])}]")
            for pair in group['pairs']:
                print(f"  {pair['kind']}:")
                print(f"    A: {_fmt_claim(pair['claim_a'])}")
                print(f"    B: {_fmt_claim(pair['claim_b'])}")
    else:
        print('No candidate pairs found.')

    if unscoped:
        print(f"\n{len(unscoped)} marriage/divorce claim(s) could not be scoped for comparison:")
        for item in unscoped:
            names = ', '.join(item['persons'])
            print(f"  {_fmt_claim(item['claim'])}")
            print(f"    Names {names} but not which two of them were the couple, so it "
                  "can't be compared against other records. Add a roles: map naming the "
                  'pair - `roles:` then an indented `spouse: [P-…, P-…]` line.')
    return EXIT_CLEAN


# User-facing --help text (the module docstring stays developer-facing).
_CLI_DESCRIPTION = """\
Find which of your facts back each other up and which ones conflict.

  fha xref

Read-only: it lists corroboration and contradiction candidates for you to act
on with `fha confirm xref`. It never writes to the archive."""


def register(subs: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register 'xref' onto the main fha parser."""
    p = subs.add_parser(
        'xref',
        help='Cross-reference accepted/needs-review claims for corroboration/contradiction candidates',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    p.set_defaults(func=_cmd_xref)
    return p


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha xref',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    parser.set_defaults(func=_cmd_xref)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
