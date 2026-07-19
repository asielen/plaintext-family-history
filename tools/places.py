#!/usr/bin/env python3
"""
places.py - fha places: place registry hygiene, recurrence detection, and
human-directed registry edits (TOOLING §10, SPEC §15).

  fha places lint [--root PATH]
  fha places candidates [--root PATH] [--threshold N]
  fha places geocode [--place L-id] [--all] [--offline] [--root PATH]
  fha places set L-id [--coords "LAT, LON"] [--aka NAME]... [--history "PERIOD | HIERARCHY"]...
  fha places note L-id --text TEXT [--dry-run]

`fha places lint` checks `places/places.yaml` (via the index's `places`/
`place_names`/`place_history` tables) plus `claims.place_id` for registry
hygiene:
  - orphan L-ids referenced by a claim's `place_id` but absent from the registry
  - duplicate place names (case-folded across `name` + `alt_names`)
  - dangling `within:` links (target L-id not in the registry)
  - cyclic `within:` chains
  - a `within:` link whose source is itself a settlement - i.e. it is already
    the target of some other place's `within:` link, so it cannot also point
    further up the containment chain (SPEC §15: settlement-to-jurisdiction
    links live only in dated `history:` strings, never in `within:`)

`fha places candidates` is the recurrence detector (TOOLING §10), sibling to
`fha cooccur`: distinct *unlinked*, active (`accepted`/`needs-review`) claim
`place_text` values (no `place_id`) are normalized (case-fold, punctuation, whitespace, St/Street and Co/County
expansion) and clustered by a sorted token-set key so word-order variants and
abbreviation variants land in the same group; groups with >= `--threshold`
(default 3) occurrences are surfaced with their claim count and EDTF date
spread. A second, independent detector clusters geotagged photos
(`.cache/photos.sqlite`) that have no known place within ~150m of them.

`fha places geocode` (TOOLING §10) backfills `coords` (and proposes alt-names)
for registry places that lack coordinates, using a one-time **offline GeoNames**
dump (`cities15000.txt`, downloaded into `.cache/geonames/` unless `--offline`):
no live API. A place's `name` + `hierarchy` tokens are matched against the
gazetteer; only a *single* high-confidence candidate is proposed (place identity
is a research judgment, not a string match - ambiguous matches are skipped, not
guessed), and **every write requires interactive `[y/N]` confirmation**. Writes
edit `places/places.yaml` surgically (the matched block only) so the file's hand
comments and unrelated entries are preserved without needing `ruamel.yaml`.

`fha places set` / `fha places note` are the human-directed registry edits the
workbench place page drives: coordinates, the also-known-as list, the dated
names-over-time entries (each a FULL per-field replace), and a dated research
note appended to `notes:`. Same surgical one-block writes as geocode; no [y/N]
gate because the values are the human's own, previewed and applied by them
(see the write-backs section comment for how this satisfies the AGENTS.md
coordinate-confirmation rule).

CODE MAP
--------
  Lint
    _within_map, _lint_orphan_place_ids, _lint_duplicate_names,
    _lint_dangling_within, _lint_cyclic_within, _lint_within_on_settlement
    run_lint

  Candidates
    _expand_abbreviations, _candidate_key  - normalization for clustering
    _place_text_candidates                 - unlinked place_text clusters
    _haversine_meters, _gps_clusters        - photo-GPS clusters
    run_candidates

  Geocode
    _US_STATE_CODES, _country_code_of      - hierarchy-token → code helpers
    GeoRow, _load_gazetteer                - parse the GeoNames dump
    _download_gazetteer                    - one-time offline-dump fetch
    _match_place                           - name+hierarchy → unique hit / ambiguous / none
    _locate_place_block                    - one `- id:` block's line span (shared grammar)
    _apply_geocode_to_yaml                 - surgical places.yaml block edit
    run_geocode                            - gather candidates, match, (confirm) write

  Registry write-backs (set / note)
    _key_span, _set_block_key              - one key's span within a block; replace/insert
    _parse_coords_text, _parse_history_lines - human-typed field values → stored shapes
    _place_write_result, _finish_place_write - shared head (validate/read) and tail (diff/write)
    run_place_set                          - coords / alt_names / history, full per-field replace
    run_place_note                         - dated research note appended to notes:
    run_place_edit_note                    - rewrite ONE notes: entry (matched by exact text)

  CLI
    _cmd_places_lint, _cmd_places_candidates, _cmd_places_geocode,
    _cmd_places_set, _cmd_places_note, register, _standalone_main
"""

from __future__ import annotations

import argparse
import datetime
import difflib
import io
import re
import sqlite3
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from math import atan2, cos, radians, sin, sqrt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import yaml

from _lib import (
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    Finding,
    FhaConfigError,
    Result,
    edtf_bounds,
    fmt_id_display,
    format_edtf_error,
    id_type_of,
    is_valid_id,
    load_fha_yaml,
    normalize_date,
    normalize_id,
    normalize_place_text,
    open_index_db,
    photoindex_status,
    read_text_exact,
    reapply_newline,
    resolve_root_arg,
    result_fail,
    split_log_entries,
    write_text_exact,
    yaml_inline,
)

_LINT_REQUIRED_TABLES = ('places', 'place_names', 'place_history', 'claims')
_CANDIDATES_REQUIRED_TABLES = ('claims', 'places')
_GEOCODE_REQUIRED_TABLES = ('places', 'place_names')

_GPS_CLUSTER_RADIUS_M = 150.0
_EARTH_RADIUS_M = 6371000.0


# ── Lint ──────────────────────────────────────────────────────────────────────

def _lint_orphan_place_ids(conn: sqlite3.Connection) -> list[Finding]:
    known = {row['id'] for row in conn.execute('SELECT id FROM places')}
    findings = []
    seen: set[str] = set()
    for row in conn.execute(
        "SELECT id, place_id FROM claims WHERE place_id IS NOT NULL AND place_id != ''"
    ):
        pid = normalize_id(row['place_id'])
        if pid and pid not in known and pid not in seen:
            seen.add(pid)
            findings.append(Finding(
                'E', 'PL001', 'places/places.yaml',
                f'Claim {fmt_id_display(row["id"])} references unknown place {fmt_id_display(pid)}',
            ))
    return findings


def _lint_duplicate_names(conn: sqlite3.Connection) -> list[Finding]:
    names_by_key: dict[str, set[str]] = {}
    for row in conn.execute('SELECT id, name FROM places WHERE name IS NOT NULL'):
        key = normalize_place_text(row['name'])
        if key:
            names_by_key.setdefault(key, set()).add(row['id'])
    for row in conn.execute('SELECT place_id, alt_name FROM place_names WHERE alt_name IS NOT NULL'):
        key = normalize_place_text(row['alt_name'])
        if key:
            names_by_key.setdefault(key, set()).add(row['place_id'])

    findings = []
    for key, place_ids in sorted(names_by_key.items()):
        if len(place_ids) > 1:
            ids_display = ', '.join(fmt_id_display(p) for p in sorted(place_ids))
            findings.append(Finding(
                'W', 'PL002', 'places/places.yaml',
                f'Duplicate place name {key!r} shared by {ids_display}',
            ))
    return findings


def _within_map(conn: sqlite3.Connection) -> tuple[dict[str, str | None], list[Finding]]:
    """
    Build {place_id: normalized within: target or None}, flagging (PL006)
    any `within:` value that isn't a string (e.g. a YAML scalar like `within:
    123`) instead of letting normalize_id's `.strip()` raise.
    """
    rows: dict[str, str | None] = {}
    findings: list[Finding] = []
    for row in conn.execute('SELECT id, within FROM places'):
        raw = row['within']
        if raw is None or raw == '':
            rows[row['id']] = None
        elif isinstance(raw, str):
            rows[row['id']] = normalize_id(raw) or None
        else:
            findings.append(Finding(
                'E', 'PL006', 'places/places.yaml',
                f'{fmt_id_display(row["id"])} has a non-string within: value ({raw!r}) - within: must be an L-id string',
            ))
            rows[row['id']] = None
    return rows, findings


