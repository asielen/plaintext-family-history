#!/usr/bin/env python3
"""
doctor.py — fha doctor: archive health check.

  fha doctor [--root PATH]

Runs a structured suite of checks and prints a health report.  Safe to run on
a fresh archive before any indexes are built — absent caches contribute exit
code 1 (warning), not 2 (error).  Design decision D5, TOOLING §3a.

Checks (in order):
  1. Archive root present, fha.yaml parses              [fatal exit 2 if bad]
  2. Mapped roots (photos/, documents/, …) reachable
  3. exiftool on PATH
  4. Python deps (PyYAML)
  5. Index freshness    (.cache/index.sqlite vs newest record mtime)
  6. Photoindex freshness  (.cache/photos.sqlite vs photos root mtime)
  7. Lint summary       (E/W counts, import-and-call, no shell-out)
  8. Inbox aging        (items older than 14 days)
  9. Counts             (restricted sources, living/unknown persons)
 10. E018 findings      (agent-instruction drift details)
 11. Backup reminder    (always printed)

Exit codes: 0 = all pass; 1 = warnings only; 2 = errors.  TOOLING §3a.
"""

from __future__ import annotations

import argparse
import datetime
import os
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

try:
    import yaml  # noqa: F401 — imported for side-effect check; _lib also uses it
except ImportError:
    print(
        'ERROR: PyYAML is required but not installed. '
        'Install it with: pip install pyyaml',
        file=sys.stderr,
    )
    sys.exit(2)

from _lib import (
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    configure_utf8_stdout,
    find_archive_root,
    get_roots,
    is_fixture_path,
    load_fha_yaml,
    newest_record_mtime,
    parse_filename,
    read_record,
    resolve_path,
)

configure_utf8_stdout()

# ── CODE MAP ──────────────────────────────────────────────────────────────────
#
#  Freshness helpers (newest_record_mtime imported from _lib)
#    _fmt_delta                — format a timedelta as a readable lag string
#    _index_freshness          — .cache/index.sqlite age vs newest record
#    _photoindex_freshness     — .cache/photos.sqlite age vs photos root
#
#  Count helpers
#    _counts_from_index        — SQL queries against the fresh index
#    _counts_from_scan         — quick file walk when index is absent or stale
#
#  Top-level
#    run_doctor                — orchestrate all checks, print report, return exit code
#    register                  — attach 'doctor' to the main fha parser
#    _run_doctor               — argparse → run_doctor bridge
#    _standalone_main          — for `python tools/doctor.py` direct invocation
#
# ─────────────────────────────────────────────────────────────────────────────

_OK   = '✓'
_BAD  = '✗'
_WARN = '⚠'


# ── Freshness helpers ─────────────────────────────────────────────────────────

def _db_mtime(db_path: Path) -> float | None:
    """Return mtime of db_path, or None if absent or unreadable."""
    try:
        return db_path.stat().st_mtime
    except OSError:
        return None


def _fmt_delta(seconds: float) -> str:
    """Format a lag in seconds as 'Xh YmZs', 'YmZs', or 'Zs'."""
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f'{hours}h{minutes:02d}m{secs:02d}s'
    if minutes:
        return f'{minutes}m{secs:02d}s'
    return f'{secs}s'


def _index_freshness(archive_root: Path) -> tuple[str, str]:
    """
    Check .cache/index.sqlite against the newest record mtime.

    Returns (status, detail):
      'fresh'  → detail = ''
      'stale'  → detail = human-readable lag (e.g. '5m32s')
      'absent' → detail = ''
    """
    db_path = archive_root / '.cache' / 'index.sqlite'
    db_mtime = _db_mtime(db_path)
    if db_mtime is None:
        return ('absent', '')

    record_mtime = newest_record_mtime(archive_root)
    if record_mtime == 0.0:
        return ('fresh', '')   # no records yet — trivially up-to-date

    if db_mtime < record_mtime:
        return ('stale', _fmt_delta(record_mtime - db_mtime))

    # Mtime looks fresh — verify the schema is readable before declaring it so.
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute('SELECT 1 FROM persons LIMIT 1')
        conn.close()
    except Exception:
        return ('absent', '')   # treat corrupt/schema-less as absent

    return ('fresh', '')


def _photoindex_freshness(archive_root: Path, fha_config: dict) -> tuple[str, str]:
    """
    Check .cache/photos.sqlite against the newest file in the photos root.

    Same return shape as _index_freshness.  If the photos root doesn't exist
    (no photos yet) and the db doesn't exist either, we still report 'absent'
    because the user should know to run fha photoindex when they add photos.
    """
    db_path = archive_root / '.cache' / 'photos.sqlite'
    db_mtime = _db_mtime(db_path)
    if db_mtime is None:
        return ('absent', '')

    photos_root = resolve_path('photos', fha_config, archive_root)
    if not photos_root.is_dir():
        return ('fresh', '')   # no photos root — nothing to compare against

    max_mtime = 0.0
    for p in photos_root.rglob('*'):
        if p.is_file():
            try:
                mtime = p.stat().st_mtime
                if mtime > max_mtime:
                    max_mtime = mtime
            except OSError:
                pass

    if max_mtime == 0.0:
        return ('fresh', '')   # photos root exists but empty

    if db_mtime >= max_mtime:
        return ('fresh', '')

    return ('stale', _fmt_delta(max_mtime - db_mtime))


