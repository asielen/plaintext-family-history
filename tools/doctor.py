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
  4. Python deps (PyYAML; Jinja2/Pillow for `fha site`; pypdf for `fha source extract`)
  5. Index freshness    (.cache/index.sqlite vs newest record mtime)
  6. Photoindex freshness  (.cache/photos.sqlite vs photos root mtime)
  7. Lint summary       (E/W counts, import-and-call, no shell-out)
  8. Inbox aging        (items older than 14 days)
  8b. Staged captures   (browser-companion bundles waiting for `fha capture --ingest`)
  9. Counts             (restricted sources, living/unknown persons)
 10. E018 findings      (agent-instruction drift details)
 11. Tools version      (.plaintext-version + pending update backups)
 12. Backup recency     (reads .cache/last_backup.json, the `fha backup` stamp;
                         always printed, info-level - never changes the exit code)
 13. Sources swallowed by .gitignore (#57: an unanchored pattern like `photos/`
                         also matches `sources/photos/`, silently untracking
                         SOURCE RECORDS, not the binary asset it was meant for;
                         asks `git check-ignore`, never a hand-rolled parser)

Exit codes: 0 = all pass; 1 = warnings only; 2 = errors.  TOOLING §3a.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import shlex
import subprocess
import os
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

try:
    import yaml  # noqa: F401 - imported for side-effect check; _lib also uses it
except ImportError:
    # Built inline rather than via `_lib.pip_command`: _lib imports yaml too, so
    # at this point it is exactly the module that cannot be loaded. Same rule
    # though - name the interpreter that is actually short of the package, or the
    # fix can land in a different one and the retry fails identically.
    _exe = f'"{sys.executable}"' if ' ' in sys.executable else sys.executable
    print(
        'ERROR: PyYAML is required but not installed. '
        f'Install it with: {_exe} -m pip install pyyaml',
        file=sys.stderr,
    )
    sys.exit(2)

from _lib import (
    configure_utf8_stdout,
    db_mtime,
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    FhaConfigError,
    format_roots_orphan_warning,
    get_roots,
    INDEX_SCHEMA_VERSION,
    is_fixture_path,
    is_working_copy,
    load_fha_yaml,
    newest_record_mtime,
    parse_filename,
    PHOTOINDEX_SCHEMA_VERSION,
    photoindex_status,
    pip_command,
    probe_sqlite,
    read_record,
    resolve_path,
    resolve_root_arg,
    Result,
    roots_change_orphans,
    sqlite_cache_schema_status,
    unreadable_dir_recorder,
    VENDOR_DIR,
    walk_files,)

configure_utf8_stdout()

# ── CODE MAP ──────────────────────────────────────────────────────────────────
#
#  Freshness helpers (newest_record_mtime imported from _lib)
#    _fmt_delta                - format a timedelta as a readable lag string
#    _unreadable_record_dirs   - WHICH record folder is holding the index stale
#    _index_freshness          - .cache/index.sqlite age vs newest record
#    _photoindex_freshness     - .cache/photos.sqlite age vs photos root
#
#  Count helpers
#    _is_restricted_value      - restricted marker predicate (mirrors index.py)
#    _counts_from_index        - SQL queries against the fresh index
#    _counts_from_scan         - quick file walk when index is absent or stale
#
#  Git-asked checks (never a hand-rolled parser - precedent commit 0f92e0a)
#    _check_sources_gitignore  - #57: is anything under sources/ git-ignored?
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

# How to spell the launcher in a command meant to be copied and run as it stands.
#
# `fha` is a FILE at the archive root - tools/scaffold.py ships `fha` (POSIX sh)
# and `fha.cmd` and nothing else - never a program on PATH, so a bare `fha …`
# is a command-not-found for everyone except a Windows Command Prompt user.
# Doctor's next steps are the report's whole point: they are already filled in
# with this archive's --root and are meant to be pasted back, so each one
# carries the prefix this machine's shell needs.
#
# Windows gets the PowerShell spelling because cmd.exe resolves a
# path-qualified `.\fha` through PATHEXT exactly as it resolves the bare name,
# while PowerShell refuses the bare form - `.\fha` strands nobody. This is the
# convention commit 7c6ee13 settled for the browser companion's copy card and
# f1a246d reused for transcribe-audio's attach line; GETTING_STARTED.md and
# CHEATSHEET.md carry the same two spellings plus the bare cmd.exe one.
_LAUNCHER = '.\\fha' if os.name == 'nt' else './fha'


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


