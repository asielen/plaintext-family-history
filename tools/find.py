#!/usr/bin/env python3
"""
find.py - fha find: the universal locator.

  fha find <ID>                       Locate any archive ID (P/S/C/L/H)
  fha find --text "phrase"            Full-text search across records, notes, transcripts
  fha find --related <ID> [--date E]  Neighborhood of any ID, optionally time-sliced
  fha find --related --date <EDTF>    Standalone time-slice neighborhood (no ID)

Detects ID type from its prefix letter and prints structured output.
Uses the SQLite index when present.  If the index is stale, it prints a
warning and still gives the structured report; if the index is absent or
unreadable, it degrades to a tree scan.

fha id check <ID> is wired as an alias in fha.py.  The canonical
implementation of "where does this ID live?" now lives here.  TOOLING §4a.

Design decisions in TOOLING §4a:
  D4: --related implemented (BUILD.md M4.3), after fha xref / fha cooccur.
      It is a pure read-only query over the index - no schema change, no
      writes. Unlike find_by_id, --related requires a real index (the
      relational joins it runs have no meaningful tree-scan fallback): an
      absent or unreadable index is a hard error (exit 3), not a degrade.
  D7: --text searches records + notes; photo captions are searched when
      .cache/photos.sqlite is fresh (else find prints a skip note). The
      transcripts_fts table exists but is not yet populated - transcript
      search is deferred to a later milestone.
"""

from __future__ import annotations

import argparse
import itertools
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    FhaConfigError,
    Result,
    configure_utf8_stdout,
    edtf_bounds,
    format_edtf_error,
    id_type_of,
    is_valid_edtf,
    is_valid_id,
    is_working_copy,
    load_fha_yaml,
    newest_record_mtime,
    normalize_date,
    normalize_id,
    normalize_place_text,
    open_index_db,
    photoindex_status,
    read_record,
    resolve_path,
    resolve_root_arg,
)

configure_utf8_stdout()

# ── CODE MAP ──────────────────────────────────────────────────────────────────
#
#  Index freshness helpers (newest_record_mtime imported from _lib)
#    _index_is_fresh           - compare index.sqlite vs record mtimes
#    _open_index               - open .cache/index.sqlite; returns None on failure
#
#  Per-ID-type finders (index path)
#    _find_person              - P-id: file, couple folder, companions, claims, cites
#    _find_source              - S-id: record, files (resolved + on-disk), claims, cites
#    _find_claim               - C-id: source record, line, status, value, links
#    _find_place               - L-id: place entry, claims referencing it, mentions
#    _find_hypothesis          - H-id: research file, heading, status, verified_claim
#
#  Tree-scan fallback (no fresh index)
#    _find_by_scan             - grep all text files for bare ID string
#
#  Text search
#    _find_text                - FTS (when index fresh) + re.search over record bodies
#
#  --related (M4.3): neighborhood queries over the relational index
#    (opened via _lib.open_index_db with the full relational table set - see
#    _RELATED_REQUIRED_TABLES below)
#    _related_person           - P-id world: edges, co-occurrence, places, hubs, sources, photos
#    _person_cooccur_neighbors - co-occurring persons with no existing relationship edge
#    _person_places            - places ranked by this person's claim frequency
#    _person_org_hubs          - recurring occupation/military/membership affiliations shared
#                                 with others
#    _person_source_count      - distinct source count for a person
#    _print_person_photos      - photo_people lookup in .cache/photos.sqlite
#    _related_place            - L-id world: claims, people, sources, micro-places, photos
#    _print_place_photos       - GPS-proximity photo lookup for a place
#    _related_source           - S-id world: claims, persons, places, linked/sibling sources
#    _related_claim            - C-id neighborhood: source, persons, place, links, siblings
#    _related_hypothesis       - H-id neighborhood: person, referencing claims, verifying claim
#    _related_date             - standalone --date time-slice neighborhood
#    _related_dispatch         - top-level neighborhood dispatcher (prints, returns int)
#    run_related                - wraps _related_dispatch into the Result contract
#
#  Public API
#    find_by_id                - locate a single ID; called by fha id check alias in fha.py
#    run_find                  - full dispatcher (id | text | --related); returns a Result
#    _as_find_result           - wrap a find/related exit code into a Result
#
#  CLI
#    register                  - attach 'find' to the main fha parser
#    _run_find                 - argparse → run_find bridge (returns the int exit code)
#    _standalone_main          - for `python tools/find.py` direct invocation
#
# ─────────────────────────────────────────────────────────────────────────────

_OK  = '✓'
_BAD = '✗'

_REL_INVERSE: dict[str, str] = {
    'corroborates': 'corroborated-by',
    'contradicts':  'contradicted-by',
}

# Sentinel distinguishing "--related not given at all" from "--related given
# with no ID value" (the standalone `--related --date EDTF` form). Both end
# up routed to run_related, but only the latter sets related_requested=True
# with related_id=None.
_NO_RELATED = object()


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
        return True   # no records yet - trivially current
    return db_mtime >= record_mtime


