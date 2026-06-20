#!/usr/bin/env python3
"""
xref.py — fha xref: cross-reference pass over the claim index.

  fha xref [--root PATH]

Read-only candidate-suggestion tool (TOOLING §14a). Does not write to the
archive — it only prints candidate pairs for a human (or a future skill
layer) to confirm. Confirmation, link-writing, and question-spawning are out
of scope for this tool.

ALGORITHM
---------
For every person, group their accepted/needs-review claims by claim `type`
(relationship claims are further split by `subtype`, this person's `role`,
and the other person(s) named in the claim, since a person can be e.g. both
a child in one `child-of` claim and a parent in another). Within each group,
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

CODE MAP
--------
  DB / root helpers (per-tool copies; tools never import other tools)
    _open_db, _resolve_root

  Classification
    _normalize_place, _place_from_vital_value — vital-claim place extraction
    _classify_pair             — corroborates/contradicts for one claim pair
    run_xref                   — group claims by person+type, pair, classify

  CLI
    _fmt_id, _fmt_claim         — display formatting
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
    edtf_bounds,
    find_archive_root,
    newest_record_mtime,
)

_VITAL_TYPES = {'birth', 'death', 'marriage', 'baptism', 'burial'}


def _fmt_id(id_str: str) -> str:
    """Return an ID string with its type prefix uppercased (p-xxx -> P-xxx).

    The index stores all IDs in lowercase; display output uses the
    uppercase-prefix convention used everywhere else in the CLI output.
    """
    if not id_str:
        return id_str
    return id_str[0].upper() + id_str[1:]


# ── DB / root helpers (per-tool copies; tools never import other tools) ──────

def _open_db(archive_root: Path) -> sqlite3.Connection | None:
    """
    Open the index database for read-only querying.

    Absent or unreadable -> print error, return None (caller exits 3).
    Stale -> warn, but still return the connection (xref is read-only).
    """
    db_path = archive_root / '.cache' / 'index.sqlite'
    if not db_path.exists():
        print(
            'ERROR: .cache/index.sqlite not found - run `fha index` first '
            'then re-run this command.',
            file=sys.stderr,
        )
        return None

    try:
        db_mtime = db_path.stat().st_mtime
        stale = newest_record_mtime(archive_root) > db_mtime
    except OSError:
        stale = False
    if stale:
        print(
            'WARNING: index may be stale — a record file is newer than '
            '.cache/index.sqlite. Run `fha index` to refresh.',
            file=sys.stderr,
        )

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        for table in ('persons', 'claims', 'sources', 'claim_persons', 'claim_links'):
            conn.execute(f'SELECT 1 FROM {table} LIMIT 1')
        return conn
    except Exception:
        print(
            'ERROR: .cache/index.sqlite is unreadable or has an incompatible schema. '
            'Run `fha index` to rebuild.',
            file=sys.stderr,
        )
        return None


def _resolve_root(args: argparse.Namespace) -> Path | None:
    """Resolve archive root from --root flag or auto-detection."""
    if getattr(args, 'root', None):
        return Path(args.root).resolve()
    detected = find_archive_root()
    if detected is None:
        print(
            'ERROR: cannot find archive root (no fha.yaml found). '
            'Use --root to specify.',
            file=sys.stderr,
        )
        return None
    return detected


# ── Core query ────────────────────────────────────────────────────────────────

def _normalize_place(text: str | None) -> str:
    return ' '.join((text or '').strip().lower().split())


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
    patterns = (
        r'\b(?:born|died|married|buried|baptized|baptised)\s+(?:in|at)\s+([^.;\n]+)',
        r'\b(?:birthplace|deathplace|marriage place|burial place|baptism place|place)\s*:\s*([^.;\n]+)',
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return _normalize_place(match.group(1))
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
        # design (§8.2) — non-overlapping dates are expected, not a conflict.
        return None

    if bool(a['negated']) != bool(b['negated']):
        # One claim asserts the fact happened, the other confirms it never
        # did, for the same place in time — that's a genuine conflict. (Vital
        # types always reach here: an undated negated claim gets unbounded
        # bounds, so it overlaps any dated positive claim of the same type.)
        # For repeatable substantive types (residence, occupation, ...) the
        # absence and the presence have to be about the *same* place — a
        # negated "did not reside in Topeka" doesn't conflict with a positive
        # "resided in Boston" the same year, since both can be true at once.
        if a['type'] not in _VITAL_TYPES:
            if a['place_id'] and b['place_id']:
                if a['place_id'] != b['place_id']:
                    return None
            else:
                place_a = _normalize_place(a['place_text'])
                place_b = _normalize_place(b['place_text'])
                if place_a and place_b and place_a != place_b:
                    return None
        return 'contradicts'

    if a['type'] in _VITAL_TYPES:
        if a['place_id'] and b['place_id']:
            if a['place_id'] != b['place_id']:
                return 'contradicts'
        else:
            place_a = _normalize_place(a['place_text']) or _place_from_vital_value(a['value'])
            place_b = _normalize_place(b['place_text']) or _place_from_vital_value(b['value'])
            if place_a and place_b and place_a != place_b:
                return 'contradicts'

    return 'corroborates'


def run_xref(archive_root: Path) -> dict:
    """
    Find corroboration/contradiction candidate claim pairs.

    Returns {'status': 'ok'|'failed', 'groups': [{'person_id', 'person_name',
    'pairs': [{'kind', 'claim_a', 'claim_b'}, ...]}, ...]}.

    Each claim dict embedded in a pair carries: id, source_id, source_title,
    type, date_edtf, place_text, value.
    """
    conn = _open_db(archive_root)
    if conn is None:
        return {'status': 'failed', 'groups': []}

    try:
        return _run_xref_queries(conn)
    except sqlite3.OperationalError:
        print(
            'ERROR: .cache/index.sqlite is unreadable or has an incompatible schema. '
            'Run `fha index` to rebuild.',
            file=sys.stderr,
        )
        return {'status': 'failed', 'groups': []}
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
    for row in conn.execute('SELECT claim_id, person_id, role FROM claim_persons'):
        if row['claim_id'] not in claims_by_id:
            continue
        claims_by_person.setdefault(row['person_id'], []).append(row['claim_id'])
        claim_persons.setdefault(row['claim_id'], []).append(row['person_id'])
        claim_role[(row['claim_id'], row['person_id'])] = row['role']

    linked_pairs: set[frozenset[str]] = set()
    for row in conn.execute('SELECT claim_id, target_id FROM claim_links'):
        linked_pairs.add(frozenset((row['claim_id'], row['target_id'])))

    person_names = {row['id']: row['name'] for row in conn.execute('SELECT id, name FROM persons')}

    groups = []
    for person_id, claim_ids in sorted(claims_by_person.items()):
        by_group: dict[tuple, list[str]] = {}
        for cid in claim_ids:
            claim = claims_by_id[cid]
            if claim['type'] == 'relationship':
                # A person can be e.g. a child in one child-of claim and a
                # parent in another — only pair claims with the same subtype
                # and this person's role. A claim can bundle several
                # counterparts at once (e.g. roles: parent: [P2, P3]), so it's
                # bucketed once per individual counterpart rather than once
                # per whole counterpart set — otherwise a claim naming {P2, P3}
                # would never compare against one naming only {P2}.
                role = claim_role.get((cid, person_id))
                others = [p for p in claim_persons.get(cid, []) if p != person_id]
                for other in others:
                    key = (claim['type'], claim['subtype'], role, other)
                    by_group.setdefault(key, []).append(cid)
            elif claim['type'] == 'marriage':
                # Marriage claims share the literal role "spouse" for both
                # parties, so the counterpart set (not role) is what
                # distinguishes one marriage from another for this person.
                others = frozenset(p for p in claim_persons.get(cid, []) if p != person_id)
                key = (claim['type'], others)
                by_group.setdefault(key, []).append(cid)
            else:
                key = (claim['type'],)
                by_group.setdefault(key, []).append(cid)

        pairs = []
        seen_pairs: set[frozenset[str]] = set()
        for ids in by_group.values():
            ids = sorted(set(ids))
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    cid_a, cid_b = ids[i], ids[j]
                    # A relationship claim can land in more than one
                    # per-counterpart bucket; skip a pair already classified
                    # via another shared counterpart.
                    pair_key = frozenset((cid_a, cid_b))
                    if pair_key in seen_pairs:
                        continue
                    seen_pairs.add(pair_key)
                    claim_a, claim_b = claims_by_id[cid_a], claims_by_id[cid_b]
                    if claim_a['source_id'] == claim_b['source_id']:
                        continue
                    if pair_key in linked_pairs:
                        continue
                    kind = _classify_pair(claim_a, claim_b)
                    if kind is None:
                        continue
                    pairs.append({
                        'kind': kind,
                        'claim_a': claim_a,
                        'claim_b': claim_b,
                    })

        if pairs:
            pairs.sort(key=lambda p: (p['claim_a']['type'], p['claim_a']['id'], p['claim_b']['id']))
            groups.append({
                'person_id': person_id,
                'person_name': person_names.get(person_id, person_id),
                'pairs': pairs,
            })

    groups.sort(key=lambda g: g['person_name'] or '')
    return {'status': 'ok', 'groups': groups}


# ── CLI ───────────────────────────────────────────────────────────────────────

def _fmt_claim(c: dict) -> str:
    date_label = c['date_edtf'] or '(no date)'
    place = f"  @ {c['place_text']}" if c.get('place_text') else ''
    return (
        f"{_fmt_id(c['id'])}  [{c['source_title']} / {_fmt_id(c['source_id'])}]  "
        f"{date_label}{place} — {c['value']}"
    )


def _cmd_xref(args: argparse.Namespace) -> int:
    archive_root = _resolve_root(args)
    if archive_root is None:
        return EXIT_FAILURE

    result = run_xref(archive_root)
    if result['status'] == 'failed':
        return EXIT_FAILURE

    groups = result['groups']
    if not groups:
        print('No candidate pairs found.')
        return EXIT_CLEAN

    total = sum(len(g['pairs']) for g in groups)
    print(f'Found {total} candidate pair(s) across {len(groups)} person(s):')
    for group in groups:
        print(f"\n{group['person_name']}  [{_fmt_id(group['person_id'])}]")
        for pair in group['pairs']:
            print(f"  {pair['kind']}:")
            print(f"    A: {_fmt_claim(pair['claim_a'])}")
            print(f"    B: {_fmt_claim(pair['claim_b'])}")
    return EXIT_CLEAN


def register(subs: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register 'xref' onto the main fha parser."""
    p = subs.add_parser(
        'xref',
        help='Cross-reference accepted/needs-review claims for corroboration/contradiction candidates',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    p.set_defaults(func=_cmd_xref)
    return p


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha xref',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    parser.set_defaults(func=_cmd_xref)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