def _unreadable_record_dirs(archive_root: Path) -> list[str]:
    """Record folders this machine cannot list, named as the human filed them.

    `newest_record_mtime` holds the index behind a folder it could not open -
    correctly, since records nobody read may have changed - but it reports one
    number and cannot say which folder. That leaves `fha doctor` printing
    "index is stale, run fha index" for a staleness `fha index` cannot clear:
    the human runs it, it stays stale, and the tool that exists to explain the
    archive to him has nothing more to say. So doctor asks the same question
    itself and names the folders.

    Deliberately walked only when something already looks wrong (see
    `run_doctor`) - this is a diagnosis, not a routine cost - and it reads
    exactly the three record trees the watermark reads, so the two agree about
    what "a record folder" means.
    """
    unreadable: list[Path] = []
    on_error = unreadable_dir_recorder(unreadable)
    for name in ('sources', 'people', 'notes'):
        for _ in walk_files(archive_root / name, suffix='.md', on_error=on_error):
            pass
    shown = []
    for path in unreadable:
        try:
            shown.append(path.relative_to(archive_root).as_posix())
        except ValueError:
            shown.append(str(path).replace('\\', '/'))
    return sorted(shown)


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


# ── Sources swallowed by .gitignore (#57) ───────────────────────────────────

def _check_sources_gitignore(archive_root: Path, roots: dict,
                              lines: list[str], checks: list[dict]) -> int:
    """Ask git whether an unanchored `.gitignore` pattern is untracking sources/.

    #57: a `.gitignore` pattern without a leading slash (`photos/`) matches a
    directory of that name at ANY depth, so it also catches `sources/photos/` -
    the SOURCE RECORDS (.md files carrying claims) that document each photo,
    not the binary asset the pattern was meant for. Nothing else surfaces
    this: `fha lint` is clean, `git status` shows nothing, the files read
    fine on disk. An archive made from a template carrying that mistake
    silently drops its photo (or document, or inbox-named) source records
    from version control forever, and `fha update-tools` never touches a
    committed `.gitignore` - it is the archive's own file - so an archive
    already exposed stays exposed until something tells the owner.

    ASK GIT, do not reimplement it - same reasoning as the .gitattributes
    check below (precedent commit 0f92e0a): three hand-rolled parsers in
    this codebase's history were each wrong in a different way, and `git
    check-ignore` already implements precedence, negation and anchoring
    correctly. Probes every mapped-root alias name (`roots:` keys - normally
    `photos`/`documents`, the exact names the bug collides with) as a
    sources/ subfolder, plus every subfolder that actually exists under
    sources/ today, so a custom source_type folder name is covered too. One
    batched `git check-ignore -v` call answers for every probe at once - it
    exits 0 if ANY probe is ignored, 1 if none are, and either way prints one
    line per ignored probe naming the exact pattern and line number that did it.
    """
    sources_dir = archive_root / 'sources'
    probe_names: set[str] = set(roots.keys()) if isinstance(roots, dict) else set()
    if sources_dir.is_dir():
        probe_names.update(p.name for p in sources_dir.iterdir() if p.is_dir())
    if not probe_names:
        return EXIT_CLEAN

    probes = [f'sources/{name}/probe.md' for name in sorted(probe_names)]
    try:
        out = subprocess.run(
            ['git', 'check-ignore', '-v', '--'] + probes,
            cwd=str(archive_root), capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return EXIT_CLEAN          # no usable git: this check cannot answer, so it says nothing

    if out.returncode not in (0, 1):
        return EXIT_CLEAN          # not a git repo (or another answer git can't give) - say nothing

    # -v prints one "source:line:pattern\tpath" line per IGNORED probe and
    # nothing for the rest, so the exit code alone can't tell us which
    # sources/ subfolder (if any) is affected - parse the matched lines.
    findings: list[tuple[str, str, str, str]] = []
    for line in out.stdout.splitlines():
        if not line.strip() or '\t' not in line:
            continue
        rule, path = line.split('\t', 1)
        m = re.match(r'^(?P<file>.+):(?P<lineno>\d+):(?P<pattern>.*)$', rule)
        if not m:
            continue
        findings.append((path, m.group('file'), m.group('lineno'), m.group('pattern')))

    if not findings:
        return EXIT_CLEAN

    for path, gi_file, lineno, pattern in findings:
        rel_dir = path[:-len('/probe.md')] if path.endswith('/probe.md') else path
        anchored = pattern if pattern.startswith('/') else f'/{pattern}'
        lines.append(
            f'sources ignored: {rel_dir}/ is caught by {gi_file}:{lineno} '
            f'(pattern `{pattern}`)  next: anchor it to the archive root - change '
            f'`{pattern}` to `{anchored}` in {gi_file} so it stops matching {rel_dir}/'
        )
    checks.append({
        'id': 'sources_gitignore', 'status': 'warn',
        'detail': f'{len(findings)} sources/ subfolder(s) ignored: '
                  + ', '.join(f[0].rsplit("/probe.md", 1)[0] for f in findings),
        'next_step': 'anchor the offending .gitignore pattern(s) with a leading slash',
    })
    return EXIT_WARNINGS


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
        # `fha install` REFUSES an unstamped archive that still holds a flat
        # tools/fha.py, because installing beside it would leave those files
        # in place but unused. Advising it anyway would send the owner to a
        # command guaranteed to refuse - so say what actually works instead.
        legacy_tools = (archive_root / 'tools' / 'fha.py').is_file()
        vendored = (archive_root / VENDOR_DIR / 'tools').is_dir()
        if legacy_tools and not vendored:
            lines.append(
                'tools version: not stamped, and tools/ sits at the archive root '
                '(the tools now live under .fha/)  '
                'next: move or delete the old tools/ folder, copying across any '
                'edits you made to it, then run `fha install` - it refuses while '
                'both could be present'
            )
            checks.append({'id': 'tools_version', 'status': 'info',
                           'detail': 'not stamped; legacy flat tools/',
                           'next_step': 'reconcile tools/ by hand, then install'})
        else:
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


# ── Backup recency (fha backup stamp, TOOLING §13e) ─────────────────────────────

def _check_backup_stamp(archive_root: Path, lines: list[str], checks: list[dict],
                        backup_cmd: str) -> None:
    """Report the last `fha backup` run from `.cache/last_backup.json`.

    The stamp is a per-copy, per-machine fact (the TOOLING §13d rationale that
    keeps WORKING_COPY out of fha.yaml), written by `fha backup` after a
    verified zip and read here as a plain artifact (tools never import tools).
    Three states, all deliberately info-level with a CLEAN exit contribution -
    a reminder that turns every fresh archive's doctor red trains people to
    ignore red (the same alarm-blindness reasoning as the detectors' exit-0
    posture, TOOLING §14a):
      - stamp present  → the real date, age, zip path, and whether assets rode
        along (a timezone-aware date - a hand-edit or a foreign tool's stamp -
        is converted to local time rather than crashing the arithmetic)
      - stamp absent   → an honest "no backup recorded" naming the command
      - stamp unreadable → treated as absent (the cache is disposable; the next
        backup rewrites it) with the cause shown; any parse or date-arithmetic
        failure lands here, never as an uncaught exception
      - stamp says `complete: false` → the date, plus the plain fact that the
        zip is missing folders `fha backup` could not read at the time (only
        an --allow-incomplete run can write such a stamp). Still info-level:
        the human asked for that zip, and telling him what he has is the job -
        an older stamp without the key was complete and is read that way.
    A restored archive has no `.cache/`, so it lands on "no backup recorded"
    and prompts a fresh one - correct for a copy that just survived a disaster.
    """
    stamp_path = archive_root / '.cache' / 'last_backup.json'
    if not stamp_path.is_file():
        lines.append(
            f'last backup: none recorded  next: run `{backup_cmd}` - it writes a '
            f'dated zip beside your archive (restore = unzip)'
        )
        checks.append({'id': 'backup', 'status': 'info',
                       'detail': 'no backup recorded', 'next_step': backup_cmd})
        return
    try:
        stamp = json.loads(stamp_path.read_text(encoding='utf-8'))
        if not isinstance(stamp, dict):
            raise ValueError(f'expected a JSON object, got {type(stamp).__name__}')
        when = datetime.datetime.fromisoformat(str(stamp['date']))
        if when.tzinfo is not None:
            # fha backup writes naive local times, but a hand-edited or
            # foreign stamp may carry a timezone. Convert to local naive so
            # the age subtraction never mixes aware and naive datetimes
            # (that mix raises TypeError, and an uncaught one would kill
            # the whole doctor run over a disposable cache file).
            when = when.astimezone().replace(tzinfo=None)
        age_days = (datetime.datetime.now() - when).days
    except (KeyError, ValueError, OSError, TypeError, OverflowError) as exc:
        lines.append(
            f'last backup: the note at {stamp_path} is unreadable ({exc}) - '
            f'treating it as none recorded  next: run `{backup_cmd}` to write a '
            f'fresh backup (and a fresh note)'
        )
        checks.append({'id': 'backup', 'status': 'info',
                       'detail': f'stamp unreadable ({exc})', 'next_step': backup_cmd})
        return
    if age_days <= 0:
        age = 'today'
    elif age_days == 1:
        age = '1 day ago'
    else:
        age = f'{age_days} days ago'
    scope = ('assets included' if stamp.get('assets_included')
             else 'records only - photos and documents not included')
    zip_name = stamp.get('zip', '?')
    detail = f'{when.date().isoformat()} ({age}) -> {zip_name} ({scope})'
    # `fha backup` refuses to write a zip it could not fill, so the only way
    # `complete: false` reaches this stamp is the human's own
    # --allow-incomplete. He is owed the reminder anyway: this is the line he
    # reads to decide whether he is covered, and "last backup: 3 days ago" over
    # a zip missing half the photos is the same false comfort one layer out.
    # An older stamp (written before the key existed) has no opinion and is
    # treated as complete, which is what it was.
    if stamp.get('complete') is False:
        missing = stamp.get('unreadable_dirs') or []
        named = f' ({", ".join(str(m) for m in missing[:3])})' if missing else ''
        lines.append(
            f'last backup: {detail}  INCOMPLETE - folder(s) could not be read '
            f'when it was made{named}, so they are not in that zip  next: run '
            f'`{backup_cmd}` once they can be read again for a complete one')
        # Info-level like the other three states (this check never moves the
        # exit code - see the docstring): the human chose this backup
        # knowingly, and the line says so in words where he will read it.
        checks.append({'id': 'backup', 'status': 'info',
                       'detail': f'{detail} - incomplete',
                       'next_step': backup_cmd})
        return
    lines.append(f'last backup: {detail}  next: run `{backup_cmd}` any time for a fresh one')
    checks.append({'id': 'backup', 'status': 'ok', 'detail': detail,
                   'next_step': backup_cmd})


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
    if not isinstance(roots, dict):
        # A hand-edited `roots: []` reached `.items()` and raised, so `fha
        # doctor` died with a traceback whose advice was to run `fha doctor`.
        # The diagnostic tool has to survive the thing it diagnoses.
        lines.append(
            "fha.yaml: `roots:` is not a mapping  next: it should read like\n"
            "  roots:\n    photos: photos\n    documents: documents"
        )
        checks.append({'id': 'roots_shape', 'status': 'warn',
                       'detail': f'roots is {type(roots).__name__}, not a mapping',
                       'next_step': 'fix the roots: block in fha.yaml'})
        worst = max(worst, EXIT_WARNINGS)
        roots = {}
    is_fixture = is_fixture_path(archive_root)
    wc_mode = is_working_copy(archive_root)
    # Spelled with the launcher (see _LAUNCHER): every one of these is printed
    # as a copy-me next step, not as prose about a command.
    index_cmd = f'{_LAUNCHER} index --root "{root_arg}"'
    photoindex_cmd = f'{_LAUNCHER} photoindex --root "{root_arg}"'
    lint_cmd = f'{_LAUNCHER} lint --root "{root_arg}"'
    doctor_cmd = f'{_LAUNCHER} doctor --root "{root_arg}"'
    # docs/ stays at the archive root in every layout (only tools/ and design/
    # are vendored under .fha/), so this path needs no layout probe.
    troubleshooting = archive_root / 'docs' / 'TROUBLESHOOTING.md'

    # Original evidence is immutable, and git is the one thing in an archive that
    # rewrites bytes without being asked. `.gitattributes` ships with the DEFAULT
    # asset folders marked `-text`, but it cannot know where this owner pointed
    # `roots:` - and a CRLF GEDCOM or transcript under a custom root would be
    # silently normalized on checkout. Only meaningful for a git-tracked archive
    # with roots INSIDE it; an external drive is not git's business.
    ga = archive_root / '.gitattributes'
    if isinstance(roots, dict) and (archive_root / '.git').exists() and ga.is_file():
        # ASK GIT, do not reimplement it.
        #
        # Three versions of this check parsed `.gitattributes` by hand and three
        # were wrong: a substring match found the example inside a comment; a
        # bare split misread quoted patterns; and matching on pattern PRESENCE
        # ignored both attribute values and rule order, so `media/** -text`
        # followed by `*.txt text eol=lf` read as protected while git normalizes
        # `media/x.txt`. Precedence, negation, quoting and macros are git's
        # semantics, and the only thing that implements them correctly is git.
        #
        # `check-attr` answers for a PATH, and different extensions can resolve
        # differently under the same root, so probe a representative spread. The
        # question being asked is "could anything ordinary in here be rewritten?"
        unprotected = []
        for name, target in sorted(roots.items()):
            if not target:
                continue
            # Relative does not mean inside: `../FamilyPhotos` is an ordinary way
            # to keep originals beside the archive, and git cannot govern files
            # outside the repository - advising a pattern for one is advice that
            # cannot work. Resolve and test containment.
            resolved = (archive_root / target).resolve()
            try:
                inside = resolved.is_relative_to(archive_root.resolve())
            except AttributeError:                     # Python < 3.9
                inside = str(resolved).startswith(str(archive_root.resolve()) + os.sep)
            if not inside or not resolved.is_dir():
                continue
            rel = resolved.relative_to(archive_root.resolve()).as_posix()
            if not rel:
                continue
            probes = [f'{rel}/probe.{ext}' for ext in
                      ('txt', 'csv', 'md', 'ged', 'jpg')] + [f'{rel}/probe']
            try:
                out = subprocess.run(
                    ['git', 'check-attr', 'text', '--'] + probes,
                    cwd=str(archive_root), capture_output=True, text=True,
                    timeout=15,
                )
            except (OSError, subprocess.SubprocessError):
                break          # no usable git: this check cannot answer, so it says nothing
            if out.returncode != 0:
                break
            # "path: text: unset" is the protected answer. Anything else - set,
            # or unspecified under a `* text=auto` rule - means git may rewrite.
            exposed = [ln.rsplit(': ', 2)[0] for ln in out.stdout.splitlines()
                       if ln.strip() and not ln.endswith(': unset')]
            if exposed:
                unprotected.append((name, rel))
        if unprotected:
            worst = max(worst, EXIT_WARNINGS)
            for name, target in unprotected:
                # Quote the SUGGESTION too. Git parses the second token of a
                # rule as an attribute name, so `Family Photos/** -text` is not
                # merely ugly - it is invalid, and git says so while still
                # normalizing the file the rule was meant to protect.
                pattern = f'{target}/**'
                if any(c.isspace() for c in pattern):
                    pattern = f'"{pattern}"'
                lines.append(
                    f'originals ({name}): {target}/ is not protected in '
                    f'.gitattributes  next: add `{pattern} -text` to it so a '
                    f'checkout cannot rewrite your originals'
                )
            checks.append({
                'id': 'originals_gitattributes', 'status': 'warn',
                'detail': f'{len(unprotected)} asset root(s) unprotected',
                'next_step': 'add `<root>/** -text` to .gitattributes',
            })

    # #57: an unanchored .gitignore pattern can silently untrack sources/.
    # Same "ask git" gate as the .gitattributes check above - only meaningful
    # for a git-tracked archive, and only answerable when git itself is usable.
    if isinstance(roots, dict) and (archive_root / '.git').exists():
        worst = max(worst, _check_sources_gitignore(archive_root, roots, lines, checks))

    if wc_mode:
        lines.append(
            '[working copy] photos and documents live on the main machine - '
            'asset features are paused here'
        )
        lines.append('')
        checks.append({'id': 'working_copy', 'status': 'info',
                       'detail': 'working-copy mode active', 'next_step': None})

    # Said once, at the top, so the reader knows why every command below starts
    # with `./` (or `.\`) and can retype it for a shell this machine is not
    # running - the same gloss docs/TROUBLESHOOTING.md gives its bare commands.
    lines.append(
        '(Every `next:` below is a command you can copy. `fha` is the launcher '
        'file in the archive folder, not a program on your PATH, so it is '
        f'written `{_LAUNCHER}` here; the Windows Command Prompt also takes a '
        'bare `fha`.)')
    lines.append('')
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

    # A root that resolves fine can still be the WRONG root: narrowing
    # `photos:` to a subfolder orphans every filed asset under the alias, and
    # until now the first sign was a wall of lint E011 pointing at
    # `fha reconcile`, which cannot help because nothing moved (#36). Outside
    # the `if roots:` block on purpose: deleting the whole roots: mapping is
    # a change too (every alias falls back to an internal folder), and lint
    # and index report it - doctor must not stay silent on the same fha.yaml.
    orphaning = roots_change_orphans(archive_root, fha_config)
    if orphaning:
        for item in orphaning:
            lines.append(
                f"{_WARN} {format_roots_orphan_warning(item, archive_root)}  "
                f'next: revert the {item["alias"]}: value in fha.yaml, or re-point '
                'the records; use photos_ignore: to exclude a subtree'
            )
            checks.append({
                'id': f'root_change:{item["alias"]}', 'status': 'warn',
                'detail': f"{item['orphaned']} filed file(s) orphaned by the change "
                          f"{item['old']!r} -> {item['new']!r}",
                'next_step': 'revert the roots: value or re-point the records',
            })
            worst = max(worst, EXIT_WARNINGS)
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
            f'next: `{pip_command("jinja2")}` to build the family website'
        )
        checks.append({'id': 'jinja2', 'status': 'warn', 'detail': 'not installed',
                       'next_step': pip_command('jinja2')})
        worst = max(worst, EXIT_WARNINGS)
    if _ilu.find_spec('PIL') is not None:
        lines.append(f'pillow (fha site images): {_OK}  next: no action needed')
        checks.append({'id': 'pillow', 'status': 'ok', 'detail': 'installed', 'next_step': None})
    else:
        lines.append(
            'pillow (fha site images): not installed (optional)  '
            f'next: `{pip_command("pillow")}` for photos in the standalone site'
        )
        checks.append({'id': 'pillow', 'status': 'info', 'detail': 'not installed (optional)',
                       'next_step': pip_command('pillow')})
    # pypdf mirrors Pillow's posture: purely optional (`fha source extract`
    # PDF text layers, M11.5) - absence is informational, never a warning.
    if _ilu.find_spec('pypdf') is not None:
        lines.append(f'pypdf (fha source extract): {_OK}  next: no action needed')
        checks.append({'id': 'pypdf', 'status': 'ok', 'detail': 'installed', 'next_step': None})
    else:
        lines.append(
            'pypdf (fha source extract): not installed (optional)  '
            f'next: `{pip_command("pypdf")}` to dump PDF text layers'
        )
        checks.append({'id': 'pypdf', 'status': 'info', 'detail': 'not installed (optional)',
                       'next_step': pip_command('pypdf')})
    lines.append('')

    idx_status, idx_delta = _index_freshness(archive_root)
    idx_path = archive_root / '.cache' / 'index.sqlite'
    # A record folder that will not list holds the index at 'stale' forever
    # (`_lib.newest_record_mtime` reports 'now' rather than a watermark it
    # cannot stand behind), so "run fha index" alone would be an instruction
    # to loop. Asked only on the failure path: when the index reads fresh,
    # that same rule guarantees every record folder opened.
    unreadable_dirs = (
        [] if idx_status == 'fresh' else _unreadable_record_dirs(archive_root))
    unreadable_cause = ''
    if unreadable_dirs:
        listed = ', '.join(unreadable_dirs[:5])
        if len(unreadable_dirs) > 5:
            listed += f' and {len(unreadable_dirs) - 5} more'
        unreadable_cause = (
            f' - and it will stay that way: {len(unreadable_dirs)} folder(s) '
            f'could not be opened ({listed}), so nothing filed in them can be '
            f'indexed, searched, or exported. This is usually a folder whose '
            f'permissions changed, or a drive or network share that is not '
            f'connected'
        )
    if idx_status == 'fresh':
        lines.append(f'index: {_OK} fresh at {idx_path}  next: no action needed')
        checks.append({'id': 'index', 'status': 'ok', 'detail': 'fresh', 'next_step': None})
    elif idx_status == 'stale':
        if unreadable_dirs:
            lines.append(
                f'index: {_WARN} stale by {idx_delta} at {idx_path}'
                f'{unreadable_cause}  next: reconnect it (or restore your '
                f'access to the folder), then run `{index_cmd}`')
            checks.append({'id': 'index', 'status': 'warn',
                           'detail': f'stale by {idx_delta}; '
                                     f'{len(unreadable_dirs)} unreadable folder(s)',
                           'unreadable_dirs': unreadable_dirs,
                           'next_step': index_cmd})
        else:
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
        ingest_cmd = f'{_LAUNCHER} capture --ingest --root "{root_arg}"'
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
    if counts is not None and label.startswith('counts (scanned') and unreadable_dirs:
        # These are counts of PRIVACY-bearing records, read by a human
        # deciding what is safe to share. Counted by walking the same folders
        # that would not open, so they are floors, not totals - saying so is
        # the difference between a low number and a wrong one.
        lines.append(
            f'  (counted low: {len(unreadable_dirs)} folder(s) above could not '
            f'be opened, so anything filed in them is not in these numbers)')
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

    # ── 12. Backup recency (always printed; info-level, CLEAN contribution) ──
    # Real state first (the fha backup stamp), then the always-printed list of
    # paths a full backup must cover - the reminder names the command and the
    # date instead of restating policy with no state behind it.
    backup_cmd = f'{_LAUNCHER} backup --root "{root_arg}"'
    lines.append('-' * 60)
    _check_backup_stamp(archive_root, lines, checks, backup_cmd)
    lines.append('Backup policy must cover both the archive root and all mapped asset roots.')
    lines.append(f'Archive root: {archive_root}')
    for alias in roots:
        lines.append(f'Asset root {alias}: {resolve_path(alias, fha_config, archive_root)}')
    lines.append(
        f'Next: `{backup_cmd}` zips the records; cover the asset roots with '
        f'`--include-assets` or your own copy of those folders. More help: {troubleshooting}'
    )

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


# User-facing --help text (the module docstring stays developer-facing).
_CLI_DESCRIPTION = """\
Check that the tools, dependencies, and file paths are all wired correctly.

  fha doctor

Run this when something's broken, or when setting up on a new machine. Safe on a
fresh archive: missing caches are warnings, not errors."""


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register 'doctor' onto the main fha parser."""
    p = subparsers.add_parser(
        'doctor',
        help='Archive health check - what is wrong with this archive?',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root')
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
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--root', metavar='PATH', help='Archive root')
    args = parser.parse_args(argv)
    return _run_doctor(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
