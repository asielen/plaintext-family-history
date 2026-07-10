#!/usr/bin/env python3
"""
gedcom.py - fha gedcom: derive a GEDCOM 5.5.1 exchange file (TOOLING §13a).

  fha gedcom [<P-id>] [--mode descendants|ancestors|connected]
             [--generations N] [--all] [--include-living] [--out FILE]
             [--root PATH]

GEDCOM is a one-way bridge to other genealogy applications. It is *derived* at
export time from the index's `relationships` edges and accepted vital claims -
never stored in the archive, never re-imported as truth (the archive is never
GEDCOM's corpus - GEDCOM is a one-way bridge to other apps). The header carries an explicit
"do not re-import as truth" note to make that contract travel with the file.

SCOPE SELECTION
---------------
Either a starting `<P-id>` with a traversal `--mode`, or `--all` for everyone:
  - descendants  - BFS down `child` edges from the seed (depth-capped by
                   --generations); each person's spouses are pulled in so couples
                   stay whole, but a spouse's own ancestry is not followed.
  - ancestors    - BFS up `parent` edges from the seed (depth-capped); spouses of
                   reached people are added so each ancestral couple is complete.
  - connected    - the entire connected component reachable from the seed over
                   parent/child/spouse edges (--generations ignored).
  - --all        - every (non-merged) person in the index.

PRIVACY (TOOLING §13a, SPEC §21)
--------------------------------
`living: true` and `living: unknown` are redacted by default (unknown == living):
the person's NAME becomes `/Living/`, and their birth/death events and the
marriage details of any family they belong to are withheld. Structural links
(FAMC/FAMS/HUSB/WIFE/CHIL) are kept so the tree shape survives. `--include-living`
lifts the redaction for a fully private export.

GEDCOM is public/export-facing unless the human opts into a future private mode,
so restricted and DNA sources are not eligible fact sources. Relationship edges
backed exclusively by restricted/DNA sources are excluded alongside their vital
and marriage event details.

SOURCES
-------
Every exported vital fact's `source_id` becomes a `SOUR` pointer to a top-level
`SOUR` record (TITL = source title, REFN = the S-id), so the citation trail
follows the facts into the other application.

CODE MAP
--------
  Date / name formatting
    _edtf_to_gedcom        - EDTF date string → GEDCOM 5.5.1 date phrase
    _gedcom_name           - (name, surname) → GEDCOM `Given /Surname/`
    _escape                - collapse newlines for a single GEDCOM line value

  Graph
    _RelIndex              - adjacency over the relationships table
    _traverse              - descendants/ancestors/connected person selection
    _build_families        - couples + children → family records keyed by parent set

  Data gathering
    _load_persons, _load_vitals, _load_marriages

  Emission
    _emit_header, _emit_individual, _emit_family, _emit_source, run_gedcom

  CLI
    _cmd_gedcom, register, _standalone_main
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    Result,
    configure_utf8_stdout,
    fmt_id_display,
    id_type_of,
    is_valid_id,
    normalize_id,
    open_index_db,
    read_record,
    resolve_root_arg,
)

configure_utf8_stdout()


# ── The `restricted` marker (SPEC §19, §21) ────────────────────────────────────
# GEDCOM is a public-export path, so anything `restricted` is withheld wherever
# it appears. The index stores `restricted` only as 0/1, so a free-text type
# (`restricted: by-request` on a source or person) is read from the record file.
# Duplicated per export tool (tools never import tools, TOOLING §15).

def _is_restricted_value(value) -> bool:
    """True when a `restricted:` value withholds the record from public output.

    The marker is open (SPEC §19): the plain boolean `true` or any free-text
    type all mean restricted; only absent/false is not. (`read_record` coerces
    booleans to `'true'`/`'false'`.) For a public path there is no opt-in - even
    `restricted: by-request` is honored - so a single truthiness test suffices."""
    return value not in (None, False, '', 'false')

_REQUIRED_TABLES = (
    'persons', 'claims', 'claim_persons', 'relationships', 'places', 'sources',
)

_MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN',
           'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC']

_GEDCOM_VERSION = '5.5.1'


# ── Date / name formatting ──────────────────────────────────────────────────────

def _one_edtf_to_gedcom(s: str) -> str | None:
    """Convert a single (non-interval) EDTF token to a GEDCOM date phrase.

    Returns None when the token carries no usable year (an open/unknown bound),
    so the caller can omit the DATE line rather than emit a bare/empty one.
    """
    s = s.strip()
    if not s:
        return None

    # Open-ended bound like [..1920] - express as a GEDCOM "before" date.
    before = re.match(r'^\[\.{2}(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?\]$', s)
    if before:
        y, m, d = before.group(1), before.group(2), before.group(3)
        tail = y
        if m and 1 <= int(m) <= 12:
            tail = f'{_MONTHS[int(m) - 1]} {y}'
            if d:
                tail = f'{int(d)} {tail}'
        return f'BEF {tail}'

    # Decade like 185X → approximate mid-decade.
    decade = re.match(r'^(\d{3})X$', s)
    if decade:
        return f'ABT {decade.group(1)}5'

    approx = s.endswith('~') or s.endswith('?') or '~' in s or '?' in s
    core = s.replace('~', '').replace('?', '')

    ymd = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', core)
    if ymd:
        y, m, d = int(ymd.group(1)), int(ymd.group(2)), int(ymd.group(3))
        if 1 <= m <= 12:
            phrase = f'{d} {_MONTHS[m - 1]} {y}'
        else:
            phrase = str(y)
        return f'ABT {phrase}' if approx else phrase

    ym = re.match(r'^(\d{4})-(\d{2})$', core)
    if ym:
        y, m = int(ym.group(1)), int(ym.group(2))
        phrase = f'{_MONTHS[m - 1]} {y}' if 1 <= m <= 12 else str(y)
        return f'ABT {phrase}' if approx else phrase

    yr = re.match(r'^(\d{4})$', core)
    if yr:
        return f'ABT {yr.group(1)}' if approx else yr.group(1)

    return None


def _edtf_to_gedcom(edtf: str | None) -> str | None:
    """Convert an EDTF date (possibly an A/B interval) to a GEDCOM date phrase."""
    if not edtf:
        return None
    edtf = edtf.strip()
    if '/' in edtf:
        a, b = edtf.split('/', 1)
        ga = _one_edtf_to_gedcom(a)
        gb = _one_edtf_to_gedcom(b)
        if ga and gb:
            # Strip ABT/BEF qualifiers inside a range - BET..AND already conveys
            # the span's fuzziness.
            ga_core = ga.split(' ', 1)[-1] if ga.startswith(('ABT ', 'BEF ')) else ga
            gb_core = gb.split(' ', 1)[-1] if gb.startswith(('ABT ', 'BEF ')) else gb
            return f'BET {ga_core} AND {gb_core}'
        return ga or gb
    return _one_edtf_to_gedcom(edtf)


def _escape(value: str | None) -> str:
    """Collapse a value to a single GEDCOM line (newlines/tabs → spaces)."""
    if not value:
        return ''
    return ' '.join(str(value).split())


def _gedcom_name(name: str, surname: str | None) -> str:
    """Render a person name as GEDCOM `Given /Surname/`.

    Uses the index's `surname` (the birth surname from the filename slug) when
    present; otherwise falls back to the last whitespace token of the full name.
    """
    name = _escape(name) or 'Unknown'
    sn = _escape(surname) if surname else ''
    if sn and name.lower().endswith(sn.lower()):
        given = name[: len(name) - len(sn)].strip()
        return f'{given} /{sn}/'.strip()
    if not sn:
        parts = name.split()
        if len(parts) == 1:
            return f'{parts[0]} //'
        sn = parts[-1]
        given = ' '.join(parts[:-1])
        return f'{given} /{sn}/'
    # surname recorded but not a suffix of name - append it as the slash field
    return f'{name} /{sn}/'


# ── Graph traversal ─────────────────────────────────────────────────────────────

class _RelIndex:
    """Adjacency over the relationships table, grouped by rel type."""

    def __init__(self, rows: list[sqlite3.Row]):
        self._by_rel: dict[str, dict[str, set[str]]] = {
            'parent': {}, 'child': {}, 'spouse': {},
        }
        self._all: dict[str, set[str]] = {}
        for row in rows:
            rel = row['rel']
            a, b = row['person_id'], row['other_id']
            if rel in self._by_rel:
                self._by_rel[rel].setdefault(a, set()).add(b)
                self._all.setdefault(a, set()).add(b)

    def neighbors(self, pid: str, rel: str) -> set[str]:
        return self._by_rel.get(rel, {}).get(pid, set())

    def all_neighbors(self, pid: str) -> set[str]:
        return self._all.get(pid, set())


def _traverse(rel: _RelIndex, seed: str, mode: str, generations: int | None) -> set[str]:
    """Select the person set for one export run (see module docstring)."""
    if mode == 'connected':
        included = {seed}
        frontier = [seed]
        while frontier:
            nxt = []
            for p in frontier:
                for o in rel.all_neighbors(p):
                    if o not in included:
                        included.add(o)
                        nxt.append(o)
            frontier = nxt
        return included

    follow = 'child' if mode == 'descendants' else 'parent'
    included = {seed}
    frontier = [(seed, 0)]
    while frontier:
        nxt = []
        for p, depth in frontier:
            if generations is not None and depth >= generations:
                continue
            for o in rel.neighbors(p, follow):
                if o not in included:
                    included.add(o)
                    nxt.append((o, depth + 1))
        frontier = nxt

    # One spouse hop so each couple is whole (a spouse's own lineage is not pulled in).
    for p in list(included):
        included |= rel.neighbors(p, 'spouse')
    return included


def _build_families(
    rel: _RelIndex, included: set[str],
) -> tuple[list[tuple[frozenset[str], set[str]]], dict[str, set[frozenset[str]]],
           dict[str, frozenset[str]]]:
    """
    Group included persons into families.

    Returns:
      families      - list of (parent_key, child_ids), parent_key a frozenset of
                      1-2 parent ids, in deterministic order.
      person_fams   - person_id → set of family parent_keys they are a SPOUSE/parent in.
      child_fam     - person_id → the family parent_key they are a CHILD in (one only;
                      first deterministic parent set wins if data is contradictory).
    """
    fam_children: dict[frozenset[str], set[str]] = {}

    # Children → their parent set (restricted to included persons).
    for child in sorted(included):
        parents = sorted(p for p in rel.neighbors(child, 'parent') if p in included)
        if parents:
            if len(parents) > 2:
                parents = parents[:2]
            key = frozenset(parents)
            fam_children.setdefault(key, set()).add(child)

    # Spouse pairs → ensure a (childless) family exists for each couple.
    for p in sorted(included):
        for o in rel.neighbors(p, 'spouse'):
            if o in included:
                key = frozenset({p, o})
                fam_children.setdefault(key, set())

    families = sorted(fam_children.items(), key=lambda kv: tuple(sorted(kv[0])))

    person_fams: dict[str, set[frozenset[str]]] = {}
    child_fam: dict[str, frozenset[str]] = {}
    for key, children in families:
        for parent in key:
            person_fams.setdefault(parent, set()).add(key)
        for child in children:
            # A person is a child in at most one family record; keep the first
            # (deterministic) assignment rather than emitting two FAMC links.
            child_fam.setdefault(child, key)
    return families, person_fams, child_fam


# ── Data gathering ──────────────────────────────────────────────────────────────

def _load_persons(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        "SELECT id, name, surname, sex, living, tier, status, path FROM persons "
        "WHERE status != 'merged'"
    ).fetchall()
    return {r['id']: r for r in rows}


def _restricted_person_ids(archive_root: Path, persons: dict[str, sqlite3.Row]) -> set[str]:
    """Person ids whose record carries a `restricted` marker (any value).

    Read from the person `.md` because the index does not carry a person-level
    `restricted` column. A restricted person's name is withheld (`/Restricted/`)
    while their structural family links stay, mirroring living redaction."""
    out: set[str] = set()
    for pid, row in persons.items():
        path = row['path']
        if not path:
            continue
        try:
            value = read_record(archive_root / path)['meta'].get('restricted')
        except Exception:
            continue
        if _is_restricted_value(value):
            out.add(pid)
    return out


def _restricted_source_ids(conn: sqlite3.Connection, archive_root: Path) -> set[str]:
    """Source ids that are restricted by a free-text type the index missed.

    `_public_source_filter_sql` already excludes index `restricted=1`, DNA, and
    `publication_ok=0`. A source carrying a free-text `restricted: by-request`
    stores `restricted=0` in the index, so it is read from the record file here
    and added to the not-eligible set the caller unions into its source filter."""
    out: set[str] = set()
    for row in conn.execute('SELECT id, path, restricted FROM sources').fetchall():
        if row['restricted']:
            continue   # already caught by the SQL filter
        if not row['path']:
            continue
        try:
            value = read_record(archive_root / row['path'])['meta'].get('restricted')
        except Exception:
            continue
        if _is_restricted_value(value):
            out.add(row['id'])
    return out


def _restricted_claim_ids(conn: sqlite3.Connection, archive_root: Path) -> set[str]:
    """Claim ids that carry a per-claim `restricted:` marker in their source record.

    The claims table stores no claim-level `restricted` column; the flag lives
    in the source record file.  This function reads every source record whose
    source is otherwise public (index-restricted sources are already excluded by
    `_public_source_filter_sql`) to collect per-claim ids that are withheld."""
    out: set[str] = set()
    for row in conn.execute('SELECT id, path, restricted FROM sources').fetchall():
        if row['restricted'] or not row['path']:
            continue
        try:
            rec = read_record(archive_root / row['path'])
        except Exception:
            continue
        for claim in rec.get('claims') or []:
            if not isinstance(claim, dict):
                continue
            cid = normalize_id(str(claim.get('id', '')).strip())
            if cid and _is_restricted_value(claim.get('restricted')):
                out.add(cid)
    return out


def _public_source_filter_sql() -> str:
    """SQL predicate for public/export-safe sources.

    SPEC §21 excludes restricted, DNA, and publication_ok=false sources from
    public output. GEDCOM's current CLI has no include-restricted flag, so
    public-safe selection is the only supported path.
    """
    return "(COALESCE(s.restricted, 0) = 0 AND COALESCE(s.source_type, '') != 'dna' AND COALESCE(s.publication_ok, 1) != 0)"


def _load_vitals(
    conn: sqlite3.Connection, pids: set[str],
    is_public: 'Callable[[sqlite3.Row], bool] | None' = None,
) -> dict[tuple[str, str], sqlite3.Row]:
    """First public-safe accepted birth/death claim per (person_id, type).

    Filtering joins through `sources` here rather than later during emission so
    a restricted/DNA fact cannot leak as an event with the source pointer merely
    omitted. `is_public`, when given, also rejects free-text-restricted and
    per-claim-restricted rows BEFORE the per-key pick, so an earlier-dated
    restricted claim cannot evict (and thereby suppress) a publishable later one.
    """
    if not pids:
        return {}
    placeholders = ','.join('?' * len(pids))
    rows = conn.execute(
        f"""
        SELECT cp.person_id, c.id, c.type, c.date_edtf, c.place_id, c.place_text, c.source_id
        FROM claim_persons cp
        JOIN claims c ON cp.claim_id = c.id
        JOIN sources s ON s.id = c.source_id
        WHERE cp.person_id IN ({placeholders})
          AND c.type IN ('birth', 'death')
          AND c.status = 'accepted'
          AND {_public_source_filter_sql()}
        ORDER BY
            CASE WHEN c.date_min IS NULL OR c.date_min = '' THEN 1 ELSE 0 END,
            c.date_min ASC
        """,
        list(pids),
    ).fetchall()
    out: dict[tuple[str, str], sqlite3.Row] = {}
    for r in rows:
        if is_public is not None and not is_public(r):
            continue
        out.setdefault((r['person_id'], r['type']), r)
    return out


def _spouse_persons_for_claim(conn: sqlite3.Connection, claim_id: str) -> frozenset[str]:
    """Return the spouse pair for a marriage claim.

    Marriage claims may name witnesses or other participants in `persons:`.
    When roles are present, only role=`spouse` belongs in the GEDCOM family
    event key. Older/simple claims often omit roles, so the fallback is the
    first two persons by position, matching the historical positional convention
    without letting extra witnesses break a valid couple match.
    """
    rows = conn.execute(
        """
        SELECT person_id, role, position
        FROM claim_persons
        WHERE claim_id = ?
        ORDER BY position
        """,
        (claim_id,),
    ).fetchall()
    spouses = [r['person_id'] for r in rows if (r['role'] or '').lower() == 'spouse']
    if len(spouses) >= 2:
        return frozenset(spouses[:2])
    ordered = [r['person_id'] for r in rows]
    if len(ordered) >= 2:
        return frozenset(ordered[:2])
    return frozenset()


def _load_marriages(
    conn: sqlite3.Connection,
    is_public: 'Callable[[sqlite3.Row], bool] | None' = None,
) -> dict[frozenset[str], sqlite3.Row]:
    """Accepted public-safe marriage claims keyed by spouse pair.

    `is_public`, when given, rejects free-text-restricted and per-claim-restricted
    rows BEFORE the per-couple pick, so a restricted marriage claim cannot evict
    (and thereby suppress) a publishable one for the same couple.
    """
    rows = conn.execute(
        f"""
        SELECT c.id, c.date_edtf, c.place_id, c.place_text, c.source_id
        FROM claims c
        JOIN sources s ON s.id = c.source_id
        WHERE c.type = 'marriage' AND c.status = 'accepted'
          AND {_public_source_filter_sql()}
        """
    ).fetchall()
    out: dict[frozenset[str], sqlite3.Row] = {}
    for r in rows:
        if is_public is not None and not is_public(r):
            continue
        persons = _spouse_persons_for_claim(conn, r['id'])
        if len(persons) >= 2:
            out.setdefault(frozenset(persons), r)
    return out


def _place_name(conn: sqlite3.Connection, place_id: str | None, place_text: str | None,
                place_cache: dict[str, str]) -> str | None:
    """Resolve a claim's place to a display string (place_text preferred)."""
    if place_text:
        return _escape(place_text)
    if place_id:
        if place_id not in place_cache:
            row = conn.execute(
                'SELECT hierarchy, name FROM places WHERE id = ?', (place_id,)
            ).fetchone()
            place_cache[place_id] = (
                _escape(row['hierarchy'] or row['name']) if row else ''
            )
        return place_cache[place_id] or None
    return None


