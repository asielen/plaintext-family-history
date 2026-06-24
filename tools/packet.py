#!/usr/bin/env python3
"""
packet.py — fha packet: build a person data-export packet.

  fha packet <P-id> [-o out/] [--include-research] [--include-restricted]
                     [--include-dna] [--no-photos] [--dry-run] [--overwrite]
                     [--root PATH]

ARCHITECTURE OVERVIEW
----------------------
A packet is a private, family-facing export of everything the archive knows
about one curated person: their profile, a freshly generated timeline,
every source that cites them, those sources' asset files, and (unless
suppressed) every photo of them the photo index can find. It is gathered as
**copies** into a working directory, then zipped (TOOLING §8). The archive
itself is never touched.

This is explicitly NOT the public/standalone export path (`fha site
--standalone`, TOOLING §12) — the packet's README says so. A packet may
include `living: false` people's full prose and cite other people who are
still living, with a caution in the README. The packet subject is different:
SPEC §21 binds person packets to their own subject rule (separate from the
public-output redaction rules `fha site` follows), so `living: true` and
`living: unknown` subjects are refused unless a future SPEC/TOOLING change
adds an explicit packet opt-in.

PRIVACY RULES (TOOLING §8 — apply at gather time, not as a post-filter):
  - `living: unknown` is treated the same as `living: true`.
  - The packet subject must be `living: false`; packets for living/unknown
    subjects are refused before any output directory is created.
  - `restricted: true` sources are excluded by default; `--include-restricted`
    overrides — EXCEPT `source_type == 'dna'`, which stays excluded even then.
    Only `--include-dna` includes DNA sources, and DNA sources are always
    `restricted: true` (lint E017), so `--include-dna` alone is sufficient.
  - Excluded sources are still named (ID + title only) in the README so the
    human knows material exists but was withheld, not silently dropped.
  - Any *other* person named in the packet's included claims/sources who is
    themselves `living`/`unknown` gets a README caution (their prose/facts
    are still included — packets are private, not for redistribution).

PHOTO GATHERING (TOOLING §8's "all photos of grandma" union):
  (a) photos carrying the bare P-id keyword           — photo_people via='pid-keyword'
  (b) photos whose face-region tags matched exactly    — photo_people via='face-tag'
  (c) photos matched by name/name_variants (unverified) — photo_people via='name-match'
  (d) image files attached to the *included* sources (whether or not the
      photoindex separately resolved them to this person)
  `photo_people` already computes the union of (a)-(c) per photoindex.py's
  `_resolve_photo_people`; this tool only adds (d) and then expands every
  matched path to its full variation group (front+back+crop, etc.) via
  `photo_groups`/`photos.group_id` so a person's photo entry never ships
  the front scan without its back.

WHY A LIBRARY FUNCTION (`run_packet`): mirrors the xref/cooccur/report
convention of a testable `run_*(archive_root, ...) -> dict` core, separate
from the CLI handler that turns the dict into exit codes and stdout text.

CODE MAP
--------
  Helpers
    _today                         — packet directory/README date stamp
    _curated_person                — lookup + curated-tier gate
    _source_ids_for_person        — claim_persons ∪ source_people union (views.py's pattern,
                                     duplicated per-tool per TOOLING §15 "tools never import tools")
    _classify_sources             — split source ids into included/excluded by privacy rules
    _other_named_persons          — living/unknown persons named by included sources, for the
                                     README caution
    _resolve_source_files         — source_files rows → resolved paths + missing/unresolvable notes
    _is_image_path                — extension sniff for photo-type asset files

  Photo gathering
    _photo_people_paths           — photo_people rows for this pid (a/b/c union, already resolved)
    _expand_photo_groups          — path set → full variation-group path set
    _source_image_paths           — image-suffixed files among included sources' assets (d)

  Timeline
    _build_timeline_text          — self-contained fresh timeline.md content, filtered to the
                                     packet's included sources (no GENERATED header — this is an
                                     export copy, not a tracked archive view file)

  Packaging
    _unique_dest_path             — collision-safe copy destination inside a packet subdirectory
    _copy_into                    — copy one file, returning the dest path or None on a missing src
    _write_readme                 — manifest + disclaimer + privacy captions
    _zip_directory                — zip the finished packet directory

  Core / CLI
    _display_path                 — print paths relative to archive when possible
    run_packet                    — library entry point: gather, copy, write, zip
    _cmd_packet, register, _standalone_main
"""

from __future__ import annotations

import argparse
import datetime
import shutil
import sqlite3
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    FhaConfigError,
    Result,
    configure_utf8_stdout,
    fmt_id_display,
    load_fha_yaml,
    normalize_id,
    open_index_db,
    path_to_alias,
    photoindex_status,
    resolve_path,
    resolve_root_arg,
)

configure_utf8_stdout()

_REQUIRED_TABLES = (
    'persons', 'claims', 'sources', 'claim_persons', 'source_files',
    'source_people', 'person_files', 'citations',
)

