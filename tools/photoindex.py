#!/usr/bin/env python3
"""
photoindex.py - fha photoindex: photo metadata catalog.

  fha photoindex [--full]

Scrapes embedded metadata (exiftool) for every file under the photos root
into a disposable SQLite catalog, `.cache/photos.sqlite`, so the photo
library is searchable without opening Lightroom (or any other catalog tool).
TOOLING §9.

ARCHITECTURE OVERVIEW
----------------------
Photos are never renamed (Lightroom catalog integrity - SPEC §13) and most
are never "processed" into a source record at all, so the filename and the
embedded metadata are the only durable identity a photo carries. This tool
reads that metadata in one batched `exiftool -j` pass per scan and writes it
into a queryable cache, then derives two things on top of the raw rows:

  1. Variation grouping - fronts/backs/copies/negatives/crops of one
     physical photo are one logical photo. Group key is (in priority order)
     a shared `SOURCE:` S-id keyword, then same-directory + same filename
     base_id (`_lib.parse_media_filename`). Conservative by design: never
     groups across directories, never on caption similarity alone.
  2. Person resolution - bare `P-id` keywords (authoritative) → cached face
     region strings matched against `person_face_tags` → name/name_variant
     matches (weakest). The latter two require a fresh `.cache/index.sqlite`;
     absent, stale, or unreadable indexes degrade to pid-keyword-only
     resolution.

Scanning is incremental by (path, mtime, size): unchanged files are not
re-scraped via exiftool (the slow step). Face-region metadata is cached in
SQLite alongside keywords, so grouping, person resolution, and the FTS table
can be recomputed in full after every scan from already-scraped rows.

This PR (BUILD.md M3.4) adds `fha photoindex reconcile` and `fha photoindex
tag-person`, the two sub-commands that touch on-disk state or embedded
metadata, completing layer 3. `reconcile` heals drift between the catalog's
stored paths and what is actually on disk (a file moved outside `fha` is
re-matched by its embedded `SOURCE:` keyword, never by trusting the old
path); `tag-person` writes a bare `P-id` keyword into specific photos -
either explicit `--paths` or every photo carrying an ambiguous `--from-face-
tag` name - making a human identification durable in the file itself.

CODE MAP
--------
  Schema
    _DDL                      - CREATE TABLE statements for photos.sqlite

  exiftool integration
    PHOTO_EXTENSIONS          - recognised photo/scan file extensions (imported from _lib)
    _run_exiftool             - batched `exiftool -j -struct` invocation (test seam)
    _SOURCE_KEYWORD_RE, _PID_KEYWORD_RE, _DATE_KEYWORD_RE - keyword patterns
    _extract_keywords         - flatten Keywords+Subject into one string list
    _keyword_to_edtf          - confidence-coded DATE: keyword -> EDTF (SPEC §20)
    _extract_face_regions     - XMP-mwg-rs:RegionInfo -> list of (name, type, area)
    _row_to_photo             - one exiftool JSON row -> photos table column dict

  Person resolution
    _index_is_fresh           - schema-valid + not-older-than-newest-record check on index.sqlite
    _load_face_tag_index      - person_face_tags + persons name/variants from index.sqlite
    _load_source_people       - source_id -> [P-id] from source record people: lists (source-people tier)
    _resolve_photo_people     - pid-keyword / face-tag / name-match, in confidence order
    _rebuild_photo_people     - recompute photo_people from keywords + face regions + source-people

  Variation grouping
    _edtf_confidence          - sortable confidence score for an EDTF string (SPEC §20)
    _group_photos             - assign group_id/is_primary/variant_role; build photo_groups

  Query (fha photoindex find - M3.2)
    _paths_by_person/_keyword/_edtf/_text - one matching-path set per filter
    _group_keys_for           - map each path to its group key (group_id or singleton)
    _primary_path_for         - the primary_path of a group_id
    run_find                  - AND the requested filters at the group level;
                                 group-dedupe unless --files

  Triage (fha photoindex triage - M3.3)
    _candidate_groups         - groups with no source_id on any member (unprocessed)
    _score_group              - TOOLING §15b evidence-signal score for one group
    run_triage                - rank candidates, return top N

  Report (fha photoindex report - M3.3)
    run_report                - list photo_groups with date_conflict=1, all variants' dates/captions

  Reconcile (fha photoindex reconcile - M3.4)
    _on_disk_aliases          - alias path -> absolute Path for every file under the photos root
    _scrape_source_ids        - exiftool SOURCE: keyword read over untracked candidate files
    run_reconcile             - re-match moved files by source_id, flag the rest as MISSING

  Tag-person (fha photoindex tag-person - M3.4)
    run_tag_person_plan       - resolve candidate paths (read-only; CLI previews this before writing)
    _run_exiftool_write       - per-file `exiftool -keywords+=` write (the one path here that
                                 mutates an original photo file, not the cache); reports per-path
                                 success/failure rather than batching all candidates into one call
    _refresh_photo_fts_keywords - resync photo_fts.keywords for just-tagged paths
    apply_tag_person          - write the P-id keyword + update photo_keywords/photo_people/photo_fts

  Scan orchestration
    _get_db                   - open (or create) .cache/photos.sqlite, apply DDL
    run_scan                  - top-level: walk photos root, scrape, group, report

  CLI
    register                  - attach 'photoindex' to the main fha parser; wires 'find',
                                 'triage', 'report', 'reconcile', 'tag-person'
    _resolve_root_and_config  - shared --root/fha.yaml preamble for every _cmd_* handler
    _print_photoindex_status  - shared absent/unreadable/stale message + exit-code mapping
    _cmd_scan                 - argparse -> run_scan bridge
    _cmd_find                 - argparse -> run_find bridge
    _cmd_triage               - argparse -> run_triage bridge
    _cmd_report               - argparse -> run_report bridge
    _cmd_reconcile            - argparse -> run_reconcile bridge
    _cmd_tag_person           - argparse -> run_tag_person_plan -> preview/confirm -> apply_tag_person
    _standalone_main          - for `python tools/photoindex.py` direct invocation
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    CACHE_SCHEMA_KEY,
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    FhaConfigError,
    Result,
    configure_utf8_stdout,
    db_mtime,
    find_source_record,
    ParsedName,
    edtf_bounds,
    format_edtf_error,
    format_exiftool_error,
    grouping_stem as _grouping_stem,
    id_type_of,
    INDEX_SCHEMA_VERSION,
    is_valid_edtf,
    is_valid_id,
    load_fha_yaml,
    newest_person_record_mtime,
    normalize_date,
    normalize_id,
    parse_media_filename,
    select_variation_primary,
    variant_role as _variant_role,
    path_to_alias,
    PHOTO_EXTENSIONS,
    PHOTOINDEX_SCHEMA_VERSION,
    photoindex_status,
    is_working_copy,
    probe_sqlite,
    resolve_path,
    resolve_root_arg,
    scan_person_record_ids,
    sqlite_cache_schema_status,
)

configure_utf8_stdout()

# ── Schema (TOOLING §9 plus cached face regions) ─────────────────────────────

_DDL = f"""
PRAGMA user_version={PHOTOINDEX_SCHEMA_VERSION};

-- meta: cache identity and schema version; disposable, rebuilt by `fha photoindex`.
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY,       -- setting name, currently schema_version
  value TEXT NOT NULL         -- setting value, stored as text for readability
);
INSERT OR REPLACE INTO meta(key, value)
  VALUES ('{CACHE_SCHEMA_KEY}', '{PHOTOINDEX_SCHEMA_VERSION}');

-- photos: one cached metadata row per photo file alias.
CREATE TABLE IF NOT EXISTS photos(
  path TEXT PRIMARY KEY, mtime REAL, size INTEGER,
  title TEXT, caption TEXT, user_comment TEXT,
  exif_date TEXT, date_pattern TEXT, edtf TEXT,
  sublocation TEXT, city TEXT, state TEXT, country TEXT,
  gps_lat REAL, gps_lon REAL,
  source_id TEXT,
  group_id TEXT, is_primary INTEGER DEFAULT 0,
  variant_copy TEXT, variant_role TEXT
);
-- photo_groups: variation groups and the selected primary photo.
CREATE TABLE IF NOT EXISTS photo_groups(
  group_id TEXT PRIMARY KEY, primary_path TEXT,
  edtf_resolved TEXT, date_conflict INTEGER DEFAULT 0, file_count INTEGER
);
-- photo_keywords: flattened Keywords/Subject values for filtering.
CREATE TABLE IF NOT EXISTS photo_keywords(path TEXT, keyword TEXT);
-- photo_face_regions: cached face-region labels and geometry.
CREATE TABLE IF NOT EXISTS photo_face_regions(
  path TEXT, name TEXT, region_type TEXT, area_json TEXT
);
-- photo_people: resolved person references for photo search.
CREATE TABLE IF NOT EXISTS photo_people(path TEXT, person_ref TEXT, via TEXT);
-- photo_fts: full-text search over captions, comments, and keywords.
CREATE VIRTUAL TABLE IF NOT EXISTS photo_fts USING fts5(
  path, title, caption, user_comment, keywords
);
"""

_REQUIRED_SCHEMA = {
    'photos': {
        'path', 'mtime', 'size', 'title', 'caption', 'user_comment', 'exif_date',
        'date_pattern', 'edtf', 'sublocation', 'city', 'state', 'country',
        'gps_lat', 'gps_lon', 'source_id', 'group_id', 'is_primary',
        'variant_copy', 'variant_role',
    },
    'photo_groups': {'group_id', 'primary_path', 'edtf_resolved', 'date_conflict', 'file_count'},
    'photo_keywords': {'path', 'keyword'},
    'photo_face_regions': {'path', 'name', 'region_type', 'area_json'},
    'photo_people': {'path', 'person_ref', 'via'},
    'photo_fts': {'path', 'title', 'caption', 'user_comment', 'keywords'},
}

# ── exiftool integration ──────────────────────────────────────────────────

# PHOTO_EXTENSIONS now lives in _lib (shared with `fha process`); imported above.

_EXIFTOOL_FIELDS = [
    '-Title', '-Caption-Abstract', '-XMP-dc:Description', '-UserComment', '-DateTimeOriginal',
    '-Location', '-City', '-State', '-Country', '-GPSLatitude', '-GPSLongitude',
    '-Keywords', '-Subject', '-XMP-mwg-rs:RegionInfo',
]

_SOURCE_KEYWORD_RE = re.compile(r'^SOURCE:\s*(S-[0-9a-hjkmnp-tv-z]{10})$', re.I)
_PID_KEYWORD_RE = re.compile(r'^P-[0-9a-hjkmnp-tv-z]{10}$', re.I)
_DATE_KEYWORD_RE = re.compile(r'^DATE:\s*(.+)$')

# Keeps a single exiftool invocation's command line bounded on a large photo
# root - every batched call to _run_exiftool (full scan or reconcile rematch)
# uses this same chunk size.
_EXIFTOOL_BATCH_SIZE = 500


def _iter_photo_files(photos_root: Path):
    """Yield catalogable photo files under photos_root, or nothing if it is absent."""
    if not photos_root.is_dir():
        return
    for p in photos_root.rglob('*'):
        if p.is_file() and p.suffix.lower() in PHOTO_EXTENSIONS:
            yield p


def _run_exiftool(paths: list[Path]) -> list[dict]:
    """
    Run one batched `exiftool -j -struct` call over `paths` and return the
    parsed JSON rows.

    This is the seam a test harness replaces to inject pre-cooked JSON
    without requiring exiftool on PATH (BUILD.md layer-3 fixture note) -
    keep its signature (list of paths in, list of JSON-row dicts out)
    stable. `-n` forces numeric GPS output (default exiftool formats GPS as
    a DMS string); `-struct` is required for RegionInfo to come back as a
    nested object instead of a flattened/skipped field.
    """
    if not paths:
        return []
    cmd = ['exiftool', '-j', '-struct', '-n'] + _EXIFTOOL_FIELDS + [str(p) for p in paths]
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, encoding='utf-8')
    except FileNotFoundError as e:
        raise RuntimeError(format_exiftool_error('fha photoindex')) from e
    if proc.returncode != 0:
        raise RuntimeError(f'exiftool failed while scanning photos: {proc.stderr.strip()}')
    try:
        return json.loads(proc.stdout or '[]')
    except json.JSONDecodeError as e:
        raise RuntimeError(f'exiftool returned invalid JSON: {e}') from e


def _as_list(value: object) -> list[str]:
    """Normalize an exiftool scalar/list field value into a list of strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def _extract_keywords(row: dict) -> list[str]:
    """Keywords and Subject overlap heavily in practice; union them, dedup, keep order."""
    seen: list[str] = []
    for value in _as_list(row.get('Keywords')) + _as_list(row.get('Subject')):
        if value not in seen:
            seen.append(value)
    return seen


