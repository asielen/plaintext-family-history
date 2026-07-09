#!/usr/bin/env python3
"""
index.py - fha index: build the SQLite query surface.

  fha index                  Full rebuild of .cache/index.sqlite from scratch
  fha index --source S-xxxx  Upsert one source (incremental, sub-second)

The index is a disposable SQLite cache - never authoritative, always rebuildable.
SPEC §8.7, TOOLING §2.

ARCHITECTURE
------------
The index is the query surface for views, find, and report.  It mirrors the
SPEC record model: persons, sources, claims, and derived tables (relationships,
citations, FTS) built for query efficiency.

Two modes:
  Full rebuild (build_index):     drop all tables and re-index everything from
    scratch.  Use after any structural change (new person files, moved records).
  Incremental upsert (upsert_source):  re-index one source and its claims in
    place, then re-derive relationships.  Use after editing a single source file
    - completes in under a second on a normal archive.

The schema lives in _DDL.  Foreign keys are OFF because the archive allows
forward references (a claim can reference a person whose file appears later in
the walk), and referential integrity is enforced by `fha lint` instead.
WAL mode is set for resilience: a crash during indexing leaves the previous
clean index readable rather than corrupting it.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    SIGNIFICANCE,
    CLAIM_TYPES,
    CACHE_SCHEMA_KEY,
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    ID_RE,
    INDEX_SCHEMA_VERSION,
    TOKEN_RE,
    FhaConfigError,
    Message,
    Result,
    edtf_bounds,
    extract_wikilinks,
    id_type_of,
    is_template_file,
    is_valid_id,
    is_working_copy,
    link_field_refs,
    load_fha_yaml,
    normalize_id,
    parse_filename,
    read_record,
    resolve_path,
    resolve_ref,
    resolve_root_arg,
    resolve_typed_ref,
    sqlite_cache_schema_status,
    strip_link_wrapper,
)

import yaml

# ── CODE MAP ──────────────────────────────────────────────────────────────────
#
#  Schema
#    _DDL                    - CREATE TABLE statements for all index tables
#
#  Low-level DB helpers
#    _get_db                 - open (or create) the SQLite file, apply DDL
#    _drop_tables            - wipe all tables before a full rebuild
#
#  Indexers (one per record type)
#    (claim-block refs resolve via _lib.resolve_typed_ref - K4 shared home)
#    _coerce_coord           - one coords entry → float | None
#    _parse_place_coords     - hand-edited coords: → (lat, lon, warning)
#    _index_places           - places.yaml → places, place_names, place_history
#    _index_person           - one person .md → persons + person_files
#                              + hypotheses + search_log (research files)
#    _index_source           - one source .md → sources + claims + claim_persons
#                              + claim_links + source_files + source_people
#    _index_notes            - notes/*.md → notes_fts
#                              + search_log (notes/research-log.md)
#    _index_citations        - all .md → citations (token → file + line)
#
#  Markdown block parsing
#    _parse_md_list_blocks   - generic "- field: value" block parser, shared by
#                              the Hypotheses and Research Log section parsers
#    _index_hypotheses_block - ## Hypotheses entries → hypotheses rows
#    _index_research_log_block - ## Research Log entries → search_log rows
#
#  Derived tables
#    _derive_relationships   - accepted claims → relationships adjacency list
#
#  Top-level build functions
#    build_index             - full rebuild: drop, re-index everything, derive
#    upsert_source           - incremental: re-index one source, re-derive
#
#  CLI
#    register                - attach 'index' to the main fha parser
#    _run_index              - argparse → build_index / upsert_source bridge
#    _standalone_main        - for `python tools/index.py` direct invocation
#
# ─────────────────────────────────────────────────────────────────────────────


def _is_restricted_value(value) -> bool:
    """True when a `restricted:` field value withholds a record from public output.

    The marker is open (SPEC §19): the boolean `true` OR any free-text type
    (`dna`, `by-request`, `deadname`, ...) all mean restricted - and the typed
    values are the strongest markers (`by-request` never opens under any
    export flag), so a narrow `in (True, 'true')` test would flatten exactly
    the wrong ones to unrestricted. Only an absent or explicitly-false value
    is unrestricted. `read_record` coerces YAML booleans to the strings
    'true'/'false'; the bare True/False checks cover direct-dict callers.

    Every `restricted` column write in this file must use this predicate
    (full rebuild and incremental upsert both flow through `_index_source`,
    so one write site keeps the two paths equivalent), and the per-tool
    copies in doctor/lint/gedcom/wikitree/site agree with it exactly
    (tools never import tools - TOOLING §15)."""
    return value not in (None, False, '', 'false')


# ── DDL ───────────────────────────────────────────────────────────────────────
# Schema mirrors the SPEC record model plus derived tables for query speed.
# Foreign keys are OFF - forward references are valid and lint enforces integrity.
# WAL journal mode: a crash during indexing leaves the prior index readable.
# kind column in person_files: profile | research | timeline | sources-index | draft-queue

_DDL = f"""
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=OFF;
PRAGMA user_version={INDEX_SCHEMA_VERSION};

-- meta: cache identity and schema version; disposable, rebuilt by `fha index`.
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY,       -- setting name, currently schema_version
  value TEXT NOT NULL         -- setting value, stored as text for readability
);
INSERT OR REPLACE INTO meta(key, value)
  VALUES ('{CACHE_SCHEMA_KEY}', '{INDEX_SCHEMA_VERSION}');

-- persons: one row per person profile used by find/views/exports.
CREATE TABLE IF NOT EXISTS persons(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  surname TEXT,
  sex TEXT,
  living TEXT NOT NULL DEFAULT 'unknown',
  tier TEXT NOT NULL DEFAULT 'stub',
  status TEXT DEFAULT 'active',
  merged_into TEXT,
  no_known_marriages INTEGER DEFAULT 0,
  no_known_children INTEGER DEFAULT 0,
  birth TEXT,                 -- provisional, unsourced birth EDTF (non-load-bearing)
  death TEXT,                 -- provisional, unsourced death EDTF (non-load-bearing)
  path TEXT NOT NULL
);
-- person_variants: alternate searchable names from person records.
CREATE TABLE IF NOT EXISTS person_variants(person_id TEXT, variant TEXT);
-- person_face_tags: face-region labels that resolve photos to people.
CREATE TABLE IF NOT EXISTS person_face_tags(person_id TEXT, tag TEXT);
-- person_files: profile and generated companion files for each person.
CREATE TABLE IF NOT EXISTS person_files(
  person_id TEXT,
  kind TEXT,
  path TEXT,
  generated INTEGER DEFAULT 0,
  PRIMARY KEY(person_id, kind)
);
-- person_external: outside-system identifiers attached to people.
CREATE TABLE IF NOT EXISTS person_external(person_id TEXT, system TEXT, ext_id TEXT);

-- sources: one row per source record and its searchable metadata.
CREATE TABLE IF NOT EXISTS sources(
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  source_type TEXT,
  date_edtf TEXT,
  date_min TEXT,
  date_max TEXT,
  repository TEXT,
  restricted INTEGER DEFAULT 0,
  source_class TEXT,
  publication_ok INTEGER,
  status TEXT DEFAULT 'active',
  superseded_by TEXT,
  path TEXT NOT NULL
);
-- source_files: original/derived evidence files attached to each source.
CREATE TABLE IF NOT EXISTS source_files(
  source_id TEXT,
  path TEXT,
  role TEXT,
  copy TEXT,
  derived INTEGER DEFAULT 0,
  original_filename TEXT,
  exists_on_disk INTEGER,
  in_inventory INTEGER
);

-- claims: extracted assertions from sources, with date/place/search fields.
CREATE TABLE IF NOT EXISTS claims(
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  type TEXT NOT NULL,
  subtype TEXT,
  date_edtf TEXT,
  date_min TEXT,
  date_max TEXT,
  place_id TEXT,
  place_text TEXT,
  value TEXT NOT NULL,
  status TEXT NOT NULL,
  reviewed TEXT,
  confidence TEXT,
  information TEXT,
  evidence TEXT,
  asset TEXT,
  anchor TEXT,
  hypothesis TEXT,
  significance_override TEXT,
  significance_reason TEXT,
  negated INTEGER DEFAULT 0,
  notes TEXT
);
-- claim_persons: ordered people and roles named by each claim.
CREATE TABLE IF NOT EXISTS claim_persons(
  claim_id TEXT,
  person_id TEXT,
  position INTEGER,
  role TEXT
);
-- claim_links: links from claims to other claims, sources, or hypotheses.
CREATE TABLE IF NOT EXISTS claim_links(
  claim_id TEXT,
  rel TEXT,
  target_id TEXT
);
-- source_people: denormalized source-to-person lookup for fast browsing.
CREATE TABLE IF NOT EXISTS source_people(source_id TEXT, person_id TEXT);

