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

Neither mode is allowed to hold less than the files do without saying so.  A
record folder that will not open, and a record whose YAML will not parse, both
mean the index is missing content the archive has - so both are collected
during the walk and reported at the end, and either one ends the run on the
warnings exit (1) rather than a clean 0.  The rows stay dropped; the silence
does not (#62).

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
    SEARCHABLE_TEXT_SUFFIXES,
    TEXT_COMPANION_ROLES,
    TOKEN_RE,
    FhaConfigError,
    Message,
    Result,
    carries_person_record_fields,
    edtf_bounds,
    extract_wikilinks,
    id_type_of,
    is_merged_meta,
    is_template_file,
    is_valid_id,
    is_working_copy,
    link_field_refs,
    load_fha_yaml,
    normalize_id,
    parentage_parties,
    parse_filename,
    person_file_kind,
    read_record,
    read_text_or_report,
    resolve_path,
    resolve_ref,
    resolve_root_arg,
    resolve_typed_ref,
    roots_change_orphans,
    format_roots_orphan_warning,
    spouse_parties,
    sqlite_cache_schema_status,
    strip_generational_suffix,
    strip_link_wrapper,
    undecodable_file_recorder,
    unreadable_dir_recorder,
    walk_files,
)

import yaml

# ── CODE MAP ──────────────────────────────────────────────────────────────────
#
#  Schema
#    _DDL                    - CREATE TABLE statements for all index tables
#
#  Unreadable records
#    _parse_error_recorder   - shared collector: a record whose YAML would not
#                              parse → one warning naming the file and the loss
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
#    _insert_parent_edges    - one claim's parent/child pairs → both directions
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
  lon REAL,
  notes TEXT
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

# _lib.TEXT_COMPANION_ROLES holds the files: roles whose body belongs in
# transcripts_fts: `extracted-text` (what `fha source extract` stamps on a PDF's
# dumped text layer - source.py's `_EXTRACT_ROLE`) and `transcript` /
# `transcription` (what a transcript written by any other means carries: by
# hand, by the transcribe-audio skill, by the transcribe-source skill reading a
# scan and typing it out). All of them are the same thing to a search - the
# archive's copy of what the evidence says - and loading only the extract verb's
# own dumps left every other transcript unsearchable through the index (#46).
# The vocabulary lives in _lib because `fha lint` counts the same files this
# loads, and one shared rule is what keeps the two from drifting apart.
#
# _lib.SEARCHABLE_TEXT_SUFFIXES is the second half of the gate: a transcript is
# only a transcript if it is text. A role tag can land on anything - a `.m4a`
# attached as `role: transcript` by a slip of the hand - and reading a media
# file as UTF-8 would fail on every build and print a "re-save it as UTF-8"
# warning naming a file that was never text at all.


# ── Unreadable records ────────────────────────────────────────────────────────
#
# `read_record` never raises on a malformed record: it hands back whatever it
# could parse and lists what it could not in `parse_errors`.  That is the right
# shape for a walker - one bad file must not take a whole build down - but it
# means a record whose YAML does not parse indexes as an EMPTY record, and for
# a source that is silent data loss: the claims block is dropped, so the claims
# and every relationship edge derived from them simply are not there, while the
# build reports success (#62).  A field report of this cost 16% of an archive's
# spouse edges with nothing on screen to say so.
#
# The claims stay dropped - unparseable YAML cannot be indexed, and guessing at
# it would be worse than losing it.  What changes is that the build now SAYS so,
# through the same collect-then-report seam the unreadable-folder warning uses:
# every record walk shares one recorder, and a non-empty list ends the build on
# the documented warnings exit (§1: 1 = warnings only).  Warning, not error: the
# index is built and usable, the fix is in the human's file rather than in the
# tools, and `fha index` already exits 1 for the strictly worse case of a folder
# it could not open at all.

_LOST_CLAIMS = (
    'its claims could not be read, so none of them are in the index - they '
    'will not appear in `fha find`, on a timeline, in `fha report` or in any '
    'export, and any relationship they record is missing from the family tree'
)

_LOST_SOURCE = (
    'this source could not be read, so it is not in the index at all - neither '
    'it nor its claims will appear in `fha find`, on a timeline, in `fha '
    'report` or in any export'
)

# Deliberately vaguer about the fields than the source phrasing: this text
# covers a person PROFILE (where the frontmatter carries the name, the vitals
# and the living flag) and its generated companions alike, and promising the
# reader a specific loss the file never held would be its own small lie.
_LOST_PERSON_FIELDS = (
    'its frontmatter could not be read, so nothing that block records - a '
    'person record keeps its name, vital dates, living flag and tier there - '
    'reached the index, and what it holds reads as blank wherever the index '
    'is queried'
)

_LOST_PERSON = (
    'this person record could not be read, so it is not in the index at all - '
    'the person will not appear in `fha find`, on a timeline, in `fha report` '
    'or in any export'
)


