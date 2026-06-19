#!/usr/bin/env python3
"""
photoindex.py — fha photoindex: photo metadata catalog.

  fha photoindex [--full]

Scrapes embedded metadata (exiftool) for every file under the photos root
into a disposable SQLite catalog, `.cache/photos.sqlite`, so the photo
library is searchable without opening Lightroom (or any other catalog tool).
TOOLING §9.

ARCHITECTURE OVERVIEW
----------------------
Photos are never renamed (Lightroom catalog integrity — SPEC §13) and most
are never "processed" into a source record at all, so the filename and the
embedded metadata are the only durable identity a photo carries. This tool
reads that metadata in one batched `exiftool -j` pass per scan and writes it
into a queryable cache, then derives two things on top of the raw rows:

  1. Variation grouping — fronts/backs/copies/negatives/crops of one
     physical photo are one logical photo. Group key is (in priority order)
     a shared `SOURCE:` S-id keyword, then same-directory + same filename
     base_id (`_lib.parse_media_filename`). Conservative by design: never
     groups across directories, never on caption similarity alone.
  2. Person resolution — bare `P-id` keywords (authoritative) → cached face
     region strings matched against `person_face_tags` → name/name_variant
     matches (weakest). The latter two require a fresh `.cache/index.sqlite`;
     absent, stale, or unreadable indexes degrade to pid-keyword-only
     resolution.

Scanning is incremental by (path, mtime, size): unchanged files are not
re-scraped via exiftool (the slow step). Face-region metadata is cached in
SQLite alongside keywords, so grouping, person resolution, and the FTS table
can be recomputed in full after every scan from already-scraped rows.

This PR builds the scan + schema + grouping pipeline only. `fha photoindex
find` (the query subcommand) and the triage/reconcile/tag-person/report
sub-commands are follow-up PRs per BUILD.md layer 3.

CODE MAP
--------
  Schema
    _DDL                      — CREATE TABLE statements for photos.sqlite

  exiftool integration
    PHOTO_EXTENSIONS          — recognised photo/scan file extensions
    _run_exiftool             — batched `exiftool -j -struct` invocation (test seam)
    _SOURCE_KEYWORD_RE, _PID_KEYWORD_RE, _DATE_KEYWORD_RE — keyword patterns
    _extract_keywords         — flatten Keywords+Subject into one string list
    _keyword_to_edtf          — confidence-coded DATE: keyword -> EDTF (SPEC §20)
    _extract_face_regions     — XMP-mwg-rs:RegionInfo -> list of (name, type, area)
    _row_to_photo             — one exiftool JSON row -> photos table column dict

  Person resolution
    _index_is_fresh           — schema-valid + not-older-than-newest-record check on index.sqlite
    _load_face_tag_index      — person_face_tags + persons name/variants from index.sqlite
    _resolve_photo_people     — pid-keyword / face-tag / name-match, in confidence order
    _rebuild_photo_people     — recompute photo_people from cached keywords + face regions

  Variation grouping
    _edtf_confidence          — sortable confidence score for an EDTF string (SPEC §20)
    _group_photos             — assign group_id/is_primary/variant_role; build photo_groups

  Scan orchestration
    _get_db                   — open (or create) .cache/photos.sqlite, apply DDL
    run_scan                  — top-level: walk photos root, scrape, group, report

  CLI
    register                  — attach 'photoindex' to the main fha parser; stubs the
                                 not-yet-built find/triage/reconcile/tag-person/report
                                 sub-commands so the command tree is coherent (views.py
                                 precedent, BUILD.md layer 3)
    _cmd_scan                 — argparse -> run_scan bridge
    _cmd_deferred             — prints the deferral message for stubbed sub-commands
    _standalone_main          — for `python tools/photoindex.py` direct invocation
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    FhaConfigError,
    db_mtime,
    ParsedName,
    edtf_bounds,
    find_archive_root,
    is_valid_edtf,
    load_fha_yaml,
    newest_person_record_mtime,
    normalize_id,
    parse_media_filename,
    path_to_alias,
    probe_sqlite,
    resolve_path,
)

# ── Schema (TOOLING §9 plus cached face regions) ─────────────────────────────

_DDL = """
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
CREATE TABLE IF NOT EXISTS photo_groups(
  group_id TEXT PRIMARY KEY, primary_path TEXT,
  edtf_resolved TEXT, date_conflict INTEGER DEFAULT 0, file_count INTEGER
);
CREATE TABLE IF NOT EXISTS photo_keywords(path TEXT, keyword TEXT);
CREATE TABLE IF NOT EXISTS photo_face_regions(
  path TEXT, name TEXT, region_type TEXT, area_json TEXT
);
CREATE TABLE IF NOT EXISTS photo_people(path TEXT, person_ref TEXT, via TEXT);
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