# ── Emission ─────────────────────────────────────────────────────────────────────

def _emit_header() -> list[str]:
    return [
        '0 HEAD',
        '1 SOUR fha',
        '2 NAME family history archive (fha)',
        '1 GEDC',
        f'2 VERS {_GEDCOM_VERSION}',
        '2 FORM LINEAGE-LINKED',
        '1 CHAR UTF-8',
        '1 NOTE Generated by fha gedcom - do not re-import as truth. GEDCOM is a',
        '2 CONT one-way export bridge; the plain-file archive remains the source of record.',
    ]


def _is_redacted(person: sqlite3.Row, include_living: bool,
                 restricted_persons: set[str] = frozenset()) -> bool:
    """A person's facts are withheld when living-redacted or restricted.

    Living redaction is lifted by --include-living; a `restricted` person is
    withheld with NO override (SPEC §21) - GEDCOM is a public path. Either way
    the structural family links survive so the tree shape is intact."""
    if person['id'] in restricted_persons:
        return True
    return (not include_living) and person['living'] in ('true', 'unknown')


def _redacted_name(person: sqlite3.Row, restricted_persons: set[str]) -> str:
    """The NAME line for a withheld person: `/Restricted/` for a restricted
    person, `/Living/` for a living-redacted one (à la standard GEDCOM privacy)."""
    return '/Restricted/' if person['id'] in restricted_persons else '/Living/'


