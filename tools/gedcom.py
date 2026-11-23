#!/usr/bin/env python3
"""
gedcom.py - fha gedcom: derive a GEDCOM 5.5.1 exchange file (TOOLING §13a).

  fha gedcom [<P-id>] [--mode descendants|ancestors|connected]
             [--generations N] [--all] [--include-living] [--basic]
             [--out FILE] [--root PATH]

GEDCOM is a one-way bridge to other genealogy applications. It is *derived* at
export time from the index's `relationships` edges and accepted vital claims -
never stored in the archive, never re-imported as truth (the archive is never
GEDCOM's corpus - GEDCOM is a one-way bridge to other apps). The header carries an explicit
"do not re-import as truth" note to make that contract travel with the file.

Importing a GEDCOM from Ancestry or another program? That is the separate
`fha gedcom import <file.ged>` (TOOLING §13a2) - this command is the exporter.

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

A person or source whose record file is present but whose own `restricted:`
marker cannot be read (not UTF-8, no frontmatter block, frontmatter that is not
valid YAML or not a block of fields - `_lib.read_restriction_marker`) is
withheld as if it were restricted (fail closed, #267), under every flag; the
export names each such file that touches it, says to run `fha lint`, and exits
1. A source whose claims block alone is broken keeps its record and loses only
its claims, whose per-claim markers cannot be read.

SOURCES
-------
Every exported vital fact's `source_id` becomes a `SOUR` pointer to a top-level
`SOUR` record (TITL = source title, REFN = the S-id), so the citation trail
follows the facts into the other application.

BASIC EXPORT (--basic)
----------------------
A DNA site (MyHeritage, Ancestry DNA and the like) reads a tree only to compare
matches: it needs names, sex, the life events with their dates and places, and
the family links. Everything else in a full export is extra that leaves the
archive for nothing, and some of it is a quiet leak: a source title such as an
interview or a cousin's letter can name a living relative whose own record is
redacted to "Living". `--basic` writes the same people and families with the
same redaction, and leaves out every source record, every citation pointer
under an event, and every REFN archive id. The exporter writes no NOTE, OBJE or
_UID outside the header in either mode, so nothing else needs dropping. The
records are never written in the first place (not filtered out of the text
afterwards), so no pointer can be left aimed at a record that is not there.
A birth or death with neither a date nor a place says nothing a match can
use, so a basic export leaves it out rather than writing a bare `1 BIRT`. A
marriage is kept even then: `1 MARR` alone still tells a match the two married.

CODE MAP
--------
  Date / name formatting
    _edtf_to_gedcom        - EDTF date string → GEDCOM 5.5.1 date phrase
    _gedcom_name           - (name, surname) → (GEDCOM `Given /Surname/ [Suffix]`, suffix)
    _escape                - collapse newlines for a single GEDCOM line value
    _name_text             - drop `/` (the NAME line's surname delimiter) from name text

  Graph
    _RelIndex              - adjacency over the relationships table
    _traverse              - descendants/ancestors/connected person selection
    _build_families        - couples + children → family records keyed by parent set

  Data gathering
    _load_persons, _load_vitals, _load_marriages
    (privacy)              - `_lib.export_privacy` reads what the marker files
                             withhold (fail closed, #267), `_lib.public_source_sql`
                             the index's own source filter: one reader for every
                             exporter that is not the site

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
    export_privacy,
    public_source_sql,
    recorded_surname,
    living_carried_to_survivors,
    fold_accents,
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    Result,
    claim_is_own_vital,
    claim_role_pairs,
    configure_utf8_stdout,
    fmt_id_display,
    id_type_of,
    is_placeholder_name,
    is_valid_id,
    normalize_id,
    open_index_db,
    read_record,
    read_restriction_marker,
    resolve_root_arg,
    spouse_parties,
    strip_generational_suffix,
    vital_pick_key,
)

configure_utf8_stdout()


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

    # Its mirror, [1920..] (#206) - a GEDCOM "after" date.
    after = re.match(r'^\[(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?\.{2}\]$', s)
    if after:
        y, m, d = after.group(1), after.group(2), after.group(3)
        tail = y
        if m and 1 <= int(m) <= 12:
            tail = f'{_MONTHS[int(m) - 1]} {y}'
            if d:
                tail = f'{int(d)} {tail}'
        return f'AFT {tail}'

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
            # An OPEN side stays open (#218 audit): `1910/[1920..]` runs from
            # 1910 to no known end, so it is AFT 1910 - a BET 1910 AND 1920
            # would hand another program a ten-year window the evidence never
            # set. Likewise `[..1900]/1910` is BEF 1910, and open at both
            # ends says nothing a GEDCOM date can, so no date is exported.
            start_open = ga.startswith('BEF ')
            end_open = gb.startswith('AFT ')
            if start_open and end_open:
                return None
            # Otherwise strip the qualifiers inside the range - BET..AND
            # already conveys the span's fuzziness.
            ga_core = ga.split(' ', 1)[-1] if ga.startswith(('ABT ', 'BEF ', 'AFT ')) else ga
            gb_core = gb.split(' ', 1)[-1] if gb.startswith(('ABT ', 'BEF ', 'AFT ')) else gb
            if start_open:
                return f'BEF {gb_core}'
            if end_open:
                return f'AFT {ga_core}'
            return f'BET {ga_core} AND {gb_core}'
        return ga or gb
    return _one_edtf_to_gedcom(edtf)


def _escape(value: str | None) -> str:
    """Collapse a value to a single GEDCOM line (newlines/tabs → spaces)."""
    if not value:
        return ''
    return ' '.join(str(value).split())


def _name_text(value: str) -> str:
    """Strip GEDCOM's surname delimiter out of free NAME text.

    `1 NAME` is the one line in 5.5.1 where `/` is grammar rather than
    text: it delimits the surname, and NAME_TEXT excludes it for exactly
    that reason. A slash anywhere in a person's `name:` field (or in the
    indexed surname) therefore redraws the field. `Roy A/B Dodson` with an
    indexed "Dodson" wrote `1 NAME Roy A/B /Dodson/`, which any reader -
    this repo's own `fha gedcom import` included - reads back with the
    surname "B": the same defect as issue #78, arriving by a different
    road. Slashes become spaces rather than vanishing ("Anne/Anna" is two
    readings of one name, not "AnneAnna"), and the result is re-collapsed
    so the substitution cannot leave a double space behind.
    """
    return ' '.join(value.replace('/', ' ').split())


def _same_word(a: str, b: str) -> bool:
    """Whether two spellings are one word of a name: case, accents and a
    trailing comma or period aside ("Wiśniewska" / "Wisniewska")."""
    return fold_accents(a.rstrip('.,')).lower() == fold_accents(b.rstrip('.,')).lower()


def _gedcom_name(name: str, surname: str | None, *,
                 no_surname_recorded: bool = False) -> tuple[str, str | None]:
    """Render a person name as GEDCOM `Given /Surname/` (+ NSFX suffix).

    `surname` is the index's `persons.surname`, which `fha index` reads by
    the one rule (`_lib.person_surname`, SPEC §13): the record's `surname:`
    line when it has one, else the last word of `name:` once a generational
    or regnal suffix is set aside. It is never read from the file name, so
    there is no slug spelling to undo here ("Winiewska", "Vii") - only three
    cases:

      - None (no usable name, or an old index): split `name` the same way.
      - '' : no surname is read from the name. "Not known" and "none" stay
        different (SPEC §13), and in GEDCOM an empty slash field (`Cher //`)
        says "no surname". So a one-word name with nothing recorded (the
        surname is just not known) is written with no slashes at all
        (`1 NAME Cher`, `1 NAME Henry VII`), and the empty field is written
        only for a recorded `surname: ""` (`no_surname_recorded`: `Cher //`,
        `Jon Thorsson //`, `Henry // VII`). A name of two words or more
        reads '' only from a recorded `surname: ""`.
      - a surname: matched as a trailing word run of the name, first with
        the suffix still on (a `surname: XI` on "Jinping XI" says XI IS the
        surname, so `Jinping /XI/`), then without it (`Roy /Dodson/ Jr`).
        The slash field carries the name's own spelling of those words. A
        surname the name does not end with (an override such as a birth
        surname) keeps the full name as the given part rather than
        guessing which words to drop.

    A trailing generational suffix (Jr, Sr, II-V, Esq, regnal VI-XXX -
    `_lib.strip_generational_suffix`) is never put in the slash field:
    GEDCOM's `1 NAME` line carries it as trailing free text and the caller
    also emits `2 NSFX <suffix>` (issue #78). Returns `(name_field, suffix)`,
    `suffix` as written in `name` or None.

    Both inputs run through `_name_text` first, so a `/` in either can never
    redraw the slash field. A placeholder (`_lib.is_placeholder_name`:
    "unknown", a bare `name:`, "?") is not a name on either side: a person
    nobody has named exports as `1 NAME Unknown //`, never `/Unknown/`.
    """
    name = _name_text(_escape(name))
    if is_placeholder_name(name):
        name = ''
    parts = (name or 'Unknown').split()
    core, suffix = strip_generational_suffix(parts)
    raw_suffix = parts[-1] if suffix else None

    def compose(given: str, sn_field: str, tail: str | None) -> str:
        field = f'{given} /{sn_field}/'.strip()
        return f'{field} {tail}' if tail else field

    sn = _name_text(_escape(surname)) if surname else ''
    def bare(given: str, tail: str | None) -> str:
        # No slash field: the surname is not known, not absent.
        return f'{given} {tail}' if tail else given

    if surname is None or (surname and is_placeholder_name(sn)):
        # No surname read, or a placeholder standing in for one ("Unknown"
        # from an old index): split the name itself.
        if len(core) <= 1:
            if not name:
                return compose(core[0], '', raw_suffix), raw_suffix
            return bare(core[0], raw_suffix), raw_suffix
        return compose(' '.join(core[:-1]), core[-1], raw_suffix), raw_suffix
    if not sn:
        if len(core) <= 1 and not no_surname_recorded:
            return bare(' '.join(core), raw_suffix), raw_suffix
        return compose(' '.join(core), '', raw_suffix), raw_suffix

    sn_tokens = sn.split()
    n = len(sn_tokens)
    if raw_suffix and len(parts) > n and all(
            _same_word(p, t) for p, t in zip(parts[-n:], sn_tokens)):
        # The recorded surname is the word the suffix rule took.
        return compose(' '.join(parts[:-n]), ' '.join(parts[-n:]), None), None
    # `>=`: a name that is only its surname (`/Dodson/` imported) has an
    # empty given part, not the surname twice.
    if len(core) >= n and all(_same_word(p, t) for p, t in zip(core[-n:], sn_tokens)):
        return compose(' '.join(core[:-n]), ' '.join(core[-n:]), raw_suffix), raw_suffix
    return compose(' '.join(core), sn, raw_suffix), raw_suffix


def _recorded_no_surname_ids(archive_root: Path, rows: list) -> set[str]:
    """The people the index reads no surname for whose record says so with
    `surname: ""` (SPEC §13). The index stores '' for both a one-word name
    (surname not known) and a recorded "no surname", so only the record can
    tell them apart, and only those rows are read. A record that cannot be
    read counts as "not known", which writes no slashes."""
    out: set[str] = set()
    for row in rows:
        if row['surname'] != '' or not row['path']:
            continue
        try:
            meta = read_record(archive_root / row['path'])['meta']
        except Exception:  # noqa: BLE001 (unreadable: read as "not known")
            continue
        if recorded_surname(meta) == '':
            out.add(row['id'])
    return out


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


def _load_vitals(
    conn: sqlite3.Connection, pids: set[str],
    is_public: 'Callable[[sqlite3.Row], bool] | None' = None,
) -> dict[tuple[str, str], sqlite3.Row]:
    """The best public-safe accepted birth/death claim per (person_id, type).

    "Best" is `_lib.vital_pick_key` (#201): the most exact date, then
    confidence, then claim id - the same pick the site and `fha views` make,
    so BIRT/DEAT match the dates the family sees on the site. The old rule
    here, earliest `date_min`, systematically exported the vaguest date,
    because an "about 1940" range starts before the certificate's
    19 Jan 1941.

    Filtering joins through `sources` here rather than later during emission so
    a restricted/DNA fact cannot leak as an event with the source pointer merely
    omitted. `is_public`, when given, also rejects free-text-restricted and
    per-claim-restricted rows BEFORE the per-key pick, so a better-ranked
    restricted claim cannot evict (and thereby suppress) a publishable one.

    The claim must also be a record OF that person, not merely one that names
    them (`_lib.claim_is_own_vital`, #126). A birth certificate names the baby, both
    parents and the informant; a death certificate names the deceased and the
    relative who reported it. Keyed on `person_id` alone this wrote the son's
    1888 BIRT onto his mother's INDI record - the same defect
    `_spouse_persons_for_claim` fixes for MARR, one query along, and with the
    same consequence: a fact about the wrong person, exported as truth into
    whatever program reads the file. A claim naming at most one person with
    no `roles:` map at all has not said, and keeps the behaviour it always
    had (nobody else to be ambiguous about); a claim naming two or more
    people with no `roles:` map at all has not said which of them it is
    about, and `vital_subjects` scopes it to none of them (#126, reopened) -
    it exports no BIRT/DEAT for anyone it names.

    The scoping test runs BEFORE the per-key pick, for the reason `is_public`
    does: a relative's better-ranked record would otherwise take the slot and
    suppress the person's own.
    """
    if not pids:
        return {}
    placeholders = ','.join('?' * len(pids))
    rows = conn.execute(
        f"""
        SELECT cp.person_id, c.id, c.type, c.date_edtf, c.date_min, c.date_max, c.confidence,
               c.place_id, c.place_text, c.source_id
        FROM claim_persons cp
        JOIN claims c ON cp.claim_id = c.id
        JOIN sources s ON s.id = c.source_id
        WHERE cp.person_id IN ({placeholders})
          AND c.type IN ('birth', 'death')
          AND c.status = 'accepted'
          AND COALESCE(c.negated, 0) = 0
          AND {public_source_sql()}
        """,
        list(pids),
    ).fetchall()
    subjects: dict[str, list[str] | None] = {}
    out: dict[tuple[str, str], sqlite3.Row] = {}
    for r in sorted(rows, key=vital_pick_key):
        if is_public is not None and not is_public(r):
            continue
        if not claim_is_own_vital(conn, r['person_id'], r['id'], r['type'], subjects):
            continue
        out.setdefault((r['person_id'], r['type']), r)
    return out


def _spouse_persons_for_claim(conn: sqlite3.Connection, claim_id: str) -> frozenset[str]:
    """Return the people a marriage claim says married EACH OTHER.

    This decides which FAM record the marriage event is written onto, so it has
    to answer the question exactly as the index does - `_lib.spouse_parties`,
    the one shared rule (SPEC §8.3, TOOLING §197): `roles: spouse:` when it
    names two or more people, else the two people named when there are exactly
    two, else nobody.

    The old fallback here was "the first two persons by position", and that is
    the bug this replaces. A marriage certificate names the couple AND both
    sets of parents; on a printed form the parents are often transcribed first.
    Taking the first two then hung a son's wedding date and place on his
    parents' family record - a fact about the wrong marriage, exported as truth
    into whatever program reads the file, and contradicting the index, which
    (correctly) derives no couple from a claim that never says who the couple
    were. A missing marriage event is recoverable; a wrong one is not.

    A claim whose roles: map names three or more spouses (successive marriages
    recorded on one claim) yields a party set larger than a couple. No FAM can
    hold it, so `_load_marriages` finds no home for that event rather than
    picking two of the three - the same refusal to guess. The export says so
    out loud (`_gedcom_payload` reports every marriage event it could not
    place, beside the redaction counts): refusing to guess is right, but
    dropping a wedding date out of a complete-looking file in silence is not.
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
    return frozenset(spouse_parties(claim_role_pairs((r['person_id'], r['role']) for r in rows)))


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
          AND COALESCE(c.negated, 0) = 0
          AND {public_source_sql()}
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
    # A basic export passes no source xrefs, so a source alone does not make
    # an event worth writing there (module docstring, BASIC EXPORT).
    if not (date or place or (src and src in src_xref)):
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
    with_ids: bool = True,
    no_surname_ids: set[str] = frozenset(),
) -> list[str]:
    """One INDI record. `with_ids` False (a `--basic` export) leaves out the
    REFN line, the archive id another program would otherwise keep.
    `no_surname_ids` are the people whose record says `surname: ""`
    (`_gedcom_name`)."""
    pid = person['id']
    redacted = _is_redacted(person, include_living, restricted_persons)
    lines = [f'0 @{xref}@ INDI']

    if redacted:
        lines.append(f'1 NAME {_redacted_name(person, restricted_persons)}')
    else:
        name_field, suffix = _gedcom_name(person['name'], person['surname'],
                                          no_surname_recorded=pid in no_surname_ids)
        lines.append(f'1 NAME {name_field}')
        if suffix:
            lines.append(f'2 NSFX {suffix}')

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

    if with_ids and not redacted:
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
    basic: bool = False,
) -> dict:
    """
    Build a GEDCOM 5.5.1 export. `basic` writes no source records, citations
    or archive ids (module docstring, BASIC EXPORT). Returns:
      {'status': 'ok'|'not-found'|'no-index'|'no-persons'|'bad-args',
       'text': str|None, 'messages': [str, ...],
       'person_count': int, 'family_count': int}
    """
    messages: list[str] = []

    # A seed P-id and --all name two different exports (from that person, vs
    # everyone), so the combination is ambiguous - reject it rather than
    # silently letting --all win and dropping the seed the user typed.
    if all_persons and pid:
        return {'status': 'bad-args', 'text': None,
                'messages': ['ERROR: a P-id and --all conflict - drop one. '
                             'Pass <P-id> to export from that person, or --all to '
                             'export everyone.'],
                'person_count': 0, 'family_count': 0}

    if not all_persons:
        if not pid:
            return {'status': 'bad-args', 'text': None,
                    'messages': ['ERROR: a P-id argument or --all is required.'],
                    'person_count': 0, 'family_count': 0}
        if not is_valid_id(pid) or id_type_of(pid) != 'P':
            return {'status': 'bad-args', 'text': None,
                    'messages': [f'ERROR: {pid!r} is not a valid p-id. p-ids look like p-2b3c4d5e6f.'],
                    'person_count': 0, 'family_count': 0}

    conn = open_index_db(archive_root, _REQUIRED_TABLES, strict=True)
    if conn is None:
        return {'status': 'no-index', 'text': None, 'messages': messages,
                'person_count': 0, 'family_count': 0}

    try:
        persons = _load_persons(conn)
        # A tombstone that says `living: true` hands its claims to the
        # survivor (SPEC §9), so the survivor is held as living too (#304:
        # only an explicit true carries): every `living` check below,
        # redaction and --include-living alike, then reads the merged truth.
        _rank = {'false': 0, 'unknown': 1, 'true': 2}
        for surv, carried in living_carried_to_survivors(conn).items():
            row = persons.get(surv)
            if row is not None and _rank.get(carried, 1) > _rank.get(
                    str(row['living'] or 'unknown').lower(), 1):
                persons[surv] = {**dict(row), 'living': carried}

        if not all_persons and pid not in persons:
            return {'status': 'not-found', 'text': None, 'messages': messages,
                    'person_count': 0, 'family_count': 0}

        # This GEDCOM file and a printed report for a relative agree about who
        # is private, because both read it once through `_lib.export_privacy`:
        # restricted persons (any value, incl. by-request, and the survivors a
        # restricted merged record speaks through, #304 review), sources
        # restricted by a free-text type the index stores as 0, per-claim
        # markers, and every file that would not read (fail closed, #267). The
        # SQL `public_source_sql` already excludes index restricted=1 / DNA /
        # publication_ok=0; this catches what it can't see.
        privacy = export_privacy(conn, archive_root)
        restricted_persons = privacy.restricted_persons
        restricted_sources = privacy.restricted_sources
        restricted_claims = privacy.restricted_claims
        unreadable_persons = privacy.unreadable_persons
        unreadable_sources = privacy.unreadable_sources   # (source id, path)

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
                   WHERE r.claim_id IS NULL OR {public_source_sql()}"""
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
        # A basic export cites none, so with no source xrefs no SOUR record and
        # no SOUR pointer is ever written.
        used_sources: set[str] = set()
        for (vp, _t), claim in ([] if basic else vitals.items()):
            if (vp in included and claim['source_id']
                    and not _is_redacted(persons[vp], include_living, restricted_persons)):
                used_sources.add(claim['source_id'])
        for key, _children in ([] if basic else families):
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
        no_surname_ids = _recorded_no_surname_ids(
            archive_root, [persons[p] for p in included])

        for pid_ in sorted(included):
            lines += _emit_individual(
                persons[pid_], person_xref[pid_], conn, vitals, person_fams,
                child_fam, fam_xref, src_xref, place_cache, include_living,
                restricted_persons, with_ids=not basic, no_surname_ids=no_surname_ids,
            )
        for key, children in families:
            lines += _emit_family(
                key, children, fam_xref[key], persons, person_xref, marriages,
                conn, src_xref, place_cache, include_living, restricted_persons,
            )
        for sid in sorted(used_sources):
            lines += _emit_source(sid, src_titles.get(sid, ''), src_xref[sid])

        lines.append('0 TRLR')

        if basic:
            messages.append(
                'Basic export: names, sex, birth, death and marriage dates and places, '
                'and the family links only. No sources, citations or archive IDs '
                'were written.'
            )
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

        # Withheld because a file could not be read (#267): say which file and
        # how to fix it, or the human reads "/Restricted/" on a relative who
        # never asked for it and has no way to learn why. Only the files that
        # touch this export are named - a person outside the selection, or a
        # source whose claims name nobody in it, is scoping, not something
        # this file lost. A source touches the export when any of its claims
        # names an included person: those are the facts it could have carried.
        included_paths = {persons[p]['path'] for p in included}
        unread = [path for path in unreadable_persons if path in included_paths]
        if unreadable_sources:
            ids = sorted(included)
            touching: set[str] = set()
            for start in range(0, len(ids), 500):   # stay under SQLite's variable cap
                chunk = ids[start:start + 500]
                touching.update(r[0] for r in conn.execute(
                    'SELECT DISTINCT c.source_id FROM claims c '
                    'JOIN claim_persons cp ON cp.claim_id = c.id '
                    f'WHERE cp.person_id IN ({",".join("?" * len(chunk))})', chunk))
            unread += sorted(path for sid, path in unreadable_sources if sid in touching)
        for path in unread:
            # The reason, read again here so the warning can say what is wrong
            # (a missing `---` block is not something `fha lint` reports as a
            # parse error, so "run fha lint" alone could leave the human stuck).
            _value, trouble = read_restriction_marker(archive_root / path, missing_ok=True)
            messages.append(
                f'WARNING: could not read {path} for its privacy marker'
                + (f' ({trouble})' if trouble else '')
                + ', so there is no way to tell whether it was marked private '
                '(restricted:). It was withheld from this export - a person as '
                '/Restricted/, a source with every fact it is the evidence for. '
                'Repair the file (`fha lint` points at what is wrong), run '
                '`fha index`, then export again.'
            )

        # A marriage event that found no family to live on. `_load_marriages`
        # keys each claim by the people it says married EACH OTHER, and a FAM
        # record holds one couple - so a claim whose `roles: spouse:` names
        # three or more (successive marriages recorded on one claim) matches no
        # key at all, and its date and place go nowhere. Refusing to pick two
        # of the three is right, and the index still derives every pairing, so
        # the export carries the families; dropping the marriage facts without
        # a word is the part that is not. The human would open a
        # complete-looking file with the wedding quietly missing from it.
        # Reported in the same place as the redaction counts, for the same
        # reason: what left the archive, and what did not.
        # Scoped to events whose people are ALL in this export - a couple left
        # out by a seed person or a generation cap is scoping, not a lost fact,
        # and saying so on every capped export would train the human to ignore
        # the line.
        family_keys = {key for key, _children in families}
        for key, row in sorted(marriages.items(), key=lambda kv: kv[1]['id']):
            if key in family_keys or not key <= included:
                continue
            if len(key) > 2:
                messages.append(
                    f'Marriage {fmt_id_display(row["id"])} names {len(key)} people as '
                    'spouses, and a family record holds one couple, so its date and '
                    'place were left out of the export. To export them, write one '
                    'marriage claim per couple - each with its own `roles:` then an '
                    'indented `spouse: [P-…, P-…]` line - and run `fha index`.'
                )
            else:
                pair = ' and '.join(fmt_id_display(p) for p in sorted(key))
                messages.append(
                    f'Marriage {fmt_id_display(row["id"])} names {pair}, who have no '
                    'family record in this export, so its date and place were left '
                    'out. Run `fha index` and export again; if it is still missing, '
                    'the two are not linked by an accepted claim - add one.'
                )

        # GEDCOM 5.5.1 lines are CR/LF-terminated; emit the file with a trailing newline.
        text = '\r\n'.join(lines) + '\r\n'
        return {'status': 'ok', 'text': text, 'messages': messages,
                'person_count': len(included), 'family_count': len(families),
                'unreadable': unread}
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
    basic: bool = False,
) -> Result:
    """Build a GEDCOM 5.5.1 export and return a Result.

    `data` is the {'status', 'text', 'messages', 'person_count', 'family_count'}
    payload `_gedcom_payload` computes; Result exposes dict-style access
    (_lib.py), so callers keep reading `result['text']` unchanged.  Producing the
    GEDCOM text is pure (the `_cmd` layer prints/writes it), so `changed` is empty.
    """
    payload = _gedcom_payload(
        archive_root, pid, mode=mode, generations=generations,
        all_persons=all_persons, include_living=include_living, basic=basic,
    )
    status = payload['status']
    # Mirror _cmd_gedcom's per-status exit codes so headless callers returning
    # Result.exit_code see a failed export as a failure, not a clean 0.
    if status == 'ok':
        # A record withheld because it could not be read is a warning: the
        # file was written, but not with everything the human asked for (#267).
        exit_code = EXIT_WARNINGS if payload.get('unreadable') else EXIT_CLEAN
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
        basic=getattr(args, 'basic', False),
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
    # warning condition (cf. `--include-living` to suppress it). A record
    # withheld because its file could not be read is different: that is damage
    # the human has to repair, so it exits 1 (warnings) beside its message.
    return result.exit_code


def _add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument('person_id', metavar='p-id', nargs='?',
                   help='Seed person for traversal (omit with --all).')
    p.add_argument('--mode', choices=('descendants', 'ancestors', 'connected'),
                   default='descendants',
                   help='Traversal from the seed (default: descendants).')
    p.add_argument('--generations', type=int, metavar='N',
                   help='Depth cap for descendants/ancestors (default: unlimited).')
    p.add_argument('--all', action='store_true',
                   help='Export every person in the index. Do not combine with a '
                        'p-id (they conflict); --all needs no --mode.')
    p.add_argument('--include-living', action='store_true', dest='include_living',
                   help='Export living/unknown persons in full (default: redacted).')
    p.add_argument('--basic', action='store_true',
                   help='For a DNA site: only names, sex, birth/death/marriage dates '
                        'and places, and family links. No sources, citations or '
                        'archive IDs leave the archive.')
    p.add_argument('--out', metavar='FILE', help='Write to FILE (default: stdout).')


# User-facing --help text (the module docstring stays developer-facing).
_CLI_DESCRIPTION = """\
Export your tree to a GEDCOM file for another genealogy app.

  fha gedcom --all                        Export everyone
  fha gedcom <p-id> --mode descendants    Export from one person, down the line
  fha gedcom <p-id> --mode ancestors      Export from one person, up the line
  fha gedcom --all --out family.ged       Choose the output file
  fha gedcom --all --basic --out dna.ged  For a DNA site: names, dates, places
                                          and family links only

Living people are redacted by default (use --include-living to override). GEDCOM
is the portable format Ancestry, RootsMagic, and others read; it is a one-way
export, never re-imported as truth.

Importing a GEDCOM from Ancestry or another program? That is the separate
`fha gedcom import <file.ged>`; this command is the exporter."""


def register(subs: argparse._SubParsersAction) -> argparse.ArgumentParser:
    p = subs.add_parser(
        'gedcom',
        help='Derive a GEDCOM 5.5.1 export from relationships + accepted vitals.',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(p)
    p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    p.set_defaults(func=_cmd_gedcom)
    return p


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha gedcom', description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    parser.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    parser.set_defaults(func=_cmd_gedcom)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