-- relationships: accepted relationship edges derived from accepted claims.
CREATE TABLE IF NOT EXISTS relationships(
  person_id TEXT,
  rel TEXT,
  other_id TEXT,
  claim_id TEXT,
  date_start TEXT,
  date_end TEXT,
  UNIQUE(person_id, rel, other_id, claim_id)
);

-- places: registry places from places/places.yaml.
CREATE TABLE IF NOT EXISTS places(
  id TEXT PRIMARY KEY,
  name TEXT,
  hierarchy TEXT,
  within TEXT,
  lat REAL,
  lon REAL
);
-- place_names: alternate names for each registered place.
CREATE TABLE IF NOT EXISTS place_names(place_id TEXT, alt_name TEXT);
-- place_history: dated hierarchy names for places over time.
CREATE TABLE IF NOT EXISTS place_history(
  place_id TEXT,
  period_edtf TEXT,
  date_min TEXT,
  date_max TEXT,
  hierarchy TEXT
);

-- search_log: prior searches and nil results from research logs/capture.
CREATE TABLE IF NOT EXISTS search_log(
  date TEXT,
  person_id TEXT,
  question TEXT,
  repository TEXT,
  collection TEXT,
  terms TEXT,
  result TEXT,
  source_id TEXT,
  path TEXT
);

-- hypotheses: open research hypotheses attached to people.
CREATE TABLE IF NOT EXISTS hypotheses(
  id TEXT PRIMARY KEY,
  person_id TEXT,
  hypothesis TEXT,
  basis TEXT,
  verify TEXT,
  origin TEXT,
  status TEXT,
  verified_claim TEXT,
  path TEXT
);

-- citations: citation/cross-link token locations by file and line number.
-- `token` holds the RESOLVED canonical ID - a `[[grandmas-album]]` stem or a
-- `[[Ken Smith]]` name that resolves via the alias map is recorded as the
-- record's ID, so every query is ID-uniform regardless of the surface text.
CREATE TABLE IF NOT EXISTS citations(
  token TEXT,
  kind TEXT,
  path TEXT,
  line INTEGER
);

-- aliases: the resolution surface every front door (find, lint, normalize)
-- shares. One row per string that resolves to a record: its own canonical ID,
-- any human stem, an on-demand C-id (added only when a `[[C-…]]` citation
-- exists), and a person's/place's display name + variants. `alias` is stored
-- lowercased. Pure projection - disposable, rebuilt by `fha index`.
CREATE TABLE IF NOT EXISTS aliases(
  alias TEXT,            -- lowercased reference string
  canonical_id TEXT,     -- the record it resolves to
  kind TEXT              -- id | stem | name | variant | claim
);

-- source_places: source-to-place edges from a source's `places:` frontmatter
-- (resolved to L-ids), the location half of the human graph surface.
CREATE TABLE IF NOT EXISTS source_places(source_id TEXT, place_id TEXT);

-- notes_fts: full-text search over notes and record prose.
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts
  USING fts5(path, content);
-- transcripts_fts: full-text search over source transcripts.
CREATE VIRTUAL TABLE IF NOT EXISTS transcripts_fts
  USING fts5(source_id, path, content);
"""

_RELATIONSHIPS_SOCIAL_SUBTYPES = {'friend', 'associate', 'neighbor'}


# ── Build helpers ─────────────────────────────────────────────────────────────

def _get_db(cache_dir: Path) -> sqlite3.Connection:
    cache_dir.mkdir(parents=True, exist_ok=True)
    db_path = cache_dir / 'index.sqlite'
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_DDL)
    return conn


def _drop_tables(conn: sqlite3.Connection) -> None:
    """Drop all data tables for a full rebuild."""
    tables = [
        'meta',
        'persons', 'person_variants', 'person_face_tags', 'person_files',
        'person_external', 'sources', 'source_files', 'claims', 'claim_persons',
        'claim_links', 'source_people', 'source_places', 'relationships',
        'places', 'place_names', 'place_history', 'search_log', 'hypotheses',
        'citations', 'aliases', 'notes_fts', 'transcripts_fts',
    ]
    for t in tables:
        conn.execute(f'DROP TABLE IF EXISTS {t}')
    conn.commit()


# ── Alias resolution surface ──────────────────────────────────────────────────

def _insert_record_aliases(
    conn: sqlite3.Connection,
    canonical_id: str,
    *,
    stems: tuple[str, ...] = (),
    names: tuple[str, ...] = (),
    variants: tuple[str, ...] = (),
) -> None:
    """Insert the alias rows for one record: its own canonical ID (always - the
    line that makes `[[S-…]]` click through in Obsidian), plus any human stems,
    display name(s), and name/alt variants. Strings are unwrapped and lowercased;
    blanks and per-record duplicates are skipped."""
    canonical_id = normalize_id(canonical_id)
    if not canonical_id:
        return
    seen: set[str] = set()

    def add(value: str, kind: str) -> None:
        key = strip_link_wrapper(str(value)).lower()
        if not key or key in seen:
            return
        seen.add(key)
        conn.execute(
            'INSERT INTO aliases(alias, canonical_id, kind) VALUES (?,?,?)',
            (key, canonical_id, kind),
        )

    add(canonical_id, 'id')
    for s in stems:
        add(s, 'stem')
    for n in names:
        add(n, 'name')
    for v in variants:
        add(v, 'variant')


def _resolve_map_from_aliases(
    conn: sqlite3.Connection,
    record_types: tuple[str, ...] | None = None,
) -> dict[str, str]:
    """Build the read-time resolve map `alias → canonical_id` from the aliases
    table. Clash-aware: an alias naming ≥2 distinct records is omitted, so a bare
    ambiguous name never silently resolves (SPEC §7) - the linter flags it.

    `record_types` filters by the canonical TARGET's type prefix ('P', 'L', ...)
    BEFORE clash detection - this is the full-build/upsert equivalence contract
    (round-2 finding 8). The full rebuild snapshots its claim/frontmatter-link
    map at a moment when only persons and places are in the table; the upsert
    reads a table where every other record's aliases survive, so without the
    filter a source alias (say a source hand-aliased 'Ken Smith') clashed the
    person 'Ken Smith' out of the upsert's map and silently dropped the
    claim_persons/source_people rows the full build keeps. Filtering to
    ('P', 'L') makes both maps identical by construction, and the filter runs
    before clash detection so an out-of-scope alias can never veto an
    in-scope name. The citation scans pass None on purpose - they resolve
    source stems and on-demand C-ids too."""
    idx: dict[str, set[str]] = {}
    for alias, cid in conn.execute('SELECT alias, canonical_id FROM aliases'):
        if record_types is not None and id_type_of(cid) not in record_types:
            continue
        idx.setdefault(alias, set()).add(cid)
    return {a: next(iter(ids)) for a, ids in idx.items() if len(ids) == 1}


def _resolve_link_field(value: object, alias_map: dict[str, str] | None) -> list[str]:
    """Resolve a link-valued frontmatter field (`people:`/`places:`) to canonical
    IDs. Each entry may be a bare ID, a `[[Name]]`, a `[[P-…|Name]]`, or the
    nested-list shape an unquoted `[[Name]]` parses into. A name that resolves via
    the alias map becomes its ID; an unresolved-but-ID-shaped entry is kept as-is
    (a possibly-dangling bare ID, which lint flags); an unresolved name draws no
    edge (inert until some record claims it as an alias)."""
    out: list[str] = []
    for ref in link_field_refs(value):
        resolved = resolve_ref(ref, alias_map) if alias_map else None
        if resolved:
            out.append(resolved)
        elif id_type_of(ref):
            out.append(normalize_id(ref))
    return out


def _coerce_coord(value: object) -> float | None:
    """One coordinate value → float, or None when it isn't numeric.

    Accepts int/float and numeric strings (a hand-editor may quote a number);
    bools are excluded because YAML `true` is an int subclass and would silently
    become latitude 1.0."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _parse_place_coords(place: dict) -> tuple[float | None, float | None, str | None]:
    """Validate one place's `coords:` field → (lat, lon, warning_or_None).

    places.yaml is hand-edited, so every malformed shape a human can produce
    must degrade to NULL coordinates plus one plain warning instead of killing
    the whole index build (the old `len(None)` TypeError) or silently storing
    corrupt values (a string `39.8, -95.6` used to index as lat='3', lon='9').
    Valid: a list/tuple whose first two entries are numeric (int/float or a
    numeric string). An absent `coords:` key is normal and silent; a present
    key with anything else gets the warning naming the place and the shape."""
    if 'coords' not in place:
        return (None, None, None)
    raw = place.get('coords')
    # Name the place the way the human knows it (its name), with the id as
    # the precise locator when both exist.
    name = str(place.get('name') or '').strip()
    pid = str(place.get('id') or '').strip()
    if name and pid:
        label = f'{name} ({pid})'
    else:
        label = name or pid or 'an unnamed place'
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        lat = _coerce_coord(raw[0])
        lon = _coerce_coord(raw[1])
        if lat is not None and lon is not None:
            # Numeric, but is it a real point on Earth? A missing decimal
            # (`398` for `39.8`), a transposed lat/lon, or a nan/inf all parse
            # to a float yet index a pin off the globe. Range-check so those
            # degrade to a warning + NULL coords, not a silent bad coordinate.
            if (math.isfinite(lat) and math.isfinite(lon)
                    and -90 <= lat <= 90 and -180 <= lon <= 180):
                return (lat, lon, None)
            return (None, None,
                    f'places/places.yaml: {label} has coords: {raw!r}, which is '
                    f'out of range - latitude must be -90..90 and longitude '
                    f'-180..180 (a missing decimal or swapped pair is the usual '
                    f'cause). The place was indexed without map coordinates; fix '
                    f'the line and re-run `fha index`.')
    return (None, None,
            f'places/places.yaml: {label} has coords: {raw!r}, which is not a '
            f'coordinate pair - write it as coords: [39.8, -95.6] (latitude, '
            f'longitude). The place was indexed without map coordinates; fix '
            f'the line and re-run `fha index`.')


