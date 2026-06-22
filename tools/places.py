#!/usr/bin/env python3
"""
places.py — fha places lint / fha places candidates: place registry hygiene
and recurrence detection (TOOLING §10, SPEC §15).

  fha places lint [--root PATH]
  fha places candidates [--root PATH] [--threshold N]
  fha places geocode [--place L-id] [--all] [--offline] [--root PATH]

`fha places lint` checks `places/places.yaml` (via the index's `places`/
`place_names`/`place_history` tables) plus `claims.place_id` for registry
hygiene:
  - orphan L-ids referenced by a claim's `place_id` but absent from the registry
  - duplicate place names (case-folded across `name` + `alt_names`)
  - dangling `within:` links (target L-id not in the registry)
  - cyclic `within:` chains
  - a `within:` link whose source is itself a settlement — i.e. it is already
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
is a research judgment, not a string match — ambiguous matches are skipped, not
guessed), and **every write requires interactive `[y/N]` confirmation**. Writes
edit `places/places.yaml` surgically (the matched block only) so the file's hand
comments and unrelated entries are preserved without needing `ruamel.yaml`.

CODE MAP
--------
  Lint
    _within_map, _lint_orphan_place_ids, _lint_duplicate_names,
    _lint_dangling_within, _lint_cyclic_within, _lint_within_on_settlement
    run_lint

  Candidates
    _expand_abbreviations, _candidate_key  — normalization for clustering
    _place_text_candidates                 — unlinked place_text clusters
    _haversine_meters, _gps_clusters        — photo-GPS clusters
    run_candidates

  Geocode
    _US_STATE_CODES, _country_code_of      — hierarchy-token → code helpers
    GeoRow, _load_gazetteer                — parse the GeoNames dump
    _download_gazetteer                    — one-time offline-dump fetch
    _match_place                           — name+hierarchy → unique hit / ambiguous / none
    _apply_geocode_to_yaml                 — surgical places.yaml block edit
    run_geocode                            — gather candidates, match, (confirm) write

  CLI
    _cmd_places_lint, _cmd_places_candidates, _cmd_places_geocode,
    register, _standalone_main
"""

from __future__ import annotations

import argparse
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

from _lib import (
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    Finding,
    FhaConfigError,
    edtf_bounds,
    fmt_id_display,
    id_type_of,
    is_valid_id,
    load_fha_yaml,
    normalize_id,
    normalize_place_text,
    open_index_db,
    photoindex_status,
    resolve_root_arg,
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
                f'{fmt_id_display(row["id"])} has a non-string within: value ({raw!r}) — within: must be an L-id string',
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
                f'links within: {fmt_id_display(target)} — settlement-to-jurisdiction containment '
                'belongs in history:, not within:',
            ))
    return findings