def _keyword_to_edtf(pattern: str) -> str | None:
    """
    Map a confidence-coded DATE: keyword body to an EDTF string (SPEC §20 table).

    The pipeline writes per-component confidence markers (`!` confident,
    `~` best guess, `?`/omitted unknown) directly on the digits, e.g.
    '1942!-11!-25!' or '1960~'. We stop building the EDTF string at the
    first component that is missing or marked '?' - SPEC §20 states 'Y!' is
    deliberately equivalent to 'Y!M?D?', i.e. an unconfirmed component is
    the same as an absent one, not a reason to guess. A bare digit string
    with no markers at all (e.g. a hand-typed '1880') is treated as fully
    confident, since most of the archive's DATE: keywords come from human
    transcription rather than the AI pipeline's marker syntax.
    """
    m = re.match(r'^(\d{4})([!~?])?(?:-(\d{2})([!~?])?(?:-(\d{2})([!~?])?)?)?$', pattern.strip())
    if not m:
        return pattern.strip() if is_valid_edtf(pattern.strip()) else None

    year, year_c, month, month_c, day, day_c = m.groups()
    if year_c == '?':
        return None

    # Collect the present, non-'?' components with their per-component markers.
    comps: list[tuple[str, str | None]] = [(year, year_c)]
    if month and month_c != '?':
        comps.append((month, month_c))
        if day and day_c != '?':
            comps.append((day, day_c))

    if len(comps) == 1:
        # Year only: an approximate year trails its qualifier (EDTF `1960~`).
        edtf = year + ('~' if year_c == '~' else '')
    else:
        # Multi-component: EDTF qualifies a component with a '~' written
        # immediately *before* it (SPEC §20: `Y!M~` -> `1960-~05`), so a
        # per-component best-guess marker is preserved on the right component
        # instead of being collapsed into one trailing '~' (or dropped when a
        # confident component follows the approximate one).
        edtf = '-'.join(('~' + comp if mark == '~' else comp) for comp, mark in comps)

    return edtf if is_valid_edtf(edtf) else None


def _extract_face_regions(row: dict) -> list[tuple[str, str, str | None]]:
    """
    Return [(name, type, area_json), ...] from XMP-mwg-rs:RegionInfo.

    Region area is cached as compact JSON text because exiftool's structure is
    metadata-provider dependent. Keeping it as JSON preserves the durable
    rectangle/shape details for later query/report work without freezing a
    premature column set into this first photoindex pass.
    """
    region_info = row.get('RegionInfo')
    if not isinstance(region_info, dict):
        return []
    regions = region_info.get('RegionList')
    if not isinstance(regions, list):
        return []
    out = []
    for r in regions:
        if isinstance(r, dict) and r.get('Name'):
            area = r.get('Area')
            area_json = (
                json.dumps(area, sort_keys=True, separators=(',', ':'), default=str)
                if area is not None else None
            )
            out.append((str(r['Name']), str(r.get('Type', '')), area_json))
    return out


def _row_to_photo(row: dict, mtime: float, size: int) -> dict:
    """Map one exiftool JSON row to the `photos` table's scraped (non-grouping) columns."""
    keywords = _extract_keywords(row)

    source_id = None
    for kw in keywords:
        m = _SOURCE_KEYWORD_RE.match(kw.strip())
        if m:
            source_id = normalize_id(m.group(1))
            break

    date_pattern = None
    edtf = None
    for kw in keywords:
        m = _DATE_KEYWORD_RE.match(kw.strip())
        if m:
            date_pattern = m.group(1).strip()
            edtf = _keyword_to_edtf(date_pattern)
            break

    lat = row.get('GPSLatitude')
    lon = row.get('GPSLongitude')

    return {
        'mtime': mtime,
        'size': size,
        'title': row.get('Title'),
        'caption': row.get('Caption-Abstract') or row.get('Description'),
        'user_comment': row.get('UserComment'),
        'exif_date': row.get('DateTimeOriginal'),
        'date_pattern': date_pattern,
        'edtf': edtf,
        'sublocation': row.get('Location'),
        'city': row.get('City'),
        'state': row.get('State'),
        'country': row.get('Country'),
        'gps_lat': float(lat) if isinstance(lat, (int, float)) else None,
        'gps_lon': float(lon) if isinstance(lon, (int, float)) else None,
        'source_id': source_id,
        '_keywords': keywords,
        '_face_regions': _extract_face_regions(row),
    }


# ── Person resolution ────────────────────────────────────────────────────

def _index_is_fresh(archive_root: Path) -> bool:
    """
    Return True only when .cache/index.sqlite is schema-valid and current.

    Bare P-id photo keywords do not need the archive index, but face-tag and
    name matching are weaker inferred matches. Using a stale index would write
    plausible-looking `photo_people` rows from old names/tags after a person
    record changed, so photoindex treats stale, absent, corrupt, and old-schema
    indexes the same way: skip weak resolution and keep scanning photos.
    """
    db_path = archive_root / '.cache' / 'index.sqlite'
    mtime = db_mtime(db_path)
    if mtime is None:
        return False
    status, _detail = sqlite_cache_schema_status(
        db_path,
        INDEX_SCHEMA_VERSION,
        ('persons', 'person_face_tags', 'person_variants'),
    )
    if status != 'fresh':
        return False
    for probe in (
        'SELECT 1 FROM persons LIMIT 1',
        'SELECT 1 FROM person_face_tags LIMIT 1',
        'SELECT 1 FROM person_variants LIMIT 1',
    ):
        if not probe_sqlite(db_path, probe):
            return False
    record_mtime = newest_person_record_mtime(archive_root)
    return record_mtime == 0.0 or mtime >= record_mtime


def _load_face_tag_index(archive_root: Path) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """
    Load (face_tag -> {person_id}) and (name/variant -> {person_id}) maps from
    a fresh .cache/index.sqlite, for resolving the weaker two confidence tiers.

    Returns ({}, {}) if the index is absent, stale, unreadable, or old-schema:
    pid-keyword resolution (the strongest tier, regex-only, no index needed)
    still works without it. Degrading the other two tiers to "find nothing"
    rather than crashing matches the rest of the suite's cache handling, and
    avoids writing weak matches from stale person metadata.
    """
    db_path = archive_root / '.cache' / 'index.sqlite'
    if not _index_is_fresh(archive_root):
        return {}, {}
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db_path))
        face_tags: dict[str, set[str]] = {}
        for tag, pid in conn.execute('SELECT tag, person_id FROM person_face_tags'):
            face_tags.setdefault(tag, set()).add(pid)

        names: dict[str, set[str]] = {}
        for name, pid in conn.execute('SELECT name, id FROM persons'):
            if name:
                names.setdefault(name, set()).add(pid)
        for variant, pid in conn.execute('SELECT variant, person_id FROM person_variants'):
            if variant:
                names.setdefault(variant, set()).add(pid)
        return face_tags, names
    except sqlite3.Error:
        return {}, {}
    finally:
        if conn is not None:
            conn.close()


def _resolve_photo_people(
    keywords: list[str],
    face_regions: list[tuple[str, str]],
    face_tags: dict[str, set[str]],
    names: dict[str, set[str]],
) -> list[tuple[str, str]]:
    """
    Resolve a photo's person references in confidence order (SPEC §20 / TOOLING §9):
    bare P-id keyword (authoritative) -> face-region name matched exactly against
    person_face_tags (skip if ambiguous) -> name/name_variant match (weakest).

    A tag matching more than one person is never guessed at this layer - it's
    surfaced (by being absent here) for `fha photoindex tag-person` to settle
    by hand in a later PR.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(pid: str, via: str) -> None:
        if pid not in seen:
            out.append((pid, via))
            seen.add(pid)

    for kw in keywords:
        kw = kw.strip()
        if _PID_KEYWORD_RE.match(kw):
            add(normalize_id(kw), 'pid-keyword')

    matched_names = {n for n, _ in face_regions}
    resolved_via_face_tags: set[str] = set()
    for region_name, _region_type in face_regions:
        pids = face_tags.get(region_name)
        if pids:
            resolved_via_face_tags.add(region_name)
            if len(pids) == 1:
                add(next(iter(pids)), 'face-tag')

    for region_name in matched_names - resolved_via_face_tags:
        pids = names.get(region_name)
        if pids and len(pids) == 1:
            add(next(iter(pids)), 'name-match')

    return out


def _load_keywords_by_path(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Bulk-load photo_keywords into one path -> [keyword, ...] map (one query, not one per path)."""
    by_path: dict[str, list[str]] = {}
    for path, kw in conn.execute('SELECT path, keyword FROM photo_keywords ORDER BY path, rowid'):
        by_path.setdefault(path, []).append(kw)
    return by_path


def _load_face_regions_by_path(conn: sqlite3.Connection) -> dict[str, list[tuple[str, str]]]:
    """Bulk-load photo_face_regions into one path -> [(name, region_type), ...] map."""
    by_path: dict[str, list[tuple[str, str]]] = {}
    for path, name, region_type in conn.execute(
        'SELECT path, name, region_type FROM photo_face_regions ORDER BY path, rowid'
    ):
        by_path.setdefault(path, []).append((name, region_type))
    return by_path


# Priority 0 = authoritative (pid-keyword embeds the P-id directly in the file;
# source-people means the source record's `people:` field names the person).
# Higher numbers are weaker inferences; face-tag and name-match both require
# face regions (XMP-mwg-rs) and a fresh .cache/index.sqlite.
_VIA_PRIORITY = {'pid-keyword': 0, 'source-people': 0, 'face-tag': 1, 'name-match': 2}


def _load_alias_resolver(archive_root: Path) -> dict[str, str]:
    """Clash-aware {alias_lower -> canonical_id} map from a fresh .cache/index.sqlite.

    Mirrors `fha index`'s name resolution (`_resolve_map_from_aliases`): an alias
    that names >=2 distinct records is OMITTED, so an ambiguous `[[Name]]` resolves
    to nothing rather than silently picking one record (SPEC §7). The `aliases`
    table lives in index.sqlite, NOT the photos.sqlite connection the caller holds,
    so it is opened here. Returns {} when the index is absent/stale/unreadable -
    callers then resolve only bare P-id `people:` entries (no index needed).
    """
    if not _index_is_fresh(archive_root):
        return {}
    db_path = archive_root / '.cache' / 'index.sqlite'
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db_path))
        idx: dict[str, set[str]] = {}
        for alias, cid in conn.execute('SELECT alias, canonical_id FROM aliases'):
            idx.setdefault(alias, set()).add(cid)
        return {a: next(iter(ids)) for a, ids in idx.items() if len(ids) == 1}
    except sqlite3.Error:
        return {}
    finally:
        if conn is not None:
            conn.close()


def _load_source_people(
    conn: sqlite3.Connection, archive_root: Path,
) -> dict[str, list[str]]:
    """
    Return a {source_id -> [person_id, ...]} map loaded from source record files.

    Reads the `people:` list from every source record whose S-id appears in the
    catalog's `photos.source_id` column. This is the `source-people` resolution
    tier: a person named in the source record's `people:` field is authoritative
    evidence that the person appears in the source, equivalent in confidence to a
    bare `pid-keyword` written to the image file. It works even when exiftool was
    never run after `fha process --people`, or when the user manually edited the
    source record after initial processing.

    Bare-P-id entries resolve without an index; name-style `[[Name]]` entries are
    resolved through the clash-aware alias map from index.sqlite (an ambiguous or
    unresolvable name draws no edge, matching `fha index`).

    Returns an empty dict when no photos carry source_ids, or when source records
    are absent or unparseable - callers fall back gracefully to keyword-only resolution.
    """
    source_ids = {
        row[0]
        for row in conn.execute('SELECT DISTINCT source_id FROM photos WHERE source_id IS NOT NULL')
    }
    if not source_ids:
        return {}
    alias_map = _load_alias_resolver(archive_root)
    result: dict[str, list[str]] = {}
    for sid in source_ids:
        rec = find_source_record(archive_root, sid)
        if rec is None:
            continue
        pids = []
        for p in (rec.get('meta') or {}).get('people') or []:
            if not p:
                continue
            raw = str(p).strip()
            # Support bare P-ids and [[P-id]] / [[Person Name]] wikilink forms.
            if raw.startswith('[[') and raw.endswith(']]'):
                raw = raw[2:-2].split('|')[0].strip()
            if id_type_of(raw) == 'P':
                pids.append(normalize_id(raw))
            else:
                # Name-style target: resolve through the clash-aware alias map
                # (the aliases table is in index.sqlite, not this connection).
                cid = alias_map.get(raw.lower())
                if cid and id_type_of(cid) == 'P':
                    pids.append(normalize_id(cid))
        if pids:
            result[normalize_id(sid)] = pids
    return result