# Common raster and camera-raw extensions a personal photo library mixes in.
PHOTO_EXTENSIONS = frozenset({
    '.jpg', '.jpeg', '.png', '.tif', '.tiff', '.bmp', '.gif', '.heic', '.heif',
    '.cr2', '.nef', '.dng', '.arw', '.orf', '.rw2',
})

_EXIFTOOL_FIELDS = [
    '-Title', '-Caption-Abstract', '-XMP-dc:Description', '-UserComment', '-DateTimeOriginal',
    '-Location', '-City', '-State', '-Country', '-GPSLatitude', '-GPSLongitude',
    '-Keywords', '-Subject', '-XMP-mwg-rs:RegionInfo',
]

_SOURCE_KEYWORD_RE = re.compile(r'^SOURCE:\s*(S-[0-9a-hjkmnp-tv-z]{10})$', re.I)
_PID_KEYWORD_RE = re.compile(r'^P-[0-9a-hjkmnp-tv-z]{10}$', re.I)
_DATE_KEYWORD_RE = re.compile(r'^DATE:\s*(.+)$')


def _run_exiftool(paths: list[Path]) -> list[dict]:
    """
    Run one batched `exiftool -j -struct` call over `paths` and return the
    parsed JSON rows.

    This is the seam a test harness replaces to inject pre-cooked JSON
    without requiring exiftool on PATH (BUILD.md layer-3 fixture note) —
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
        raise RuntimeError('fha photoindex requires exiftool on PATH') from e
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
    first component that is missing or marked '?' — SPEC §20 states 'Y!' is
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

    parts = [year]
    suffix = '~' if year_c == '~' else ''
    if month and month_c != '?':
        parts.append(month)
        suffix = '~' if month_c == '~' else ''
        if day and day_c != '?':
            parts.append(day)
            suffix = '~' if day_c == '~' else ''

    edtf = '-'.join(parts) + suffix
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

    A tag matching more than one person is never guessed at this layer — it's
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
    resolved_by_face_tag: set[str] = set()
    ambiguous_tags: set[str] = set()
    for region_name, _region_type in face_regions:
        pids = face_tags.get(region_name)
        if pids:
            if len(pids) == 1:
                add(next(iter(pids)), 'face-tag')
                resolved_by_face_tag.add(region_name)
            else:
                ambiguous_tags.add(region_name)

    for region_name in matched_names - ambiguous_tags - resolved_by_face_tag:
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


_VIA_PRIORITY = {'pid-keyword': 0, 'face-tag': 1, 'name-match': 2}


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
    """
    face_tags, names = _load_face_tag_index(archive_root)
    keywords_by_path = _load_keywords_by_path(conn)
    face_regions_by_path = _load_face_regions_by_path(conn)
    conn.execute('DELETE FROM photo_people')

    matches_by_path: dict[str, list[tuple[str, str]]] = {}
    paths_by_group: dict[str, list[str]] = {}
    for path, group_id in conn.execute('SELECT path, group_id FROM photos ORDER BY path'):
        keywords = keywords_by_path.get(path, [])
        face_regions = face_regions_by_path.get(path, [])
        matches_by_path[path] = _resolve_photo_people(keywords, face_regions, face_tags, names)
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


def _variant_role(parsed: ParsedName) -> str | None:
    """Compound role string for the photos.variant_role column (TOOLING §9)."""
    if parsed.part_kind == 'page':
        base = f'page-{parsed.page_num}'
    elif parsed.part_kind == 'freeform':
        base = parsed.freeform_role
    elif parsed.part_kind != 'none':
        base = parsed.part_kind
    else:
        base = None
    if parsed.is_crop:
        return f'{base}-crop' if base else 'crop'
    return base


def _grouping_stem(parsed: ParsedName) -> str:
    """
    base_id for use as a grouping key (TOOLING §9): the recognized suffix
    grammar (copy letter, negative/back/front/page-N/bw, crop) is stripped,
    but an unrecognized freeform suffix is kept folded back in. Freeform
    roles are not part of the documented grouping grammar — two unrelated
    files like 'smith-family.jpg' and 'smith-house.jpg' must not collapse
    into one group just because both have a trailing '-word'.
    """
    if parsed.part_kind == 'freeform':
        return f'{parsed.base_id}-{parsed.freeform_role}'
    return parsed.base_id


