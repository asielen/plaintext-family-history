"""
_lib.py — shared library for all fha tools.

This is the foundation every other tool builds on.  Tools never import each
other — _lib.py is the only shared dependency (TOOLING §15 build rule).

What lives here:
  - ID grammar and validation  (Crockford Base32, SPEC §10)
  - EDTF date parsing and bounds computation  (TOOLING §1)
  - Record file parsing  (frontmatter + fenced claims block + body)
  - Path and alias resolution  (fha.yaml roots mapping)
  - Filename grammar parsing  (person and source naming conventions, SPEC §13)
  - Shared constants: claim types, source types, COMPANION_KINDS, significance
  - The Finding class and exit-code constants shared by lint and other tools
  - The Result contract (see below) every tool's run_* function returns

THE STRUCTURED-RESULT CONTRACT (the rule every `run_*` follows)
--------------------------------------------------------------
Every operation a tool performs is split in two:

  - `run_*` **computes** and **returns a `Result`** — a small, JSON-serializable
    record of what happened.  It does NOT print human-facing report text and does
    NOT call `sys.exit`.  (File side effects and interactive prompts are out of
    scope for this rule: a tool that must write `report_2026.md` or ask the human
    a yes/no question still does so inside `run_*`.  The rule governs return
    values and human-text *printing*, not side effects.)
  - `_cmd_*` is the **only** layer that renders a `Result` to stdout/stderr and
    returns the process exit code.

A `Result` carries:
  - `ok`        — did the operation succeed (no error-level messages)?
  - `exit_code` — the process exit code the CLI should return (EXIT_* constants).
  - `data`      — the structured payload: whatever a consumer would want as data
                  (matched records, per-check rows, counts, a rendered string …).
  - `messages`  — human-facing lines, each a `Message{level, text, next_step,
                  code, path}`.  A lint `Finding` folds into one of these:
                  severity → level, its E/W code → code, the file → path.
  - `changed`   — paths this operation created, wrote, renamed, or embedded into
                  (empty under --dry-run).

`lint` is the reference implementation: `run_lint` returns a `Result`; `_cmd_lint`
renders the existing human text and `--json` payload from it (TOOLING §3).
"""

from __future__ import annotations

import calendar
import dataclasses
import datetime
import itertools
import os
import re
import secrets
import sqlite3
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - exercised by fha.py import-path tests
    yaml = None  # type: ignore[assignment]

# ── CODE MAP ──────────────────────────────────────────────────────────────────
#
#  Constants and patterns
#    CROCKFORD_ALPHA           — the 32-char ID alphabet (i l o u omitted)
#    ID_RE, TOKEN_RE           — bare ID and [ID] token patterns (SPEC §10)
#    FRONT_RE, CLAIMS_RE       — frontmatter and fenced claims block patterns
#    SIGNIFICANCE              — claim type → 'vital'/'substantive'/'incidental'
#    CLAIM_TYPES, VITAL_TYPES  — frozensets derived from SIGNIFICANCE
#    SOURCE_TYPES              — controlled vocabulary for source_type field
#    PHOTO_EXTENSIONS          — recognised photo/scan file extensions (photoindex + process)
#    COMPANION_KINDS           — generated file kinds that share a P-id with their profile
#
#  Archive configuration
#    find_archive_root         — walk up from CWD to find fha.yaml
#    archive_root_missing_message — one plain recovery message for missing roots
#    resolve_root_arg          — CLI --root flag, else find_archive_root(), with the
#                                 shared "cannot find archive root" error message
#    load_fha_yaml             — parse fha.yaml into a dict
#    format_*_error            — shared teaching messages for CLI refusals
#    get_roots                 — extract roots mapping from config
#    resolve_path              — alias path ('photos/…') → absolute Path via fha.yaml
#    path_to_alias             — absolute Path → alias path ('photos/…'), the inverse
#
#  Index database access
#    db_mtime                  — mtime of a cache db file, or None if absent/unreadable
#    probe_sqlite              — does this db open and run this one probe query?
#    open_index_db             — open .cache/index.sqlite with the freshness check +
#                                 required-table probe every index-reading tool needs
#    photoindex_status         — classify .cache/photos.sqlite freshness for find/doctor
#
#  Record parsing
#    _coerce_yaml              — normalise YAML scalar types for consistent comparisons
#    read_record               — parse frontmatter + claims + body from a .md file
#    parse_filename            — decompose filename into {id_str, kind, is_companion}
#    ParsedName, parse_media_filename — decompose an unprocessed photo/scan filename
#                                 into base_id + variant/part-kind/page/crop (TOOLING §6/§9)
#
#  EDTF handling
#    is_valid_edtf             — validate an EDTF string against this project's subset
#    normalize_date            — loose human date ("circa 1870", "1870s") → canonical EDTF
#    edtf_bounds               — compute (date_min, date_max) ISO strings
#    _pad_date, _last_day      — internal date-padding helpers
#
#  ID utilities
#    mint_ids                  — mint collision-checked Crockford IDs
#    normalize_id              — lowercase for consistent set/dict keying
#    is_valid_id               — syntactic validity check
#    id_type_of                — extract P/S/C/L/H type prefix
#    fmt_id_display            — uppercase the type prefix for display (p-xxx → P-xxx)
#    scan_ids_in_tree          — full-tree scan used by id mint for collision checking
#
#  Filename / path helpers
#    is_fixture_path           — path under example-archive/ or tests/fixtures/?
#    extract_token_ids         — all [ID] tokens from a text block
#    extract_bare_ids          — all bare IDs from a text block
#    normalize_place_text      — lowercase/collapse-whitespace key for comparing
#                                 free-text place names without a shared place_id
#
#  Archive freshness
#    newest_record_mtime       — max mtime of sources/people/notes .md + places.yaml
#    newest_source_record_mtime — max mtime of source .md records only
#    newest_person_record_mtime — max mtime of people/*.md only
#    configure_utf8_stdout     — reconfigure stdout to UTF-8 (Windows cp1252 compat)
#
#  Output helpers
#    EXIT_CLEAN / EXIT_WARNINGS / EXIT_ERRORS / EXIT_FAILURE  — shared exit codes
#    Finding                   — one lint finding: severity + code + path + message
#    emit_findings             — print findings list and return exit code
#    Message                   — one human-facing line: level/text/next_step (+code/path)
#    Result                    — the structured-result contract every run_* returns
#    finding_to_message        — fold a lint Finding into a Result Message
#
# ─────────────────────────────────────────────────────────────────────────────


# ── Regex patterns (TOOLING.md §1) ───────────────────────────────────────────

# Crockford Base32 alphabet — lowercase, omitting i l o u
CROCKFORD_ALPHA = '0123456789abcdefghjkmnpqrstvwxyz'

# Matches any bare ID in text (case-insensitive)
ID_RE = re.compile(r'\b([PSCLH])-([0-9a-hjkmnp-tv-z]{10})\b', re.I)

# Matches [ID] citation/cross-link tokens
TOKEN_RE = re.compile(r'\[([PSCLH]-[0-9a-hjkmnp-tv-z]{10})\]', re.I)

# Extracts YAML frontmatter (between first --- pair)
FRONT_RE = re.compile(r'\A---\r?\n(.*?)\r?\n---\r?\n', re.S)

# Extracts fenced YAML claims block under ## Claims
CLAIMS_RE = re.compile(r'^## Claims.*?```yaml\r?\n(.*?)```', re.S | re.M)

# ── Significance table (SPEC §8.2) ────────────────────────────────────────────

SIGNIFICANCE: dict[str, str] = {
    'birth': 'vital', 'death': 'vital', 'marriage': 'vital',
    'baptism': 'vital', 'burial': 'vital',
    'residence': 'substantive', 'census': 'substantive',
    'occupation': 'substantive', 'education': 'substantive',
    'military': 'substantive', 'immigration': 'substantive',
    'divorce': 'substantive', 'name': 'substantive',
    'relationship': 'substantive',
    'event': 'incidental', 'note': 'incidental',
}

CLAIM_TYPES: frozenset[str] = frozenset(SIGNIFICANCE.keys())

VITAL_TYPES: frozenset[str] = frozenset(
    t for t, sig in SIGNIFICANCE.items() if sig == 'vital'
)

SOURCE_TYPES: frozenset[str] = frozenset({
    'census', 'vital-record', 'newspaper', 'photo', 'interview', 'letter',
    'military-record', 'land-record', 'probate', 'directory', 'dna', 'book',
    'website', 'artifact', 'proof-argument', 'other',
})

EDTF_EXAMPLE_TEXT = 'like 1880, 1880-06-15, or 188X for "the 1880s"'