def _emit_event(tag: str, claim: sqlite3.Row | None, conn: sqlite3.Connection,
                src_xref: dict[str, str], place_cache: dict[str, str]) -> list[str]:
    if claim is None:
        return []
    date = _edtf_to_gedcom(claim['date_edtf'])
    place = _place_name(conn, claim['place_id'], claim['place_text'], place_cache)
    src = claim['source_id']
    if not (date or place or src):
        return []
    lines = [f'1 {tag}']
    if date:
        lines.append(f'2 DATE {date}')
    if place:
        lines.append(f'2 PLAC {place}')
    if src and src in src_xref:
        lines.append(f'2 SOUR @{src_xref[src]}@')
    return lines


def _emit_individual(
    person: sqlite3.Row, xref: str, conn: sqlite3.Connection,
    vitals: dict[tuple[str, str], sqlite3.Row],
    person_fams: dict[str, set[frozenset[str]]],
    child_fam: dict[str, frozenset[str]],
    fam_xref: dict[frozenset[str], str],
    src_xref: dict[str, str], place_cache: dict[str, str],
    include_living: bool, restricted_persons: set[str] = frozenset(),
) -> list[str]:
    pid = person['id']
    redacted = _is_redacted(person, include_living, restricted_persons)
    lines = [f'0 @{xref}@ INDI']

    if redacted:
        lines.append(f'1 NAME {_redacted_name(person, restricted_persons)}')
    else:
        lines.append(f'1 NAME {_gedcom_name(person["name"], person["surname"])}')

    sex = (person['sex'] or '').strip().upper()
    if sex in ('M', 'F'):
        lines.append(f'1 SEX {sex}')

    if not redacted:
        lines += _emit_event('BIRT', vitals.get((pid, 'birth')), conn, src_xref, place_cache)
        lines += _emit_event('DEAT', vitals.get((pid, 'death')), conn, src_xref, place_cache)

    # FAMS (as spouse/parent), FAMC (as child)
    for key in sorted(person_fams.get(pid, set()), key=lambda k: fam_xref[k]):
        lines.append(f'1 FAMS @{fam_xref[key]}@')
    if pid in child_fam:
        lines.append(f'1 FAMC @{fam_xref[child_fam[pid]]}@')

    if not redacted:
        lines.append(f'1 REFN {fmt_id_display(pid)}')
    return lines


