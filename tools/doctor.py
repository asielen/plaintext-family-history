#!/usr/bin/env python3
"""
doctor.py - fha doctor: archive health check.

  fha doctor [--root PATH]

Runs a structured suite of checks and prints a health report.  Safe to run on
a fresh archive before any indexes are built - absent caches contribute exit
code 1 (warning), not 2 (error).  Design decision D5, TOOLING §3a.

Checks (in order):
  1. Archive root present, fha.yaml parses              [fatal exit 2 if bad]
  2. Mapped roots (photos/, documents/, …) reachable
  3. exiftool on PATH
  4. Python deps (PyYAML; Jinja2/Pillow for `fha site`)
  5. Index freshness    (.cache/index.sqlite vs newest record mtime)
  6. Photoindex freshness  (.cache/photos.sqlite vs photos root mtime)
  7. Lint summary       (E/W counts, import-and-call, no shell-out)
  8. Inbox aging        (items older than 14 days)
  8b. Staged captures   (browser-companion bundles waiting for `fha capture --ingest`)
  9. Counts             (restricted sources, living/unknown persons)
 10. E018 findings      (agent-instruction drift details)
 11. Tools version      (.plaintext-version + pending update backups)
 12. Backup reminder    (always printed)

Exit codes: 0 = all pass; 1 = warnings only; 2 = errors.  TOOLING §3a.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

try:
    import yaml  # noqa: F401 - imported for side-effect check; _lib also uses it
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
    FhaConfigError,
    INDEX_SCHEMA_VERSION,
    PHOTOINDEX_SCHEMA_VERSION,
    Result,
    configure_utf8_stdout,
    db_mtime,
    get_roots,
    is_fixture_path,
    is_working_copy,
    load_fha_yaml,
    newest_record_mtime,
    parse_filename,
    photoindex_status,
    probe_sqlite,
    read_record,
    resolve_path,
    resolve_root_arg,
    sqlite_cache_schema_status,
)

configure_utf8_stdout()

# ── CODE MAP ──────────────────────────────────────────────────────────────────
#
#  Freshness helpers (newest_record_mtime imported from _lib)
#    _fmt_delta                - format a timedelta as a readable lag string
#    _index_freshness          - .cache/index.sqlite age vs newest record
#    _photoindex_freshness     - .cache/photos.sqlite age vs photos root
#
#  Count helpers
#    _is_restricted_value      - restricted marker predicate (mirrors index.py)
#    _counts_from_index        - SQL queries against the fresh index
#    _counts_from_scan         - quick file walk when index is absent or stale
#
#  Top-level
#    run_doctor                - orchestrate all checks; return a Result (no printing)
#    _cmd_doctor               - render a doctor Result to stdout → exit code
#    register                  - attach 'doctor' to the main fha parser
#    _run_doctor               - argparse → run_doctor → _cmd_doctor bridge
#    _standalone_main          - for `python tools/doctor.py` direct invocation
#
# ─────────────────────────────────────────────────────────────────────────────

_OK   = '✓'
_BAD  = '✗'
_WARN = '⚠'


# ── Freshness helpers (db_mtime / probe_sqlite live in _lib, shared with find) ──

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
    mtime = db_mtime(db_path)
    if mtime is None:
        return ('absent', '')

    schema_status, schema_detail = sqlite_cache_schema_status(
        db_path,
        INDEX_SCHEMA_VERSION,
        ('persons', 'sources', 'claims'),
    )
    if schema_status in {'unreadable', 'old-schema'}:
        return (schema_status, schema_detail)

    record_mtime = newest_record_mtime(archive_root)
    if record_mtime == 0.0:
        return ('fresh', '')   # no records yet - trivially up-to-date

    if mtime < record_mtime:
        return ('stale', _fmt_delta(record_mtime - mtime))

    return ('fresh', '')


def _photoindex_freshness(archive_root: Path, fha_config: dict) -> tuple[str, str]:
    """
    Check .cache/photos.sqlite against the newest file in the photos root.

    Delegates to the shared _lib.photoindex_status so find and doctor agree on
    whether photos.sqlite is usable.  The shared helper probes the schema BEFORE
    the empty/missing-photo-root short-circuit, so a corrupt DB is reported
    'unreadable' rather than 'fresh'.  Returns (status, detail) with status in
    {'fresh', 'stale', 'unreadable', 'absent'}.
    """
    status, lag = photoindex_status(archive_root, fha_config)
    if status == 'stale':
        return ('stale', _fmt_delta(lag))
    return (status, '')


# ── Count helpers ─────────────────────────────────────────────────────────────

def _is_restricted_value(value) -> bool:
    """True when a `restricted:` value marks a source as restricted.

    Mirrors the predicate the index builder uses to fill sources.restricted
    (index.py `_is_restricted_value`; duplicated per tool because tools never
    import tools - TOOLING §15): the boolean `true` or any free-text type
    (`dna`, `by-request`, `deadname`, ...) all count; only an absent or
    explicitly-false value does not. The scan path must count with exactly
    these semantics or the two count paths would disagree on any archive
    that uses typed markers. (`read_record` coerces YAML booleans to the
    strings 'true'/'false'.)"""
    return value not in (None, False, '', 'false')


def _counts_from_index(archive_root: Path) -> dict | None:
    """
    Query restricted / living counts directly from the fresh index.
    Returns None if the index can't be opened (fall back to scan).
    """
    db_path = archive_root / '.cache' / 'index.sqlite'
    status, _detail = sqlite_cache_schema_status(
        db_path,
        INDEX_SCHEMA_VERSION,
        ('persons', 'sources'),
    )
    if status != 'fresh':
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
            # Same predicate the index write uses - a typed value
            # (`restricted: by-request`) counts, matching WHERE restricted = 1
            # on the index path.
            if _is_restricted_value(rec['meta'].get('restricted')):
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


# ── Tools-version check (fha install / fha update-tools, BUILD.md M9) ───────────

def _check_tools_version(archive_root: Path, lines: list[str], checks: list[dict]) -> int:
    """Report the vendored-tools version stamp and any pending update backups.

    `fha install` writes `.plaintext-version` (the manifest version + per-file
    checksums received); `fha update-tools` moves anything it can't safely
    overwrite into `.plaintext-backup/{date}/`. Both are plain artifacts this
    check reads directly rather than importing scaffold.py (tools never import
    tools). The three states a human could otherwise be stuck on:
      - absent stamp   → informational (a hand-assembled archive is fine)
      - unreadable stamp → warning, with the exact recovery command
      - pending backups  → reminder to reconcile + prune (informational)

    Per the structured-result contract the report text accumulates in `lines`
    (rendered later by `_cmd_doctor`) and the structured status lands in `checks`;
    returns the worst exit contribution (EXIT_CLEAN or EXIT_WARNINGS).
    """
    worst = EXIT_CLEAN
    stamp_path = archive_root / '.plaintext-version'
    if not stamp_path.is_file():
        lines.append(
            'tools version: not stamped (no .plaintext-version)  '
            'next: no action needed if you copied the tools by hand; '
            'or run `fha install` from a tools clone to stamp it'
        )
        checks.append({'id': 'tools_version', 'status': 'info',
                       'detail': 'not stamped', 'next_step': None})
    else:
        try:
            stamp = json.loads(stamp_path.read_text(encoding='utf-8'))
            if not isinstance(stamp, dict):
                raise ValueError(f'expected a JSON object, got {type(stamp).__name__}')
            ver = stamp.get('manifest_version', '?')
            spec = stamp.get('spec_version', '?')
            installed = stamp.get('installed', '?')
            lines.append(
                f'tools version: {_OK} manifest {ver}, spec {spec} '
                f'(installed {installed})  next: `fha update-tools --repo PATH` '
                f'to pull improvements'
            )
            checks.append({'id': 'tools_version', 'status': 'ok',
                           'detail': f'manifest {ver}, spec {spec}',
                           'next_step': 'fha update-tools --repo PATH'})
        except (ValueError, OSError) as exc:
            lines.append(
                f'tools version: {_WARN} .plaintext-version is unreadable ({exc})  '
                f'next: delete {stamp_path} and run '
                f'`fha update-tools --repo PATH` to rewrite it (your tool files '
                f'are not affected)'
            )
            checks.append({'id': 'tools_version', 'status': 'warn',
                           'detail': f'unreadable ({exc})',
                           'next_step': f'delete {stamp_path} and run fha update-tools'})
            worst = max(worst, EXIT_WARNINGS)

    backup_dir = archive_root / '.plaintext-backup'
    if backup_dir.is_dir():
        pending = sum(1 for p in backup_dir.rglob('*') if p.is_file())
        if pending:
            lines.append(
                f'update backups: {pending} file(s) saved under {backup_dir}  '
                f'next: compare them to the current tools, fold in any edits you '
                f'want to keep, then delete the backup folder'
            )
            checks.append({'id': 'update_backups', 'status': 'info',
                           'detail': f'{pending} pending', 'next_step': None})
    return worst


# ── Main report ───────────────────────────────────────────────────────────────

def _legacy_doctor_report_before_next_step_audit(archive_root: Path, fha_config: dict) -> int:
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
    # roots (no photos dir yet, missing-fixture assets) - same grace given to
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
                suffix = '  (fixture - expected)' if is_fixture else '  not reachable'
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
        print(f'index: {_WARN} stale by {idx_delta} - run fha index')
        worst = max(worst, EXIT_WARNINGS)
    else:
        print('index: not yet built - run fha index')
        worst = max(worst, EXIT_WARNINGS)

    # ── 6. Photoindex freshness ─────────────────────────────────────────────
    photo_status, photo_delta = _photoindex_freshness(archive_root, fha_config)
    if photo_status == 'fresh':
        print(f'photoindex: {_OK} fresh')
    elif photo_status == 'stale':
        print(f'photoindex: {_WARN} stale by {photo_delta} - run fha photoindex')
        worst = max(worst, EXIT_WARNINGS)
    elif photo_status == 'unreadable':
        print(f'photoindex: {_BAD} unreadable/corrupt - rebuild with fha photoindex')
        worst = max(worst, EXIT_WARNINGS)
    else:
        print('photoindex: not yet built - run fha photoindex')
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
            label = 'counts (scanned - index unreadable):'
        else:
            label = 'counts (from index):'
    else:
        counts = _counts_from_scan(archive_root)
        label = 'counts (scanned - index not fresh):'

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

def run_doctor(archive_root: Path, fha_config: dict) -> Result:
    """Run all health checks and return a structured `Result` (no printing).

    Per the structured-result contract (_lib.py), this compute layer gathers two
    things and returns them in the Result for `_cmd_doctor` to render:
      - data['lines']:  the exact report text, one entry per output line (a blank
        entry is a blank line), so the human report renders byte-for-byte as
        before - the worst-code ladder and the one-next-step-per-line voice are
        unchanged.
      - data['checks']: each check as {id, status, detail, next_step}, so a
        headless consumer can read the health report as data instead of parsing
        text.
    The 0/1/2 exit-code ladder (clean / warnings / errors) becomes the Result's
    exit_code. Doctor performs no archive mutations, so `changed` stays empty.
    """
    worst = EXIT_CLEAN
    lines: list[str] = []
    checks: list[dict] = []
    root_arg = str(archive_root)
    roots = get_roots(fha_config)
    is_fixture = is_fixture_path(archive_root)
    wc_mode = is_working_copy(archive_root)
    index_cmd = f'fha index --root "{root_arg}"'
    photoindex_cmd = f'fha photoindex --root "{root_arg}"'
    lint_cmd = f'fha lint --root "{root_arg}"'
    doctor_cmd = f'fha doctor --root "{root_arg}"'
    troubleshooting = archive_root / 'docs' / 'TROUBLESHOOTING.md'

    if wc_mode:
        lines.append(
            '[working copy] photos and documents live on the main machine - '
            'asset features are paused here'
        )
        lines.append('')
        checks.append({'id': 'working_copy', 'status': 'info',
                       'detail': 'working-copy mode active', 'next_step': None})

    lines.append(f'archive root: {_OK} {archive_root}  next: no action needed')
    lines.append(f'fha.yaml:     {_OK} {archive_root / "fha.yaml"} loaded  next: no action needed')
    lines.append('')
    checks.append({'id': 'archive_root', 'status': 'ok', 'detail': str(archive_root), 'next_step': None})
    checks.append({'id': 'fha_yaml', 'status': 'ok', 'detail': 'loaded', 'next_step': None})

    if roots:
        lines.append('mapped roots:')
        for alias in roots:
            resolved = resolve_path(alias, fha_config, archive_root)
            if os.path.isdir(resolved):
                lines.append(f'  {alias} -> {resolved}  {_OK}  next: no action needed')
                checks.append({'id': f'root:{alias}', 'status': 'ok', 'detail': str(resolved), 'next_step': None})
            elif wc_mode and alias in ('photos', 'documents'):
                lines.append(
                    f'  {alias} -> {resolved}  (not present - assumed on main machine)'
                )
                checks.append({'id': f'root:{alias}', 'status': 'info',
                               'detail': f'{resolved} absent - working-copy mode', 'next_step': None})
            elif is_fixture:
                lines.append(
                    f'  {alias} -> {resolved}  {_WARN} fixture path is missing  '
                    f'next: add fixture files or rerun `{doctor_cmd}` on a real archive'
                )
                checks.append({'id': f'root:{alias}', 'status': 'warn',
                               'detail': f'{resolved} fixture path is missing', 'next_step': doctor_cmd})
                worst = max(worst, EXIT_WARNINGS)
            else:
                lines.append(
                    f'  {alias} -> {resolved}  {_BAD} not reachable  '
                    f'next: fix roots in {archive_root / "fha.yaml"} or create that folder, '
                    f'then run `{doctor_cmd}`'
                )
                checks.append({'id': f'root:{alias}', 'status': 'error',
                               'detail': f'{resolved} not reachable', 'next_step': doctor_cmd})
                worst = max(worst, EXIT_ERRORS)
        lines.append('')

    exiftool_path = shutil.which('exiftool')
    if exiftool_path:
        lines.append(f'exiftool:  {_OK} {exiftool_path}  next: no action needed')
        checks.append({'id': 'exiftool', 'status': 'ok', 'detail': exiftool_path, 'next_step': None})
    else:
        lines.append(
            f'exiftool:  {_WARN} not found on PATH  next: install exiftool, '
            f'then run `{doctor_cmd}`'
        )
        checks.append({'id': 'exiftool', 'status': 'warn', 'detail': 'not found on PATH', 'next_step': doctor_cmd})
        worst = max(worst, EXIT_WARNINGS)
    lines.append(f'python deps (PyYAML): {_OK}  next: no action needed')
    checks.append({'id': 'pyyaml', 'status': 'ok', 'detail': 'installed', 'next_step': None})

    # Publication deps (fha site). Jinja2 is required for `fha site`, like
    # exiftool is for photos - its absence is a warning, not a hard error,
    # because the rest of the suite runs without it. Pillow is purely optional
    # (standalone-site image derivatives) so its absence is informational only.
    import importlib.util as _ilu
    if _ilu.find_spec('jinja2') is not None:
        lines.append(f'jinja2 (fha site): {_OK}  next: no action needed')
        checks.append({'id': 'jinja2', 'status': 'ok', 'detail': 'installed', 'next_step': None})
    else:
        lines.append(
            f'jinja2 (fha site): {_WARN} not installed  '
            'next: `python -m pip install jinja2` to build the family website'
        )
        checks.append({'id': 'jinja2', 'status': 'warn', 'detail': 'not installed',
                       'next_step': 'python -m pip install jinja2'})
        worst = max(worst, EXIT_WARNINGS)
    if _ilu.find_spec('PIL') is not None:
        lines.append(f'pillow (fha site images): {_OK}  next: no action needed')
        checks.append({'id': 'pillow', 'status': 'ok', 'detail': 'installed', 'next_step': None})
    else:
        lines.append(
            'pillow (fha site images): not installed (optional)  '
            'next: `python -m pip install pillow` for photos in the standalone site'
        )
        checks.append({'id': 'pillow', 'status': 'info', 'detail': 'not installed (optional)',
                       'next_step': 'python -m pip install pillow'})
    lines.append('')

    idx_status, idx_delta = _index_freshness(archive_root)
    idx_path = archive_root / '.cache' / 'index.sqlite'
    if idx_status == 'fresh':
        lines.append(f'index: {_OK} fresh at {idx_path}  next: no action needed')
        checks.append({'id': 'index', 'status': 'ok', 'detail': 'fresh', 'next_step': None})
    elif idx_status == 'stale':
        lines.append(f'index: {_WARN} stale by {idx_delta} at {idx_path}  next: run `{index_cmd}`')
        checks.append({'id': 'index', 'status': 'warn', 'detail': f'stale by {idx_delta}', 'next_step': index_cmd})
        worst = max(worst, EXIT_WARNINGS)
    elif idx_status in {'unreadable', 'old-schema'}:
        detail = f' ({idx_delta})' if idx_delta else ''
        lines.append(
            f'index: {_WARN} search index is out of date or unreadable{detail}: '
            f'{idx_path}  next: run `{index_cmd}`'
        )
        checks.append({'id': 'index', 'status': 'warn',
                       'detail': f'out of date or unreadable{detail}', 'next_step': index_cmd})
        worst = max(worst, EXIT_WARNINGS)
    else:
        lines.append(f'index: {_WARN} not yet built at {idx_path}  next: run `{index_cmd}`')
        checks.append({'id': 'index', 'status': 'warn', 'detail': 'not yet built', 'next_step': index_cmd})
        worst = max(worst, EXIT_WARNINGS)

    photo_status, photo_delta = _photoindex_freshness(archive_root, fha_config)
    photo_path = archive_root / '.cache' / 'photos.sqlite'
    if wc_mode and photo_status in {'unreadable', 'old-schema', 'stale'}:
        if photo_status == 'stale':
            label = f'stale by {photo_delta}'
        elif photo_status == 'old-schema':
            label = 'out of date'
        else:
            label = 'unreadable'
        lines.append(
            f'photoindex: {_WARN} {label}: {photo_path}'
            f'  next: copy a fresh cache from the main machine'
        )
        checks.append({'id': 'photoindex', 'status': 'warn', 'detail': label,
                       'next_step': 'copy cache from main machine'})
        worst = max(worst, EXIT_WARNINGS)
    elif wc_mode:
        lines.append(
            f'photoindex: (paused in working-copy mode - run `{photoindex_cmd}` on the main machine)'
        )
        checks.append({'id': 'photoindex', 'status': 'info',
                       'detail': 'paused - working-copy mode', 'next_step': None})
    elif photo_status == 'fresh':
        lines.append(f'photoindex: {_OK} fresh at {photo_path}  next: no action needed')
        checks.append({'id': 'photoindex', 'status': 'ok', 'detail': 'fresh', 'next_step': None})
    elif photo_status == 'stale':
        lines.append(f'photoindex: {_WARN} stale by {photo_delta} at {photo_path}  next: run `{photoindex_cmd}`')
        checks.append({'id': 'photoindex', 'status': 'warn', 'detail': f'stale by {photo_delta}', 'next_step': photoindex_cmd})
        worst = max(worst, EXIT_WARNINGS)
    elif photo_status in {'unreadable', 'old-schema'}:
        label = 'out of date' if photo_status == 'old-schema' else 'unreadable'
        lines.append(f'photoindex: {_WARN} {label}: {photo_path}  next: run `{photoindex_cmd}`')
        checks.append({'id': 'photoindex', 'status': 'warn', 'detail': label, 'next_step': photoindex_cmd})
        worst = max(worst, EXIT_WARNINGS)
    else:
        lines.append(f'photoindex: {_WARN} not yet built at {photo_path}  next: run `{photoindex_cmd}`')
        checks.append({'id': 'photoindex', 'status': 'warn', 'detail': 'not yet built', 'next_step': photoindex_cmd})
        worst = max(worst, EXIT_WARNINGS)
    lines.append('')

    e018_findings: list = []
    try:
        from lint import run_lint_silent
        n_errors, n_warnings, e018_findings = run_lint_silent(archive_root, fha_config)
        symbol = _OK if n_errors == 0 else _BAD
        action = 'no action needed' if n_errors == 0 and n_warnings == 0 else f'run `{lint_cmd}` for details'
        lines.append(f'lint: E:{n_errors} W:{n_warnings}  {symbol}  next: {action}')
        checks.append({'id': 'lint', 'status': 'ok' if n_errors == 0 else 'error',
                       'detail': f'E:{n_errors} W:{n_warnings}',
                       'next_step': None if (n_errors == 0 and n_warnings == 0) else lint_cmd})
        if n_errors > 0:
            worst = max(worst, EXIT_ERRORS)
        elif n_warnings > 0:
            worst = max(worst, EXIT_WARNINGS)
    except Exception as exc:
        lines.append(
            f'lint: {_BAD} lint machinery failed: {exc}  '
            f'next: run `{lint_cmd}`; if it still fails see {troubleshooting}'
        )
        checks.append({'id': 'lint', 'status': 'warn', 'detail': f'machinery failed: {exc}', 'next_step': lint_cmd})
        worst = max(worst, EXIT_WARNINGS)
    lines.append('')

    inbox_dir = archive_root / 'inbox'
    if inbox_dir.is_dir():
        now = datetime.datetime.now().timestamp()
        cutoff = now - 14 * 86400
        aged: list[tuple[int, Path]] = []
        for item in inbox_dir.iterdir():
            try:
                mtime = item.stat().st_mtime
                if mtime < cutoff:
                    age_days = int((now - mtime) / 86400)
                    aged.append((age_days, item))
            except OSError:
                pass
        if aged:
            aged.sort(reverse=True)
            oldest_days, oldest_path = aged[0]
            lines.append(
                f'inbox: {len(aged)} item(s) older than 14 days '
                f'(oldest: {oldest_path}, {oldest_days} days)  '
                f'next: preview filing with `fha process "{oldest_path}" --root "{root_arg}" --dry-run`'
            )
            checks.append({'id': 'inbox', 'status': 'warn',
                           'detail': f'{len(aged)} item(s) older than 14 days', 'next_step': 'fha process'})
            worst = max(worst, EXIT_WARNINGS)
        else:
            lines.append(f'inbox: {_OK} no items older than 14 days  next: no action needed')
            checks.append({'id': 'inbox', 'status': 'ok', 'detail': 'no aged items', 'next_step': None})
        lines.append('')

    # ── Staged captures waiting to be ingested ───────────────────────────────
    # The browser companion drops bundles in a Downloads-tree staging folder
    # (TOOLING_INGESTION §6); nothing sweeps them automatically. Only surface
    # this when the folder exists at all (most machines never run the companion).
    # Guarded like the lint import above: doctor is the tool a human reaches
    # for when something is broken, so a broken/missing capture.py (a partial
    # tools update, say) must degrade this one check to a warning line, never
    # kill the whole health report.
    staging_dir = None
    pending: list = []
    try:
        from capture import staged_bundles
        staging_dir, pending = staged_bundles(fha_config)
    except Exception as exc:
        lines.append(
            f'staged captures: {_WARN} check skipped ({exc})  '
            f'next: if you use the browser companion, run '
            f'`fha capture --ingest --root "{root_arg}"` by hand; '
            f'otherwise no action needed'
        )
        lines.append('')
        checks.append({'id': 'staged-captures', 'status': 'warn',
                       'detail': f'check skipped: {exc}',
                       'next_step': f'fha capture --ingest --root "{root_arg}"'})
        worst = max(worst, EXIT_WARNINGS)
    if staging_dir is not None and staging_dir.is_dir():
        ingest_cmd = f'fha capture --ingest --root "{root_arg}"'
        if pending:
            lines.append(
                f'staged captures: {len(pending)} bundle(s) in {staging_dir} '
                f'waiting to be filed  next: run `{ingest_cmd}`'
            )
            checks.append({'id': 'staged-captures', 'status': 'warn',
                           'detail': f'{len(pending)} bundle(s) waiting',
                           'next_step': 'fha capture --ingest'})
            worst = max(worst, EXIT_WARNINGS)
        else:
            lines.append(f'staged captures: {_OK} none waiting  next: no action needed')
            checks.append({'id': 'staged-captures', 'status': 'ok',
                           'detail': 'none waiting', 'next_step': None})
        lines.append('')

    if idx_status == 'fresh':
        counts = _counts_from_index(archive_root)
        if counts is None:
            counts = _counts_from_scan(archive_root)
            label = 'counts (scanned - index unreadable):'
        else:
            label = 'counts (from index):'
    else:
        counts = _counts_from_scan(archive_root)
        label = 'counts (scanned - index not fresh):'

    lines.append(label)
    lines.append(f'  sources restricted:  {counts["restricted"]}')
    lines.append(f'  persons living:      {counts["living"]}')
    lines.append(f'  persons unknown:     {counts["unknown"]}')
    lines.append(f'  next: run `{index_cmd}` if these counts look wrong')
    lines.append('')
    checks.append({'id': 'counts', 'status': 'info', 'detail': counts, 'next_step': None})

    if e018_findings:
        lines.append(f'E018 agent-instruction drift ({len(e018_findings)} finding(s)):')
        for f in e018_findings:
            try:
                rel = Path(f.path).relative_to(archive_root)
            except (ValueError, AttributeError):
                rel = f.path
            lines.append(f'  {rel}: {f.message}')
        lines.append(f'  next: run `{lint_cmd}` and repair the listed instruction files')
        checks.append({'id': 'e018', 'status': 'warn', 'detail': f'{len(e018_findings)} finding(s)', 'next_step': lint_cmd})
    else:
        lines.append('E018 findings: none  next: no action needed')
        checks.append({'id': 'e018', 'status': 'ok', 'detail': 'none', 'next_step': None})
    lines.append('')

    # ── 11. Tools version (fha install / fha update-tools footprints) ────────
    # Self-contained reads (tools never import tools): .plaintext-version and
    # .plaintext-backup/ are plain JSON / a folder. Surfaces the two new states
    # the scaffolding tools can leave behind so a human is never stuck wondering.
    worst = max(worst, _check_tools_version(archive_root, lines, checks))
    lines.append('')

    lines.append('-' * 60)
    lines.append('Backup policy must cover both the archive root and all mapped asset roots.')
    lines.append(f'Archive root: {archive_root}')
    for alias in roots:
        lines.append(f'Asset root {alias}: {resolve_path(alias, fha_config, archive_root)}')
    lines.append(f'Next: make sure those paths are included in your backup. More help: {troubleshooting}')

    return Result(
        ok=(worst not in (EXIT_ERRORS, EXIT_FAILURE)),
        exit_code=worst,
        data={'checks': checks, 'lines': lines, 'counts': counts},
    )


def _cmd_doctor(result: Result) -> int:
    """Render a doctor Result to stdout and return its exit code.

    The only layer that prints the health report; the line buffer in
    data['lines'] reproduces the historical output byte-for-byte.
    """
    print('\n'.join(result.data['lines']))
    return result.exit_code


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register 'doctor' onto the main fha parser."""
    p = subparsers.add_parser(
        'doctor',
        help='Archive health check - what is wrong with this archive?',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root')
    p.add_argument('--spec-root', metavar='PATH', help='Spec docs root')
    p.set_defaults(func=_run_doctor)


def _run_doctor(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE

    fha_yaml_path = archive_root / 'fha.yaml'
    if not fha_yaml_path.exists():
        print(f'ERROR: {archive_root}/fha.yaml not found - is this an archive root?',
              file=sys.stderr)
        return EXIT_ERRORS

    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as exc:
        print(f'ERROR: fha.yaml: {exc}', file=sys.stderr)
        return EXIT_ERRORS

    return _cmd_doctor(run_doctor(archive_root, fha_config))


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