def _rebuild_photo_people(conn: sqlite3.Connection, archive_root: Path) -> None:
    """
    Recompute photo_people for every cached photo from SQLite metadata.

    Keywords and face regions both come from the photo files, while face-tag
    and name matching come from the archive index. Rebuilding this derived
    table in one pass keeps incremental scans equivalent to full scans: a
    changed person record can alter weak matches without forcing exiftool to
    re-read unchanged image files.

    Person matches are aggregated at the group level (TOOLING §9): grouped
    photos are one logical photo, so a match found on any variant (e.g. a
    P-id keyword on the back of a print) is recorded for every variant in
    the group, not just the path that carries it. When variants disagree on
    `via` for the same person, the most confident one wins.

    Resolution tiers in confidence order (all authoritative - priority 0):
      pid-keyword   - bare P-id keyword embedded directly in the image file
      source-people - person listed in the source record's `people:` field

    Weaker inferred tiers (require face regions + a fresh index.sqlite):
      face-tag    - face region name matched against person_face_tags
      name-match  - face region name matched against person name/variants

    Bulk pid-keyword tagging is incremental work (TOOLING §9): most of the
    archive starts out resolved only via the weaker `face-tag`/`name-match`
    tiers, and stays that way until someone works through it by hand. A
    stale `.cache/index.sqlite` already means new weak matches can't be
    trusted (`_load_face_tag_index` returns nothing for that case), but the
    weak matches an earlier, fresher rebuild already recorded are not
    re-derived here at all - wiping them out on every rebuild until the
    index catches up would erase real, already-screened identifications
    just because some *other* photo's tagging triggered this rebuild. So a
    stale index keeps every existing non-`pid-keyword`/`source-people` row
    as-is and only refreshes the authoritative tiers.
    """
    index_fresh = _index_is_fresh(archive_root)
    face_tags, names = _load_face_tag_index(archive_root)
    keywords_by_path = _load_keywords_by_path(conn)
    face_regions_by_path = _load_face_regions_by_path(conn)
    source_people = _load_source_people(conn, archive_root)

    # source_id stored in the photos table uses normalize_id (lowercase).
    source_id_by_path: dict[str, str | None] = {
        row[0]: row[1]
        for row in conn.execute('SELECT path, source_id FROM photos')
    }

    preserved_by_path: dict[str, list[tuple[str, str]]] = {}
    if not index_fresh:
        for path, pid, via in conn.execute(
            "SELECT path, person_ref, via FROM photo_people "
            "WHERE via != 'pid-keyword' AND via != 'source-people'"
        ):
            preserved_by_path.setdefault(path, []).append((pid, via))

    conn.execute('DELETE FROM photo_people')

    matches_by_path: dict[str, list[tuple[str, str]]] = {}
    paths_by_group: dict[str, list[str]] = {}
    for path, group_id in conn.execute('SELECT path, group_id FROM photos ORDER BY path'):
        keywords = keywords_by_path.get(path, [])
        face_regions = face_regions_by_path.get(path, [])
        resolved = _resolve_photo_people(keywords, face_regions, face_tags, names)
        # source-people tier: people listed in the source record are authoritative
        # even when no P-id keyword has been embedded in the file yet - the source
        # record is the human's canonical "who is in this photo" statement.
        source_id = source_id_by_path.get(path)
        if source_id and source_id in source_people:
            resolved_pids = {pid for pid, _via in resolved}
            for pid in source_people[source_id]:
                if pid not in resolved_pids:
                    resolved.append((pid, 'source-people'))
                    resolved_pids.add(pid)
        if not index_fresh and path in preserved_by_path:
            resolved_pids = {pid for pid, _via in resolved}
            resolved = resolved + [
                (pid, via) for pid, via in preserved_by_path[path] if pid not in resolved_pids
            ]
        matches_by_path[path] = resolved
        key = group_id if group_id is not None else path
        paths_by_group.setdefault(key, []).append(path)

    for paths in paths_by_group.values():
        best: dict[str, str] = {}
        for path in paths:
            for pid, via in matches_by_path[path]:
                current = best.get(pid)
                if current is None or _VIA_PRIORITY[via] < _VIA_PRIORITY[current]:
                    best[pid] = via
        for path in paths:
            for pid, via in best.items():
                conn.execute(
                    'INSERT INTO photo_people(path, person_ref, via) VALUES (?,?,?)',
                    (path, pid, via),
                )


# ── Variation grouping ───────────────────────────────────────────────────

def _edtf_confidence(edtf: str | None) -> tuple[int, int]:
    """
    Sortable confidence score for an EDTF string: more present components
    (day > month > year) beats fewer, and an unmarked (fully confident)
    component beats '~' beats '?' (SPEC §20). Used to pick a group's
    best-confidence date among its variants' individually-resolved dates.
    """
    if not edtf:
        return (-1, 0)
    n_components = edtf.count('-') + 1
    if '?' in edtf:
        marker_rank = 2
    elif '~' in edtf:
        marker_rank = 1
    else:
        marker_rank = 0
    return (n_components, -marker_rank)


def _group_photos(conn: sqlite3.Connection) -> None:
    """
    Recompute group_id/is_primary/variant_copy/variant_role for every photo,
    and rebuild photo_groups, from the current `photos` rows.

    Grouping key, in priority order (TOOLING §9): (1) a shared SOURCE: S-id -
    files already processed into one source are one logical photo regardless
    of name; (2) same directory + same filename base_id after stripping the
    *recognized* suffix grammar (_lib.parse_media_filename, excluding
    freeform roles - see _grouping_stem) - the pipeline's own variation
    convention. Always run over every row (not just changed ones): grouping
    is cheap pure-SQL/Python and a partial re-group after an incremental scan
    would silently miss a newly-added sibling joining an existing group.
    """
    rows = conn.execute('SELECT path, source_id, edtf FROM photos').fetchall()
    parsed_by_path: dict[str, ParsedName] = {}
    edtf_by_path: dict[str, str | None] = {}
    stem_key_by_path: dict[str, str] = {}
    source_id_by_path: dict[str, str | None] = {}

    for path, source_id, edtf in rows:
        p = Path(path)
        parsed = parse_media_filename(p.stem)
        parsed_by_path[path] = parsed
        edtf_by_path[path] = edtf
        source_id_by_path[path] = source_id
        stem_key_by_path[path] = f'{p.parent.as_posix()}:{_grouping_stem(parsed)}'

    # A SOURCE: S-id keyword wins for every file sharing a stem group, not
    # just the file(s) that carry the keyword themselves - a front/back/crop
    # set is one logical photo even when only one member has been processed
    # into a source record.
    source_id_by_stem_key: dict[str, str] = {}
    for path, source_id in source_id_by_path.items():
        if source_id:
            source_id_by_stem_key.setdefault(stem_key_by_path[path], source_id)

    groups: dict[str, list[str]] = {}
    for path in parsed_by_path:
        stem_key = stem_key_by_path[path]
        resolved_source_id = source_id_by_path[path] or source_id_by_stem_key.get(stem_key)
        key = f'SOURCE:{resolved_source_id}' if resolved_source_id else f'STEM:{stem_key}'
        groups.setdefault(key, []).append(path)

    conn.execute('DELETE FROM photo_groups')

    for group_id, paths in groups.items():
        # Primary = a file with no variant/role/crop suffix at all (the plain
        # scan), then a front scan, then the lexicographically-first path when
        # every member carries some other suffix (e.g. a back-only group).
        # Shared with `fha process` so both tools agree on the primary file.
        primary = select_variation_primary(paths, parsed_by_path.__getitem__)

        # Order candidates with the primary file first, then lexicographically,
        # so a confidence tie (e.g. two variants both 'year only, no marker')
        # resolves deterministically to the primary's date rather than
        # insertion order - and falls back to path order only when the
        # primary itself carries no date.
        dated = sorted(
            ((edtf_by_path[p], p) for p in paths if edtf_by_path[p]),
            key=lambda pair: (pair[1] != primary, pair[1]),
        )
        edtfs = [e for e, _path in dated]
        best_edtf = max(dated, key=lambda pair: _edtf_confidence(pair[0]))[0] if dated else None

        date_conflict = 0
        bounds = [edtf_bounds(e) for e in edtfs]
        for i in range(len(bounds)):
            for j in range(i + 1, len(bounds)):
                if bounds[i][1] < bounds[j][0] or bounds[j][1] < bounds[i][0]:
                    date_conflict = 1

        conn.execute(
            'INSERT INTO photo_groups(group_id, primary_path, edtf_resolved, '
            'date_conflict, file_count) VALUES (?,?,?,?,?)',
            (group_id, primary, best_edtf, date_conflict, len(paths)),
        )

        for path in paths:
            parsed = parsed_by_path[path]
            # Negatives are stored at the stem level regardless of any variant
            # letter in their filename (TOOLING §9) - a negative is source
            # material for the root image, not an A/B variant of the print.
            variant_copy = None if parsed.part_kind == 'negative' else parsed.variant_id
            conn.execute(
                'UPDATE photos SET group_id=?, is_primary=?, variant_copy=?, variant_role=? '
                'WHERE path=?',
                (group_id, 1 if path == primary else 0, variant_copy,
                 _variant_role(parsed), path),
            )


# ── Query (fha photoindex find - BUILD.md M3.2) ──────────────────────────

def _paths_by_person(conn: sqlite3.Connection, person_id: str) -> set[str]:
    return {
        row[0] for row in conn.execute(
            'SELECT path FROM photo_people WHERE person_ref = ?', (person_id,)
        )
    }


def _paths_by_keyword(conn: sqlite3.Connection, term: str) -> set[str]:
    """Case-insensitive substring match against cached keywords."""
    like = f'%{term.lower()}%'
    return {
        row[0] for row in conn.execute(
            'SELECT path FROM photo_keywords WHERE LOWER(keyword) LIKE ?', (like,)
        )
    }


def _paths_by_edtf(conn: sqlite3.Connection, query: str) -> set[str]:
    """Bounds-overlap filter (TOOLING §1 edtf_bounds) against each photo's own edtf.

    The query string must itself be valid EDTF. edtf_bounds() silently widens an
    unparseable string to the open range 0001..9999, which would turn a typo like
    --edtf banana into a match-every-dated-photo query; reject it up front instead.
    """
    query = normalize_date(query) or query
    if not is_valid_edtf(query):
        raise ValueError(format_edtf_error(query, field='--edtf'))
    q_min, q_max = edtf_bounds(query)
    out: set[str] = set()
    for path, edtf in conn.execute('SELECT path, edtf FROM photos WHERE edtf IS NOT NULL'):
        r_min, r_max = edtf_bounds(edtf)
        if r_min <= q_max and r_max >= q_min:
            out.add(path)
    return out


def _paths_by_text(conn: sqlite3.Connection, query: str) -> set[str]:
    """Full-text search over the metadata columns --text documents.

    photo_fts also indexes `path` so the table can be rebuilt from one row per
    file, but `--text` is documented as searching title/caption/comment/keywords
    only; an unscoped MATCH would let a descriptive filename or folder produce a
    hit whose metadata never mentions the term. Restrict the match to the four
    metadata columns with an FTS5 column filter.

    --text is a metadata search, not an FTS expression: the CLI does not ask the
    user to know FTS5 syntax. Each whitespace-separated token is therefore quoted
    as a literal phrase (doubling embedded quotes) so punctuation like `-`, `:`,
    `"`, `*` or bare AND/OR/NOT is matched literally instead of parsed as an
    operator - `--text Smith-Jones` and `--text P-de957bcda1` find those strings
    rather than raising. Tokens are space-joined, preserving implicit-AND across
    words; a whitespace-only query matches nothing.
    """
    tokens = query.split()
    if not tokens:
        return set()
    quoted = ' '.join('"' + token.replace('"', '""') + '"' for token in tokens)
    scoped = f'{{title caption user_comment keywords}} : ({quoted})'
    try:
        return {
            row[0] for row in conn.execute(
                'SELECT path FROM photo_fts WHERE photo_fts MATCH ?', (scoped,)
            )
        }
    except sqlite3.OperationalError as e:
        raise RuntimeError(f'--text query is not valid FTS syntax: {e}') from e