def _emit_family(
    key: frozenset[str], children: set[str], xref: str,
    persons: dict[str, sqlite3.Row], person_xref: dict[str, str],
    marriages: dict[frozenset[str], sqlite3.Row], conn: sqlite3.Connection,
    src_xref: dict[str, str], place_cache: dict[str, str], include_living: bool,
    restricted_persons: set[str] = frozenset(),
) -> list[str]:
    lines = [f'0 @{xref}@ FAM']

    spouses = sorted(key)
    husband = next((p for p in spouses if (persons[p]['sex'] or '').upper() == 'M'), None)
    wife = next((p for p in spouses if (persons[p]['sex'] or '').upper() == 'F'), None)
    assigned = {p for p in (husband, wife) if p}
    # Unknown/same-sex: fill remaining slots deterministically.
    for p in spouses:
        if p in assigned:
            continue
        if husband is None:
            husband = p
        elif wife is None:
            wife = p
        assigned.add(p)

    if husband:
        lines.append(f'1 HUSB @{person_xref[husband]}@')
    if wife:
        lines.append(f'1 WIFE @{person_xref[wife]}@')

    couple_redacted = any(_is_redacted(persons[p], include_living, restricted_persons) for p in key)
    marriage = marriages.get(key)
    if marriage is not None and not couple_redacted:
        date = _edtf_to_gedcom(marriage['date_edtf'])
        place = _place_name(conn, marriage['place_id'], marriage['place_text'], place_cache)
        src = marriage['source_id']
        lines.append('1 MARR')
        if date:
            lines.append(f'2 DATE {date}')
        if place:
            lines.append(f'2 PLAC {place}')
        if src and src in src_xref:
            lines.append(f'2 SOUR @{src_xref[src]}@')

    for child in sorted(children, key=lambda c: person_xref[c]):
        lines.append(f'1 CHIL @{person_xref[child]}@')
    return lines


