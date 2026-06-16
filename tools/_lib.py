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
"""

from __future__ import annotations

import calendar
import datetime
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml

# ── CODE MAP ──────────────────────────────────────────────────────────────────
#
#  Constants and patterns
#    CROCKFORD_ALPHA           — the 32-char ID alphabet (i l o u omitted)
#    ID_RE, TOKEN_RE           — bare ID and [ID] token patterns (SPEC §10)
#    FRONT_RE, CLAIMS_RE       — frontmatter and fenced claims block patterns
#    SIGNIFICANCE              — claim type → 'vital'/'substantive'/'incidental'
#    CLAIM_TYPES, VITAL_TYPES  — frozensets derived from SIGNIFICANCE
#    SOURCE_TYPES              — controlled vocabulary for source_type field
#    COMPANION_KINDS           — generated file kinds that share a P-id with their profile
#
#  Archive configuration
#    find_archive_root         — walk up from CWD to find fha.yaml
#    load_fha_yaml             — parse fha.yaml into a dict
#    get_roots                 — extract roots mapping from config
#    resolve_path              — alias path ('photos/…') → absolute Path via fha.yaml
#
#  Record parsing
#    _coerce_yaml              — normalise YAML scalar types for consistent comparisons
#    read_record               — parse frontmatter + claims + body from a .md file
#    parse_filename            — decompose filename into {id_str, kind, is_companion}
#
#  EDTF handling
#    is_valid_edtf             — validate an EDTF string against this project's subset
#    edtf_bounds               — compute (date_min, date_max) ISO strings
#    _pad_date, _last_day      — internal date-padding helpers
#
#  ID utilities
#    normalize_id              — lowercase for consistent set/dict keying
#    is_valid_id               — syntactic validity check
#    id_type_of                — extract P/S/C/L/H type prefix
#    scan_ids_in_tree          — full-tree scan used by id mint for collision checking
#
#  Filename / path helpers
#    is_fixture_path           — path under example-archive/ or tests/fixtures/?
#    extract_token_ids         — all [ID] tokens from a text block
#    extract_bare_ids          — all bare IDs from a text block
#
#  Archive freshness
#    newest_record_mtime       — max mtime of sources/people/notes .md + places.yaml
#    configure_utf8_stdout     — reconfigure stdout to UTF-8 (Windows cp1252 compat)
#
#  Output helpers
#    EXIT_CLEAN / EXIT_WARNINGS / EXIT_ERRORS / EXIT_FAILURE  — shared exit codes
#    Finding                   — one lint finding: severity + code + path + message
#    emit_findings             — print findings list and return exit code
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

# Companion file kinds: generated view files that share a P-id with their profile
# and live in the same folder.  Enumerated here so that parse_filename (kind
# detection) and index.py (person_files.kind column) stay in sync when new view
# types are added — add the kind here, and both consumers pick it up automatically.
COMPANION_KINDS: frozenset[str] = frozenset({'research', 'timeline', 'sources-index', 'draft-queue'})

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


def load_fha_yaml(archive_root: str | Path) -> dict:
    """Load fha.yaml; return the parsed dict (empty dict on missing/error)."""
    path = Path(archive_root) / 'fha.yaml'
    if not path.exists():
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
        return data
    except Exception:
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
            raw_meta = yaml.safe_load(fm_match.group(1)) or {}
            meta = _coerce_yaml(raw_meta)
        except yaml.YAMLError as e:
            errors.append(('E010', f'Frontmatter YAML error: {e}'))
        body = text[fm_match.end():]

    # Claims block
    claims: list[dict] = []
    cm_match = CLAIMS_RE.search(body)
    if cm_match:
        try:
            raw_claims = yaml.safe_load(cm_match.group(1))
            if raw_claims is None:
                raw_claims = []
            if isinstance(raw_claims, list):
                claims = [_coerce_yaml(c) for c in raw_claims if c is not None]
            else:
                errors.append(('E010', 'Claims block is not a YAML list'))
        except yaml.YAMLError as e:
            errors.append(('E010', f'Claims YAML error: {e}'))

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
    """
    parts = Path(path).parts
    return any(
        p in ('example-archive', 'tests') for p in parts
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
    for d_name in ('sources', 'people', 'notes'):
        d = archive_root / d_name
        if d.is_dir():
            for p in d.rglob('*.md'):
                try:
                    mtime = p.stat().st_mtime
                    if mtime > max_mtime:
                        max_mtime = mtime
                except OSError:
                    pass
    places_yaml = archive_root / 'places' / 'places.yaml'
    if places_yaml.exists():
        try:
            mtime = places_yaml.stat().st_mtime
            if mtime > max_mtime:
                max_mtime = mtime
        except OSError:
            pass
    return max_mtime


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