def source_type_list() -> str:
    """Return the controlled source_type vocabulary in a stable display order.

    The same list appears in CLI refusals, lint findings, and docs. Keeping the
    formatting here prevents one tool from teaching a shorter or stale version
    of the vocabulary than another.
    """
    return ', '.join(sorted(SOURCE_TYPES))


def format_source_type_error(value: object, *, where: str = 'source_type') -> str:
    """Explain an unknown source type with the valid list and a concrete fix.

    `source_type` is archive jargon, so every hard refusal that names it must
    also say what it means: the source category stored on a source record. The
    caller supplies `where` when the bad value came from a flag or sidecar file.
    """
    return (
        f'unknown {where} {value!r}. source_type means the source category, '
        f'for example census or photo. Use one of: {source_type_list()}.'
    )


def format_edtf_error(value: object, *, field: str = 'date') -> str:
    """Explain an unreadable date with examples the human can copy.

    EDTF is the archive's compact date form. As of PR 05 the tools first try to
    READ loose human input (`normalize_date`: "circa 1870" → "1870~", "1870s" →
    "187X") and only fall back to this hard message when no clear reading exists —
    so this is reserved for genuinely ambiguous values, and it teaches the
    accepted shapes (including the natural phrasings now understood) rather than
    stopping at the acronym.
    """
    return (
        f'{field} {value!r} is not a date the archive can read. '
        f'Write it {EDTF_EXAMPLE_TEXT}, or in plain words like '
        f'"about 1880", "before 1880", or "the 1880s".'
    )


def format_exiftool_error(command: str = 'fha process') -> str:
    """Explain that photo features need exiftool and name the recovery command.

    `exiftool` is an external program used for the only sanctioned photo writes:
    reading and adding metadata keywords. A missing binary is not a data error,
    so the message tells the user what capability is blocked and where to check
    the archive after installation.
    """
    return (
        f'{command} needs exiftool for photo metadata. Install exiftool and make '
        f'sure the `exiftool` command works, then run `{command}` again. '
        'Run `fha doctor` to check your archive.'
    )


def format_yaml_dependency_error() -> str:
    """Return the central missing-PyYAML message used before config parsing.

    Most tools read `fha.yaml`, source records, or claims through PyYAML. Import
    failure used to surface as a Python traceback; this text gives the install
    line and a verification command instead.
    """
    return (
        'This tool needs PyYAML to read archive YAML files. Install it with '
        '`python -m pip install pyyaml`, then run `fha doctor` to check your archive.'
    )


def archive_root_missing_message() -> str:
    """Return the one archive-root recovery message shared by every entry point."""
    return (
        'cannot find archive root (no fha.yaml found). Run this from inside the '
        'archive, or add `--root PATH` with the folder that contains fha.yaml.'
    )

# Common raster and camera-raw extensions a personal photo library mixes in.
# Canonical home for the set so that `photoindex` (cataloguing) and `process`
# (document-vs-photo intake detection) agree on what counts as a photo without
# either tool importing the other (tools never import tools — TOOLING §15).
PHOTO_EXTENSIONS: frozenset[str] = frozenset({
    '.jpg', '.jpeg', '.png', '.tif', '.tiff', '.bmp', '.gif', '.heic', '.heif',
    '.cr2', '.nef', '.dng', '.arw', '.orf', '.rw2',
})

# Companion file kinds: generated view files that share a P-id with their profile
# and live in the same folder.  Enumerated here so that parse_filename (kind
# detection) and index.py (person_files.kind column) stay in sync when new view
# types are added — add the kind here, and both consumers pick it up automatically.
COMPANION_KINDS: frozenset[str] = frozenset({'research', 'timeline', 'sources-index', 'draft-queue'})

# Disposable cache schema versions. These are deliberately small integers stored
# in both a meta row and PRAGMA user_version so humans and SQLite tools can see
# which cache shape a file was built with.
# v2: rights.publication_ok is now stored three-state (1/0/NULL) instead of
# folding explicit false to NULL. Exporters redact on `COALESCE(publication_ok,
# 1) = 0`, which only fires on a stored 0 — so a v1 index (false → NULL) would
# silently under-redact publication_ok:false sources. Bumping forces `fha index`
# to rebuild before the redaction-critical consumers (site/gedcom/wikitree) trust it.
INDEX_SCHEMA_VERSION = 2
PHOTOINDEX_SCHEMA_VERSION = 1
CACHE_SCHEMA_KEY = 'schema_version'

# ── fha.yaml loading ──────────────────────────────────────────────────────────

def find_archive_root(start: str | Path | None = None) -> Path | None:
    """Walk upward from `start` (or CWD) to find a directory containing fha.yaml."""
    p = Path(start or os.getcwd()).resolve()
    while True:
        if (p / 'fha.yaml').exists():
            return p
        parent = p.parent
        if parent == p:
            return None
        p = parent


def resolve_root_arg(args: Any) -> Path | None:
    """
    Resolve the archive root from a parsed CLI namespace: its own `--root`
    flag if given, else walk up from CWD via `find_archive_root()`.

    Every subcommand defines its own `--root` (TOOLING §1 — argparse doesn't
    propagate parent-parser flags into subparsers), so every tool used to
    re-implement this same five-line lookup. Centralized here so there's one
    error message and one behavior to keep correct.

    Prints an ERROR to stderr and returns None when neither source finds a
    root; the caller decides the exit code (most tools return EXIT_FAILURE).
    """
    root = getattr(args, 'root', None)
    if root:
        return Path(root).resolve()
    detected = find_archive_root()
    if detected is None:
        print(f'ERROR: {archive_root_missing_message()}', file=sys.stderr)
        return None
    return detected


class FhaConfigError(Exception):
    """Raised by load_fha_yaml(strict=True) when fha.yaml is malformed.

    A silent empty-dict fallback can make tools ignore external documents/photos
    roots without telling the user, quietly changing which files are considered
    truth — strict mode surfaces that instead.
    """


def _require_yaml() -> None:
    """Raise a friendly dependency error before any PyYAML API is used."""
    if yaml is None:
        raise FhaConfigError(format_yaml_dependency_error())


def _yaml_problem_location(exc: object) -> str:
    """Return a plain line/column locator for PyYAML exceptions when available."""
    mark = getattr(exc, 'problem_mark', None)
    if mark is None:
        return ''
    return f' on line {mark.line + 1}, column {mark.column + 1}'


def format_fha_config_error(path: str | Path, detail: object) -> str:
    """Explain a bad fha.yaml in plain language with a minimal valid example.

    `fha.yaml` is the file that tells the tools where archive folders live. A
    YAML parser message alone is not actionable for the target user, so this
    wrapper gives the line location when PyYAML provides it and a tiny shape
    the file can be repaired toward.
    """
    path = Path(path)
    loc = _yaml_problem_location(detail)
    return (
        f'{path.name} has a problem{loc}. It should be a small YAML settings file, '
        'for example:\n'
        'roots:\n'
        '  documents: documents\n'
        '  photos: photos\n'
        f'Original parser note: {detail}'
    )


def format_record_yaml_error(path: str | Path, detail: object, *, section: str) -> str:
    """Explain malformed YAML inside an archive record or sidecar.

    Source/person records and inbox sidecars are not `fha.yaml`, so their
    repair hint should point at the section being edited: frontmatter is the
    key/value block between `---` lines, while claims are a YAML list under
    `## Claims`. Keeping this separate prevents config examples from leaking
    into record-editing errors.
    """
    path = Path(path)
    loc = _yaml_problem_location(detail)
    if section == 'claims':
        example = (
            'Claims should be a YAML list, for example:\n'
            '- id: C-0123456789\n'
            '  type: birth\n'
            '  persons: [P-0123456789]\n'
            '  value: born about 1880\n'
            '  status: suggested'
        )
    else:
        example = (
            'Frontmatter should be key/value lines between --- markers, for example:\n'
            '---\n'
            'title: Family census page\n'
            'source_type: census\n'
            '---'
        )
    return (
        f'{path.name} has a YAML problem in its {section}{loc}. {example}\n'
        f'Original parser note: {detail}'
    )


def load_fha_yaml(archive_root: str | Path, *, strict: bool = False) -> dict:
    """Load fha.yaml and return the parsed dict.

    A missing file returns {} (running without fha.yaml on default roots is
    legitimate).  A *malformed* file is handled per `strict`:
      - strict=False (default): return {} (permissive/legacy behavior).
      - strict=True: raise FhaConfigError so the caller can fail loudly rather
        than silently dropping configured roots.
    """
    path = Path(archive_root) / 'fha.yaml'
    if not path.exists():
        return {}
    try:
        _require_yaml()
    except FhaConfigError:
        if strict:
            raise
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            data = yaml.safe_load(f)
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise FhaConfigError(
                f'{path.name} must be a YAML mapping: key/value lines like '
                '`roots:` followed by indented entries. Example:\n'
                'roots:\n'
                '  documents: documents\n'
                '  photos: photos'
            )
        return data
    except FhaConfigError:
        if strict:
            raise
        return {}
    except Exception as e:
        if strict:
            raise FhaConfigError(format_fha_config_error(path, e)) from e
        return {}


