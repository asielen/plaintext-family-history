#!/usr/bin/env python3
"""
index.py — fha index: build the SQLite query surface.

  fha index                  Full rebuild of .cache/index.sqlite from scratch
  fha index --source S-xxxx  Upsert one source (incremental, sub-second)

The index is a disposable SQLite cache — never authoritative, always rebuildable.
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
    — completes in under a second on a normal archive.

The schema lives in _DDL.  Foreign keys are OFF because the archive allows
forward references (a claim can reference a person whose file appears later in
the walk), and referential integrity is enforced by `fha lint` instead.
WAL mode is set for resilience: a crash during indexing leaves the previous
clean index readable rather than corrupting it.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    SIGNIFICANCE,
    CLAIM_TYPES,
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    ID_RE,
    TOKEN_RE,
    edtf_bounds,
    find_archive_root,
    load_fha_yaml,
    normalize_id,
    parse_filename,
    read_record,
    resolve_path,
)

import yaml

# ── CODE MAP ──────────────────────────────────────────────────────────────────
#
#  Schema
#    _DDL                    — CREATE TABLE statements for all index tables
#
#  Low-level DB helpers
#    _get_db                 — open (or create) the SQLite file, apply DDL
#    _drop_tables            — wipe all tables before a full rebuild
#
#  Indexers (one per record type)
#    _index_places           — places.yaml → places, place_names, place_history
#    _index_person           — one person .md → persons + person_files
#    _index_source           — one source .md → sources + claims + claim_persons
#                              + claim_links + source_files + source_people
#    _index_notes            — notes/*.md → notes_fts
#    _index_citations        — all .md → citations (token → file + line)
#
#  Derived tables
#    _derive_relationships   — accepted claims → relationships adjacency list
#
#  Top-level build functions
#    build_index             — full rebuild: drop, re-index everything, derive
#    upsert_source           — incremental: re-index one source, re-derive
#
#  CLI
#    register                — attach 'index' to the main fha parser
#    _run_index              — argparse → build_index / upsert_source bridge
#    _standalone_main        — for `python tools/index.py` direct invocation
#
# ─────────────────────────────────────────────────────────────────────────────


# ── DDL ───────────────────────────────────────────────────────────────────────
# Schema mirrors the SPEC record model plus derived tables for query speed.
# Foreign keys are OFF — forward references are valid and lint enforces integrity.
# WAL journal mode: a crash during indexing leaves the prior index readable.
# kind column in person_files: profile | research | timeline | sources-index | draft-queue

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=OFF;

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
  path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS person_variants(person_id TEXT, variant TEXT);
CREATE TABLE IF NOT EXISTS person_face_tags(person_id TEXT, tag TEXT);
CREATE TABLE IF NOT EXISTS person_files(
  person_id TEXT,
  kind TEXT,
  path TEXT,
  generated INTEGER DEFAULT 0,
  PRIMARY KEY(person_id, kind)
);
CREATE TABLE IF NOT EXISTS person_external(person_id TEXT, system TEXT, ext_id TEXT);

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
CREATE TABLE IF NOT EXISTS claim_persons(
  claim_id TEXT,
  person_id TEXT,
  position INTEGER,
  role TEXT
);
CREATE TABLE IF NOT EXISTS claim_links(
  claim_id TEXT,
  rel TEXT,
  target_id TEXT
);
CREATE TABLE IF NOT EXISTS source_people(source_id TEXT, person_id TEXT);

CREATE TABLE IF NOT EXISTS relationships(
  person_id TEXT,
  rel TEXT,
  other_id TEXT,
  claim_id TEXT,
  date_start TEXT,
  date_end TEXT
);

CREATE TABLE IF NOT EXISTS places(
  id TEXT PRIMARY KEY,
  name TEXT,
  hierarchy TEXT,
  within TEXT,
  lat REAL,
  lon REAL
);
CREATE TABLE IF NOT EXISTS place_names(place_id TEXT, alt_name TEXT);
CREATE TABLE IF NOT EXISTS place_history(
  place_id TEXT,
  period_edtf TEXT,
  date_min TEXT,
  date_max TEXT,
  hierarchy TEXT
);

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

CREATE TABLE IF NOT EXISTS citations(
  token TEXT,
  kind TEXT,
  path TEXT,
  line INTEGER
);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts
  USING fts5(path, content);
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
        'persons', 'person_variants', 'person_face_tags', 'person_files',
        'person_external', 'sources', 'source_files', 'claims', 'claim_persons',
        'claim_links', 'source_people', 'relationships', 'places', 'place_names',
        'place_history', 'search_log', 'hypotheses', 'citations',
        'notes_fts', 'transcripts_fts',
    ]
    for t in tables:
        conn.execute(f'DROP TABLE IF EXISTS {t}')
    conn.commit()


def _index_places(conn: sqlite3.Connection, archive_root: Path) -> None:
    places_path = archive_root / 'places' / 'places.yaml'
    if not places_path.exists():
        return
    try:
        with open(places_path, encoding='utf-8') as f:
            places = yaml.safe_load(f) or []
    except Exception:
        return

    for place in (places if isinstance(places, list) else []):
        if not isinstance(place, dict):
            continue
        pid = normalize_id(str(place.get('id', '')))
        if not pid:
            continue
        coords = place.get('coords', [None, None])
        lat = coords[0] if len(coords) > 0 else None
        lon = coords[1] if len(coords) > 1 else None
        conn.execute(
            'INSERT OR REPLACE INTO places(id, name, hierarchy, within, lat, lon) VALUES (?,?,?,?,?,?)',
            (pid, place.get('name'), place.get('hierarchy'), place.get('within'),
             lat, lon),
        )
        for alt in (place.get('alt_names') or []):
            conn.execute('INSERT INTO place_names(place_id, alt_name) VALUES (?,?)', (pid, alt))
        for h in (place.get('history') or []):
            if isinstance(h, dict):
                period = str(h.get('period', ''))
                mn, mx = edtf_bounds(period) if period else ('', '')
                conn.execute(
                    'INSERT INTO place_history(place_id, period_edtf, date_min, date_max, hierarchy) VALUES (?,?,?,?,?)',
                    (pid, period, mn, mx, h.get('hierarchy')),
                )


def _index_person(conn: sqlite3.Connection, path: Path, archive_root: Path) -> None:
    """
    Index one person .md file into persons and person_files.

    Profile files (kind='profile') get a full persons row upsert.  Companion
    files (kind='timeline', 'sources-index', etc.) only get a person_files row
    — they don't create a second persons entry, but views can find them by
    person_id and kind.

    Surname is parsed from the filename's double-underscore convention
    ({surname}__{given}_{P-id}) rather than the name: field, because the
    frontmatter name may include middle names or honorifics while the filename
    slug is always the birth surname.
    """
    rec = read_record(path)
    meta = rec['meta']

    pid = normalize_id(str(meta.get('id', '')))
    if not pid:
        # Generated companion files (timeline, sources-index, draft-queue) carry no
        # frontmatter id — the P-id lives in the filename instead.  Extract it so
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
        # Primary profile — upsert person row
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
                no_known_marriages, no_known_children, path)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
            (
                pid, name, surname,
                str(meta.get('sex', '')),
                living_val,
                str(meta.get('tier', 'stub')),
                str(meta.get('status', 'active')),
                normalize_id(str(meta.get('merged_into', ''))) or None,
                1 if meta.get('no_known_marriages') in (True, 'true') else 0,
                1 if meta.get('no_known_children') in (True, 'true') else 0,
                str(path.relative_to(archive_root)),
            ),
        )
        for v in (meta.get('name_variants') or []):
            conn.execute('INSERT INTO person_variants(person_id, variant) VALUES (?,?)', (pid, str(v)))
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


def _index_source(
    conn: sqlite3.Connection,
    path: Path,
    archive_root: Path,
    fha_config: dict,
) -> None:
    """Index one source markdown file."""
    rec = read_record(path)
    meta = rec['meta']

    sid = normalize_id(str(meta.get('id', '')))
    if not sid or not sid.startswith('s-'):
        return

    title = str(meta.get('title', ''))
    source_type = str(meta.get('source_type', ''))
    date_edtf = str(meta.get('source_date', ''))
    mn, mx = edtf_bounds(date_edtf) if date_edtf else ('', '')
    restricted = 1 if meta.get('restricted') in (True, 'true') else 0
    pub_ok = meta.get('rights', {})
    if isinstance(pub_ok, dict):
        pub_ok = 1 if pub_ok.get('publication_ok') in (True, 'true') else None
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

    # People listed on the source
    for p in (meta.get('people') or []):
        pid = normalize_id(str(p))
        if pid:
            conn.execute(
                'INSERT INTO source_people(source_id, person_id) VALUES (?,?)',
                (sid, pid),
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
        exists = 1 if resolved.exists() else 0

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
        place_id_raw = normalize_id(str(claim.get('place', ''))) or None

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

        # claim_persons
        persons_list = claim.get('persons') or []
        if isinstance(persons_list, str):
            persons_list = [persons_list]
        roles_map = claim.get('roles') or {}

        for pos, p_raw in enumerate(persons_list):
            ppid = normalize_id(str(p_raw))
            if not ppid:
                continue
            # Find role for this person from roles map
            role = None
            if isinstance(roles_map, dict):
                for role_name, role_val in roles_map.items():
                    if isinstance(role_val, list):
                        if ppid in [normalize_id(str(v)) for v in role_val]:
                            role = role_name
                            break
                    elif normalize_id(str(role_val)) == ppid:
                        role = role_name
                        break
            conn.execute(
                'INSERT INTO claim_persons(claim_id, person_id, position, role) VALUES (?,?,?,?)',
                (cid, ppid, pos, role),
            )

        # claim_links
        for link_type in ('corroborates', 'contradicts'):
            targets = claim.get(link_type) or []
            if isinstance(targets, str):
                targets = [targets]
            for t in targets:
                tid = normalize_id(str(t))
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


def _index_citations(conn: sqlite3.Connection, archive_root: Path) -> None:
    """Scan all .md files for [ID] citation tokens."""
    from _lib import TOKEN_RE
    for path in archive_root.rglob('*.md'):
        if '.cache' in path.parts:
            continue
        try:
            lines = path.read_text(encoding='utf-8', errors='ignore').splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, start=1):
            for m in TOKEN_RE.finditer(line):
                token = m.group(1).lower()
                kind = token[0].upper()
                conn.execute(
                    'INSERT INTO citations(token, kind, path, line) VALUES (?,?,?,?)',
                    (token, kind, str(path.relative_to(archive_root)), lineno),
                )


def _derive_relationships(conn: sqlite3.Connection) -> None:
    """
    Materialise relationship edges from accepted claims into the relationships table.

    This is a pre-computed adjacency list: rather than joining claim_persons on
    every query, known parent/child/spouse edges are written here so callers
    can ask "who are this person's parents?" with a simple SELECT.

    Called at the end of both full rebuild and incremental upsert so the table
    is always current.  Only accepted claims are used — suggested and
    needs-review claims don't become load-bearing graph edges.
    """
    conn.execute('DELETE FROM relationships')

    rows = conn.execute(
        '''SELECT c.id, c.type, c.subtype, c.date_edtf, c.date_min, c.date_max
           FROM claims c
           WHERE c.status = 'accepted'
             AND c.type IN ('relationship', 'marriage', 'divorce', 'death')'''
    ).fetchall()

    for (cid, ctype, subtype, date_edtf, dmin, dmax) in rows:
        all_persons = conn.execute(
            'SELECT person_id, role FROM claim_persons WHERE claim_id=?', (cid,)
        ).fetchall()
        pids = [p for p, r in all_persons]

        if ctype == 'relationship' and subtype == 'child-of':
            # child → parents
            child_ids = [p for p, r in all_persons if r == 'child']
            parent_ids = [p for p, r in all_persons if r == 'parent']
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
        elif ctype in ('relationship',) and subtype in _RELATIONSHIPS_SOCIAL_SUBTYPES:
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


# ── Full build ────────────────────────────────────────────────────────────────

def build_index(archive_root: Path, fha_config: dict, verbose: bool = False) -> None:
    """Rebuild the index from scratch."""
    cache_dir = archive_root / '.cache'

    if verbose:
        print('Building index...')

    # Drop and recreate
    conn = _get_db(cache_dir)
    _drop_tables(conn)
    conn = _get_db(cache_dir)   # recreate tables after drop

    with conn:
        # Places
        _index_places(conn, archive_root)
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

        # Sources
        sources_root = archive_root / 'sources'
        source_count = 0
        if sources_root.exists():
            for path in sources_root.rglob('*.md'):
                _index_source(conn, path, archive_root, fha_config)
                source_count += 1
        if verbose:
            print(f'  indexed {source_count} source files')

        # Notes FTS
        _index_notes(conn, archive_root)

        # Citation scan
        _index_citations(conn, archive_root)

        # Relationship derivation
        _derive_relationships(conn)

    if verbose:
        db_path = cache_dir / 'index.sqlite'
        size_kb = db_path.stat().st_size // 1024
        print(f'Done. Index at {db_path} ({size_kb} KB)')


def upsert_source(archive_root: Path, fha_config: dict, source_id: str) -> None:
    """
    Incremental re-index of one source and its claims.

    Deletes all existing rows for this source, then re-reads the source file
    from disk and inserts fresh rows.  Faster than a full rebuild when only
    one source file has changed.

    Deletion order matters: child tables must be deleted before their parent rows.
    citations references sources.path, so it is deleted before sources.

    Re-derives relationships after the upsert so the relationships table
    reflects any changed claim statuses.  Does not re-index persons or places
    — those only change on a full rebuild.
    """
    cache_dir = archive_root / '.cache'
    conn = _get_db(cache_dir)

    sid = normalize_id(source_id)
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

        # Find the source file
        sources_root = archive_root / 'sources'
        found = None
        if sources_root.exists():
            for path in sources_root.rglob('*.md'):
                if sid in path.stem.lower():
                    found = path
                    break

        if found:
            _index_source(conn, found, archive_root, fha_config)
            # Re-add citation tokens for the re-indexed source file.
            try:
                lines = found.read_text(encoding='utf-8', errors='ignore').splitlines()
            except OSError:
                lines = []
            rel = str(found.relative_to(archive_root))
            for lineno, line in enumerate(lines, start=1):
                for m in TOKEN_RE.finditer(line):
                    token = m.group(1).lower()
                    conn.execute(
                        'INSERT INTO citations(token, kind, path, line) VALUES (?,?,?,?)',
                        (token, token[0].upper(), rel, lineno),
                    )

        _derive_relationships(conn)


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
    root = getattr(args, 'root', None)
    if root:
        archive_root = Path(root).resolve()
    else:
        archive_root = find_archive_root()
        if archive_root is None:
            print('ERROR: cannot find archive root. Use --root.', file=sys.stderr)
            return EXIT_FAILURE

    fha_config = load_fha_yaml(archive_root)

    if getattr(args, 'source', None):
        upsert_source(archive_root, fha_config, args.source)
        print(f'Upserted source {args.source}')
    else:
        build_index(archive_root, fha_config, verbose=getattr(args, 'verbose', False))
        if not getattr(args, 'verbose', False):
            print(f'Index rebuilt: {archive_root / ".cache" / "index.sqlite"}')

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
