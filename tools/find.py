#!/usr/bin/env python3
"""
find.py — fha find: the universal locator.

  fha find <ID>                Locate any archive ID (P/S/C/L/H)
  fha find --text "phrase"     Full-text search across records, notes, transcripts
  fha find --related <ID>      Neighborhood of any ID  [deferred to milestone 3]

Detects ID type from its prefix letter and prints structured output.
Uses the SQLite index when present.  If the index is stale, it prints a
warning and still gives the structured report; if the index is absent or
unreadable, it degrades to a tree scan.

fha id check <ID> is wired as an alias in fha.py.  The canonical
implementation of "where does this ID live?" now lives here.  TOOLING §4a.

Design decisions in TOOLING §4a:
  D4: --related deferred to milestone 3 (after fha xref / fha cooccur).
  D7: --text scope is records + notes + transcripts in milestone 2;
      photo captions added once photoindex is built.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    configure_utf8_stdout,
    find_archive_root,
    id_type_of,
    is_valid_id,
    load_fha_yaml,
    newest_record_mtime,
    normalize_id,
    read_record,
    resolve_path,
)

configure_utf8_stdout()

# ── CODE MAP ──────────────────────────────────────────────────────────────────
#
#  Index freshness helpers (newest_record_mtime imported from _lib)
#    _index_is_fresh           — compare index.sqlite vs record mtimes
#    _open_index               — open .cache/index.sqlite; returns None on failure
#
#  Per-ID-type finders (index path)
#    _find_person              — P-id: file, couple folder, companions, claims, cites
#    _find_source              — S-id: record, files (resolved + on-disk), claims, cites
#    _find_claim               — C-id: source record, line, status, value, links
#    _find_place               — L-id: place entry, claims referencing it, mentions
#    _find_hypothesis          — H-id: research file, heading, status, verified_claim
#
#  Tree-scan fallback (no fresh index)
#    _find_by_scan             — grep all text files for bare ID string
#
#  Text search
#    _find_text                — FTS (when index fresh) + re.search over record bodies
#
#  Public API
#    find_by_id                — locate a single ID; called by fha id check alias in fha.py
#    run_find                  — full dispatcher (id | text | --related)
#
#  CLI
#    register                  — attach 'find' to the main fha parser
#    _run_find                 — argparse → run_find bridge
#    _standalone_main          — for `python tools/find.py` direct invocation
#
# ─────────────────────────────────────────────────────────────────────────────

_OK  = '✓'
_BAD = '✗'


# ── Index freshness ───────────────────────────────────────────────────────────

def _index_is_fresh(archive_root: Path) -> bool:
    """Return True if .cache/index.sqlite exists and is not stale vs record files."""
    db_path = archive_root / '.cache' / 'index.sqlite'
    if not db_path.exists():
        return False
    try:
        db_mtime = db_path.stat().st_mtime
    except OSError:
        return False
    record_mtime = newest_record_mtime(archive_root)
    if record_mtime == 0.0:
        return True   # no records yet — trivially current
    return db_mtime >= record_mtime


def _open_index(archive_root: Path) -> sqlite3.Connection | None:
    """Open .cache/index.sqlite for reading; return None if missing or corrupt."""
    db_path = archive_root / '.cache' / 'index.sqlite'
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


# ── Per-ID-type finders (index path) ─────────────────────────────────────────

def _find_person(
    pid: str,
    conn: sqlite3.Connection,
    archive_root: Path,
    fha_config: dict,
) -> int:
    """
    Print the P-id report: file path, couple folder, companion files,
    claim summary grouped by type+status, citation sites, and photo note.

    The couple folder is the parent directory of the profile file — the
    meaningful unit of organisation in the people/ tree.  Companion files
    (research, timeline, sources-index, draft-queue) share the P-id and
    live alongside the profile; they're stored in person_files with kind != 'profile'.
    """
    row = conn.execute(
        "SELECT id, name, living, tier, path FROM persons WHERE id = ?", (pid,)
    ).fetchone()
    if row is None:
        print(f'{pid}: not found in index.')
        return EXIT_WARNINGS

    profile_path = row['path']
    # couple folder = parent dir name (the "NNN Surname + Surname [children]" folder)
    folder_path = Path(profile_path).parent
    couple_folder = folder_path.name if str(folder_path) not in ('.', '') else '(archive root)'

    print(f'{pid}  [{row["name"]}]')
    print(f'  living: {row["living"]}   tier: {row["tier"]}')
    print(f'  file:          {profile_path}')
    print(f'  couple folder: {couple_folder}')

    # Companion files: research, timeline, sources-index, draft-queue
    companions = conn.execute(
        """
        SELECT kind, path FROM person_files
        WHERE person_id = ? AND kind != 'profile'
        ORDER BY kind
        """,
        (pid,)
    ).fetchall()
    if companions:
        print('  companion files:')
        for c in companions:
            print(f'    {c["kind"]}: {c["path"]}')
    else:
        print('  companion files: none')

    # Claim summary: type + status → count
    claim_rows = conn.execute(
        """
        SELECT c.type, c.status, COUNT(*) AS n
        FROM claims c
        JOIN claim_persons cp ON c.id = cp.claim_id
        WHERE cp.person_id = ?
        GROUP BY c.type, c.status
        ORDER BY c.type, c.status
        """,
        (pid,)
    ).fetchall()
    if claim_rows:
        print('  claims:')
        for r in claim_rows:
            print(f'    {r["type"]} / {r["status"]}: {r["n"]}')
    else:
        print('  claims: none')

    # Citation sites: prose [P-id] references in other records
    citations = conn.execute(
        "SELECT path, line FROM citations WHERE token = ? ORDER BY path, line",
        (pid,)
    ).fetchall()
    if citations:
        print(f'  citation sites ({len(citations)}):')
        for c in citations:
            print(f'    {c["path"]}:{c["line"]}')
    else:
        print('  citation sites: none')

    # Photo count requires photoindex
    photos_db = archive_root / '.cache' / 'photos.sqlite'
    if photos_db.exists():
        try:
            pconn = sqlite3.connect(str(photos_db))
            pconn.row_factory = sqlite3.Row
            count = pconn.execute(
                "SELECT COUNT(DISTINCT path) FROM photo_people WHERE person_ref = ?",
                (pid,)
            ).fetchone()[0]
            pconn.close()
            print(f'  photos: {count}')
        except Exception:
            print('  photos: not indexed (run fha photoindex)')
    else:
        print('  photos: not indexed (run fha photoindex)')

    return EXIT_CLEAN


def _find_source(
    sid: str,
    conn: sqlite3.Connection,
    archive_root: Path,
    fha_config: dict,
) -> int:
    """
    Print the S-id report: record path, files with resolved paths and on-disk
    status (✓ / ✗ / missing-fixture), claim counts by status, citation sites.

    On-disk status is drawn from source_files.exists_on_disk; 'missing-fixture'
    is detected by re-reading the source record's files: list for entries that
    carry status: missing-fixture in the YAML (a fixture-only exception).
    """
    row = conn.execute(
        "SELECT id, title, source_type, path, restricted FROM sources WHERE id = ?",
        (sid,)
    ).fetchone()
    if row is None:
        print(f'{sid}: not found in index.')
        return EXIT_WARNINGS

    restricted_label = '  [restricted]' if row['restricted'] else ''
    print(f'{sid}  [{row["title"]}]{restricted_label}')
    print(f'  source_type: {row["source_type"] or "(none)"}')
    print(f'  record: {row["path"]}')

    # Files: pull from source_files, but also re-read the YAML inventory to
    # catch 'status: missing-fixture' which the index doesn't store separately.
    file_rows = conn.execute(
        "SELECT path, role, exists_on_disk FROM source_files WHERE source_id = ? ORDER BY path",
        (sid,)
    ).fetchall()
    if file_rows:
        # Build missing-fixture set from the YAML record so we don't incorrectly mark ✗
        missing_fixture_paths: set[str] = set()
        record_abs = archive_root / row['path']
        if record_abs.exists():
            rec = read_record(record_abs)
            for f_entry in rec['meta'].get('files') or []:
                if isinstance(f_entry, dict):
                    status_val = str(f_entry.get('status', ''))
                    if 'missing-fixture' in status_val:
                        missing_fixture_paths.add(str(f_entry.get('file', '')))

        print(f'  files ({len(file_rows)}):')
        for f in file_rows:
            role_str = f'  [{f["role"]}]' if f['role'] else ''
            try:
                resolved = resolve_path(f['path'], fha_config, archive_root)
                resolved_str = str(resolved)
            except Exception:
                resolved_str = '(unresolvable)'
            if f['path'] in missing_fixture_paths:
                status_sym = 'missing-fixture'
            elif f['exists_on_disk']:
                status_sym = _OK
            else:
                status_sym = _BAD
            print(f'    {f["path"]}{role_str}  →  {resolved_str}  {status_sym}')
    else:
        print('  files: none in inventory')

    # Claim counts by status
    status_rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM claims WHERE source_id = ? GROUP BY status ORDER BY status",
        (sid,)
    ).fetchall()
    if status_rows:
        parts = [f'{r["status"]}: {r["n"]}' for r in status_rows]
        print(f'  claims: {", ".join(parts)}')
    else:
        print('  claims: none')

    # Citation sites
    citations = conn.execute(
        "SELECT path, line FROM citations WHERE token = ? ORDER BY path, line",
        (sid,)
    ).fetchall()
    if citations:
        print(f'  citation sites ({len(citations)}):')
        for c in citations:
            print(f'    {c["path"]}:{c["line"]}')
    else:
        print('  citation sites: none')

    return EXIT_CLEAN


def _find_claim(
    cid: str,
    conn: sqlite3.Connection,
    archive_root: Path,
    fha_config: dict,
) -> int:
    """
    Print the C-id report: source record path and approximate line (found by
    scanning the source file for the claim ID), status, value, and any
    corroborates/contradicts links.

    Line number is approximate because the claims block is YAML — the line
    shown is the first occurrence of the C-id string in the file.
    """
    row = conn.execute(
        "SELECT id, source_id, type, status, value, date_edtf FROM claims WHERE id = ?",
        (cid,)
    ).fetchone()
    if row is None:
        print(f'{cid}: not found in index.')
        return EXIT_WARNINGS

    # Source record path
    src_row = conn.execute(
        "SELECT path FROM sources WHERE id = ?", (row['source_id'],)
    ).fetchone()
    src_path = src_row['path'] if src_row else '(source not found)'

    # Approximate line — scan the source file for the first occurrence of cid
    approx_line: int | None = None
    if src_row:
        src_abs = archive_root / src_path
        if src_abs.exists():
            try:
                cid_norm = cid.lower()
                with open(src_abs, encoding='utf-8', errors='ignore') as fh:
                    for lineno, line in enumerate(fh, start=1):
                        if cid_norm in line.lower():
                            approx_line = lineno
                            break
            except OSError:
                pass

    loc = f'{src_path}:{approx_line}' if approx_line else src_path
    print(f'{cid}')
    print(f'  source:  {loc}')
    print(f'  type:    {row["type"]}')
    if row['date_edtf']:
        print(f'  date:    {row["date_edtf"]}')
    print(f'  status:  {row["status"]}')
    print(f'  value:   {row["value"]}')

    links = conn.execute(
        "SELECT rel, target_id FROM claim_links WHERE claim_id = ? ORDER BY rel, target_id",
        (cid,)
    ).fetchall()
    for lnk in links:
        print(f'  {lnk["rel"]}: {lnk["target_id"]}')

    return EXIT_CLEAN


def _find_place(
    lid: str,
    conn: sqlite3.Connection,
    archive_root: Path,
) -> int:
    """
    Print the L-id report: place name, hierarchy, coordinates, every claim
    referencing this place (via place_id), and records that mention [L-id]
    (via citations table).
    """
    row = conn.execute(
        "SELECT id, name, hierarchy, lat, lon FROM places WHERE id = ?", (lid,)
    ).fetchone()
    if row is None:
        print(f'{lid}: not found in index.')
        return EXIT_WARNINGS

    coords = f'({row["lat"]}, {row["lon"]})' if row['lat'] is not None else 'no coords'
    print(f'{lid}  {row["name"]}')
    print(f'  hierarchy: {row["hierarchy"] or "(none)"}')
    print(f'  coords:    {coords}')

    # Claims referencing this place
    claim_rows = conn.execute(
        """
        SELECT c.id, c.type, c.value, c.status, c.date_edtf,
               GROUP_CONCAT(cp.person_id, ', ') AS persons
        FROM claims c
        LEFT JOIN claim_persons cp ON c.id = cp.claim_id
        WHERE c.place_id = ?
        GROUP BY c.id
        ORDER BY c.date_min
        """,
        (lid,)
    ).fetchall()
    if claim_rows:
        print(f'  claims ({len(claim_rows)}):')
        for c in claim_rows:
            date_str = f'  {c["date_edtf"]}' if c['date_edtf'] else ''
            value_preview = c['value'][:60] + ('…' if len(c['value']) > 60 else '')
            print(f'    {c["id"]}  {c["type"]}{date_str}: {value_preview}  [{c["status"]}]')
            if c['persons']:
                print(f'      persons: {c["persons"]}')
    else:
        print('  claims: none reference this place')

    # Records mentioning [L-id] in prose
    citations = conn.execute(
        "SELECT path, line FROM citations WHERE token = ? ORDER BY path, line",
        (lid,)
    ).fetchall()
    if citations:
        print(f'  mentioned in ({len(citations)}):')
        for c in citations:
            print(f'    {c["path"]}:{c["line"]}')
    else:
        print('  mentioned in: none')

    return EXIT_CLEAN


def _find_hypothesis(
    hid: str,
    conn: sqlite3.Connection,
    archive_root: Path,
) -> int:
    """
    Print the H-id report: research file path, section heading (by scanning the
    file for the H-id to locate its ## heading), status, verified_claim if set,
    and every record mentioning [H-id].

    Section heading is found by walking upward from the H-id's line in the file
    to the most recent ## heading — this reflects where the hypothesis is
    documented in the research narrative.
    """
    row = conn.execute(
        "SELECT id, person_id, hypothesis, status, verified_claim, path FROM hypotheses WHERE id = ?",
        (hid,)
    ).fetchone()
    if row is None:
        print(f'{hid}: not found in index.')
        return EXIT_WARNINGS

    print(f'{hid}')
    print(f'  file:   {row["path"]}')
    if row['person_id']:
        print(f'  person: {row["person_id"]}')
    print(f'  status: {row["status"] or "(none)"}')
    if row['verified_claim']:
        print(f'  verified_claim: {row["verified_claim"]}')
    if row['hypothesis']:
        preview = row['hypothesis'][:100] + ('…' if len(row['hypothesis']) > 100 else '')
        print(f'  hypothesis: {preview}')

    # Find section heading in the research file
    if row['path']:
        res_abs = archive_root / row['path']
        if res_abs.exists():
            heading = _find_section_heading(res_abs, hid)
            if heading:
                print(f'  section: {heading}')

    # Records mentioning [H-id]
    citations = conn.execute(
        "SELECT path, line FROM citations WHERE token = ? ORDER BY path, line",
        (hid,)
    ).fetchall()
    if citations:
        print(f'  mentioned in ({len(citations)}):')
        for c in citations:
            print(f'    {c["path"]}:{c["line"]}')
    else:
        print('  mentioned in: none')

    return EXIT_CLEAN


def _find_section_heading(path: Path, target_id: str) -> str | None:
    """
    Walk the file and return the most recent ## heading before the line
    containing target_id.  Used to locate where a hypothesis is documented.
    """
    id_norm = target_id.lower()
    last_heading: str | None = None
    try:
        for line in path.read_text(encoding='utf-8', errors='ignore').splitlines():
            if line.startswith('## '):
                last_heading = line.lstrip('# ').strip()
            if id_norm in line.lower():
                return last_heading
    except OSError:
        pass
    return None


# ── Tree-scan fallback ────────────────────────────────────────────────────────

def _find_by_scan(id_str: str, archive_root: Path) -> int:
    """
    Grep all text files for id_str when the index is absent or stale.
    Reports one hit per file (enough to locate the record); prints a
    WARNING header so the caller knows this is a degraded fallback.
    """
    id_norm = id_str.lower()
    hits: list[tuple[str, int]] = []

    candidates = [
        p for p in archive_root.rglob('*')
        if p.is_file()
        and p.suffix.lower() in ('.md', '.yaml', '.yml', '.txt')
        and '.cache' not in p.parts
    ]
    for path in sorted(candidates):
        try:
            for lineno, line in enumerate(
                path.read_text(encoding='utf-8', errors='ignore').splitlines(),
                start=1,
            ):
                if id_norm in line.lower():
                    hits.append((str(path.relative_to(archive_root)), lineno))
                    break   # one hit per file is enough
        except OSError:
            pass

    if not hits:
        print(f'{id_str}: not found in archive tree.')
        return EXIT_WARNINGS

    print(f'{id_str}: found in {len(hits)} file(s):')
    for rel, lineno in hits:
        print(f'  {rel}:{lineno}')
    return EXIT_CLEAN


# ── Text search ───────────────────────────────────────────────────────────────

def _find_text(
    query: str,
    archive_root: Path,
    fha_config: dict,
    conn: sqlite3.Connection | None,
) -> int:
    """
    Search records, notes, and transcripts for query text.

    When the index is fresh: query notes_fts and transcripts_fts FTS tables
    first (fast, ranked), then do a re.search pass to catch anything the FTS
    tables may not cover (e.g. fresh lint-only run without FTS populated).

    When the index is absent: re.search only.

    Design decision D7 (TOOLING §4a): photo/document captions are not searched
    in milestone 2 — they require the photoindex.  A note is printed when the
    photoindex is absent so the user knows the scope.
    """
    hits: list[tuple[str, str]] = []   # (relative path, context snippet)
    seen_paths: set[str] = set()

    # FTS queries from the index
    if conn is not None:
        try:
            # FTS5 snippet: 32 tokens, mark matches with [...]
            fts_sql = (
                "SELECT path, snippet({table}, {col}, '[', ']', '…', 32) "
                "FROM {table} WHERE {table} MATCH ?"
            )
            for row in conn.execute(
                fts_sql.format(table='notes_fts', col='1'), (query,)
            ):
                rel = row[0]
                hits.append((rel, row[1]))
                seen_paths.add(rel)
            for row in conn.execute(
                fts_sql.format(table='transcripts_fts', col='2'), (query,)
            ):
                rel = row[0]
                if rel not in seen_paths:
                    hits.append((rel, row[1]))
                    seen_paths.add(rel)
        except Exception:
            pass   # FTS tables absent (index built without note content) — fall through

    # re.search pass over all record directories
    pattern = re.compile(re.escape(query), re.I)
    for d_name in ('sources', 'people', 'notes'):
        d = archive_root / d_name
        if not d.is_dir():
            continue
        for p in sorted(d.rglob('*.md')):
            rel = str(p.relative_to(archive_root))
            if rel in seen_paths:
                continue
            try:
                text = p.read_text(encoding='utf-8', errors='ignore')
                m = pattern.search(text)
                if m:
                    # Extract 2 lines of context around the match
                    line_start = text.rfind('\n', 0, m.start()) + 1
                    line_end = text.find('\n', m.end())
                    if line_end == -1:
                        line_end = len(text)
                    context = text[line_start:line_end].strip()[:120]
                    hits.append((rel, context))
                    seen_paths.add(rel)
            except OSError:
                pass

    photos_db_absent = not (archive_root / '.cache' / 'photos.sqlite').exists()

    if not hits:
        print(f'No results for: {query!r}')
        if photos_db_absent:
            print('Note: photo captions not searched — run fha photoindex to include.')
        return EXIT_CLEAN

    print(f'Found {len(hits)} result(s) for: {query!r}')
    for rel_path, context in hits:
        print(f'\n  {rel_path}')
        if context:
            print(f'    … {context} …')
    if photos_db_absent:
        print('\nNote: photo captions not searched — run fha photoindex to include.')

    return EXIT_CLEAN


# ── Public API ────────────────────────────────────────────────────────────────

def _text_search(query: str, archive_root: Path, fha_config: dict) -> int:
    """
    Open the index when present, then call _find_text.

    A stale index still has useful FTS tables, and _find_text also performs a
    live re.search pass over record bodies.  So stale means "warn and search
    both surfaces," not "throw away the structured search surface."  Only an
    absent or unreadable index runs scan-only.
    """
    fresh = _index_is_fresh(archive_root)
    if not fresh:
        print('WARNING: index not fresh, results may be incomplete')
    conn = _open_index(archive_root)
    try:
        return _find_text(query, archive_root, fha_config, conn)
    finally:
        if conn is not None:
            conn.close()


def find_by_id(
    id_str: str,
    archive_root: Path,
    fha_config: dict,
) -> int:
    """
    Locate a single archive ID and print a structured report.

    Called by run_find and also directly from fha.py's main() for the
    `fha id check <ID>` alias.  Uses the SQLite index when present; stale
    indexes produce a warning but still give the structured report.  This keeps
    `find` useful after generated views change their mtimes.  Only an absent
    or unreadable index falls back to a tree scan.
    """
    id_str = normalize_id(id_str)
    if not is_valid_id(id_str):
        print(f'ERROR: {id_str!r} is not a valid archive ID.', file=sys.stderr)
        return EXIT_FAILURE

    id_type = id_type_of(id_str)

    fresh = _index_is_fresh(archive_root)
    if not fresh:
        print('WARNING: index not fresh, results may be incomplete')

    conn = _open_index(archive_root)
    if conn is None:
        return _find_by_scan(id_str, archive_root)

    try:
        if id_type == 'P':
            return _find_person(id_str, conn, archive_root, fha_config)
        elif id_type == 'S':
            return _find_source(id_str, conn, archive_root, fha_config)
        elif id_type == 'C':
            return _find_claim(id_str, conn, archive_root, fha_config)
        elif id_type == 'L':
            return _find_place(id_str, conn, archive_root)
        elif id_type == 'H':
            return _find_hypothesis(id_str, conn, archive_root)
        else:
            print(f'{id_str}: unknown ID type prefix.', file=sys.stderr)
            return EXIT_FAILURE
    finally:
        conn.close()


def run_find(
    id_or_text: str | None,
    archive_root: Path,
    fha_config: dict,
    text_mode: bool = False,
    related_id: str | None = None,
) -> int:
    """
    Top-level dispatcher:
      - --related <ID>        → deferred message (D4)
      - --text "phrase"       → full-text search
      - bare valid ID         → find_by_id
      - bare non-ID string    → treated as text search
    """
    # --related is deferred to milestone 3 (Design decision D4, TOOLING §4a)
    if related_id is not None:
        print(
            '--related is not yet available. It will be implemented in milestone 3 '
            'after fha xref and fha cooccur are built. '
            '(Design decision D4, TOOLING §4a)'
        )
        return EXIT_CLEAN

    # Text search path
    if text_mode:
        return _text_search(id_or_text or '', archive_root, fha_config)

    # Bare argument — distinguish ID from text
    if not id_or_text:
        print('ERROR: provide an ID or --text "phrase"', file=sys.stderr)
        return EXIT_FAILURE

    id_norm = normalize_id(id_or_text)
    if is_valid_id(id_norm):
        return find_by_id(id_norm, archive_root, fha_config)

    # Doesn't look like an ID — treat as text search
    return _text_search(id_or_text, archive_root, fha_config)


# ── CLI ───────────────────────────────────────────────────────────────────────

def register(subparsers: argparse._SubParsersAction) -> None:
    """Register 'find' onto the main fha parser."""
    p = subparsers.add_parser(
        'find',
        help='Locate any ID, or full-text search across records and notes',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root')
    p.add_argument('--spec-root', metavar='PATH', help='Spec docs root')

    # Mutually exclusive modes
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        '--text', metavar='PHRASE',
        help='Full-text search across records, notes, transcripts',
    )
    mode.add_argument(
        '--related', metavar='ID',
        help='Neighborhood of an ID — people/places/sources adjacent (deferred to milestone 3)',
    )

    p.add_argument(
        'query',
        nargs='?',
        metavar='ID_OR_TEXT',
        help='Archive ID (P-/S-/C-/L-/H-) or text to search',
    )
    p.set_defaults(func=_run_find)


def _run_find(args: argparse.Namespace) -> int:
    root = getattr(args, 'root', None)
    if root:
        archive_root = Path(root).resolve()
    else:
        archive_root = find_archive_root()
        if archive_root is None:
            print('ERROR: cannot find archive root. Use --root.', file=sys.stderr)
            return EXIT_FAILURE

    fha_config = load_fha_yaml(archive_root)

    text_query = getattr(args, 'text', None)
    related = getattr(args, 'related', None)
    query = getattr(args, 'query', None)

    if text_query is not None:
        return run_find(text_query, archive_root, fha_config, text_mode=True)
    elif related is not None:
        return run_find(None, archive_root, fha_config, related_id=related)
    elif query:
        return run_find(query, archive_root, fha_config)
    else:
        print('Usage: fha find <ID>  |  fha find --text "phrase"  |  fha find --related <ID>',
              file=sys.stderr)
        return EXIT_FAILURE


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha find',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--root', metavar='PATH', help='Archive root')
    parser.add_argument('--spec-root', metavar='PATH', help='Spec docs root')

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--text', metavar='PHRASE')
    mode.add_argument('--related', metavar='ID')

    parser.add_argument('query', nargs='?', metavar='ID_OR_TEXT')
    args = parser.parse_args(argv)
    return _run_find(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