def _index_places(conn: sqlite3.Connection, archive_root: Path) -> list[str]:
    """Index places/places.yaml → places tables. Returns warning lines.

    Warnings (bad coords shapes) are returned rather than printed so
    build_index can carry them on its Result and the CLI can render them -
    per the structured-result contract, run_* computes, _cmd_* prints. The
    two pre-existing parse-level warnings below still print to stderr
    directly so non-CLI callers (fha report's in-process rebuild) keep
    seeing them; folding those into the Result too is a follow-up."""
    warnings: list[str] = []
    places_path = archive_root / 'places' / 'places.yaml'
    if not places_path.exists():
        return warnings
    try:
        with open(places_path, encoding='utf-8') as f:
            places = yaml.safe_load(f)
    except Exception as exc:
        print(
            f'WARNING: places/places.yaml could not be parsed ({exc}); '
            'place registry will be empty until this is fixed.',
            file=sys.stderr,
        )
        return warnings

    if places is None:
        return warnings
    if not isinstance(places, list):
        print(
            'WARNING: places/places.yaml is not a YAML list; '
            'place registry will be empty until this is fixed.',
            file=sys.stderr,
        )
        return warnings

    for place in places:
        if not isinstance(place, dict):
            continue
        pid = normalize_id(str(place.get('id', '')))
        if not pid:
            continue
        lat, lon, coord_warning = _parse_place_coords(place)
        if coord_warning:
            warnings.append(coord_warning)
        conn.execute(
            'INSERT OR REPLACE INTO places(id, name, hierarchy, within, lat, lon) VALUES (?,?,?,?,?,?)',
            (pid, place.get('name'), place.get('hierarchy'), place.get('within'),
             lat, lon),
        )
        alt_names = [str(a) for a in (place.get('alt_names') or [])]
        for alt in alt_names:
            conn.execute('INSERT INTO place_names(place_id, alt_name) VALUES (?,?)', (pid, alt))
        # Register the place's name + alt_names as aliases so a hand-typed
        # `[[Fairview]]` resolves to its L-id in Obsidian and our tools.
        place_name = place.get('name')
        _insert_record_aliases(
            conn, pid,
            names=(str(place_name),) if place_name else (),
            variants=tuple(alt_names),
        )
        for h in (place.get('history') or []):
            if isinstance(h, dict):
                period = str(h.get('period', ''))
                mn, mx = edtf_bounds(period) if period else ('', '')
                conn.execute(
                    'INSERT INTO place_history(place_id, period_edtf, date_min, date_max, hierarchy) VALUES (?,?,?,?,?)',
                    (pid, period, mn, mx, h.get('hierarchy')),
                )

    return warnings


_MD_HEADING_RE = re.compile(r'^##\s')
_MD_LIST_ITEM_RE = re.compile(r'^-\s+(\w+):\s*(.*)$')
_MD_CONTINUATION_RE = re.compile(r'^\s{2,}(\w+):\s*(.*)$')


def _parse_md_list_blocks(section_body: str) -> list[dict[str, str]]:
    """
    Parse a markdown section body into a list of `- field: value` entries.

    Each entry starts with a line matching `- field: value` and continues
    with indented `  field: value` continuation lines until the next `- `
    entry, a blank line, or the next `##` heading (the caller is expected to
    have already sliced the body down to one section, but this also bails out
    defensively on a heading so a malformed slice can't bleed into the next
    section's entries).

    Tolerant of the well-formed two-space-indent style (the canonical example,
    SPEC §16) and is otherwise strict about line shape - a continuation line
    that lacks the leading indent is just not picked up, rather than guessed
    at, since the field-name-as-disambiguator trick is fragile prose to rely on.
    Values are returned exactly as written, quotes and all; callers that care
    about quoting strip it themselves.
    """
    entries: list[dict[str, str]] = []
    current: dict[str, str] | None = None

    for line in section_body.splitlines():
        if _MD_HEADING_RE.match(line):
            break
        if not line.strip():
            current = None
            continue

        m = _MD_LIST_ITEM_RE.match(line)
        if m:
            current = {m.group(1): m.group(2).strip()}
            entries.append(current)
            continue

        if current is not None:
            cm = _MD_CONTINUATION_RE.match(line)
            if cm:
                current[cm.group(1)] = cm.group(2).strip()
            else:
                # Unindented or unrecognized line inside an entry - the entry
                # is over (matches the "blank line or next `- `" termination
                # rule in spirit: anything that isn't a recognized field line
                # ends the current entry rather than corrupting it).
                current = None

    return entries