def get_roots(fha_config: dict) -> dict[str, str]:
    """Extract the roots mapping from fha.yaml config."""
    return fha_config.get('roots', {})


def resolve_path(
    record_path: str,
    fha_config: dict,
    archive_root: str | Path,
) -> Path:
    """
    Resolve a record-relative alias path like 'photos/1880/foo.jpg' to an absolute Path.
    Alias is the first path segment; mapped through fha.yaml roots:
      - absolute value → used as-is
      - relative value → joined to archive_root
      - missing alias → internal directory of that name under archive_root
    """
    record_path = record_path.replace('\\', '/')
    parts = record_path.split('/', 1)
    alias = parts[0]
    rest = parts[1] if len(parts) > 1 else ''

    roots = get_roots(fha_config)
    archive_root = Path(archive_root)

    if alias in roots:
        root_val = str(roots[alias])
        if os.path.isabs(root_val):
            base = Path(root_val)
        else:
            base = archive_root / root_val
    else:
        base = archive_root / alias

    return (base / rest) if rest else base


def path_to_alias(path: str | Path, alias: str, fha_config: dict, archive_root: str | Path) -> str:
    """
    Inverse of resolve_path: turn an absolute Path under `alias`'s root back into
    the stored alias-form path ('photos/1880/foo.jpg', forward slashes — TOOLING
    "All stored paths are alias-form with forward slashes").

    Falls back to the absolute path's forward-slash form if `path` isn't under the
    alias's resolved root (e.g. an absolute root configured outside archive_root).
    """
    # Resolve both sides: a relative root containing '..' (an external asset
    # root like 'documents: ../family-docs') stays lexically distinct from a
    # caller's already-resolved file path even though they name the same
    # directory, which would otherwise send every file under it to the
    # non-portable absolute-path fallback below.
    root = resolve_path(alias, fha_config, archive_root).resolve()
    path = Path(path).resolve()
    try:
        rel = path.relative_to(root)
    except ValueError:
        return path.as_posix()
    return f'{alias}/{rel.as_posix()}' if str(rel) != '.' else alias


def db_mtime(db_path: Path) -> float | None:
    """Return the mtime of db_path, or None if it is absent/unreadable."""
    try:
        return db_path.stat().st_mtime
    except OSError:
        return None


def probe_sqlite(db_path: str | Path, probe_sql: str) -> bool:
    """Return True if db_path opens and probe_sql executes without error."""
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(probe_sql)
        finally:
            conn.close()
        return True
    except Exception:
        return False