def _lint_dangling_within(rows: dict[str, str | None]) -> list[Finding]:
    known = set(rows.keys())
    findings = []
    for pid, target in sorted(rows.items()):
        if target and target not in known:
            findings.append(Finding(
                'E', 'PL003', 'places/places.yaml',
                f'{fmt_id_display(pid)} has a dangling within: link to unknown place {fmt_id_display(target)}',
            ))
    return findings


def _lint_cyclic_within(rows: dict[str, str | None]) -> list[Finding]:
    findings = []
    reported: set[frozenset[str]] = set()
    for start in sorted(rows.keys()):
        visited: list[str] = []
        seen: set[str] = set()
        cur: str | None = start
        while cur is not None and cur in rows:
            if cur in seen:
                cycle = visited[visited.index(cur):]
                key = frozenset(cycle)
                if key not in reported:
                    reported.add(key)
                    chain = ' -> '.join(fmt_id_display(p) for p in cycle + [cur])
                    findings.append(Finding(
                        'E', 'PL004', 'places/places.yaml',
                        f'Cyclic within: chain: {chain}',
                    ))
                break
            seen.add(cur)
            visited.append(cur)
            cur = rows.get(cur)
    return findings


def _lint_within_on_settlement(rows: dict[str, str | None]) -> list[Finding]:
    """
    A place that is itself the target of another place's `within:` link has
    been established as a containing settlement (something physically inside
    it); SPEC §15 says settlement-to-jurisdiction containment is never
    expressed via `within:` (only via dated `history:` strings), so that same
    place carrying its own outward `within:` link is invalid.
    """
    targets = {target for target in rows.values() if target}
    findings = []
    for pid, target in sorted(rows.items()):
        if target and pid in targets:
            findings.append(Finding(
                'E', 'PL005', 'places/places.yaml',
                f'{fmt_id_display(pid)} is itself a within: target (a settlement) but also '
                f'links within: {fmt_id_display(target)} - settlement-to-jurisdiction containment '
                'belongs in history:, not within:',
            ))
    return findings


def run_lint(archive_root: Path) -> Result:
    """Lint the place registry; return a Result.

    `data` is {'status': 'ok'|'failed', 'findings': [Finding, ...]}.  Result
    exposes dict-style access (_lib.py), so callers keep reading
    `result['findings']` unchanged.

    `exit_code` is derived from the findings' severities right here (any E ->
    EXIT_ERRORS, else any finding -> EXIT_WARNINGS, else EXIT_CLEAN) so a
    headless caller reading Result.exit_code sees the same verdict the CLI
    exits with - the CLI renders findings and returns this code unchanged.
    """
    conn = open_index_db(archive_root, _LINT_REQUIRED_TABLES)
    if conn is None:
        return Result(ok=False, exit_code=EXIT_FAILURE,
                      data={'status': 'failed', 'findings': []})

    try:
        findings: list[Finding] = []
        findings += _lint_orphan_place_ids(conn)
        findings += _lint_duplicate_names(conn)
        rows, within_findings = _within_map(conn)
        findings += within_findings
        findings += _lint_dangling_within(rows)
        findings += _lint_cyclic_within(rows)
        findings += _lint_within_on_settlement(rows)
    except sqlite3.OperationalError:
        print(
            'ERROR: .cache/index.sqlite is unreadable or has an incompatible schema. '
            'Run `fha index` to rebuild.',
            file=sys.stderr,
        )
        return Result(ok=False, exit_code=EXIT_FAILURE,
                      data={'status': 'failed', 'findings': []})
    finally:
        conn.close()

    if any(f.severity == 'E' for f in findings):
        exit_code = EXIT_ERRORS
    elif findings:
        exit_code = EXIT_WARNINGS
    else:
        exit_code = EXIT_CLEAN
    return Result(exit_code=exit_code, data={'status': 'ok', 'findings': findings})


# ── Candidates: place-text clustering ──────────────────────────────────────────

_ABBREV_RE = [
    (re.compile(r'\bst\b\.?'), 'street'),
    (re.compile(r'\bco\b\.?'), 'county'),
]


def _expand_abbreviations(text: str) -> str:
    """Expand St->Street and Co->County abbreviations (TOOLING §10)."""
    for pattern, expansion in _ABBREV_RE:
        text = pattern.sub(expansion, text)
    return text


def _candidate_key(text: str) -> str:
    """
    Normalize a place_text into a sorted-token-set key so word-order and
    abbreviation variants ("Topeka, Kansas" / "Kansas, Topeka" / "Topeka Co")
    cluster together.

    TOOLING §10 includes punctuation normalization; punctuation is converted
    to token boundaries rather than deleted so `St. Mary` and `St Mary`
    remain equivalent without accidentally joining neighboring words.
    """
    norm = _expand_abbreviations(normalize_place_text(text))
    norm = re.sub(r'[^\w\s]+', ' ', norm)
    tokens = sorted(t for t in norm.split() if t)
    return ' '.join(tokens)