def _strip_quotes(value: str) -> str:
    """Strip a single layer of matching quotes a YAML-ish hand-written value may carry."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def _extract_section_body(body: str, heading: str) -> str:
    """Return the text between `## {heading}` and the next `##` heading (or EOF)."""
    pattern = re.compile(
        rf'^##\s*{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)', re.M | re.S,
    )
    m = pattern.search(body)
    return m.group(1) if m else ''


def _index_hypotheses_block(
    conn: sqlite3.Connection, body: str, pid: str | None, rel_path: str,
) -> None:
    """Parse `## Hypotheses` entries from a research file body and insert rows."""
    section = _extract_section_body(body, 'Hypotheses')
    if not section.strip():
        return
    for entry in _parse_md_list_blocks(section):
        hid = normalize_id(_strip_quotes(entry.get('id', '')))
        if not hid or not hid.startswith('h-'):
            continue
        status = _strip_quotes(entry.get('status', ''))
        verified_claim = None
        cm = ID_RE.search(status)
        if cm and cm.group(1).upper() == 'C':
            verified_claim = normalize_id(f"{cm.group(1)}-{cm.group(2)}")
        conn.execute(
            '''INSERT OR REPLACE INTO hypotheses
               (id, person_id, hypothesis, basis, verify, origin, status, verified_claim, path)
               VALUES (?,?,?,?,?,?,?,?,?)''',
            (
                hid, pid,
                _strip_quotes(entry.get('hypothesis', '')),
                _strip_quotes(entry.get('basis', '')),
                _strip_quotes(entry.get('verify', '')),
                _strip_quotes(entry.get('origin', '')),
                status,
                verified_claim,
                rel_path,
            ),
        )


def _index_research_log_block(
    conn: sqlite3.Connection, body: str, pid: str | None, rel_path: str,
) -> None:
    """Parse `## Research Log` entries from a research file (or notes/research-log.md)
    body and insert rows into search_log.

    notes/research-log.md (SPEC §16) isn't specified to require the
    `## Research Log` heading the way a person research file does - it may
    just be a bare list of entries.  Fall back to treating the whole body as
    the section when no heading is present, so either shape works.
    """
    section = _extract_section_body(body, 'Research Log')
    if not section.strip():
        section = body
    if not section.strip():
        return
    for entry in _parse_md_list_blocks(section):
        date = _strip_quotes(entry.get('date', ''))
        if not date:
            continue
        result = _strip_quotes(entry.get('result', ''))
        source_id = None
        sm = ID_RE.search(result)
        if sm and sm.group(1).upper() == 'S':
            source_id = normalize_id(f"{sm.group(1)}-{sm.group(2)}")
        entry_pid = pid
        if not entry_pid:
            # Multi-person/locality entries (notes/research-log.md) carry no
            # implicit person - only pick one up if the entry explicitly names
            # a person_id or P-id (SPEC §16: "no person_id field there since
            # it's not person-scoped the same way").
            explicit = entry.get('person_id') or ''
            qm = ID_RE.search(explicit) or ID_RE.search(entry.get('question', ''))
            if qm and qm.group(1).upper() == 'P':
                entry_pid = normalize_id(f"{qm.group(1)}-{qm.group(2)}")
        conn.execute(
            '''INSERT INTO search_log
               (date, person_id, question, repository, collection, terms, result, source_id, path)
               VALUES (?,?,?,?,?,?,?,?,?)''',
            (
                date, entry_pid,
                _strip_quotes(entry.get('question', '')),
                _strip_quotes(entry.get('repository', '')),
                _strip_quotes(entry.get('collection', '')),
                _strip_quotes(entry.get('terms', '')),
                result,
                source_id,
                rel_path,
            ),
        )


def _index_person(conn: sqlite3.Connection, path: Path, archive_root: Path) -> None:
    """
    Index one person .md file into persons and person_files.

    Profile files (kind='profile') get a full persons row upsert.  Companion
    files (kind='timeline', 'sources-index', etc.) only get a person_files row
    - they don't create a second persons entry, but views can find them by
    person_id and kind.

    Surname is parsed from the filename's double-underscore convention
    ({surname}__{given}_{P-id}) rather than the name: field, because the
    frontmatter name may include middle names or honorifics while the filename
    slug is always the birth surname.
    """
    if is_template_file(path):
        return   # `_TEMPLATE.*` is a teaching template, not a record
    rec = read_record(path)
    meta = rec['meta']

    pid = normalize_id(str(meta.get('id', '')))
    if not pid:
        # Generated companion files (timeline, sources-index, draft-queue) carry no
        # frontmatter id - the P-id lives in the filename instead.  Extract it so
        # these files appear in person_files and are discoverable via fha find.
        parsed = parse_filename(path)
        if parsed and parsed['id_type'] == 'P':
            pid = parsed['id_str']
    if not pid or not pid.startswith('p-'):
        return

    name = str(meta.get('name', '')) or 'unknown'
    # Determine kind from filename
    stem = path.stem
    kind = 'profile'
    for k in ('research', 'timeline', 'sources-index', 'draft-queue'):
        if f'_{k}_' in stem or stem.endswith(f'_{k}'):
            kind = k
            break

    is_companion = kind != 'profile'

    if not is_companion:
        # Primary profile - upsert person row
        name_parts = name.rsplit(' ', 1)
        surname = None
        if '__' in stem:
            # extract from filename: {surname}__{given...}
            surname_part = stem.split('__')[0]
            surname = surname_part.replace('_', ' ').title()

        living_val = str(meta.get('living', 'unknown')).lower()
        if living_val not in ('true', 'false', 'unknown'):
            living_val = 'unknown'

        conn.execute(
            '''INSERT OR REPLACE INTO persons
               (id, name, surname, sex, living, tier, status, merged_into,
                no_known_marriages, no_known_children, birth, death, path)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (
                pid, name, surname,
                str(meta.get('sex', '')),
                living_val,
                str(meta.get('tier', 'stub')),
                str(meta.get('status', 'active')),
                normalize_id(str(meta.get('merged_into', ''))) or None,
                1 if meta.get('no_known_marriages') in (True, 'true') else 0,
                1 if meta.get('no_known_children') in (True, 'true') else 0,
                str(meta.get('birth', '')) or None,
                str(meta.get('death', '')) or None,
                str(path.relative_to(archive_root)),
            ),
        )
        # Restricted variants (deadnames, SPEC §18) go into aliases for internal
        # link resolution only - they must not enter person_variants, which feeds
        # public rendering paths (WikiTree fold forms, search display, etc.).
        # Single pass over the raw list (entries are still dicts or strings here);
        # deriving public_variants from the already-flattened all_variants strings
        # would break the isinstance check needed to detect the restricted flag.
        all_variants: list[str] = []
        public_variants: list[str] = []
        for _v in (meta.get('name_variants') or []):
            if isinstance(_v, dict):
                _val = _v.get('value')
                if not _val:
                    continue
                all_variants.append(_val)
                if not _is_restricted_value(_v.get('restricted')):
                    public_variants.append(_val)
            else:
                _s = str(_v)
                all_variants.append(_s)
                public_variants.append(_s)
        for v in public_variants:
            conn.execute('INSERT INTO person_variants(person_id, variant) VALUES (?,?)', (pid, v))
        # Register this person's resolution surface: the P-id (so `[[P-…]]`
        # clicks through), any hand-typed `aliases:` stems, the display name, and
        # each name variant - so `[[Ken Smith]]` resolves to the right P-id.
        # All variants (including restricted) go into aliases so name-wikilinks
        # to former names still resolve internally; render paths redact the display.
        # `also_known_as`, `name_at_birth`, and `married_name` (SPEC person
        # template) are additional resolution surfaces so `[[Peggy]]` /
        # `[[Margaret Cole]]` / `[[Margaret Hartley]]` all click through to the
        # same P-id. The template documents them as aliases; the indexer folds
        # them into the alias-insertion path so the promise holds.
        # `name_variants` above unwraps the `{value, restricted}` dict form; the
        # same shape is legal here (SPEC §18), so mirror the unwrap - a bare
        # `str(x)` on a dict would insert its Python repr as the alias, which
        # never resolves. Restricted variants still enter aliases so name-links
        # to a former name resolve internally (render paths handle redaction).
        def _variant_value(x):
            if isinstance(x, dict):
                v = x.get('value')
                return str(v) if v else None
            return str(x) if x else None

        extra_alias_names: list[str] = []
        aka = meta.get('also_known_as') or []
        if isinstance(aka, (list, tuple)):
            for a in aka:
                v = _variant_value(a)
                if v:
                    extra_alias_names.append(v)
        elif aka:
            v = _variant_value(aka)
            if v:
                extra_alias_names.append(v)
        for _fld in ('name_at_birth', 'married_name'):
            v = _variant_value(meta.get(_fld))
            if v:
                extra_alias_names.append(v)
        _insert_record_aliases(
            conn, pid,
            stems=tuple(str(a) for a in (meta.get('aliases') or [])),
            names=(name,) if name and name != 'unknown' else (),
            variants=tuple(all_variants) + tuple(extra_alias_names),
        )
        for t in (meta.get('face_tags') or []):
            conn.execute('INSERT INTO person_face_tags(person_id, tag) VALUES (?,?)', (pid, str(t)))
        ext_ids = meta.get('external_ids') or {}
        if isinstance(ext_ids, dict):
            for system, ext_id in ext_ids.items():
                conn.execute(
                    'INSERT INTO person_external(person_id, system, ext_id) VALUES (?,?,?)',
                    (pid, system, str(ext_id)),
                )

    # Always record the file association.  Generated views have no frontmatter id
    # (their id comes from the filename fallback above) so mark them generated=1.
    is_generated = not meta.get('id')
    conn.execute(
        'INSERT OR REPLACE INTO person_files(person_id, kind, path, generated) VALUES (?,?,?,?)',
        (pid, kind, str(path.relative_to(archive_root)), 1 if is_generated else 0),
    )

    # FTS index the body
    body = rec['body']
    if body.strip():
        conn.execute(
            'INSERT INTO notes_fts(path, content) VALUES (?,?)',
            (str(path.relative_to(archive_root)), body),
        )

    # Research files (SPEC §16) carry ## Hypotheses and ## Research Log
    # sections - the only place those durable records live.  Without this,
    # the report's hypotheses/search-log sections always read empty even when
    # the archive has real entries (the report rebuilds the index right
    # before querying these tables).
    if kind == 'research' and body.strip():
        rel_path = str(path.relative_to(archive_root))
        _index_hypotheses_block(conn, body, pid, rel_path)
        _index_research_log_block(conn, body, pid, rel_path)


def _index_source(
    conn: sqlite3.Connection,
    path: Path,
    archive_root: Path,
    fha_config: dict,
    alias_map: dict[str, str] | None = None,
) -> None:
    """Index one source markdown file.

    `alias_map`, when supplied, resolves name-first frontmatter link fields
    (`people:`/`places:`) - e.g. `people: ["[[Ken Smith]]"]` → the matching
    P-id. Without it the fields are read the legacy bare-ID way, so this stays
    callable for the incremental and test paths that pass no map.
    """
    if is_template_file(path):
        return   # `_TEMPLATE.*` is a teaching template, not a record
    rec = read_record(path)
    meta = rec['meta']

    sid = normalize_id(str(meta.get('id', '')))
    if not sid or not sid.startswith('s-'):
        return

    title = str(meta.get('title', ''))
    source_type = str(meta.get('source_type', ''))
    date_edtf = str(meta.get('source_date', ''))
    mn, mx = edtf_bounds(date_edtf) if date_edtf else ('', '')
    # Any truthy `restricted:` - including the typed values `dna`/`by-request`,
    # the strongest markers - stores 1, so every SQL prefilter built on this
    # column excludes them. The narrow `in (True, 'true')` idiom used here
    # before flattened typed values to 0 (unrestricted).
    restricted = 1 if _is_restricted_value(meta.get('restricted')) else 0
    # Three-state on purpose: exporters distinguish "explicitly not publishable"
    # from "unset". 1 = rights.publication_ok true; 0 = explicit false; NULL =
    # absent (publishable by default). The redaction predicate consumers share
    # is COALESCE(publication_ok, 1) = 0 (gedcom, wikitree, site), which only
    # fires on a stored 0 - so a false MUST be stored as 0, not folded to NULL.
    pub_ok = meta.get('rights', {})
    if isinstance(pub_ok, dict) and 'publication_ok' in pub_ok:
        pub_ok = 1 if pub_ok.get('publication_ok') in (True, 'true') else 0
    else:
        pub_ok = None

    conn.execute(
        '''INSERT OR REPLACE INTO sources
           (id, title, source_type, date_edtf, date_min, date_max,
            repository, restricted, source_class, publication_ok,
            status, superseded_by, path)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (
            sid, title, source_type, date_edtf, mn, mx,
            str(meta.get('repository', '')),
            restricted,
            str(meta.get('source_class', '')),
            pub_ok,
            str(meta.get('status', 'active')),
            normalize_id(str(meta.get('superseded_by', ''))) or None,
            str(path.relative_to(archive_root)),
        ),
    )

    # Register the source's own resolution surface: the S-id (so `[[S-…]]`
    # clicks through) plus any hand-typed `aliases:` stems (`grandmas-album`).
    _insert_record_aliases(
        conn, sid,
        stems=tuple(str(a) for a in (meta.get('aliases') or [])),
    )

    # People listed on the source - the human graph surface (frontmatter
    # cross-links). Entries may be bare P-ids or name-first `[[Ken Smith]]`
    # links; resolve each to a canonical P-id via the alias map.
    for pid in _resolve_link_field(meta.get('people'), alias_map):
        conn.execute(
            'INSERT INTO source_people(source_id, person_id) VALUES (?,?)',
            (sid, pid),
        )

    # Places the source involves - optional location half of the graph surface.
    for lid in _resolve_link_field(meta.get('places'), alias_map):
        conn.execute(
            'INSERT INTO source_places(source_id, place_id) VALUES (?,?)',
            (sid, lid),
        )

    # File inventory
    for f in (meta.get('files') or []):
        if not isinstance(f, dict):
            continue
        file_path = str(f.get('file', ''))
        role = str(f.get('role', ''))
        derived = 1 if f.get('derived') in (True, 'true') else 0
        orig_name = str(f.get('original_filename', '')) or None
        file_status = str(f.get('status', ''))

        resolved = resolve_path(file_path, fha_config, archive_root)
        # In working-copy mode assets are assumed present on the main machine;
        # store NULL rather than 0 so callers know "unknown" vs "absent".
        exists = None if is_working_copy(archive_root) else (1 if resolved.exists() else 0)

        conn.execute(
            '''INSERT INTO source_files
               (source_id, path, role, copy, derived, original_filename,
                exists_on_disk, in_inventory)
               VALUES (?,?,?,?,?,?,?,1)''',
            (sid, file_path, role, None, derived, orig_name, exists),
        )

    # Claims
    for claim in rec['claims']:
        if not isinstance(claim, dict):
            continue
        cid = normalize_id(str(claim.get('id', '')))
        if not cid or not cid.startswith('c-'):
            continue

        claim_date = str(claim.get('date', ''))
        cmn, cmx = edtf_bounds(claim_date) if claim_date else ('', '')
        negated = 1 if claim.get('negated') in (True, 'true') else 0
        # place: gets the same tolerance as persons: - a wrapped `[[L-…]]` or an
        # unambiguous registered place name resolves; free text stays out of
        # place_id (it lives in place_text) instead of being stored as garbage.
        place_id_raw = resolve_typed_ref(claim.get('place'), alias_map, want='L')

        sig_override = str(claim.get('significance', '')) or None

        conn.execute(
            '''INSERT OR REPLACE INTO claims
               (id, source_id, type, subtype, date_edtf, date_min, date_max,
                place_id, place_text, value, status, reviewed, confidence,
                information, evidence, asset, anchor, hypothesis,
                significance_override, significance_reason, negated, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (
                cid, sid,
                str(claim.get('type', '')),
                str(claim.get('subtype', '')) or None,
                claim_date, cmn, cmx,
                place_id_raw,
                str(claim.get('place_text', '')) or None,
                str(claim.get('value', '')),
                str(claim.get('status', '')),
                str(claim.get('reviewed', '')) or None,
                str(claim.get('confidence', '')) or None,
                str(claim.get('information', '')) or None,
                str(claim.get('evidence', '')) or None,
                str(claim.get('asset', '')) or None,
                str(claim.get('anchor', '')) or None,
                normalize_id(str(claim.get('hypothesis', ''))) or None,
                sig_override,
                str(claim.get('significance_reason', '')) or None,
                negated,
                str(claim.get('notes', '')) or None,
            ),
        )

        # claim_persons - entries may be bare P-ids, wrapped `[[P-…|Name]]`
        # links, or `[[Name]]` links (the quickstart's hand-authored form).
        # Each resolves via _lib.resolve_typed_ref; an unresolvable name is an
        # inert note-link and draws no row (TOOLING §3 E004). link_field_refs
        # also flattens the nested-list shape an unquoted `[[Name]]` parses to.
        roles_map = claim.get('roles') or {}
        resolved_roles: list[tuple[str, set[str]]] = []
        if isinstance(roles_map, dict):
            # Pre-resolve roles values once so `roles: {child: "[[Sam Rivera]]"}`
            # matches the same resolved P-id its persons: entry produces.
            for role_name, role_val in roles_map.items():
                role_pids = {
                    rid for r in link_field_refs(role_val)
                    for rid in [resolve_typed_ref(r, alias_map, want='P')]
                    if rid
                }
                resolved_roles.append((str(role_name), role_pids))

        for pos, p_raw in enumerate(link_field_refs(claim.get('persons'))):
            ppid = resolve_typed_ref(p_raw, alias_map, want='P')
            if not ppid:
                continue   # inert note-link: unknown/ambiguous name, no garbage row
            role = next((rn for rn, pids in resolved_roles if ppid in pids), None)
            conn.execute(
                'INSERT INTO claim_persons(claim_id, person_id, position, role) VALUES (?,?,?,?)',
                (cid, ppid, pos, role),
            )

        # claim_links - targets are C-ids, possibly wrapped (`[[C-…]]`).
        # ID-shaped only, deliberately: the claim-time alias map carries only
        # person/place targets (the _resolve_map_from_aliases equivalence
        # contract), so it could never resolve a name to a C-id anyway - a
        # name here would land on a person and store a cross-type edge.
        # Lint's E004 handles name targets per the inert-note-link contract.
        for link_type in ('corroborates', 'contradicts'):
            for t in link_field_refs(claim.get(link_type)):
                tid = resolve_typed_ref(t, alias_map=None)
                if tid:
                    conn.execute(
                        'INSERT INTO claim_links(claim_id, rel, target_id) VALUES (?,?,?)',
                        (cid, link_type, tid),
                    )

    # FTS index body
    body = rec['body']
    if body.strip():
        conn.execute(
            'INSERT INTO notes_fts(path, content) VALUES (?,?)',
            (str(path.relative_to(archive_root)), body),
        )


def _index_notes(conn: sqlite3.Connection, archive_root: Path) -> None:
    """Index notes files for FTS."""
    notes_dir = archive_root / 'notes'
    if not notes_dir.exists():
        return
    for path in notes_dir.rglob('*.md'):
        try:
            content = path.read_text(encoding='utf-8')
        except OSError:
            continue
        conn.execute(
            'INSERT INTO notes_fts(path, content) VALUES (?,?)',
            (str(path.relative_to(archive_root)), content),
        )

    # notes/research-log.md (SPEC §16): multi-person/locality searches log
    # here with the same field shape as a research file's ## Research Log,
    # but no implicit person_id (it isn't person-scoped) - picked up only
    # when an entry explicitly names one.
    research_log_path = notes_dir / 'research-log.md'
    if research_log_path.exists():
        try:
            content = research_log_path.read_text(encoding='utf-8')
        except OSError:
            content = ''
        if content.strip():
            rel_path = str(research_log_path.relative_to(archive_root))
            _index_research_log_block(conn, content, None, rel_path)


def _index_capture_log(conn: sqlite3.Connection, archive_root: Path) -> None:
    """Re-ingest `.cache/capture_log.jsonl` rows into search_log.

    `fha capture` writes a search_log row directly into index.sqlite for
    immediate freshness, but a full rebuild drops and recreates search_log
    from scratch (`_drop_tables`) - without this, that row would vanish on the
    next `fha index` run. capture.py also always appends the same row to this
    jsonl file, so re-ingesting it here makes every capture survive a reindex
    regardless of whether the index existed at capture time.
    """
    capture_log_path = archive_root / '.cache' / 'capture_log.jsonl'
    if capture_log_path.exists():
        try:
            lines = capture_log_path.read_text(encoding='utf-8').splitlines()
        except OSError:
            lines = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            conn.execute(
                '''INSERT INTO search_log
                   (date, person_id, question, repository, collection, terms, result, source_id, path)
                   VALUES (?,?,?,?,?,?,?,?,?)''',
                (
                    entry.get('date', ''), None, entry.get('question', ''),
                    entry.get('repository', ''), entry.get('collection', ''),
                    entry.get('terms', ''), entry.get('result', ''), None,
                    entry.get('path', ''),
                ),
            )


def _index_citations(
    conn: sqlite3.Connection,
    archive_root: Path,
    alias_map: dict[str, str] | None = None,
) -> None:
    """Scan all .md files for citation tokens and record the RESOLVED canonical ID.

    Two kinds of token are picked up:
      - ID tokens (`[[S-…]]`, legacy `[S-…]`) - stored as their own lowercased ID,
        exactly as before. Dangling IDs are still recorded; lint flags them.
      - name/stem wikilinks (`[[grandmas-album]]`, `[[Ken Smith]]`) - stored as
        the record's ID *only when* `alias_map` resolves them, so a stem citation
        is ID-uniform with an ID citation. An unresolved name link is inert (it's
        an ordinary Obsidian note-link, not a citation) and recorded nowhere.

    As a side effect, a cited `[[C-…]]` registers that C-id as an alias of its
    owning source - the on-demand C-id aliasing (added only when the citation
    actually exists, so a 60-claim interview carries no dead weight).
    """
    from _lib import TOKEN_RE
    # archive_root/out/ is fha packet's default, gitignored output directory
    # (TOOLING §8) - disposable export copies, not archive truth, so they
    # must not become citation sites in the index. Only the root-level out/
    # is skipped - a record tree's own 'out' subdirectory (sources/out/, …)
    # is real archive content and must still be scanned.
    packet_out_root = archive_root / 'out'
    cited_cids: set[str] = set()
    for path in archive_root.rglob('*.md'):
        if '.cache' in path.parts:
            continue
        if path.is_relative_to(packet_out_root):
            continue
        cited_cids |= _index_citations_for_file(conn, path, archive_root, alias_map)

    _register_cited_claim_aliases(conn, cited_cids)


def _index_citations_for_file(
    conn: sqlite3.Connection,
    path: Path,
    archive_root: Path,
    alias_map: dict[str, str] | None = None,
) -> set[str]:
    """Scan one .md file for citation tokens, inserting one citations row per
    occurrence (resolved canonical ID), and return the set of C-ids it cites.

    Shared by the full scan and the incremental upsert so both record citations
    - ID tokens and resolved name/stem wikilinks - identically. The caller turns
    the returned C-ids into on-demand source aliases via
    `_register_cited_claim_aliases`."""
    from _lib import TOKEN_RE
    if is_template_file(path):
        return set()   # `_TEMPLATE.*` placeholder tokens are not citations
    try:
        lines = path.read_text(encoding='utf-8', errors='ignore').splitlines()
    except OSError:
        return set()
    rel = str(path.relative_to(archive_root))
    cited_cids: set[str] = set()
    for lineno, line in enumerate(lines, start=1):
        for m in TOKEN_RE.finditer(line):
            token = m.group(1).lower()
            kind = token[0].upper()
            if kind == 'C':
                cited_cids.add(token)
            conn.execute(
                'INSERT INTO citations(token, kind, path, line) VALUES (?,?,?,?)',
                (token, kind, rel, lineno),
            )
        # Name/stem wikilinks resolve through the alias map to the record's
        # canonical ID. ID-shaped targets are skipped here - already handled by
        # the TOKEN_RE pass above - so `[[S-…]]` is never double-counted.
        if alias_map:
            for target, _disp, _frag, _span in extract_wikilinks(line):
                if id_type_of(target):
                    continue
                resolved = resolve_ref(target, alias_map)
                if not resolved:
                    continue
                if resolved.startswith('c-'):
                    cited_cids.add(resolved)
                conn.execute(
                    'INSERT INTO citations(token, kind, path, line) VALUES (?,?,?,?)',
                    (resolved, resolved[0].upper(), rel, lineno),
                )
    return cited_cids


def _register_cited_claim_aliases(conn: sqlite3.Connection, cited_cids: set[str]) -> None:
    """On-demand C-id aliasing: for each cited C-id, register it as an alias of
    its owning source so `[[C-…]]` opens the source record (the claim's home,
    SPEC §8.7). Only cited C-ids get a row - a claim nobody links to stays out of
    the alias surface, keeping a many-claim source lean."""
    for cid in sorted(cited_cids):
        row = conn.execute('SELECT source_id FROM claims WHERE id=?', (cid,)).fetchone()
        if row is None:
            continue
        source_id = row[0] if not isinstance(row, sqlite3.Row) else row['source_id']
        if source_id:
            conn.execute(
                'INSERT INTO aliases(alias, canonical_id, kind) VALUES (?,?,?)',
                (cid, normalize_id(str(source_id)), 'claim'),
            )


def _derive_relationships(conn: sqlite3.Connection) -> None:
    """
    Materialise relationship edges from accepted claims into the relationships table.

    This is a pre-computed adjacency list: rather than joining claim_persons on
    every query, known parent/child/spouse edges are written here so callers
    can ask "who are this person's parents?" with a simple SELECT.

    Called at the end of both full rebuild and incremental upsert so the table
    is always current.  Only accepted claims are used - suggested and
    needs-review claims don't become load-bearing graph edges.

    Parent/child and spouse edges are keyed on the claim's `roles:` map (child +
    parent, or spouse), not on `subtype:` - `subtype` now names the *nature* of a
    bond (biological, adoptive, …; SPEC §8.2), and every parent edge is recorded
    regardless of nature. Legacy `subtype: child-of`/`spouse-of` claims still
    derive correctly since they carry the same roles.
    """
    conn.execute('DELETE FROM relationships')

    rows = conn.execute(
        '''SELECT c.id, c.type, c.subtype, c.date_edtf, c.date_min, c.date_max
           FROM claims c
           WHERE c.status = 'accepted'
             AND c.type IN ('relationship', 'marriage', 'divorce', 'death')
           ORDER BY CASE c.type WHEN 'divorce' THEN 1 WHEN 'death' THEN 1 ELSE 0 END'''
    ).fetchall()

    for (cid, ctype, subtype, date_edtf, dmin, dmax) in rows:
        all_persons = conn.execute(
            'SELECT person_id, role FROM claim_persons WHERE claim_id=?', (cid,)
        ).fetchall()
        pids = [p for p, r in all_persons]

        if ctype == 'relationship':
            # The edge's kind comes from the roles: map (the part each person
            # plays), not from subtype: - subtype now names the *nature* of a
            # parent/child bond (biological, adoptive, step, …; SPEC §8.2). A
            # claim naming a child and a parent is a parent/child edge whatever
            # its nature; legacy `subtype: child-of` claims still match because
            # they carry the same roles, and legacy `spouse-of` is caught by the
            # subtype fallback below.
            child_ids = [p for p, r in all_persons if r == 'child']
            parent_ids = [p for p, r in all_persons if r == 'parent']
            spouse_ids = [p for p, r in all_persons if r == 'spouse']

            if child_ids and parent_ids:
                for child_id in child_ids:
                    for parent_id in parent_ids:
                        conn.execute(
                            'INSERT OR IGNORE INTO relationships(person_id, rel, other_id, claim_id, date_start, date_end) VALUES (?,?,?,?,?,?)',
                            (child_id, 'parent', parent_id, cid, dmin, dmax),
                        )
                        conn.execute(
                            'INSERT OR IGNORE INTO relationships(person_id, rel, other_id, claim_id, date_start, date_end) VALUES (?,?,?,?,?,?)',
                            (parent_id, 'child', child_id, cid, dmin, dmax),
                        )
            elif spouse_ids or subtype == 'spouse-of':
                # A relationship claim naming spouses (or a legacy spouse-of
                # subtype) yields reciprocal spouse edges, like a marriage claim.
                spouse_pids = spouse_ids or pids
                for i, p1 in enumerate(spouse_pids):
                    for p2 in spouse_pids[i+1:]:
                        conn.execute(
                            'INSERT OR IGNORE INTO relationships VALUES (?,?,?,?,?,?)',
                            (p1, 'spouse', p2, cid, dmin, None),
                        )
                        conn.execute(
                            'INSERT OR IGNORE INTO relationships VALUES (?,?,?,?,?,?)',
                            (p2, 'spouse', p1, cid, dmin, None),
                        )
            elif subtype in _RELATIONSHIPS_SOCIAL_SUBTYPES:
                for i, p1 in enumerate(pids):
                    for p2 in pids[i+1:]:
                        rel = subtype or 'associate'
                        conn.execute(
                            'INSERT OR IGNORE INTO relationships VALUES (?,?,?,?,?,?)',
                            (p1, rel, p2, cid, dmin, dmax),
                        )
                        conn.execute(
                            'INSERT OR IGNORE INTO relationships VALUES (?,?,?,?,?,?)',
                            (p2, rel, p1, cid, dmin, dmax),
                        )
            else:
                # Directional power-tie roles: enslaved/enslaver, employer/employee.
                # Each directed pair gets an asymmetric edge so callers can
                # distinguish victim from perpetrator (SPEC §8.2).
                for (role_a, edge_a), (role_b, edge_b) in (
                    (('enslaved', 'enslaved-by'), ('enslaver', 'enslaver')),
                    (('employee', 'employee'), ('employer', 'employer')),
                ):
                    a_ids = [p for p, r in all_persons if r == role_a]
                    b_ids = [p for p, r in all_persons if r == role_b]
                    for pa in a_ids:
                        for pb in b_ids:
                            conn.execute(
                                'INSERT OR IGNORE INTO relationships VALUES (?,?,?,?,?,?)',
                                (pa, edge_a, pb, cid, dmin, dmax),
                            )
                            conn.execute(
                                'INSERT OR IGNORE INTO relationships VALUES (?,?,?,?,?,?)',
                                (pb, edge_b, pa, cid, dmin, dmax),
                            )
        elif ctype == 'marriage':
            for i, p1 in enumerate(pids):
                for p2 in pids[i+1:]:
                    conn.execute(
                        'INSERT OR IGNORE INTO relationships VALUES (?,?,?,?,?,?)',
                        (p1, 'spouse', p2, cid, dmin, None),
                    )
                    conn.execute(
                        'INSERT OR IGNORE INTO relationships VALUES (?,?,?,?,?,?)',
                        (p2, 'spouse', p1, cid, dmin, None),
                    )
        elif ctype == 'divorce':
            for i, p1 in enumerate(pids):
                for p2 in pids[i+1:]:
                    conn.execute(
                        '''UPDATE relationships SET date_end = ?
                           WHERE person_id = ? AND rel = 'spouse' AND other_id = ?
                             AND (date_end IS NULL OR date_end > ?)''',
                        (dmin, p1, p2, dmin),
                    )
                    conn.execute(
                        '''UPDATE relationships SET date_end = ?
                           WHERE person_id = ? AND rel = 'spouse' AND other_id = ?
                             AND (date_end IS NULL OR date_end > ?)''',
                        (dmin, p2, p1, dmin),
                    )
        elif ctype == 'death':
            for deceased_id in pids:
                conn.execute(
                    '''UPDATE relationships SET date_end = ?
                       WHERE rel = 'spouse' AND (person_id = ? OR other_id = ?)
                         AND (date_end IS NULL OR date_end > ?)''',
                    (dmin, deceased_id, deceased_id, dmin),
                )


# ── Full build ────────────────────────────────────────────────────────────────

def build_index(archive_root: Path, fha_config: dict, verbose: bool = False) -> Result:
    """Rebuild the index from scratch; return a Result summarizing the build.

    The `verbose` progress lines stay inline (they narrate each build step as it
    runs); the Result reports the build as data instead of only logs - per-table
    row counts and the schema version in `data`, the rebuilt cache file in
    `changed`, and `data['mode'] = 'full'` to distinguish this drop-and-rebuild
    from the incremental `upsert_source` path.  An in-process caller (e.g.
    `fha report`'s refresh) can read what was built without parsing the logs.
    """
    cache_dir = archive_root / '.cache'
    db_path = cache_dir / 'index.sqlite'

    status, _detail = sqlite_cache_schema_status(
        db_path, INDEX_SCHEMA_VERSION, ('persons', 'sources', 'claims'),
    )
    if status == 'unreadable':
        try:
            db_path.unlink()
        except FileNotFoundError:
            pass

    if verbose:
        print('Building index...')

    # Drop and recreate
    conn = _get_db(cache_dir)
    _drop_tables(conn)
    conn.close()   # release the OS file handle before reopening (Windows: a
                    # leaked handle here blocks anyone trying to delete/replace
                    # the .sqlite file, e.g. a tempdir-based test's cleanup)
    conn = _get_db(cache_dir)   # recreate tables after drop

    try:
        with conn:
            # Places. Coord-shape warnings are collected (not printed) so they
            # ride the Result to whichever front door ran the build.
            place_warnings = _index_places(conn, archive_root)
            if verbose:
                print('  indexed places')

            # People
            people_root = archive_root / 'people'
            person_count = 0
            if people_root.exists():
                for path in people_root.rglob('*.md'):
                    _index_person(conn, path, archive_root)
                    person_count += 1
            if verbose:
                print(f'  indexed {person_count} person files')

            # Resolve map for name-first frontmatter links - persons and places
            # are fully indexed (their names registered as aliases) before any
            # source's `people:`/`places:` is read. The explicit ('P', 'L')
            # filter is a no-op at this moment (nothing else is in the table
            # yet) but states the equivalence contract with upsert_source's
            # map, which reads a fully-populated table and NEEDS the filter to
            # build this same map (see _resolve_map_from_aliases).
            link_alias_map = _resolve_map_from_aliases(conn, record_types=('P', 'L'))

            # Sources
            sources_root = archive_root / 'sources'
            source_count = 0
            if sources_root.exists():
                for path in sources_root.rglob('*.md'):
                    _index_source(conn, path, archive_root, fha_config, link_alias_map)
                    source_count += 1
            if verbose:
                print(f'  indexed {source_count} source files')

            # Notes FTS
            _index_notes(conn, archive_root)

            # Capture log (durability: survives a search_log drop/rebuild)
            _index_capture_log(conn, archive_root)

            # Citation scan - the full alias map now includes source stems, so a
            # `[[grandmas-album]]` prose link resolves to its S-id and a cited
            # `[[C-…]]` registers its on-demand source alias.
            _index_citations(conn, archive_root, _resolve_map_from_aliases(conn))

            # Relationship derivation
            _derive_relationships(conn)
    finally:
        # Without this, every build_index call leaks one open sqlite3.Connection
        # (the `with conn:` context manager only commits/rolls back - it never
        # closes). On Windows that held-open file handle blocks anything trying
        # to delete or replace the .sqlite file afterward, e.g. a tempdir-based
        # test's cleanup, or `fha photoindex tag-person` writing right after a
        # `fha report` refresh in the same process.
        conn.close()

    if verbose:
        size_kb = db_path.stat().st_size // 1024
        print(f'Done. Index at {db_path} ({size_kb} KB)')

    # Warnings (today: malformed place coords) put the build on the documented
    # warnings exit path (§1: 1 = warnings only) without failing it - the human
    # must SEE that a hand-edited line was skipped, but the index is complete.
    return Result(
        exit_code=EXIT_WARNINGS if place_warnings else EXIT_CLEAN,
        data={
            'mode': 'full',
            'schema_version': INDEX_SCHEMA_VERSION,
            'persons': person_count,
            'sources': source_count,
            'db_path': str(db_path),
        },
        messages=[
            Message(level='warning', text=w, path='places/places.yaml')
            for w in place_warnings
        ],
        changed=[str(db_path)],
    )


def _find_source_file(archive_root: Path, sid: str) -> Path | None:
    """Locate the source record file for canonical source id `sid` by EXACT
    identity - its filename id (`{slug}_{S-id}.md`), or failing that its
    frontmatter `id`.  Returns None when no source matches.

    Exact matching (not the old `sid in path.stem` substring test) means a typo
    or a partial ID can never silently bind to the wrong file.
    """
    sources_root = archive_root / 'sources'
    if not sources_root.exists():
        return None
    # Primary: match the id embedded in the canonical filename (cheap, no parse).
    for path in sources_root.rglob('*.md'):
        parsed = parse_filename(path)
        if parsed and normalize_id(parsed.get('id_str', '')) == sid:
            return path
    # Fallback: match by frontmatter id (handles non-canonical filenames).
    for path in sources_root.rglob('*.md'):
        try:
            rec = read_record(path)
        except Exception:
            continue
        if normalize_id(str(rec.get('meta', {}).get('id', ''))) == sid:
            return path
    return None


def _require_existing_index(cache_dir: Path) -> bool:
    """
    Return True if a full index exists with the required core tables.

    Called by upsert_source() before any mutation: --source must never
    create the DB from scratch (that would produce a partial index with
    only one source's rows, missing persons/places/notes_fts).
    """
    db_path = cache_dir / 'index.sqlite'
    status, _detail = sqlite_cache_schema_status(
        db_path,
        INDEX_SCHEMA_VERSION,
        ('persons', 'sources', 'claims'),
    )
    if status != 'fresh':
        return False
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute('SELECT 1 FROM persons LIMIT 1')
            conn.execute('SELECT 1 FROM sources LIMIT 1')
            conn.execute('SELECT 1 FROM claims LIMIT 1')
        finally:
            conn.close()
        return True
    except Exception:
        return False


def upsert_source(archive_root: Path, fha_config: dict, source_id: str) -> str:
    """
    Incremental re-index of one source and its claims.

    Validates `source_id` and locates the matching source file by EXACT identity
    *before* mutating anything, so a typo or a stale ID never deletes rows or
    reports false success.  Returns one of:
      'indexed'       - the source was found and re-indexed.
      'not_found'     - no source under sources/ matches that exact ID.
      'invalid_id'    - source_id is not a syntactically valid S- ID.
      'index_absent'  - no full index exists; run `fha index` first.

    Deletion order matters: child tables must be deleted before their parent rows.
    citations references sources.path, so it is deleted before sources.

    Re-derives relationships after the upsert so the relationships table
    reflects any changed claim statuses.  Does not re-index persons or places
    - those only change on a full rebuild.
    """
    sid = normalize_id(source_id)
    if not is_valid_id(sid) or id_type_of(sid) != 'S':
        return 'invalid_id'

    found = _find_source_file(archive_root, sid)
    if found is None:
        return 'not_found'

    cache_dir = archive_root / '.cache'
    if not _require_existing_index(cache_dir):
        return 'index_absent'

    conn = _get_db(cache_dir)
    try:
        with conn:
            source_row = conn.execute('SELECT path FROM sources WHERE id=?', (sid,)).fetchone()
            source_path = source_row[0] if source_row else None

            existing_claim_ids = [
                row[0] for row in
                conn.execute('SELECT id FROM claims WHERE source_id=?', (sid,)).fetchall()
            ]
            if existing_claim_ids:
                placeholders = ','.join('?' * len(existing_claim_ids))
                conn.execute(f'DELETE FROM claim_persons WHERE claim_id IN ({placeholders})', existing_claim_ids)
                conn.execute(f'DELETE FROM claim_links WHERE claim_id IN ({placeholders})', existing_claim_ids)
            conn.execute('DELETE FROM claims WHERE source_id=?', (sid,))
            if source_path:
                conn.execute('DELETE FROM citations WHERE path=?', (source_path,))
                conn.execute('DELETE FROM notes_fts WHERE path=?', (source_path,))
            conn.execute('DELETE FROM sources WHERE id=?', (sid,))
            conn.execute('DELETE FROM source_files WHERE source_id=?', (sid,))
            conn.execute('DELETE FROM source_people WHERE source_id=?', (sid,))
            conn.execute('DELETE FROM source_places WHERE source_id=?', (sid,))
            # Drop this source's alias rows (its own id/stems and any on-demand
            # C-id aliases owned by it) so a renamed stem or removed citation
            # doesn't leave a stale resolution behind.
            conn.execute('DELETE FROM aliases WHERE canonical_id=?', (sid,))
            # Forward-safety: drop any transcript rows for this source so a future
            # transcript-indexing pass cannot leave stale FTS content behind.
            conn.execute('DELETE FROM transcripts_fts WHERE source_id=?', (sid,))

            # Resolve map for this source's name-first frontmatter links and
            # claims. Persons/places are unchanged on an upsert, so the
            # surviving alias rows already carry their names - but OTHER
            # sources' aliases survive here too, which the full build's map
            # never saw (it snapshots before any source is indexed). The
            # ('P', 'L') filter reduces this table to that same snapshot, so
            # a clashing alias on another record can't drop the
            # claim_persons/source_people rows the full build keeps (the
            # row-for-row equivalence contract, round-2 finding 8).
            link_alias_map = _resolve_map_from_aliases(conn, record_types=('P', 'L'))
            _index_source(conn, found, archive_root, fha_config, link_alias_map)
            # Re-scan citations for the re-indexed source file (resolving stems),
            # with the map refreshed to include this source's reinserted stems.
            _index_citations_for_file(
                conn, found, archive_root, _resolve_map_from_aliases(conn),
            )
            # Rebuild this source's on-demand C-id aliases from EVERY citation
            # site, not just its own file - a `[[C-…]]` to one of its claims may
            # live in a person profile we didn't rescan, and we just dropped the
            # alias row above.
            this_claims = {
                row[0] for row in conn.execute('SELECT id FROM claims WHERE source_id=?', (sid,))
            }
            cited_here = {
                row[0] for row in conn.execute("SELECT DISTINCT token FROM citations WHERE kind='C'")
            }
            _register_cited_claim_aliases(conn, cited_here & this_claims)

            _derive_relationships(conn)
    finally:
        # See build_index's matching comment: `with conn:` never closes the
        # connection, and a leaked handle blocks later deletion/replacement
        # of the .sqlite file (most visibly on Windows).
        conn.close()
    return 'indexed'


# ── CLI ───────────────────────────────────────────────────────────────────────

def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        'index',
        help='Rebuild the SQLite index from the archive tree',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root')
    p.add_argument(
        '--source', metavar='S-ID',
        help='Upsert only this source (incremental mode)',
    )
    p.add_argument('-v', '--verbose', action='store_true', help='Show progress')
    p.set_defaults(func=_run_index)


def _run_index(args: argparse.Namespace) -> int:
    """argparse → build_index / upsert_source bridge; returns the exit code.

    Root resolution (including the refusal of a typo'd --root that doesn't
    carry fha.yaml - which once minted an empty .cache/index.sqlite inside
    ANY folder and printed "Index rebuilt" with exit 0) lives in
    `_lib.resolve_root_arg`, the shared chokepoint every tool resolves
    through. The refusal happens before any .cache creation.
    """
    archive_root = resolve_root_arg(args, command='fha index')
    if archive_root is None:
        return EXIT_FAILURE

    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE

    if getattr(args, 'source', None):
        status = upsert_source(archive_root, fha_config, args.source)
        if status == 'invalid_id':
            print(
                f'ERROR: {args.source!r} is not a valid S- source ID.',
                file=sys.stderr,
            )
            return EXIT_FAILURE
        if status == 'not_found':
            print(
                f'ERROR: source {args.source} not found under sources/ - nothing indexed.',
                file=sys.stderr,
            )
            return EXIT_FAILURE
        if status == 'index_absent':
            print(
                'ERROR: incremental --source requires an existing full index; run `fha index` first.',
                file=sys.stderr,
            )
            return EXIT_FAILURE
        print(f'Upserted source {args.source}')
    else:
        db_path = archive_root / '.cache' / 'index.sqlite'
        status, detail = sqlite_cache_schema_status(
            db_path, INDEX_SCHEMA_VERSION, ('persons', 'sources', 'claims'),
        )
        if status in {'old-schema', 'unreadable'}:
            suffix = f' ({detail})' if detail else ''
            print(
                f'Index cache is out of date or unreadable{suffix}; rebuilding from archive files.'
            )
        result = build_index(archive_root, fha_config, verbose=getattr(args, 'verbose', False))
        # Render the Result's warnings (the _cmd layer's job): each already
        # names the record and the fix, per the next-step rule.
        for m in result.messages:
            print(f'WARNING: {m.text}', file=sys.stderr)
        if not getattr(args, 'verbose', False):
            print(f'Index rebuilt: {archive_root / ".cache" / "index.sqlite"}')
        return result.exit_code

    return EXIT_CLEAN


# ── Standalone ────────────────────────────────────────────────────────────────

def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha index',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--root', metavar='PATH')
    parser.add_argument('--source', metavar='S-ID')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args(argv)
    return _run_index(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