def _emit_source(sid: str, title: str, xref: str) -> list[str]:
    lines = [f'0 @{xref}@ SOUR']
    lines.append(f'1 TITL {_escape(title) or fmt_id_display(sid)}')
    lines.append(f'1 REFN {fmt_id_display(sid)}')
    return lines


# ── Core ──────────────────────────────────────────────────────────────────────

def _gedcom_payload(
    archive_root: Path,
    pid: str | None,
    *,
    mode: str = 'descendants',
    generations: int | None = None,
    all_persons: bool = False,
    include_living: bool = False,
) -> dict:
    """
    Build a GEDCOM 5.5.1 export. Returns:
      {'status': 'ok'|'not-found'|'no-index'|'no-persons'|'bad-args',
       'text': str|None, 'messages': [str, ...],
       'person_count': int, 'family_count': int}
    """
    messages: list[str] = []

    if not all_persons:
        if not pid:
            return {'status': 'bad-args', 'text': None,
                    'messages': ['ERROR: a P-id argument or --all is required.'],
                    'person_count': 0, 'family_count': 0}
        if not is_valid_id(pid) or id_type_of(pid) != 'P':
            return {'status': 'bad-args', 'text': None,
                    'messages': [f'ERROR: {pid!r} is not a valid P-id.'],
                    'person_count': 0, 'family_count': 0}

    conn = open_index_db(archive_root, _REQUIRED_TABLES, strict=True)
    if conn is None:
        return {'status': 'no-index', 'text': None, 'messages': messages,
                'person_count': 0, 'family_count': 0}

    try:
        persons = _load_persons(conn)

        if not all_persons and pid not in persons:
            return {'status': 'not-found', 'text': None, 'messages': messages,
                    'person_count': 0, 'family_count': 0}

        # Restricted persons (any value, incl. by-request) and restricted-by-
        # free-text-type sources are read from the record files: the index
        # carries no person-level `restricted`, and stores a free-text source
        # type as 0. The SQL `_public_source_filter_sql` already excludes index
        # restricted=1 / DNA / publication_ok=0; this catches what it can't see.
        restricted_persons = _restricted_person_ids(archive_root, persons)
        restricted_sources = _restricted_source_ids(conn, archive_root)
        restricted_claims = _restricted_claim_ids(conn, archive_root)

        def _public_claim_row(row: sqlite3.Row) -> bool:
            """A vital/marriage row not withheld by source or per-claim restriction."""
            if normalize_id(str(row['id'])) in restricted_claims:
                return False
            return not row['source_id'] or row['source_id'] not in restricted_sources

        # One load of the relationship graph serves both traversal and family
        # building (it was previously read twice for the non-`--all` path).
        # A relationship backed only by a free-text-restricted source is dropped
        # here (the SQL filter caught the index-restricted/DNA ones already).
        rel = _RelIndex([
            r for r in conn.execute(
                f"""SELECT r.person_id, r.rel, r.other_id, r.claim_id, c.source_id
                   FROM relationships r
                   LEFT JOIN claims c ON c.id = r.claim_id
                   LEFT JOIN sources s ON s.id = c.source_id
                   WHERE r.claim_id IS NULL OR {_public_source_filter_sql()}"""
            ).fetchall()
            if (not r['source_id'] or r['source_id'] not in restricted_sources)
            and (r['claim_id'] is None or normalize_id(str(r['claim_id'])) not in restricted_claims)
        ])

        if all_persons:
            included = set(persons.keys())
        else:
            included = _traverse(rel, pid, mode, generations)

        # Keep only real (non-merged) person rows.
        included = {p for p in included if p in persons}
        if not included:
            return {'status': 'no-persons', 'text': None, 'messages': messages,
                    'person_count': 0, 'family_count': 0}

        # Families are computed from the *included* set only, over the full
        # relationship graph (a connected/all run already has every edge; a
        # depth-capped descendants/ancestors run intentionally excludes edges
        # leaving the set).
        families, person_fams, child_fam = _build_families(rel, included)

        # Stable xrefs: persons by id, families by parent-key, sources by id.
        person_xref = {pid_: f'I{i}' for i, pid_ in enumerate(sorted(included), start=1)}
        fam_xref = {key: f'F{i}' for i, (key, _ch) in enumerate(families, start=1)}

        # A free-text-restricted source is not an eligible fact source, so its
        # vital/marriage rows are dropped before emission (the SQL filter in the
        # loaders already removed index-restricted/DNA/publication_ok=0 ones).
        vitals = _load_vitals(conn, included, _public_claim_row)
        marriages = _load_marriages(conn, _public_claim_row)

        # Collect the sources actually cited by an emitted, non-redacted fact.
        used_sources: set[str] = set()
        for (vp, _t), claim in vitals.items():
            if (vp in included and claim['source_id']
                    and not _is_redacted(persons[vp], include_living, restricted_persons)):
                used_sources.add(claim['source_id'])
        for key, _children in families:
            if any(_is_redacted(persons[p], include_living, restricted_persons) for p in key):
                continue
            m = marriages.get(key)
            if m is not None and m['source_id']:
                used_sources.add(m['source_id'])

        src_titles = {}
        if used_sources:
            placeholders = ','.join('?' * len(used_sources))
            for row in conn.execute(
                f'SELECT id, title FROM sources WHERE id IN ({placeholders})',
                list(used_sources),
            ).fetchall():
                src_titles[row['id']] = row['title']
        src_xref = {sid: f'S{i}' for i, sid in enumerate(sorted(used_sources), start=1)}

        place_cache: dict[str, str] = {}
        lines: list[str] = _emit_header()

        for pid_ in sorted(included):
            lines += _emit_individual(
                persons[pid_], person_xref[pid_], conn, vitals, person_fams,
                child_fam, fam_xref, src_xref, place_cache, include_living,
                restricted_persons,
            )
        for key, children in families:
            lines += _emit_family(
                key, children, fam_xref[key], persons, person_xref, marriages,
                conn, src_xref, place_cache, include_living, restricted_persons,
            )
        for sid in sorted(used_sources):
            lines += _emit_source(sid, src_titles.get(sid, ''), src_xref[sid])

        lines.append('0 TRLR')

        living_redacted = sum(
            1 for p in included
            if p not in restricted_persons and not include_living
            and persons[p]['living'] in ('true', 'unknown')
        )
        if living_redacted:
            messages.append(
                f'{living_redacted} living/unknown person(s) redacted as /Living/ '
                '(pass --include-living to export their details).'
            )
        restricted_redacted = sum(1 for p in included if p in restricted_persons)
        if restricted_redacted:
            messages.append(
                f'{restricted_redacted} restricted person(s) redacted as /Restricted/ '
                '(no override - SPEC §21).'
            )

        # GEDCOM 5.5.1 lines are CR/LF-terminated; emit the file with a trailing newline.
        text = '\r\n'.join(lines) + '\r\n'
        return {'status': 'ok', 'text': text, 'messages': messages,
                'person_count': len(included), 'family_count': len(families)}
    finally:
        conn.close()


