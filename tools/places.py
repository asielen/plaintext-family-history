#!/usr/bin/env python3
"""
places.py - fha places: place registry hygiene, recurrence detection, and
human-directed registry edits (TOOLING §10, SPEC §15).

  fha places lint [--root PATH]
  fha places candidates [--root PATH] [--threshold N] [--include-suggested]
  fha places geocode [--place L-id] [--all] [--offline] [--root PATH]
  fha places set l-id [--coords "LAT, LON"] [--aka NAME]... [--history "PERIOD | HIERARCHY"]...
  fha places note l-id --text TEXT [--dry-run]

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
`fha cooccur`: distinct *unlinked*, active (accepted, or looked at and still open) claim
`place_text` values (no `place_id`) are normalized (case-fold, punctuation, whitespace, St/Street and Co/County
expansion) and clustered by a sorted token-set key so word-order variants and
abbreviation variants land in the same group; groups with >= `--threshold`
(default 3) occurrences are surfaced with their claim count and EDTF date
spread. `--include-suggested` widens the status filter to also count
`status: suggested` claims - off by default (a still-unreviewed claim might
be disputed away before it becomes fact), meant for a one-time backlog scan
right after a fresh `fha gedcom import` (which mints everything as
`suggested`), not everyday use. A second, independent detector clusters
geotagged photos (`.cache/photos.sqlite`) that have no known place within
~150m of them.

`fha places geocode` (TOOLING §10) fills in `coords` for registry places that
lack them, plus a missing county, state or country in `hierarchy` and GeoNames'
spellings as `alt_names`, from the **GeoNames** export saved in
`.cache/geonames/`: the country/state/county name files and one dump per
country the registry names, downloaded once (never with `--offline`), filtered
to towns and divisions and cached as SQLite. No live API. A place's name is
looked up in its countries and every other part of its hierarchy must be the
candidate's commune, county, state or country; only a *single* fitting
candidate is proposed (place identity is a research judgment, not a string
match). Every proposal is shown in one table with one `[y/N]`; a yes writes
them all in one atomic rewrite. Places with several candidates list each with
its `fha places set --coords` command. Writes edit `places/places.yaml`
surgically (the matched blocks only) so hand comments and unrelated entries
survive without needing `ruamel.yaml`.

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
    _fold, _geo_key, _hierarchy_parts      : name comparison forms; hierarchy split
    GeoRow, _feature_rank                  : one GeoNames feature and its level
    _fetch_url                             : the one network call (download to .part, rename)
    _read_country_info, _open_gazetteer_db,
    _import_admin_codes, _import_country   : GeoNames files to .cache/geonames/gazetteer.sqlite
    _Gazetteer                             : lookups: names, divisions, labels, descriptions
    _place_name_and_context, _place_countries : what to look up, and in which countries
    _says_division, _skipped_levels        : tie-break helpers
    _match_place                           : name+hierarchy to hit / ambiguous / partial / none
    _filled_hierarchy, _proposed_alt_names : what a hit adds besides coordinates
    _locate_place_block                    : one `- id:` block's line span (shared grammar)
    _apply_geocode_to_yaml                 : surgical places.yaml block edit
    _hierarchy_is_one_plain_line           : is a hierarchy safe to rewrite (no comment)
    _prepare_gazetteer                     : download/import what the places need
    _registry_country_spellings            : write the country the registry's way
    _proposal_table, run_geocode           : one table, one yes, one atomic write

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
import os
import re
import shutil
import sqlite3
import sys
import unicodedata
import urllib.request
import zipfile
from dataclasses import dataclass
from math import atan2, cos, radians, sin, sqrt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import yaml

from _lib import (
    PARKED_CLAIM_SQL,
    configure_utf8_stdout,
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    Finding,
    FhaConfigError,
    Result,
    edtf_bounds,
    format_date_spread,
    expand_place_abbreviations,
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
    place_text_cluster_key,
    read_text_exact,
    reapply_newline,
    resolve_root_arg,
    result_fail,
    match_log_entry,
    split_log_entries,
    write_text_exact_atomic,
    yaml_inline,
    safe_load_yaml,
    shell_quote,
)

configure_utf8_stdout()

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

# `_expand_abbreviations`/`_candidate_key` moved to `_lib.py` as
# `expand_place_abbreviations`/`place_text_cluster_key` (issue #79 point 3):
# `fha claim`'s write-time registry lookup needs this exact normalization too
# (a write-time auto-attach and this file's own clustering must never
# disagree about what counts as "the same place"), and tools never import
# tools (TOOLING §15) - _lib is the one place both can share it from. Kept
# as thin aliases here so this file's own call sites below, and any test
# importing `places._expand_abbreviations`/`places._candidate_key`, are
# unchanged.
_expand_abbreviations = expand_place_abbreviations
_candidate_key = place_text_cluster_key


def _place_text_candidates(
    conn: sqlite3.Connection, threshold: int, *, include_suggested: bool = False,
) -> list[dict]:
    """Cluster distinct unlinked `place_text` values into candidate groups.

    Normal use (the everyday `fha places candidates`/`fha report` §6b view)
    scopes to ACTIVE claims only - `accepted`, plus `suggested` claims a
    reviewer already looked at (a `reviewed:` date, SPEC §8.1) - deliberately
    excluding never-reviewed `suggested`: a still-unreviewed claim's `place_text` might be
    disputed away by the human before it ever becomes a fact worth a
    registry entry, so folding it into "here is what your archive actually
    says" would overstate the case.

    `include_suggested=True` widens the status filter to also count
    `suggested` claims (Codex review, PR #150, following issue #79 point 4).
    This exists for exactly one caller today: the setup-interview skill's
    one-time, pre-family-interview backlog check. `fha gedcom import` - an
    order this project's own docs sanction running BEFORE that interview -
    mints every claim at `status: suggested`, so a fresh import with a real
    place-text backlog and zero registered places produced ZERO candidate
    clusters under the accepted-or-reviewed-only filter, silently missing
    the exact scenario that check exists to catch. Widening is safe there
    because that check is a one-time informational scan ("is there a
    backlog worth an interruption"), not a "confirmed data" report - it
    never counts as ground truth and never itself decides anything. The
    flag is opt-in and defaults to the normal, conservative scope so every
    other caller (this file's own CLI default, `fha report`) is unaffected.
    """
    status_filter = ("c.status IN ('accepted', 'suggested')" if include_suggested
                     else f"(c.status = 'accepted' OR {PARKED_CLAIM_SQL})")
    rows = conn.execute(
        f"""
        SELECT id, place_text, date_edtf FROM claims c
        WHERE (place_id IS NULL OR place_id = '')
          AND place_text IS NOT NULL AND place_text != ''
          AND {status_filter}
        """,
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
        date_min, date_max = _known_date_spread(group['date_bounds'])
        out.append({
            'label': label,
            'key': key,
            'claim_ids': sorted(group['claim_ids']),
            'claim_count': len(group['claim_ids']),
            'date_min': date_min,
            'date_max': date_max,
        })

    out.sort(key=lambda g: (-g['claim_count'], g['label']))
    return out


_OPEN_LOW = '0001-01-01'
_OPEN_HIGH = '9999-12-31'


def _known_date_spread(bounds: list) -> tuple[str | None, str | None]:
    """The earliest and latest dates a place's claims actually name.

    `edtf_bounds` writes an open side as a sentinel ('0001-01-01' for
    "before 1900", '9999-12-31' for "after 1920"). Those are not dates, so
    they never reach the spread line. Dropping them from each side
    separately (the #218 audit fix) turned "before 1900" plus "after 1920"
    into a spread that ran backwards, 1920-01-01/1900-12-31 (Codex, #218).
    Each claim's open side now stands in for its known side instead, so the
    spread always runs forward over the dates the claims name. A side is
    left open (None, shown "from …" / "up to …") only when EVERY dated
    claim is open on it, e.g. a place known only from "after" dates.
    """
    lows: list[str] = []
    highs: list[str] = []
    open_low = open_high = True
    for lo, hi in bounds:
        lo = lo if lo and lo != _OPEN_LOW else None
        hi = hi if hi and hi != _OPEN_HIGH else None
        if not lo and not hi:
            continue                     # undated: names no date at all
        open_low = open_low and lo is None
        open_high = open_high and hi is None
        lows.append(lo or hi)
        highs.append(hi or lo)
    if not lows:
        return None, None
    return (None if open_low else min(lows),
            None if open_high else max(highs))


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

def run_candidates(
    archive_root: Path, fha_config: dict, threshold: int = 3, *, include_suggested: bool = False,
) -> Result:
    """
    Cluster unlinked place-text and GPS candidates; return a Result.

    `data` is {'status': 'ok'|'failed', 'groups': [str, ...],
    'place_text_groups': [dict, ...], 'gps_clusters': [dict, ...]}.  `groups` is a
    flat list of pre-formatted summary strings - the shape `fha report`'s §6b
    section expects (it just prints `f"- {g}"` for each).  Result exposes
    dict-style access (_lib.py), so callers keep reading `result['groups']`.

    `include_suggested` (Codex review, PR #150) widens the place-text
    clustering to also count `status: suggested` claims - see
    `_place_text_candidates`'s docstring for why this stays off by default
    (`fha report` and the plain CLI both leave it False) and who actually
    needs it on (the setup-interview skill's one-time pre-family-interview
    backlog scan, which runs right after a `fha gedcom import` that mints
    everything as `suggested`). GPS clustering is untouched either way - it
    has no claim `status:` to filter on.
    """
    conn = open_index_db(archive_root, _CANDIDATES_REQUIRED_TABLES)
    if conn is None:
        return Result(ok=False, exit_code=EXIT_FAILURE, data={
            'status': 'failed', 'groups': [], 'place_text_groups': [], 'gps_clusters': []})

    try:
        place_text_groups = _place_text_candidates(conn, threshold, include_suggested=include_suggested)
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
        spread = format_date_spread(g['date_min'], g['date_max'])
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
# The gazetteer is GeoNames' free export (download.geonames.org/export/dump/):
#
#   countryInfo.txt       every country's name and two-letter code
#   admin1CodesASCII.txt  every state/province name, keyed "US.IL"
#   admin2Codes.txt       every county-level name, keyed "US.IL.031"
#   {CC}.zip              one country's every feature, of which geocode keeps
#                         the populated places (class P: cities, towns,
#                         villages) and the administrative divisions (class A:
#                         states, counties, communes, and the country itself)
#
# The three name files are fetched once; a country's dump is fetched only when
# a place in the registry names that country (or a state only it has). Each
# dump is read once, filtered to classes P and A while streaming, and stored
# in `.cache/geonames/gazetteer.sqlite`, so later runs look names up instead of
# re-reading a 300 MB text file. Everything in `.cache/` is disposable: delete
# the folder and the next run fetches it again.

_GEONAMES_BASE_URL = 'https://download.geonames.org/export/dump/'
_GEONAMES_NAME_FILES = ('countryInfo.txt', 'admin1CodesASCII.txt', 'admin2Codes.txt')
_GAZETTEER_DB = 'gazetteer.sqlite'
# Bump when the gazetteer tables change shape: an older cache is then rebuilt
# from the dumps already on disk, never trusted.
_GAZETTEER_SCHEMA = '1'
_DOWNLOAD_TIMEOUT_S = 60
_KEPT_FEATURE_CLASSES = ('P', 'A')

# Country spellings genealogy hierarchies use that countryInfo.txt does not
# carry (it has "United States", "United Kingdom", "Poland", "France", ...).
# Two-letter and three-letter codes are deliberately absent apart from USA/US/
# UK: as the last part of a hierarchy, "IL" or "Ind." is a state, not Israel
# or India.
_COUNTRY_EXTRA_NAMES = {
    'usa': 'US', 'us': 'US', 'u s a': 'US', 'u s': 'US',
    'united states of america': 'US', 'america': 'US',
    'england': 'GB', 'scotland': 'GB', 'wales': 'GB', 'northern ireland': 'GB',
    'great britain': 'GB', 'britain': 'GB', 'uk': 'GB', 'u k': 'GB',
    'holland': 'NL', 'polska': 'PL', 'deutschland': 'DE', 'czech republic': 'CZ',
}

# Words that name a kind of division rather than the division itself, so
# "Cook County", "Cook Co." and "Cook" read alike, and so do "powiat
# białostocki" and GeoNames' "Powiat białostocki". Only the first and last
# word of a part are dropped, and never the part's only word.
_ADMIN_WORDS = frozenset({
    'county', 'co', 'parish', 'parafia', 'powiat', 'gmina', 'wojewodztwo',
    'voivodeship', 'province', 'department', 'departement', 'state',
})

# Letters that Unicode decomposition leaves alone but a person typing on an
# English keyboard writes plainly ("Lodz" for "Łódź").
_FOLD_LETTERS = str.maketrans({
    'ł': 'l', 'ø': 'o', 'đ': 'd', 'æ': 'ae', 'œ': 'oe', 'ı': 'i', 'þ': 'th',
})

# GeoNames feature codes for a neighbourhood, an abandoned, destroyed or
# historical settlement. When a living town of the same name also fits, these
# step aside so "Chicago" is the city, not a ghost town or a district of it.
_NOT_CURRENT_CODES = frozenset({'PPLX', 'PPLQ', 'PPLW', 'PPLH', 'PPLCH'})

# Hierarchy ranks, smallest place first: the place itself, then its
# commune/township, county, state, country.
_RANK_SELF, _RANK_ADMIN3, _RANK_ADMIN2, _RANK_ADMIN1, _RANK_COUNTRY = 4, 3, 2, 1, 0
_MAX_CANDIDATES_SHOWN = 3


def _fold(text: str | None) -> list[str]:
    """A name's words with case and accents folded and punctuation dropped:
    "Wola Mała" and "WOLA MALA" fold alike."""
    s = unicodedata.normalize('NFKD', str(text or '').casefold().translate(_FOLD_LETTERS))
    s = ''.join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r'[\W_]+', ' ', s).split()


def _geo_key(text: str | None) -> str:
    """The comparison form of one place name: case and accents folded,
    punctuation turned to spaces, and a leading or trailing division word
    ("County", "Co.", "powiat") dropped.

    Every name on both sides (the registry's and GeoNames') goes through
    this one function, so "Białystok", "Bialystok" and "BIALYSTOK" are the
    same key, as are "Cook County", "Cook Co." and "Cook"."""
    tokens = _fold(text)
    while len(tokens) > 1 and tokens[0] in _ADMIN_WORDS:
        tokens.pop(0)
    while len(tokens) > 1 and tokens[-1] in _ADMIN_WORDS:
        tokens.pop()
    return ' '.join(tokens)


def _hierarchy_parts(hierarchy: str | None) -> list[str]:
    """A hierarchy string split on its commas, blanks dropped."""
    return [p.strip() for p in str(hierarchy or '').split(',') if p.strip()]


@dataclass(frozen=True)
class GeoRow:
    """One GeoNames feature: a town or village (class P) or an administrative
    division (class A: a state, a county, a commune, a country). `admin1`,
    `admin2`, `admin3` are GeoNames' codes for the divisions it sits in."""
    gid: int
    name: str
    asciiname: str
    lat: float
    lon: float
    fclass: str
    fcode: str
    country: str
    admin1: str
    admin2: str
    admin3: str
    population: int


def _feature_rank(geo: GeoRow) -> int:
    """Where a feature sits in a hierarchy: a country, a state, a county, a
    commune, or (for a town and anything else) the place itself. A division
    is never its own context: "New York" the state does not count the state
    of New York among the divisions it sits in."""
    code = geo.fcode or ''
    if geo.fclass == 'A':
        if code.startswith('PCL'):
            return _RANK_COUNTRY
        if code.startswith('ADM1'):
            return _RANK_ADMIN1
        if code.startswith('ADM2'):
            return _RANK_ADMIN2
        if code.startswith('ADM3'):
            return _RANK_ADMIN3
    return _RANK_SELF


def _fetch_url(url: str, dest: Path) -> str | None:
    """Download `url` to `dest` (via a `.part` file renamed into place, so an
    interrupted download never leaves a half file that looks complete).

    The only network access in the tools. Returns None on success or a plain
    reason on any failure; no exception escapes, so a missing connection
    reads as one sentence, not a traceback."""
    part = dest.with_name(dest.name + '.part')
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT_S) as resp, \
                open(part, 'wb') as out:
            shutil.copyfileobj(resp, out, 1024 * 1024)
        os.replace(part, dest)
    except Exception as e:  # urllib, ssl, socket and disk errors are many and platform-varied
        try:
            part.unlink()
        except OSError:
            pass
        return str(e) or e.__class__.__name__
    return None


def _read_country_info(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """countryInfo.txt -> ({name key: country code}, {code: country name}).

    The name map also carries `_COUNTRY_EXTRA_NAMES`. Comment lines start
    with '#'; a short or malformed line is skipped."""
    names: dict[str, str] = {}
    labels: dict[str, str] = {}
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            if line.startswith('#'):
                continue
            cols = line.rstrip('\r\n').split('\t')
            if len(cols) < 5 or len(cols[0]) != 2 or not cols[4].strip():
                continue
            cc, label = cols[0].upper(), cols[4].strip()
            labels[cc] = label
            key = _geo_key(label)
            if key:
                names.setdefault(key, cc)
    for raw, cc in _COUNTRY_EXTRA_NAMES.items():
        names.setdefault(_geo_key(raw), cc)
    return names, labels


_GAZETTEER_DDL = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS loaded(cc TEXT PRIMARY KEY, features INTEGER);
CREATE TABLE IF NOT EXISTS features(
    gid INTEGER PRIMARY KEY, cc TEXT, name TEXT, asciiname TEXT, lat REAL, lon REAL,
    fclass TEXT, fcode TEXT, a1 TEXT, a2 TEXT, a3 TEXT, population INTEGER);
CREATE TABLE IF NOT EXISTS names(key TEXT, cc TEXT, gid INTEGER);
CREATE INDEX IF NOT EXISTS names_key ON names(key, cc);
CREATE TABLE IF NOT EXISTS admin_keys(cc TEXT, level INTEGER, code TEXT, key TEXT);
CREATE INDEX IF NOT EXISTS admin_keys_code ON admin_keys(cc, level, code);
CREATE INDEX IF NOT EXISTS admin_keys_key ON admin_keys(key, level);
CREATE TABLE IF NOT EXISTS admin_labels(
    cc TEXT, level INTEGER, code TEXT, label TEXT, PRIMARY KEY(cc, level, code));
"""


def _open_gazetteer_db(path: Path) -> sqlite3.Connection:
    """Open (or create) the gazetteer cache; a cache from another schema
    version, or one SQLite cannot read, is deleted and started fresh, since
    it is rebuilt from the dumps on disk."""
    for _attempt in range(2):
        conn = None
        try:
            conn = sqlite3.connect(str(path))
            # A disposable cache rebuilt from the dumps: speed over durability.
            conn.execute('PRAGMA synchronous = OFF')
            conn.executescript(_GAZETTEER_DDL)
            row = conn.execute("SELECT v FROM meta WHERE k = 'schema'").fetchone()
            if row is None:
                conn.execute("INSERT INTO meta(k, v) VALUES ('schema', ?)", (_GAZETTEER_SCHEMA,))
                conn.commit()
                return conn
            if row[0] == _GAZETTEER_SCHEMA:
                return conn
        except sqlite3.DatabaseError:
            pass
        if conn is not None:
            conn.close()
        path.unlink(missing_ok=True)
    raise sqlite3.DatabaseError(f'cannot create {path}')


def _import_admin_codes(conn: sqlite3.Connection, gdir: Path) -> None:
    """Load admin1CodesASCII.txt and admin2Codes.txt (every country's states
    and counties) into the cache, once. Their names are the labels a filled-in
    hierarchy uses ("Illinois", "Cook County")."""
    if conn.execute("SELECT 1 FROM meta WHERE k = 'admin_codes'").fetchone():
        return
    for level, fname in ((1, 'admin1CodesASCII.txt'), (2, 'admin2Codes.txt')):
        with open(gdir / fname, encoding='utf-8') as fh:
            for line in fh:
                cols = line.rstrip('\r\n').split('\t')
                if len(cols) < 3 or '.' not in cols[0]:
                    continue
                cc, _, code = cols[0].partition('.')
                cc = cc.upper()
                conn.execute('INSERT OR REPLACE INTO admin_labels VALUES (?,?,?,?)',
                             (cc, level, code, cols[1]))
                for key in {_geo_key(cols[1]), _geo_key(cols[2])} - {''}:
                    conn.execute('INSERT INTO admin_keys VALUES (?,?,?,?)', (cc, level, code, key))
    conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES ('admin_codes', '1')")
    conn.commit()


def _import_country(conn: sqlite3.Connection, zip_path: Path, cc: str) -> str | None:
    """Read one country's dump ({CC}.zip holding {CC}.txt) into the cache.

    Streams the text out of the zip and keeps only towns/villages (class P)
    and divisions (class A): for the United States that is about 250,000 of
    2.2 million lines, read in seconds without holding the file in memory.
    A division also files its names under its code, so a town's county and
    state can be matched by any spelling GeoNames knows. One transaction: a
    failed import leaves the country unloaded, never half loaded. Returns
    None or a plain reason."""
    member = f'{cc}.txt'
    count = 0
    try:
        with zipfile.ZipFile(zip_path) as zf, zf.open(member) as raw:
            conn.execute('DELETE FROM names WHERE cc = ?', (cc,))
            conn.execute('DELETE FROM admin_keys WHERE cc = ? AND level = 3', (cc,))
            conn.execute('DELETE FROM features WHERE cc = ?', (cc,))
            for line in io.TextIOWrapper(raw, encoding='utf-8'):
                cols = line.rstrip('\r\n').split('\t')
                if len(cols) < 15 or cols[6] not in _KEPT_FEATURE_CLASSES:
                    continue
                try:
                    gid, lat, lon = int(cols[0]), float(cols[4]), float(cols[5])
                except ValueError:
                    continue
                try:
                    population = int(cols[14] or 0)
                except ValueError:
                    population = 0
                fcode = cols[7]
                a1, a2, a3 = cols[10], cols[11], cols[12]
                conn.execute('INSERT OR REPLACE INTO features VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                             (gid, cc, cols[1], cols[2], lat, lon, cols[6], fcode,
                              a1, a2, a3, population))
                keys = {_geo_key(cols[1]), _geo_key(cols[2])}
                keys.update(_geo_key(a) for a in cols[3].split(','))
                keys.discard('')
                conn.executemany('INSERT INTO names VALUES (?,?,?)',
                                 [(k, cc, gid) for k in keys])
                level = {'ADM1': 1, 'ADM2': 2, 'ADM3': 3}.get(fcode)
                if level:
                    code = '.'.join((a1, a2, a3)[:level])
                    conn.execute('INSERT OR IGNORE INTO admin_labels VALUES (?,?,?,?)',
                                 (cc, level, code, cols[1]))
                    conn.executemany('INSERT INTO admin_keys VALUES (?,?,?,?)',
                                     [(cc, level, code, k) for k in keys])
                count += 1
            conn.execute('INSERT OR REPLACE INTO loaded VALUES (?,?)', (cc, count))
            conn.commit()
    except (OSError, KeyError, zipfile.BadZipFile, UnicodeDecodeError, sqlite3.DatabaseError) as e:
        conn.rollback()
        return f'{zip_path.name} could not be read ({e})'
    return None


class _Gazetteer:
    """The cached GeoNames data one geocode run matches against.

    `country_names` maps a country-name key to its code (from countryInfo.txt
    plus `_COUNTRY_EXTRA_NAMES`); `country_labels` maps a code to the name a
    filled-in hierarchy writes, preferring the spelling the registry already
    uses most for that country ("USA") over GeoNames' ("United States")."""

    def __init__(self, conn: sqlite3.Connection, country_names: dict[str, str],
                 country_labels: dict[str, str]):
        self.conn = conn
        self.country_names = country_names
        self.country_labels = dict(country_labels)
        self.loaded = {r[0] for r in conn.execute('SELECT cc FROM loaded')}
        self._ctx_cache: dict[tuple[str, int, str], set[str]] = {}

    def country_keys(self, cc: str) -> set[str]:
        return {k for k, c in self.country_names.items() if c == cc}

    def admin1_countries(self, key: str) -> set[str]:
        """Countries with a state/province spelled `key`, from the all-country
        code file and from every dump already loaded."""
        return {r[0] for r in self.conn.execute(
            'SELECT DISTINCT cc FROM admin_keys WHERE key = ? AND level = 1', (key,))}

    def features_named(self, key: str, countries: list[str]) -> list[GeoRow]:
        if not countries:
            return []
        marks = ','.join('?' * len(countries))
        rows = self.conn.execute(
            'SELECT DISTINCT f.* FROM names n JOIN features f ON f.gid = n.gid '
            f'WHERE n.key = ? AND n.cc IN ({marks})', [key, *countries]).fetchall()
        return [_georow(r) for r in rows]

    def admin_keys(self, cc: str, level: int, code: str) -> set[str]:
        ck = (cc, level, code)
        if ck not in self._ctx_cache:
            self._ctx_cache[ck] = {r[0] for r in self.conn.execute(
                'SELECT key FROM admin_keys WHERE cc = ? AND level = ? AND code = ?', ck)}
        return self._ctx_cache[ck]

    def admin_label(self, cc: str, level: int, code: str) -> str | None:
        row = self.conn.execute(
            'SELECT label FROM admin_labels WHERE cc = ? AND level = ? AND code = ?',
            (cc, level, code)).fetchone()
        return row[0] if row else None

    def context(self, geo: GeoRow) -> dict[str, set[int]]:
        """Every spelling of the divisions `geo` sits in, each mapped to the
        ranks it names: its commune, county, state and country. A division
        lists only the ones above it."""
        own = _feature_rank(geo)
        out: dict[str, set[int]] = {}
        codes = (geo.admin1, geo.admin2, geo.admin3)
        for level, rank in ((1, _RANK_ADMIN1), (2, _RANK_ADMIN2), (3, _RANK_ADMIN3)):
            if rank >= own or not all(c and c != '00' for c in codes[:level]):
                continue
            for key in self.admin_keys(geo.country, level, '.'.join(codes[:level])):
                out.setdefault(key, set()).add(rank)
        if own > _RANK_COUNTRY:
            for key in self.country_keys(geo.country):
                out.setdefault(key, set()).add(_RANK_COUNTRY)
        return out

    def describe(self, geo: GeoRow) -> str:
        """"Name, county, state, country" for a candidate line."""
        bits = [geo.name]
        own = _feature_rank(geo)
        for level, rank in ((2, _RANK_ADMIN2), (1, _RANK_ADMIN1)):
            codes = (geo.admin1, geo.admin2)[:level]
            if rank < own and all(codes):
                label = self.admin_label(geo.country, level, '.'.join(codes))
                if label and label not in bits:
                    bits.append(label)
        country = self.country_labels.get(geo.country, geo.country)
        if own > _RANK_COUNTRY and country not in bits:
            bits.append(country)
        return ', '.join(bits)


def _georow(row) -> GeoRow:
    return GeoRow(gid=row[0], country=row[1], name=row[2], asciiname=row[3],
                  lat=row[4], lon=row[5], fclass=row[6], fcode=row[7],
                  admin1=row[8] or '', admin2=row[9] or '', admin3=row[10] or '',
                  population=row[11] or 0)


@dataclass
class _PlaceMatch:
    """What the gazetteer says about one registry place.

    status: 'hit' (one feature fits every part of the hierarchy),
    'ambiguous' (several fit), 'partial' (the name is known but some part of
    the hierarchy fits none of them), 'none' (no feature has the name),
    'no-data' (the countries it names could not be loaded).
    `countries` are the countries searched; `unmatched` the hierarchy parts
    the best candidates did not fit."""
    status: str
    hit: GeoRow | None = None
    candidates: list[GeoRow] = None
    unmatched: list[str] = None
    part_ranks: list[set[int]] = None
    countries: list[str] = None


def _place_name_and_context(name: str, hierarchy: str | None) -> tuple[str, list[str], list[str]]:
    """(lookup name, hierarchy parts, the parts above the place).

    A name written with commas ("Weston, Missouri") is looked up by its first
    part. The hierarchy normally starts with the place itself ("Chicago, Cook
    County, Illinois, USA"); that first part is not context. A hierarchy that
    lists only the places above ("Cook County, Illinois") is context whole."""
    lookup = (str(name or '').split(',')[0]).strip() or str(name or '').strip()
    parts = _hierarchy_parts(hierarchy)
    if parts and _geo_key(parts[0]) == _geo_key(lookup):
        return lookup, parts, parts[1:]
    return lookup, parts, parts


def _place_countries(name_key: str, context: list[str], gaz: _Gazetteer) -> set[str]:
    """The countries a place's hierarchy points at, loaded or not.

    Read from the hierarchy's last part: a country name ("Poland", "USA",
    "England"), or a state or province only one country has ("Illinois" is
    the US; "podlaskie" is Poland once Poland is loaded). Both readings
    count, so "Atlanta, Georgia" searches the country Georgia and the US
    state alike, and the hierarchy check keeps the one that fits. A place
    that is itself a country ("Poland" with no parents) names its own."""
    found: set[str] = set()
    if context:
        last = _geo_key(context[-1])
        cc = gaz.country_names.get(last)
        if cc:
            found.add(cc)
        owners = gaz.admin1_countries(last)
        if len(owners) == 1:
            found |= owners
    elif name_key in gaz.country_names:
        found.add(gaz.country_names[name_key])
    return found


def _says_division(name: str) -> bool:
    """Whether a name itself says it is a division: "Cook County", "Gmina
    Tykocin", "Powiat białostocki"."""
    return any(t in _ADMIN_WORDS for t in _fold(name))


def _skipped_levels(geo: GeoRow, part_ranks: list[set[int]]) -> int:
    """How many of the divisions `geo` sits in (commune, county, state) the
    hierarchy leaves out. "Illinois, USA" leaves out nothing for the state
    of Illinois but two levels for a town that happens to be called
    Illinois, so the state is the closer reading."""
    present = set().union(*part_ranks) if part_ranks else set()
    own = _feature_rank(geo)
    codes = (geo.admin1, geo.admin2, geo.admin3)
    skipped = 0
    for level, rank in ((1, _RANK_ADMIN1), (2, _RANK_ADMIN2), (3, _RANK_ADMIN3)):
        if rank < own and all(c and c != '00' for c in codes[:level]) and rank not in present:
            skipped += 1
    return skipped


def _match_place(name: str, hierarchy: str | None, gaz: _Gazetteer) -> _PlaceMatch:
    """Match one registry place against the gazetteer.

    The place's name is looked up among the towns, villages and divisions of
    the countries its hierarchy names (all loaded countries when it names
    none). A candidate fits when every other part of the hierarchy is the
    name of its commune, county, state or country. Only a single fitting
    candidate is a hit: place identity is a research judgment, so two
    fitting Fairviews stay a question for the human, never a guess.

    When several fit, four tie-breaks, in order, keep the obvious answer
    from reading as ambiguous: a living town outranks a neighbourhood, ghost
    town or historical entry of the same name; an entry whose own name is
    the place's name outranks one that lists it only among its other
    spellings ("Yakima" is Yakima, not the town once called Yakima); a
    town outranks a division named after it ("Ventura" is the city, not
    Ventura County; the commune of Reims is the town) unless the place's
    own name says "County", "Gmina" and the like; and the candidate whose
    divisions the hierarchy names most completely wins ("Mississippi, USA"
    is the state, not a town in some county of another state)."""
    lookup, _parts, context = _place_name_and_context(name, hierarchy)
    name_key = _geo_key(lookup)
    if not name_key:
        return _PlaceMatch('none', candidates=[], unmatched=[], countries=[])
    named = _place_countries(name_key, context, gaz)
    countries = sorted(named & gaz.loaded) if named else sorted(gaz.loaded)
    if not countries:
        return _PlaceMatch('no-data', candidates=[], unmatched=[], countries=sorted(named))

    feats = gaz.features_named(name_key, countries)
    if not context and name_key in gaz.country_names:
        # "France" or "Wales" on its own is the country (or the nation
        # within one), not a village that happens to share the name.
        feats = [g for g in feats if _feature_rank(g) <= _RANK_ADMIN1] or feats
    if not feats:
        return _PlaceMatch('none', candidates=[], unmatched=[], countries=countries)

    scored = []
    for geo in feats:
        ctx = gaz.context(geo)
        ranks = [ctx.get(_geo_key(p), set()) for p in context]
        missing = [p for p, r in zip(context, ranks) if not r]
        scored.append((geo, ranks, missing))

    fitting = [s for s in scored if not s[2]]
    if len(fitting) > 1:
        current = [s for s in fitting if s[0].fcode not in _NOT_CURRENT_CODES
                   and not (s[0].fclass == 'A' and s[0].fcode.endswith('H'))]
        fitting = current or fitting
    if len(fitting) > 1:
        # Whole words compared, division words included: "New York County"
        # is not named "New York".
        folded = _fold(lookup)
        own_name = [s for s in fitting
                    if folded in (_fold(s[0].name), _fold(s[0].asciiname))]
        fitting = own_name or fitting
    if len(fitting) > 1:
        towns = [s for s in fitting if s[0].fclass == 'P']
        divisions = [s for s in fitting if s[0].fclass == 'A']
        if _says_division(lookup) and divisions:
            fitting = divisions
        elif towns:
            fitting = towns + [s for s in divisions if _feature_rank(s[0]) < _RANK_ADMIN3
                               and not _says_division(s[0].name)]
    if len(fitting) > 1 and context:
        fewest = min(_skipped_levels(s[0], s[1]) for s in fitting)
        fitting = [s for s in fitting if _skipped_levels(s[0], s[1]) == fewest]
    if len(fitting) == 1:
        geo, ranks, _ = fitting[0]
        return _PlaceMatch('hit', hit=geo, candidates=[geo], unmatched=[],
                           part_ranks=ranks, countries=countries)

    def _rank_key(s):
        return (len(s[2]), -s[0].population)

    if fitting:
        best = sorted(fitting, key=_rank_key)[:_MAX_CANDIDATES_SHOWN]
        return _PlaceMatch('ambiguous', candidates=[s[0] for s in best], unmatched=[],
                           countries=countries)
    best = sorted(scored, key=_rank_key)[:_MAX_CANDIDATES_SHOWN]
    return _PlaceMatch('partial', candidates=[s[0] for s in best],
                       unmatched=best[0][2], countries=countries)


def _filled_hierarchy(name: str, hierarchy: str | None, match: _PlaceMatch,
                      gaz: _Gazetteer) -> str | None:
    """The hierarchy with the county, state and country GeoNames gives added
    where it has none, or None when nothing is missing.

    Existing parts are kept word for word and in order; a missing level is
    slotted in where it belongs ("Chicago, Illinois" becomes "Chicago, Cook
    County, Illinois, USA"). A part that could be two levels ("New York" is
    both a county and a state of New York City) counts as both, so nothing
    that might repeat it is added. A hierarchy whose parts run out of order
    is left alone."""
    geo = match.hit
    lookup, parts, context = _place_name_and_context(name, hierarchy)
    if not parts:
        items = [(_RANK_SELF, lookup)]
    elif context is not parts:
        items = [(_RANK_SELF, parts[0])]
    else:
        items = []
    present: set[int] = set()
    prev = _RANK_SELF
    for text, ranks in zip(context, match.part_ranks or []):
        lower = [r for r in ranks if r < prev]
        if not lower:
            return None
        prev = max(lower)
        present |= ranks
        items.append((prev, text))

    own = _feature_rank(geo)
    added = False
    for level, rank in ((2, _RANK_ADMIN2), (1, _RANK_ADMIN1)):
        codes = (geo.admin1, geo.admin2)[:level]
        if rank >= own or rank in present or not all(c and c != '00' for c in codes):
            continue
        label = gaz.admin_label(geo.country, level, '.'.join(codes))
        if label:
            items.append((rank, label))
            added = True
    if own > _RANK_COUNTRY and _RANK_COUNTRY not in present:
        label = gaz.country_labels.get(geo.country)
        if label:
            items.append((_RANK_COUNTRY, label))
            added = True
    if not added:
        return None
    items.sort(key=lambda it: -it[0])   # stable: existing parts keep their order
    return ', '.join(text for _, text in items)


def _proposed_alt_names(geo: GeoRow, current_name: str, existing_alts: set[str],
                        taken: set[str] | None = None) -> list[str]:
    """Alt-name spellings worth adding: the gazetteer's name/asciiname when they
    differ from what the registry already records (case-folded comparison).

    `taken` holds the names and alt names of every OTHER place: a spelling
    another place already answers to is not added, since two places sharing
    a name is exactly what `fha places lint` warns about (PL002) and what
    keeps a claim's place text from linking."""
    have = {normalize_place_text(current_name)} | {normalize_place_text(a) for a in existing_alts}
    out: list[str] = []
    for cand in (geo.name, geo.asciiname):
        key = normalize_place_text(cand)
        if key and key not in have and key not in (taken or set()):
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
    hierarchy: str | None = None,
) -> tuple[str, bool]:
    """Surgically set `coords:` (and add `alt_names:` when absent, and set a
    filled-in `hierarchy:` when given) on one place block, preserving every
    other line and comment. Returns (new_text, changed).

    Only the block whose `- id:` matches `place_id` (case-insensitive) is touched.
    An existing `coords:` line in that block is replaced; `alt_names:` is added
    only when the block has none (a human-curated list is never clobbered).
    `hierarchy` is the old hierarchy with missing levels added
    (`_filled_hierarchy`), so replacing the key loses none of its words.

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

    if hierarchy:
        _set_block_key(block, 'hierarchy', indent,
                       [f'{indent}hierarchy: {yaml_inline(hierarchy)}'])

    if alt_names and not has_alt:
        # The block may carry trailing blank line(s) that visually separate it
        # from the next entry; appending after them would detach alt_names from
        # its mapping. Insert before any such trailing blanks instead.
        insert_at = len(block)
        while insert_at > 0 and not block[insert_at - 1].strip():
            insert_at -= 1
        block.insert(insert_at, f'{indent}alt_names: {yaml_inline(list(alt_names))}')

    new_lines = lines[:start] + block + lines[end:]
    trailing_nl = '\n' if text.endswith('\n') else ''
    return '\n'.join(new_lines) + trailing_nl, True


def _hierarchy_is_one_plain_line(text: str, place_id: str) -> bool:
    """Whether the place's `hierarchy:` is absent or a single line with no
    `#` comment, so replacing it with a filled-in one loses nothing."""
    lines = text.splitlines()
    span = _locate_place_block(lines, place_id)
    if span is None:
        return False
    block = lines[span[0]:span[1]]
    key = _key_span(block, 'hierarchy', _block_indent(lines, *span))
    if key is None:
        return True
    return key[1] - key[0] == 1 and '#' not in block[key[0]]


def _prepare_gazetteer(
    archive_root: Path, rows: list, *, offline: bool, progress,
) -> tuple[_Gazetteer | None, list[str]]:
    """Make sure the name files and every needed country are in the cache.

    Downloads (unless `offline`) the three name files when missing, then the
    dump of each country the places point at (`_place_countries`) that is not
    loaded yet, importing each into the SQLite cache. A place that points at
    no country searches the countries already loaded; nothing is fetched for
    it. Returns (gazetteer, notes) or (None, notes) when there is nothing to
    match against. A failed download is one plain note, never a traceback,
    and the other countries carry on."""
    gdir = archive_root / '.cache' / 'geonames'
    notes: list[str] = []
    missing = [f for f in _GEONAMES_NAME_FILES if not (gdir / f).is_file()]
    if missing:
        if offline:
            notes.append(
                'No GeoNames place-name data is saved yet, and --offline says not '
                'to fetch it. Run `fha places geocode --all` without --offline once '
                'to download it.')
            return None, notes
        progress('Downloading the GeoNames country, state and county names (one time)...')
        for fname in missing:
            err = _fetch_url(_GEONAMES_BASE_URL + fname, gdir / fname)
            if err:
                notes.append(
                    f'Could not download {fname} from GeoNames ({err}). Check the '
                    'internet connection and run the same command again.')
                return None, notes
    try:
        country_names, country_labels = _read_country_info(gdir / 'countryInfo.txt')
        conn = _open_gazetteer_db(gdir / _GAZETTEER_DB)
        _import_admin_codes(conn, gdir)
    except (OSError, UnicodeDecodeError, sqlite3.DatabaseError) as e:
        notes.append(
            f'The saved GeoNames data in {gdir} could not be read ({e}). Delete that '
            'folder and run the command again to fetch it fresh.')
        return None, notes
    gaz = _Gazetteer(conn, country_names, country_labels)

    wanted: set[str] = set()
    for r in rows:
        lookup, _parts, context = _place_name_and_context(r['name'], r['hierarchy'])
        wanted |= _place_countries(_geo_key(lookup), context, gaz)
    for cc in sorted(wanted - gaz.loaded):
        label = country_labels.get(cc, cc)
        zip_path = gdir / f'{cc}.zip'
        if not zip_path.is_file():
            if offline:
                notes.append(f'No GeoNames data saved for {label}; --offline skips '
                             'fetching it, so its places are not matched.')
                continue
            progress(f'Downloading the GeoNames places of {label} (one time; a large '
                     'country takes a few minutes)...')
            err = _fetch_url(f'{_GEONAMES_BASE_URL}{cc}.zip', zip_path)
            if err:
                notes.append(f'Could not download the GeoNames places of {label} ({err}); '
                             'its places are not matched this time. Run the command '
                             'again to retry.')
                continue
        progress(f'Reading the GeoNames places of {label}...')
        err = _import_country(conn, zip_path, cc)
        if err:
            notes.append(f'{err}. Delete it and run the command again to fetch it fresh.')
            continue
        gaz.loaded.add(cc)
    if not gaz.loaded:
        notes.append('None of the countries these places name could be loaded from '
                     'GeoNames, so nothing was matched.')
        conn.close()
        return None, notes
    return gaz, notes


def _registry_country_spellings(rows: list, gaz: _Gazetteer) -> None:
    """Prefer the registry's own way of writing a country in filled-in
    hierarchies: if most places end "USA", an added country reads "USA", not
    GeoNames' "United States". A spelling that is also one of the country's
    states ("England" for the United Kingdom) is not a name for the whole
    country, so it never stands in for one."""
    counts: dict[str, dict[str, int]] = {}
    for r in rows:
        parts = _hierarchy_parts(r['hierarchy'])
        if len(parts) < 2:
            continue
        key = _geo_key(parts[-1])
        cc = gaz.country_names.get(key)
        if cc and cc not in gaz.admin1_countries(key):
            spelled = counts.setdefault(cc, {})
            spelled[parts[-1]] = spelled.get(parts[-1], 0) + 1
    for cc, spelled in counts.items():
        gaz.country_labels[cc] = max(sorted(spelled), key=lambda s: spelled[s])


def _fmt_coord(value: float) -> str:
    return f'{value:.5f}'.rstrip('0').rstrip('.')


def _proposal_table(proposals: list[dict]) -> list[str]:
    """The one table the human reviews: place, coordinates, and what else is
    added (hierarchy, also-known-as names), one line per place."""
    width = min(max([len(p['name']) for p in proposals] + [5]), 30)
    lines = [f'  {"Place":<13} {"Name":<{width}}  {"Coordinates":<22} Also adds']
    for p in proposals:
        adds = []
        if p['hierarchy']:
            adds.append(f'hierarchy "{p["hierarchy"]}"')
        if p['alt_names']:
            adds.append('also known as ' + ', '.join(p['alt_names']))
        if p['replaces']:
            adds.append(f'(replaces {p["replaces"]})')
        name = p['name'] if len(p['name']) <= width else p['name'][:width - 1] + '…'
        lines.append(f'  {p["place_id"]:<13} {name:<{width}}  {p["coords"]:<22} '
                     + ('; '.join(adds) or '-'))
    return lines


def run_geocode(
    archive_root: Path,
    fha_config: dict,
    *,
    place_id: str | None = None,
    offline: bool = False,
    dry_run: bool = False,
    confirm=None,
    progress=None,
) -> Result:
    """
    Fill in coordinates (and missing county/state/country, and GeoNames'
    spellings as also-known-as names) for registry places, from the saved
    GeoNames data, after ONE yes from the human for the whole batch.

    Every place without coordinates (or the one `place_id`) is matched
    (`_match_place`). The hits become one table, shown once with one
    question; a yes writes them all in one atomic rewrite of places.yaml, so
    the registry holds either every change or none. The AGENTS.md rule that
    coordinates are never written without the human's confirmation is that
    one question. Places with several fitting candidates are listed after,
    each candidate with the exact `fha places set --coords` command that
    settles it; places with no fit say why.

    `confirm` is a callable `(prompt: str) -> bool` (an interactive `[y/N]`
    reader by default; tests inject one). `progress` is a callable taking one
    line, for download progress while the run is under way (the CLI prints
    it to stderr). Network access happens only here, only for missing data,
    and never with `offline`.

    Returns a `Result` whose `data` is {'status': 'ok'|'failed'|'no-gazetteer'|
    'not-found', 'written': int, 'messages': [str, ...], 'proposals': [...],
    'ambiguous': [...], 'unmatched': [...]}, with `changed` listing
    `places.yaml` when it is written. `written` counts places written (or, in
    a dry run, that would be).
    """
    _exit_for = {
        'failed': EXIT_FAILURE, 'not-found': EXIT_WARNINGS,
        'no-gazetteer': EXIT_WARNINGS, 'ok': EXIT_CLEAN,
    }
    report: dict = {'proposals': [], 'ambiguous': [], 'unmatched': []}

    def _geo_result(status: str, written: int, msgs: list[str],
                    changed: list[str] | None = None) -> Result:
        return Result(
            ok=(status == 'ok'),
            exit_code=_exit_for.get(status, EXIT_FAILURE),
            data={'status': status, 'written': written, 'messages': msgs, **report},
            changed=changed or [],
        )

    messages: list[str] = []
    if confirm is None:
        confirm = _interactive_confirm
    if progress is None:
        def progress(_line: str) -> None:
            return None

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
                    [f'{fmt_id_display(pid)} is not in the place registry. Check the id '
                     f'with `fha find {fmt_id_display(pid)}`.'])
        else:
            rows = conn.execute(
                'SELECT id, name, hierarchy, lat, lon FROM places '
                'WHERE lat IS NULL OR lon IS NULL ORDER BY id'
            ).fetchall()
        all_rows = conn.execute('SELECT id, name, hierarchy FROM places').fetchall()

        alts_by_place: dict[str, set[str]] = {}
        # Every name and alt name in the registry -> the places answering to it.
        names_in_use: dict[str, set[str]] = {}
        for r in all_rows:
            names_in_use.setdefault(normalize_place_text(r['name']), set()).add(r['id'])
        for r in conn.execute('SELECT place_id, alt_name FROM place_names'):
            alts_by_place.setdefault(r['place_id'], set()).add(r['alt_name'])
            names_in_use.setdefault(normalize_place_text(r['alt_name']), set()).add(r['place_id'])
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

    gaz, notes = _prepare_gazetteer(archive_root, rows, offline=offline, progress=progress)
    messages.extend(notes)
    if gaz is None:
        return _geo_result('no-gazetteer', 0, messages)

    try:
        _registry_country_spellings(all_rows, gaz)
        for r in rows:
            pid_disp = fmt_id_display(r['id'])
            match = _match_place(r['name'], r['hierarchy'], gaz)
            where = r['hierarchy'] or r['name']
            if match.status == 'hit':
                geo = match.hit
                existing_alts = alts_by_place.get(r['id'], set())
                taken = {k for k, ids in names_in_use.items() if ids - {r['id']}}
                alt_names = ([] if existing_alts else
                             _proposed_alt_names(geo, r['name'], set(), taken))
                for alt in alt_names:
                    names_in_use.setdefault(normalize_place_text(alt), set()).add(r['id'])
                report['proposals'].append({
                    'place_id': pid_disp, 'name': r['name'],
                    'lat': geo.lat, 'lon': geo.lon,
                    'coords': f'{_fmt_coord(geo.lat)}, {_fmt_coord(geo.lon)}',
                    'hierarchy': _filled_hierarchy(r['name'], r['hierarchy'], match, gaz),
                    # A curated alt_names list is never extended or rewritten.
                    'alt_names': alt_names,
                    'replaces': (f'{_fmt_coord(r["lat"])}, {_fmt_coord(r["lon"])}'
                                 if r['lat'] is not None and r['lon'] is not None else None),
                    'match': gaz.describe(geo),
                })
            elif match.status in ('ambiguous', 'partial'):
                report['ambiguous'].append({
                    'place_id': pid_disp, 'name': r['name'], 'where': where,
                    'unmatched': match.unmatched,
                    'candidates': [{
                        'describe': gaz.describe(g),
                        'coords': f'{_fmt_coord(g.lat)}, {_fmt_coord(g.lon)}',
                        'command': (f'fha places set {pid_disp} --coords '
                                    + shell_quote(f'{_fmt_coord(g.lat)}, {_fmt_coord(g.lon)}')),
                    } for g in match.candidates],
                })
            else:
                searched = ', '.join(gaz.country_labels.get(c, c) for c in match.countries)
                report['unmatched'].append({
                    'place_id': pid_disp, 'name': r['name'], 'where': where,
                    'reason': (f'no GeoNames data loaded for {searched}'
                               if match.status == 'no-data' else
                               f'no place of that name in {searched}' if searched else
                               'no place of that name'),
                })
    finally:
        gaz.conn.close()

    proposals = report['proposals']
    places_yaml = archive_root / 'places' / 'places.yaml'
    changed: list[str] = []
    written = 0
    if proposals:
        # Preflight the whole batch before asking: read the registry once and
        # apply every edit in memory, so the question is only asked about
        # changes that can all land, and the write is one atomic rewrite.
        try:
            text = read_text_exact(places_yaml)
        except OSError as e:
            messages.append(f'ERROR: cannot read {places_yaml}: {e}. Check the file '
                            'exists and is not open elsewhere, then retry.')
            return _geo_result('failed', 0, messages)
        new_text = text
        kept = []
        for p in proposals:
            if p['hierarchy'] and not _hierarchy_is_one_plain_line(new_text, p['place_id']):
                # A hierarchy carrying a hand comment, or written over several
                # lines, is not rewritten: the comment would be lost.
                p['hierarchy'] = None
            new_text, block_changed = _apply_geocode_to_yaml(
                new_text, p['place_id'], p['lat'], p['lon'], p['alt_names'], p['hierarchy'])
            if block_changed:
                kept.append(p)
            else:
                messages.append(f'{p["place_id"]} ({p["name"]}) is in the index but not in '
                                'places.yaml; run `fha index` and try again.')
        report['proposals'] = proposals = kept

    if proposals:
        n = len(proposals)
        noun = 'place' if n == 1 else 'places'
        table = [f'GeoNames found a match for {n} {noun}:', *_proposal_table(proposals)]
        if dry_run:
            messages.extend(table)
            messages.append(f'[dry-run] Would write coordinates for these {n} {noun}. '
                            'Nothing written; run without --dry-run to review and apply.')
            written = n
        elif confirm('\n'.join(table) + f'\nWrite coordinates for these {n} {noun}?'):
            try:
                # The registry for every place in the archive, rewritten whole:
                # atomic, so the file holds either all of these changes or none.
                write_text_exact_atomic(places_yaml, reapply_newline(new_text, text))
            except OSError as e:
                messages.append(f'ERROR: cannot write {places_yaml}: {e}. Nothing was '
                                'changed; check the file is not open elsewhere and retry.')
                return _geo_result('failed', 0, messages)
            written = n
            changed.append(str(places_yaml))
            messages.append(f'Wrote coordinates for {n} {noun} to places/places.yaml.')
        else:
            messages.append('Nothing written.')

    if report['ambiguous']:
        messages.append('')
        messages.append('Not written: more than one GeoNames entry could be these places. '
                        'If you know which, run its command:')
        for a in report['ambiguous']:
            head = f'  {a["place_id"]} {a["name"]} ({a["where"]})'
            if a['unmatched']:
                head += (': no entry fits "' + '", "'.join(a['unmatched'])
                         + '" in its hierarchy')
            messages.append(head)
            for c in a['candidates']:
                messages.append(f'    {c["describe"]} ({c["coords"]})')
                messages.append(f'      {c["command"]}')
    if report['unmatched']:
        messages.append('')
        messages.append('Not found in GeoNames (check the spelling, or add the country '
                        'to the hierarchy):')
        for u in report['unmatched']:
            messages.append(f'  {u["place_id"]} {u["name"]} ({u["where"]}): {u["reason"]}')

    if written and not dry_run:
        messages.append('Next: run `fha index` so maps and place pages show the new '
                        'coordinates.')
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
            # A period with no name is refused, not silently dropped: the
            # workbench prefills an (illegal but hand-authorable) empty
            # hierarchy as exactly 'PERIOD | ', and a silent drop here would
            # delete that entry from the registry on the human's next
            # unrelated history edit.
            if not entry['hierarchy']:
                return (f'history entry "{line}" names a period but no place '
                        'name - write it as "PERIOD | Name, Parent, ..." (the '
                        'name the place bore then), or delete the line to '
                        'drop the entry.')
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
                    f'{place_id!r} is not a valid place ID. l-ids look like '
                    'l-7c1a9f4e22: an l, a dash and 10 characters '
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
        # Atomic, not the plain writer: this one file is the registry for EVERY
        # place in the archive, so a write that dies partway (disk full, the
        # process killed) would truncate every other place's block along with
        # the one being edited - and the refusal below would tell the human
        # nothing had changed. `write_text_exact_atomic` writes a sibling temp
        # and renames it over the target, so the registry holds either the old
        # text or the new one and the message stays true.
        write_text_exact_atomic(path, reapply_newline(new_text, old_text))
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
        # An empty replacement of a key that was never there is already
        # clear: writing 'alt_names: []' onto such a place would mutate the
        # registry on a no-op apply (the workbench modal posts an empty
        # field even untouched, by design - data-wb-allowempty).
        if cleaned or any(re.match(rf'^{re.escape(indent)}alt_names:', ln)
                          for ln in block):
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
        # Same no-op guard as alt_names above: clearing a history that was
        # never recorded writes nothing.
        if history_entries or any(re.match(rf'^{re.escape(indent)}history:', ln)
                                  for ln in block):
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
        entries = safe_load_yaml(registry_text) or []
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

    notes_entries = split_log_entries(old_notes)
    found, reason, message = match_log_entry(
        notes_entries, old_text, noun='note',
        where=f'the notes on {result.data["place_id"]}',
        file_hint='places/places.yaml')
    if found is None:
        result.data['reason'] = reason
        return result_fail(result, 'refused', message)
    notes_entries[found] = '\n'.join(
        ln.rstrip('\r') for ln in text.strip('\n').split('\n'))
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
        entries = safe_load_yaml(registry_text) or []
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

    include_suggested = bool(getattr(args, 'include_suggested', False))
    result = run_candidates(archive_root, fha_config, threshold=threshold,
                             include_suggested=include_suggested)
    if result['status'] == 'failed':
        return EXIT_FAILURE

    place_text_groups = result['place_text_groups']
    if place_text_groups:
        print(f'Found {len(place_text_groups)} candidate place-text cluster(s):')
        for g in place_text_groups:
            spread = format_date_spread(g['date_min'], g['date_max'])
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
        print(f'ERROR: {place_id!r} is not a valid l-id. l-ids look like l-7c1a9f4e22.', file=sys.stderr)
        return EXIT_FAILURE

    result = run_geocode(
        archive_root, fha_config,
        place_id=place_id,
        offline=getattr(args, 'offline', False),
        dry_run=getattr(args, 'dry_run', False),
        progress=lambda line: print(line, file=sys.stderr, flush=True),
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
        # A lone `--aka -` clears the list (the `fha person estimate` clear
        # sentinel), so the workbench's emptied-textarea apply has an exact
        # CLI spelling.
        aka = [str(a).strip() for a in args.aka]
        if aka == ['-']:
            aka = []
    history = getattr(args, 'history', None)
    if history is not None and [str(h).strip() for h in history] == ['-']:
        history = []   # lone `--history -` clears the list, same sentinel
    return _emit_place_result(run_place_set(
        archive_root, args.place_id,
        coords=getattr(args, 'coords', None), aka=aka,
        history=history,
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
  fha places candidates              Place text that recurs often enough for a registry entry
  fha places geocode (--place l-id | --all)  Fill in coordinates from GeoNames (one table, one yes)
  fha places set l-id [--coords "LAT, LON"] [--aka NAME]... [--history "PERIOD | HIERARCHY"]...
  fha places note l-id --text "..."  Append a dated research note to the place
  fha places edit-note l-id --old-text "..." --text "..."  Rewrite one existing note
                                     (--old-text is the note's whole paragraph)

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
    candidates_p.add_argument('--include-suggested', action='store_true', dest='include_suggested',
                               help='Also cluster place_text on status: suggested claims (normally '
                                    'excluded): a one-time scan of a backlog not yet reviewed, e.g. right after '
                                    'a fresh `fha gedcom import`, not the everyday view.')
    candidates_p.set_defaults(func=_cmd_places_candidates)

    geocode_p = deferred.add_parser(
        'geocode', help='Fill in coordinates, missing county/state/country and spellings from GeoNames',
        description=('Match places without coordinates against GeoNames, the free world '
                     'gazetteer: towns, villages, counties, states and countries. The data '
                     'is downloaded once into .cache/geonames/ (only the countries your '
                     'places name). Every match is shown in one table and nothing is '
                     'written until you answer yes. Places that could be more than one '
                     'GeoNames entry are listed with the command that settles each.'))
    geocode_p.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS, help='Archive root (auto-detected if omitted).')
    geocode_p.add_argument('--place', metavar='l-id', help='Geocode only this place.')
    geocode_p.add_argument('--all', action='store_true', help='Geocode every registry place lacking coordinates.')
    geocode_p.add_argument('--offline', action='store_true',
                           help='Never download; match only against GeoNames data already saved.')
    geocode_p.add_argument('--dry-run', action='store_true', dest='dry_run',
                           help='Show the same table without asking or writing.')
    geocode_p.set_defaults(func=_cmd_places_geocode)

    set_p = deferred.add_parser(
        'set', help="Set a place's coordinates, alt-names, or names-over-time (full replace per field)")
    set_p.add_argument('place_id', metavar='l-id', help='The place to update.')
    set_p.add_argument('--coords', metavar='"LAT, LON"',
                       help='New coordinates, latitude first, e.g. "39.8000, -95.6000".')
    set_p.add_argument('--aka', metavar='NAME', action='append',
                       help='One also-known-as name, taken verbatim, commas and all '
                            '("Washington, D.C." is one name); repeat the flag for more. '
                            'The given set replaces the old list; a lone `--aka -` '
                            'clears it.')
    set_p.add_argument('--history', metavar='"PERIOD | HIERARCHY"', action='append',
                       help='One names-over-time entry; repeat the flag for more (replaces '
                            'the old list; a lone `--history -` clears it).')
    set_p.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS,
                       help='Archive root (auto-detected if omitted).')
    set_p.add_argument('--dry-run', action='store_true', dest='dry_run',
                       help='Preview the change without writing.')
    set_p.set_defaults(func=_cmd_places_set)

    note_p = deferred.add_parser('note', help='Append a dated research note to a place')
    note_p.add_argument('place_id', metavar='l-id', help='The place the note is about.')
    note_p.add_argument('--text', metavar='TEXT', required=True,
                        help='The note, in your own words (loose citations welcome).')
    note_p.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS,
                        help='Archive root (auto-detected if omitted).')
    note_p.add_argument('--dry-run', action='store_true', dest='dry_run',
                        help='Preview the change without writing.')
    note_p.set_defaults(func=_cmd_places_note)

    edit_note_p = deferred.add_parser(
        'edit-note',
        help="Rewrite one existing research note on a place (named by its whole current paragraph)",
        description=("Rewrite one existing research note on a place; the other notes are "
                     "untouched. --old-text names the note by its exact current text: give the "
                     "whole paragraph, word for word, not one sentence of it. If it is only part "
                     "of a note, is not found, or appears more than once, nothing is written and "
                     "the message says which."))
    edit_note_p.add_argument('place_id', metavar='l-id', help='The place whose note is being corrected.')
    edit_note_p.add_argument('--old-text', metavar='TEXT', required=True, dest='old_text',
                             help="The note's current text: the whole paragraph, exactly as it stands.")
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
    candidates_p.add_argument('--include-suggested', action='store_true', dest='include_suggested')
    candidates_p.set_defaults(func=_cmd_places_candidates)

    geocode_p = subs.add_parser('geocode')
    geocode_p.add_argument('--root', metavar='PATH')
    geocode_p.add_argument('--place', metavar='l-id')
    geocode_p.add_argument('--all', action='store_true')
    geocode_p.add_argument('--offline', action='store_true')
    geocode_p.add_argument('--dry-run', action='store_true', dest='dry_run')
    geocode_p.set_defaults(func=_cmd_places_geocode)

    set_p = subs.add_parser('set')
    set_p.add_argument('place_id', metavar='l-id')
    set_p.add_argument('--coords', metavar='"LAT, LON"')
    set_p.add_argument('--aka', metavar='NAME', action='append')
    set_p.add_argument('--history', metavar='"PERIOD | HIERARCHY"', action='append')
    set_p.add_argument('--root', metavar='PATH')
    set_p.add_argument('--dry-run', action='store_true', dest='dry_run')
    set_p.set_defaults(func=_cmd_places_set)

    note_p = subs.add_parser('note')
    note_p.add_argument('place_id', metavar='l-id')
    note_p.add_argument('--text', metavar='TEXT', required=True)
    note_p.add_argument('--root', metavar='PATH')
    note_p.add_argument('--dry-run', action='store_true', dest='dry_run')
    note_p.set_defaults(func=_cmd_places_note)

    edit_note_p = subs.add_parser('edit-note')
    edit_note_p.add_argument('place_id', metavar='l-id')
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