def _group_keys_for(conn: sqlite3.Connection, paths: set[str]) -> dict[str, object]:
    """Map each path to its group key for group-level set operations.

    A grouped photo's key is its `group_id` (a string); an ungrouped photo is its
    own singleton, keyed by the tuple ('path', path) so it can never collide with
    a real group_id. Used to AND filters at the logical-photo level - see run_find.
    """
    keys: dict[str, object] = {}
    for path in paths:
        row = conn.execute('SELECT group_id FROM photos WHERE path=?', (path,)).fetchone()
        group_id = row['group_id'] if row and row['group_id'] else None
        keys[path] = group_id if group_id else ('path', path)
    return keys


def _primary_path_for(conn: sqlite3.Connection, group_id: str) -> str | None:
    row = conn.execute(
        'SELECT primary_path FROM photo_groups WHERE group_id=?', (group_id,)
    ).fetchone()
    return row['primary_path'] if row else None


def _query_photoindex(
    archive_root: Path,
    fha_config: dict,
    empty_payload: dict,
    query,
) -> Result:
    """
    Run a read-only query against an existing photos.sqlite with shared cache handling.

    `photoindex_status` owns the documented absent/unreadable/stale classification,
    while `_schema_is_usable` catches older compatible-looking caches that are
    missing columns a specific command needs. The callback may still raise
    ValueError/RuntimeError for user-facing validation failures; only sqlite
    errors are folded into the standard unreadable-cache result.

    Returns a `Result` whose `data` is {'status': ..., **payload}; Result exposes
    dict-style access (_lib.py), so callers keep reading `result['status']` /
    `result['candidates']` unchanged.  The `_cmd_*` layer derives the process exit
    code from the status, so this read helper leaves exit_code at its clean default.
    """
    status, _lag = photoindex_status(archive_root, fha_config)
    # An absent/unreadable index is a failure the CLI maps to EXIT_FAILURE; set
    # the same code here so headless callers returning Result.exit_code don't
    # read a missing/corrupt photos.sqlite as success.
    if status in ('absent', 'unreadable'):
        return Result(ok=False, exit_code=EXIT_FAILURE, data={'status': status, **empty_payload})

    conn = sqlite3.connect(str(archive_root / '.cache' / 'photos.sqlite'))
    conn.row_factory = sqlite3.Row
    try:
        if not _schema_is_usable(conn):
            return Result(ok=False, exit_code=EXIT_FAILURE,
                          data={'status': 'unreadable', **empty_payload})
        try:
            return Result(data={'status': status, **query(conn)})
        except sqlite3.Error:
            return Result(ok=False, exit_code=EXIT_FAILURE,
                          data={'status': 'unreadable', **empty_payload})
    finally:
        conn.close()


def run_find(
    archive_root: Path,
    fha_config: dict,
    person: str | None = None,
    keyword: str | None = None,
    edtf: str | None = None,
    text: str | None = None,
    files: bool = False,
) -> Result:
    """
    Apply the requested filters (AND'd together when more than one is given)
    and return a Result whose data is {'status': photoindex_status, 'rows': [...]}.

    Filters are AND'd at the GROUP level, not the raw-path level: two filters may
    each match a different variant of one physical photo (e.g. --edtf hits the
    front scan's DATE keyword while --text hits the back scan's caption), and those
    variants are a single logical hit. Intersecting raw paths would wrongly drop
    such a group, so each filter's hits are widened to their groups before the AND.

    Default output is one row per group (the group's primary_path) - search
    results and packet-style gathering always treat variants of one physical
    photo as a single hit (TOOLING §9). `--files` instead returns every raw row
    of each matched group, including sibling variants that did not themselves
    match a filter (the matched group, not the matched path, is the unit).

    photoindex_status only probes that the cache's tables exist, not that every
    selected column does; an older or partially written schema can therefore pass
    the freshness gate. _schema_is_usable is checked up front so an incompatible
    cache is reported as 'unreadable' even when the query would short-circuit to
    an empty result before touching the missing columns; any residual sqlite error
    inside the query is mapped to the same status as a final backstop, so the
    caller reports the documented rebuild message instead of leaking a traceback.
    """
    status, _lag = photoindex_status(archive_root, fha_config)
    if status in ('absent', 'unreadable'):
        return Result(ok=False, exit_code=EXIT_FAILURE, data={'status': status, 'rows': []})

    conn = sqlite3.connect(str(archive_root / '.cache' / 'photos.sqlite'))
    conn.row_factory = sqlite3.Row
    try:
        if not _schema_is_usable(conn):
            return Result(ok=False, exit_code=EXIT_FAILURE, data={'status': 'unreadable', 'rows': []})
        try:
            filters: list[set[str]] = []
            if person:
                filters.append(_paths_by_person(conn, normalize_id(person)))
            if keyword:
                filters.append(_paths_by_keyword(conn, keyword))
            if edtf:
                filters.append(_paths_by_edtf(conn, edtf))
            if text:
                filters.append(_paths_by_text(conn, text))
            if not filters:
                raise ValueError('at least one of --person/--keyword/--edtf/--text is required')

            all_paths: set[str] = set().union(*filters)
            group_key = _group_keys_for(conn, all_paths)

            group_sets = [{group_key[p] for p in f} for f in filters]
            matched_groups = group_sets[0]
            for gs in group_sets[1:]:
                matched_groups &= gs
            if not matched_groups:
                return Result(data={'status': status, 'rows': []})

            if files:
                # --files lists every raw row of each matched group, including
                # variants that did not themselves match a filter: the group is
                # the unit of matching and all its files are one physical photo.
                matched: set[str] = set()
                for key in matched_groups:
                    if isinstance(key, str):     # real group - pull all members
                        matched.update(
                            row[0] for row in conn.execute(
                                'SELECT path FROM photos WHERE group_id=?', (key,)
                            )
                        )
                    else:                        # singleton - the path itself
                        matched.add(key[1])
                paths = sorted(matched)
            else:
                qualifying = {p for p in all_paths if group_key[p] in matched_groups}
                primaries: set[str] = set()
                for path in qualifying:
                    key = group_key[path]
                    if isinstance(key, str):     # grouped - collapse to the group primary
                        primaries.add(_primary_path_for(conn, key) or path)
                    else:                        # singleton - the path is its own primary
                        primaries.add(path)
                paths = sorted(primaries)

            rows = []
            for path in paths:
                row = conn.execute(
                    'SELECT path, title, caption, edtf, source_id, group_id, is_primary, '
                    'variant_role FROM photos WHERE path=?', (path,)
                ).fetchone()
                if row:
                    rows.append(dict(row))
            return Result(data={'status': status, 'rows': rows})
        except sqlite3.Error:
            return Result(ok=False, exit_code=EXIT_FAILURE, data={'status': 'unreadable', 'rows': []})
    finally:
        conn.close()


# ── Triage (fha photoindex triage - BUILD.md M3.3) ───────────────────────

_AI_COMMENT_RE = re.compile(r'^\s*(AI|Model):', re.I)