def _place_text_candidates(conn: sqlite3.Connection, threshold: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, place_text, date_edtf FROM claims
        WHERE (place_id IS NULL OR place_id = '')
          AND place_text IS NOT NULL AND place_text != ''
          AND status IN ('accepted', 'needs-review')
        """
    ).fetchall()

    groups: dict[str, dict] = {}
    for row in rows:
        key = _candidate_key(row['place_text'])
        if not key:
            continue
        group = groups.setdefault(key, {'labels': {}, 'claim_ids': [], 'date_bounds': []})
        group['labels'][row['place_text']] = group['labels'].get(row['place_text'], 0) + 1
        group['claim_ids'].append(row['id'])
        if row['date_edtf']:
            group['date_bounds'].append(edtf_bounds(row['date_edtf']))

    out = []
    for key, group in groups.items():
        if len(group['claim_ids']) < threshold:
            continue
        label = max(group['labels'].items(), key=lambda kv: kv[1])[0]
        mins = [b[0] for b in group['date_bounds'] if b[0]]
        maxs = [b[1] for b in group['date_bounds'] if b[1]]
        out.append({
            'label': label,
            'key': key,
            'claim_ids': sorted(group['claim_ids']),
            'claim_count': len(group['claim_ids']),
            'date_min': min(mins) if mins else None,
            'date_max': max(maxs) if maxs else None,
        })

    out.sort(key=lambda g: (-g['claim_count'], g['label']))
    return out


# ── Candidates: GPS clustering ─────────────────────────────────────────────────

def _haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_M * atan2(sqrt(a), sqrt(1 - a))


def _gps_clusters(
    archive_root: Path, fha_config: dict, known_coords: list[tuple[float, float]], threshold: int,
) -> list[dict]:
    """
    Cluster geotagged photos (>= threshold within ~150m of each other) that
    have no known place within that same radius. Returns [] when the photo
    index is absent/unreadable/corrupt - this is an optional, best-effort
    detector, not a hard dependency (mirrors `fha packet --no-photos`'s
    treatment of an unusable photoindex as "skip", not "fail").
    """
    status, _lag = photoindex_status(archive_root, fha_config)
    if status in ('absent', 'unreadable'):
        return []
    if status == 'stale':
        print(
            'WARNING: photo index may be stale - skipping GPS cluster detection. '
            'Run `fha photoindex` to refresh.',
            file=sys.stderr,
        )
        return []

    db_path = archive_root / '.cache' / 'photos.sqlite'
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        # One row per group_id (preferring the primary variant) so front/
        # back/crop/copy variants of the same logical photo count once
        # toward the cluster threshold, matching the photoindex contract.
        by_group: dict[str, tuple[str, float, float]] = {}
        for row in conn.execute(
            'SELECT path, group_id, gps_lat, gps_lon FROM photos '
            "WHERE gps_lat IS NOT NULL AND gps_lon IS NOT NULL AND path NOT LIKE 'MISSING:%' "
            'ORDER BY group_id, is_primary DESC'
        ):
            by_group.setdefault(row['group_id'], (row['path'], row['gps_lat'], row['gps_lon']))
        points = list(by_group.values())
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()

    # Drop points already near a known place - only "no known L-id coords
    # nearby" photos are candidates for a *new* place.
    far_points = []
    for path, lat, lon in points:
        if any(_haversine_meters(lat, lon, klat, klon) <= _GPS_CLUSTER_RADIUS_M for klat, klon in known_coords):
            continue
        far_points.append((path, lat, lon))

    # Simple greedy clustering: each unclustered point seeds a cluster that
    # absorbs every remaining point within the radius of the seed.
    remaining = list(far_points)
    clusters: list[list[tuple[str, float, float]]] = []
    while remaining:
        seed = remaining.pop(0)
        cluster = [seed]
        rest = []
        for point in remaining:
            if _haversine_meters(seed[1], seed[2], point[1], point[2]) <= _GPS_CLUSTER_RADIUS_M:
                cluster.append(point)
            else:
                rest.append(point)
        remaining = rest
        clusters.append(cluster)

    out = []
    for cluster in clusters:
        if len(cluster) < threshold:
            continue
        avg_lat = sum(p[1] for p in cluster) / len(cluster)
        avg_lon = sum(p[2] for p in cluster) / len(cluster)
        out.append({
            'paths': sorted(p[0] for p in cluster),
            'photo_count': len(cluster),
            'lat': avg_lat,
            'lon': avg_lon,
        })
    out.sort(key=lambda c: -c['photo_count'])
    return out


# ── Top-level query ───────────────────────────────────────────────────────────

def run_candidates(archive_root: Path, fha_config: dict, threshold: int = 3) -> Result:
    """
    Cluster unlinked place-text and GPS candidates; return a Result.

    `data` is {'status': 'ok'|'failed', 'groups': [str, ...],
    'place_text_groups': [dict, ...], 'gps_clusters': [dict, ...]}.  `groups` is a
    flat list of pre-formatted summary strings - the shape `fha report`'s §6b
    section expects (it just prints `f"- {g}"` for each).  Result exposes
    dict-style access (_lib.py), so callers keep reading `result['groups']`.
    """
    conn = open_index_db(archive_root, _CANDIDATES_REQUIRED_TABLES)
    if conn is None:
        return Result(ok=False, exit_code=EXIT_FAILURE, data={
            'status': 'failed', 'groups': [], 'place_text_groups': [], 'gps_clusters': []})

    try:
        place_text_groups = _place_text_candidates(conn, threshold)
        known_coords = []
        for row in conn.execute('SELECT lat, lon FROM places WHERE lat IS NOT NULL AND lon IS NOT NULL'):
            try:
                known_coords.append((float(row['lat']), float(row['lon'])))
            except (TypeError, ValueError):
                continue
    except sqlite3.OperationalError:
        print(
            'ERROR: .cache/index.sqlite is unreadable or has an incompatible schema. '
            'Run `fha index` to rebuild.',
            file=sys.stderr,
        )
        return Result(ok=False, exit_code=EXIT_FAILURE, data={
            'status': 'failed', 'groups': [], 'place_text_groups': [], 'gps_clusters': []})
    finally:
        conn.close()

    gps_clusters = _gps_clusters(archive_root, fha_config, known_coords, threshold)

    groups = []
    for g in place_text_groups:
        spread = f"{g['date_min']}/{g['date_max']}" if g['date_min'] or g['date_max'] else 'no dates'
        groups.append(f"{g['label']} - {g['claim_count']} claim(s), {spread}")
    for c in gps_clusters:
        groups.append(
            f"GPS cluster near {c['lat']:.4f},{c['lon']:.4f} - {c['photo_count']} photo(s), no known place nearby"
        )

    return Result(exit_code=EXIT_CLEAN, data={
        'status': 'ok',
        'groups': groups,
        'place_text_groups': place_text_groups,
        'gps_clusters': gps_clusters,
    })


# ── Geocode ─────────────────────────────────────────────────────────────────────

_GEONAMES_URL = 'https://download.geonames.org/export/dump/cities15000.zip'
_GEONAMES_MEMBER = 'cities15000.txt'

# US state name → GeoNames admin1 code (the two-letter postal code used in the
# cities dump). Lets "Fairview, Kansas" narrow to admin1 KS when several
# Fairviews share a name - without this, multi-state name collisions stay
# (correctly) ambiguous and are skipped rather than guessed.
_US_STATE_CODES = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR',
    'california': 'CA', 'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE',
    'florida': 'FL', 'georgia': 'GA', 'hawaii': 'HI', 'idaho': 'ID',
    'illinois': 'IL', 'indiana': 'IN', 'iowa': 'IA', 'kansas': 'KS',
    'kentucky': 'KY', 'louisiana': 'LA', 'maine': 'ME', 'maryland': 'MD',
    'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN', 'mississippi': 'MS',
    'missouri': 'MO', 'montana': 'MT', 'nebraska': 'NE', 'nevada': 'NV',
    'new hampshire': 'NH', 'new jersey': 'NJ', 'new mexico': 'NM', 'new york': 'NY',
    'north carolina': 'NC', 'north dakota': 'ND', 'ohio': 'OH', 'oklahoma': 'OK',
    'oregon': 'OR', 'pennsylvania': 'PA', 'rhode island': 'RI',
    'south carolina': 'SC', 'south dakota': 'SD', 'tennessee': 'TN', 'texas': 'TX',
    'utah': 'UT', 'vermont': 'VT', 'virginia': 'VA', 'washington': 'WA',
    'west virginia': 'WV', 'wisconsin': 'WI', 'wyoming': 'WY',
}

_COUNTRY_CODES = {
    'usa': 'US', 'us': 'US', 'u.s.a.': 'US', 'u.s.': 'US',
    'united states': 'US', 'united states of america': 'US',
    'canada': 'CA', 'mexico': 'MX', 'england': 'GB', 'scotland': 'GB',
    'wales': 'GB', 'united kingdom': 'GB', 'uk': 'GB', 'great britain': 'GB',
    'ireland': 'IE', 'germany': 'DE', 'france': 'FR', 'italy': 'IT',
    'australia': 'AU',
}


def _country_code_of(tokens: list[str]) -> str | None:
    for tok in tokens:
        if tok in _COUNTRY_CODES:
            return _COUNTRY_CODES[tok]
    return None


@dataclass(frozen=True)
class GeoRow:
    name: str
    asciiname: str
    altnames: frozenset[str]
    lat: float
    lon: float
    country: str
    admin1: str
    population: int


def _load_gazetteer(path: Path) -> list[GeoRow]:
    """Parse a GeoNames cities dump (tab-separated) into GeoRow records.

    Malformed lines (short column count, non-numeric coords) are skipped rather
    than aborting the load - the dump is a disposable cache, not archive truth.
    """
    rows: list[GeoRow] = []
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        return rows
    for line in text.splitlines():
        cols = line.split('\t')
        if len(cols) < 15:
            continue
        try:
            lat = float(cols[4])
            lon = float(cols[5])
        except ValueError:
            continue
        try:
            population = int(cols[14])
        except ValueError:
            population = 0
        alt = frozenset(
            a.strip().lower() for a in cols[3].split(',') if a.strip()
        )
        rows.append(GeoRow(
            name=cols[1], asciiname=cols[2], altnames=alt,
            lat=lat, lon=lon, country=cols[8], admin1=cols[10],
            population=population,
        ))
    return rows


def _download_gazetteer(dest_dir: Path) -> tuple[Path | None, str | None]:
    """Download + unzip the GeoNames cities dump into dest_dir.

    Returns (path, None) on success or (None, error_message) on any failure -
    no exception escapes, so an offline run or a network hiccup degrades to a
    clear message instead of a traceback.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    out_path = dest_dir / _GEONAMES_MEMBER
    try:
        with urllib.request.urlopen(_GEONAMES_URL, timeout=60) as resp:
            data = resp.read()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            with zf.open(_GEONAMES_MEMBER) as member:
                out_path.write_bytes(member.read())
    except Exception as e:  # urllib/ssl/zip errors are many and platform-varied
        return None, f'could not download GeoNames dump: {e}'
    return out_path, None


def _match_place(name: str, hierarchy: str | None, gazetteer: list[GeoRow]):
    """Match a registry place against the gazetteer.

    Returns:
      GeoRow       - a single high-confidence hit, OK to propose
      'ambiguous'  - more than one plausible hit; a human must decide
      None         - no plausible hit
    """
    name_norm = normalize_place_text(name)
    if not name_norm:
        return None

    candidates = [
        g for g in gazetteer
        if name_norm in (g.name.lower(), g.asciiname.lower()) or name_norm in g.altnames
    ]
    if not candidates:
        return None

    tokens = [normalize_place_text(t) for t in (hierarchy or '').split(',')]
    tokens = [t for t in tokens if t and t != name_norm]

    country = _country_code_of(tokens)
    if country:
        narrowed = [g for g in candidates if g.country == country]
        if narrowed:
            candidates = narrowed

    state = next((_US_STATE_CODES[t] for t in tokens if t in _US_STATE_CODES), None)
    if state and len(candidates) > 1:
        narrowed = [g for g in candidates if g.admin1.upper() == state]
        if narrowed:
            candidates = narrowed

    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        return 'ambiguous'
    return None


def _proposed_alt_names(geo: GeoRow, current_name: str, existing_alts: set[str]) -> list[str]:
    """Alt-name spellings worth adding: the gazetteer's name/asciiname when they
    differ from what the registry already records (case-folded comparison)."""
    have = {normalize_place_text(current_name)} | {normalize_place_text(a) for a in existing_alts}
    out: list[str] = []
    for cand in (geo.name, geo.asciiname):
        key = normalize_place_text(cand)
        if key and key not in have:
            have.add(key)
            out.append(cand)
    return out


def _block_indent(lines: list[str], start: int, end: int) -> str:
    """Infer the key indentation of a places.yaml block (the indent of its first
    `key:` line under `- id:`), defaulting to two spaces."""
    for line in lines[start + 1:end]:
        m = re.match(r'^(\s+)\S', line)
        if m:
            return m.group(1)
    return '  '


def _locate_place_block(lines: list[str], place_id: str) -> tuple[int, int] | None:
    """[start, end) line span of the `- id:` block for `place_id`, or None.

    The one block grammar every surgical places.yaml writer shares (geocode,
    set, note): the block starts at the matching `- id:` line and ends at the
    next list item at the SAME or a shallower indent - not only at column 0,
    since the registry list may be written flush-left or uniformly indented
    (both valid YAML). A deeper-indented dash is a nested alt_names/history
    item inside the block, never a sibling, so it never ends the block."""
    pid = normalize_id(place_id)
    id_re = re.compile(r'^\s*-\s+id:\s*([PSCLHpsclh]-[0-9a-hjkmnp-tv-z]{10})\s*(?:#.*)?$')
    start = None
    for i, line in enumerate(lines):
        m = id_re.match(line)
        if m and normalize_id(m.group(1)) == pid:
            start = i
            break
    if start is None:
        return None
    start_indent = len(re.match(r'^(\s*)', lines[start]).group(1))
    end = len(lines)
    for j in range(start + 1, len(lines)):
        m = re.match(r'^(\s*)-\s', lines[j])
        if m and len(m.group(1)) <= start_indent:
            end = j
            break
    return start, end


def _apply_geocode_to_yaml(
    text: str, place_id: str, lat: float, lon: float, alt_names: list[str],
) -> tuple[str, bool]:
    """Surgically set `coords:` (and add `alt_names:` when absent) on one place
    block, preserving every other line and comment. Returns (new_text, changed).

    Only the block whose `- id:` matches `place_id` (case-insensitive) is touched.
    An existing `coords:` line in that block is replaced; `alt_names:` is added
    only when the block has none (a human-curated list is never clobbered).

    Block bounds come from `_locate_place_block` (shared with the set/note
    write-backs): the block ends at the next list item at the SAME or a
    shallower indent than the matched `- id:` line - not only at column 0
    (the registry list may be written flush-left or uniformly indented, and
    a deeper-indented dash is a nested alt_names/history item, never a
    sibling)."""
    lines = text.splitlines()
    span = _locate_place_block(lines, place_id)
    if span is None:
        return text, False
    start, end = span

    indent = _block_indent(lines, start, end)
    block = lines[start:end]

    coords_line = f'{indent}coords: [{lat}, {lon}]'
    coords_idx = None
    has_alt = False
    for k, line in enumerate(block):
        if re.match(rf'^{re.escape(indent)}coords:', line):
            coords_idx = k
        if re.match(rf'^{re.escape(indent)}alt_names:', line):
            has_alt = True

    if coords_idx is not None:
        block[coords_idx] = coords_line
    else:
        block.insert(1, coords_line)  # right after the `- id:` line

    if alt_names and not has_alt:
        # The block may carry trailing blank line(s) that visually separate it
        # from the next entry; appending after them would detach alt_names from
        # its mapping. Insert before any such trailing blanks instead.
        insert_at = len(block)
        while insert_at > 0 and not block[insert_at - 1].strip():
            insert_at -= 1
        formatted = ', '.join(alt_names)
        block.insert(insert_at, f'{indent}alt_names: [{formatted}]')

    new_lines = lines[:start] + block + lines[end:]
    trailing_nl = '\n' if text.endswith('\n') else ''
    return '\n'.join(new_lines) + trailing_nl, True


def run_geocode(
    archive_root: Path,
    fha_config: dict,
    *,
    place_id: str | None = None,
    offline: bool = False,
    dry_run: bool = False,
    confirm=None,
) -> Result:
    """
    Backfill coords/alt-names for registry places lacking coordinates.

    `confirm` is a callable `(prompt: str) -> bool` gating each write (defaults
    to an interactive `[y/N]` reader); injected by tests to exercise the
    accept/decline paths without real stdin.  That interactive prompt seam is
    left exactly as-is (a deferred Phase-3 concern); only the return is
    standardized here.

    Returns a `Result` whose `data` is {'status': 'ok'|'failed'|'no-gazetteer'|
    'not-found', 'written': int, 'messages': [str, ...]}, with `changed` listing
    `places.yaml` when any coordinate block is edited.  Result exposes dict-style
    access (_lib.py), so callers keep reading `result['status']` / `result['written']`.
    """
    # status -> process exit code: a 'failed' geocode is a hard error; the soft
    # outcomes (no gazetteer, place not found) are warnings; 'ok' is clean.
    _exit_for = {
        'failed': EXIT_FAILURE, 'not-found': EXIT_WARNINGS,
        'no-gazetteer': EXIT_WARNINGS, 'ok': EXIT_CLEAN,
    }

    def _geo_result(status: str, written: int, msgs: list[str],
                    changed: list[str] | None = None) -> Result:
        return Result(
            ok=(status == 'ok'),
            exit_code=_exit_for.get(status, EXIT_FAILURE),
            data={'status': status, 'written': written, 'messages': msgs},
            changed=changed or [],
        )

    messages: list[str] = []
    if confirm is None:
        confirm = _interactive_confirm

    conn = open_index_db(archive_root, _GEOCODE_REQUIRED_TABLES, strict=True)
    if conn is None:
        return _geo_result('failed', 0, messages)

    try:
        if place_id:
            pid = normalize_id(place_id)
            rows = conn.execute(
                'SELECT id, name, hierarchy, lat, lon FROM places WHERE id = ?', (pid,)
            ).fetchall()
            if not rows:
                return _geo_result(
                    'not-found', 0,
                    [f'{fmt_id_display(pid)} not found in the place registry.'])
        else:
            rows = conn.execute(
                'SELECT id, name, hierarchy, lat, lon FROM places '
                'WHERE lat IS NULL OR lon IS NULL ORDER BY id'
            ).fetchall()

        alts_by_place: dict[str, set[str]] = {}
        for r in conn.execute('SELECT place_id, alt_name FROM place_names'):
            alts_by_place.setdefault(r['place_id'], set()).add(r['alt_name'])
    except sqlite3.OperationalError:
        print(
            'ERROR: .cache/index.sqlite is unreadable or has an incompatible schema. '
            'Run `fha index` to rebuild.',
            file=sys.stderr,
        )
        return _geo_result('failed', 0, messages)
    finally:
        conn.close()

    if not rows:
        messages.append('No places need geocoding (all have coordinates).')
        return _geo_result('ok', 0, messages)

    # Gazetteer: cached dump, or download once (unless offline).
    geonames_dir = archive_root / '.cache' / 'geonames'
    gaz_path = geonames_dir / _GEONAMES_MEMBER
    if not gaz_path.exists():
        if offline:
            messages.append(
                'No GeoNames data cached and --offline set - nothing to match against. '
                'Re-run without --offline to download the gazetteer once.'
            )
            return _geo_result('no-gazetteer', 0, messages)
        messages.append(f'Downloading GeoNames dump to {geonames_dir} (one time)...')
        downloaded, err = _download_gazetteer(geonames_dir)
        if downloaded is None:
            messages.append(err or 'gazetteer download failed.')
            return _geo_result('no-gazetteer', 0, messages)

    gazetteer = _load_gazetteer(gaz_path)
    if not gazetteer:
        messages.append(f'GeoNames dump at {gaz_path} is empty or unreadable.')
        return _geo_result('no-gazetteer', 0, messages)

    places_yaml = archive_root / 'places' / 'places.yaml'
    changed: list[str] = []
    written = 0
    for r in rows:
        pid = r['id']
        result = _match_place(r['name'], r['hierarchy'], gazetteer)
        if result is None:
            messages.append(f'{fmt_id_display(pid)} ({r["name"]}): no gazetteer match - skipped.')
            continue
        if result == 'ambiguous':
            messages.append(
                f'{fmt_id_display(pid)} ({r["name"]}): multiple gazetteer candidates - '
                'skipped (resolve by hand).'
            )
            continue

        geo = result
        alt_names = _proposed_alt_names(geo, r['name'], alts_by_place.get(pid, set()))
        prompt = (
            f'\n{fmt_id_display(pid)} {r["name"]} '
            f'[{r["hierarchy"] or "no hierarchy"}]\n'
            f'  GeoNames: {geo.name} ({geo.admin1}, {geo.country}) '
            f'pop {geo.population}\n'
            f'  Propose coords: [{geo.lat}, {geo.lon}]'
            + (f'; alt_names: {alt_names}' if alt_names else '')
            + '\n  Write this to places.yaml?'
        )
        if dry_run:
            messages.append(
                f'{fmt_id_display(pid)}: would write coords [{geo.lat}, {geo.lon}]'
                + (f'; alt_names: {alt_names}' if alt_names else '') + '.'
            )
            written += 1
            continue
        if not confirm(prompt):
            messages.append(f'{fmt_id_display(pid)}: declined - not written.')
            continue

        try:
            text = places_yaml.read_text(encoding='utf-8')
        except OSError as e:
            messages.append(f'ERROR: cannot read {places_yaml}: {e}')
            return _geo_result('failed', written, messages, changed)
        new_text, block_changed = _apply_geocode_to_yaml(text, pid, geo.lat, geo.lon, alt_names)
        if not block_changed:
            messages.append(
                f'{fmt_id_display(pid)}: block not found in places.yaml - skipped.'
            )
            continue
        try:
            places_yaml.write_text(new_text, encoding='utf-8')
        except OSError as e:
            messages.append(f'ERROR: cannot write {places_yaml}: {e}')
            return _geo_result('failed', written, messages, changed)
        written += 1
        if str(places_yaml) not in changed:
            changed.append(str(places_yaml))
        messages.append(f'{fmt_id_display(pid)}: coords written.')

    if written:
        messages.append(
            'Reminder: re-run `fha index` so the new coordinates enter the query surface.'
        )
    return _geo_result('ok', written, messages, changed)


def _interactive_confirm(prompt: str) -> bool:
    try:
        ans = input(f'{prompt} [y/N] ').strip().lower()
    except EOFError:
        return False
    return ans in ('y', 'yes')


# ── Registry write-backs (set / note) ─────────────────────────────────────────
# The human-directed edits the workbench place page drives: coordinates, the
# alt-names ("also known as") list, the dated `history:` entries, and a dated
# research note appended to `notes:`. All of them are the same text surgery
# `_apply_geocode_to_yaml` performs - one block, one key, comments and every
# other entry untouched. The AGENTS.md rule "no editing places.yaml
# coordinates without human confirmation" is satisfied by construction here:
# unlike geocode (which INFERS coordinates and so asks [y/N] per write),
# these engines only ever write values the human personally supplied - typed
# into the command or into the workbench form, previewed as a dry-run diff,
# and applied by their own click. The command IS the confirmation, the same
# way directing `fha claim` is the human's accept.

def _key_span(block: list[str], key: str, indent: str) -> tuple[int, int] | None:
    """[start, end) span of `key:` plus its continuation lines within a block.

    A mapping value may continue over following lines (a block scalar, a
    multi-line list) - every continuation line is MORE indented than the key.
    An internal blank line belongs to the value only when more-indented
    content follows it, so the span ends after the LAST more-indented line
    and a trailing blank separator between registry entries is never
    swallowed into a rewrite."""
    key_re = re.compile(rf'^{re.escape(indent)}{re.escape(key)}:')
    for k, line in enumerate(block):
        if not key_re.match(line):
            continue
        last_content = k
        for j in range(k + 1, len(block)):
            if not block[j].strip():
                continue
            if len(re.match(r'^(\s*)', block[j]).group(1)) > len(indent):
                last_content = j
            else:
                break
        return k, last_content + 1
    return None


def _set_block_key(block: list[str], key: str, indent: str, value_lines: list[str]) -> None:
    """Replace `key:`'s span with `value_lines`, or insert them into the block.

    In-place. A fresh key lands before any trailing blank separator lines
    (the same rule `_apply_geocode_to_yaml` follows for alt_names) so it
    stays attached to its own mapping instead of drifting toward the next
    entry."""
    span = _key_span(block, key, indent)
    if span is not None:
        block[span[0]:span[1]] = value_lines
        return
    insert_at = len(block)
    while insert_at > 0 and not block[insert_at - 1].strip():
        insert_at -= 1
    block[insert_at:insert_at] = value_lines


def _parse_coords_text(raw: str) -> tuple[float, float] | str:
    """Parse a human-typed "lat, lon" pair; return (lat, lon) or a plain error.

    Forgiving on shape (comma or whitespace separated, stray brackets
    tolerated) but strict on meaning: two numbers, latitude within +-90,
    longitude within +-180 - a swapped pair is the classic error and the
    range check catches half of those."""
    cleaned = str(raw).strip().strip('[]()')
    parts = [p for p in re.split(r'[,\s]+', cleaned) if p]
    if len(parts) != 2:
        return ('coordinates need two numbers - latitude, longitude - like '
                '"39.8000, -95.6000".')
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        return (f'could not read {raw!r} as numbers. Coordinates look like '
                '"39.8000, -95.6000" (latitude first).')
    if not (-90.0 <= lat <= 90.0):
        return (f'latitude {lat} is out of range (-90 to 90). Latitude comes '
                'first - the pair may be swapped.')
    if not (-180.0 <= lon <= 180.0):
        return f'longitude {lon} is out of range (-180 to 180).'
    return lat, lon


def _parse_history_lines(entries: list[str]) -> list[dict] | str:
    """Parse "PERIOD | HIERARCHY" lines into SPEC §15 history entries, or
    return a plain error string (the `_parse_coords_text` contract).

    The workbench textarea and the repeatable --history flag both speak this
    one plain shape: an EDTF period, a pipe, the hierarchy of that era. A
    line with no pipe is taken as a hierarchy with no period (legal - the
    period is optional in the spec's `{period, hierarchy}` mapping).

    The period is validated BEFORE it is written: loose forms are read the
    same way claim dates are (`normalize_date`: "circa 1858" -> "1858~"),
    and a period with no clear reading is a refusal - an unreadable period
    would silently index at the all-time `0001..9999` bounds, scrambling the
    place page's names-over-time order while looking accepted."""
    out: list[dict] = []
    for raw in entries:
        line = str(raw).strip()
        if not line:
            continue
        if '|' in line:
            period, _, hierarchy = line.partition('|')
            entry = {'period': period.strip(), 'hierarchy': hierarchy.strip()}
            if not entry['period']:
                entry.pop('period')
            else:
                normalized = normalize_date(entry['period'])
                if normalized is None:
                    return (format_edtf_error(entry['period'], field='history period')
                            + ' Periods may also be a range like "1858/1861".')
                entry['period'] = normalized
        else:
            entry = {'hierarchy': line}
        if entry.get('hierarchy'):
            out.append(entry)
    return out


def _place_write_result(archive_root: Path, place_id: str) -> tuple[Result, str | None, str | None]:
    """The shared head of run_place_set/run_place_note: validate the L-id,
    read places/places.yaml, and confirm the block exists.

    Returns (result, text, error): on any failure `result` already carries
    the refusal and `(text, error)` explain nothing further needs doing
    (`error` non-None); on success `text` is the registry's current content."""
    result = Result(data={'status': None, 'place_id': None, 'path': None})
    if not (is_valid_id(place_id) and id_type_of(place_id) == 'L'):
        result_fail(result, 'refused',
                    f'{place_id!r} is not a valid place ID. L-ids look like '
                    'L-7c1a9f4e22 - an L followed by a dash and 10 characters '
                    'from the archive alphabet.')
        return result, None, 'refused'
    pid = normalize_id(place_id)
    result.data['place_id'] = fmt_id_display(pid)
    path = archive_root / 'places' / 'places.yaml'
    result.data['path'] = str(path)
    if not path.is_file():
        result_fail(result, 'not-found',
                    f'there is no place registry at {path} yet - nothing to edit.',
                    exit_code=EXIT_WARNINGS, level='warning',
                    next_step='fha confirm place')
        return result, None, 'not-found'
    try:
        text = read_text_exact(path)
    except OSError as e:
        result_fail(result, 'refused', f'cannot read {path}: {e}')
        return result, None, 'refused'
    if _locate_place_block(text.splitlines(), pid) is None:
        result_fail(result, 'not-found',
                    f'no place {fmt_id_display(pid)} in the registry - check the '
                    f'id with `fha find {fmt_id_display(pid)}`.',
                    exit_code=EXIT_WARNINGS, level='warning',
                    next_step=f'fha find {fmt_id_display(pid)}')
        return result, None, 'not-found'
    return result, text, None


def _finish_place_write(result: Result, path: Path, old_text: str, new_text: str,
                        summary: str, dry_run: bool) -> Result:
    """The shared tail of run_place_set/run_place_note: diff preview or write."""
    if dry_run:
        result.data['status'] = 'dry-run'
        result.add('info', f'[dry-run] {summary}')
        for dline in difflib.unified_diff(
            old_text.splitlines(), new_text.splitlines(),
            fromfile='places/places.yaml (before)',
            tofile='places/places.yaml (after)', lineterm='',
        ):
            result.add('info', dline)
        result.add('info', '[dry-run] No file written. Re-run without --dry-run to apply.')
        return result
    try:
        write_text_exact(path, reapply_newline(new_text, old_text))
    except OSError as e:
        return result_fail(result, 'refused',
                           f'cannot write {path}: {e}. Check the file is not open '
                           'elsewhere and the folder is writable, then retry.')
    result.data['status'] = 'ok'
    result.note_changed(path)
    result.add('info', summary, path=path)
    result.add('info',
               'Next: run `fha index` when convenient so pages and search see the change.',
               next_step='fha index')
    return result


def run_place_set(
    archive_root: Path, place_id: str, *,
    coords: str | None = None, aka: list[str] | None = None,
    history: list[str] | None = None, dry_run: bool = False,
) -> Result:
    """Set a registry place's coords / alt_names / history; return a Result.

    Each given field is a FULL replacement of that key (the workbench form
    shows the current value and the human rewrites it); an omitted field is
    untouched. `coords` is the human's "lat, lon" text, `aka` the complete
    alt-names list, `history` "PERIOD | HIERARCHY" lines. The write is the
    same one-block text surgery geocode does; see the section comment above
    for why no [y/N] gate applies to these human-supplied values."""
    result, text, err = _place_write_result(archive_root, place_id)
    if err is not None:
        return result
    if coords is None and aka is None and history is None:
        return result_fail(result, 'refused',
                           'nothing to change - give --coords, --aka, and/or --history.')

    latlon: tuple[float, float] | None = None
    if coords is not None:
        parsed = _parse_coords_text(coords)
        if isinstance(parsed, str):
            return result_fail(result, 'refused', parsed)
        latlon = parsed
    history_entries: list[dict] | None = None
    if history is not None:
        parsed_history = _parse_history_lines(history)
        if isinstance(parsed_history, str):
            return result_fail(result, 'refused', parsed_history)
        history_entries = parsed_history
        if history_entries == [] and any(str(h).strip() for h in history):
            return result_fail(result, 'refused',
                               'no usable history entries - write one per line as '
                               '"1858/1861 | Fairview, Breton Co., Kansas Territory, USA".')

    lines = text.splitlines()
    start, end = _locate_place_block(lines, place_id)
    indent = _block_indent(lines, start, end)
    block = lines[start:end]

    changed_fields: list[str] = []
    if latlon is not None:
        _set_block_key(block, 'coords', indent,
                       [f'{indent}coords: [{latlon[0]}, {latlon[1]}]'])
        changed_fields.append(f'coordinates -> [{latlon[0]}, {latlon[1]}]')
    if aka is not None:
        cleaned = [a.strip() for a in aka if str(a).strip()]
        _set_block_key(block, 'alt_names', indent,
                       [f'{indent}alt_names: {yaml_inline(cleaned)}'])
        changed_fields.append('also-known-as -> ' + (', '.join(cleaned) or '(none)'))
    if history_entries is not None:
        # Each entry is dumped as ONE flow mapping by yaml itself (not
        # hand-spliced from yaml_inline'd scalars: a hierarchy's own commas
        # would break a hand-built `{...}`). sort_keys=False keeps the
        # spec's period-first shape.
        value_lines = [f'{indent}history:']
        for entry in history_entries:
            rendered = yaml.safe_dump(
                entry, default_flow_style=True, allow_unicode=True,
                width=10 ** 9, sort_keys=False).strip()
            value_lines.append(f'{indent}  - {rendered}')
        if not history_entries:
            value_lines = [f'{indent}history: []']
        _set_block_key(block, 'history', indent, value_lines)
        changed_fields.append(f'names-over-time -> {len(history_entries)} entr'
                              + ('y' if len(history_entries) == 1 else 'ies'))

    new_text = '\n'.join(lines[:start] + block + lines[end:])
    if text.endswith('\n'):
        new_text += '\n'
    if new_text == text:
        result.data['status'] = 'ok'
        result.add('info', 'no change - the registry already matches.')
        return result
    summary = (f'Update {result.data["place_id"]} in places/places.yaml: '
               + '; '.join(changed_fields) + '.')
    return _finish_place_write(result, Path(result.data['path']), text, new_text,
                               summary, dry_run)


def run_place_edit_note(
    archive_root: Path, place_id: str, old_text: str, text: str,
    dry_run: bool = False,
) -> Result:
    """Rewrite ONE entry of a place's `notes:` append-log; return a Result.

    The `fha person edit-note` twin for the registry: `notes:` holds dated,
    blank-line-separated paragraphs (what `run_place_note` appends), and this
    swaps exactly one of them - named by its exact current text, the same
    position-free match every per-entry editor uses (`_lib`'s
    `split_log_entries` defines the entry grammar). No match, an ambiguous
    duplicate, or an empty replacement are plain refusals with nothing
    written; removals stay a deliberate hand edit. The whole `notes:` value
    is then re-emitted as a `notes: |` block scalar, same as the appender."""
    result, registry_text, err = _place_write_result(archive_root, place_id)
    if err is not None:
        return result
    if not (old_text or '').strip():
        return result_fail(result, 'refused',
                           'no entry was named - --old-text (the entry\'s current '
                           'text) was empty.')
    if not (text or '').strip():
        return result_fail(result, 'refused',
                           'the replacement text was empty. To remove a note '
                           'entirely, edit places/places.yaml itself - this tool '
                           'only rewrites notes, never deletes them.')

    pid = normalize_id(place_id)
    try:
        entries = yaml.safe_load(registry_text) or []
    except yaml.YAMLError as e:
        return result_fail(result, 'refused',
                           f'places/places.yaml could not be parsed ({e}) - fix the '
                           'registry first (`fha places lint` points at problems).',
                           next_step='fha places lint')
    old_notes = ''
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and normalize_id(str(entry.get('id') or '')) == pid:
            old_notes = str(entry.get('notes') or '').strip()
            break

    def norm(t: str) -> str:
        return '\n'.join(ln.rstrip('\r') for ln in t.strip('\n').split('\n'))

    notes_entries = split_log_entries(old_notes)
    target = norm(old_text)
    matches = [i for i, e in enumerate(notes_entries) if norm(e) == target]
    if not matches:
        return result_fail(result, 'refused',
                           'that note was not found on this place - it may have '
                           'been edited since the page was loaded. Reload the '
                           'page and try again.')
    if len(matches) > 1:
        return result_fail(result, 'refused',
                           f'that exact note appears {len(matches)} times on this '
                           'place, so this edit cannot tell which one you meant. '
                           'Edit places/places.yaml directly for this one.')
    notes_entries[matches[0]] = norm(text)
    new_value = '\n\n'.join(notes_entries)

    lines = registry_text.splitlines()
    start, end = _locate_place_block(lines, pid)
    indent = _block_indent(lines, start, end)
    block = lines[start:end]
    value_lines = [f'{indent}notes: |']
    for ln in new_value.split('\n'):
        value_lines.append(f'{indent}  {ln}'.rstrip())
    _set_block_key(block, 'notes', indent, value_lines)

    new_text = '\n'.join(lines[:start] + block + lines[end:])
    if registry_text.endswith('\n'):
        new_text += '\n'
    summary = f'Rewrite one research note on {result.data["place_id"]} in places/places.yaml.'
    return _finish_place_write(result, Path(result.data['path']), registry_text,
                               new_text, summary, dry_run)


def run_place_note(
    archive_root: Path, place_id: str, text: str, dry_run: bool = False,
) -> Result:
    """Append a dated research note to a registry place's `notes:`; return a
    Result.

    Place notes are reference prose (SPEC §15 - loose citations welcome, a
    place is not a genealogical conclusion), and this keeps them an append
    log like a person's Research Notes: the existing value is preserved and
    the new note lands after it as its own dated paragraph, so the key is
    rewritten as a `notes: |` block scalar (the one YAML shape that holds
    paragraphs legibly). Whatever scalar style the value had before is
    parsed with plain yaml.safe_load, never guessed from the text."""
    result, registry_text, err = _place_write_result(archive_root, place_id)
    if err is not None:
        return result
    note_body = (text or '').strip()
    if not note_body:
        return result_fail(result, 'refused',
                           'there is no note text to add - --text was empty.')

    pid = normalize_id(place_id)
    try:
        entries = yaml.safe_load(registry_text) or []
    except yaml.YAMLError as e:
        return result_fail(result, 'refused',
                           f'places/places.yaml could not be parsed ({e}) - fix the '
                           'registry first (`fha places lint` points at problems).',
                           next_step='fha places lint')
    old_notes = ''
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and normalize_id(str(entry.get('id') or '')) == pid:
            old_notes = str(entry.get('notes') or '').strip()
            break

    stamp = datetime.date.today().isoformat()
    new_value = (old_notes + '\n\n' if old_notes else '') + f'{stamp}: {note_body}'

    lines = registry_text.splitlines()
    start, end = _locate_place_block(lines, pid)
    indent = _block_indent(lines, start, end)
    block = lines[start:end]
    value_lines = [f'{indent}notes: |']
    for ln in new_value.split('\n'):
        value_lines.append(f'{indent}  {ln}'.rstrip())
    _set_block_key(block, 'notes', indent, value_lines)

    new_text = '\n'.join(lines[:start] + block + lines[end:])
    if registry_text.endswith('\n'):
        new_text += '\n'
    summary = f'Add a research note to {result.data["place_id"]} in places/places.yaml.'
    return _finish_place_write(result, Path(result.data['path']), registry_text,
                               new_text, summary, dry_run)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cmd_places_lint(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE

    result = run_lint(archive_root)
    if result['status'] == 'failed':
        # The engine already printed the cause; its exit_code is EXIT_FAILURE.
        return result.exit_code

    findings = result['findings']
    for f in findings:
        print(str(f))
    if not findings:
        print('No place lint findings.')

    # The severity -> exit-code mapping lives in run_lint (one source of truth),
    # so a headless caller and this CLI can never disagree about the verdict.
    return result.exit_code


def _cmd_places_candidates(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return EXIT_FAILURE

    threshold = getattr(args, 'threshold', None)
    if threshold is None:
        threshold = 3
    if threshold < 1:
        print('ERROR: --threshold must be a positive integer.', file=sys.stderr)
        return EXIT_FAILURE

    result = run_candidates(archive_root, fha_config, threshold=threshold)
    if result['status'] == 'failed':
        return EXIT_FAILURE

    place_text_groups = result['place_text_groups']
    if place_text_groups:
        print(f'Found {len(place_text_groups)} candidate place-text cluster(s):')
        for g in place_text_groups:
            spread = f"{g['date_min']}/{g['date_max']}" if g['date_min'] or g['date_max'] else 'no dates'
            print(f"  {g['label']} - {g['claim_count']} claim(s), {spread}")
            for cid in g['claim_ids']:
                print(f"    {fmt_id_display(cid)}")
    else:
        print('No candidate place-text clusters found.')

    gps_clusters = result['gps_clusters']
    if gps_clusters:
        print(f'\nFound {len(gps_clusters)} candidate GPS cluster(s):')
        for c in gps_clusters:
            print(f"  {c['lat']:.4f},{c['lon']:.4f} - {c['photo_count']} photo(s)")
            for p in c['paths']:
                print(f"    {p}")
    else:
        print('\nNo candidate GPS clusters found.')

    return EXIT_CLEAN


def _cmd_places_geocode(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return EXIT_FAILURE

    place_id = getattr(args, 'place', None)
    # --all is the explicit opt-in for the bulk mode; the CLI requires a scope so
    # a bare `fha places geocode` can't silently process every place. The engine
    # itself needs no all_places flag - place_id=None IS the all-mode.
    all_places = getattr(args, 'all', False)
    if not place_id and not all_places:
        print('ERROR: pass --place L-id or --all.', file=sys.stderr)
        return EXIT_FAILURE
    if place_id and not (is_valid_id(place_id) and id_type_of(place_id) == 'L'):
        print(f'ERROR: {place_id!r} is not a valid L-id.', file=sys.stderr)
        return EXIT_FAILURE

    result = run_geocode(
        archive_root, fha_config,
        place_id=place_id,
        offline=getattr(args, 'offline', False),
        dry_run=getattr(args, 'dry_run', False),
    )
    for m in result['messages']:
        print(m)

    status = result['status']
    if status == 'failed':
        return EXIT_FAILURE
    if status in ('no-gazetteer', 'not-found'):
        return EXIT_WARNINGS
    return EXIT_CLEAN


def _emit_place_result(result: Result) -> int:
    """Print a set/note Result's messages the standard engine way."""
    for msg in result.messages:
        stream = sys.stderr if msg.level == 'error' else sys.stdout
        prefix = 'ERROR: ' if msg.level == 'error' else ''
        print(f'{prefix}{msg.text}', file=stream)
    return result.exit_code


def _cmd_places_set(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    aka = None
    if getattr(args, 'aka', None) is not None:
        # Each --aka value is ONE name, verbatim - "Washington, D.C." keeps
        # its comma (P2 codex finding, round 2, PR #31: the old comma-split
        # made a comma-bearing alias impossible to record or round-trip).
        aka = [str(a).strip() for a in args.aka]
    return _emit_place_result(run_place_set(
        archive_root, args.place_id,
        coords=getattr(args, 'coords', None), aka=aka,
        history=getattr(args, 'history', None),
        dry_run=bool(getattr(args, 'dry_run', False))))


def _cmd_places_note(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    return _emit_place_result(run_place_note(
        archive_root, args.place_id, args.text,
        dry_run=bool(getattr(args, 'dry_run', False))))


def _cmd_places_edit_note(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    return _emit_place_result(run_place_edit_note(
        archive_root, args.place_id, args.old_text, args.text,
        dry_run=bool(getattr(args, 'dry_run', False))))


# User-facing --help text (the module docstring stays developer-facing).
_CLI_DESCRIPTION = """\
Keep your places tidy, fill in their coordinates, and record what you learn.

  fha places lint                    Check the place registry for problems
  fha places candidates              Recurring place-text worth a registry entry
  fha places geocode (--place L-id | --all)  Fill in coordinates (offline, confirmed one by one)
  fha places set L-id [--coords "LAT, LON"] [--aka NAME]... [--history "PERIOD | HIERARCHY"]...
  fha places note L-id --text "..."  Append a dated research note to the place
  fha places edit-note L-id --old-text "..." --text "..."  Rewrite one existing note

"Am I spelling this town three different ways?", "which place should become a
real entry?", "fill in the coordinates for these towns", "move the pin",
"note what I found out about this town.\""""


def register(subs: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register 'places' onto the main fha parser."""
    p = subs.add_parser(
        'places',
        help='Place registry hygiene (lint) and recurrence detection (candidates)',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    deferred = p.add_subparsers(dest='places_command', metavar='SUBCOMMAND')

    lint_p = deferred.add_parser('lint', help='Check places/places.yaml + claims.place_id for registry hygiene')
    lint_p.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS, help='Archive root (auto-detected if omitted).')
    lint_p.set_defaults(func=_cmd_places_lint)

    candidates_p = deferred.add_parser('candidates', help='Detect recurring unlinked place_text and GPS clusters')
    candidates_p.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS, help='Archive root (auto-detected if omitted).')
    candidates_p.add_argument('--threshold', type=int, default=3, metavar='N',
                               help='Minimum occurrences for a candidate cluster (default: 3).')
    candidates_p.set_defaults(func=_cmd_places_candidates)

    geocode_p = deferred.add_parser('geocode', help='Backfill coords/alt-names from the offline GeoNames dump')
    geocode_p.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS, help='Archive root (auto-detected if omitted).')
    geocode_p.add_argument('--place', metavar='L-id', help='Geocode only this place.')
    geocode_p.add_argument('--all', action='store_true', help='Geocode every registry place lacking coordinates.')
    geocode_p.add_argument('--offline', action='store_true',
                           help='Never download; match only against an already-cached GeoNames dump.')
    geocode_p.add_argument('--dry-run', action='store_true', dest='dry_run',
                           help='Preview proposed changes without writing.')
    geocode_p.set_defaults(func=_cmd_places_geocode)

    set_p = deferred.add_parser(
        'set', help="Set a place's coordinates, alt-names, or names-over-time (full replace per field)")
    set_p.add_argument('place_id', metavar='L-id', help='The place to update.')
    set_p.add_argument('--coords', metavar='"LAT, LON"',
                       help='New coordinates, latitude first - e.g. "39.8000, -95.6000".')
    set_p.add_argument('--aka', metavar='NAME', action='append',
                       help='One also-known-as name, taken verbatim (commas and all - '
                            '"Washington, D.C." is one name); repeat the flag for more. '
                            'The given set replaces the old list.')
    set_p.add_argument('--history', metavar='"PERIOD | HIERARCHY"', action='append',
                       help='One names-over-time entry; repeat the flag for more (replaces the old list).')
    set_p.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS,
                       help='Archive root (auto-detected if omitted).')
    set_p.add_argument('--dry-run', action='store_true', dest='dry_run',
                       help='Preview the change without writing.')
    set_p.set_defaults(func=_cmd_places_set)

    note_p = deferred.add_parser('note', help='Append a dated research note to a place')
    note_p.add_argument('place_id', metavar='L-id', help='The place the note is about.')
    note_p.add_argument('--text', metavar='TEXT', required=True,
                        help='The note, in your own words (loose citations welcome).')
    note_p.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS,
                        help='Archive root (auto-detected if omitted).')
    note_p.add_argument('--dry-run', action='store_true', dest='dry_run',
                        help='Preview the change without writing.')
    note_p.set_defaults(func=_cmd_places_note)

    edit_note_p = deferred.add_parser(
        'edit-note', help="Rewrite one existing research note on a place (named by its exact text)")
    edit_note_p.add_argument('place_id', metavar='L-id', help='The place whose note is being corrected.')
    edit_note_p.add_argument('--old-text', metavar='TEXT', required=True, dest='old_text',
                             help="The note's current text, exactly as it stands.")
    edit_note_p.add_argument('--text', metavar='TEXT', required=True,
                             help='The corrected note text.')
    edit_note_p.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS,
                             help='Archive root (auto-detected if omitted).')
    edit_note_p.add_argument('--dry-run', action='store_true', dest='dry_run',
                             help='Preview the change without writing.')
    edit_note_p.set_defaults(func=_cmd_places_edit_note)

    # Bare `fha places` (no verb) is a usage error, not a tool failure: exit 2,
    # matching `fha person`/`fha confirm` (audit flag 15).
    p.set_defaults(func=lambda a: p.print_help() or EXIT_ERRORS)
    return p


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha places',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subs = parser.add_subparsers(dest='places_command', metavar='SUBCOMMAND')

    lint_p = subs.add_parser('lint')
    lint_p.add_argument('--root', metavar='PATH')
    lint_p.set_defaults(func=_cmd_places_lint)

    candidates_p = subs.add_parser('candidates')
    candidates_p.add_argument('--root', metavar='PATH')
    candidates_p.add_argument('--threshold', type=int, default=3)
    candidates_p.set_defaults(func=_cmd_places_candidates)

    geocode_p = subs.add_parser('geocode')
    geocode_p.add_argument('--root', metavar='PATH')
    geocode_p.add_argument('--place', metavar='L-id')
    geocode_p.add_argument('--all', action='store_true')
    geocode_p.add_argument('--offline', action='store_true')
    geocode_p.add_argument('--dry-run', action='store_true', dest='dry_run')
    geocode_p.set_defaults(func=_cmd_places_geocode)

    set_p = subs.add_parser('set')
    set_p.add_argument('place_id', metavar='L-id')
    set_p.add_argument('--coords', metavar='"LAT, LON"')
    set_p.add_argument('--aka', metavar='NAME', action='append')
    set_p.add_argument('--history', metavar='"PERIOD | HIERARCHY"', action='append')
    set_p.add_argument('--root', metavar='PATH')
    set_p.add_argument('--dry-run', action='store_true', dest='dry_run')
    set_p.set_defaults(func=_cmd_places_set)

    note_p = subs.add_parser('note')
    note_p.add_argument('place_id', metavar='L-id')
    note_p.add_argument('--text', metavar='TEXT', required=True)
    note_p.add_argument('--root', metavar='PATH')
    note_p.add_argument('--dry-run', action='store_true', dest='dry_run')
    note_p.set_defaults(func=_cmd_places_note)

    edit_note_p = subs.add_parser('edit-note')
    edit_note_p.add_argument('place_id', metavar='L-id')
    edit_note_p.add_argument('--old-text', metavar='TEXT', required=True, dest='old_text')
    edit_note_p.add_argument('--text', metavar='TEXT', required=True)
    edit_note_p.add_argument('--root', metavar='PATH')
    edit_note_p.add_argument('--dry-run', action='store_true', dest='dry_run')
    edit_note_p.set_defaults(func=_cmd_places_edit_note)

    args = parser.parse_args(argv)
    if not getattr(args, 'func', None):
        parser.print_help()
        return EXIT_ERRORS
    return args.func(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