# ── Count helpers ─────────────────────────────────────────────────────────────

def _counts_from_index(archive_root: Path) -> dict | None:
    """
    Query restricted / living counts directly from the fresh index.
    Returns None if the index can't be opened (fall back to scan).
    """
    db_path = archive_root / '.cache' / 'index.sqlite'
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        restricted = conn.execute(
            "SELECT COUNT(*) FROM sources WHERE restricted = 1"
        ).fetchone()[0]
        row = conn.execute(
            "SELECT SUM(living='true'), SUM(living='unknown') FROM persons"
        ).fetchone()
        conn.close()
        return {
            'restricted': restricted,
            'living': row[0] or 0,
            'unknown': row[1] or 0,
        }
    except Exception:
        return None


def _counts_from_scan(archive_root: Path) -> dict:
    """
    Quick-scan counts when the index is absent or stale.  Parses only
    frontmatter of profile files (skips companion files to avoid double-counting
    person records that share a P-id with timeline/research/etc. companions).
    """
    restricted = living_true = living_unknown = 0

    sources_dir = archive_root / 'sources'
    if sources_dir.is_dir():
        for p in sources_dir.rglob('*.md'):
            rec = read_record(p)
            if rec['meta'].get('restricted') in (True, 'true'):
                restricted += 1

    people_dir = archive_root / 'people'
    if people_dir.is_dir():
        for p in people_dir.rglob('*.md'):
            parsed = parse_filename(p)
            if not parsed or parsed.get('kind') != 'profile':
                continue
            rec = read_record(p)
            living_val = str(rec['meta'].get('living', '')).lower()
            if living_val == 'true':
                living_true += 1
            elif living_val == 'unknown':
                living_unknown += 1

    return {'restricted': restricted, 'living': living_true, 'unknown': living_unknown}


# ── Main report ───────────────────────────────────────────────────────────────