def _candidate_groups(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """
    Groups with no `source_id` on any member - TOOLING §15b's 'unprocessed' set.

    Deliberately `NOT EXISTS`, not `NOT IN (SELECT group_id FROM photos ...)`:
    a `NOT IN` against a subquery that returns even one NULL makes the whole
    comparison UNKNOWN for every row, silently emptying the candidate list.
    `_group_photos` never writes a NULL `group_id` after a real scan (every
    group key is a non-empty f-string), but this guards a malformed/external
    cache row regardless.

    A group made up entirely of 'MISSING:'-prefixed rows (reconcile's
    bookkeeping for a vanished file, kept around so a later --with-exif
    retry can rematch it) is excluded: it has no on-disk asset a human could
    process, so surfacing it would send `fha process` after a synthetic path.
    """
    return conn.execute(
        '''
        SELECT pg.group_id, pg.primary_path, pg.file_count
        FROM photo_groups pg
        WHERE NOT EXISTS (
          SELECT 1 FROM photos p
          WHERE p.group_id = pg.group_id AND p.source_id IS NOT NULL
        )
        AND EXISTS (
          SELECT 1 FROM photos p2
          WHERE p2.group_id = pg.group_id AND p2.path NOT LIKE ?
        )
        ''',
        (f'{_MISSING_PREFIX}%',),
    ).fetchall()


def _members_by_group(conn: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    """Bulk-load every photo, grouped by group_id - one query, not one per group."""
    by_group: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute(
        'SELECT group_id, path, caption, user_comment, edtf, variant_role FROM photos'
    ):
        by_group.setdefault(row['group_id'], []).append(row)
    return by_group


def _pid_keyword_paths(conn: sqlite3.Connection) -> set[str]:
    """Every path with an authoritative pid-keyword person match - one query, not one per group."""
    return {
        row[0] for row in conn.execute(
            "SELECT path FROM photo_people WHERE via='pid-keyword'"
        )
    }


def _score_group(
    members: list[sqlite3.Row], pid_keyword_paths: set[str]
) -> tuple[int, list[str]]:
    """
    Score one candidate group per TOOLING §15b's evidence signals and return
    (score, [signal, ...]) for display.

    All signals are evaluated across every variant in the group (TOOLING §9:
    grouped photos are one logical photo) - a caption transcribed onto the
    back of a print scores the whole group, not just that file. `members` and
    `pid_keyword_paths` are bulk-loaded once by the caller (see
    `_members_by_group`/`_pid_keyword_paths`) so scoring N groups costs no
    extra SQL round-trips.
    """
    score = 0
    signals: list[str] = []

    has_caption = any(m['caption'] for m in members)
    if has_caption:
        score += 3
        signals.append('caption')

    has_pid_keyword = any(m['path'] in pid_keyword_paths for m in members)
    if has_pid_keyword:
        score += 2
        signals.append('pid-keyword')

    confident_date = any(
        m['edtf'] and _edtf_confidence(m['edtf'])[1] == 0 for m in members
    )
    if confident_date:
        score += 1
        signals.append('date:Y!+')

    has_back = any(m['variant_role'] and m['variant_role'].startswith('back') for m in members)
    if has_back:
        score += 1
        signals.append('back-variant')

    ai_only = (not has_caption) and any(
        m['user_comment'] and _AI_COMMENT_RE.match(m['user_comment']) for m in members
    )
    if ai_only:
        score -= 2
        signals.append('ai-only')

    return score, signals


def run_triage(archive_root: Path, fha_config: dict, top: int = 10) -> Result:
    """
    Rank unprocessed photo groups (no `source_id`) by evidence signals
    (TOOLING §15b) and return a Result whose data is {'status': ..., 'candidates': [...]}.

    Each candidate is {'path': primary_path, 'score': int, 'signals': [...]}.
    Consumed almost entirely through the report's triage section (BUILD.md
    M5.3); this command is the standalone, directly-callable form.
    """
    if top < 1:
        raise ValueError('--top must be a positive integer')

    def query(conn: sqlite3.Connection) -> dict:
        members_by_group = _members_by_group(conn)
        pid_keyword_paths = _pid_keyword_paths(conn)
        scored = []
        for group in _candidate_groups(conn):
            score, signals = _score_group(
                members_by_group.get(group['group_id'], []), pid_keyword_paths
            )
            scored.append({
                'path': group['primary_path'],
                'score': score,
                'signals': signals,
            })
        scored.sort(key=lambda c: (-c['score'], c['path']))
        return {'candidates': scored[:top]}

    return _query_photoindex(archive_root, fha_config, {'candidates': []}, query)


# ── Report (fha photoindex report - BUILD.md M3.3) ────────────────────────

def run_report(archive_root: Path, fha_config: dict) -> Result:
    """
    List every photo_groups row with `date_conflict=1` - a date disagreement
    between variants of one physical photo (e.g. front vs. back) is a research
    finding worth a question, not a value to silently average (TOOLING §9).

    Returns a Result whose data is {'status': ..., 'conflicts': [{'group_id',
    'primary_path', 'photos': [{'path', 'edtf', 'caption'}, ...]}, ...]}.
    """
    def query(conn: sqlite3.Connection) -> dict:
        # One join, not one photos query per conflicted group.
        rows = conn.execute(
            '''
            SELECT pg.group_id, pg.primary_path, p.path, p.edtf, p.caption
            FROM photo_groups pg JOIN photos p ON p.group_id = pg.group_id
            WHERE pg.date_conflict = 1
            ORDER BY pg.group_id, p.path
            '''
        ).fetchall()
        conflicts: dict[str, dict] = {}
        for row in rows:
            group = conflicts.setdefault(row['group_id'], {
                'group_id': row['group_id'],
                'primary_path': row['primary_path'],
                'photos': [],
            })
            group['photos'].append({
                'path': row['path'], 'edtf': row['edtf'], 'caption': row['caption'],
            })
        return {'conflicts': list(conflicts.values())}

    return _query_photoindex(archive_root, fha_config, {'conflicts': []}, query)


# ── Reconcile (fha photoindex reconcile - BUILD.md M3.4) ─────────────────

_MISSING_PREFIX = 'MISSING:'
_RECONCILE_TABLES = (
    'photos', 'photo_keywords', 'photo_face_regions', 'photo_people', 'photo_fts',
)


def _move_cached_path(conn: sqlite3.Connection, old_path: str, new_path: str) -> None:
    """
    Rename one cached photo path across every path-keyed table, including
    `photo_fts` (an `UPDATE` against an FTS5 table re-indexes the row in
    place, same as for an ordinary table) - otherwise `fha find --text`
    would keep matching the photo's pre-reconcile path indefinitely.

    Reconcile moves path text as cache maintenance, not source-truth editing.
    Keeping the table list in one helper makes rematch and mark-missing use the
    same mutation, including the `photo_groups.primary_path` mirror.
    """
    for table in _RECONCILE_TABLES:
        conn.execute(f'UPDATE {table} SET path=? WHERE path=?', (new_path, old_path))
    conn.execute(
        'UPDATE photo_groups SET primary_path=? WHERE primary_path=?',
        (new_path, old_path),
    )


def _on_disk_aliases(photos_root: Path, fha_config: dict, archive_root: Path) -> dict[str, Path]:
    """Map alias-form path -> absolute Path for every photo file currently on disk."""
    out: dict[str, Path] = {}
    for p in _iter_photo_files(photos_root):
        try:
            out[path_to_alias(p, 'photos', fha_config, archive_root)] = p
        except OSError:
            continue
    return out


def _scrape_source_ids(paths: list[Path]) -> dict[Path, str]:
    """
    Read the SOURCE: keyword (if any) off each candidate file via exiftool,
    keyed by the candidate's resolved Path.

    Used only by reconcile's re-match step: an untracked file's *content*
    identity (its embedded SOURCE: keyword) is the only reliable way to tell
    whether it is a missing cached photo that moved, since its new filename
    carries no S-id (photos are never renamed - SPEC §13).
    """
    out: dict[Path, str] = {}
    for start in range(0, len(paths), _EXIFTOOL_BATCH_SIZE):
        batch = paths[start:start + _EXIFTOOL_BATCH_SIZE]
        rows_by_file = {
            Path(row['SourceFile']).resolve(): row
            for row in _run_exiftool(batch) if row.get('SourceFile')
        }
        for p in batch:
            row = rows_by_file.get(p.resolve())
            if not row:
                continue
            for kw in _extract_keywords(row):
                m = _SOURCE_KEYWORD_RE.match(kw.strip())
                if m:
                    out[p] = normalize_id(m.group(1))
                    break
    return out


def run_reconcile(
    archive_root: Path, fha_config: dict, with_exif: bool = False, dry_run: bool = False,
) -> Result:
    """
    Heal drift between the catalog's stored paths and what is actually on disk
    (TOOLING §9 reconciliation). A photo's stored path is a refreshable cache,
    never its identity - identity rides in the embedded SOURCE: keyword (or,
    failing that, nothing photoindex can verify), so a row whose path no
    longer exists on disk is handled three ways:

      - re-matched (only with --with-exif): an untracked on-disk file's own
        SOURCE: keyword equals a missing row's -> the row's path (and its
        photo_keywords/photo_face_regions/photo_people rows) move to the new
        path silently. Without --with-exif there is no way to read a
        candidate's embedded keyword, so no re-match is attempted. Rows
        already flagged 'MISSING:' from an earlier run remain eligible here
        too, so re-running with --with-exif after fixing/restoring the photo
        can still heal them.
      - flagged missing: no source_id to verify against, --with-exif was not
        given, or no untracked file matched -> the row is kept (its caption/
        keyword history stays queryable) but its path is prefixed
        'MISSING:' so it can never be mistaken for a still-valid path. A row
        already carrying that prefix is left as-is (not double-prefixed) when
        it fails to rematch again. An ordinary `fha photoindex` scan never
        touches a 'MISSING:' key (it never matches a real on-disk alias, so a
        naive cache-removal pass would erase it instead of resolving it) -
        only reconcile itself ever removes or transforms one, so the row's
        source_id/path history survives until a later --with-exif retry
        heals it.
      - left untracked: a file with no claimed missing row is reported as new.
        With --with-exif, its SOURCE: keyword (if any) is read so it can be
        attached to that source's inventory in the report rather than being
        reduced to a bare count (TOOLING §9: "if they carry a SOURCE:/S-id,
        attach to the source's inventory"); a full content rescrape remains
        the ordinary scan's job, not reconcile's.

    With dry_run=True, the plan (rematched/missing/new) is computed and
    reported exactly as it would be applied, but no cache row is moved and
    nothing is committed - mirroring the repo's "every mutating operation
    ships --dry-run" contract for what is otherwise an unprompted mutation.

    Any untracked file left over (new_count > 0) was never scraped into the
    `photos` table, so the index is still incomplete with respect to disk even
    after a successful rematch/missing pass. Left alone, committing the
    rematch/missing mutations would bump photos.sqlite's mtime past those
    files' own mtimes, and `photoindex_status()` (which watermarks freshness
    by mtime) would then misreport the catalog as 'fresh' to find/doctor while
    it still omits them. The cache's mtime is pulled back behind the oldest
    untracked file's mtime so the next status check still sees 'stale'.

    A temporarily missing or unmounted photos root (an external drive that
    isn't plugged in, a bad roots.photos mapping) must not be treated as
    "every cached photo vanished" - `run_scan` already warns and leaves the
    cache untouched for the same case, so reconcile checks `photos_root.is_dir()`
    up front and returns 'root_found': False instead of mass-flagging every
    row 'MISSING:'.

    Returns {'status', 'root_found': bool, 'rematched': [(old, new), ...],
    'missing': [path, ...], 'new_count': int,
    'new_sourced': {source_id: [path, ...]}, 'new_unsourced': [path, ...]}.
    """
    empty = {
        'rematched': [], 'missing': [], 'new_count': 0,
        'new_sourced': {}, 'new_unsourced': [],
    }
    if is_working_copy(archive_root):
        return Result(exit_code=EXIT_CLEAN, data={
            'status': 'working-copy', 'working_copy': True, 'root_found': True,
            'photos_root': str(resolve_path('photos', fha_config, archive_root)),
            **empty,
        })

    status, _lag = photoindex_status(archive_root, fha_config)
    if status in ('absent', 'unreadable'):
        return Result(ok=False, exit_code=EXIT_FAILURE,
                      data={'status': status, 'root_found': True, **empty})

    photos_root = resolve_path('photos', fha_config, archive_root)
    if not photos_root.is_dir():
        # A missing photos root is a warning, not a failure (mirrors _cmd_reconcile).
        return Result(ok=False, exit_code=EXIT_WARNINGS, data={
            'status': status, 'root_found': False, 'photos_root': str(photos_root), **empty})
    on_disk = _on_disk_aliases(photos_root, fha_config, archive_root)

    db_path = archive_root / '.cache' / 'photos.sqlite'
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        if not _schema_is_usable(conn):
            return Result(ok=False, exit_code=EXIT_FAILURE,
                          data={'status': 'unreadable', 'root_found': True, **empty})
        try:
            cached = {
                row['path']: row['source_id']
                for row in conn.execute('SELECT path, source_id FROM photos')
            }
            missing = {path: sid for path, sid in cached.items() if path not in on_disk}
            untracked = {alias: p for alias, p in on_disk.items() if alias not in cached}

            candidate_source_ids: dict[Path, str] = {}
            if with_exif and untracked:
                candidate_source_ids = _scrape_source_ids(list(untracked.values()))

            rematched: list[tuple[str, str]] = []
            rematched_paths: list[Path] = []
            if missing and candidate_source_ids:
                claimed: set[str] = set()
                for old_path, source_id in missing.items():
                    if not source_id:
                        continue
                    hits = [
                        alias for alias, p in untracked.items()
                        if alias not in claimed and candidate_source_ids.get(p) == source_id
                    ]
                    if len(hits) == 1:
                        new_path = hits[0]
                        claimed.add(new_path)
                        if not dry_run:
                            _move_cached_path(conn, old_path, new_path)
                        rematched.append((old_path, new_path))
                        rematched_paths.append(untracked[new_path])
                rematched_old = {old for old, _new in rematched}
                missing = {p: sid for p, sid in missing.items() if p not in rematched_old}
                untracked = {a: p for a, p in untracked.items() if a not in claimed}

            now_missing: list[str] = []
            for old_path in missing:
                if old_path.startswith(_MISSING_PREFIX):
                    now_missing.append(old_path)
                    continue
                new_key = f'{_MISSING_PREFIX}{old_path}'
                if not dry_run:
                    _move_cached_path(conn, old_path, new_key)
                now_missing.append(new_key)

            new_sourced: dict[str, list[str]] = {}
            new_unsourced: list[str] = []
            for alias, p in untracked.items():
                source_id = candidate_source_ids.get(p)
                if source_id:
                    new_sourced.setdefault(source_id, []).append(alias)
                else:
                    new_unsourced.append(alias)
            for paths in new_sourced.values():
                paths.sort()
            new_unsourced.sort()

            if dry_run:
                conn.rollback()
            else:
                conn.commit()
            result = {
                'status': status,
                'root_found': True,
                'rematched': rematched,
                'missing': now_missing,
                'new_count': len(untracked),
                'new_sourced': new_sourced,
                'new_unsourced': new_unsourced,
            }
        except sqlite3.Error:
            return Result(ok=False, exit_code=EXIT_FAILURE,
                          data={'status': 'unreadable', 'root_found': True, **empty})
    finally:
        conn.close()

    if not dry_run and (untracked or rematched_paths):
        # Cover both still-untracked files (never scraped) and files just
        # rematched by SOURCE: keyword (path renamed in cache, but mtime/size
        # never refreshed) - either can leave photos.sqlite newer than a file
        # whose on-disk content the cache doesn't actually reflect yet, which
        # would make photoindex_status() report 'fresh' regardless. A photo
        # root on removable/network storage can also vanish mid-stat once the
        # cache write has already committed, so a stat failure here is a
        # missed staleness pullback, not a reason to crash after the fact.
        try:
            oldest = min(
                p.stat().st_mtime for p in (*untracked.values(), *rematched_paths)
            )
            os.utime(db_path, (time.time(), oldest - 1))
        except OSError:
            pass
    # photos.sqlite is a disposable cache (AGENTS.md), so reconcile's row moves
    # are not archive-content changes; `changed` stays empty. Unresolved missing
    # files are a warning (mirrors _cmd_reconcile).
    return Result(exit_code=(EXIT_WARNINGS if result['missing'] else EXIT_CLEAN), data=result)


# ── Tag-person (fha photoindex tag-person - BUILD.md M3.4) ───────────────

def run_tag_person_plan(
    archive_root: Path,
    fha_config: dict,
    person_id: str,
    from_face_tag: str | None = None,
    paths: list[str] | None = None,
) -> Result:
    """
    Resolve the candidate photo paths for `fha photoindex tag-person` without
    writing anything (TOOLING §9: tag-person settles an ambiguous face-tag
    match or explicitly tags --paths by hand). Raises ValueError for a bad
    selector combination or an invalid P-id; otherwise returns a Result whose
    data is {'status', 'person_id', 'candidates': [path, ...], 'already_tagged':
    [path, ...]}.  Result exposes dict-style access (_lib.py), so callers keep
    reading `result['candidates']` unchanged.

    Splitting the plan from the write (apply_tag_person) lets the CLI preview
    and prompt before any original file is touched, and lets tests exercise
    the resolution logic without invoking exiftool.
    """
    if bool(from_face_tag) == bool(paths):
        raise ValueError('exactly one of --from-face-tag or --paths is required')
    if not is_valid_id(person_id) or id_type_of(person_id) != 'P':
        raise ValueError(f'{person_id!r} is not a valid P-id')
    person_id = normalize_id(person_id)
    if person_id not in scan_person_record_ids(archive_root):
        raise ValueError(f'{person_id} is not a known person in the archive')

    status, _lag = photoindex_status(archive_root, fha_config)
    if status in ('absent', 'unreadable'):
        return Result(ok=False, exit_code=EXIT_FAILURE, data={
            'status': status, 'person_id': person_id, 'candidates': [], 'already_tagged': []})

    conn = sqlite3.connect(str(archive_root / '.cache' / 'photos.sqlite'))
    conn.row_factory = sqlite3.Row
    try:
        if not _schema_is_usable(conn):
            return Result(ok=False, exit_code=EXIT_FAILURE, data={
                'status': 'unreadable', 'person_id': person_id, 'candidates': [], 'already_tagged': []})
        try:
            if from_face_tag:
                rows = conn.execute(
                    'SELECT DISTINCT path FROM photo_face_regions WHERE name=?', (from_face_tag,)
                ).fetchall()
                candidate_paths = sorted(row['path'] for row in rows)
                if not candidate_paths:
                    raise ValueError(f'no photo carries a face region named {from_face_tag!r}')
            else:
                resolved = [_resolve_catalog_path(conn, archive_root, fha_config, raw) for raw in paths]
                seen: set[str] = set()
                candidate_paths = [p for p in resolved if not (p in seen or seen.add(p))]

            # photo_people's pid-keyword rows are group-propagated (TOOLING §9:
            # one tagged sibling marks every variant), so they overstate which
            # *files* actually carry the embedded keyword. tag-person previews
            # a per-file write, so "already tagged" must check the file's own
            # photo_keywords row, not the group-aggregated photo_people view.
            already = {
                row['path'] for row in conn.execute(
                    'SELECT path FROM photo_keywords WHERE LOWER(keyword)=?',
                    (person_id.lower(),),
                )
            }
            return Result(data={
                'status': status, 'person_id': person_id,
                'candidates': [p for p in candidate_paths if p not in already],
                'already_tagged': [p for p in candidate_paths if p in already],
            })
        except sqlite3.Error:
            return Result(ok=False, exit_code=EXIT_FAILURE, data={
                'status': 'unreadable', 'person_id': person_id, 'candidates': [], 'already_tagged': []})
    finally:
        conn.close()


def _resolve_catalog_path(
    conn: sqlite3.Connection, archive_root: Path, fha_config: dict, raw: str,
) -> str:
    """Match a user-supplied --paths argument to its stored alias-form path.

    Accepts the alias form directly ('photos/x.jpg', the common case since
    that is what `fha photoindex find` prints), or a filesystem path under the
    configured photos root. Raises ValueError if neither resolves to a row
    already in the catalog - tag-person only ever touches cataloged photos.
    """
    direct = raw.replace('\\', '/')
    row = conn.execute('SELECT path FROM photos WHERE path=?', (direct,)).fetchone()
    if row is None:
        try:
            alias = path_to_alias(Path(raw).resolve(), 'photos', fha_config, archive_root)
        except OSError:
            alias = None
        row = conn.execute('SELECT path FROM photos WHERE path=?', (alias,)).fetchone() if alias else None
    if row is None:
        raise ValueError(f'{raw!r} is not a known photo in the catalog')
    return row['path']


def _run_exiftool_write(paths: list[Path], keyword: str) -> dict[Path, str | None]:
    """
    Add `keyword` to each file's embedded Keywords (exiftool's `+=` list-
    append syntax - existing keywords, including SOURCE:/DATE:, are never
    removed) and overwrite the original in place.

    One exiftool process per file, not one batched call across every
    candidate: exiftool reports a single non-zero exit for the whole
    invocation if *any* file in a multi-file call fails (locked, read-only,
    corrupt), which would hide a successful write to every other file behind
    one bare error. Writing one file at a time lets the caller learn exactly
    which paths succeeded so it can update the cache for those and report
    the rest - AGENTS_TOOLING's "partial success must be reported clearly"
    rule, applied to a keyword write instead of a rename.

    This is the one path in this tool that mutates an original photo file
    rather than the disposable cache (AGENTS.md contract: photos are never
    renamed, but spec'd keyword writes through `fha` tools are permitted).
    Callers must preview and obtain human confirmation before calling this -
    see `_cmd_tag_person`.

    Returns `{path: None}` for each successful write, `{path: stderr text}`
    for each failed one. Raises RuntimeError only when exiftool itself is
    missing - that is an environment problem, not a per-file outcome.
    """
    results: dict[Path, str | None] = {}
    for p in paths:
        cmd = ['exiftool', f'-keywords+={keyword}', '-overwrite_original_in_place', str(p)]
        try:
            proc = subprocess.run(cmd, check=False, capture_output=True, text=True, encoding='utf-8')
        except FileNotFoundError as e:
            raise RuntimeError(format_exiftool_error('fha photoindex tag-person')) from e
        results[p] = None if proc.returncode == 0 else proc.stderr.strip()
    return results


def _refresh_photo_fts_keywords(conn: sqlite3.Connection, paths: list[str]) -> None:
    """
    Resync `photo_fts.keywords` for `paths` from the current `photo_keywords`
    rows. `photo_fts` is otherwise only rebuilt wholesale by a full
    `fha photoindex` scan; `apply_tag_person` writes `photo_keywords` directly,
    so without this in-place patch a just-tagged P-id keyword would stay
    invisible to `fha find --text` until the next scan.
    """
    for path in paths:
        keywords = ' '.join(
            kw for (kw,) in conn.execute(
                'SELECT keyword FROM photo_keywords WHERE path=? ORDER BY rowid', (path,)
            )
        )
        conn.execute('UPDATE photo_fts SET keywords=? WHERE path=?', (keywords, path))


def apply_tag_person(archive_root: Path, fha_config: dict, person_id: str, candidates: list[str]) -> Result:
    """
    Write the bare P-id keyword into each candidate photo's embedded metadata,
    then update the cache so `photo_people` and `photo_fts` reflect the new
    authoritative pid-keyword match immediately (TOOLING §9) - without
    waiting for a full `fha photoindex` rescan to notice the new keyword.

    `person_id` must already be normalized (lowercase) by the caller - see
    `run_tag_person_plan`. The embedded keyword text uses the canonical
    'P-' + lowercase-id form regardless of the casing the human typed, so a
    later scan's `_PID_KEYWORD_RE` match and this call agree on one spelling.

    Each candidate is written and cached independently: a failed write on one
    photo never discards the cache update for photos that did succeed.
    Returns a Result whose data is {'tagged': [path, ...], 'failed': [(path,
    error), ...]} with the embedded-keyword writes listed in `changed`.  Result
    exposes dict-style access (_lib.py), so callers keep reading
    `result['tagged']` unchanged.

    Raises RuntimeError (wrapping a `sqlite3.Error`) if the cache update
    itself fails after one or more original files were already written -
    the original-file writes cannot be rolled back, so the caller must learn
    exactly which `tagged` paths now carry the keyword in-file even though
    the cache update did not complete. `tagged` is recorded as soon as each
    file's own exiftool write succeeds, before its cache insert is attempted -
    otherwise a cache failure on the very candidate whose file write just
    succeeded would drop it from the recovery list this error reports.
    """
    if is_working_copy(archive_root):
        # Warning-level refusal, not a failure: ok stays True, exit stays clean,
        # data.status='working-copy' is the machine discriminator (TOOLING §13d).
        return Result(
            ok=True,
            exit_code=EXIT_CLEAN,
            data={'status': 'working-copy', 'tagged': [], 'failed': []},
        ).add(
            'warning',
            'photoindex tag-person is not available in working-copy mode - '
            'the photo files are on the main machine. '
            'Run this command there.',
        )
    if not candidates:
        return Result(data={'tagged': [], 'failed': []})
    keyword = 'P-' + person_id.split('-', 1)[1]
    abs_paths = [resolve_path(p, fha_config, archive_root) for p in candidates]
    write_results = _run_exiftool_write(abs_paths, keyword)

    tagged: list[str] = []
    failed: list[tuple[str, str]] = []
    conn = sqlite3.connect(str(archive_root / '.cache' / 'photos.sqlite'))
    try:
        try:
            for path, abs_path in zip(candidates, abs_paths):
                error = write_results[abs_path]
                if error is not None:
                    failed.append((path, error))
                    continue
                tagged.append(path)
                conn.execute(
                    'INSERT INTO photo_keywords(path, keyword) SELECT ?, ? WHERE NOT EXISTS '
                    '(SELECT 1 FROM photo_keywords WHERE path=? AND keyword=?)',
                    (path, keyword, path, keyword),
                )
            if tagged:
                _refresh_photo_fts_keywords(conn, tagged)
                # Rebuild rather than upsert one row per tagged path: a pid-keyword
                # match propagates to every variant in the photo's group (front/back,
                # copies - TOOLING §9), and only a full recompute keeps those sibling
                # paths' photo_people rows in sync with the new keyword.
                _rebuild_photo_people(conn, archive_root)
            conn.commit()
        except sqlite3.Error as e:
            tagged_list = ', '.join(tagged) if tagged else 'none'
            raise RuntimeError(
                f'photo cache update failed after writing in-file keywords to: {tagged_list} ({e})'
            ) from e
    finally:
        conn.close()
    return Result(
        ok=(not failed),
        exit_code=(EXIT_FAILURE if failed else EXIT_CLEAN),
        data={'tagged': tagged, 'failed': failed},
        changed=list(tagged),
    )


# ── Scan orchestration ───────────────────────────────────────────────────

def _delete_path_rows(conn: sqlite3.Connection, tables: tuple[str, ...], path_key: str) -> None:
    """Delete path_key's rows from each of `tables` (all keyed by `path`)."""
    for table in tables:
        conn.execute(f'DELETE FROM {table} WHERE path=?', (path_key,))


def _open_and_apply_schema(db_path: Path) -> tuple[sqlite3.Connection, bool]:
    """Open db_path, apply the DDL, and report whether photo_face_regions pre-existed."""
    conn = sqlite3.connect(str(db_path))
    try:
        had_face_regions = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='photo_face_regions'"
        ).fetchone() is not None
        conn.executescript(_DDL)
        conn.execute('PRAGMA user_version=1')
        return conn, not had_face_regions
    except sqlite3.DatabaseError:
        conn.close()
        raise


def _schema_is_usable(conn: sqlite3.Connection) -> bool:
    """Check every _REQUIRED_SCHEMA table has all its required columns (incl. photo_fts)."""
    for table, required_columns in _REQUIRED_SCHEMA.items():
        try:
            rows = conn.execute(f'PRAGMA table_info({table})').fetchall()
        except sqlite3.DatabaseError:
            return False
        columns = {row[1] for row in rows}
        if not required_columns.issubset(columns):
            return False
    return True


def _get_db(cache_dir: Path) -> tuple[sqlite3.Connection, bool, str | None]:
    """
    Open (or create) .cache/photos.sqlite and apply the schema.

    Raises RuntimeError on a corrupt/unreadable existing file rather than
    letting sqlite3's exception surface as a raw traceback - `_cmd_scan`
    turns this into a clean error message and EXIT_FAILURE, matching how
    other tools in the suite report a broken cache (AGENTS_TOOLING error-path
    inventory: a corrupt cache must degrade with a message, never crash).
    The boolean return value tells the scan whether this database existed
    before cached face regions, so unchanged files need one backfill scrape.
    The optional string tells the caller why a cache had to be recreated.
    """
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(f'cannot create .cache directory: {e}') from e
    db_path = cache_dir / 'photos.sqlite'
    schema_status, schema_detail = sqlite_cache_schema_status(
        db_path,
        PHOTOINDEX_SCHEMA_VERSION,
        (
            'photos', 'photo_face_regions', 'photo_fts', 'photo_groups',
            'photo_keywords', 'photo_people',
        ),
    )

    try:
        conn, needs_face_backfill = _open_and_apply_schema(db_path)
        if schema_status in {'absent', 'fresh'} and _schema_is_usable(conn):
            return conn, needs_face_backfill, None
        conn.close()
    except sqlite3.DatabaseError:
        pass

    # photos.sqlite is a disposable cache. Recreate incompatible or corrupt
    # versions instead of letting old schemas crash midway through a scan.
    try:
        if db_path.exists():
            db_path.unlink()
        conn, _needs_face_backfill = _open_and_apply_schema(db_path)
        reason = schema_detail or schema_status or 'schema could not be used'
        return conn, False, reason
    except OSError as e:
        raise RuntimeError(f'cannot replace incompatible photos.sqlite: {e}') from e
    except sqlite3.DatabaseError as e:
        raise RuntimeError(f'photos.sqlite is corrupt or unreadable: {e}') from e


def run_scan(archive_root: Path, fha_config: dict, full: bool = False) -> Result:
    """
    Scan the photos root, scrape new/changed files via exiftool, regroup, and
    rebuild the FTS index. Returns a Result whose data is the summary dict for the
    CLI to print; Result exposes dict-style access (_lib.py), so callers keep
    reading `summary['scraped']` unchanged.  The only writes are to the disposable
    photos.sqlite cache, so `changed` stays empty.

    Incremental by (path, mtime, size): a file already in `photos` with a
    matching mtime+size is assumed unchanged and is not re-sent to exiftool
    (the slow step, ~50-100 files/sec) - `--full` bypasses this and rescans
    everything. Files that have disappeared from disk since the last scan are
    deleted from the cache so stale entries never linger as phantom search
    hits.

    In working-copy mode this function refuses: the photo files aren't here.
    Use _cmd_scan to surface a friendly refusal message; run_scan itself
    returns a working-copy status so callers can detect the mode.
    """
    if is_working_copy(archive_root):
        return Result(ok=False, exit_code=EXIT_CLEAN, data={
            'working_copy': True,
            'photos_root': str(resolve_path('photos', fha_config, archive_root)),
            'root_found': False,
            'total': 0, 'scraped': 0, 'unchanged': 0, 'removed': 0,
            'groups': 0, 'conflicts': 0, 'rebuilt_reason': None,
        })

    photos_root = resolve_path('photos', fha_config, archive_root)
    if not photos_root.is_dir():
        # A missing photos root is a warning, not a failure (mirrors _cmd_scan).
        return Result(ok=False, exit_code=EXIT_WARNINGS, data={
            'photos_root': str(photos_root), 'root_found': False,
            'total': 0, 'scraped': 0, 'unchanged': 0, 'removed': 0,
            'groups': 0, 'conflicts': 0, 'rebuilt_reason': None,
        })

    on_disk: dict[Path, tuple[float, int]] = {}
    alias_by_path: dict[Path, str] = {}
    stat_failures: list[Path] = []
    for p in _iter_photo_files(photos_root):
        try:
            st = p.stat()
            on_disk[p] = (st.st_mtime, st.st_size)
            alias_by_path[p] = path_to_alias(p, 'photos', fha_config, archive_root)
        except OSError:
            stat_failures.append(p)

    if stat_failures:
        sample = ', '.join(str(p) for p in stat_failures[:5])
        more = f' and {len(stat_failures) - 5} more' if len(stat_failures) > 5 else ''
        raise RuntimeError(
            f'could not stat {len(stat_failures)} photo file(s): {sample}{more}'
        )

    conn, needs_face_backfill, rebuilt_reason = _get_db(archive_root / '.cache')
    try:
        existing = {
            path: (mtime, size)
            for path, mtime, size in conn.execute('SELECT path, mtime, size FROM photos')
        }

        to_scrape: list[Path] = []
        for p, (mtime, size) in on_disk.items():
            prior = existing.get(alias_by_path[p])
            if full or needs_face_backfill or prior is None or prior[0] != mtime or prior[1] != size:
                to_scrape.append(p)

        scraped = 0
        for start in range(0, len(to_scrape), _EXIFTOOL_BATCH_SIZE):
            batch = to_scrape[start:start + _EXIFTOOL_BATCH_SIZE]
            resolved = {p: p.resolve() for p in batch}
            rows_by_file = {
                Path(row['SourceFile']).resolve(): row
                for row in _run_exiftool(batch) if row.get('SourceFile')
            }
            missing = [p for p in batch if resolved[p] not in rows_by_file]
            if missing:
                sample = ', '.join(str(p) for p in missing[:5])
                more = f' and {len(missing) - 5} more' if len(missing) > 5 else ''
                raise RuntimeError(
                    'exiftool did not return metadata for '
                    f'{len(missing)} requested file(s): {sample}{more}'
                )
            for p in batch:
                row = rows_by_file[resolved[p]]
                mtime, size = on_disk[p]
                photo = _row_to_photo(row, mtime, size)
                path_key = alias_by_path[p]

                conn.execute(
                    'INSERT OR REPLACE INTO photos(path, mtime, size, title, caption, '
                    'user_comment, exif_date, date_pattern, edtf, sublocation, city, state, '
                    'country, gps_lat, gps_lon, source_id, group_id, is_primary, variant_copy, '
                    'variant_role) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,0,NULL,NULL)',
                    (path_key, photo['mtime'], photo['size'], photo['title'], photo['caption'],
                     photo['user_comment'], photo['exif_date'], photo['date_pattern'],
                     photo['edtf'], photo['sublocation'], photo['city'], photo['state'],
                     photo['country'], photo['gps_lat'], photo['gps_lon'], photo['source_id']),
                )

                _delete_path_rows(conn, ('photo_keywords', 'photo_face_regions'), path_key)
                for kw in photo['_keywords']:
                    conn.execute('INSERT INTO photo_keywords(path, keyword) VALUES (?,?)', (path_key, kw))

                for name, region_type, area_json in photo['_face_regions']:
                    conn.execute(
                        'INSERT INTO photo_face_regions(path, name, region_type, area_json) '
                        'VALUES (?,?,?,?)',
                        (path_key, name, region_type, area_json),
                    )
                scraped += 1

        removed = 0
        alias_on_disk = set(alias_by_path.values())
        for path_key in list(existing):
            # A 'MISSING:'-prefixed key is reconcile's own bookkeeping, not a
            # stale cache entry: it never matches a real on-disk alias (the
            # prefix makes sure of that), so without this guard an ordinary
            # scan run between a no-exif reconcile and a later --with-exif
            # retry would erase the row's source_id/path history that the
            # retry needs to heal it. Only reconcile (rematch or re-flag)
            # ever removes or transforms a MISSING: row -- except here, where
            # the file has reappeared at the exact alias the row remembers:
            # the scrape loop above already inserted a fresh row for that
            # alias, so the synthetic row is a stale duplicate, not bookkeeping
            # an --with-exif retry still needs.
            if path_key.startswith(_MISSING_PREFIX):
                if path_key[len(_MISSING_PREFIX):] in alias_on_disk:
                    conn.execute('DELETE FROM photos WHERE path=?', (path_key,))
                    _delete_path_rows(
                        conn, ('photo_keywords', 'photo_face_regions', 'photo_people'), path_key
                    )
                    removed += 1
                continue
            if path_key not in alias_on_disk:
                conn.execute('DELETE FROM photos WHERE path=?', (path_key,))
                _delete_path_rows(conn, ('photo_keywords', 'photo_face_regions', 'photo_people'), path_key)
                removed += 1

        _group_photos(conn)
        _rebuild_photo_people(conn, archive_root)

        conn.execute('DELETE FROM photo_fts')
        keywords_by_path = _load_keywords_by_path(conn)
        for path, title, caption, user_comment in conn.execute(
            'SELECT path, title, caption, user_comment FROM photos'
        ):
            keywords = ' '.join(keywords_by_path.get(path, []))
            conn.execute(
                'INSERT INTO photo_fts(path, title, caption, user_comment, keywords) VALUES (?,?,?,?,?)',
                (path, title or '', caption or '', user_comment or '', keywords),
            )

        conflicts = conn.execute(
            'SELECT COUNT(*) FROM photo_groups WHERE date_conflict=1'
        ).fetchone()[0]
        groups = conn.execute('SELECT COUNT(*) FROM photo_groups').fetchone()[0]

        conn.commit()
    finally:
        conn.close()

    return Result(data={
        'photos_root': str(photos_root), 'root_found': True,
        'total': len(on_disk), 'scraped': scraped,
        'unchanged': len(on_disk) - scraped, 'removed': removed,
        'groups': groups, 'conflicts': conflicts,
        'rebuilt_reason': rebuilt_reason,
    })


# ── CLI ───────────────────────────────────────────────────────────────────

def _add_photoindex_args(p: argparse.ArgumentParser) -> None:
    """
    Attach photoindex's arguments and deferred-subcommand stubs to `p`.

    Shared by `register()` (the `fha photoindex` dispatcher path) and
    `_standalone_main()` (`python tools/photoindex.py` direct invocation) so
    the two entry points cannot drift apart - without this, a fix applied to
    one path (e.g. adding a new stubbed sub-command) would silently miss the
    other.
    """
    p.add_argument('--root', metavar='PATH', help='Archive root (overrides auto-detection)')
    p.add_argument('--full', action='store_true', help='Rescan every file via exiftool, bypassing the incremental mtime/size check')
    p.set_defaults(func=_cmd_scan)
    deferred = p.add_subparsers(dest='photoindex_command', metavar='SUBCOMMAND')

    # default=SUPPRESS so omitting the child --root does NOT clobber a value
    # already parsed from the parent `photoindex` form
    # (`fha photoindex --root X find ...`); argparse otherwise resets the shared
    # dest back to the subparser's default. The attribute stays absent when neither
    # form supplies it, which `_cmd_find`'s getattr(..., None) handles.
    find_p = deferred.add_parser('find', help='Query the photo catalog')
    find_p.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS, help='Archive root (overrides auto-detection)')
    find_p.add_argument('--person', metavar='P-ID', help='Photos resolved to this P-id (any confidence tier)')
    find_p.add_argument('--keyword', metavar='TERM', help='Case-insensitive substring match against cached keywords')
    find_p.add_argument('--edtf', metavar='EDTF', help='Bounds-overlap filter against each photo\'s resolved date')
    find_p.add_argument('--text', metavar='TEXT', help='Full-text search over title/caption/comment/keywords (photo_fts)')
    find_p.add_argument('--files', action='store_true', help='Show every matching raw row instead of one path per group')
    find_p.set_defaults(func=_cmd_find)

    triage_p = deferred.add_parser('triage', help='Rank unprocessed photo groups by evidence signals')
    triage_p.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS, help='Archive root (overrides auto-detection)')
    triage_p.add_argument('--top', metavar='N', type=int, default=10, help='Show this many top candidates (default 10)')
    triage_p.set_defaults(func=_cmd_triage)

    report_p = deferred.add_parser('report', help='List photo groups with conflicting variant dates')
    report_p.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS, help='Archive root (overrides auto-detection)')
    report_p.set_defaults(func=_cmd_report)

    reconcile_p = deferred.add_parser(
        'reconcile', help='Re-match moved photos by SOURCE: keyword; flag the rest as missing'
    )
    reconcile_p.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS, help='Archive root (overrides auto-detection)')
    reconcile_p.add_argument(
        '--with-exif', action='store_true',
        help='Read embedded SOURCE: keywords from untracked files to re-match missing paths (requires exiftool)',
    )
    reconcile_p.add_argument(
        '--dry-run', action='store_true', dest='dry_run',
        help='Report the rematch/missing/new-file plan without changing the cache',
    )
    reconcile_p.set_defaults(func=_cmd_reconcile)

    tag_p = deferred.add_parser('tag-person', help='Write a bare P-id keyword onto matched or explicit photos')
    tag_p.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS, help='Archive root (overrides auto-detection)')
    tag_p.add_argument('person_id', metavar='P-ID', help='Person to tag')
    tag_p.add_argument('--from-face-tag', metavar='TAG', help='Tag every photo whose cached face region carries this name')
    tag_p.add_argument('--paths', nargs='+', metavar='PATH', help='Tag these specific catalog paths')
    tag_p.add_argument('--dry-run', action='store_true', dest='dry_run', help='Preview the candidate list without writing or prompting')
    tag_p.set_defaults(func=_cmd_tag_person)