def _group_photos(conn: sqlite3.Connection) -> None:
    """
    Recompute group_id/is_primary/variant_copy/variant_role for every photo,
    and rebuild photo_groups, from the current `photos` rows.

    Grouping key, in priority order (TOOLING §9): (1) a shared SOURCE: S-id —
    files already processed into one source are one logical photo regardless
    of name; (2) same directory + same filename base_id after stripping the
    *recognized* suffix grammar (_lib.parse_media_filename, excluding
    freeform roles — see _grouping_stem) — the pipeline's own variation
    convention. Always run over every row (not just changed ones): grouping
    is cheap pure-SQL/Python and a partial re-group after an incremental scan
    would silently miss a newly-added sibling joining an existing group.
    """
    rows = conn.execute('SELECT path, source_id, edtf FROM photos').fetchall()
    groups: dict[str, list[str]] = {}
    parsed_by_path: dict[str, ParsedName] = {}
    edtf_by_path: dict[str, str | None] = {}

    for path, source_id, edtf in rows:
        p = Path(path)
        parsed = parse_media_filename(p.stem)
        parsed_by_path[path] = parsed
        edtf_by_path[path] = edtf
        if source_id:
            key = f'SOURCE:{source_id}'
        else:
            key = f'STEM:{p.parent.as_posix()}:{_grouping_stem(parsed)}'
        groups.setdefault(key, []).append(path)

    conn.execute('DELETE FROM photo_groups')

    for group_id, paths in groups.items():
        # Primary = a file with no variant/role/crop suffix at all (the plain
        # scan), then a front scan, then the lexicographically-first path when
        # every member carries some other suffix (e.g. a back-only group).
        plain = [
            p for p in paths
            if parsed_by_path[p].variant_id is None
            and parsed_by_path[p].part_kind == 'none'
            and not parsed_by_path[p].is_crop
        ]
        fronts = [
            p for p in paths
            if parsed_by_path[p].variant_id in (None, 'a')
            and parsed_by_path[p].part_kind == 'front'
            and not parsed_by_path[p].is_crop
        ]
        primary = min(plain) if plain else (min(fronts) if fronts else min(paths))

        # Order candidates with the primary file first, then lexicographically,
        # so a confidence tie (e.g. two variants both 'year only, no marker')
        # resolves deterministically to the primary's date rather than
        # insertion order — and falls back to path order only when the
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
            # letter in their filename (TOOLING §9) — a negative is source
            # material for the root image, not an A/B variant of the print.
            variant_copy = None if parsed.part_kind == 'negative' else parsed.variant_id
            conn.execute(
                'UPDATE photos SET group_id=?, is_primary=?, variant_copy=?, variant_role=? '
                'WHERE path=?',
                (group_id, 1 if path == primary else 0, variant_copy,
                 _variant_role(parsed), path),
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


def _get_db(cache_dir: Path) -> tuple[sqlite3.Connection, bool]:
    """
    Open (or create) .cache/photos.sqlite and apply the schema.

    Raises RuntimeError on a corrupt/unreadable existing file rather than
    letting sqlite3's exception surface as a raw traceback — `_cmd_scan`
    turns this into a clean error message and EXIT_FAILURE, matching how
    other tools in the suite report a broken cache (AGENTS_TOOLING error-path
    inventory: a corrupt cache must degrade with a message, never crash).
    The boolean return value tells the scan whether this database existed
    before cached face regions, so unchanged files need one backfill scrape.
    """
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(f'cannot create .cache directory: {e}') from e
    db_path = cache_dir / 'photos.sqlite'

    try:
        conn, needs_face_backfill = _open_and_apply_schema(db_path)
        if _schema_is_usable(conn):
            return conn, needs_face_backfill
        conn.close()
    except sqlite3.DatabaseError:
        pass

    # photos.sqlite is a disposable cache. Recreate incompatible or corrupt
    # versions instead of letting old schemas crash midway through a scan.
    try:
        if db_path.exists():
            db_path.unlink()
        conn, _needs_face_backfill = _open_and_apply_schema(db_path)
        return conn, False
    except OSError as e:
        raise RuntimeError(f'cannot replace incompatible photos.sqlite: {e}') from e
    except sqlite3.DatabaseError as e:
        raise RuntimeError(f'photos.sqlite is corrupt or unreadable: {e}') from e


def run_scan(archive_root: Path, fha_config: dict, full: bool = False) -> dict:
    """
    Scan the photos root, scrape new/changed files via exiftool, regroup, and
    rebuild the FTS index. Returns a summary dict for the CLI to print.

    Incremental by (path, mtime, size): a file already in `photos` with a
    matching mtime+size is assumed unchanged and is not re-sent to exiftool
    (the slow step, ~50-100 files/sec) — `--full` bypasses this and rescans
    everything. Files that have disappeared from disk since the last scan are
    deleted from the cache so stale entries never linger as phantom search
    hits.
    """
    photos_root = resolve_path('photos', fha_config, archive_root)
    if not photos_root.is_dir():
        return {
            'photos_root': str(photos_root), 'root_found': False,
            'total': 0, 'scraped': 0, 'unchanged': 0, 'removed': 0,
            'groups': 0, 'conflicts': 0,
        }

    on_disk: dict[Path, tuple[float, int]] = {}
    alias_by_path: dict[Path, str] = {}
    for p in photos_root.rglob('*'):
        if p.is_file() and p.suffix.lower() in PHOTO_EXTENSIONS:
            try:
                st = p.stat()
                on_disk[p] = (st.st_mtime, st.st_size)
                alias_by_path[p] = path_to_alias(p, 'photos', fha_config, archive_root)
            except OSError:
                pass

    conn, needs_face_backfill = _get_db(archive_root / '.cache')
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

        batch_size = 500
        scraped = 0
        for start in range(0, len(to_scrape), batch_size):
            batch = to_scrape[start:start + batch_size]
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

    return {
        'photos_root': str(photos_root), 'root_found': True,
        'total': len(on_disk), 'scraped': scraped,
        'unchanged': len(on_disk) - scraped, 'removed': removed,
        'groups': groups, 'conflicts': conflicts,
    }


# ── CLI ───────────────────────────────────────────────────────────────────

def _add_photoindex_args(p: argparse.ArgumentParser) -> None:
    """
    Attach photoindex's arguments and deferred-subcommand stubs to `p`.

    Shared by `register()` (the `fha photoindex` dispatcher path) and
    `_standalone_main()` (`python tools/photoindex.py` direct invocation) so
    the two entry points cannot drift apart — without this, a fix applied to
    one path (e.g. adding a new stubbed sub-command) would silently miss the
    other.
    """
    p.add_argument('--root', metavar='PATH', help='Archive root (overrides auto-detection)')
    p.add_argument('--spec-root', metavar='PATH', help='Spec docs root (accepted for CLI consistency)')
    p.add_argument('--full', action='store_true', help='Rescan every file via exiftool, bypassing the incremental mtime/size check')
    p.set_defaults(func=_cmd_scan)
    deferred = p.add_subparsers(dest='photoindex_command', metavar='SUBCOMMAND')
    for name in ('find', 'triage', 'reconcile', 'tag-person', 'report'):
        child = deferred.add_parser(name, help='Deferred to a follow-up photoindex PR')
        child.add_argument('--root', metavar='PATH', help=argparse.SUPPRESS)
        child.add_argument('--spec-root', metavar='PATH', help=argparse.SUPPRESS)
        if name == 'find':
            child.add_argument('--person', metavar='P-ID', help=argparse.SUPPRESS)
            child.add_argument('--keyword', metavar='TEXT', help=argparse.SUPPRESS)
            child.add_argument('--edtf', metavar='EDTF', help=argparse.SUPPRESS)
            child.add_argument('--text', metavar='TEXT', help=argparse.SUPPRESS)
            child.add_argument('--files', action='store_true', help=argparse.SUPPRESS)
        elif name == 'triage':
            child.add_argument('--top', metavar='N', help=argparse.SUPPRESS)
        elif name == 'tag-person':
            child.add_argument('person_id', nargs='?', metavar='P-ID', help=argparse.SUPPRESS)
            child.add_argument('--from-face-tag', metavar='TAG', help=argparse.SUPPRESS)
            child.add_argument('--paths', nargs='*', metavar='PATH', help=argparse.SUPPRESS)
        child.set_defaults(func=_cmd_deferred)


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        'photoindex',
        help='Scrape photo metadata into .cache/photos.sqlite',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_photoindex_args(p)


def _cmd_deferred(args: argparse.Namespace) -> int:
    """Print a clear deferral message for documented but unbuilt subcommands."""
    name = getattr(args, 'photoindex_command', 'subcommand')
    print(f'fha photoindex {name} is deferred to a follow-up photoindex PR.')
    return EXIT_CLEAN


def _cmd_scan(args: argparse.Namespace) -> int:
    root = getattr(args, 'root', None)
    if root:
        archive_root = Path(root).resolve()
    else:
        archive_root = find_archive_root()
        if archive_root is None:
            print('ERROR: cannot find archive root. Use --root.', file=sys.stderr)
            return EXIT_FAILURE

    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE

    try:
        summary = run_scan(archive_root, fha_config, full=getattr(args, 'full', False))
    except RuntimeError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE

    if not summary['root_found']:
        print(f"Photos root not found: {summary['photos_root']} — nothing scanned.")
        return EXIT_WARNINGS

    print(
        f"Scanned {summary['total']} files under {summary['photos_root']} "
        f"({summary['scraped']} scraped, {summary['unchanged']} unchanged, "
        f"{summary['removed']} removed from cache).\n"
        f"Groups: {summary['groups']} ({summary['conflicts']} with date conflicts)."
    )
    return EXIT_CLEAN


# ── Standalone ────────────────────────────────────────────────────────────

def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha photoindex',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_photoindex_args(parser)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