def run_doctor(archive_root: Path, fha_config: dict) -> int:
    """
    Run all health checks and print a structured report.

    Returns exit code: 0 (clean), 1 (warnings), 2 (errors).
    Keeps a running worst-code so every check contributes before we return.
    """
    worst = EXIT_CLEAN

    # ── 1. Archive root + fha.yaml (already verified by caller) ────────────
    print(f'archive root: {_OK} {archive_root}')
    print(f'fha.yaml:     {_OK} loaded')
    print()

    # ── 2. Mapped roots reachable ───────────────────────────────────────────
    # Fixture archives (example-archive/, tests/) may legitimately have missing
    # roots (no photos dir yet, missing-fixture assets) — same grace given to
    # E011 in lint.  Missing roots in fixture context → warning, not error.
    roots = get_roots(fha_config)
    is_fixture = is_fixture_path(archive_root)
    if roots:
        print('mapped roots:')
        for alias in roots:
            resolved = resolve_path(alias, fha_config, archive_root)
            if os.path.isdir(resolved):
                print(f'  {alias} → {resolved}  {_OK}')
            else:
                suffix = '  (fixture — expected)' if is_fixture else '  not reachable'
                print(f'  {alias} → {resolved}  {_BAD}{suffix}')
                worst = max(worst, EXIT_WARNINGS if is_fixture else EXIT_ERRORS)
        print()

    # ── 3. exiftool on PATH ─────────────────────────────────────────────────
    exiftool_path = shutil.which('exiftool')
    if exiftool_path:
        print(f'exiftool:  {_OK} {exiftool_path}')
    else:
        print(f'exiftool:  {_BAD} not found on PATH')
        worst = max(worst, EXIT_WARNINGS)

    # ── 4. Python deps (PyYAML) ─────────────────────────────────────────────
    # yaml is imported at module level; reaching here guarantees it loaded.
    print(f'python deps (PyYAML): {_OK}')
    print()

    # ── 5. Index freshness ──────────────────────────────────────────────────
    idx_status, idx_delta = _index_freshness(archive_root)
    if idx_status == 'fresh':
        print(f'index: {_OK} fresh')
    elif idx_status == 'stale':
        print(f'index: {_WARN} stale by {idx_delta} — run fha index')
        worst = max(worst, EXIT_WARNINGS)
    else:
        print('index: not yet built — run fha index')
        worst = max(worst, EXIT_WARNINGS)

    # ── 6. Photoindex freshness ─────────────────────────────────────────────
    photo_status, photo_delta = _photoindex_freshness(archive_root, fha_config)
    if photo_status == 'fresh':
        print(f'photoindex: {_OK} fresh')
    elif photo_status == 'stale':
        print(f'photoindex: {_WARN} stale by {photo_delta} — run fha photoindex')
        worst = max(worst, EXIT_WARNINGS)
    else:
        print('photoindex: not yet built — run fha photoindex')
        worst = max(worst, EXIT_WARNINGS)
    print()

    # ── 7. Lint summary (import-and-call, no shell-out) ─────────────────────
    e018_findings: list = []
    try:
        from lint import run_lint_silent
        n_errors, n_warnings, e018_findings = run_lint_silent(archive_root, fha_config)
        symbol = _OK if n_errors == 0 else _BAD
        print(f'lint: E:{n_errors} W:{n_warnings}  {symbol}')
        if n_errors > 0:
            worst = max(worst, EXIT_ERRORS)
        elif n_warnings > 0:
            worst = max(worst, EXIT_WARNINGS)
    except Exception as exc:
        print(f'lint: {_BAD} lint machinery failed: {exc}')
        worst = max(worst, EXIT_WARNINGS)
    print()

    # ── 8. Inbox aging ──────────────────────────────────────────────────────
    inbox_dir = archive_root / 'inbox'
    if inbox_dir.is_dir():
        now = datetime.datetime.now().timestamp()
        cutoff = now - 14 * 86400
        aged: list[tuple[int, str]] = []
        for item in inbox_dir.iterdir():
            try:
                mtime = item.stat().st_mtime
                if mtime < cutoff:
                    age_days = int((now - mtime) / 86400)
                    aged.append((age_days, item.name))
            except OSError:
                pass
        if aged:
            aged.sort(reverse=True)
            oldest_days, oldest_name = aged[0]
            print(f'inbox: {len(aged)} item(s) older than 14 days '
                  f'(oldest: {oldest_name}, {oldest_days} days)')
            worst = max(worst, EXIT_WARNINGS)
        else:
            print(f'inbox: {_OK} no items older than 14 days')
        print()

    # ── 9. Counts ───────────────────────────────────────────────────────────
    if idx_status == 'fresh':
        counts = _counts_from_index(archive_root)
        if counts is None:
            counts = _counts_from_scan(archive_root)
            label = 'counts (scanned — index unreadable):'
        else:
            label = 'counts (from index):'
    else:
        counts = _counts_from_scan(archive_root)
        label = 'counts (scanned — index not fresh):'

    print(label)
    print(f'  sources restricted:  {counts["restricted"]}')
    print(f'  persons living:      {counts["living"]}')
    print(f'  persons unknown:    {counts["unknown"]}')
    print()

    # ── 10. E018 findings ───────────────────────────────────────────────────
    if e018_findings:
        print(f'E018 agent-instruction drift ({len(e018_findings)} finding(s)):')
        for f in e018_findings:
            try:
                rel = Path(f.path).relative_to(archive_root)
            except (ValueError, AttributeError):
                rel = f.path
            print(f'  {rel}: {f.message}')
    else:
        print('E018 findings: none')
    print()

    # ── 11. Backup reminder (always printed) ────────────────────────────────
    print('─' * 60)
    print('Backup policy must cover both the archive root and all mapped asset roots.')

    return worst


# ── CLI ───────────────────────────────────────────────────────────────────────

def register(subparsers: argparse._SubParsersAction) -> None:
    """Register 'doctor' onto the main fha parser."""
    p = subparsers.add_parser(
        'doctor',
        help='Archive health check — what is wrong with this archive?',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root')
    p.add_argument('--spec-root', metavar='PATH', help='Spec docs root')
    p.set_defaults(func=_run_doctor)


def _run_doctor(args: argparse.Namespace) -> int:
    root = getattr(args, 'root', None)
    if root:
        archive_root = Path(root).resolve()
    else:
        archive_root = find_archive_root()
        if archive_root is None:
            print('ERROR: cannot find archive root (no fha.yaml found). '
                  'Use --root to specify.', file=sys.stderr)
            return EXIT_FAILURE

    fha_yaml_path = archive_root / 'fha.yaml'
    if not fha_yaml_path.exists():
        print(f'ERROR: {archive_root}/fha.yaml not found — is this an archive root?',
              file=sys.stderr)
        return EXIT_ERRORS

    try:
        with open(fha_yaml_path, encoding='utf-8') as f:
            fha_config = yaml.safe_load(f) or {}
    except Exception as exc:
        print(f'ERROR: fha.yaml parse error: {exc}', file=sys.stderr)
        return EXIT_ERRORS

    return run_doctor(archive_root, fha_config)


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha doctor',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--root', metavar='PATH', help='Archive root')
    parser.add_argument('--spec-root', metavar='PATH', help='Spec docs root')
    args = parser.parse_args(argv)
    return _run_doctor(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