# User-facing --help text (the module docstring stays developer-facing).
_CLI_DESCRIPTION = """\
Make your photo library searchable without opening Lightroom.

  fha photoindex                       Scan the photos root into the catalog
  fha photoindex find --person <P-id>  Every photo of someone
  fha photoindex triage --top 20       Un-filed photos worth processing next
  fha photoindex tag-person <P-id>     Tag a face across every copy

Photos are never renamed; identity lives in the embedded metadata."""


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        'photoindex',
        help='Scrape photo metadata into .cache/photos.sqlite',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_photoindex_args(p)


def _resolve_root_and_config(args: argparse.Namespace) -> tuple[Path, dict] | int:
    """
    Resolve --root (or auto-detect) and load fha.yaml, the preamble every
    `_cmd_*` handler needs before it can call its `run_*` function.

    Returns (archive_root, fha_config) on success, or an EXIT_FAILURE int the
    caller should return immediately - callers do
    `resolved = _resolve_root_and_config(args); if isinstance(resolved, int): return resolved`.
    """
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE

    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE

    return archive_root, fha_config


def _print_photoindex_status(
    status: str,
    *,
    require_fresh: bool = False,
    archive_root: Path | None = None,
) -> int | None:
    """
    Print the documented absent/unreadable/stale message for a photoindex_status
    value. Returns an EXIT_FAILURE int the caller should return immediately for
    absent/unreadable, or None to keep going (status is 'fresh', or 'stale' -
    stale still queries, it just warns first).

    `require_fresh=True` is for mutating commands like tag-person: a stale
    index may carry face-region/path data for a photo that has since been
    replaced or changed on disk, so writing a P-id keyword from that cache
    would mutate the wrong file's metadata. Those callers must block on
    'stale' and force a rescan rather than warn-and-continue.

    `archive_root` enables working-copy-aware messages: when WC mode is active,
    the next step is "copy a fresh index from the main machine" rather than
    "run fha photoindex" (which is refused in WC mode).
    """
    in_wc = archive_root is not None and is_working_copy(archive_root)
    if status == 'absent':
        if in_wc:
            print(
                'ERROR: no photo index found. '
                'Copy a fresh photo index from the main machine.',
                file=sys.stderr,
            )
        else:
            print('ERROR: no photo index found. Run fha photoindex first.', file=sys.stderr)
        return EXIT_FAILURE
    if status == 'unreadable':
        if in_wc:
            print(
                'ERROR: photo index is unreadable. '
                'Copy a fresh photo index from the main machine.',
                file=sys.stderr,
            )
        else:
            print(
                'ERROR: your photo index is unreadable. Run `fha photoindex` to rebuild it.',
                file=sys.stderr,
            )
        return EXIT_FAILURE
    if status == 'old-schema':
        if in_wc:
            print(
                'ERROR: photo index is out of date. '
                'Copy a fresh photo index from the main machine.',
                file=sys.stderr,
            )
        else:
            print(
                'ERROR: your photo index is out of date. Run `fha photoindex` to rebuild it.',
                file=sys.stderr,
            )
        return EXIT_FAILURE
    if status == 'stale':
        if require_fresh:
            if in_wc:
                print(
                    'ERROR: photo index is stale; '
                    'copy a fresh index from the main machine.',
                    file=sys.stderr,
                )
            else:
                print(
                    'ERROR: photo index is stale; run fha photoindex to refresh it before tagging.',
                    file=sys.stderr,
                )
            return EXIT_FAILURE
        if in_wc:
            print(
                'WARNING: photo index is stale; results may be out of date. '
                'Copy a fresh index from the main machine.'
            )
        else:
            print('WARNING: photo index is stale; results may be out of date. Run fha photoindex to refresh.')
    return None