_IMAGE_SUFFIXES = {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.heic', '.bmp', '.gif'}


def _today() -> str:
    return datetime.date.today().isoformat()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _curated_person(conn: sqlite3.Connection, pid: str) -> sqlite3.Row | None:
    """Return the persons row for pid, or None if absent. Caller checks tier."""
    return conn.execute(
        'SELECT id, name, surname, living, tier, status, merged_into, path FROM persons WHERE id = ?',
        (pid,),
    ).fetchone()


def _resolve_merged_person(
    conn: sqlite3.Connection, person: sqlite3.Row
) -> tuple[sqlite3.Row, list[str]]:
    """
    Follow `merged_into` to the survivor (SPEC §8.8: "tools resolve
    references through merged_into"). A merged tombstone's own `tier`/
    `living` are irrelevant once redirected — the survivor's gate checks
    apply instead. Guards against a corrupt merge cycle by capping the
    chain length rather than looping forever.
    """
    notes: list[str] = []
    seen = {person['id']}
    while person['status'] == 'merged' and person['merged_into']:
        target_id = person['merged_into']
        if target_id in seen:
            notes.append(f'{fmt_id_display(target_id)}: merge chain cycle detected; stopping redirect.')
            return person, notes
        target = _curated_person(conn, target_id)
        if target is None:
            notes.append(
                f'{fmt_id_display(person["id"])} is merged into {fmt_id_display(target_id)}, '
                'which is not in the index.'
            )
            return person, notes
        notes.append(
            f'{fmt_id_display(person["id"])} is merged into {fmt_id_display(target_id)}; '
            'building the packet for the survivor.'
        )
        seen.add(target_id)
        person = target
    return person, notes


def _merged_alias_ids(conn: sqlite3.Connection, survivor_id: str) -> list[str]:
    """
    Every person id whose merged_into chain resolves to survivor_id (SPEC
    §8.8), found by walking merged_into outward from the survivor rather
    than assuming a single hop. Once `_resolve_merged_person` redirects pid
    to the survivor, sources/claims still citing one of these old ids must
    still be gathered, not dropped.
    """
    aliases: set[str] = set()
    frontier = {survivor_id}
    while frontier:
        placeholders = ','.join('?' * len(frontier))
        rows = conn.execute(
            f"SELECT id FROM persons WHERE status = 'merged' AND merged_into IN ({placeholders})",
            list(frontier),
        ).fetchall()
        frontier = {r['id'] for r in rows if r['id'] not in aliases and r['id'] != survivor_id}
        aliases |= frontier
    return sorted(aliases)


def _source_ids_for_person(conn: sqlite3.Connection, pids: list[str]) -> list[str]:
    """
    Distinct source IDs citing any of pids — the same two-table UNION
    views.py uses for sources-index (claim_persons→claims, plus the direct
    source_people table for sources that name someone without yet having
    extracted claims). Duplicated here rather than imported: tools never
    import tools (TOOLING §15).

    pids carries the survivor plus any merged-away aliases (SPEC §8.8) so a
    source that still cites an old id isn't dropped from the packet.
    """
    placeholders = ','.join('?' * len(pids))
    rows = conn.execute(
        f"""
        SELECT DISTINCT c.source_id
        FROM claim_persons cp
        JOIN claims c ON cp.claim_id = c.id
        WHERE cp.person_id IN ({placeholders})
        UNION
        SELECT DISTINCT source_id
        FROM source_people
        WHERE person_id IN ({placeholders})
        """,
        list(pids) + list(pids),
    ).fetchall()
    return [r[0] for r in rows]


def _classify_sources(
    conn: sqlite3.Connection,
    source_ids: list[str],
    *,
    include_restricted: bool,
    include_dna: bool,
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    """
    Split source_ids into (included, excluded) rows per TOOLING §8 privacy rules.

    DNA is checked first and independently of --include-restricted: a DNA
    source is always restricted (lint E017), so checking restricted alone
    would let --include-restricted leak DNA material the human didn't ask for.
    """
    if not source_ids:
        return [], []
    placeholders = ','.join('?' * len(source_ids))
    rows = conn.execute(
        f"""
        SELECT id, title, source_type, restricted, path
        FROM sources WHERE id IN ({placeholders})
        ORDER BY title
        """,
        source_ids,
    ).fetchall()

    included, excluded = [], []
    for row in rows:
        if row['source_type'] == 'dna':
            (included if include_dna else excluded).append(row)
        elif row['restricted']:
            (included if include_restricted else excluded).append(row)
        else:
            included.append(row)
    return included, excluded


def _other_named_persons(
    conn: sqlite3.Connection, included_source_ids: list[str], pid: str
) -> list[sqlite3.Row]:
    """
    Return living/unknown persons (other than pid) named by an included
    source's claims or its source_people list — the README caution set
    (TOOLING §8: "any *other* person ... with living: true is named in a
    README caution"). living: unknown counts as living throughout.
    """
    if not included_source_ids:
        return []
    placeholders = ','.join('?' * len(included_source_ids))
    rows = conn.execute(
        f"""
        SELECT DISTINCT p.id, p.name
        FROM persons p
        WHERE p.id != ? AND p.living IN ('true', 'unknown') AND p.id IN (
            SELECT cp.person_id FROM claim_persons cp
            JOIN claims c ON cp.claim_id = c.id
            WHERE c.source_id IN ({placeholders})
            UNION
            SELECT person_id FROM source_people WHERE source_id IN ({placeholders})
        )
        ORDER BY p.name
        """,
        [pid] + included_source_ids + included_source_ids,
    ).fetchall()
    return rows


def _citation_named_persons(
    conn: sqlite3.Connection, copied_paths: set[str], pid: str
) -> list[sqlite3.Row]:
    """
    Return living/unknown persons (other than pid) named by a bare `[P-id]`
    citation token anywhere in the packet's copied .md files (profile,
    research note, included source records) — catches a living person
    mentioned only in prose, with no `claim_persons`/`source_people` row,
    that `_other_named_persons` would otherwise miss.
    """
    if not copied_paths:
        return []
    placeholders = ','.join('?' * len(copied_paths))
    rows = conn.execute(
        f"""
        SELECT DISTINCT p.id, p.name
        FROM persons p
        WHERE p.id != ? AND p.living IN ('true', 'unknown') AND p.id IN (
            SELECT token FROM citations WHERE kind = 'P' AND path IN ({placeholders})
        )
        ORDER BY p.name
        """,
        [pid] + list(copied_paths),
    ).fetchall()
    return rows


def _resolve_source_files(
    conn: sqlite3.Connection,
    archive_root: Path,
    fha_config: dict,
    source_ids: list[str],
) -> tuple[dict[str, list[Path]], list[str]]:
    """Map source_id -> existing asset Paths, plus missing/unresolvable notes.

    Packet output should be useful even when a fixture or archive points at a
    missing file, but omission must not be silent: the caller writes these
    notes into README.txt and returns a warning exit.
    """
    if not source_ids:
        return {}, []
    placeholders = ','.join('?' * len(source_ids))
    rows = conn.execute(
        f'SELECT source_id, path FROM source_files WHERE source_id IN ({placeholders})',
        source_ids,
    ).fetchall()
    out: dict[str, list[Path]] = {}
    missing: list[str] = []
    for row in rows:
        try:
            resolved = resolve_path(row['path'], fha_config, archive_root)
        except Exception as e:
            missing.append(
                f'{fmt_id_display(row["source_id"])} asset {row["path"]!r} could not be resolved: {e}'
            )
            continue
        if resolved.exists():
            out.setdefault(row['source_id'], []).append(resolved)
        else:
            missing.append(
                f'{fmt_id_display(row["source_id"])} asset missing on disk: {row["path"]}'
            )
    return out, missing


def _is_image_path(p: Path) -> bool:
    return p.suffix.lower() in _IMAGE_SUFFIXES


# ── Photo gathering ───────────────────────────────────────────────────────────

def _photo_people_paths(photos_conn: sqlite3.Connection, pid: str) -> set[str]:
    """
    Raw photo_people paths for pid — already the union of pid-keyword,
    face-tag, and name-match resolution (photoindex.py's _resolve_photo_people
    computes this once per scan; we just read it).
    """
    return {
        row['path']
        for row in photos_conn.execute(
            'SELECT DISTINCT path FROM photo_people WHERE person_ref = ?', (pid,)
        ).fetchall()
    }


def _expand_photo_groups(photos_conn: sqlite3.Connection, paths: set[str]) -> set[str]:
    """
    Expand a set of matched photo paths to every path sharing their
    group_id — so a person tagged on the front of a scan also gets its back
    and crop variants (TOOLING §9: a logical photo is the whole group, not
    one file). Paths with no group_id (shouldn't happen post-scan, but a
    stale/partial cache is possible) pass through unchanged.
    """
    if not paths:
        return set()
    placeholders = ','.join('?' * len(paths))
    group_ids = {
        row['group_id']
        for row in photos_conn.execute(
            f'SELECT DISTINCT group_id FROM photos WHERE path IN ({placeholders}) '
            f'AND group_id IS NOT NULL',
            list(paths),
        ).fetchall()
    }
    expanded = set(paths)
    if group_ids:
        gplaceholders = ','.join('?' * len(group_ids))
        for row in photos_conn.execute(
            f'SELECT path FROM photos WHERE group_id IN ({gplaceholders})', list(group_ids)
        ).fetchall():
            expanded.add(row['path'])
    return expanded


def _source_image_paths(
    source_files_by_id: dict[str, list[Path]],
) -> set[Path]:
    """Image-suffixed asset files among the included sources (gathering rule d)."""
    found: set[Path] = set()
    for paths in source_files_by_id.values():
        for p in paths:
            if _is_image_path(p):
                found.add(p)
    return found


# ── Timeline ──────────────────────────────────────────────────────────────────

def _build_timeline_text(
    conn: sqlite3.Connection, pids: list[str], person_name: str, included_source_ids: set[str]
) -> str:
    """
    Build a fresh timeline.md body for the packet.

    Filtered to `included_source_ids` so a claim sourced from a restricted/DNA
    record that was excluded from the packet doesn't leak its facts into the
    timeline anyway. Intentionally simpler than `fha views timeline`'s decade
    grouping (no GENERATED header, no decade headers) — this is a one-shot
    export artifact, not a tracked, regenerable archive view.

    pids carries the survivor plus any merged-away aliases (SPEC §8.8) so
    claims still attached to an old id still surface here.
    """
    if not included_source_ids:
        rows = []
    else:
        pid_placeholders = ','.join('?' * len(pids))
        src_placeholders = ','.join('?' * len(included_source_ids))
        rows = conn.execute(
            f"""
            SELECT DISTINCT c.date_edtf, c.date_min, c.type, c.value,
                   c.place_text, c.source_id
            FROM claim_persons cp
            JOIN claims c ON cp.claim_id = c.id
            WHERE cp.person_id IN ({pid_placeholders}) AND c.status IN ('accepted', 'needs-review')
              AND c.source_id IN ({src_placeholders})
            ORDER BY
                CASE WHEN c.date_min IS NULL OR c.date_min = '' THEN 1 ELSE 0 END,
                c.date_min ASC
            """,
            list(pids) + list(included_source_ids),
        ).fetchall()

    lines = [f'# Timeline: {person_name}\n']
    if not rows:
        lines.append('\n*(No claims from included sources.)*\n')
        return ''.join(lines)

    for row in rows:
        date_str = row['date_edtf'] or '(undated)'
        line = f'- {date_str} — {row["type"]}: {row["value"]}'
        if row['place_text']:
            line += f' @ {row["place_text"]}'
        line += f' [{fmt_id_display(row["source_id"])}]\n'
        lines.append(line)
    return ''.join(lines)


# ── Packaging ─────────────────────────────────────────────────────────────────

def _unique_dest_path(dest_dir: Path, filename: str) -> Path:
    """Return a collision-free path for filename inside dest_dir.

    Two different sources rarely share a filename, but a stem-clash from
    same-named scans on different machines is possible — append ` (2)`, ` (3)`
    etc. rather than silently overwriting one file with another.
    """
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate
    stem, suffix = Path(filename).stem, Path(filename).suffix
    n = 2
    while True:
        candidate = dest_dir / f'{stem} ({n}){suffix}'
        if not candidate.exists():
            return candidate
        n += 1


def _copy_into(src: Path, dest_dir: Path, *, messages: list[str] | None = None) -> Path | None:
    """
    Copy src into dest_dir, keeping its on-disk filename. None if src is gone
    or the copy itself failed.

    The copy is wrapped in try/except rather than left to propagate: a locked
    file, a permission error, or a full disk on ONE asset must not abort the
    whole packet build and must not exit 0 either — when `messages` is given,
    the failure is appended there so the caller's exit code reflects it
    (AGENTS_TOOLING.md: filesystem errors must affect exit status, never be
    silently swallowed).
    """
    if not src.exists():
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest_path(dest_dir, src.name)
    try:
        shutil.copy2(src, dest)
    except OSError as e:
        if messages is not None:
            messages.append(f'WARNING: could not copy {src}: {e}')
        return None
    return dest


def _write_readme(
    readme_path: Path,
    *,
    person_name: str,
    pid: str,
    included_sources: list[sqlite3.Row],
    excluded_sources: list[sqlite3.Row],
    other_named: list[sqlite3.Row],
    photo_count: int,
    unverified_photo_count: int,
    research_included: bool,
    has_asset_files: bool,
    missing_assets: list[str],
) -> None:
    lines = [
        f'fha packet — {person_name} ({fmt_id_display(pid)})\n',
        f'Generated {_today()}\n',
        '\n'
        'This is a derived export for family/private use — NOT a publication\n'
        'format, and not itself research data. Facts live in the family\n'
        'archive; this packet is a point-in-time copy of what the archive\n'
        'said about this person on the date above. Edits made here are not\n'
        'reflected back into the archive.\n',
    ]

    lines.append('\nContents:\n')
    lines.append('  profile/      person profile' + (' + research notes\n' if research_included else '\n'))
    lines.append('  timeline.md   chronological claims, generated fresh for this export\n')
    if included_sources:
        lines.append('  sources/      every included source record\n')
    if has_asset_files:
        lines.append('  files/        those sources\' asset files\n')
    lines.append(f'  photos/       {photo_count} photo file(s) of {person_name}\n')

    if unverified_photo_count:
        lines.append(
            f'\nNOTE: {unverified_photo_count} photo(s) in photos/ are matched by name only\n'
            'and have not been visually confirmed — treat as unverified.\n'
        )

    if included_sources:
        lines.append(f'\nIncluded sources ({len(included_sources)}):\n')
        for row in included_sources:
            lines.append(f'  [{fmt_id_display(row["id"])}] {row["title"]}\n')

    if excluded_sources:
        lines.append(
            f'\nExcluded sources ({len(excluded_sources)}) — restricted or DNA material '
            'withheld by default, listed by ID only:\n'
        )
        for row in excluded_sources:
            reason = 'DNA' if row['source_type'] == 'dna' else 'restricted'
            lines.append(f'  [{fmt_id_display(row["id"])}] ({reason})\n')

    if missing_assets:
        lines.append('\nMissing files (not copied):\n')
        for item in missing_assets:
            lines.append(f'  - {item}\n')

    if other_named:
        lines.append(
            f'\nCAUTION: this packet\'s materials name {len(other_named)} other living '
            'person(s). Handle accordingly before sharing further:\n'
        )
        for row in other_named:
            lines.append(f'  - {row["name"]} [{fmt_id_display(row["id"])}]\n')

    readme_path.write_text(''.join(lines), encoding='utf-8')


def _zip_directory(src_dir: Path, zip_path: Path) -> None:
    """Zip src_dir's contents into zip_path with paths relative to src_dir's parent
    (so the zip extracts back into a single top-level packet folder)."""
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(src_dir.rglob('*')):
            if p.is_file():
                zf.write(p, p.relative_to(src_dir.parent))


def _display_path(path: Path, archive_root: Path) -> str:
    """Return an archive-relative display path when possible, else absolute."""
    try:
        return str(path.relative_to(archive_root))
    except ValueError:
        return str(path)


# ── Core ──────────────────────────────────────────────────────────────────────

def _packet_payload(
    archive_root: Path,
    pid: str,
    out_dir: Path,
    *,
    include_research: bool = False,
    include_restricted: bool = False,
    include_dna: bool = False,
    no_photos: bool = False,
    dry_run: bool = False,
    overwrite: bool = False,
) -> dict:
    """
    Build a packet for pid under out_dir. Returns a result dict:

      {'status': 'ok'|'dry-run'|'not-found'|'not-curated'|'living-subject'|
       'no-index'|'no-photoindex'|'output-exists'|'write-failed'|'bad-config'|
       'bad-output-path',
       'packet_dir': Path|None, 'zip_path': Path|None,
       'messages': [str, ...]}

    Strict index freshness is required because packet is a derived export
    whose privacy filters come from SQLite. Photoindex absence, unreadability,
    and staleness all block photo-bearing packets per TOOLING §8; --no-photos
    is the explicit escape hatch.

    fha.yaml is also loaded strictly: a malformed config must not be silently
    treated as {}, which would fall back external photos/documents roots to
    directories under the archive root and copy from (or report missing) the
    wrong files.

    out_dir is refused if it falls inside the archive root anywhere other
    than the top-level `out/` directory: a packet's copied .md records
    there would be picked up by a later `fha index` as if they were
    archive truth (TOOLING §15 "tools never import tools" applies just as
    much to one tool's output becoming another's input by accident).
    `out/` itself is exempt because `_index_citations` already skips it by
    the same rule — the two must agree on what's safe.
    """
    messages: list[str] = []
    try:
        resolved_out = out_dir.resolve()
        out_relative = resolved_out.relative_to(archive_root.resolve())
    except ValueError:
        out_relative = None
    if out_relative is not None and out_relative.parts and out_relative.parts[0] != 'out':
        return {
            'status': 'bad-output-path', 'packet_dir': None, 'zip_path': None,
            'messages': [
                f'ERROR: --out {out_dir} is inside {out_relative.parts[0]}/ — '
                'packet output must not be written into a record tree that '
                '`fha index` scans.'
            ],
        }
    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        return {
            'status': 'bad-config', 'packet_dir': None, 'zip_path': None,
            'messages': [f'ERROR: {e}'],
        }

    conn = open_index_db(archive_root, _REQUIRED_TABLES, strict=True)
    if conn is None:
        return {'status': 'no-index', 'packet_dir': None, 'zip_path': None, 'messages': messages}

    try:
        person = _curated_person(conn, pid)
        if person is None:
            return {'status': 'not-found', 'packet_dir': None, 'zip_path': None, 'messages': messages}
        person, merge_notes = _resolve_merged_person(conn, person)
        if merge_notes:
            messages.extend(merge_notes)
        pid = person['id']
        if person['tier'] != 'curated':
            return {'status': 'not-curated', 'packet_dir': None, 'zip_path': None, 'messages': messages}
        if person['living'] in ('true', 'unknown'):
            return {
                'status': 'living-subject', 'packet_dir': None, 'zip_path': None,
                'messages': [
                    f'{fmt_id_display(pid)} has living={person["living"]}; '
                    'packet exports refuse living/unknown subjects by default.'
                ],
            }

        person_name = person['name']
        profile_path = archive_root / person['path']

        photo_status = 'absent'
        if not no_photos:
            photo_status, _lag = photoindex_status(archive_root, fha_config)
            if photo_status in ('absent', 'unreadable', 'old-schema', 'stale'):
                return {
                    'status': 'no-photoindex', 'packet_dir': None, 'zip_path': None,
                    'messages': [
                        f'Photo index is {photo_status} — run `fha photoindex` first, '
                        'or pass --no-photos to export without photos.'
                    ],
                }

        alias_pids = [pid] + _merged_alias_ids(conn, pid)
        source_ids = _source_ids_for_person(conn, alias_pids)
        included_rows, excluded_rows = _classify_sources(
            conn, source_ids, include_restricted=include_restricted, include_dna=include_dna,
        )
        included_ids = {r['id'] for r in included_rows}

        research_row = None
        if include_research:
            research_row = conn.execute(
                "SELECT path FROM person_files WHERE person_id = ? AND kind = 'research'",
                (pid,),
            ).fetchone()

        # Caution list combines structured-data matches now, and gets
        # extended with prose-citation and photo-only matches further below
        # — the dict stays open until just before the README is written so
        # every source can contribute without re-sorting repeatedly.
        copied_md_paths = {person['path']} | {r['path'] for r in included_rows}
        if research_row is not None:
            copied_md_paths.add(research_row['path'])
        other_named_by_id = {r['id']: r for r in _other_named_persons(conn, list(included_ids), pid)}
        for r in _citation_named_persons(conn, copied_md_paths, pid):
            other_named_by_id.setdefault(r['id'], r)

        files_by_source, missing_assets = _resolve_source_files(
            conn, archive_root, fha_config, list(included_ids)
        )
        for item in missing_assets:
            messages.append(f'WARNING: {item}')

        surname = person['surname'] or person_name.split()[-1]
        slug_surname = ''.join(c for c in surname.lower() if c.isalnum()) or 'person'
        packet_name = f'packet_{slug_surname}_{fmt_id_display(pid)}_{_today()}'
        packet_dir = out_dir / packet_name
        zip_path = out_dir / f'{packet_name}.zip'

        if packet_dir.exists() and not overwrite:
            return {
                'status': 'output-exists', 'packet_dir': packet_dir, 'zip_path': zip_path,
                'messages': [
                    f'Output already exists: {packet_dir}. '
                    'Pass --overwrite to replace this disposable packet output.'
                ],
            }
        if zip_path.exists() and not overwrite:
            return {
                'status': 'output-exists', 'packet_dir': packet_dir, 'zip_path': zip_path,
                'messages': [
                    f'Zip already exists: {zip_path}. '
                    'Pass --overwrite to replace this disposable packet output.'
                ],
            }

        if dry_run:
            return {
                'status': 'dry-run', 'packet_dir': packet_dir, 'zip_path': zip_path,
                'messages': messages,
            }

        try:
            if packet_dir.exists():
                shutil.rmtree(packet_dir)
            if zip_path.exists():
                zip_path.unlink()
            packet_dir.mkdir(parents=True)

            # profile/ — the person's curated .md is the packet's central
            # record; a missing/failed copy is a structural failure (not the
            # per-file warning path used for optional assets), so it raises
            # into the cleanup handler below rather than shipping a packet
            # without it.
            profile_dir = packet_dir / 'profile'
            profile_dir.mkdir()
            if not profile_path.exists():
                raise OSError(f'required profile file not found on disk: {profile_path}')
            if _copy_into(profile_path, profile_dir, messages=messages) is None:
                raise OSError(f'could not copy required profile file: {profile_path}')
            research_included = False
            if include_research:
                research_path = archive_root / research_row['path'] if research_row else None
                if research_path is not None and research_path.exists():
                    research_included = _copy_into(research_path, profile_dir, messages=messages) is not None
                elif research_path is not None:
                    messages.append(f'WARNING: research file not found on disk: {research_path}')
                else:
                    messages.append(
                        f'WARNING: --include-research requested but no research file is recorded for {fmt_id_display(pid)}.'
                    )

            # timeline.md
            (packet_dir / 'timeline.md').write_text(
                _build_timeline_text(conn, alias_pids, person_name, included_ids), encoding='utf-8',
            )

            # sources/ + files/
            sources_dir = packet_dir / 'sources'
            files_dir = packet_dir / 'files'
            for row in included_rows:
                src_record = archive_root / row['path']
                if src_record.exists():
                    sources_dir.mkdir(exist_ok=True)
                    _copy_into(src_record, sources_dir, messages=messages)
                else:
                    messages.append(f'WARNING: source record not found on disk: {src_record}')
                for asset_path in files_by_source.get(row['id'], []):
                    files_dir.mkdir(exist_ok=True)
                    _copy_into(asset_path, files_dir, messages=messages)

            # photos/
            photo_count = 0
            unverified_count = 0
            if not no_photos:
                photos_db = archive_root / '.cache' / 'photos.sqlite'
                pconn = sqlite3.connect(str(photos_db))
                pconn.row_factory = sqlite3.Row
                try:
                    people_paths = _photo_people_paths(pconn, pid)
                    unverified_count = len({
                        r['path'] for r in pconn.execute(
                            "SELECT path FROM photo_people WHERE person_ref=? AND via='name-match'",
                            (pid,),
                        ).fetchall()
                    })

                    # Source-linked images aren't under photos/ control by tag, but a
                    # scan/copy of one may still share a photo_groups entry with a
                    # tagged photo (front/back/crop of the same physical item) — convert
                    # each to alias form and union with the tagged paths *before*
                    # expanding through photo_groups, so those siblings are captured too
                    # (TOOLING §9: a logical photo is the whole group, not one file).
                    # path_to_alias falls back to the absolute path's forward-slash form
                    # when the file isn't under the photos root at all; track those
                    # originals so they can still be copied directly.
                    source_alias_map: dict[str, Path] = {}
                    for src_image_path in _source_image_paths(files_by_source):
                        alias = path_to_alias(src_image_path, 'photos', fha_config, archive_root)
                        source_alias_map[alias] = src_image_path

                    combined_paths = set(people_paths) | set(source_alias_map)
                    expanded_aliases = _expand_photo_groups(pconn, combined_paths)

                    def _is_photo_alias(a: str) -> bool:
                        return a == 'photos' or a.startswith('photos/')

                    # photo_people/photos store alias-form paths ('photos/…') that need
                    # resolve_path; a source image outside the photos root falls back to
                    # its own absolute path above and is used as-is. Keep the alias form
                    # alongside the resolved path so a "missing on disk" note can report
                    # it instead of a machine-specific absolute path when the photos
                    # root is mapped outside the archive.
                    photo_targets: dict[Path, str | None] = {}
                    for alias_path in expanded_aliases:
                        if _is_photo_alias(alias_path):
                            try:
                                resolved = resolve_path(alias_path, fha_config, archive_root)
                            except Exception:
                                continue
                            photo_targets[resolved] = alias_path
                        else:
                            photo_targets[source_alias_map.get(alias_path, Path(alias_path))] = None

                    # A photo-group sibling may be tagged with a different,
                    # still-living/unknown person who never appears in any claim or
                    # source — catch that here so the caution list covers photo-only
                    # matches too.
                    tagged_aliases = {a for a in expanded_aliases if _is_photo_alias(a)}
                    if tagged_aliases:
                        placeholders = ','.join('?' * len(tagged_aliases))
                        photo_person_ids = {
                            row['person_ref'] for row in pconn.execute(
                                f"SELECT DISTINCT person_ref FROM photo_people "
                                f"WHERE path IN ({placeholders}) AND person_ref != ?",
                                list(tagged_aliases) + [pid],
                            ).fetchall()
                        }
                        if photo_person_ids:
                            pplaceholders = ','.join('?' * len(photo_person_ids))
                            for row in conn.execute(
                                f"SELECT id, name FROM persons WHERE id IN ({pplaceholders}) "
                                f"AND living IN ('true', 'unknown')",
                                list(photo_person_ids),
                            ).fetchall():
                                other_named_by_id.setdefault(row['id'], row)

                    if photo_targets:
                        photos_dir = packet_dir / 'photos'
                        photos_dir.mkdir(exist_ok=True)
                        for abs_path in sorted(photo_targets, key=str):
                            alias_path = photo_targets[abs_path]
                            if not abs_path.exists():
                                display = alias_path or _display_path(abs_path, archive_root)
                                note = f'photo missing on disk: {display}'
                                messages.append(f'WARNING: {note}')
                                missing_assets.append(note)
                                continue
                            if _copy_into(abs_path, photos_dir, messages=messages):
                                photo_count += 1
                finally:
                    pconn.close()

            # README.txt
            other_named = sorted(other_named_by_id.values(), key=lambda r: r['name'])
            _write_readme(
                packet_dir / 'README.txt',
                person_name=person_name, pid=pid,
                included_sources=included_rows, excluded_sources=excluded_rows,
                other_named=other_named, photo_count=photo_count,
                unverified_photo_count=unverified_count, research_included=research_included,
                has_asset_files=any(files_by_source.values()), missing_assets=missing_assets,
            )

            _zip_directory(packet_dir, zip_path)
        except (OSError, sqlite3.DatabaseError) as e:
            # A structural failure (can't create the packet dir, can't write
            # the zip, disk full mid-build, an incompatible photos.sqlite
            # schema) is different from one missing/locked file: it leaves
            # the build incomplete in a way per-file warnings can't express.
            # Clean up the half-built directory and any partial zip on a
            # best-effort basis (their own failure is swallowed — we're
            # already reporting the primary error) rather than leave debris
            # that would then block a retry with a misleading
            # "output already exists".
            try:
                if packet_dir.exists():
                    shutil.rmtree(packet_dir)
                if zip_path.exists():
                    zip_path.unlink()
            except OSError:
                pass
            messages.append(f'ERROR: packet build failed: {e}')
            return {
                'status': 'write-failed', 'packet_dir': None, 'zip_path': None,
                'messages': messages,
            }

        return {'status': 'ok', 'packet_dir': packet_dir, 'zip_path': zip_path, 'messages': messages}
    finally:
        conn.close()


def run_packet(
    archive_root: Path,
    pid: str,
    out_dir: Path,
    *,
    include_research: bool = False,
    include_restricted: bool = False,
    include_dna: bool = False,
    no_photos: bool = False,
    dry_run: bool = False,
    overwrite: bool = False,
) -> Result:
    """Build a person packet and return a Result.

    `data` is the `_packet_payload` dict ({'status', 'packet_dir', 'zip_path',
    'messages'}); Result exposes dict-style access (_lib.py), so callers keep
    reading `result['status']` / `result['packet_dir']` unchanged.  On a real
    build the written packet directory and zip are listed in `changed`; a
    --dry-run (status 'dry-run') writes nothing and leaves `changed` empty.
    """
    payload = _packet_payload(
        archive_root, pid, out_dir,
        include_research=include_research, include_restricted=include_restricted,
        include_dna=include_dna, no_photos=no_photos, dry_run=dry_run,
        overwrite=overwrite,
    )
    changed: list[str] = []
    if payload['status'] == 'ok':
        for key in ('packet_dir', 'zip_path'):
            value = payload.get(key)
            if value:
                changed.append(str(value))
    return Result(
        ok=(payload['status'] in ('ok', 'dry-run')),
        data=payload,
        changed=changed,
    )


# ── CLI ────────────────────────────────────────────────────────────────────────

def _cmd_packet(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE

    pid = normalize_id(getattr(args, 'person_id', ''))
    if not pid:
        print('ERROR: a P-id argument is required.', file=sys.stderr)
        return EXIT_FAILURE

    out_dir = Path(getattr(args, 'out', None) or 'out')
    if not out_dir.is_absolute():
        out_dir = archive_root / out_dir

    result = run_packet(
        archive_root, pid, out_dir,
        include_research=getattr(args, 'include_research', False),
        include_restricted=getattr(args, 'include_restricted', False),
        include_dna=getattr(args, 'include_dna', False),
        no_photos=getattr(args, 'no_photos', False),
        dry_run=getattr(args, 'dry_run', False),
        overwrite=getattr(args, 'overwrite', False),
    )

    for m in result['messages']:
        print(m, file=sys.stderr)

    status = result['status']
    if status == 'no-index':
        return EXIT_FAILURE
    if status == 'bad-output-path':
        return EXIT_FAILURE
    if status == 'bad-config':
        return EXIT_FAILURE
    if status == 'not-found':
        print(f'{pid}: not found in index.', file=sys.stderr)
        return EXIT_WARNINGS
    if status == 'not-curated':
        print(f'{pid}: not a curated person — packets are only built for curated profiles.', file=sys.stderr)
        return EXIT_WARNINGS
    if status == 'living-subject':
        return EXIT_FAILURE
    if status == 'no-photoindex':
        return EXIT_FAILURE
    if status == 'output-exists':
        return EXIT_FAILURE
    if status == 'write-failed':
        return EXIT_FAILURE
    if status == 'dry-run':
        print('(dry run — no changes written)')
        print(f'Would write: {_display_path(result["packet_dir"], archive_root)}')
        print(f'Would zip:   {_display_path(result["zip_path"], archive_root)}')
        return EXIT_WARNINGS if result['messages'] else EXIT_CLEAN

    print(f'Packet written: {_display_path(result["packet_dir"], archive_root)}')
    print(f'Zip:            {_display_path(result["zip_path"], archive_root)}')
    return EXIT_WARNINGS if result['messages'] else EXIT_CLEAN


def register(subs: argparse._SubParsersAction) -> argparse.ArgumentParser:
    p = subs.add_parser(
        'packet',
        help='Build a person export packet (profile, timeline, sources, files, photos) and zip it.',
        description=(
            'Gather everything the archive knows about one curated person into\n'
            'packet_{surname}_{P-id}_{date}/, then zip it. A private/family export,\n'
            'not a publication format (TOOLING §8).'
        ),
    )
    p.add_argument('person_id', metavar='P-id', help='Curated person to export.')
    p.add_argument('-o', '--out', metavar='PATH', dest='out',
                    help="Output directory (default: 'out/' under the archive root).")
    p.add_argument('--include-research', action='store_true',
                    help="Include the person's research.md alongside the profile.")
    p.add_argument('--include-restricted', action='store_true',
                    help='Include restricted (non-DNA) sources. Excluded by default.')
    p.add_argument('--include-dna', action='store_true',
                    help='Include DNA sources. Excluded even with --include-restricted.')
    p.add_argument('--no-photos', action='store_true',
                    help='Skip photo gathering entirely (no photoindex required).')
    p.add_argument('--dry-run', action='store_true', dest='dry_run',
                    help='Preview the packet path and checks without writing files.')
    p.add_argument('--overwrite', action='store_true',
                    help='Replace an existing same-name packet directory/zip.')
    p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    p.add_argument('--spec-root', metavar='PATH', help='Spec docs root (accepted for CLI consistency).')
    p.set_defaults(func=_cmd_packet)
    return p


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha packet', description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('person_id', metavar='P-id', help='Curated person to export.')
    parser.add_argument('-o', '--out', metavar='PATH', dest='out',
                        help="Output directory (default: 'out/' under the archive root).")
    parser.add_argument('--include-research', action='store_true')
    parser.add_argument('--include-restricted', action='store_true')
    parser.add_argument('--include-dna', action='store_true')
    parser.add_argument('--no-photos', action='store_true')
    parser.add_argument('--dry-run', action='store_true', dest='dry_run')
    parser.add_argument('--overwrite', action='store_true')
    parser.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    parser.add_argument('--spec-root', metavar='PATH', help='Spec docs root (accepted for CLI consistency).')
    parser.set_defaults(func=_cmd_packet)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