def _parse_error_recorder(
    collected: list[tuple[str, str, str]], archive_root: Path,
):
    """Return the callback the record walkers report an unreadable record through.

    Mirrors `_lib.unreadable_dir_recorder`: one recorder is shared by every walk
    in a build, so the Result can report "these records did not read" once,
    whichever tree they came from.  Each entry is (archive-relative path, code,
    text) - the path and the code travel separately from the prose because a
    Message carries both as fields, and because index output can end up in a
    committed `fha report`, where a local absolute path has no business.

    The code is `read_record`'s own (E010 today), passed through rather than
    written out here: this build is not minting a new finding, it is reporting
    the one `fha lint` already reports, and the two must never drift into
    describing the same broken file by different names.

    `lost` is the caller's phrase for what the failure actually cost, because
    only the caller knows: a source whose frontmatter parsed but whose claims
    did not is still in the index minus its claims, while one whose frontmatter
    failed is not there at all.  Naming the wrong loss would be worse than
    naming none.
    """
    def record(path: Path, code: str, detail: str, lost: str) -> None:
        rel = _archive_relative(path, archive_root)
        body = (
            f'{lost}. {detail}\n'
            f'Fix that spot in the file, then run `fha index` again - '
            f'`fha lint` reports the same problem ({code}) and points at it too.'
        )
        # Every line after the first is indented two spaces. `detail` carries a
        # worked example whose lines start with '- ', and `fha report` writes
        # these messages into a markdown bullet list: unindented, the example
        # would break out of its own bullet and read as four more findings.
        # The same indent is what makes it a readable block on a terminal.
        collected.append((rel, code, f'{rel}: ' + body.replace('\n', '\n  ')))

    return record


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
        notes_text = str(place.get('notes') or '').strip()
        conn.execute(
            'INSERT OR REPLACE INTO places(id, name, hierarchy, within, lat, lon, notes) '
            'VALUES (?,?,?,?,?,?,?)',
            (pid, place.get('name'), place.get('hierarchy'), place.get('within'),
             lat, lon, notes_text or None),
        )
        # Place research notes are prose like any other note, so they join
        # notes_fts too - the JSON/workbench search reads text hits ONLY from
        # there, and without this row a word that appears only in an `fha
        # places note` entry was undiscoverable the moment it was written
        # (P2 codex finding, round 4, PR #31). The registry file is the
        # honest path for every place's row; the ranked search dedupes text
        # hits by path, so a query matching several places' notes still
        # returns one places.yaml hit.
        if notes_text:
            conn.execute(
                'INSERT INTO notes_fts(path, content) VALUES (?,?)',
                ('places/places.yaml', notes_text),
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


def _index_person(
    conn: sqlite3.Connection,
    path: Path,
    archive_root: Path,
    on_parse_error=None,
    on_decode_error=None,
) -> None:
    """
    Index one person .md file into persons and person_files.

    Profile files (kind='profile') get a full persons row upsert.  Companion
    files (kind='timeline', 'sources-index', etc.) only get a person_files row
    - they don't create a second persons entry, but views can find them by
    person_id and kind.

    Which of the two a file is comes from its CONTENT first and its filename
    only as a fallback - see `_lib.carries_person_record_fields` for why the
    filename alone cannot answer the question.

    Surname is parsed from the filename's double-underscore convention
    ({surname}__{given}_{P-id}) rather than the name: field, because the
    frontmatter name may include middle names or honorifics while the filename
    slug is always the birth surname. A hand-authored, not-yet-minted record
    (SPEC §10's legal pre-machine state - no `__` in the stem yet) has no
    filename slug to read, so `surname` falls back to splitting `name:` with
    the same `_lib.strip_generational_suffix` rule `stub_slug_name` and
    `lint._person_filename_parts` use (issue #53), so a hand-typed "Roy
    Eugene Dodson Jr" indexes under Dodson even before its first `fha lint
    --fix-ids` rename - rather than staying surname-less, which is what this
    fallback used to do (it computed a name-based split and then never used
    it; see the fix commit).

    `on_parse_error`, when supplied, is the build's shared recorder (see
    `_parse_error_recorder`).  A person file whose frontmatter will not parse
    does not fail loudly here - it indexes with an empty `meta`, which means the
    name, the vital dates and the `living` flag are quietly gone, or (with no
    P-id in the filename to fall back on) the person is gone entirely.  Either
    way the human has to be told; the argument is optional so the incremental
    and test paths that pass no recorder still work.

    `on_decode_error` is the build's sibling recorder for the failure one step
    earlier: a record whose BYTES are not UTF-8 at all (#68).  #66 fixed the
    three note/log reads; this is the fourth and largest - every person record
    in the archive - and until it was wired the whole build still died with a
    traceback on one file saved in cp1252.  Reported through the same channel
    as the notes (`build_index`'s undecodable-files warning, which already
    says these words are not in the index and names the re-save), and the file
    is SKIPPED rather than indexed as an empty person: an empty `meta` here
    would put a nameless, date-less row in `persons` where a real ancestor
    belongs, which reads as a sparse record rather than as a file nobody read.
    """
    if path.suffix.lower() != '.md':
        # SPEC §13 spells every person record `.md`, and the walker only ever
        # hands us `.md` files.  The guard is here because `parse_filename`
        # reads the companion-kind slot for `.md` alone: give it any other
        # extension and it returns kind=None, which the fallback below would
        # turn into 'profile' - minting a full persons row (name 'unknown')
        # for a stray `.txt` or `.pdf` that happened to land under people/.
        return
    if is_template_file(path):
        return   # `_TEMPLATE.*` is a teaching template, not a record
    rec = read_record(path, on_decode_error=on_decode_error)
    if rec['undecodable']:
        return   # nothing was read; the build reports it (#68, see docstring)
    meta = rec['meta']
    parsed_name = parse_filename(path)

    # Identity: the frontmatter id first, the filename's P-id as the fallback.
    # Generated companions (timeline, sources-index, draft-queue) carry no
    # frontmatter id at all - the P-id lives in the filename instead - so the
    # fallback is what puts them in person_files and on `fha find`.
    #
    # Both are checked with `is_valid_id`, not a `p-` prefix test.  A hand-typed
    # `id: P-notanid` passed the prefix test and went straight into persons.id,
    # where it joins to nothing: claim_persons, relationships, citations and
    # every view key off a real Crockford id.  The person read as present in one
    # table and absent from every query built on it.  A malformed id is not an
    # identity, so fall through to the filename - the id the archive's existing
    # `[[P-…]]` links already use - and let `fha lint` E002 name the typo.
    pid = normalize_id(str(meta.get('id', '')))
    if not is_valid_id(pid) or id_type_of(pid) != 'P':
        pid = ''
        if parsed_name and parsed_name['id_type'] == 'P':
            pid = parsed_name['id_str']

    # Reported here rather than straight after read_record because only now do
    # we know which loss to name: the filename P-id fallback decides whether
    # this record lands in the index stripped of its frontmatter or not at all.
    if rec['parse_errors'] and on_parse_error is not None:
        lost = _LOST_PERSON_FIELDS if pid else _LOST_PERSON
        for code, detail in rec['parse_errors']:
            on_parse_error(path, code, detail, lost)

    if not pid:
        return

    name = str(meta.get('name', '')) or 'unknown'
    stem = path.stem
    # Content decides, the filename hints.  The rule itself lives in
    # `_lib.person_file_kind` - index and lint reading it differently is what
    # lost a person - and the reasoning is written out in
    # `_lib.carries_person_record_fields`: SPEC §13 puts the companion kind
    # immediately before the P-id (`hartley__thomas_timeline_P-…`), but
    # underscores are legal inside given names, so that slot is shared with the
    # last given name and the grammar cannot separate them.  A file whose
    # frontmatter carries the SPEC §9 person-record fields is a profile whatever
    # its stem says; a kind-suffixed stem with no such frontmatter is the
    # generated companion it looks like.
    #
    # Note the asymmetry: content can only promote a file TO a profile, never
    # demote one.  A profile-named file with sparse frontmatter (a stub carrying
    # just `id:`) stays a profile, which is what it is.
    is_person_record = carries_person_record_fields(meta)
    kind = person_file_kind(path, meta)

    is_companion = kind != 'profile'

    if not is_companion:
        # Primary profile - upsert person row
        surname = None
        if '__' in stem:
            # extract from filename: {surname}__{given...}
            surname_part = stem.split('__')[0]
            surname = surname_part.replace('_', ' ').title()
        elif name and name != 'unknown':
            # Pre-machine, not-yet-minted file (SPEC §10): no §13 filename
            # slug to read yet, so fall back to splitting name: with the
            # same suffix-aware rule the filename-writing sites use, rather
            # than leaving surname permanently None until the next
            # `fha lint --fix-ids` rename.
            core, _suffix = strip_generational_suffix(name.split())
            if len(core) >= 2:
                surname = core[-1].title()

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
        if is_merged_meta(meta):
            # A merged tombstone (SPEC §9) registers ONLY its bare P-id - the
            # same rule as _lib._record_alias_strings. Its name/variants/stems
            # were folded into the survivor by the merge; registering them
            # here too would clash every folded name out of the resolve map,
            # breaking the very [[Name]] links the fold preserved. The bare
            # id keeps resolving, and readers follow merged_into from there.
            _insert_record_aliases(conn, pid)
        else:
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
    # A file whose frontmatter names a person is never machine output, even when
    # its id has not been minted yet - a hand-authored record with no id is a
    # legal pre-machine state (SPEC §10), not a generated view.
    is_generated = not meta.get('id') and not is_person_record
    conn.execute(
        'INSERT OR REPLACE INTO person_files(person_id, kind, path, generated) VALUES (?,?,?,?)',
        (pid, kind, str(path.relative_to(archive_root)), 1 if is_generated else 0),
    )

    # FTS index the body.
    #
    # Generated companions are indexed here like any other person file, and
    # that is deliberate: a companion says things no record does. It resolves
    # `place_id: L-…` to the place's NAME and `[[S-…]]` to the source's title,
    # and it attaches all of it to ONE person - so a text search for a town
    # name lands on "Margaret's timeline" where the claim itself only carries
    # an ID. Dropping these bodies would narrow what `fha find --text` can
    # answer, which is why the rows are kept and maintained. The cost is that
    # these rows go stale invisibly - a companion is outside the freshness
    # watermark (#37), so nothing would prompt a rebuild after a regeneration.
    # `fha views` therefore maintains its own rows between rebuilds through
    # `_lib.sync_generated_view_rows`; this insert and that one must keep
    # deriving person_id, kind and generated the same way.
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
    on_parse_error=None,
    on_decode_error=None,
) -> None:
    """Index one source markdown file.

    `alias_map`, when supplied, resolves name-first frontmatter link fields
    (`people:`/`places:`) - e.g. `people: ["[[Ken Smith]]"]` → the matching
    P-id. Without it the fields are read the legacy bare-ID way, so this stays
    callable for the incremental and test paths that pass no map.

    `on_parse_error`, when supplied, is the build's shared recorder (see
    `_parse_error_recorder`).  This is the seam #62 was about: `read_record`
    reports a malformed `## Claims` block through `parse_errors` and hands back
    `claims: []`, so without this call the block is dropped and the build says
    nothing.  Optional for the same reason as `alias_map`.

    `on_decode_error` is the same seam one step earlier: a source file whose
    bytes will not decode as UTF-8 is skipped and reported, never indexed as
    an empty source (#68) - see `_index_person` for the full note.
    """
    if is_template_file(path):
        return   # `_TEMPLATE.*` is a teaching template, not a record
    rec = read_record(path, on_decode_error=on_decode_error)
    if rec['undecodable']:
        return   # nothing was read; the build reports it (#68)
    meta = rec['meta']

    sid = normalize_id(str(meta.get('id', '')))

    # Before the early return below, so a source whose FRONTMATTER failed (and
    # is therefore about to vanish whole) is reported too, not just one whose
    # claims failed. Which of those two happened decides the phrase.
    if rec['parse_errors'] and on_parse_error is not None:
        lost = _LOST_CLAIMS if sid.startswith('s-') else _LOST_SOURCE
        for code, detail in rec['parse_errors']:
            on_parse_error(path, code, detail, lost)

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

        # Text companion (role: transcript / transcription / extracted-text):
        # feed its body into transcripts_fts so JSON/workbench search reaches
        # inside what the evidence actually says. `fha source extract`'s success
        # message promises `fha index` makes its dump searchable, and this is
        # where that promise is kept - but a transcript written by any other
        # means is the same kind of text and earns the same treatment, which is
        # the whole point of #46: an archive can hold a full transcript of a
        # scan and still answer a search as though the scan were mute. This runs
        # inside _index_source, which BOTH build_index and upsert_source call,
        # so full-rebuild and incremental stay symmetric (upsert drops this
        # source's transcripts_fts rows first). Guarded on the file being on
        # disk: a working copy that never synced the companion simply has
        # nothing to read, and skipping is the graceful answer - a full build on
        # the main archive fills it in.
        suffix = Path(file_path.replace('\\', '/')).suffix.lower()
        if (role.strip().lower() in TEXT_COMPANION_ROLES
                and suffix in SEARCHABLE_TEXT_SUFFIXES
                and resolved.exists()):
            try:
                dump_text = resolved.read_text(encoding='utf-8')
            except OSError:
                dump_text = ''
            except UnicodeDecodeError:
                # A hand-edited or corrupted companion that is not valid UTF-8 must
                # NOT abort the whole build. UnicodeDecodeError is a ValueError, not
                # an OSError, so without this it escapes here; on a full rebuild the
                # source-indexing transaction then rolls back over an ALREADY-dropped
                # index, leaving a current-schema cache with zero records that later
                # readers accept as fresh. Skip the malformed dump with a named
                # warning - the rest of the source still indexes.
                print(f'WARNING: {file_path} is not valid UTF-8 and was skipped for '
                      'transcript search - re-save it as UTF-8, then re-run '
                      '`fha index`.', file=sys.stderr)
                dump_text = ''
            if dump_text.strip():
                conn.execute(
                    'INSERT INTO transcripts_fts(source_id, path, content) '
                    'VALUES (?,?,?)',
                    (sid, file_path, dump_text),
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


def _index_notes(conn: sqlite3.Connection, archive_root: Path, on_error=None,
                 on_decode_error=None) -> None:
    """Index notes files for FTS.

    `on_error` is the build's shared unreadable-folder recorder (see
    `build_index`): notes_fts is dropped and rewritten on every build, so a
    notes subfolder that will not list silently takes its research out of
    `fha find --text` with nothing said.
    """
    notes_dir = archive_root / 'notes'
    if not notes_dir.exists():
        return
    for path in walk_files(notes_dir, suffix='.md', on_error=on_error):
        try:
            content = path.read_text(encoding='utf-8')
        except OSError:
            continue
        except UnicodeDecodeError:
            # A note saved in the machine's own codepage rather than UTF-8 -
            # cp1252 is what a Windows editor writes by default, and the names
            # this archive is full of (Krakow, Muller, nee) are exactly the
            # characters that differ. Left uncaught this raised straight out of
            # build_index and took the WHOLE index down: nothing indexed at
            # all, a traceback for an answer. UnicodeDecodeError is a
            # ValueError, not an OSError, so the guard above never saw it -
            # which is why the sibling read at `dump_text` catches it by name
            # and these three did not.
            if on_decode_error is not None:
                on_decode_error(path)
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
        except UnicodeDecodeError:
            # Same class as the per-note read above; the research log is one
            # file, so losing it silently loses every search ever logged there.
            if on_decode_error is not None:
                on_decode_error(research_log_path)
            content = ''
        if content.strip():
            rel_path = str(research_log_path.relative_to(archive_root))
            _index_research_log_block(conn, content, None, rel_path)


def _index_capture_log(conn: sqlite3.Connection, archive_root: Path,
                       on_decode_error=None) -> None:
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
        except UnicodeDecodeError:
            # capture_log.jsonl is the ONLY record of captures made before this
            # index existed (`_drop_tables` clears search_log every build and
            # this replays it), so decoding it away silently loses that history
            # for good rather than until the next run.
            if on_decode_error is not None:
                on_decode_error(capture_log_path)
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
    on_error=None,
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

    `on_error` is the build's shared unreadable-folder recorder: the citations
    table is rebuilt from nothing each time, so a folder that will not list
    drops every `[[S-…]]` written inside it and `fha find --related` quietly
    shows fewer connections than the archive really holds.
    """
    from _lib import TOKEN_RE
    # archive_root/out/ is fha packet's default, gitignored output directory
    # (TOOLING §8) - disposable export copies, not archive truth, so they
    # must not become citation sites in the index. Only the root-level out/
    # is skipped - a record tree's own 'out' subdirectory (sources/out/, …)
    # is real archive content and must still be scanned.
    packet_out_root = archive_root / 'out'
    cited_cids: set[str] = set()
    for path in walk_files(archive_root, suffix='.md', on_error=on_error):
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


def _insert_parent_edges(
    conn: sqlite3.Connection, child_ids: list[str], parent_ids: list[str],
    cid: str, dmin: str | None, dmax: str | None,
) -> None:
    """Write one claim's parent/child edges, both directions, for every pair.

    Two branches of `_derive_relationships` mint parentage - a `relationship`
    claim and a `birth` claim - and they must produce byte-identical rows, or
    the same fact written two ways would reach the tree as two different
    shapes. One writer, so they cannot drift.

    Both directions always: the child's parent list and the parents' child list
    are read by different consumers (the Ahnentafel walk follows `parent`, the
    couple-folder bracket lists follow `child`), so an edge written one way
    only is half-invisible rather than merely wrong.

    The self-edge guard is belt and braces, the same one the spouse loops
    carry: `roles: {child: [P-a], parent: [P-a]}` is a typo, but nobody is
    their own parent, and a self-edge is exactly the shape lint cannot see
    (W126 needs two distinct people to speak) while every consumer reads it
    back as fact.
    """
    for child_id in child_ids:
        for parent_id in parent_ids:
            if child_id == parent_id:
                continue
            conn.execute(
                'INSERT OR IGNORE INTO relationships(person_id, rel, other_id, claim_id, date_start, date_end) VALUES (?,?,?,?,?,?)',
                (child_id, 'parent', parent_id, cid, dmin, dmax),
            )
            conn.execute(
                'INSERT OR IGNORE INTO relationships(person_id, rel, other_id, claim_id, date_start, date_end) VALUES (?,?,?,?,?,?)',
                (parent_id, 'child', child_id, cid, dmin, dmax),
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

    `birth` is in the claim types read here for the same reason `death` is: a
    vital record is evidence about the family graph, not only about one person.
    A birth record is in fact the plainest statement of parentage an archive
    ever holds - "born to X and Y" - and until issue #71 it contributed nothing
    to the pedigree even when its `roles:` map said so in as many words. It
    derives on `roles:` alone (`_lib.parentage_parties`); the `persons:` order
    of an unroled birth claim is never read as a parent list.
    """
    conn.execute('DELETE FROM relationships')

    # Negated claims record a researched negative - "these two did NOT marry",
    # "no death record found" (SPEC §8.6). They are accepted findings, but they
    # assert the absence of a bond, so they must never mint a relationship edge
    # nor end one: a negated marriage would otherwise create phantom spouse
    # edges, and a negated divorce/death would wrongly close a real spouse edge.
    # `negated` is stored 0/1 on the claims table (COALESCE guards legacy NULLs).
    rows = conn.execute(
        '''SELECT c.id, c.type, c.subtype, c.date_edtf, c.date_min, c.date_max
           FROM claims c
           WHERE c.status = 'accepted'
             AND COALESCE(c.negated, 0) = 0
             AND c.type IN ('relationship', 'birth', 'marriage', 'divorce', 'death')
           ORDER BY CASE c.type WHEN 'divorce' THEN 1 WHEN 'death' THEN 1 ELSE 0 END'''
    ).fetchall()

    for (cid, ctype, subtype, date_edtf, dmin, dmax) in rows:
        # One row per persons: ENTRY, and claim_persons has no UNIQUE
        # constraint - `persons: [P-a, "[[Alice Smith]]"]` is one person named
        # two ways and lands twice. Every derivation below asks "who does this
        # claim name", which is a question about people, so fold the duplicates
        # out once here: undeduplicated, a pairing loop pairs a person with
        # themselves (a self-marriage, a self-friendship) and every consumer
        # reads that edge back as fact.
        all_persons: list[tuple[str, str | None]] = []
        seen_pids: set[str] = set()
        for person_id, role in conn.execute(
                'SELECT person_id, role FROM claim_persons WHERE claim_id=? '
                'ORDER BY position', (cid,)):
            if person_id not in seen_pids:
                seen_pids.add(person_id)
                all_persons.append((person_id, role))
        pids = [p for p, r in all_persons]

        if ctype == 'relationship':
            # The edge's kind comes from the roles: map (the part each person
            # plays), not from subtype: - subtype now names the *nature* of a
            # parent/child bond (biological, adoptive, step, …; SPEC §8.2). A
            # claim naming a child and a parent is a parent/child edge whatever
            # its nature; legacy `subtype: child-of` claims still match because
            # they carry the same roles, and legacy `spouse-of` is caught by the
            # subtype fallback below.
            child_ids, parent_ids = parentage_parties(all_persons)
            spouse_ids = [p for p, r in all_persons if r == 'spouse']

            if child_ids and parent_ids:
                _insert_parent_edges(conn, child_ids, parent_ids, cid, dmin, dmax)
            elif spouse_ids or subtype == 'spouse-of':
                # A relationship claim naming spouses (or a legacy spouse-of
                # subtype) yields reciprocal spouse edges, like a marriage claim
                # - and obeys the same scoping rule (_lib.spouse_parties), for
                # the same reason. The two-person fallback inside that rule is
                # reached here by a claim whose roles: map resolves to fewer
                # than two spouses - a legacy `spouse-of` with no usable map, or
                # a map holding one typo'd id - and pairing off three or more
                # people in that state would invent marriages exactly as the
                # marriage branch used to.
                spouse_pids = spouse_parties(all_persons)
                for i, p1 in enumerate(spouse_pids):
                    for p2 in spouse_pids[i+1:]:
                        if p1 == p2:
                            continue   # nobody is married to themselves
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
        elif ctype == 'birth':
            # "Born to X and Y" - the plainest parentage evidence an archive
            # ever holds, and the natural place to write it down. It reaches
            # the pedigree exactly when the claim's roles: map says who was
            # born and to whom, through the same rule and the same writer the
            # relationship branch uses, so one fact written two ways cannot
            # arrive as two different shapes.
            #
            # Roles only, deliberately (_lib.parentage_parties). There is no
            # two-person fallback here as there is for marriage: parentage is
            # directed, `persons: [P-a, P-b]` does not say which of them was
            # born, and the second person on a birth register is as often an
            # informant or the attending physician as a parent. An unroled
            # birth claim therefore derives NOTHING - `fha lint` W126 reports
            # every one of them, so the silence is never the end of the story.
            child_ids, parent_ids = parentage_parties(all_persons)
            _insert_parent_edges(conn, child_ids, parent_ids, cid, dmin, dmax)
        elif ctype == 'marriage':
            # Only the people the claim calls spouses married each other. A
            # marriage certificate ordinarily names the couple AND both sets of
            # parents (six people), and listing all six in persons: is correct -
            # persons: is "who the claim is about" (SPEC §8.3). Pairing them off
            # blindly would make a man's father-in-law his spouse.
            # The p1 == p2 guard on this loop and its two siblings is belt and
            # braces: spouse_parties already returns each person once, and
            # all_persons is deduplicated above, so a self-marriage is
            # unreachable twice over. It stays because the insert is what
            # actually writes the tree - a future caller handing this loop a
            # repeated id must not be able to marry somebody to themselves.
            spouse_pids = spouse_parties(all_persons)
            for i, p1 in enumerate(spouse_pids):
                for p2 in spouse_pids[i+1:]:
                    if p1 == p2:
                        continue   # nobody is married to themselves
                    conn.execute(
                        'INSERT OR IGNORE INTO relationships VALUES (?,?,?,?,?,?)',
                        (p1, 'spouse', p2, cid, dmin, None),
                    )
                    conn.execute(
                        'INSERT OR IGNORE INTO relationships VALUES (?,?,?,?,?,?)',
                        (p2, 'spouse', p1, cid, dmin, None),
                    )
        elif ctype == 'divorce':
            # Same scoping rule, opposite failure. A divorce ENDS an edge
            # instead of minting one, so an unscoped pair loop does not invent
            # marriages - it closes real ones belonging to other people. A
            # decree naming the couple plus both sets of parents pairs each set
            # of parents with itself, and those two ARE married, so the UPDATE
            # lands and the parents' marriage is recorded as ending on their
            # child's divorce date. TOOLING §197 is explicit that date_end is
            # backfilled from a divorce claim *between the pair*.
            spouse_pids = spouse_parties(all_persons)
            for i, p1 in enumerate(spouse_pids):
                for p2 in spouse_pids[i+1:]:
                    if p1 == p2:
                        continue   # nobody is married to themselves
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

    # Every walk below shares one recorder, so the build reports "these
    # folders would not open" once, whichever tree they were in.
    unreadable_dirs: list[Path] = []
    on_error = unreadable_dir_recorder(unreadable_dirs)

    # The same idea one level down: a folder that would not open loses every
    # record in it, and a record whose YAML will not parse loses whatever that
    # YAML held. Both are "the index holds less than your files do", so both
    # ride one collector to one warning list.
    parse_warnings: list[tuple[str, str, str]] = []
    on_parse_error = _parse_error_recorder(parse_warnings, archive_root)
    # Files whose BYTES would not decode as UTF-8 - a different failure from a
    # record that decoded and then would not parse, and it needs its own list:
    # `_parse_error_recorder`'s message promises "`fha lint` reports the same
    # problem", and for these it says it its own way: lint reports the same
    # files as W128 (#68) rather than crashing on them as it once did. The
    # separate list stays because the REMEDY differs - a parse error is a fix
    # inside the record, this is a re-save of the whole file - and because the
    # message below has to name what the index lost, which lint's cannot.
    undecodable_files: list[Path] = []
    # The shared recorder, not a bare `list.append`: since the record walks
    # feed this too (#68), a file can now be reached by more than one pass in
    # one build, and the human should read its name once.
    on_decode_error = undecodable_file_recorder(undecodable_files)

    try:
        with conn:
            # Places. Coord-shape warnings are collected (not printed) so they
            # ride the Result to whichever front door ran the build.
            place_warnings = _index_places(conn, archive_root)
            if verbose:
                print('  indexed places')

            # People
            #
            # `walk_files` with a recorder, not rglob: this build DROPS every
            # table first, so a folder that will not list does not merely go
            # unread - the persons, claims and citations that live under it
            # disappear from the index while the build reports success. The
            # rows themselves are rebuildable from the records (the index is a
            # cache), but nothing would tell the human that half his tree had
            # gone quiet, and `photo_people` is derived from these very rows.
            # Collecting the folders lets the build say so and exit 1.
            people_root = archive_root / 'people'
            person_count = 0
            if people_root.exists():
                for path in walk_files(people_root, suffix='.md', on_error=on_error):
                    _index_person(conn, path, archive_root, on_parse_error,
                                  on_decode_error)
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
                for path in walk_files(sources_root, suffix='.md', on_error=on_error):
                    _index_source(conn, path, archive_root, fha_config,
                                  link_alias_map, on_parse_error, on_decode_error)
                    source_count += 1
            if verbose:
                print(f'  indexed {source_count} source files')

            # Notes FTS
            _index_notes(conn, archive_root, on_error, on_decode_error)

            # Capture log (durability: survives a search_log drop/rebuild)
            _index_capture_log(conn, archive_root, on_decode_error)

            # Citation scan - the full alias map now includes source stems, so a
            # `[[grandmas-album]]` prose link resolves to its S-id and a cited
            # `[[C-…]]` registers its on-demand source alias.
            _index_citations(
                conn, archive_root, _resolve_map_from_aliases(conn), on_error)

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

    # `fha index` is the one command every workflow runs right after editing
    # fha.yaml, so it is the earliest place a roots: change that orphaned
    # filed assets can be caught (#36) - before the next lint's wall of E011.
    roots_warnings = [
        format_roots_orphan_warning(item, archive_root)
        for item in roots_change_orphans(archive_root, fha_config)
    ]

    # A folder that would not list is the one warning here that means the
    # index is INCOMPLETE rather than merely imperfect, so it is worded that
    # way: name the folders, say what is missing from search until they open,
    # and give the command to run afterwards. The paths are archive-relative -
    # this text rides a Result that `fha report` writes into a committed
    # markdown file, and a local absolute path has no business in one.
    unreadable_warnings = []
    if unreadable_dirs:
        shown = ', '.join(
            _archive_relative(p, archive_root) for p in unreadable_dirs[:5])
        if len(unreadable_dirs) > 5:
            shown += f' and {len(unreadable_dirs) - 5} more'
        unreadable_warnings.append(
            f'{len(unreadable_dirs)} folder(s) could not be opened, so nothing '
            f'inside them was indexed: {shown}. Anything filed there will not '
            'appear in searches, on timelines, or in exports until it can be '
            'read. This is usually a folder whose permissions changed, or a '
            'drive or network share that is not connected - reconnect it (or '
            'restore your access), then run `fha index` again.'
        )
    if undecodable_files:
        shown = ', '.join(
            _archive_relative(p, archive_root) for p in undecodable_files[:5])
        if len(undecodable_files) > 5:
            shown += f' and {len(undecodable_files) - 5} more'
        unreadable_warnings.append(
            f'{len(undecodable_files)} file(s) are not saved as UTF-8 text, so '
            f'nothing in them was indexed: {shown}. A note will not be found by '
            '`fha find --text`; a research log or capture log that will not '
            'decode loses the searches recorded in it; and a person or source '
            'record that will not decode is absent from the index entirely - '
            'off timelines, out of searches and exports, and not counted in any '
            'report - until it can be read. The file itself is fine and nothing '
            'was changed - it was written in an older encoding (a Windows editor '
            'defaults to one, commonly cp1252). Re-save it as UTF-8, then run '
            '`fha index` again.'
        )

    # One row per record that would not parse, in walk order, without repeating
    # a file that produced two errors (a broken frontmatter AND a broken claims
    # block is one file to go and fix).
    unreadable_records: list[str] = []
    for rel, _code, _text in parse_warnings:
        if rel not in unreadable_records:
            unreadable_records.append(rel)

    # Warnings (today: malformed place coords, an orphaning roots: change, a
    # folder that would not open, a record whose YAML would not parse) put the
    # build on the documented warnings exit path (§1: 1 = warnings only)
    # without failing it - the human must SEE that a hand-edited line was
    # skipped, a folder went unread, or a claims block was dropped.
    return Result(
        exit_code=(EXIT_WARNINGS
                   if (place_warnings or roots_warnings or unreadable_warnings
                       or parse_warnings)
                   else EXIT_CLEAN),
        data={
            'mode': 'full',
            'schema_version': INDEX_SCHEMA_VERSION,
            'persons': person_count,
            'sources': source_count,
            'unreadable_dirs': [
                _archive_relative(p, archive_root) for p in unreadable_dirs],
            # Records whose YAML the build could not read, so the index holds
            # less than the files do until they are fixed (#62).
            'unreadable_records': unreadable_records,
            'db_path': str(db_path),
        },
        messages=[
            Message(level='warning', text=w, path='places/places.yaml')
            for w in place_warnings
        ] + [
            Message(level='warning', text=w, path='fha.yaml')
            for w in roots_warnings
        ] + [
            Message(level='warning', text=w) for w in unreadable_warnings
        ] + [
            Message(level='warning', text=text, code=code, path=rel)
            for rel, code, text in parse_warnings
        ],
        changed=[str(db_path)],
    )


def _archive_relative(path: Path, archive_root: Path) -> str:
    """A folder's name as the human filed it - 'people/003 Hartley', not /Users/….

    Index output can end up in a committed report, so it never carries a local
    absolute path. A folder somehow outside the archive keeps its own spelling
    (forward-slashed): naming it wrongly is worse than naming it long.
    """
    try:
        return Path(path).relative_to(archive_root).as_posix()
    except ValueError:
        return str(path).replace('\\', '/')


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


def upsert_source(
    archive_root: Path, fha_config: dict, source_id: str, on_parse_error=None,
) -> str:
    """
    Incremental re-index of one source and its claims.

    Validates `source_id` and locates the matching source file by EXACT identity
    *before* mutating anything, so a typo or a stale ID never deletes rows or
    reports false success.  Returns one of:
      'indexed'       - the source was found and re-indexed.
      'not_found'     - no source under sources/ matches that exact ID.
      'invalid_id'    - source_id is not a syntactically valid S- ID.
      'index_absent'  - no full index exists; run `fha index` first.
      'undecodable'   - the file was found but its bytes are not UTF-8, so
                        nothing could be read from it (#68).

    Deletion order matters: child tables must be deleted before their parent rows.
    citations references sources.path, so it is deleted before sources.

    Re-derives relationships after the upsert so the relationships table
    reflects any changed claim statuses.  Does not re-index persons or places
    - those only change on a full rebuild.

    `on_parse_error` is the same recorder the full rebuild uses (see
    `_parse_error_recorder`), and it is here because the two paths have to
    answer identically: an upsert of a source whose claims block will not parse
    DELETES that source's claim rows and re-inserts nothing, so the incremental
    path can lose a claims block just as silently as the full rebuild did
    (#62).  The return value stays the documented status string - callers
    branch on `'indexed'` - so the report travels through the recorder rather
    than through a widened return type.

    `'undecodable'` is the one failure that DOES widen the return type,
    because it has to be answered before the deletes rather than reported
    after them: a source saved in another codepage (#68) reads as nothing at
    all, so letting it reach the mutation below would delete the source, its
    claims, its citations and its FTS rows and re-insert none of them - the
    exact silent loss `on_parse_error` exists to prevent, one step earlier and
    with no record left to report against. It is checked immediately after the
    file is located, alongside the other three before-any-mutation guards.
    """
    sid = normalize_id(source_id)
    if not is_valid_id(sid) or id_type_of(sid) != 'S':
        return 'invalid_id'

    found = _find_source_file(archive_root, sid)
    if found is None:
        return 'not_found'

    # Before any mutation (see the docstring): a file whose bytes will not
    # decode is a file with nothing to re-insert, and the upsert deletes
    # first. Asked through `read_text_or_report` rather than `read_record`
    # because the question here is only "do these bytes decode" - an OSError
    # is a different answer (the file is gone or locked), and the walk below
    # already reports that its own way.
    _decode_failed: list[Path] = []
    read_text_or_report(found, on_decode_error=_decode_failed.append)
    if _decode_failed:
        return 'undecodable'

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
            _index_source(conn, found, archive_root, fha_config, link_alias_map,
                          on_parse_error)
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

# User-facing --help text (the module docstring stays developer-facing).
_CLI_DESCRIPTION = """\
Rebuild the search index so timelines, find, and reports see your latest edits.

  fha index                  Full rebuild (after adding or moving records)
  fha index --source S-xxxx  Update just one source (fast)

The index is a disposable cache, always rebuildable from your files."""


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        'index',
        help='Rebuild the SQLite index from the archive tree',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root')
    p.add_argument(
        '--source', metavar='S-ID',
        help='Upsert only this source (incremental mode)',
    )
    p.add_argument('-v', '--verbose', action='store_true',
                   help='Show progress (full rebuilds only; ignored with --source)')
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
        # Same seam as the full rebuild: collect, then render at this layer.
        parse_warnings: list[tuple[str, str, str]] = []
        status = upsert_source(
            archive_root, fha_config, args.source,
            _parse_error_recorder(parse_warnings, archive_root),
        )
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
        if status == 'undecodable':
            print(
                f'ERROR: source {args.source} is not saved as UTF-8 text, so nothing '
                'could be read from it and the index was left exactly as it was. '
                'The file itself is fine and nothing about it was changed - it is '
                'only saved in an older encoding (a Windows editor defaults to one, '
                'commonly cp1252). Open it and save it again choosing UTF-8 (in '
                'Notepad: Save As, then pick UTF-8 from the Encoding menu), then run '
                'this command again.',
                file=sys.stderr,
            )
            return EXIT_FAILURE
        print(f'Upserted source {args.source}')
        if parse_warnings:
            for _rel, _code, text in parse_warnings:
                print(f'WARNING: {text}', file=sys.stderr)
            return EXIT_WARNINGS
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
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--root', metavar='PATH')
    parser.add_argument('--source', metavar='S-ID')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args(argv)
    return _run_index(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