def sqlite_cache_schema_status(
    db_path: str | Path,
    expected_version: int,
    required_tables: tuple[str, ...],
) -> tuple[str, str]:
    """
    Classify a disposable SQLite cache before any caller trusts its rows.

    Returns (status, detail):
      'absent'     -> no DB file exists
      'unreadable' -> SQLite cannot open/query it at all
      'old-schema' -> readable, but missing/wrong schema_version or tables
      'fresh'      -> version marker and required tables are present
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return ('absent', '')

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db_path))
        meta_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        if meta_exists is None:
            return ('old-schema', 'schema version is missing')

        row = conn.execute(
            'SELECT value FROM meta WHERE key=?', (CACHE_SCHEMA_KEY,)
        ).fetchone()
        if row is None:
            return ('old-schema', 'schema version is missing')
        try:
            actual_version = int(row[0])
        except (TypeError, ValueError):
            return ('old-schema', f"schema version {row[0]!r} is not readable")
        if actual_version != expected_version:
            return (
                'old-schema',
                f'schema version {actual_version} does not match expected {expected_version}',
            )

        user_version = conn.execute('PRAGMA user_version').fetchone()[0]
        if int(user_version or 0) != expected_version:
            return (
                'old-schema',
                f'SQLite user_version {user_version} does not match expected {expected_version}',
            )

        for table in required_tables:
            conn.execute(f'SELECT 1 FROM {table} LIMIT 1')
        return ('fresh', '')
    except sqlite3.DatabaseError as exc:
        return ('unreadable', str(exc))
    except Exception as exc:
        return ('unreadable', str(exc))
    finally:
        if conn is not None:
            conn.close()


def open_index_db(
    archive_root: str | Path,
    required_tables: tuple[str, ...],
    *,
    strict: bool = False,
) -> sqlite3.Connection | None:
    """
    Open `.cache/index.sqlite` for reading, with the freshness check and
    table probe every index-reading tool needs before it starts querying.

    Returns None (after printing an explanatory message to stderr) when:
      - the file doesn't exist (run `fha index` first)
      - it's stale and `strict=True` (generating/mutating commands can't
        safely act on stale data; strict=False — read-only commands — only
        warns and still returns the connection, since a slightly stale
        answer beats no answer)
      - it exists but fails the table probe (corrupt or pre-this-schema)

    `required_tables` lets each caller ask for exactly the tables its
    queries touch (e.g. `cooccur` needs `relationships`, plain `find`
    lookups only need `persons`) so a partial/older schema fails fast here
    rather than raising mid-query.

    The connection opened during the probe is always closed before
    returning None — a probe failure used to leak the connection in three
    different copies of this function across the tool files.
    """
    archive_root = Path(archive_root)
    db_path = archive_root / '.cache' / 'index.sqlite'
    if not db_path.exists():
        print(
            'ERROR: .cache/index.sqlite not found - run `fha index` first '
            'then re-run this command.',
            file=sys.stderr,
        )
        return None

    schema_status, schema_detail = sqlite_cache_schema_status(
        db_path, INDEX_SCHEMA_VERSION, required_tables,
    )
    if schema_status in {'unreadable', 'old-schema'}:
        suffix = f' ({schema_detail})' if schema_detail else ''
        print(
            'ERROR: .cache/index.sqlite is unreadable or has an incompatible schema; '
            'your search index is out of date or unreadable'
            f'{suffix}. Run `fha index` to rebuild it.',
            file=sys.stderr,
        )
        return None

    mtime = db_mtime(db_path)
    stale = mtime is not None and newest_record_mtime(archive_root) > mtime
    if stale:
        if strict:
            print(
                "ERROR: index is stale; run 'fha index' before generating views.",
                file=sys.stderr,
            )
            return None
        print(
            'WARNING: index may be stale — a record file is newer than '
            '.cache/index.sqlite. Run `fha index` to refresh.',
            file=sys.stderr,
        )

    conn: sqlite3.Connection | None = None
    try:
        # sqlite3.connect() itself can raise (path is a directory, permission
        # denied, locked, etc.) — keep it inside the guard so callers see the
        # documented unreadable-index error and exit 3 instead of a traceback.
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        for table in required_tables:
            conn.execute(f'SELECT 1 FROM {table} LIMIT 1')
        return conn
    except Exception:
        if conn is not None:
            conn.close()
        print(
            'ERROR: .cache/index.sqlite is unreadable or has an incompatible schema; '
            'your search index is out of date or unreadable. Run `fha index` to rebuild it.',
            file=sys.stderr,
        )
        return None


def photoindex_status(archive_root: str | Path, fha_config: dict) -> tuple[str, float]:
    """Classify the photo index (.cache/photos.sqlite) for find/doctor.

    Returns (status, lag_seconds):
      'absent'     → no photos.sqlite               (lag 0.0)
      'unreadable' → exists but fails a basic schema query — corrupt/incompatible (lag 0.0)
      'stale'      → older than the newest file in the photos root (lag = seconds behind)
      'fresh'      → schema OK and not older than the photos root (lag 0.0)

    The schema is probed *before* the empty/missing-photo-root short-circuit, so a
    corrupt database is never reported fresh just because there are no photos to
    compare against.  Shared by `find --text` (caption search gating) and
    `doctor` (freshness report) so both agree on whether photos.sqlite is usable.
    """
    archive_root = Path(archive_root)
    db_path = archive_root / '.cache' / 'photos.sqlite'
    mtime = db_mtime(db_path)
    if mtime is None:
        return ('absent', 0.0)

    # Probe required tables.  `photo_face_regions` is part of the scrape cache,
    # not just a derived query table; an older cache missing it needs a refresh
    # before doctor/find should call the photoindex fresh.
    schema_status, _schema_detail = sqlite_cache_schema_status(
        db_path,
        PHOTOINDEX_SCHEMA_VERSION,
        (
            'photos', 'photo_face_regions', 'photo_fts', 'photo_groups',
            'photo_keywords', 'photo_people',
        ),
    )
    if schema_status in {'unreadable', 'old-schema'}:
        return (schema_status, 0.0)

    # photo_people is derived from both .cache/index.sqlite
    # (face_tags/name_variants) and source record `people:` lists. Edits in
    # either place make photos.sqlite stale even though no photo file changed.
    index_mtime = db_mtime(archive_root / '.cache' / 'index.sqlite')
    max_mtime = index_mtime if index_mtime is not None else 0.0

    # The index.sqlite mtime only catches a person edit that has already been
    # folded into a rebuilt index. If a profile's face_tags/name_variants changed
    # but `fha index` has NOT been rerun, index.sqlite (and the photo_people rows
    # derived from it) is stale even though its mtime looks current. Fold the
    # person-record watermark in directly — mirroring photoindex._index_is_fresh —
    # so find/doctor flag the cache stale instead of serving outdated weak matches.
    record_mtime = newest_person_record_mtime(archive_root)
    if record_mtime > max_mtime:
        max_mtime = record_mtime
    source_mtime = newest_source_record_mtime(archive_root, subdir='photos')
    if source_mtime > max_mtime:
        max_mtime = source_mtime

    photos_root = resolve_path('photos', fha_config, archive_root)
    if photos_root.is_dir():
        # Directory mtimes are included (not just file mtimes) so that a deletion
        # or rename — which bumps the parent directory's mtime but touches no
        # remaining file — still makes the index look stale instead of silently
        # staying 'fresh' with photo_fts rows pointing at files that no longer exist.
        for p in photos_root.rglob('*'):
            if p.is_file() or p.is_dir():
                try:
                    m = p.stat().st_mtime
                    if m > max_mtime:
                        max_mtime = m
                except OSError:
                    pass
        try:
            root_mtime = photos_root.stat().st_mtime
            if root_mtime > max_mtime:
                max_mtime = root_mtime
        except OSError:
            pass

    if max_mtime == 0.0 or mtime >= max_mtime:
        return ('fresh', 0.0)          # empty root, or db newer than newest photo/index
    return ('stale', max_mtime - mtime)


# ── Record parsing ────────────────────────────────────────────────────────────

def _coerce_yaml(obj: Any) -> Any:
    """Recursively coerce YAML scalars to types the index expects."""
    if isinstance(obj, dict):
        return {k: _coerce_yaml(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_coerce_yaml(v) for v in obj]
    if isinstance(obj, bool):
        return str(obj).lower()          # True → 'true', False → 'false'
    if isinstance(obj, datetime.date):
        return obj.isoformat()
    return obj


def read_record(path: str | Path) -> dict:
    """
    Parse a markdown archive record file.

    Returns:
        {
            'meta': dict,           frontmatter (scalars coerced)
            'claims': list,         parsed claim dicts (empty on failure)
            'stories': str | None,  ## Stories section body
            'body': str,            full body text (after frontmatter)
            'parse_errors': list,   [(code, message), ...]
        }
    """
    path = Path(path)
    errors: list[tuple[str, str]] = []

    try:
        text = path.read_text(encoding='utf-8')
    except OSError as e:
        return {
            'meta': {}, 'claims': [], 'stories': None, 'body': '',
            'parse_errors': [('E010', f'Cannot read file: {e}')],
        }

    # Frontmatter
    meta: dict = {}
    body = text
    fm_match = FRONT_RE.match(text)
    if fm_match:
        try:
            _require_yaml()
            raw_meta = yaml.safe_load(fm_match.group(1)) or {}
            meta = _coerce_yaml(raw_meta)
        except FhaConfigError as e:
            errors.append(('E010', str(e)))
        except yaml.YAMLError as e:
            errors.append(('E010', f'Frontmatter YAML error: {format_record_yaml_error(path, e, section="frontmatter")}'))
        body = text[fm_match.end():]

    # Claims block
    claims: list[dict] = []
    cm_match = CLAIMS_RE.search(body)
    if cm_match:
        try:
            _require_yaml()
            raw_claims = yaml.safe_load(cm_match.group(1))
            if raw_claims is None:
                raw_claims = []
            if isinstance(raw_claims, list):
                claims = [_coerce_yaml(c) for c in raw_claims if c is not None]
            else:
                errors.append(('E010', 'Claims block is not a YAML list'))
        except FhaConfigError as e:
            errors.append(('E010', str(e)))
        except yaml.YAMLError as e:
            errors.append(('E010', f'Claims YAML error: {format_record_yaml_error(path, e, section="claims")}'))

    # Stories section
    stories: str | None = None
    sm = re.search(r'^## Stories\s*\r?\n(.*?)(?=^## |\Z)', body, re.S | re.M)
    if sm:
        content = sm.group(1).strip()
        if content and content not in ('*(none yet)*', '(none yet)'):
            stories = content

    return {
        'meta': meta,
        'claims': claims,
        'stories': stories,
        'body': body,
        'parse_errors': errors,
    }


def parse_filename(path: str | Path) -> dict | None:
    """
    Parse a record filename into its components.

    Person files:  {surname}__{given}[_{kind}]_{P-id}.md
    Source records: {slug}_{S-id}.md
    Source files:  {slug}[-{copy}][-{role}]_{S-id}.{ext}

    Returns dict with keys: id_str, id_type, kind (for persons), is_companion
    Returns None if filename doesn't match any expected pattern.
    """
    name = Path(path).stem          # filename without extension
    ext = Path(path).suffix.lower()

    # Look for a trailing ID: -{10 crockford chars} after the last underscore
    id_match = re.search(r'_([PSCLH]-[0-9a-hjkmnp-tv-z]{10})$', name, re.I)
    if not id_match:
        return None

    id_str = id_match.group(1).lower()
    id_type = id_str[0].upper()
    before_id = name[:id_match.start()]   # everything before _{id}

    result = {
        'id_str': id_str,
        'id_type': id_type,
        'kind': None,
        'is_companion': False,
    }

    if id_type == 'P' and ext == '.md':
        # Person file — check for companion kind suffix
        # pattern: {surname}__{given}[_{kind}]_{P-id}
        # kind is one of: research, timeline, sources-index
        for kind in sorted(COMPANION_KINDS, key=len, reverse=True):
            suffix = f'_{kind}'
            if before_id.endswith(suffix):
                result['kind'] = kind
                result['is_companion'] = True
                break
        if result['kind'] is None:
            result['kind'] = 'profile'
        # Verify double-underscore surname separator
        if '__' not in before_id.split('_research')[0].split('_timeline')[0].split('_sources-index')[0]:
            # May be a source file accidentally named with P-id — not valid person filename
            pass

    return result


# ── Media filename grammar (TOOLING.md §6, §9) ───────────────────────────────
#
# Unprocessed photos/scans in a mixed folder carry no S-id yet, but variation
# siblings (different scans of one physical photo, front/back pairs, pages of
# a booklet) share a filename "base_id" with only a suffix distinguishing
# them.  This parser recovers that structure so `fha photoindex` (grouping)
# and `fha process` (variation-detection prompt) can both recognise siblings
# without either tool importing the other (shared code lives only in _lib).

@dataclasses.dataclass(frozen=True)
class ParsedName:
    """One filename stem decomposed per the TOOLING §6 suffix grammar.

    base_id    — the stem with all recognised suffixes stripped; the grouping key.
    variant_id — trailing copy letter ('a', 'b', 'c', …) if present, else None.
    part_kind  — 'front' | 'back' | 'page' | 'negative' | 'bw' | 'freeform' | 'none'.
    page_num   — integer page number when part_kind == 'page', else None.
    freeform_role — unrecognised suffix kept as a role, per TOOLING §6.
    is_crop    — True if a '-crop' derivative-detail suffix was stripped.
    """
    base_id: str
    variant_id: str | None
    part_kind: str
    page_num: int | None
    freeform_role: str | None
    is_crop: bool


_CROP_SUFFIX_RE = re.compile(r'[-_]crop$', re.I)
_NEGATIVE_SUFFIX_RE = re.compile(r'[-_]negative$', re.I)
_BACK_SUFFIX_RE = re.compile(r'[-_]back$', re.I)
_FRONT_SUFFIX_RE = re.compile(r'[-_]front$', re.I)
_BW_SUFFIX_RE = re.compile(r'[-_]bw$', re.I)
_PAGE_SUFFIX_RE = re.compile(r'[-_]page[-_]?(\d+)$', re.I)
_VARIANT_DASH_RE = re.compile(r'-([a-z])$', re.I)
_VARIANT_BARE_RE = re.compile(r'(?<=[0-9])([a-z])$', re.I)
_FREEFORM_ROLE_RE = re.compile(r'[-_]([a-z][a-z0-9-]*)$', re.I)


def parse_media_filename(stem: str) -> ParsedName:
    """
    Decompose a photo/scan filename stem into base_id + variation metadata.

    Suffixes are stripped in a fixed priority order (TOOLING §6) because the
    grammar is ambiguous if read in any other sequence — e.g. 'portrait_1880b'
    must lose the bare trailing letter only after confirming no dash-suffix
    role applies first:
      1. '-crop'                         (stacks on any other suffix)
      2. part-kind: '-negative' before '-back'/'-front'/'-page[-]N'/'-bw'
      3. trailing variant letter: '-b' (dash) or bare 'b' right after a digit
      4. whatever remains is base_id.

    A '-negative' filename may still carry a variant letter (e.g.
    'portrait_1880b-negative') — the parser records it in variant_id, but
    TOOLING §9 directs the *grouper* to file negatives at the stem level
    regardless of that letter, since a negative is source material for the
    root image, not an A/B print variant. That grouping decision lives in
    photoindex.py, not here — this function only reports what the filename
    literally encodes.
    """
    remaining = stem
    is_crop = bool(_CROP_SUFFIX_RE.search(remaining))
    if is_crop:
        remaining = _CROP_SUFFIX_RE.sub('', remaining)

    part_kind = 'none'
    page_num: int | None = None
    freeform_role: str | None = None
    page_m = _PAGE_SUFFIX_RE.search(remaining)
    if page_m:
        part_kind = 'page'
        page_num = int(page_m.group(1))
        remaining = _PAGE_SUFFIX_RE.sub('', remaining)
    elif _NEGATIVE_SUFFIX_RE.search(remaining):
        part_kind = 'negative'
        remaining = _NEGATIVE_SUFFIX_RE.sub('', remaining)
    elif _BACK_SUFFIX_RE.search(remaining):
        part_kind = 'back'
        remaining = _BACK_SUFFIX_RE.sub('', remaining)
    elif _FRONT_SUFFIX_RE.search(remaining):
        part_kind = 'front'
        remaining = _FRONT_SUFFIX_RE.sub('', remaining)
    elif _BW_SUFFIX_RE.search(remaining):
        part_kind = 'bw'
        remaining = _BW_SUFFIX_RE.sub('', remaining)
    else:
        freeform_m = _FREEFORM_ROLE_RE.search(remaining)
        # A single trailing letter is never a freeform role — it's either a
        # documented copy variant ('-b', '034b') or, for an undocumented form
        # like '_a', not a suffix at all (TOOLING §6: only dash or
        # bare-after-digit is copy-variant grammar; underscore-letter must
        # stay part of base_id rather than being swallowed as a "role").
        if freeform_m and len(freeform_m.group(1)) > 1:
            part_kind = 'freeform'
            freeform_role = freeform_m.group(1).lower()
            remaining = _FREEFORM_ROLE_RE.sub('', remaining)

    variant_id: str | None = None
    dash_m = _VARIANT_DASH_RE.search(remaining)
    if dash_m:
        variant_id = dash_m.group(1).lower()
        remaining = _VARIANT_DASH_RE.sub('', remaining)
    else:
        bare_m = _VARIANT_BARE_RE.search(remaining)
        if bare_m:
            variant_id = bare_m.group(1).lower()
            remaining = remaining[:-1]

    return ParsedName(
        base_id=remaining, variant_id=variant_id, part_kind=part_kind,
        page_num=page_num, freeform_role=freeform_role, is_crop=is_crop,
    )


def grouping_stem(parsed: ParsedName) -> str:
    """The base_id to group variation siblings by (TOOLING §6/§9).

    The recognised suffix grammar (copy letter, negative/back/front/page-N/bw,
    crop) is stripped so different scans of one physical photo collapse to one
    key, but an *unrecognised* freeform suffix is folded back in: two unrelated
    files like 'smith-family.jpg' and 'smith-house.jpg' must not merge into one
    group just because both end in '-word'.

    Lives in _lib (not photoindex) because two tools must agree on what counts
    as a variation group: `fha photoindex` caches the grouping, and `fha
    process` re-derives it to surface the one/separate/skip prompt. If the two
    used different rules, a folder would group differently depending on which
    tool looked at it (AGENTS_TOOLING symmetry: photoindex grouping ↔ process
    variation detection). Tools never import tools, so the shared rule lives
    here.
    """
    if parsed.part_kind == 'freeform':
        return f'{parsed.base_id}-{parsed.freeform_role}'
    return parsed.base_id


def variant_role(parsed: ParsedName) -> str | None:
    """Compound role string for a non-primary variation member (TOOLING §6/§9).

    Returns None for a plain scan (no recognised suffix) — the caller treats a
    None role as the primary. 'page' carries its number ('page-3'); a freeform
    suffix becomes the role verbatim; '-crop' stacks onto whatever part-kind it
    accompanies ('back-crop') or stands alone ('crop'). Shared by `fha
    photoindex` (the cached `variant_role` column) and `fha process` (the
    `files:` role annotation written on a grouped source), so both label the
    same physical relationship identically.
    """
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


def select_variation_primary(members: list, parsed_of) -> object:
    """Pick the primary member of a variation group (TOOLING §6/§9).

    `members` is any list of comparable keys (Paths or path strings) and
    `parsed_of(member) -> ParsedName` maps each to its parsed filename. The
    primary is, in priority order: a plain scan (no variant letter, no
    part-kind, no crop); else a front scan of copy a/none; else the
    lexicographically-first member. Min() over the candidate set makes the
    choice deterministic when several qualify (e.g. two plain scans).

    Shared so `fha process` flags the same file as `is_primary: true` that
    `fha photoindex` records in `photo_groups.primary_path`.
    """
    plain = [
        m for m in members
        if parsed_of(m).variant_id is None
        and parsed_of(m).part_kind == 'none'
        and not parsed_of(m).is_crop
    ]
    if plain:
        return min(plain)
    fronts = [
        m for m in members
        if parsed_of(m).variant_id in (None, 'a')
        and parsed_of(m).part_kind == 'front'
        and not parsed_of(m).is_crop
    ]
    if fronts:
        return min(fronts)
    return min(members)


# ── EDTF handling (TOOLING.md §1) ────────────────────────────────────────────

# Validation regex for the EDTF subset this system uses.
# Both tilde-before-component (1850-~05) and tilde-at-end (1880-06~) are valid
# EDTF Level 1 syntax for approximate dates.
_EDTF_PATTERNS = [
    re.compile(r'^\d{4}[~?]?$'),                              # 1850, 1850~, 1850?
    re.compile(r'^\d{3}X$'),                                  # 185X (decade)
    re.compile(r'^\d{4}-~?\d{2}[~?]?$'),                     # 1850-05, 1850-~05, 1850-05~
    re.compile(r'^\d{4}-~?\d{2}-~?\d{2}[~?]?$'),             # 1850-05-20 and approximate variants
    re.compile(r'^\[\.{2}\d{4}(?:-\d{2})?(?:-\d{2})?\]$'),   # [..1920]
]


def is_valid_edtf(s: str | None) -> bool:
    """Return True if s is a valid EDTF date per TOOLING.md §1."""
    if not s or not isinstance(s, str):
        return False
    s = s.strip()
    if '/' in s:
        parts = s.split('/', 1)
        return is_valid_edtf(parts[0]) and is_valid_edtf(parts[1])
    if not any(p.match(s) for p in _EDTF_PATTERNS):
        return False
    try:
        edtf_bounds(s)
    except ValueError:
        return False
    return True


# Loose human date forms the archive understands and the canonical EDTF they map
# to.  The agent is taught to write canonical EDTF directly (AGENTS.md), so these
# exist for the OTHER path: a human hand-edits a claim and types "circa 1870" or
# "1870s".  That is the normal condition of this work, not an error — so the tools
# translate the meaning instead of refusing it ("forgiving, not fussy").
#
# Each prefix must be followed by whitespace so a bare word never swallows a year
# that happens to start with the same letters.  "circa"/"about" → approximate (~);
# "before"/"by" → the EDTF before-form ([..YYYY]); "maybe"/"possibly" → uncertain (?).
_APPROX_PREFIX_RE = re.compile(
    r'^(?:c|ca|circa|abt|about|around|approx|approximately|roughly|est|estimated)\.?\s+',
    re.I,
)
_BEFORE_PREFIX_RE = re.compile(r'^(?:before|bef|prior to|by)\.?\s+', re.I)
_UNCERTAIN_PREFIX_RE = re.compile(r'^(?:maybe|possibly|perhaps|probably)\.?\s+', re.I)
_MONTH_NAMES = {
    'jan': 1, 'january': 1,
    'feb': 2, 'february': 2,
    'mar': 3, 'march': 3,
    'apr': 4, 'april': 4,
    'may': 5,
    'jun': 6, 'june': 6,
    'jul': 7, 'july': 7,
    'aug': 8, 'august': 8,
    'sep': 9, 'sept': 9, 'september': 9,
    'oct': 10, 'october': 10,
    'nov': 11, 'november': 11,
    'dec': 12, 'december': 12,
}


def normalize_date(s: str | None) -> str | None:
    """Translate a loose, human-written date into canonical EDTF, or None if its
    meaning is genuinely unclear.

    Returns the input unchanged when it is ALREADY valid EDTF (the common case),
    so callers can use this as a cheap "is this fine, and if not what did they
    mean?" check.  Returns None only when no clear reading exists — that is the
    one case a tool should fall back to asking the human a plain question.

    Recognised loose forms (everything else → None):
      circa/ca/c./abt/about/around/approx/est 1870, ~1870  → 1870~  (approximate)
      maybe/possibly/perhaps 1870                          → 1870?  (uncertain)
      before/bef/prior to/by 1920                          → [..1920]
      1870s, 1870's, 187x                                  → 187X   (decade)
      between 1870 and 1875, 1870 to 1875, 1870-1875       → 1870/1875 (interval)
      a bare year/month/day already shaped like EDTF       → itself

    Month names such as "June 1923", "June 14 1923", and "14 June 1923"
    are parsed because they carry a clear calendar meaning. The result is always
    re-validated against is_valid_edtf before being returned, so this never emits
    a string the rest of the toolchain can't read.
    """
    if not s or not isinstance(s, str):
        return None
    raw = s.strip()
    if not raw:
        return None
    if is_valid_edtf(raw):
        return raw

    # Work on a lowercased, whitespace-collapsed copy stripped of trailing
    # sentence punctuation ("circa 1870." → "circa 1870").  Canonical forms with
    # meaningful punctuation (~, ?, [..], /) are already handled by the early
    # return above, so stripping '.,' here only removes human noise.
    text = re.sub(r'\s+', ' ', raw.lower()).strip().strip('.,')
    text = re.sub(r'^the\s+', '', text)

    # A leading approximate tilde ("~1870") folds into the approximate path.
    if text.startswith('~'):
        text = 'circa ' + text[1:].strip()

    approx = before = uncertain = False
    m = _APPROX_PREFIX_RE.match(text)
    if m:
        approx, text = True, text[m.end():].strip()
    elif (m := _BEFORE_PREFIX_RE.match(text)):
        before, text = True, text[m.end():].strip()
    elif (m := _UNCERTAIN_PREFIX_RE.match(text)):
        uncertain, text = True, text[m.end():].strip()

    candidate: str | None = None

    range_m = re.match(r'^(?:between\s+)?(\d{4})\s*(?:to|and|-|–|/)\s*(\d{4})$', text)
    decade_word_m = re.match(r"^(\d{3})0(?:'s|s)$", text)
    decade_x_m = re.match(r'^(\d{3})x$', text)
    date_m = re.match(r'^(\d{4})(?:-\d{2})?(?:-\d{2})?$', text)
    month_year_m = re.match(r'^([a-z]{3,9})\.?\s+(\d{4})$', text)
    month_day_year_m = re.match(
        r'^([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?[,]?\s+(\d{4})$',
        text,
    )
    day_month_year_m = re.match(
        r'^(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]{3,9})\.?[,]?\s+(\d{4})$',
        text,
    )

    if range_m:
        candidate = f'{range_m.group(1)}/{range_m.group(2)}'
    elif decade_word_m:
        candidate = f'{decade_word_m.group(1)}X'
    elif decade_x_m:
        candidate = f'{decade_x_m.group(1)}X'
    elif month_year_m and month_year_m.group(1) in _MONTH_NAMES:
        candidate = f'{month_year_m.group(2)}-{_MONTH_NAMES[month_year_m.group(1)]:02d}'
    elif month_day_year_m and month_day_year_m.group(1) in _MONTH_NAMES:
        day = int(month_day_year_m.group(2))
        candidate = (
            f'{month_day_year_m.group(3)}-'
            f'{_MONTH_NAMES[month_day_year_m.group(1)]:02d}-{day:02d}'
        )
    elif day_month_year_m and day_month_year_m.group(2) in _MONTH_NAMES:
        day = int(day_month_year_m.group(1))
        candidate = (
            f'{day_month_year_m.group(3)}-'
            f'{_MONTH_NAMES[day_month_year_m.group(2)]:02d}-{day:02d}'
        )
    elif date_m:
        base = date_m.group(0)
        candidate = base

    if candidate and (date_m or month_year_m or month_day_year_m or day_month_year_m):
        if before:
            candidate = f'[..{candidate}]'
        elif approx:
            candidate = f'{candidate}~'
        elif uncertain:
            candidate = f'{candidate}?'

    if candidate and is_valid_edtf(candidate):
        return candidate
    return None


def edtf_bounds(s: str | None) -> tuple[str, str]:
    """
    Return (date_min, date_max) ISO strings for an EDTF date.

    These bounds serve two purposes:
      - Sorting: date_min is the ORDER BY column for chronological claim ordering
      - Windowing: tools can filter claims to a date range with string comparison

    Approximate dates are deliberately widened: '1840~' (about 1840) becomes
    date_min='1839-01-01', date_max='1841-12-31'.  This reflects the uncertainty.

    IMPORTANT: do not use date_min as the display year for an approximate date.
    '1840~' has date_min=1839, but the correct decade is 1840s, not 1830s.
    Always use the EDTF string directly for display and decade grouping, stripping
    the qualifier yourself.  (See views.py _decade_from_edtf for exactly this.)

    Implements the bounds table from TOOLING.md §1.
    """
    if not s or not isinstance(s, str):
        return ('0001-01-01', '9999-12-31')
    s = s.strip()

    # Interval A/B
    if '/' in s:
        parts = s.split('/', 1)
        mn = edtf_bounds(parts[0])[0]
        mx = edtf_bounds(parts[1])[1]
        return (mn, mx)

    # Before: [..YYYY] or [..YYYY-MM] or [..YYYY-MM-DD]
    before_m = re.match(r'^\[\.{2}(\d{4}(?:-\d{2})?(?:-\d{2})?)\]$', s)
    if before_m:
        return ('0001-01-01', _pad_date(before_m.group(1), 'max'))

    # Decade: 185X
    decade_m = re.match(r'^(\d{3})X$', s)
    if decade_m:
        d = decade_m.group(1)
        return (f'{d}0-01-01', f'{d}9-12-31')

    # Year only (possibly approximate)
    year_m = re.match(r'^(\d{4})([~?])?$', s)
    if year_m:
        year = int(year_m.group(1))
        if year_m.group(2):   # approximate: widen ±1 year
            return (f'{year - 1}-01-01', f'{year + 1}-12-31')
        return (f'{year}-01-01', f'{year}-12-31')

    # Year-month: 1850-05, 1850-~05, or 1850-05~ (trailing tilde also valid EDTF)
    ym_m = re.match(r'^(\d{4})-~?(\d{2})[~?]?$', s)
    if ym_m:
        year, month = int(ym_m.group(1)), int(ym_m.group(2))
        if not (1 <= month <= 12):
            raise ValueError(f'invalid month {month} in EDTF date: {s}')
        if '~' in s or '?' in s:
            mn_m = month - 1 if month > 1 else 12
            mn_y = year if month > 1 else year - 1
            mx_m = month + 1 if month < 12 else 1
            mx_y = year if month < 12 else year + 1
            return (f'{mn_y}-{mn_m:02d}-01', _last_day(mx_y, mx_m))
        return (f'{year}-{month:02d}-01', _last_day(year, month))

    # Year-month-day (possibly with ~ on components)
    ymd_m = re.match(r'^(\d{4})-~?(\d{2})-~?(\d{2})[~?]?$', s)
    if ymd_m:
        year = int(ymd_m.group(1))
        month = int(ymd_m.group(2))
        day = int(ymd_m.group(3))
        calendar.monthrange(year, month)
        if day < 1 or day > calendar.monthrange(year, month)[1]:
            raise ValueError(f'invalid day in EDTF date: {s}')
        iso = f'{year}-{month:02d}-{day:02d}'
        return (iso, iso)

    # Nothing structured matched.  Before giving up to the widest-possible window,
    # try reading it as a loose human form ("circa 1870" → "1870~") so the index
    # and timeline sort it correctly instead of dumping it at the 0001..9999 floor.
    # normalize_date never returns a loose form back, so this recurses at most once.
    normalized = normalize_date(s)
    if normalized and normalized != s:
        return edtf_bounds(normalized)

    return ('0001-01-01', '9999-12-31')


def _pad_date(s: str, mode: str) -> str:
    parts = s.split('-')
    if mode == 'max':
        if len(parts) == 1:
            return f'{parts[0]}-12-31'
        if len(parts) == 2:
            return _last_day(int(parts[0]), int(parts[1]))
        return s
    else:
        if len(parts) == 1:
            return f'{parts[0]}-01-01'
        if len(parts) == 2:
            return f'{parts[0]}-{parts[1]}-01'
        return s


def _last_day(year: int, month: int) -> str:
    last = calendar.monthrange(year, month)[1]
    return f'{year}-{month:02d}-{last:02d}'


# ── ID utilities ──────────────────────────────────────────────────────────────

ID_TYPES: frozenset[str] = frozenset('PSCLH')


def _mint_candidate(prefix: str) -> str:
    """Draw one Crockford ID candidate with the canonical uppercase type prefix."""
    body = ''.join(secrets.choice(CROCKFORD_ALPHA) for _ in range(10))
    return f'{prefix.upper()}-{body}'


def mint_ids(prefix: str, count: int, archive_root: str | Path) -> list[str]:
    """Mint fresh IDs of one type, collision-checked against the archive tree.

    ID minting is shared archive infrastructure, so it lives in `_lib.py`
    rather than in the `id` CLI module. That keeps later tools such as
    `fha process` inside the project rule that tools do not import other tools
    while still using the same Crockford alphabet and collision scan everywhere.
    """
    prefix = prefix.upper()
    if prefix not in ID_TYPES:
        raise ValueError(f'Unknown ID type: {prefix!r}. Must be one of P S C L H.')
    if count < 1:
        raise ValueError('count must be at least 1')

    existing = scan_ids_in_tree(archive_root)
    result: list[str] = []
    while len(result) < count:
        candidate = _mint_candidate(prefix)
        if candidate.lower() not in existing:
            result.append(candidate)
            existing.add(candidate.lower())
    return result


def normalize_id(id_str: str) -> str:
    """Normalize an ID to lowercase."""
    return id_str.strip().lower() if id_str else ''


def is_valid_id(id_str: str) -> bool:
    """Return True if id_str is a syntactically valid archive ID."""
    if not id_str:
        return False
    return bool(ID_RE.fullmatch(id_str.strip()))


def id_type_of(id_str: str) -> str | None:
    """Return the type prefix (P/S/C/L/H) of a valid ID, else None."""
    if is_valid_id(id_str):
        return id_str.strip()[0].upper()
    return None


def fmt_id_display(id_str: str) -> str:
    """
    Return an ID string with its type prefix uppercased (p-xxx -> P-xxx).

    The index stores all IDs lowercase (normalize_id); display output across
    the CLI uses the uppercase-prefix convention instead, so every command
    that prints an ID runs it through this first.
    """
    if not id_str:
        return id_str
    return id_str[0].upper() + id_str[1:]


def normalize_place_text(text: str | None) -> str:
    """
    Collapse a free-text place name to a comparable key: lowercase, trimmed,
    internal whitespace collapsed to single spaces.

    Used wherever two claims' `place_text` values need to be compared for
    "same place" without a shared `place_id` — e.g. "Topeka,  Kansas" and
    "topeka, kansas" should match.
    """
    return ' '.join((text or '').strip().lower().split())


def scan_ids_in_tree(archive_root: str | Path) -> set[str]:
    """
    Scan the archive tree for all ID strings (case-normalized).
    Used by id mint to verify non-existence without a built index.
    """
    root = Path(archive_root)
    found: set[str] = set()
    for path in root.rglob('*'):
        if path.is_file() and path.suffix in ('.md', '.yaml', '.yml', '.txt'):
            try:
                text = path.read_text(encoding='utf-8', errors='ignore')
                for m in ID_RE.finditer(text):
                    found.add(m.group(0).lower())
            except OSError:
                pass
    return found


# ── Filename grammar helpers ──────────────────────────────────────────────────

def is_fixture_path(path: str | Path) -> bool:
    """
    Return True if the path is under example-archive/ or tests/fixtures/.
    Files there may use status: missing-fixture (W-level, not E-level).

    Only an actual `tests/fixtures/` prefix qualifies — an arbitrary directory
    named `tests` elsewhere in a real archive is NOT fixture space.
    """
    parts = Path(path).parts
    if 'example-archive' in parts:
        return True
    return any(
        parts[i] == 'tests' and parts[i + 1] == 'fixtures'
        for i in range(len(parts) - 1)
    )


def extract_token_ids(text: str) -> list[str]:
    """Return all [ID] token values found in text (lowercased)."""
    return [m.group(1).lower() for m in TOKEN_RE.finditer(text)]


def extract_bare_ids(text: str) -> list[str]:
    """Return all bare ID values found in text (lowercased)."""
    return [m.group(0).lower() for m in ID_RE.finditer(text)]


# ── Archive freshness ─────────────────────────────────────────────────────────

def newest_record_mtime(archive_root: Path) -> float:
    """Max mtime (epoch seconds) across sources/people/notes .md files and places/places.yaml.

    Used as the freshness baseline for index.sqlite and photos.sqlite: if the
    cache is older than this, it is stale.  Returns 0.0 on a brand-new archive
    that has no record files yet (trivially up-to-date).
    """
    max_mtime = 0.0
    dirs = [archive_root / d for d in ('sources', 'people', 'notes')]
    for p in itertools.chain.from_iterable(d.rglob('*.md') for d in dirs if d.is_dir()):
        try:
            mtime = p.stat().st_mtime
            if mtime > max_mtime:
                max_mtime = mtime
        except OSError:
            pass
    for extra in (
        archive_root / 'places' / 'places.yaml',
        archive_root / 'fha.yaml',
    ):
        try:
            mtime = extra.stat().st_mtime
            if mtime > max_mtime:
                max_mtime = mtime
        except OSError:
            pass
    return max_mtime


def newest_source_record_mtime(archive_root: Path, subdir: str | None = None) -> float:
    """Max mtime (epoch seconds) across source records only.

    `photoindex` re-reads source `people:` lists to create the authoritative
    `source-people` tier, so an edit under sources/ must stale photos.sqlite
    even when no original photo file changed. Kept separate from
    newest_record_mtime so photo freshness does not react to unrelated notes or
    generated views.

    Pass `subdir` to limit the scan to a specific subdirectory under sources/
    (e.g. `'photos'`), which avoids false staleness when unrelated source types
    such as census records are edited.
    """
    max_mtime = 0.0
    sources_dir = archive_root / 'sources'
    if subdir:
        sources_dir = sources_dir / subdir
    if not sources_dir.is_dir():
        return max_mtime
    for p in sources_dir.rglob('*.md'):
        try:
            mtime = p.stat().st_mtime
            if mtime > max_mtime:
                max_mtime = mtime
        except OSError:
            pass
    return max_mtime


def newest_person_record_mtime(archive_root: Path) -> float:
    """Max mtime (epoch seconds) across person *profile* records only.

    Narrower than `newest_record_mtime`: face-tag/name matching only reads
    `face_tags`/`name_variants` from profile records, so generated companion
    files (research/timeline/sources-index/draft-queue) and folder-level
    `sources-index.md` files under people/ must not bust this freshness
    check just because `fha views refresh` touched them.
    Returns 0.0 on a brand-new archive that has no person records yet.
    """
    max_mtime = 0.0
    people_dir = archive_root / 'people'
    if not people_dir.is_dir():
        return max_mtime
    for p in people_dir.rglob('*.md'):
        parsed = parse_filename(p)
        if parsed is None or parsed['id_type'] != 'P' or parsed['kind'] != 'profile':
            continue
        try:
            mtime = p.stat().st_mtime
            if mtime > max_mtime:
                max_mtime = mtime
        except OSError:
            pass
    return max_mtime


def scan_person_record_ids(archive_root: str | Path) -> set[str]:
    """
    Return the P-id of every actual person *profile* record under people/
    (case-normalized), excluding companion files (research/timeline/
    sources-index/draft-queue) and any P-id token that merely appears in
    body text elsewhere in the archive.

    Narrower than `scan_ids_in_tree`, which matches any bare ID-shaped token
    anywhere under .md/.yaml/.yml/.txt — fine for `id mint` collision checks,
    but too permissive for validating that an ID a mutating command is about
    to write actually names a person record (a typo'd or placeholder P-id
    mentioned in a note would otherwise pass).
    """
    root = Path(archive_root)
    people_dir = root / 'people'
    if not people_dir.is_dir():
        return set()
    found: set[str] = set()
    for p in people_dir.rglob('*.md'):
        parsed = parse_filename(p)
        if parsed is not None and parsed['id_type'] == 'P' and parsed['kind'] == 'profile':
            found.add(parsed['id_str'])
    return found


# Matches the `_{S-id}.md` suffix in a source record filename; used by
# find_source_record to locate a source by its ID without trusting the slug.
_SOURCE_RECORD_FILENAME_RE = re.compile(r'_(S-[0-9a-hjkmnp-tv-z]{10})\.md$', re.I)


def find_source_record(archive_root: str | Path, source_id: str) -> dict | None:
    """Return the parsed record dict for a source by its S-id, or None.

    Globs `sources/**/*.md` for a file whose `_{S-id}.md` suffix matches
    `source_id` (case-insensitive). The slug and subdirectory are mutable and
    are not matched — only the suffix carries identity. Used by `fha photoindex`
    to resolve `source-people` person references for photos that carry a matching
    `source_id` keyword: the source record's `people:` list is the human-maintained
    statement "this source shows these people," authoritative even when no bare
    P-id keyword has been written to the image file yet.

    Returns None when the record is absent or its frontmatter has parse errors;
    callers that need `people:` should treat None as "no people known from this source."
    """
    root = Path(archive_root)
    sources_dir = root / 'sources'
    if not sources_dir.is_dir():
        return None
    sid_norm = normalize_id(source_id)
    for p in sources_dir.rglob('*.md'):
        m = _SOURCE_RECORD_FILENAME_RE.search(p.name)
        if m and normalize_id(m.group(1)) == sid_norm:
            rec = read_record(p)
            if rec.get('parse_errors'):
                return None
            return rec
    return None


def configure_utf8_stdout() -> None:
    """Reconfigure stdout to UTF-8 so ✓/✗ render on Windows cp1252 terminals."""
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8')  # type: ignore[union-attr]
        except Exception:
            pass


# ── Output helpers ────────────────────────────────────────────────────────────

EXIT_CLEAN = 0
EXIT_WARNINGS = 1
EXIT_ERRORS = 2
EXIT_FAILURE = 3


class Finding:
    """A single lint finding (error or warning)."""

    __slots__ = ('severity', 'code', 'path', 'message')

    def __init__(self, severity: str, code: str, path: str | Path, message: str):
        self.severity = severity   # 'E' or 'W'
        self.code = code           # e.g. 'E001', 'W101'
        self.path = str(path)
        self.message = message

    def __str__(self) -> str:
        return f'{self.severity} {self.code} {self.path}: {self.message}'

    def as_dict(self) -> dict:
        return {
            'severity': self.severity,
            'code': self.code,
            'path': self.path,
            'message': self.message,
        }


def emit_findings(findings: list[Finding], use_json: bool = False) -> int:
    """
    Print findings to stdout and return the appropriate exit code.

    A convenience wrapper so tool CLIs don't need to know the EXIT_* →
    severity mapping.  Tools that want custom output formatting should
    loop over findings themselves and call EXIT_* constants directly.
    """
    import json

    if use_json:
        data = [f.as_dict() for f in findings]
        print(json.dumps(data, indent=2))
    else:
        for f in findings:
            print(str(f))

    has_errors = any(f.severity == 'E' for f in findings)
    has_warnings = any(f.severity == 'W' for f in findings)

    if has_errors:
        return EXIT_ERRORS
    if has_warnings:
        return EXIT_WARNINGS
    return EXIT_CLEAN


# ── The structured-result contract ────────────────────────────────────────────
#
# See the module docstring for the full rule.  In short: `run_*` returns a
# `Result`; `_cmd_*` renders it.  These two small dataclasses are the shared
# shape every tool conforms to, so a future consumer (a generator, a console, a
# UI) can read any tool's output as data instead of re-parsing each tool's text.

# Lint findings carry a one-letter severity ('E'/'W'); the Result contract uses a
# spelled-out level so a renderer never has to know lint's private alphabet.  The
# map is exact in both directions because lint only ever emits E or W.
_SEVERITY_TO_LEVEL: dict[str, str] = {'E': 'error', 'W': 'warning'}
LEVEL_TO_SEVERITY: dict[str, str] = {'error': 'E', 'warning': 'W'}


@dataclasses.dataclass
class Message:
    """One human-facing line a Result carries.

    `level` is the severity bucket — 'error', 'warning', or 'info' — so a renderer
    can count or color without parsing prose.  `text` is the plain-language body.
    `next_step` is the exact command or action that resolves it (AGENTS.md's
    "next-step rule"); it is None for purely informational lines, and for lint
    findings whose fix is already woven into `text`.

    `code` and `path` are optional structured locators.  They exist so a lint
    `Finding` (an E/W code against a specific file) folds losslessly into this
    one shape: code carries 'W101' etc., path carries the offending file.  Tools
    with no codes or no file context leave them None.
    """

    level: str
    text: str
    next_step: str | None = None
    code: str | None = None
    path: str | None = None

    def as_dict(self) -> dict:
        return {
            'level': self.level,
            'text': self.text,
            'next_step': self.next_step,
            'code': self.code,
            'path': self.path,
        }


@dataclasses.dataclass(eq=False)
class Result:
    """The structured return value of every tool's `run_*` function.

    One small, JSON-serializable record of what an operation computed and did.
    See the module docstring for the contract this participates in.  The defaults
    describe a clean, do-nothing success, so a caller can build one up
    incrementally: `Result().add('info', 'done')` or
    `Result(data={'rows': rows})`.

    Back-compat by design.  Before this contract, tools' run_* functions returned
    one of two shapes: a payload dict (`run_xref` → {'status', 'groups'}) or a
    bare exit-code int (`run_find` → EXIT_CLEAN).  A Result stands in for both so
    every caller keeps working while run_* uniformly returns a Result:
      - dict-style read access into `data`  → `result['groups']`, `result.get(k)`
      - equality with its exit code         → `result == EXIT_CLEAN`
    That is why `__eq__` is defined here (and the dataclass uses eq=False so this
    custom one is not overwritten); two Results compare by identity, which is all
    any caller needs.
    """

    ok: bool = True
    exit_code: int = EXIT_CLEAN
    data: dict = dataclasses.field(default_factory=dict)
    messages: list[Message] = dataclasses.field(default_factory=list)
    changed: list[str] = dataclasses.field(default_factory=list)

    def __eq__(self, other: object) -> bool:
        # `result == EXIT_CLEAN` lets callers/tests that previously received a
        # bare exit-code int keep comparing against the EXIT_* constants.
        if isinstance(other, Result):
            return self is other
        if isinstance(other, int):
            return self.exit_code == other
        return NotImplemented

    def add(
        self,
        level: str,
        text: str,
        *,
        next_step: str | None = None,
        code: str | None = None,
        path: str | Path | None = None,
    ) -> 'Result':
        """Append one human-facing message; returns self so calls can chain."""
        self.messages.append(
            Message(level, text, next_step, code,
                    str(path) if path is not None else None)
        )
        return self

    def note_changed(self, path: str | Path) -> 'Result':
        """Record a file this operation created/wrote/renamed; returns self."""
        self.changed.append(str(path))
        return self

    # Dict-style read access into `data`.  Several tools' run_* functions used to
    # return a plain payload dict (e.g. `run_report` → {'status', 'markdown', …});
    # exposing `result['markdown']` / `result.get('rows')` lets those callers (and
    # their tests) keep reading the payload by key while run_* now returns a
    # Result.  Read-only on purpose — building a Result is done through its fields.
    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self.data

    def as_dict(self) -> dict:
        """Return a fully JSON-serializable view of this Result."""
        return {
            'ok': self.ok,
            'exit_code': self.exit_code,
            'data': self.data,
            'messages': [m.as_dict() for m in self.messages],
            'changed': list(self.changed),
        }


def finding_to_message(finding: Finding) -> Message:
    """Fold a lint `Finding` into a Result `Message` (severity → level).

    The fix for a lint finding is already woven into its message text (e.g.
    "... run `fha views brackets --fix` to update"), so `next_step` stays None
    rather than duplicating it.
    """
    return Message(
        level=_SEVERITY_TO_LEVEL.get(finding.severity, 'info'),
        text=finding.message,
        next_step=None,
        code=finding.code,
        path=finding.path,
    )