def _open_index(archive_root: Path) -> sqlite3.Connection | None:
    """
    Open .cache/index.sqlite for reading; return None if missing, corrupt, or
    schema-less - silently, with no printed message.

    Deliberately not `_lib.open_index_db`: this is the find_by_id / --text
    path, where an absent or unreadable index is *not* fatal - the caller
    falls back to a tree scan and prints its own warning about that
    fallback. `open_index_db` (used by --related, which has no tree-scan
    fallback) prints its own ERROR/WARNING and is meant to be the final
    word on whether the index is usable.
    """
    db_path = archive_root / '.cache' / 'index.sqlite'
    if not db_path.exists():
        return None
    conn: sqlite3.Connection | None = None
    try:
        # sqlite3.connect() itself can raise (path is a directory, permission
        # denied, locked) - keep it inside the guard so this fallback-eligible
        # path returns None and lets find_by_id/_text_search degrade to a tree
        # scan instead of raising a traceback.
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        # Probe for a required table so empty/corrupt files fail fast here
        # rather than raising DatabaseError inside per-ID queries.
        conn.execute('SELECT 1 FROM persons LIMIT 1')
        return conn
    except Exception:
        if conn is not None:
            conn.close()
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

    The couple folder is the parent directory of the profile file - the
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
            resolved = None
            try:
                resolved = resolve_path(f['path'], fha_config, archive_root)
                resolved_str = str(resolved)
            except Exception:
                resolved_str = '(unresolvable)'
            if f['path'] in missing_fixture_paths:
                status_sym = 'missing-fixture'
            elif f['exists_on_disk'] is None:
                status_sym = '~'  # NULL = assumed present on main machine (working-copy mode)
            elif f['exists_on_disk'] == 0 and is_working_copy(archive_root):
                # Stale index built before the WORKING_COPY marker existed - WC mode overrides
                status_sym = '~'
            elif resolved is not None and resolved.exists():
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

    Line number is approximate because the claims block is YAML - the line
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

    # Approximate line - scan the source file for the first occurrence of cid
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

    incoming = conn.execute(
        "SELECT rel, claim_id FROM claim_links WHERE target_id = ? ORDER BY rel, claim_id",
        (cid,)
    ).fetchall()
    for lnk in incoming:
        label = _REL_INVERSE.get(lnk['rel'], f'{lnk["rel"]}-by')
        print(f'  {label}: {lnk["claim_id"]}')

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
    to the most recent ## heading - this reflects where the hypothesis is
    documented in the research narrative.
    """
    row = conn.execute(
        "SELECT id, person_id, hypothesis, status, verified_claim, path FROM hypotheses WHERE id = ?",
        (hid,)
    ).fetchone()
    if row is None:
        # Hypothesis indexing is deferred - the index builder never populates the
        # `hypotheses` table - so a structured miss is expected. Fall back to a
        # tree scan so real H-ids documented in research files are still located.
        return _find_by_scan(hid, archive_root)

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
    Search records and notes for query text, plus photo captions when available.

    When the index is fresh: query notes_fts (and transcripts_fts, currently
    empty - transcript population is deferred) first, then a re.search pass to
    catch anything the FTS tables may not cover (e.g. a fresh lint-only run
    without FTS populated).

    When the index is absent: re.search only.

    Design decision D7 (TOOLING §4a): photo captions ARE searched when
    .cache/photos.sqlite is verifiably fresh (DB present, schema OK, newer than
    the photos root).  When the photoindex is absent/stale/unreadable, captions
    are skipped and an explicit note tells the user why.
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
            pass   # FTS tables absent (index built without note content) - fall through

    # re.search pass over all record directories.
    # documents/ uses ('*.md', '*.txt') to catch transcript files (role: transcription).
    # Resolve the documents root via fha.yaml 'roots:' so external asset roots work.
    doc_root_raw = (fha_config or {}).get('roots', {}).get('documents')
    if doc_root_raw:
        doc_root_p = Path(str(doc_root_raw))
        docs_root = doc_root_p if doc_root_p.is_absolute() else archive_root / doc_root_p
    else:
        docs_root = archive_root / 'documents'

    pattern = re.compile(re.escape(query), re.I)
    scan_dirs = [
        (archive_root / 'sources', ('*.md',)),
        (archive_root / 'people',  ('*.md',)),
        (archive_root / 'notes',   ('*.md',)),
        (docs_root,                ('*.md', '*.txt')),
    ]
    for scan_dir, globs in scan_dirs:
        if not scan_dir.is_dir():
            continue
        for p in sorted(itertools.chain.from_iterable(scan_dir.rglob(g) for g in globs)):
            try:
                rel = str(p.relative_to(archive_root))
            except ValueError:
                rel = str(p)  # external documents root: use absolute path as key
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

    # places/places.yaml is the record store for places (TOOLING) but isn't a
    # directory of .md files like the scan_dirs above, so it needs its own pass.
    places_path = archive_root / 'places' / 'places.yaml'
    if places_path.is_file():
        rel = str(places_path.relative_to(archive_root))
        if rel not in seen_paths:
            try:
                text = places_path.read_text(encoding='utf-8', errors='ignore')
                m = pattern.search(text)
                if m:
                    line_start = text.rfind('\n', 0, m.start()) + 1
                    line_end = text.find('\n', m.end())
                    if line_end == -1:
                        line_end = len(text)
                    context = text[line_start:line_end].strip()[:120]
                    hits.append((rel, context))
                    seen_paths.add(rel)
            except OSError:
                pass

    # Photo captions: search photo_fts only when the photo index is verifiably
    # fresh (DB present, schema OK, newer than the photos root). When it is
    # absent/stale/unreadable we skip and say so explicitly - never silently.
    photo_note: str | None = None
    photo_status, _ = photoindex_status(archive_root, fha_config)
    if photo_status == 'fresh':
        try:
            pconn = sqlite3.connect(str(archive_root / '.cache' / 'photos.sqlite'))
            pconn.row_factory = sqlite3.Row
            try:
                for row in pconn.execute(
                    "SELECT path, snippet(photo_fts, 2, '[', ']', '…', 32) "
                    "FROM photo_fts WHERE photo_fts MATCH ?",
                    (query,),
                ):
                    rel = f'[photo] {row[0]}'
                    if rel not in seen_paths:
                        hits.append((rel, row[1]))
                        seen_paths.add(rel)
            finally:
                pconn.close()
        except sqlite3.OperationalError:
            # Malformed FTS MATCH query (e.g. unbalanced quotes) - report, don't crash.
            photo_note = 'Note: photo caption search skipped - query is not valid FTS syntax.'
    elif photo_status == 'stale':
        photo_note = 'Note: photo captions not searched - photo index is stale; run fha photoindex.'
    elif photo_status == 'unreadable':
        photo_note = 'Note: photo captions not searched - photo index is unreadable; rebuild with fha photoindex.'
    else:  # 'absent'
        photo_note = 'Note: photo captions not searched - run fha photoindex to include.'

    if not hits:
        print(f'No results for: {query!r}')
        if photo_note:
            print(photo_note)
        return EXIT_CLEAN

    print(f'Found {len(hits)} result(s) for: {query!r}')
    for rel_path, context in hits:
        print(f'\n  {rel_path}')
        if context:
            print(f'    … {context} …')
    if photo_note:
        print(f'\n{photo_note}')

    return EXIT_CLEAN


# ── --related (M4.3): neighborhood queries over the relational index ────────

# Unlike _open_index (used by find_by_id, which can fall back to a tree
# scan), --related's joins across relationships/claim_links/claim_persons
# have no meaningful tree-scan equivalent - a missing or incompatible index
# is a hard error (caller returns exit 3), so --related uses _lib's shared
# open_index_db (same as xref.py/cooccur.py) instead of the quieter
# _open_index above. A stale index still warns but is queried, since
# --related is read-only and a slightly stale index is more useful than no
# answer at all.
_RELATED_REQUIRED_TABLES = (
    'persons', 'sources', 'claims', 'claim_persons', 'claim_links',
    'relationships', 'places', 'source_people', 'hypotheses',
)


def _overlap_clause(start_col: str, end_col: str) -> str:
    """
    SQL predicate for "this row's [start_col, end_col] range overlaps the
    caller's date_bounds", with both ends NULL-and-empty-string safe.

    index.py stores an undated claim's date_min/date_max as '' (not the
    unbounded sentinel edtf_bounds() would return for None) - see TOOLING
    §1 / edtf_bounds docstring. A raw `end_col >= ?` comparison would treat
    '' as the lowest possible value and exclude every undated row from a
    --date filter; the explicit '' / NULL checks restore the intended
    "undated = unbounded" semantics for the relational queries here.
    """
    return (
        f"({start_col} IS NULL OR {start_col} = '' OR {start_col} <= ?) "
        f"AND ({end_col} IS NULL OR {end_col} = '' OR {end_col} >= ?)"
    )


def _person_cooccur_neighbors(
    conn: sqlite3.Connection,
    pid: str,
    exclude_others: set[str],
    date_bounds: tuple[str, str] | None,
) -> list[dict]:
    """
    Other persons sharing a source with pid that have no existing
    relationship edge - the person-pair half of TOOLING §4a's "people tied by
    ... co-occurrence" bullet, narrowed to one person's neighborhood.

    This duplicates cooccur.py's person co-occurrence grouping logic
    (TOOLING §690) rather than importing it - tools never import other tools.

    When date_bounds is set, only claim_persons participation is considered:
    each row carries a date via its backing claim, but source_people (a
    source's optional frontmatter `people:` list) has no per-claim date, so
    it's excluded from the date-filtered path rather than silently treated
    as always-in-range.
    """
    persons_by_source: dict[str, set[str]] = {}
    sources_for_pid: set[str] = set()

    if date_bounds:
        rows = conn.execute(
            f'''
            SELECT DISTINCT c.source_id AS source_id, cp.person_id AS person_id
            FROM claims c JOIN claim_persons cp ON cp.claim_id = c.id
            WHERE c.status IN ('accepted', 'needs-review')
              AND {_overlap_clause('c.date_min', 'c.date_max')}
            ''',
            (date_bounds[1], date_bounds[0]),
        ).fetchall()
    else:
        rows = list(conn.execute(
            '''
            SELECT DISTINCT c.source_id AS source_id, cp.person_id AS person_id
            FROM claims c JOIN claim_persons cp ON cp.claim_id = c.id
            WHERE c.status IN ('accepted', 'needs-review')
            '''
        )) + list(conn.execute('SELECT source_id, person_id FROM source_people'))

    for row in rows:
        persons_by_source.setdefault(row['source_id'], set()).add(row['person_id'])
        if row['person_id'] == pid:
            sources_for_pid.add(row['source_id'])

    counts: dict[str, int] = {}
    for sid in sources_for_pid:
        for other in persons_by_source.get(sid, ()):
            if other == pid or other in exclude_others:
                continue
            counts[other] = counts.get(other, 0) + 1

    out = [{'other_id': oid, 'source_count': n} for oid, n in counts.items()]
    out.sort(key=lambda c: (-c['source_count'], c['other_id']))
    return out


def _person_places(
    conn: sqlite3.Connection, pid: str, date_bounds: tuple[str, str] | None
) -> list[dict]:
    """
    Places this person's claims name, ranked by claim frequency
    (TOOLING §4a "places they recur in"). Grouped on structured place_id
    when present, else normalized place_text - same precedence cooccur.py's
    shared-place detector uses, so the two tools agree on what counts as
    "the same place."
    """
    # Same accepted/needs-review gate as _person_cooccur_neighbors and
    # _person_org_hubs use - without it, `suggested`/`rejected` draft
    # placed claims silently get promoted into the person's place ranking.
    sql = '''
        SELECT c.place_id, c.place_text
        FROM claims c JOIN claim_persons cp ON cp.claim_id = c.id
        WHERE cp.person_id = ?
          AND c.status IN ('accepted', 'needs-review')
          AND ((c.place_id IS NOT NULL AND c.place_id != '')
               OR (c.place_text IS NOT NULL AND c.place_text != ''))
    '''
    params: list = [pid]
    if date_bounds:
        sql += ' AND ' + _overlap_clause('c.date_min', 'c.date_max')
        params += [date_bounds[1], date_bounds[0]]

    counts: dict[str, dict] = {}
    for row in conn.execute(sql, params):
        key = (row['place_id'] or '').strip().lower() or normalize_place_text(row['place_text'])
        if not key:
            continue
        entry = counts.setdefault(key, {'label': row['place_text'] or row['place_id'], 'count': 0})
        entry['count'] += 1

    out = list(counts.values())
    out.sort(key=lambda p: (-p['count'], p['label'] or ''))
    return out


def _person_org_hubs(
    conn: sqlite3.Connection, pid: str, date_bounds: tuple[str, str] | None
) -> list[dict]:
    """
    Recurring occupation/military/membership affiliations this person shares
    with at least one other person - TOOLING §4a's "shared entities ...
    ranked by how many others share each" bullet.

    Duplicates cooccur.py's (category, normalized value) grouping for org/
    entity recurrence (occupation and military are direct categories;
    membership rides the subtype of event/note claims), filtered to hubs
    that include pid.

    With a date window, the same overlap predicate the rest of the
    neighborhood uses (`_overlap_clause` on `date_min/date_max`) narrows
    affiliations - otherwise a person's 1880 job would still parade their
    1950 club membership in `fha find --related P-… --date 1880`.
    """
    sql = '''
        SELECT c.type, c.subtype, c.value, cp.person_id
        FROM claims c JOIN claim_persons cp ON cp.claim_id = c.id
        WHERE c.status IN ('accepted', 'needs-review')
          AND (c.negated IS NULL OR c.negated = 0)
          AND (c.type IN ('occupation', 'military')
               OR (c.type IN ('event', 'note') AND LOWER(COALESCE(c.subtype, '')) = 'membership')
               OR (c.type = 'relationship' AND LOWER(COALESCE(c.subtype, '')) = 'member-of'))
    '''
    params: list = []
    if date_bounds:
        sql += ' AND ' + _overlap_clause('c.date_min', 'c.date_max')
        params += [date_bounds[1], date_bounds[0]]
    rows = conn.execute(sql, params).fetchall()

    groups: dict[tuple[str, str], dict] = {}
    for row in rows:
        category = row['type'] if row['type'] in ('occupation', 'military') else 'membership'
        label = (row['value'] or '').strip()
        # occupation/military values follow the documented "role, entity"
        # convention (SPEC §8.4) - split on the FIRST comma so an entity name
        # that itself contains a comma stays intact (same rule as cooccur.py).
        if category in ('occupation', 'military') and ',' in label:
            label = label.split(',', 1)[1].strip()
        normalized = normalize_place_text(label)
        if not normalized:
            continue
        key = (category, normalized)
        group = groups.setdefault(key, {'label': label, 'category': category, 'persons': set()})
        group['persons'].add(row['person_id'])

    out = []
    for group in groups.values():
        if pid in group['persons'] and len(group['persons']) > 1:
            others = sorted(p for p in group['persons'] if p != pid)
            out.append({'label': group['label'], 'category': group['category'], 'others': others})
    out.sort(key=lambda h: (-len(h['others']), h['label']))
    return out


def _person_source_count(
    conn: sqlite3.Connection,
    pid: str,
    date_bounds: tuple[str, str] | None = None,
) -> int:
    """Distinct sources naming pid, via claim_persons (claims) or source_people (frontmatter).

    With a date window, only the claim-backed half is counted (with the same
    `_overlap_clause` the rest of the neighborhood uses) - source_people is a
    frontmatter-level list with no date and can't be filtered, so a person's
    1880 source count would otherwise still include a 1950-only source whose
    frontmatter merely names them.
    """
    if date_bounds is not None:
        rows = conn.execute(
            f'''
            SELECT DISTINCT c.source_id AS sid
            FROM claims c JOIN claim_persons cp ON cp.claim_id = c.id
            WHERE cp.person_id = ?
              AND {_overlap_clause('c.date_min', 'c.date_max')}
            ''',
            (pid, date_bounds[1], date_bounds[0]),
        ).fetchall()
    else:
        rows = conn.execute(
            '''
            SELECT DISTINCT c.source_id AS sid
            FROM claims c JOIN claim_persons cp ON cp.claim_id = c.id
            WHERE cp.person_id = ?
            UNION
            SELECT source_id AS sid FROM source_people WHERE person_id = ?
            ''',
            (pid, pid),
        ).fetchall()
    return len(rows)


def _photo_edtf_overlaps(edtf_str: str | None, date_bounds: tuple[str, str]) -> bool:
    """True if a photo's EDTF overlaps the user's `--date` window.

    Undated photos are treated as unbounded (always included), mirroring the
    `_overlap_clause` convention used for claim/relationship rows: a missing
    EDTF means "no documented date," not "definitely outside the window."
    Unparseable EDTF gets the same benefit of the doubt.
    """
    if not edtf_str:
        return True
    try:
        pmin, pmax = edtf_bounds(edtf_str)
    except Exception:
        return True
    lo, hi = date_bounds
    return pmax >= lo and pmin <= hi


def _print_person_photos(
    pid: str,
    archive_root: Path,
    fha_config: dict,
    date_bounds: tuple[str, str] | None,
) -> None:
    """
    Photos tagged to this person via any resolution confidence
    (pid-keyword/face-tag/name-match - photo_people already records the
    winning method per photo). Mirrors _find_person's photo-count lookup but
    lists the group's primary_path so the photos are actually locatable.

    Gated on `photoindex_status()` so a stale photos.sqlite - e.g. after a
    name-variant change or photo rename/delete - is reported as stale rather
    than silently surfacing old `photo_people`/`photo_groups` rows that may
    point to renamed people or missing files.

    With `date_bounds`, each candidate group is filtered against
    `photo_groups.edtf_resolved` (which already merges variant EDTF agreement)
    so a 1950 photo doesn't appear in `fha find --related P-… --date 1880`.
    """
    status, _ = photoindex_status(archive_root, fha_config)
    if status == 'absent':
        print('  photos: not indexed (run fha photoindex)')
        return
    if status == 'stale':
        print('  photos: photo index is stale - run fha photoindex')
        return
    if status in ('unreadable', 'old-schema'):
        print('  photos: photo index is unreadable; rebuild with fha photoindex')
        return
    photos_db = archive_root / '.cache' / 'photos.sqlite'
    try:
        pconn = sqlite3.connect(str(photos_db))
        try:
            pconn.row_factory = sqlite3.Row
            rows = pconn.execute(
                '''
                SELECT DISTINCT pg.primary_path, pg.edtf_resolved
                FROM photo_people pp
                JOIN photos p ON p.path = pp.path
                JOIN photo_groups pg ON pg.group_id = p.group_id
                WHERE pp.person_ref = ?
                ''',
                (pid,),
            ).fetchall()
        finally:
            pconn.close()
    except Exception:
        print('  photos: not indexed (run fha photoindex)')
        return
    if date_bounds is not None:
        rows = [r for r in rows if _photo_edtf_overlaps(r['edtf_resolved'], date_bounds)]
    if rows:
        print(f'  photos ({len(rows)}):')
        for r in rows[:10]:
            print(f"    {r['primary_path']}")
    else:
        print('  photos: none')


def _related_person(
    pid: str,
    conn: sqlite3.Connection,
    archive_root: Path,
    fha_config: dict,
    date_bounds: tuple[str, str] | None,
) -> int:
    """Print a P-id's neighborhood: relationship edges, co-occurrence, places, shared affiliations, sources, photos."""
    person = conn.execute('SELECT id, name FROM persons WHERE id = ?', (pid,)).fetchone()
    if person is None:
        print(f'{pid}: not found in index.')
        return EXIT_WARNINGS

    print(f"{pid}'s world  [{person['name']}]")
    names = {row['id']: row['name'] for row in conn.execute('SELECT id, name FROM persons')}

    if date_bounds:
        # Filter on the edge's own date_start/date_end, not the originating
        # claim's date_min/date_max: for spouse edges, date_end is backfilled
        # from a later divorce/death claim (TOOLING §relationships) and can
        # extend well past the marriage claim's own narrow date bounds. NULL
        # means open-ended (no divorce/death recorded yet).
        edge_rows = conn.execute(
            f'''
            SELECT r.rel, r.other_id, COUNT(DISTINCT c.source_id) AS source_count
            FROM relationships r JOIN claims c ON c.id = r.claim_id
            WHERE r.person_id = ?
              AND {_overlap_clause('r.date_start', 'r.date_end')}
            GROUP BY r.rel, r.other_id
            ORDER BY r.rel, r.other_id
            ''',
            (pid, date_bounds[1], date_bounds[0]),
        ).fetchall()
    else:
        edge_rows = conn.execute(
            '''
            SELECT r.rel, r.other_id, COUNT(DISTINCT c.source_id) AS source_count
            FROM relationships r LEFT JOIN claims c ON c.id = r.claim_id
            WHERE r.person_id = ?
            GROUP BY r.rel, r.other_id
            ORDER BY r.rel, r.other_id
            ''',
            (pid,),
        ).fetchall()

    if edge_rows:
        print('  relationships:')
        for r in edge_rows:
            other_name = names.get(r['other_id'], r['other_id'])
            print(f"    {r['rel']}: {other_name} [{r['other_id']}] - {r['source_count']} source(s)")
    else:
        print('  relationships: none')

    # Existing edges exclude a candidate from co-occurrence regardless of the
    # date window - a confirmed relationship doesn't stop being confirmed
    # just because this particular time slice predates the backing claim.
    all_edge_others = {
        row['other_id'] for row in
        conn.execute('SELECT other_id FROM relationships WHERE person_id = ?', (pid,))
    }
    cooccur_rows = _person_cooccur_neighbors(conn, pid, all_edge_others, date_bounds)
    if cooccur_rows:
        print('  co-occurring persons (no relationship edge yet):')
        for c in cooccur_rows[:10]:
            other_name = names.get(c['other_id'], c['other_id'])
            print(f"    {other_name} [{c['other_id']}] - {c['source_count']} shared source(s)")
    else:
        print('  co-occurring persons: none')

    place_rows = _person_places(conn, pid, date_bounds)
    if place_rows:
        print('  places (by claim frequency):')
        for p in place_rows[:10]:
            print(f"    {p['label']} - {p['count']} claim(s)")
    else:
        print('  places: none')

    hub_rows = _person_org_hubs(conn, pid, date_bounds)
    if hub_rows:
        print('  shared affiliations:')
        for h in hub_rows:
            others = ', '.join(f"{names.get(o, o)} [{o}]" for o in h['others'][:5])
            print(f"    {h['label']} [{h['category']}] - also: {others}")
    else:
        print('  shared affiliations: none')

    print(f'  sources: {_person_source_count(conn, pid, date_bounds)}')
    _print_person_photos(pid, archive_root, fha_config, date_bounds)

    return EXIT_CLEAN


def _print_place_photos(
    place: sqlite3.Row,
    archive_root: Path,
    fha_config: dict,
    date_bounds: tuple[str, str] | None,
) -> None:
    """
    Photos geotagged within ~0.002 degrees of the place's coords
    (roughly 200m at mid-latitudes - TOOLING §4a "photos geotagged within
    it"). Coordinate-only proximity; a place with no within: children and no
    coords simply has no photo neighborhood to report.

    Same `photoindex_status()` gating as `_print_person_photos` so stale GPS
    rows (after photos move or get re-geotagged) aren't surfaced silently.

    With `date_bounds`, each photo is filtered against its own `photos.edtf`
    via `_photo_edtf_overlaps`, so a 1950 photo near the place doesn't appear
    in a 1880 slice.
    """
    if place['lat'] is None or place['lon'] is None:
        print('  photos: place has no coordinates')
        return
    status, _ = photoindex_status(archive_root, fha_config)
    if status == 'absent':
        print('  photos: not indexed (run fha photoindex)')
        return
    if status == 'stale':
        print('  photos: photo index is stale - run fha photoindex')
        return
    if status in ('unreadable', 'old-schema'):
        print('  photos: photo index is unreadable; rebuild with fha photoindex')
        return
    photos_db = archive_root / '.cache' / 'photos.sqlite'
    try:
        pconn = sqlite3.connect(str(photos_db))
        try:
            pconn.row_factory = sqlite3.Row
            rows = pconn.execute(
                '''
                SELECT DISTINCT pg.primary_path,
                       COALESCE(pg.edtf_resolved, p.edtf) AS edtf
                FROM photos p JOIN photo_groups pg ON pg.group_id = p.group_id
                WHERE p.gps_lat IS NOT NULL AND p.gps_lon IS NOT NULL
                  AND ABS(p.gps_lat - ?) <= 0.002 AND ABS(p.gps_lon - ?) <= 0.002
                ''',
                (place['lat'], place['lon']),
            ).fetchall()
        finally:
            pconn.close()
    except Exception:
        print('  photos: not indexed (run fha photoindex)')
        return
    if date_bounds is not None:
        rows = [r for r in rows if _photo_edtf_overlaps(r['edtf'], date_bounds)]
    if rows:
        print(f'  photos near coords ({len(rows)}):')
        for r in rows[:10]:
            print(f"    {r['primary_path']}")
    else:
        print('  photos near coords: none')


def _related_place(
    lid: str,
    conn: sqlite3.Connection,
    archive_root: Path,
    fha_config: dict,
    date_bounds: tuple[str, str] | None,
) -> int:
    """Print an L-id's neighborhood: claims naming it, people ranked by frequency, sources, micro-places, photos."""
    place = conn.execute(
        'SELECT id, name, hierarchy, lat, lon, within FROM places WHERE id = ?', (lid,)
    ).fetchone()
    if place is None:
        print(f'{lid}: not found in index.')
        return EXIT_WARNINGS

    print(f"{lid}'s world  [{place['name']}]")

    sql = '''
        SELECT c.id, c.type, c.value, c.status, c.date_edtf, c.source_id,
               GROUP_CONCAT(DISTINCT cp.person_id) AS persons
        FROM claims c LEFT JOIN claim_persons cp ON cp.claim_id = c.id
        WHERE c.place_id = ?
    '''
    params: list = [lid]
    if date_bounds:
        sql += ' AND ' + _overlap_clause('c.date_min', 'c.date_max')
        params += [date_bounds[1], date_bounds[0]]
    sql += ' GROUP BY c.id ORDER BY c.date_min'
    claim_rows = conn.execute(sql, params).fetchall()

    if claim_rows:
        print(f'  claims ({len(claim_rows)}):')
        for c in claim_rows:
            date_str = f"  {c['date_edtf']}" if c['date_edtf'] else ''
            value_preview = c['value'][:60] + ('…' if len(c['value']) > 60 else '')
            print(f"    {c['id']}  {c['type']}{date_str}: {value_preview}  [{c['status']}]")
    else:
        print('  claims: none')

    person_freq: dict[str, int] = {}
    source_ids: set[str] = set()
    for c in claim_rows:
        source_ids.add(c['source_id'])
        if c['persons']:
            for p in c['persons'].split(','):
                person_freq[p] = person_freq.get(p, 0) + 1

    if person_freq:
        names = {row['id']: row['name'] for row in conn.execute('SELECT id, name FROM persons')}
        print('  people (by frequency):')
        for pid, n in sorted(person_freq.items(), key=lambda kv: (-kv[1], kv[0]))[:10]:
            print(f"    {names.get(pid, pid)} [{pid}] - {n} claim(s)")
    else:
        print('  people: none')

    print(f'  sources: {len(source_ids)}')

    micro = conn.execute('SELECT id, name FROM places WHERE within = ?', (lid,)).fetchall()
    if micro:
        print('  micro-places:')
        for m in micro:
            print(f"    {m['name']} [{m['id']}]")
    else:
        print('  micro-places: none')

    _print_place_photos(place, archive_root, fha_config, date_bounds)

    return EXIT_CLEAN


def _related_source(
    sid: str,
    conn: sqlite3.Connection,
    date_bounds: tuple[str, str] | None,
) -> int:
    """Print an S-id's neighborhood: claims by status, persons, places, linked sources, sibling sources."""
    source = conn.execute(
        'SELECT id, title, repository FROM sources WHERE id = ?', (sid,)
    ).fetchone()
    if source is None:
        print(f'{sid}: not found in index.')
        return EXIT_WARNINGS

    print(f"{sid}'s world  [{source['title']}]")

    sql = 'SELECT id, status FROM claims WHERE source_id = ?'
    params: list = [sid]
    if date_bounds:
        sql += ' AND ' + _overlap_clause('date_min', 'date_max')
        params += [date_bounds[1], date_bounds[0]]
    claim_rows = conn.execute(sql, params).fetchall()

    by_status: dict[str, int] = {}
    for c in claim_rows:
        by_status[c['status']] = by_status.get(c['status'], 0) + 1
    print('  claims: ' + (', '.join(f'{k}: {v}' for k, v in sorted(by_status.items())) or 'none'))

    claim_ids = [c['id'] for c in claim_rows]
    persons: set[str] = set()
    places: set[str] = set()
    if claim_ids:
        placeholders = ','.join('?' * len(claim_ids))
        for row in conn.execute(
            f'SELECT DISTINCT person_id FROM claim_persons WHERE claim_id IN ({placeholders})', claim_ids
        ):
            persons.add(row['person_id'])
        for row in conn.execute(
            f'SELECT place_id, place_text FROM claims WHERE id IN ({placeholders})', claim_ids
        ):
            if row['place_id']:
                places.add(row['place_id'])
            elif row['place_text']:
                places.add(row['place_text'])
    # source_people is a frontmatter-level list with no date - including it
    # in a dated slice would surface persons whose only connection to this
    # source is an out-of-window claim (or no claim at all). Skip it when
    # date_bounds is set; otherwise keep the cross-listing as before.
    if date_bounds is None:
        for row in conn.execute('SELECT person_id FROM source_people WHERE source_id = ?', (sid,)):
            persons.add(row['person_id'])

    names = {row['id']: row['name'] for row in conn.execute('SELECT id, name FROM persons')}
    if persons:
        print('  persons:')
        for p in sorted(persons):
            print(f"    {names.get(p, p)} [{p}]")
    else:
        print('  persons: none')

    if places:
        print('  places:')
        for p in sorted(places):
            print(f'    {p}')
    else:
        print('  places: none')

    # Corroborating/contradicting sources via claim_links - a source's claims
    # may link out (claim_id) or be linked to (target_id); both directions
    # have to be checked, with the inverse-rel label for the incoming side
    # (same convention as _find_claim's links/incoming split).
    related_sources: dict[str, set[str]] = {}
    if claim_ids:
        placeholders = ','.join('?' * len(claim_ids))
        for row in conn.execute(
            f'''
            SELECT cl.rel, c2.source_id AS other_source
            FROM claim_links cl JOIN claims c2 ON c2.id = cl.target_id
            WHERE cl.claim_id IN ({placeholders})
            ''', claim_ids
        ):
            if row['other_source'] != sid:
                related_sources.setdefault(row['rel'], set()).add(row['other_source'])
        for row in conn.execute(
            f'''
            SELECT cl.rel, c2.source_id AS other_source
            FROM claim_links cl JOIN claims c2 ON c2.id = cl.claim_id
            WHERE cl.target_id IN ({placeholders})
            ''', claim_ids
        ):
            if row['other_source'] != sid:
                label = _REL_INVERSE.get(row['rel'], f"{row['rel']}-by")
                related_sources.setdefault(label, set()).add(row['other_source'])

    if related_sources:
        print('  linked sources:')
        for rel, sids in sorted(related_sources.items()):
            for other in sorted(sids):
                print(f'    {rel}: {other}')
    else:
        print('  linked sources: none')

    # Sibling sources: share a named person, or the same repository.
    siblings: set[str] = set()
    if persons:
        placeholders = ','.join('?' * len(persons))
        person_list = list(persons)
        if date_bounds is not None:
            # Dated slice: only claim-backed sibling links, and only those
            # whose claim overlaps the window. source_people has no date to
            # filter on, so dropping it here is consistent with how this
            # block already drops it from the persons set above.
            for row in conn.execute(
                f'''
                SELECT c.source_id FROM claims c JOIN claim_persons cp ON cp.claim_id = c.id
                WHERE cp.person_id IN ({placeholders})
                  AND {_overlap_clause('c.date_min', 'c.date_max')}
                ''', person_list + [date_bounds[1], date_bounds[0]]
            ):
                if row['source_id'] != sid:
                    siblings.add(row['source_id'])
        else:
            for row in conn.execute(
                f'''
                SELECT source_id FROM source_people WHERE person_id IN ({placeholders})
                UNION
                SELECT c.source_id FROM claims c JOIN claim_persons cp ON cp.claim_id = c.id
                WHERE cp.person_id IN ({placeholders})
                ''', person_list + person_list
            ):
                if row['source_id'] != sid:
                    siblings.add(row['source_id'])
    if source['repository']:
        for row in conn.execute(
            'SELECT id FROM sources WHERE repository = ? AND id != ?', (source['repository'], sid)
        ):
            siblings.add(row['id'])

    if siblings:
        print(f'  sibling sources ({len(siblings)}):')
        for s in sorted(siblings)[:15]:
            print(f'    {s}')
    else:
        print('  sibling sources: none')

    return EXIT_CLEAN


def _related_claim(cid: str, conn: sqlite3.Connection) -> int:
    """
    Print a C-id's neighborhood: source, persons, place, linked claims,
    sibling claims (same person + same type - the cluster a researcher would
    weigh together, per TOOLING §4a).

    Unlike the person/place/source neighborhoods, a single claim has no
    meaningful date-bounds narrowing - its own date_edtf already pins it to
    one point - so --date is not accepted here.
    """
    claim = conn.execute(
        'SELECT id, source_id, type, value, status, place_id, place_text, date_edtf '
        'FROM claims WHERE id = ?', (cid,)
    ).fetchone()
    if claim is None:
        print(f'{cid}: not found in index.')
        return EXIT_WARNINGS

    print(f"{cid}'s neighborhood")
    print(f"  source: {claim['source_id']}")

    persons = [row['person_id'] for row in conn.execute(
        'SELECT person_id FROM claim_persons WHERE claim_id = ?', (cid,)
    )]
    names = {row['id']: row['name'] for row in conn.execute('SELECT id, name FROM persons')}
    if persons:
        print('  persons:')
        for p in persons:
            print(f"    {names.get(p, p)} [{p}]")
    else:
        print('  persons: none')

    place_label = claim['place_text'] or claim['place_id']
    print(f"  place: {place_label or '(none)'}")

    links = conn.execute('SELECT rel, target_id FROM claim_links WHERE claim_id = ?', (cid,)).fetchall()
    incoming = conn.execute('SELECT rel, claim_id FROM claim_links WHERE target_id = ?', (cid,)).fetchall()
    if links or incoming:
        print('  linked claims:')
        for lnk in links:
            print(f"    {lnk['rel']}: {lnk['target_id']}")
        for lnk in incoming:
            label = _REL_INVERSE.get(lnk['rel'], f"{lnk['rel']}-by")
            print(f"    {label}: {lnk['claim_id']}")
    else:
        print('  linked claims: none')

    siblings: set[str] = set()
    if persons:
        placeholders = ','.join('?' * len(persons))
        for row in conn.execute(
            f'''
            SELECT DISTINCT c.id FROM claims c
            JOIN claim_persons cp ON cp.claim_id = c.id
            WHERE c.type = ? AND c.id != ? AND cp.person_id IN ({placeholders})
            ''', [claim['type'], cid] + persons
        ):
            siblings.add(row['id'])

    if siblings:
        print(f'  sibling claims (same person + type) ({len(siblings)}):')
        for s in sorted(siblings):
            print(f'    {s}')
    else:
        print('  sibling claims: none')

    return EXIT_CLEAN


def _related_hypothesis(hid: str, conn: sqlite3.Connection) -> int:
    """
    Print an H-id's neighborhood: the person it concerns, claims referencing
    it (claims.hypothesis = hid), and the verifying claim if one is set.

    The `hypotheses` table itself is never populated by the index builder
    (research-file hypothesis entries aren't parsed into rows - see
    _find_hypothesis's matching note above) - only claims.hypothesis is. So
    a missing hypotheses row is the expected case, not a failure: this falls
    back to deriving the neighborhood entirely from claims.hypothesis and the
    persons named on those claims.
    """
    row = conn.execute(
        'SELECT id, person_id, status, verified_claim FROM hypotheses WHERE id = ?', (hid,)
    ).fetchone()
    claim_rows = conn.execute(
        'SELECT id, source_id, type, value, status FROM claims WHERE hypothesis = ?', (hid,)
    ).fetchall()

    print(f"{hid}'s neighborhood")
    names = {r['id']: r['name'] for r in conn.execute('SELECT id, name FROM persons')}

    if row is None:
        print('  (no hypotheses-table row - hypothesis indexing is deferred; '
              'deriving the neighborhood from claims.hypothesis instead)')
        person_ids: set[str] = set()
        for c in claim_rows:
            for p in conn.execute('SELECT person_id FROM claim_persons WHERE claim_id = ?', (c['id'],)):
                person_ids.add(p['person_id'])
        if person_ids:
            print('  person(s) concerned:')
            for p in sorted(person_ids):
                print(f"    {names.get(p, p)} [{p}]")
        else:
            print('  person(s) concerned: none found')
    else:
        if row['person_id']:
            print(f"  person: {names.get(row['person_id'], row['person_id'])} [{row['person_id']}]")
        print(f"  status: {row['status'] or '(none)'}")
        if row['verified_claim']:
            vrow = conn.execute(
                'SELECT id, source_id, type, value FROM claims WHERE id = ?', (row['verified_claim'],)
            ).fetchone()
            if vrow:
                print(f"  verifying claim: {vrow['id']}  {vrow['type']}: {vrow['value']}  [{vrow['source_id']}]")
            else:
                print(f"  verifying claim: {row['verified_claim']} (not found)")

    if claim_rows:
        print(f'  claims referencing {hid} ({len(claim_rows)}):')
        for c in claim_rows:
            value_preview = c['value'][:60] + ('…' if len(c['value']) > 60 else '')
            print(f"    {c['id']}  {c['type']}: {value_preview}  [{c['status']}]  ({c['source_id']})")
    else:
        print('  claims referencing this hypothesis: none')

    if row is None and not claim_rows:
        return EXIT_WARNINGS
    return EXIT_CLEAN


def _related_date(date_bounds: tuple[str, str], date_str: str, conn: sqlite3.Connection) -> int:
    """
    Print the standalone time-slice neighborhood: every accepted/needs-review
    claim whose bounds overlap date_bounds, and the people/sources/places
    behind them (TOOLING §4a's `--related --date` bullet).

    Photos are omitted here (unlike the P-id/L-id neighborhoods) - photo
    EDTFs live in a separate database with no person/place join key back to
    this query's claim set, so a meaningful photo count would require a
    second, unrelated date-overlap pass; TOOLING describes this as a future
    refinement, not a same-claim join.
    """
    rows = conn.execute(
        f'''
        SELECT id, source_id, place_id, place_text
        FROM claims
        WHERE {_overlap_clause('date_min', 'date_max')}
          AND status IN ('accepted', 'needs-review')
        ''',
        (date_bounds[1], date_bounds[0]),
    ).fetchall()

    claim_ids = [r['id'] for r in rows]
    persons: set[str] = set()
    if claim_ids:
        placeholders = ','.join('?' * len(claim_ids))
        for row in conn.execute(
            f'SELECT DISTINCT person_id FROM claim_persons WHERE claim_id IN ({placeholders})', claim_ids
        ):
            persons.add(row['person_id'])

    sources: set[str] = set()
    places: set[str] = set()
    for r in rows:
        sources.add(r['source_id'])
        if r['place_id']:
            places.add(r['place_id'])
        elif r['place_text']:
            places.add(r['place_text'])

    print(f'Active in {date_str}: {len(rows)} claims, {len(persons)} people, {len(sources)} sources')

    names = {row['id']: row['name'] for row in conn.execute('SELECT id, name FROM persons')}
    if persons:
        print('  people:')
        for p in sorted(persons):
            print(f"    {names.get(p, p)} [{p}]")
    if sources:
        print('  sources:')
        for s in sorted(sources):
            print(f'    {s}')
    if places:
        print('  places:')
        for p in sorted(places):
            print(f'    {p}')

    return EXIT_CLEAN


def _related_dispatch(
    related_id: str | None,
    date_filter: str | None,
    archive_root: Path,
    fha_config: dict,
) -> int:
    """
    Top-level --related dispatcher; prints the neighborhood report, returns int.

    related_id is None for the standalone `--related --date EDTF` form;
    date_filter is None when narrowing isn't requested. At least one of the
    two must be set (the CLI layer guarantees this - see _run_find).

    Printing stays here (rather than in a renderer) because the neighborhood
    report is assembled across many small `_related_*`/`_print_*` helpers that
    each print as they go; `run_related` wraps the resulting exit code into the
    Result contract without disturbing that byte-for-byte output.
    """
    if related_id is None and date_filter is None:
        print('ERROR: --related requires an ID, --date EDTF, or both.', file=sys.stderr)
        return EXIT_FAILURE

    date_bounds = None
    if date_filter is not None:
        date_filter = normalize_date(date_filter) or date_filter
        if not is_valid_edtf(date_filter):
            print(f'ERROR: {format_edtf_error(date_filter, field="--date")}', file=sys.stderr)
            return EXIT_FAILURE
        date_bounds = edtf_bounds(date_filter)

    id_norm = None
    if related_id is not None:
        id_norm = normalize_id(related_id)
        if not is_valid_id(id_norm):
            print(f'ERROR: {related_id!r} is not a valid archive ID.', file=sys.stderr)
            return EXIT_FAILURE

    conn = open_index_db(archive_root, _RELATED_REQUIRED_TABLES)
    if conn is None:
        return EXIT_FAILURE

    try:
        try:
            if id_norm is None:
                return _related_date(date_bounds, date_filter, conn)

            id_type = id_type_of(id_norm)
            if id_type == 'P':
                return _related_person(id_norm, conn, archive_root, fha_config, date_bounds)
            elif id_type == 'L':
                return _related_place(id_norm, conn, archive_root, fha_config, date_bounds)
            elif id_type == 'S':
                return _related_source(id_norm, conn, date_bounds)
            elif id_type == 'C':
                return _related_claim(id_norm, conn)
            elif id_type == 'H':
                return _related_hypothesis(id_norm, conn)
            else:
                print(f'{id_norm}: unknown ID type prefix.', file=sys.stderr)
                return EXIT_FAILURE
        except sqlite3.OperationalError:
            # open_index_db only probes that the required tables exist, not
            # that every column a related query touches is present. An older
            # cache (e.g. with `relationships` but no `date_start`) would
            # otherwise traceback out of dispatch - same incompatible-schema
            # failure mode xref/cooccur catch with the documented rebuild
            # message and exit 3.
            print(
                'ERROR: .cache/index.sqlite is unreadable or has an incompatible schema. '
                'Run `fha index` to rebuild.',
                file=sys.stderr,
            )
            return EXIT_FAILURE
    finally:
        conn.close()


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
    if conn is None and fresh:
        # _index_is_fresh only compares mtimes; a corrupt/schema-less index can
        # still look fresh by that measure. Warn here so the silent scan-only
        # fallback below isn't mistaken for a full structured search.
        print('WARNING: index not readable, results may be incomplete')
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
    `find` useful after generated views change their mtimes.  An absent or
    unreadable index falls back to a tree scan outright; a stale index that
    has no row for the ID also falls back to a scan, since the record may
    simply have been added since the last `fha index` run.
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
        if fresh:
            # _index_is_fresh only compares mtimes; a corrupt/schema-less index
            # can still look fresh by that measure. Warn here so the silent
            # scan-only fallback below isn't mistaken for an index-backed result.
            print('WARNING: index not readable, results may be incomplete')
        return _find_by_scan(id_str, archive_root)

    try:
        if id_type == 'P':
            result = _find_person(id_str, conn, archive_root, fha_config)
        elif id_type == 'S':
            result = _find_source(id_str, conn, archive_root, fha_config)
        elif id_type == 'C':
            result = _find_claim(id_str, conn, archive_root, fha_config)
        elif id_type == 'L':
            result = _find_place(id_str, conn, archive_root)
        elif id_type == 'H':
            return _find_hypothesis(id_str, conn, archive_root)
        else:
            print(f'{id_str}: unknown ID type prefix.', file=sys.stderr)
            return EXIT_FAILURE
    finally:
        conn.close()

    # A stale index may simply be missing a record added since the last
    # `fha index` run - rescan the tree before reporting "not found".
    if result == EXIT_WARNINGS and not fresh:
        return _find_by_scan(id_str, archive_root)
    return result


def _as_find_result(exit_code: int) -> Result:
    """Wrap a find/related exit code into the structured Result contract.

    find is a read tool: its per-ID and neighborhood reports are printed by the
    finder/`_related_*` helpers as they run, so that human report is the tool's
    primary surface.  The Result therefore carries the outcome (exit_code + ok)
    rather than re-deriving the printed rows; `Result == int` (see _lib.py) keeps
    every caller and test that compared the old int return against EXIT_*
    working unchanged.
    """
    return Result(ok=(exit_code == EXIT_CLEAN), exit_code=exit_code)


def run_related(
    related_id: str | None,
    date_filter: str | None,
    archive_root: Path,
    fha_config: dict,
) -> Result:
    """Run the --related neighborhood report and return a Result (prints inline)."""
    return _as_find_result(
        _related_dispatch(related_id, date_filter, archive_root, fha_config)
    )


def run_find(
    id_or_text: str | None,
    archive_root: Path,
    fha_config: dict,
    text_mode: bool = False,
    related_id: str | None = None,
    related_requested: bool = False,
    date_filter: str | None = None,
) -> Result:
    """
    Top-level dispatcher; prints the report and returns a Result:
      - --related <ID> [--date E]  → run_related (M4.3, Design decision D4)
      - --related --date <EDTF>    → run_related, standalone time-slice (related_id None)
      - --text "phrase"            → full-text search
      - bare valid ID               → find_by_id
      - bare non-ID string          → treated as text search

    related_requested distinguishes "--related typed with no ID" (route to
    run_related's standalone date form) from "--related not typed at all"
    (related_id is None in both cases, so the flag alone isn't enough).

    The finder helpers print as they go (find's human report is its surface), so
    this returns a Result wrapping their exit code rather than print-free data -
    `_run_find` renders nothing further and just returns `result.exit_code`.
    """
    if related_requested or date_filter is not None:
        return run_related(related_id, date_filter, archive_root, fha_config)

    # Text search path
    if text_mode:
        return _as_find_result(_text_search(id_or_text or '', archive_root, fha_config))

    # Bare argument - distinguish ID from text
    if not id_or_text:
        print('ERROR: provide an ID or --text "phrase"', file=sys.stderr)
        return _as_find_result(EXIT_FAILURE)

    id_norm = normalize_id(id_or_text)
    if is_valid_id(id_norm):
        return _as_find_result(find_by_id(id_norm, archive_root, fha_config))

    # Doesn't look like an ID - treat as text search
    return _as_find_result(_text_search(id_or_text, archive_root, fha_config))


# ── CLI ───────────────────────────────────────────────────────────────────────

# User-facing --help text (the module docstring stays developer-facing).
_CLI_DESCRIPTION = """\
Locate anything in the archive, or search its full text.

  fha find <ID>              Everything about one ID (person, source, place...)
  fha find --text "phrase"   Full-text search across records, notes, captions
  fha find --related <ID>    Everything connected to an ID (add --date for a time slice)

This is your search box. For plain word search, `fha search <words>` is the same
as `fha find --text`."""


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register 'find' onto the main fha parser."""
    p = subparsers.add_parser(
        'find',
        help='Locate any ID, or full-text search across records and notes',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root')

    # Mutually exclusive modes
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        '--text', metavar='PHRASE',
        help='Full-text search across records, notes, transcripts',
    )
    mode.add_argument(
        '--related', nargs='?', default=_NO_RELATED, const=None, metavar='ID',
        help='Neighborhood of an ID - people/places/sources adjacent. '
             'Combine with --date for a time slice, or pass --related --date EDTF alone '
             'for the standalone time-slice neighborhood.',
    )

    p.add_argument(
        '--date', metavar='EDTF',
        help='Time-slice filter. With --related <ID>, narrows that neighborhood to the '
             'given EDTF range; with --related and no ID, reports everything active in that range.',
    )

    p.add_argument(
        'query',
        nargs='?',
        metavar='ID_OR_TEXT',
        help='Archive ID (P-/S-/C-/L-/H-) or text to search',
    )
    p.set_defaults(func=_run_find)

    # `fha search <words>` - the plainest verb for full-text search, the one a
    # human who never read the docs will reach for first (persona B2). Same
    # engine as `fha find --text`; a separate subparser (not an argparse alias)
    # because the argument shape differs - a bare positional phrase joined with
    # spaces, so `fha search rose hartley` works unquoted.
    s = subparsers.add_parser(
        'search',
        help='Search everything for a word or phrase (same as `fha find --text`)',
        description='Search everything for a word or phrase - records, notes, '
                    'transcripts, photo captions.\n\n'
                    'Examples:\n'
                    '  fha search rose hartley\n'
                    '  fha search "1880 census"\n\n'
                    'Same as `fha find --text "..."`.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    s.add_argument('--root', metavar='PATH', help='Archive root')
    s.add_argument(
        'phrase', nargs='+', metavar='WORD',
        help='Word or phrase to search for (joined with spaces if several words)',
    )
    s.set_defaults(func=_run_search)


def _run_find(args: argparse.Namespace) -> int:
    """argparse → run_find bridge; returns the plain int exit code.

    Root resolution (including the refusal of a typo'd --root without
    fha.yaml, which once made find scan an arbitrary folder and report a
    false "not found in archive tree") lives in `_lib.resolve_root_arg`,
    the shared chokepoint. The `fha id check` alias resolves its root in
    fha.py through the same helper, not here.
    """
    archive_root = resolve_root_arg(args, command='fha find')
    if archive_root is None:
        return EXIT_FAILURE

    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE

    text_query = getattr(args, 'text', None)
    related = getattr(args, 'related', _NO_RELATED)
    query = getattr(args, 'query', None)
    date = getattr(args, 'date', None)

    related_requested = related is not _NO_RELATED
    related_id = related if (related_requested and related) else None

    # --date only has defined behavior alongside --related (TOOLING §4a D4).
    # Catch it before the --text branch - otherwise `fha find --text "X" --date Y`
    # silently runs an unfiltered text search and drops the date.
    if date is not None and not related_requested:
        print('ERROR: --date requires --related.', file=sys.stderr)
        return EXIT_FAILURE

    # run_find returns a Result; this CLI bridge renders nothing further (the
    # finder helpers already printed) and hands fha.py the plain int exit code.
    if text_query is not None:
        return run_find(text_query, archive_root, fha_config, text_mode=True).exit_code
    elif related_requested:
        # `fha find --related --date 1900 P-…` parses as --related-with-no-value
        # + --date 1900 + positional query 'P-…'. Without this rescue the
        # positional silently routes to the standalone date-slice branch and
        # the user's P-id is dropped on the floor. Treat the leftover positional
        # as the related ID - that's almost certainly what they meant.
        if related_id is None and query:
            related_id = query
            query = None
        return run_find(
            None, archive_root, fha_config,
            related_id=related_id, related_requested=True, date_filter=date,
        ).exit_code
    elif query:
        return run_find(query, archive_root, fha_config).exit_code
    else:
        print('Usage: fha find <ID>  |  fha find --text "phrase"  |  fha find --related <ID> [--date EDTF]',
              file=sys.stderr)
        return EXIT_FAILURE


def _run_search(args: argparse.Namespace) -> int:
    """argparse → run_find bridge for `fha search <words>` (= `fha find --text`).

    The positional phrase arrives as a list of words (nargs='+'); joining with
    spaces lets `fha search rose hartley` work unquoted while `fha search "1880
    census"` still passes a single token. Root resolution goes through the same
    shared chokepoint as `fha find`.
    """
    archive_root = resolve_root_arg(args, command='fha search')
    if archive_root is None:
        return EXIT_FAILURE

    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE

    phrase = ' '.join(getattr(args, 'phrase', []) or [])
    return run_find(phrase, archive_root, fha_config, text_mode=True).exit_code


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha find',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--root', metavar='PATH', help='Archive root')

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--text', metavar='PHRASE')
    mode.add_argument('--related', nargs='?', default=_NO_RELATED, const=None, metavar='ID')

    parser.add_argument(
        '--date', metavar='EDTF',
        help='Time-slice filter for --related (alone, or combined with an ID).',
    )

    parser.add_argument('query', nargs='?', metavar='ID_OR_TEXT')
    args = parser.parse_args(argv)
    return _run_find(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