def run_gedcom(
    archive_root: Path,
    pid: str | None,
    *,
    mode: str = 'descendants',
    generations: int | None = None,
    all_persons: bool = False,
    include_living: bool = False,
) -> Result:
    """Build a GEDCOM 5.5.1 export and return a Result.

    `data` is the {'status', 'text', 'messages', 'person_count', 'family_count'}
    payload `_gedcom_payload` computes; Result exposes dict-style access
    (_lib.py), so callers keep reading `result['text']` unchanged.  Producing the
    GEDCOM text is pure (the `_cmd` layer prints/writes it), so `changed` is empty.
    """
    payload = _gedcom_payload(
        archive_root, pid, mode=mode, generations=generations,
        all_persons=all_persons, include_living=include_living,
    )
    status = payload['status']
    # Mirror _cmd_gedcom's per-status exit codes so headless callers returning
    # Result.exit_code see a failed export as a failure, not a clean 0.
    if status == 'ok':
        exit_code = EXIT_CLEAN
    elif status in ('not-found', 'no-persons'):
        exit_code = EXIT_WARNINGS
    else:  # bad-args, no-index
        exit_code = EXIT_FAILURE
    return Result(ok=(status == 'ok'), exit_code=exit_code, data=payload)


# ── CLI ──────────────────────────────────────────────────────────────────────────