def run_lint(archive_root: Path) -> dict:
    """Returns {'status': 'ok'|'failed', 'findings': [Finding, ...]}."""
    conn = open_index_db(archive_root, _LINT_REQUIRED_TABLES)
    if conn is None:
        return {'status': 'failed', 'findings': []}

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
        return {'status': 'failed', 'findings': []}
    finally:
        conn.close()

    return {'status': 'ok', 'findings': findings}


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
    index is absent/unreadable/corrupt — this is an optional, best-effort
    detector, not a hard dependency (mirrors `fha packet --no-photos`'s
    treatment of an unusable photoindex as "skip", not "fail").
    """
    status, _lag = photoindex_status(archive_root, fha_config)
    if status in ('absent', 'unreadable'):
        return []
    if status == 'stale':
        print(
            'WARNING: photo index may be stale — skipping GPS cluster detection. '
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

    # Drop points already near a known place — only "no known L-id coords
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

def run_candidates(archive_root: Path, fha_config: dict, threshold: int = 3) -> dict:
    """
    Returns {'status': 'ok'|'failed', 'groups': [str, ...],
    'place_text_groups': [dict, ...], 'gps_clusters': [dict, ...]}.

    `groups` is a flat list of pre-formatted summary strings — the shape
    `fha report`'s §6b section expects (it just prints `f"- {g}"` for each).
    """
    conn = open_index_db(archive_root, _CANDIDATES_REQUIRED_TABLES)
    if conn is None:
        return {'status': 'failed', 'groups': [], 'place_text_groups': [], 'gps_clusters': []}

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
        return {'status': 'failed', 'groups': [], 'place_text_groups': [], 'gps_clusters': []}
    finally:
        conn.close()

    gps_clusters = _gps_clusters(archive_root, fha_config, known_coords, threshold)

    groups = []
    for g in place_text_groups:
        spread = f"{g['date_min']}/{g['date_max']}" if g['date_min'] or g['date_max'] else 'no dates'
        groups.append(f"{g['label']} — {g['claim_count']} claim(s), {spread}")
    for c in gps_clusters:
        groups.append(
            f"GPS cluster near {c['lat']:.4f},{c['lon']:.4f} — {c['photo_count']} photo(s), no known place nearby"
        )

    return {
        'status': 'ok',
        'groups': groups,
        'place_text_groups': place_text_groups,
        'gps_clusters': gps_clusters,
    }


# ── Geocode ─────────────────────────────────────────────────────────────────────

_GEONAMES_URL = 'https://download.geonames.org/export/dump/cities15000.zip'
_GEONAMES_MEMBER = 'cities15000.txt'

# US state name → GeoNames admin1 code (the two-letter postal code used in the
# cities dump). Lets "Fairview, Kansas" narrow to admin1 KS when several
# Fairviews share a name — without this, multi-state name collisions stay
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
    than aborting the load — the dump is a disposable cache, not archive truth.
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

    Returns (path, None) on success or (None, error_message) on any failure —
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
      GeoRow       — a single high-confidence hit, OK to propose
      'ambiguous'  — more than one plausible hit; a human must decide
      None         — no plausible hit
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


def _apply_geocode_to_yaml(
    text: str, place_id: str, lat: float, lon: float, alt_names: list[str],
) -> tuple[str, bool]:
    """Surgically set `coords:` (and add `alt_names:` when absent) on one place
    block, preserving every other line and comment. Returns (new_text, changed).

    Only the block whose `- id:` matches `place_id` (case-insensitive) is touched.
    An existing `coords:` line in that block is replaced; `alt_names:` is added
    only when the block has none (a human-curated list is never clobbered)."""
    lines = text.splitlines()
    pid = normalize_id(place_id)

    id_re = re.compile(r'^\s*-\s+id:\s*([PSCLHpsclh]-[0-9a-hjkmnp-tv-z]{10})\s*(?:#.*)?$')
    start = None
    for i, line in enumerate(lines):
        m = id_re.match(line)
        if m and normalize_id(m.group(1)) == pid:
            start = i
            break
    if start is None:
        return text, False

    # Block runs until the next top-level list item (a line starting at column 0
    # with `- `) or EOF.
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r'^-\s', lines[j]):
            end = j
            break

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
    all_places: bool = False,
    offline: bool = False,
    dry_run: bool = False,
    confirm=None,
) -> dict:
    """
    Backfill coords/alt-names for registry places lacking coordinates.

    `confirm` is a callable `(prompt: str) -> bool` gating each write (defaults
    to an interactive `[y/N]` reader); injected by tests to exercise the
    accept/decline paths without real stdin.

    Returns {'status': 'ok'|'failed'|'no-gazetteer'|'not-found',
             'written': int, 'messages': [str, ...]}.
    """
    messages: list[str] = []
    if confirm is None:
        confirm = _interactive_confirm

    conn = open_index_db(archive_root, _GEOCODE_REQUIRED_TABLES, strict=True)
    if conn is None:
        return {'status': 'failed', 'written': 0, 'messages': messages}

    try:
        if place_id:
            pid = normalize_id(place_id)
            rows = conn.execute(
                'SELECT id, name, hierarchy, lat, lon FROM places WHERE id = ?', (pid,)
            ).fetchall()
            if not rows:
                return {'status': 'not-found', 'written': 0,
                        'messages': [f'{fmt_id_display(pid)} not found in the place registry.']}
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
        return {'status': 'failed', 'written': 0, 'messages': messages}
    finally:
        conn.close()

    if not rows:
        messages.append('No places need geocoding (all have coordinates).')
        return {'status': 'ok', 'written': 0, 'messages': messages}

    # Gazetteer: cached dump, or download once (unless offline).
    geonames_dir = archive_root / '.cache' / 'geonames'
    gaz_path = geonames_dir / _GEONAMES_MEMBER
    if not gaz_path.exists():
        if offline:
            messages.append(
                'No GeoNames data cached and --offline set — nothing to match against. '
                'Re-run without --offline to download the gazetteer once.'
            )
            return {'status': 'no-gazetteer', 'written': 0, 'messages': messages}
        messages.append(f'Downloading GeoNames dump to {geonames_dir} (one time)...')
        downloaded, err = _download_gazetteer(geonames_dir)
        if downloaded is None:
            messages.append(err or 'gazetteer download failed.')
            return {'status': 'no-gazetteer', 'written': 0, 'messages': messages}

    gazetteer = _load_gazetteer(gaz_path)
    if not gazetteer:
        messages.append(f'GeoNames dump at {gaz_path} is empty or unreadable.')
        return {'status': 'no-gazetteer', 'written': 0, 'messages': messages}

    places_yaml = archive_root / 'places' / 'places.yaml'
    written = 0
    for r in rows:
        pid = r['id']
        result = _match_place(r['name'], r['hierarchy'], gazetteer)
        if result is None:
            messages.append(f'{fmt_id_display(pid)} ({r["name"]}): no gazetteer match — skipped.')
            continue
        if result == 'ambiguous':
            messages.append(
                f'{fmt_id_display(pid)} ({r["name"]}): multiple gazetteer candidates — '
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
            messages.append(f'{fmt_id_display(pid)}: declined — not written.')
            continue

        try:
            text = places_yaml.read_text(encoding='utf-8')
        except OSError as e:
            messages.append(f'ERROR: cannot read {places_yaml}: {e}')
            return {'status': 'failed', 'written': written, 'messages': messages}
        new_text, changed = _apply_geocode_to_yaml(text, pid, geo.lat, geo.lon, alt_names)
        if not changed:
            messages.append(
                f'{fmt_id_display(pid)}: block not found in places.yaml — skipped.'
            )
            continue
        try:
            places_yaml.write_text(new_text, encoding='utf-8')
        except OSError as e:
            messages.append(f'ERROR: cannot write {places_yaml}: {e}')
            return {'status': 'failed', 'written': written, 'messages': messages}
        written += 1
        messages.append(f'{fmt_id_display(pid)}: coords written.')

    if written:
        messages.append(
            'Reminder: re-run `fha index` so the new coordinates enter the query surface.'
        )
    return {'status': 'ok', 'written': written, 'messages': messages}


def _interactive_confirm(prompt: str) -> bool:
    try:
        ans = input(f'{prompt} [y/N] ').strip().lower()
    except EOFError:
        return False
    return ans in ('y', 'yes')


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cmd_places_lint(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE

    result = run_lint(archive_root)
    if result['status'] == 'failed':
        return EXIT_FAILURE

    findings = result['findings']
    for f in findings:
        print(str(f))
    if not findings:
        print('No place lint findings.')

    if any(f.severity == 'E' for f in findings):
        return EXIT_ERRORS
    if any(f.severity == 'W' for f in findings):
        return EXIT_WARNINGS
    return EXIT_CLEAN


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
            print(f"  {g['label']} — {g['claim_count']} claim(s), {spread}")
            for cid in g['claim_ids']:
                print(f"    {fmt_id_display(cid)}")
    else:
        print('No candidate place-text clusters found.')

    gps_clusters = result['gps_clusters']
    if gps_clusters:
        print(f'\nFound {len(gps_clusters)} candidate GPS cluster(s):')
        for c in gps_clusters:
            print(f"  {c['lat']:.4f},{c['lon']:.4f} — {c['photo_count']} photo(s)")
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
    all_places = getattr(args, 'all', False)
    if not place_id and not all_places:
        print('ERROR: pass --place L-id or --all.', file=sys.stderr)
        return EXIT_FAILURE
    if place_id and not (is_valid_id(place_id) and id_type_of(place_id) == 'L'):
        print(f'ERROR: {place_id!r} is not a valid L-id.', file=sys.stderr)
        return EXIT_FAILURE

    result = run_geocode(
        archive_root, fha_config,
        place_id=place_id, all_places=all_places,
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


def register(subs: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register 'places' onto the main fha parser."""
    p = subs.add_parser(
        'places',
        help='Place registry hygiene (lint) and recurrence detection (candidates)',
        description=__doc__,
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

    p.set_defaults(func=lambda a: p.print_help() or EXIT_FAILURE)
    return p


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha places',
        description=__doc__,
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

    args = parser.parse_args(argv)
    if not getattr(args, 'func', None):
        parser.print_help()
        return EXIT_FAILURE
    return args.func(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