def _cmd_scan(args: argparse.Namespace) -> int:
    resolved = _resolve_root_and_config(args)
    if isinstance(resolved, int):
        return resolved
    archive_root, fha_config = resolved

    if is_working_copy(archive_root):
        print(
            'photoindex scan is not available in working-copy mode - '
            'the photo files are on the main machine. '
            'Run this command there.',
            file=sys.stderr,
        )
        return EXIT_CLEAN

    try:
        summary = run_scan(archive_root, fha_config, full=getattr(args, 'full', False))
    except (RuntimeError, sqlite3.Error) as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE

    if not summary['root_found']:
        print(f"Photos root not found: {summary['photos_root']} - nothing scanned.")
        return EXIT_WARNINGS

    if summary.get('rebuilt_reason'):
        print(
            'Photo index cache was out of date or unreadable '
            f"({summary['rebuilt_reason']}); rebuilt from photo files."
        )

    print(
        f"Scanned {summary['total']} files under {summary['photos_root']} "
        f"({summary['scraped']} scraped, {summary['unchanged']} unchanged, "
        f"{summary['removed']} removed from cache).\n"
        f"Groups: {summary['groups']} ({summary['conflicts']} with date conflicts)."
    )
    return EXIT_CLEAN


def _cmd_find(args: argparse.Namespace) -> int:
    resolved = _resolve_root_and_config(args)
    if isinstance(resolved, int):
        return resolved
    archive_root, fha_config = resolved

    person = getattr(args, 'person', None)
    if person:
        person = normalize_id(person)
        # --person filters photo_people, which only ever holds P-ids; a valid but
        # wrong-type id (S-/C-/…) would otherwise pass and silently match nothing.
        if not is_valid_id(person) or id_type_of(person) != 'P':
            print(f'ERROR: {person!r} is not a valid P-id.', file=sys.stderr)
            return EXIT_FAILURE

    try:
        result = run_find(
            archive_root, fha_config,
            person=person,
            keyword=getattr(args, 'keyword', None),
            edtf=getattr(args, 'edtf', None),
            text=getattr(args, 'text', None),
            files=getattr(args, 'files', False),
        )
    except (ValueError, RuntimeError) as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE

    exit_code = _print_photoindex_status(result['status'], archive_root=archive_root)
    if exit_code is not None:
        return exit_code

    rows = result['rows']
    if not rows:
        print('No photos match.')
        return EXIT_CLEAN

    print(f'Found {len(rows)} photo(s):')
    for row in rows:
        date_label = row['edtf'] or '(no date)'
        caption = row['caption'] or row['title'] or ''
        role = '' if row['is_primary'] else f"  [{row['variant_role'] or 'variant'}]"
        suffix = f'  - {caption}' if caption else ''
        print(f"  {row['path']}  [{date_label}]{role}{suffix}")
    return EXIT_CLEAN