def _cmd_gedcom(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE

    all_persons = getattr(args, 'all', False)
    pid = normalize_id(getattr(args, 'person_id', '') or '')
    if not pid and not all_persons:
        print('ERROR: a P-id argument or --all is required.', file=sys.stderr)
        return EXIT_FAILURE

    generations = getattr(args, 'generations', None)
    if generations is not None and generations < 1:
        print('ERROR: --generations must be a positive integer.', file=sys.stderr)
        return EXIT_FAILURE

    result = run_gedcom(
        archive_root, pid or None,
        mode=getattr(args, 'mode', 'descendants'),
        generations=generations,
        all_persons=all_persons,
        include_living=getattr(args, 'include_living', False),
    )

    for m in result['messages']:
        print(m, file=sys.stderr)

    status = result['status']
    if status == 'bad-args':
        return EXIT_FAILURE
    if status == 'no-index':
        return EXIT_FAILURE
    if status == 'not-found':
        print(f'{fmt_id_display(pid)}: not found in index.', file=sys.stderr)
        return EXIT_WARNINGS
    if status == 'no-persons':
        print('No persons selected for export.', file=sys.stderr)
        return EXIT_WARNINGS

    out = getattr(args, 'out', None)
    if out:
        out_path = Path(out)
        if not out_path.is_absolute():
            out_path = Path.cwd() / out_path
        try:
            out_path.write_text(result['text'], encoding='utf-8', newline='')
        except OSError as e:
            print(f'ERROR: could not write {out_path}: {e}', file=sys.stderr)
            return EXIT_FAILURE
        print(
            f'GEDCOM written: {out_path} '
            f'({result["person_count"]} individuals, {result["family_count"]} families)'
        )
    else:
        sys.stdout.write(result['text'])

    # A successful export is clean even when it redacted living persons -
    # redaction is the documented default, an informational stderr note, not a
    # warning condition (cf. `--include-living` to suppress it).
    return EXIT_CLEAN


def _add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument('person_id', metavar='P-id', nargs='?',
                   help='Seed person for traversal (omit with --all).')
    p.add_argument('--mode', choices=('descendants', 'ancestors', 'connected'),
                   default='descendants',
                   help='Traversal from the seed (default: descendants).')
    p.add_argument('--generations', type=int, metavar='N',
                   help='Depth cap for descendants/ancestors (default: unlimited).')
    p.add_argument('--all', action='store_true',
                   help='Export every person in the index (ignores --mode).')
    p.add_argument('--include-living', action='store_true', dest='include_living',
                   help='Export living/unknown persons in full (default: redacted).')
    p.add_argument('--out', metavar='FILE', help='Write to FILE (default: stdout).')


# User-facing --help text (the module docstring stays developer-facing).
_CLI_DESCRIPTION = """\
Export your tree to a GEDCOM file for another genealogy app.

  fha gedcom --all                        Export everyone
  fha gedcom <P-id> --mode descendants    Export from one person, down the line
  fha gedcom <P-id> --mode ancestors      Export from one person, up the line
  fha gedcom --all --out family.ged       Choose the output file

Living people are redacted by default (use --include-living to override). GEDCOM
is the portable format Ancestry, RootsMagic, and others read; it is a one-way
export, never re-imported as truth."""


def register(subs: argparse._SubParsersAction) -> argparse.ArgumentParser:
    p = subs.add_parser(
        'gedcom',
        help='Derive a GEDCOM 5.5.1 export from relationships + accepted vitals.',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(p)
    p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    p.add_argument('--spec-root', metavar='PATH', help='Spec docs root (accepted for CLI consistency).')
    p.set_defaults(func=_cmd_gedcom)
    return p


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha gedcom', description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    parser.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    parser.add_argument('--spec-root', metavar='PATH', help='Spec docs root (accepted for CLI consistency).')
    parser.set_defaults(func=_cmd_gedcom)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
