#!/usr/bin/env python3
"""
cooccur.py - fha cooccur: connection candidate detection over the claim index.

  fha cooccur [--threshold N] [--root PATH]

Read-only candidate-suggestion tool (TOOLING §14a2) - sibling to
`fha places candidates`: deterministic clustering, human-confirm discipline,
consumed through `fha report`. Never writes to the archive: confirming a
person-pair mints a `relationship` claim and dismissing one records a
tombstone, but both of those writes belong to a future skill layer, not this
tool. This tool only reads `.cache/cooccur_dismissed.json`; it never writes it.

THREE OUTPUTS (TOOLING §690)
----------------------------
1. Person co-occurrence: person-pairs named together in >= `--threshold`
   (default 2) distinct sources, with no existing `relationships` edge
   between them, ranked by source count then source-type variety.

2. Shared-place co-occurrence: accepted/needs-review claims of different,
   unlinked people that share a place (`place_id` if both have one, else
   normalized `place_text`) with overlapping EDTF date bounds - e.g. two
   people each placed in the same town the same year by different sources,
   with no existing `relationships` edge between them.

3. Org/entity recurrence: repeated claim values for `occupation`,
   `military`, and membership-style `event`/`note` claims. The grouping key is
   `(category, normalized value)` so employers, military units, and clubs with
   similar wording do not collapse into one hub. These stay claim values - no
   schema change, no new `O-` object type (organizations are out of scope for now).

CODE MAP
--------
  DB / root / tombstone helpers
    open_index_db, resolve_root_arg - shared via _lib.py
    _load_dismissed                - tombstone reader (unique to cooccur; see below)

  Person co-occurrence
    _person_cooccurrence       - pair candidates ranked by source count + variety

  Shared-place co-occurrence
    _same_place, _place_cooccurrence - same-place, overlapping-dates pairs
                                  (uses _lib.normalize_place_text)

  Org / entity recurrence
    _org_category, _normalize_entity_value - claim -> hub grouping key
    _org_recurrence            - group claims into recurring affiliation hubs

  Top-level query / CLI
    run_cooccur, _cmd_cooccur, register, _standalone_main
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from itertools import combinations
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
)

_ORG_CLAIM_TYPES = {'occupation', 'military', 'event', 'note'}
_DIRECT_ORG_TYPES = {'occupation', 'military'}

_REQUIRED_TABLES = ('persons', 'claims', 'sources', 'claim_persons', 'source_people', 'relationships')


def _load_dismissed(archive_root: Path) -> set[frozenset[str]]:
    """
    Read the dismissed-pairs tombstone. Missing file = empty set, not an error
    - the skill layer writes this file; this tool only ever reads it.
    """
    path = archive_root / '.cache' / 'cooccur_dismissed.json'
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return set()
    pairs = data.get('pairs') or []
    out = set()
    for pair in pairs:
        if isinstance(pair, list) and len(pair) == 2:
            out.add(frozenset(p.strip().lower() for p in pair))
    return out


# ── Person co-occurrence ─────────────────────────────────────────────────────

def _person_cooccurrence(conn: sqlite3.Connection, threshold: int, dismissed: set[frozenset[str]]) -> list[dict]:
    # `source_people` is populated from a source's optional frontmatter
    # `people:` list; a source's claims (`claim_persons`) carry participants
    # too and may name people that list omits, so union both.
    sources_by_person: dict[str, set[str]] = {}
    for row in conn.execute('SELECT source_id, person_id FROM source_people'):
        sources_by_person.setdefault(row['person_id'], set()).add(row['source_id'])
    for row in conn.execute(
        '''
        SELECT DISTINCT c.source_id AS source_id, cp.person_id AS person_id
        FROM claim_persons cp
        JOIN claims c ON c.id = cp.claim_id
        WHERE c.status IN ('accepted', 'needs-review')
        '''
    ):
        sources_by_person.setdefault(row['person_id'], set()).add(row['source_id'])

    persons_by_source: dict[str, set[str]] = {}
    for pid, sids in sources_by_person.items():
        for sid in sids:
            persons_by_source.setdefault(sid, set()).add(pid)

    pair_sources: dict[frozenset[str], set[str]] = {}
    for sid, pids in persons_by_source.items():
        for a, b in combinations(sorted(pids), 2):
            pair_sources.setdefault(frozenset((a, b)), set()).add(sid)

    existing_edges: set[frozenset[str]] = set()
    for row in conn.execute('SELECT person_id, other_id FROM relationships'):
        existing_edges.add(frozenset((row['person_id'], row['other_id'])))

    source_types = {row['id']: row['source_type'] for row in conn.execute('SELECT id, source_type FROM sources')}
    names = {row['id']: row['name'] for row in conn.execute('SELECT id, name FROM persons')}

    candidates = []
    for pair, sids in pair_sources.items():
        if len(sids) < threshold:
            continue
        if pair in existing_edges:
            continue
        if pair in dismissed:
            continue
        variety = len({source_types.get(sid) for sid in sids})
        a, b = sorted(pair)
        candidates.append({
            'person_a': a,
            'person_b': b,
            'name_a': names.get(a, a),
            'name_b': names.get(b, b),
            'source_ids': sorted(sids),
            'source_count': len(sids),
            'variety': variety,
        })

    candidates.sort(key=lambda c: (-c['source_count'], -c['variety'], c['person_a'], c['person_b']))
    return candidates


# ── Shared-place co-occurrence ───────────────────────────────────────────────

def _same_place(a: dict, b: dict) -> bool:
    """
    Whether two claims describe the same place, per the documented precedence:
    structured `place_id` when both claims have one, else normalized
    `place_text`. A claim with `place_id` but no `place_text` (or vice versa)
    still matches a counterpart that only has the other field, as long as
    that field agrees - fixing the id/text fallback ignores a partially
    migrated archive where only one side has been normalized.
    """
    if a['place_id'] and b['place_id']:
        return a['place_id'].strip().lower() == b['place_id'].strip().lower()
    place_a = normalize_place_text(a['place_text'])
    place_b = normalize_place_text(b['place_text'])
    return bool(place_a) and place_a == place_b


def _place_cooccurrence(conn: sqlite3.Connection, dismissed: set[frozenset[str]]) -> list[dict]:
    """
    Accepted/needs-review claims of different, unlinked people that share a
    place (structured `place_id` preferred, else normalized `place_text`)
    with overlapping EDTF bounds - e.g. two people each placed in the same
    town the same year by different sources (TOOLING §690b).
    """
    claims = {
        row['id']: dict(row)
        for row in conn.execute(
            '''
            SELECT id, source_id, place_id, place_text, date_edtf
            FROM claims
            WHERE status IN ('accepted', 'needs-review')
              AND (negated IS NULL OR negated = 0)
              AND date_edtf IS NOT NULL AND date_edtf != ''
              AND (
                (place_id IS NOT NULL AND place_id != '')
                OR (place_text IS NOT NULL AND place_text != '')
              )
            '''
        )
    }
    persons_by_claim: dict[str, set[str]] = {}
    for row in conn.execute('SELECT claim_id, person_id FROM claim_persons'):
        if row['claim_id'] in claims:
            persons_by_claim.setdefault(row['claim_id'], set()).add(row['person_id'])

    existing_edges: set[frozenset[str]] = set()
    for row in conn.execute('SELECT person_id, other_id FROM relationships'):
        existing_edges.add(frozenset((row['person_id'], row['other_id'])))

    names = {row['id']: row['name'] for row in conn.execute('SELECT id, name FROM persons')}

    # Bucket claims by place first so the pairwise comparison below only ever
    # runs within a shared-place bucket, not across every placed claim in the
    # archive - `_same_place` can only be true for claims that land in the
    # same place_id bucket or the same normalized place_text bucket.
    by_place_id: dict[str, list[str]] = {}
    by_place_text: dict[str, list[str]] = {}
    for cid, claim in claims.items():
        if claim['place_id']:
            by_place_id.setdefault(claim['place_id'].strip().lower(), []).append(cid)
        text_norm = normalize_place_text(claim['place_text'])
        if text_norm:
            by_place_text.setdefault(text_norm, []).append(cid)

    # Cache keyed by (person pair, normalized place) - not person pair alone -
    # so two people sharing more than one place get a separate candidate per
    # place instead of one candidate whose claim_ids/source_ids blend places.
    pair_data: dict[tuple[frozenset[str], str], dict] = {}
    compared: set[frozenset[str]] = set()

    def _consider(cid_a: str, cid_b: str) -> None:
        claim_pair = frozenset((cid_a, cid_b))
        if claim_pair in compared:
            return
        compared.add(claim_pair)
        claim_a, claim_b = claims[cid_a], claims[cid_b]
        if claim_a['source_id'] == claim_b['source_id']:
            return
        if not _same_place(claim_a, claim_b):
            return
        a_min, a_max = edtf_bounds(claim_a['date_edtf'])
        b_min, b_max = edtf_bounds(claim_b['date_edtf'])
        if not (a_min <= b_max and b_min <= a_max):
            return
        place_label = claim_a['place_text'] or claim_b['place_text'] or claim_a['place_id'] or claim_b['place_id']
        # When both claims share a place_id, canonicalize the bucket key on
        # that id rather than on whichever place_text happens to be
        # encountered first - otherwise the same person-pair/place_id can
        # fragment into multiple candidates keyed by different aliases.
        if claim_a['place_id'] and claim_b['place_id']:
            place_norm = claim_a['place_id'].strip().lower()
        else:
            place_norm = normalize_place_text(place_label) or (place_label or '').strip().lower()
        for pa in persons_by_claim.get(cid_a, ()):
            for pb in persons_by_claim.get(cid_b, ()):
                if pa == pb:
                    continue
                pair = frozenset((pa, pb))
                if pair in existing_edges or pair in dismissed:
                    continue
                entry = pair_data.setdefault((pair, place_norm), {
                    'place_label': place_label,
                    'claim_ids': set(),
                    'source_ids': set(),
                })
                entry['claim_ids'].update((cid_a, cid_b))
                entry['source_ids'].update((claim_a['source_id'], claim_b['source_id']))

    for bucket in (*by_place_id.values(), *by_place_text.values()):
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                _consider(bucket[i], bucket[j])

    candidates = []
    for (pair, _place_norm), data in pair_data.items():
        a, b = sorted(pair)
        candidates.append({
            'person_a': a,
            'person_b': b,
            'name_a': names.get(a, a),
            'name_b': names.get(b, b),
            'place_label': data['place_label'],
            'claim_ids': sorted(data['claim_ids']),
            'source_ids': sorted(data['source_ids']),
            'source_count': len(data['source_ids']),
        })

    candidates.sort(key=lambda c: (-c['source_count'], c['person_a'], c['person_b']))
    return candidates


# ── Org / entity recurrence ──────────────────────────────────────────────────

def _org_category(claim: dict) -> str | None:
    """
    Return the org/entity category a claim participates in, if any.

    `occupation` and `military` are direct categories in the claim vocabulary.
    Membership is represented as a free-text subtype on `event` or `note`, so
    those broader claim types only count when the subtype says membership.
    """
    ctype = claim.get('type')
    if ctype in _DIRECT_ORG_TYPES:
        return ctype
    subtype = str(claim.get('subtype') or '').strip().lower()
    if ctype in {'event', 'note'} and subtype == 'membership':
        return 'membership'
    return None


def _normalize_entity_value(value: str) -> str:
    """Normalize a claim value for exact recurring-affiliation grouping."""
    return ' '.join((value or '').strip().lower().split())


def _entity_label(category: str, value: str) -> str:
    """
    Extract the entity/organization portion of a claim value for grouping.

    `occupation` and `military` values follow the documented "role, entity"
    convention (SPEC §8.4, e.g. "bookkeeper, Plains Junction Railroad") - the
    role varies between claims about the same employer, so grouping on the
    whole value would split one recurring employer into separate hubs. The
    entity is the text after the FIRST comma, since entity names can
    themselves contain commas (e.g. "Plains Junction Railroad, Topeka Div.") -
    splitting on the last comma would drop everything before the entity's own
    internal comma. Membership values have no documented role/entity split,
    so they're used as-is.
    """
    label = (value or '').strip()
    if category in _DIRECT_ORG_TYPES and ',' in label:
        label = label.split(',', 1)[1].strip()
    return label


def _org_recurrence(conn: sqlite3.Connection, threshold: int = 2) -> list[dict]:
    claims = {
        row['id']: dict(row)
        for row in conn.execute(
            '''
            SELECT id, source_id, type, subtype, value
            FROM claims
            WHERE status IN ('accepted', 'needs-review')
              AND (negated IS NULL OR negated = 0)
              AND type IN ({})
            '''.format(','.join('?' * len(_ORG_CLAIM_TYPES))),
            tuple(_ORG_CLAIM_TYPES),
        )
    }
    persons_by_claim: dict[str, set[str]] = {}
    for row in conn.execute('SELECT claim_id, person_id FROM claim_persons'):
        if row['claim_id'] in claims:
            persons_by_claim.setdefault(row['claim_id'], set()).add(row['person_id'])

    groups: dict[tuple[str, str], dict] = {}
    for cid, claim in claims.items():
        category = _org_category(claim)
        if category is None:
            continue
        label = _entity_label(category, claim.get('value') or '')
        normalized = _normalize_entity_value(label)
        if not normalized:
            continue
        key = (category, normalized)
        group = groups.setdefault(key, {
            'label': label,
            'category': category,
            'claim_ids': [],
            'person_ids': set(),
            'source_ids': set(),
        })
        group['claim_ids'].append(cid)
        group['person_ids'].update(persons_by_claim.get(cid, set()))
        group['source_ids'].add(claim['source_id'])

    out = []
    for group in groups.values():
        if len(group['person_ids']) >= threshold or len(group['source_ids']) >= threshold:
            out.append({
                'label': group['label'],
                'category': group['category'],
                'claim_ids': sorted(group['claim_ids']),
                'person_count': len(group['person_ids']),
                'source_count': len(group['source_ids']),
            })

    out.sort(key=lambda g: (-g['person_count'], -g['source_count'], g['category'], g['label']))
    return out


# ── Top-level query ───────────────────────────────────────────────────────────

def run_cooccur(archive_root: Path, threshold: int = 2) -> Result:
    """
    Detect connection candidates from the index.

    Returns a `Result` whose `data` is {'status': 'ok'|'failed', 'person_pairs':
    [...], 'place_pairs': [...], 'org_groups': [...]}.  Result exposes dict-style
    access (_lib.py), so callers keep reading `result['person_pairs']` unchanged.
    """
    conn = open_index_db(archive_root, _REQUIRED_TABLES)
    if conn is None:
        return Result(ok=False, exit_code=EXIT_FAILURE, data={
            'status': 'failed', 'person_pairs': [], 'place_pairs': [], 'org_groups': [],
        })

    try:
        dismissed = _load_dismissed(archive_root)
        person_pairs = _person_cooccurrence(conn, threshold, dismissed)
        place_pairs = _place_cooccurrence(conn, dismissed)
        org_groups = _org_recurrence(conn, threshold)
    except sqlite3.OperationalError:
        print(
            'ERROR: .cache/index.sqlite is unreadable or has an incompatible schema. '
            'Run `fha index` to rebuild.',
            file=sys.stderr,
        )
        return Result(ok=False, exit_code=EXIT_FAILURE, data={
            'status': 'failed', 'person_pairs': [], 'place_pairs': [], 'org_groups': [],
        })
    finally:
        conn.close()

    return Result(exit_code=EXIT_CLEAN, data={
        'status': 'ok',
        'person_pairs': person_pairs,
        'place_pairs': place_pairs,
        'org_groups': org_groups,
    })


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cmd_cooccur(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE

    threshold = getattr(args, 'threshold', None)
    if threshold is None:
        threshold = 2
    if threshold < 1:
        print('ERROR: --threshold must be a positive integer.', file=sys.stderr)
        return EXIT_FAILURE

    result = run_cooccur(archive_root, threshold=threshold)
    if result['status'] == 'failed':
        return EXIT_FAILURE

    pairs = result['person_pairs']
    if pairs:
        print(f'Found {len(pairs)} candidate person co-occurrence pair(s):')
        for c in pairs:
            print(
                f"  {c['name_a']} [{fmt_id_display(c['person_a'])}]  <->  "
                f"{c['name_b']} [{fmt_id_display(c['person_b'])}]  "
                f"- {c['source_count']} source(s), {c['variety']} type(s)"
            )
            for sid in c['source_ids']:
                print(f"    {fmt_id_display(sid)}")
    else:
        print('No candidate person co-occurrence pairs found.')

    place_pairs = result['place_pairs']
    if place_pairs:
        print(f'\nFound {len(place_pairs)} candidate shared-place co-occurrence pair(s):')
        for c in place_pairs:
            print(
                f"  {c['name_a']} [{fmt_id_display(c['person_a'])}]  <->  "
                f"{c['name_b']} [{fmt_id_display(c['person_b'])}]  "
                f"@ {c['place_label']}  - {c['source_count']} source(s)"
            )
            for cid in c['claim_ids']:
                print(f"    {fmt_id_display(cid)}")
    else:
        print('\nNo candidate shared-place co-occurrence pairs found.')

    groups = result['org_groups']
    if groups:
        print(f'\nFound {len(groups)} candidate org/entity recurrence hub(s):')
        for g in groups:
            print(
                f"  {g['label']} [{g['category']}] - "
                f"{g['person_count']} people, {g['source_count']} sources"
            )
            for cid in g['claim_ids']:
                print(f"    {fmt_id_display(cid)}")
    else:
        print('\nNo candidate org/entity recurrence hubs found.')

    return EXIT_CLEAN


def register(subs: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register 'cooccur' onto the main fha parser."""
    p = subs.add_parser(
        'cooccur',
        help='Find people who keep showing up together (shared sources, places, organizations)',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    p.add_argument('--threshold', type=int, default=2, metavar='N',
                    help='Minimum distinct shared sources for a person co-occurrence candidate (default: 2).')
    p.set_defaults(func=_cmd_cooccur)
    return p


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha cooccur',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    parser.add_argument('--threshold', type=int, default=2, metavar='N')
    parser.set_defaults(func=_cmd_cooccur)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