def _cmd_triage(args: argparse.Namespace) -> int:
    resolved = _resolve_root_and_config(args)
    if isinstance(resolved, int):
        return resolved
    archive_root, fha_config = resolved

    try:
        result = run_triage(archive_root, fha_config, top=getattr(args, 'top', 10))
    except ValueError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE

    exit_code = _print_photoindex_status(result['status'], archive_root=archive_root)
    if exit_code is not None:
        return exit_code

    candidates = result['candidates']
    if not candidates:
        print('No unprocessed photo groups found.')
        return EXIT_CLEAN

    print(f'Top {len(candidates)} unprocessed photo group(s) by triage score:')
    for c in candidates:
        signals = ', '.join(c['signals']) if c['signals'] else 'no signals'
        print(f"  {c['path']}  score={c['score']:+d}  [{signals}]")
        print(f"    suggested: fha process {c['path']}")
    return EXIT_CLEAN


def _cmd_report(args: argparse.Namespace) -> int:
    resolved = _resolve_root_and_config(args)
    if isinstance(resolved, int):
        return resolved
    archive_root, fha_config = resolved

    result = run_report(archive_root, fha_config)

    exit_code = _print_photoindex_status(result['status'], archive_root=archive_root)
    if exit_code is not None:
        return exit_code

    conflicts = result['conflicts']
    if not conflicts:
        print('No date conflicts found among grouped photo variants.')
        return EXIT_CLEAN

    print(f'Found {len(conflicts)} photo group(s) with conflicting variant dates:')
    for group in conflicts:
        print(f"  {group['group_id']}  (primary: {group['primary_path']})")
        for p in group['photos']:
            date_label = p['edtf'] or '(no date)'
            caption = f"  - {p['caption']}" if p['caption'] else ''
            print(f"    {p['path']}  [{date_label}]{caption}")
    return EXIT_CLEAN


def _cmd_reconcile(args: argparse.Namespace) -> int:
    resolved = _resolve_root_and_config(args)
    if isinstance(resolved, int):
        return resolved
    archive_root, fha_config = resolved

    try:
        result = run_reconcile(
            archive_root, fha_config,
            with_exif=getattr(args, 'with_exif', False),
            dry_run=getattr(args, 'dry_run', False),
        )
    except RuntimeError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE

    if result.get('working_copy'):
        print(
            'photoindex reconcile is not available in working-copy mode - '
            'the photo files are on the main machine. Run this command there.',
            file=sys.stderr,
        )
        return EXIT_CLEAN

    if not result['root_found']:
        print(f"WARNING: photos root not found: {result['photos_root']}", file=sys.stderr)
        return EXIT_WARNINGS

    exit_code = _print_photoindex_status(result['status'], archive_root=archive_root)
    if exit_code is not None:
        return exit_code

    if getattr(args, 'dry_run', False):
        print('(dry run - no changes made)')

    for old, new in result['rematched']:
        print(f'  RE-MATCHED  {old} -> {new}')
    for path in result['missing']:
        print(f'  MISSING     {path[len(_MISSING_PREFIX):]}')
    for source_id, paths in sorted(result['new_sourced'].items()):
        print(f'  NEW (source {source_id})  ' + ', '.join(paths))
    if result['new_unsourced']:
        print('  NEW (unsourced)  ' + ', '.join(result['new_unsourced']))
    if result['new_count']:
        print(
            f"{result['new_count']} new file(s) on disk not yet in the catalog; "
            'run fha photoindex to add them.'
        )

    if not result['rematched'] and not result['missing'] and not result['new_count']:
        print('reconcile: no drift between the catalog and disk.')
        return EXIT_CLEAN

    return EXIT_WARNINGS if result['missing'] else EXIT_CLEAN


def _cmd_tag_person(args: argparse.Namespace) -> int:
    resolved = _resolve_root_and_config(args)
    if isinstance(resolved, int):
        return resolved
    archive_root, fha_config = resolved

    if is_working_copy(archive_root):
        print(
            'photoindex tag-person is not available in working-copy mode - '
            'the photo files are on the main machine. '
            'Run this command there.',
            file=sys.stderr,
        )
        return EXIT_CLEAN

    try:
        plan = run_tag_person_plan(
            archive_root, fha_config, getattr(args, 'person_id', ''),
            from_face_tag=getattr(args, 'from_face_tag', None),
            paths=getattr(args, 'paths', None),
        )
    except ValueError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE

    exit_code = _print_photoindex_status(plan['status'], require_fresh=True, archive_root=archive_root)
    if exit_code is not None:
        return exit_code

    if plan['already_tagged']:
        print(f"Already tagged with {plan['person_id']}: " + ', '.join(plan['already_tagged']))

    candidates = plan['candidates']
    if not candidates:
        print('No untagged photos to tag.')
        return EXIT_CLEAN

    print(f"Will tag {len(candidates)} photo(s) with {plan['person_id']}:")
    for path in candidates:
        print(f'  {path}')

    if getattr(args, 'dry_run', False):
        print('\n(dry-run: no changes written)')
        return EXIT_CLEAN

    try:
        answer = input('\nTag these photos? [y/N] ').strip().lower()
    except EOFError:
        answer = ''
    if answer != 'y':
        print('Aborted - no changes written.')
        return EXIT_CLEAN

    try:
        result = apply_tag_person(archive_root, fha_config, plan['person_id'], candidates)
    except RuntimeError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE

    if result['failed']:
        print(
            f"Tagged {len(result['tagged'])} photo(s); {len(result['failed'])} failed:",
            file=sys.stderr,
        )
        for path, error in result['failed']:
            print(f'  {path}: {error}', file=sys.stderr)
        return EXIT_FAILURE

    print(f"Tagged {len(result['tagged'])} photo(s).")
    return EXIT_CLEAN


# ── Standalone ────────────────────────────────────────────────────────────

def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha photoindex',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_photoindex_args(parser)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
