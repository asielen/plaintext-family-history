#!/usr/bin/env python3
"""
scaffold.py — fha install / fha update-tools: vendor the operating layer into a
private archive and keep it current (TOOLING §13c, BUILD.md M9.1-M9.2).

A real family archive is a *separate, private* repository: the user's records
plus a vendored copy of the generic operating layer (the `tools/`, the spec docs,
the agent rulebooks, the human docs). This file is the ritual that copies that
operating layer in, and later refreshes it from an improved public clone —
without ever destroying the human's work.

THE MANIFEST (the package's own packing list)
---------------------------------------------
`manifest.json` at the repo root IS the definition of what belongs in an archive.
Every entry names a destination `path` (archive-relative), a content `sha256`, a
`spec_version`, and a `category`:

  - "operating"  — tools, spec docs, human docs. `fha update-tools` keeps these
                   current; the human may edit any of them and the checksum
                   compare protects the edit on the next update.
  - "skeleton"   — the empty starting structure: `fha.yaml`, the seeded
                   `places/places.yaml`, `inbox/_TEMPLATE.notes.md`, and the
                   `.gitkeep` files that hold the empty record directories.
                   These are written ONCE by `fha install` and never touched by
                   `update-tools`, because `fha.yaml` and `places.yaml` quickly
                   fill with the human's own configuration and data — refreshing
                   them would clobber that. (See the design note in
                   tools/README.md; surfaced as a TOOLING clarification.)

Skeleton entries carry a `src` field (their source path *inside* the public
repo, e.g. `archive-template/fha.yaml`) because their archive `path` strips the
`archive-template/` prefix — the template folder seeds the skeleton but is never
itself copied into an archive. Operating entries omit `src` (source == dest).

The manifest is committed data, regenerated from the repo by `_write_manifest`
(`python tools/scaffold.py write-manifest --repo .`). A regression test
(`tests/test_scaffold.py`) recomputes it and asserts it still matches what is
committed, so a PR that changes a tool but forgets to regenerate fails CI.

`fha install <archive-path> [--repo PATH]`  (run from a public-repo clone)
-------------------------------------------------------------------------
Preflight (Python ≥ 3.10; exiftool on PATH — a friendly heads-up, never a hard
stop), then copy every manifest file into the archive and stamp
`.plainfile-version` (the manifest version + the per-file checksums received).
Works from a git clone OR an unzipped download (`--repo` only needs a directory
containing `manifest.json`; `.git/` is never assumed) — the zip path is
first-class for non-technical users (docs/SETUP_FROM_ZIP.md).

`fha update-tools [--dry-run] --repo PATH`  (run from inside an archive)
-----------------------------------------------------------------------
Compare the public manifest against the archive's `.plainfile-version`, reconcile
only the OPERATING layer, and NEVER destroy anything:

  - new file in the manifest                    → copy it in
  - file unchanged from the stock you installed  → overwrite silently
  - file you customized (checksum differs)       → move yours to
                                                   .plainfile-backup/{date}/,
                                                   install stock, and report it
  - file retired from the manifest upstream      → move to .plainfile-backup/,
                                                   report (never deleted)

The governing principle: the updater only adds, replaces-pristine-with-stock, or
moves-aside-and-reports. The human is always the one who throws things away.

CODE MAP
--------
  Errors / checksums
    ScaffoldError              — friendly, message-carrying failure
    _sha256_bytes/_sha256_file — content checksums (binary, exact)

  Manifest definition + IO
    _operating_files           — the operating-layer file list (repo walk)
    _skeleton_files            — the skeleton file list (archive-template remap)
    generate_manifest          — build the manifest dict from a repo clone
    _write_manifest            — (maintenance) regenerate and write manifest.json
    load_manifest              — read+validate manifest.json from a repo dir
    _resolve_repo_root         — locate the clone/zip dir holding manifest.json

  Version stamp + backups
    _load_version_stamp        — read .plainfile-version (None if absent)
    _stamp_dict                — build a .plainfile-version payload
    _write_version_stamp       — write .plainfile-version
    _unique_backup_path        — collision-free .plainfile-backup/{date}/ path

  Install (M9.1)
    _preflight                 — Python/exiftool checks → (ok, messages)
    run_install                — create skeleton + copy operating layer + stamp
    _cmd_install               — argparse → run_install

  Update (M9.2)
    _plan_update               — classify every file (add/current/stock/customized/retired)
    run_update_tools           — execute (or preview) the plan, rewrite the stamp
    _cmd_update_tools          — argparse → run_update_tools

  CLI
    register / _standalone_main
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    Result,
    configure_utf8_stdout,
    find_archive_root,
)

configure_utf8_stdout()

# The manifest's own schema/version. Bump when the *set* of installed files or
# the stamp format changes in a way an installer needs to notice.
MANIFEST_VERSION = '1'

# Operating-layer docs that live at the repo root (not under tools/ or docs/).
# Enumerated rather than walked because the repo root also holds furniture that
# never enters an archive (PRIVACY.md — the *public-repo* "no real data" policy,
# which is contradictory inside a real archive; RELEASE_CHECKLIST.md — the public
# release process; CNAME, manifest.json, .git*, …). TOOLING §13c / BUILD.md M9.1.
# README.md is shipped (project orientation a genealogist benefits from).
_ROOT_OPERATING_DOCS = (
    'README.md',
    'SPEC.md',
    'TOOLING.md',
    'AGENTS.md',
    'AGENTS_TOOLING.md',
    'CLAUDE.md',
    'BUILD.md',
)

# Subtrees walked whole for the operating layer. `.claude/skills/` carries the
# agent's genealogy workflow procedures (process-source, review-claims, …) — the
# "how to operate" an archive, so it ships. `.claude/settings.json` is *not*
# walked: it is this spec-repo's own agent config, not an archive's.
_OPERATING_SUBTREES = ('tools', 'docs', '.claude/skills')

# The template folder whose *contents* seed the skeleton. The folder itself is
# never copied into an archive — each file's archive path strips this prefix.
_SKELETON_SRC_DIR = 'archive-template'

# A file under archive-template/ that is repo furniture, not skeleton: it tells a
# human how to start an archive, which the docs/ guides already cover.
_SKELETON_EXCLUDE = {'README.md'}

# The two on-disk footprints of the updater. Both are safe to inspect or delete
# by hand; neither is ever copied or compared as part of the operating layer.
VERSION_FILE = '.plainfile-version'
BACKUP_DIR = '.plainfile-backup'


# ── Errors / checksums ─────────────────────────────────────────────────────────

class ScaffoldError(Exception):
    """A failure with a plain, human-facing message and a next step.

    Raised inside the run_* helpers and caught at the CLI boundary, so the
    non-technical user never sees a traceback — only the message, which always
    names a cause and the exact command or fix to try next.
    """


def _sha256_bytes(data: bytes) -> str:
    """Hex SHA-256 of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    """Hex SHA-256 of a file's exact bytes.

    Hashing bytes (not decoded text) is deliberate: a CRLF-vs-LF re-save or a
    trailing-newline tweak is a real change to the file the updater must detect,
    so the customization guard never silently overwrites a hand-edit.
    """
    return _sha256_bytes(path.read_bytes())


# ── Manifest definition + IO ────────────────────────────────────────────────────

def _operating_files(repo_root: Path) -> list[tuple[str, Path]]:
    """Yield (archive_path, source_path) for every operating-layer file.

    The operating layer is the generic, regenerable glue a genealogist needs to
    operate an archive: the root rulebooks + README, everything under tools/
    (minus Python bytecode caches), everything under docs/, and the agent's
    workflow skills under .claude/skills/. docs/ is included whole rather than
    cherry-picked: BUILD.md M9.1 names five docs as the floor ("must ship into
    every archive"), but the whole folder is generic human-facing documentation
    with no family data, and a directory rule auto-covers future docs and keeps
    their cross-links intact in an installed archive. Source path equals archive
    path for all of these.
    """
    out: list[tuple[str, Path]] = []

    for name in _ROOT_OPERATING_DOCS:
        src = repo_root / name
        if src.is_file():
            out.append((name, src))

    for sub in _OPERATING_SUBTREES:
        base = repo_root / sub
        if not base.is_dir():
            continue
        for p in sorted(base.rglob('*')):
            if not p.is_file():
                continue
            if '__pycache__' in p.parts or p.suffix in ('.pyc', '.pyo'):
                continue
            rel = p.relative_to(repo_root).as_posix()
            out.append((rel, p))

    return out


def _skeleton_files(repo_root: Path) -> list[tuple[str, Path]]:
    """Yield (archive_path, source_path) for every skeleton seed file.

    The skeleton is the empty starting structure an archive grows from. Its
    files live under archive-template/ in the repo; their archive path strips
    that prefix (the template folder seeds an archive but is never itself copied
    in). `.gitkeep` files are included so the empty record directories
    (sources/, people/stubs/, notes/, …) come into being from a plain file copy.
    """
    out: list[tuple[str, Path]] = []
    base = repo_root / _SKELETON_SRC_DIR
    if not base.is_dir():
        return out
    for p in sorted(base.rglob('*')):
        if not p.is_file():
            continue
        rel_in_template = p.relative_to(base)
        if rel_in_template.as_posix() in _SKELETON_EXCLUDE:
            continue
        archive_path = rel_in_template.as_posix()
        out.append((archive_path, p))
    return out


def generate_manifest(repo_root: Path, spec_version: str | None = None) -> dict:
    """Build the manifest dict from a public-repo clone.

    Walks the operating-layer and skeleton file sets, checksums each file, and
    returns the JSON-serializable manifest. Entries are sorted by archive path so
    the committed manifest.json has a stable, diff-friendly order. `spec_version`
    defaults to the value parsed from SPEC.md's "**Version X.Y …**" line.
    """
    repo_root = Path(repo_root).resolve()
    if spec_version is None:
        spec_version = _read_spec_version(repo_root)

    entries: list[dict] = []
    for category, pairs in (
        ('operating', _operating_files(repo_root)),
        ('skeleton', _skeleton_files(repo_root)),
    ):
        for archive_path, src in pairs:
            entry = {
                'path': archive_path,
                'category': category,
                'sha256': _sha256_file(src),
                'spec_version': spec_version,
            }
            # Record the in-repo source only when it differs from the archive
            # path (skeleton files, whose archive path drops archive-template/).
            src_rel = src.relative_to(repo_root).as_posix()
            if src_rel != archive_path:
                entry['src'] = src_rel
            entries.append(entry)

    entries.sort(key=lambda e: e['path'])
    return {
        'manifest_version': MANIFEST_VERSION,
        'spec_version': spec_version,
        'generated': datetime.date.today().isoformat(),
        'files': entries,
    }


def _read_spec_version(repo_root: Path) -> str:
    """Parse SPEC.md's '**Version X.Y - date**' header; fall back to 'unknown'."""
    spec = repo_root / 'SPEC.md'
    if not spec.is_file():
        return 'unknown'
    import re
    m = re.search(r'\*\*Version\s+([0-9]+(?:\.[0-9]+)*)', spec.read_text(encoding='utf-8'))
    return m.group(1) if m else 'unknown'


def _write_manifest(repo_root: Path) -> Path:
    """(Maintenance) Regenerate manifest.json from the repo and write it.

    Not part of the `fha` command surface — invoked by a tool-builder via
    `python tools/scaffold.py write-manifest --repo .` after any change to a
    tool, doc, or skeleton file. The committed manifest.json is the packing list
    `install`/`update-tools` read; this keeps it honest.
    """
    repo_root = Path(repo_root).resolve()
    manifest = generate_manifest(repo_root)
    path = repo_root / 'manifest.json'
    path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    return path


def load_manifest(repo_root: Path) -> dict:
    """Read and validate manifest.json from a repo/clone/zip directory.

    Raises ScaffoldError (never a traceback) when the file is missing or
    unparseable, with a message that names the directory looked in and the fix.
    """
    path = repo_root / 'manifest.json'
    if not path.is_file():
        raise ScaffoldError(
            f"no manifest.json in {repo_root}. Point --repo at your copy of the "
            f"plainfile tools — the folder that contains manifest.json, SPEC.md, "
            f"and the tools/ folder (a git clone or an unzipped download both work)."
        )
    try:
        manifest = json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as exc:
        raise ScaffoldError(
            f"could not read {path}: {exc}. Re-download the plainfile tools and "
            f"try again."
        ) from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get('files'), list):
        raise ScaffoldError(
            f"{path} is not a valid manifest (expected a 'files' list). "
            f"Re-download the plainfile tools and try again."
        )
    return manifest


def _resolve_repo_root(repo_arg: str | None) -> Path:
    """Resolve the public-repo directory holding manifest.json.

    When --repo is given, use it. Otherwise default to this file's repo (two
    levels up from tools/scaffold.py) — correct for `fha install` run from a
    clone or an unzipped download, where the running tools ARE the source. The
    caller (install vs update) decides whether a default is acceptable; this only
    resolves the path.
    """
    if repo_arg:
        return Path(repo_arg).resolve()
    return Path(__file__).resolve().parents[1]


# ── Version stamp + backups ─────────────────────────────────────────────────────

def _load_version_stamp(archive_root: Path) -> dict | None:
    """Read .plainfile-version; return None if absent, raise on corruption."""
    path = archive_root / VERSION_FILE
    if not path.is_file():
        return None
    try:
        stamp = json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as exc:
        raise ScaffoldError(
            f"{path} is unreadable ({exc}). Delete it and run `fha update-tools` "
            f"again — it will be rewritten (your tool files are not touched by "
            f"reading it)."
        ) from exc
    if not isinstance(stamp, dict):
        return None
    return stamp


def _stamp_dict(manifest: dict, checksums: dict[str, str]) -> dict:
    """Build a .plainfile-version payload from a manifest + the checksums installed."""
    return {
        'manifest_version': manifest.get('manifest_version', MANIFEST_VERSION),
        'spec_version': manifest.get('spec_version', 'unknown'),
        'installed': datetime.datetime.now().isoformat(timespec='seconds'),
        'files': dict(sorted(checksums.items())),
    }


def _write_version_stamp(archive_root: Path, stamp: dict) -> None:
    """Write .plainfile-version (pretty JSON, trailing newline)."""
    path = archive_root / VERSION_FILE
    path.write_text(json.dumps(stamp, indent=2) + '\n', encoding='utf-8')


def _unique_backup_path(archive_root: Path, rel_path: str, date_str: str) -> Path:
    """Compute a collision-free .plainfile-backup/{date}/{rel_path} destination.

    Backups preserve the archive-relative subtree (so a backed-up tools/fha.py
    lands at .plainfile-backup/{date}/tools/fha.py). If that target already
    exists — e.g. two updates the same day each move a re-edited file — a numeric
    suffix (-2, -3, …) is added so an earlier backup is never overwritten. The
    updater's whole promise is that nothing is lost.
    """
    base = archive_root / BACKUP_DIR / date_str / rel_path
    if not base.exists():
        return base
    stem, suffix = base.stem, base.suffix
    n = 2
    while True:
        candidate = base.with_name(f'{stem}-{n}{suffix}')
        if not candidate.exists():
            return candidate
        n += 1


# ── Install (M9.1) ──────────────────────────────────────────────────────────────

def _preflight() -> tuple[bool, list[str]]:
    """Check first-day prerequisites; return (python_ok, advisory_messages).

    Plain, friendly guidance — never a traceback or a bare "not found":
      - Python < 3.10 → a hard stop (python_ok=False) with a download pointer.
      - exiftool missing → an advisory message; install proceeds (photo features
        simply wait until it is installed). BUILD.md M9.1.
    """
    messages: list[str] = []
    python_ok = sys.version_info >= (3, 10)
    if not python_ok:
        have = f'{sys.version_info[0]}.{sys.version_info[1]}'
        messages.append(
            f"Python 3.10 or later is required. You have {have}. "
            f"Download the latest at python.org."
        )
    if shutil.which('exiftool') is None:
        messages.append(
            "exiftool is not installed. Photo features won't work until it is. "
            "Install it from exiftool.org (Mac: `brew install exiftool`; "
            "Windows: see exiftool.org/install.html)."
        )
    return python_ok, messages


def run_install(
    archive_path: Path,
    repo_root: Path,
    *,
    dry_run: bool = False,
) -> Result:
    """Create an archive's skeleton + operating layer and stamp it; return a Result.

    Run from a public-repo clone (or unzipped download). Copies every manifest
    file into `archive_path`, then writes `.plainfile-version` recording the
    manifest version and the per-file checksums received. Refuses an archive that
    already carries tools (a `.plainfile-version` or `tools/fha.py`) and points
    the human at `fha update-tools` instead — install is a one-time bootstrap.

    Returns a `Result` (Result == int, so callers/tests comparing against EXIT_*
    keep working): EXIT_CLEAN on success (even with the exiftool advisory),
    EXIT_FAILURE on a preflight failure, with the copied files and version stamp
    listed in `changed` (empty under --dry-run).  The install narration is
    printed inline.  Raises ScaffoldError for the caller to print.
    """
    archive_path = Path(archive_path).resolve()
    manifest = load_manifest(repo_root)

    python_ok, advisories = _preflight()
    if not python_ok:
        for m in advisories:
            print(f'ERROR: {m}', file=sys.stderr)
        return Result(ok=False, exit_code=EXIT_FAILURE)

    already = archive_path / VERSION_FILE
    if already.is_file():
        raise ScaffoldError(
            f"{archive_path} already has the plainfile tools installed. To refresh "
            f"them with improvements from the public repo, run from inside that "
            f"archive:\n  fha update-tools --repo \"{repo_root}\""
        )
    # tools/fha.py present without a stamp means a previous install was interrupted
    # before it could write the stamp.  Allow re-running install to complete it.

    # Validate every source exists BEFORE writing anything, so a broken/partial
    # clone fails cleanly instead of leaving a half-installed archive.
    files = manifest['files']
    missing: list[str] = []
    for entry in files:
        src = repo_root / entry.get('src', entry['path'])
        if not src.is_file():
            missing.append(entry.get('src', entry['path']))
    if missing:
        listing = '\n  '.join(missing[:10])
        more = '' if len(missing) <= 10 else f'\n  …and {len(missing) - 10} more'
        raise ScaffoldError(
            f"your copy of the plainfile tools is missing {len(missing)} file(s) "
            f"the manifest expects:\n  {listing}{more}\n"
            f"Re-download or re-clone the tools, then run install again."
        )

    # Preflight: refuse to overwrite existing user content in skeleton destinations.
    # The guard above already blocks double-installs; this catches the case where a
    # user hand-started an archive (e.g. wrote fha.yaml or seeded their own README)
    # before running `fha install`.
    # Exception: if a skeleton file is byte-for-byte identical to what install would
    # place (sha256 match) it was left by a partial previous install that never wrote
    # the stamp — safe to overwrite so the user can simply re-run install to finish.
    conflicts = [
        entry['path']
        for entry in files
        if entry.get('category') == 'skeleton'
        and Path(entry['path']).name != '.gitkeep'
        and (archive_path / entry['path']).is_file()
        and _sha256_file(archive_path / entry['path']) != entry.get('sha256')
    ]
    if conflicts:
        listing = '\n  '.join(conflicts[:10])
        more = '' if len(conflicts) <= 10 else f'\n  …and {len(conflicts) - 10} more'
        raise ScaffoldError(
            f"{archive_path} already contains files that install would overwrite:\n  "
            f"{listing}{more}\n"
            "Move or rename them first, then re-run install."
        )

    if dry_run:
        print(f'Dry run — would install into: {archive_path}')
        print(f'  {len(files)} file(s) from {repo_root / "manifest.json"}')
        skel = sum(1 for e in files if e.get('category') == 'skeleton')
        print(f'  ({skel} skeleton file(s), {len(files) - skel} operating-layer file(s))')
        print(f'  and write {archive_path / VERSION_FILE}')
        for m in advisories:
            print(f'\nNote: {m}')
        print('\nNothing was written (dry run). Re-run without --dry-run to install.')
        return Result(exit_code=EXIT_CLEAN, data={'dry_run': True, 'file_count': len(files)})

    checksums: dict[str, str] = {}
    changed: list[str] = []
    try:
        archive_path.mkdir(parents=True, exist_ok=True)
        for entry in files:
            src = repo_root / entry.get('src', entry['path'])
            dest = archive_path / entry['path']
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            checksums[entry['path']] = entry.get('sha256') or _sha256_file(src)
            changed.append(str(dest))
        _write_version_stamp(archive_path, _stamp_dict(manifest, checksums))
        changed.append(str(archive_path / VERSION_FILE))
    except OSError as exc:
        raise ScaffoldError(
            f"could not finish installing into {archive_path}: {exc}. "
            f"Check that you can write there and have enough disk space, then run "
            f"install again."
        ) from exc

    print(f'Installed the plainfile tools into: {archive_path}')
    print(f'  {len(files)} file(s) copied; recorded in {archive_path / VERSION_FILE}')
    print('\nNext steps:')
    print(f'  1. Edit {archive_path / "fha.yaml"} to point at your photos and documents.')
    print(f'  2. Open the archive in your AI agent and start filing inbox/ items.')
    print(f'  3. Run `fha doctor` from inside the archive to check everything is set up.')
    for m in advisories:
        print(f'\nNote: {m}')
    return Result(exit_code=EXIT_CLEAN, changed=changed,
                  data={'file_count': len(files)})


def _cmd_install(args: argparse.Namespace) -> int:
    """argparse bridge for `fha install`."""
    repo_root = _resolve_repo_root(getattr(args, 'repo', None))
    try:
        return run_install(
            Path(args.archive_path),
            repo_root,
            dry_run=bool(getattr(args, 'dry_run', False)),
        ).exit_code
    except ScaffoldError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return EXIT_FAILURE


# ── Update (M9.2) ───────────────────────────────────────────────────────────────

def _plan_update(
    archive_root: Path,
    repo_root: Path,
    manifest: dict,
    stamp: dict | None,
) -> dict:
    """Classify every operating-layer file without writing anything.

    Returns a plan dict with five lists of (archive_path, src_path|None):
      added      — in the manifest, not on disk yet
      current    — on disk and already byte-identical to the new stock (no-op)
      stock      — on disk, unchanged from the stock you installed, stock improved
                   → overwrite silently
      customized — on disk and different from what you installed → back up + install
      retired    — recorded in .plainfile-version but gone from the manifest, and
                   still on disk → move to backup

    Only "operating" files are considered. Skeleton seeds (fha.yaml, places.yaml,
    the template, .gitkeep) are install-once and deliberately untouched here, so
    `update-tools` can never clobber the human's configuration or place data.
    """
    recorded: dict[str, str] = (stamp or {}).get('files', {}) if stamp else {}

    plan = {'added': [], 'current': [], 'stock': [], 'customized': [], 'retired': []}

    manifest_operating_paths = set()
    for entry in manifest['files']:
        if entry.get('category') != 'operating':
            continue
        archive_path = entry['path']
        manifest_operating_paths.add(archive_path)
        src = repo_root / entry.get('src', archive_path)
        dest = archive_root / archive_path
        stock_sum = _sha256_file(src) if src.is_file() else entry.get('sha256')

        if not dest.exists():
            plan['added'].append((archive_path, src))
            continue
        try:
            disk_sum = _sha256_file(dest)
        except OSError:
            plan['customized'].append((archive_path, src))
            continue
        if disk_sum == stock_sum:
            plan['current'].append((archive_path, src))
        elif archive_path in recorded and disk_sum == recorded[archive_path]:
            plan['stock'].append((archive_path, src))
        else:
            plan['customized'].append((archive_path, src))

    # Retired: a path the stamp recorded but the manifest no longer lists at all
    # (skeleton paths stay listed, so user data is never flagged retired). Move
    # only if it still exists; an already-removed file needs nothing.
    manifest_all_paths = {e['path'] for e in manifest['files']}
    for archive_path in recorded:
        if archive_path in manifest_all_paths:
            continue
        if (archive_root / archive_path).exists():
            plan['retired'].append((archive_path, None))

    return plan


def run_update_tools(
    archive_root: Path,
    repo_root: Path,
    *,
    dry_run: bool = False,
    verbose: bool = False,
) -> Result:
    """Refresh the operating layer in an existing archive from a public clone.

    Reads the public manifest and the archive's `.plainfile-version`, classifies
    each operating file (_plan_update), then either previews (`--dry-run`) or
    applies the plan: copy new files, silently overwrite stock-unchanged ones,
    back up customized ones before installing stock, and quarantine retired ones.
    Never deletes; never silently overwrites the human's edits. Rewrites the
    stamp afterward (operating checksums refreshed; skeleton entries carried over;
    retired entries dropped).

    Returns a `Result` (Result == int, so callers/tests comparing against EXIT_*
    keep working): EXIT_CLEAN on a clean update or dry run, EXIT_WARNINGS when one
    or more files could not be updated, with the files actually installed (plus
    the rewritten stamp) listed in `changed` (empty under --dry-run). The update
    narration is printed inline. Raises ScaffoldError on any can't-run condition.
    """
    archive_root = Path(archive_root).resolve()
    manifest = load_manifest(repo_root)
    stamp = _load_version_stamp(archive_root)

    if stamp is None:
        print(
            f'No {VERSION_FILE} found in {archive_root} — treating existing tool '
            f'files as your own work. Anything different from the new version is '
            f'backed up (never overwritten), not replaced silently.'
        )
        print()

    plan = _plan_update(archive_root, repo_root, manifest, stamp)
    date_str = datetime.date.today().isoformat()

    # A broken/partial clone must fail before any mutation — otherwise a
    # customized file could be moved to backup and then have no stock to replace
    # it. Mirrors install's pre-write source check. Retired entries carry no src.
    missing = [
        ap for ap, src in (plan['added'] + plan['stock'] + plan['customized'])
        if src is None or not src.is_file()
    ]
    if missing:
        listing = '\n  '.join(missing[:10])
        more = '' if len(missing) <= 10 else f'\n  …and {len(missing) - 10} more'
        raise ScaffoldError(
            f"your copy of the plainfile tools is missing {len(missing)} file(s) "
            f"the manifest expects:\n  {listing}{more}\n"
            f"Re-download or re-clone the tools, then run `fha update-tools` again."
        )

    n_added = len(plan['added'])
    n_stock = len(plan['stock'])
    n_custom = len(plan['customized'])
    n_retired = len(plan['retired'])
    n_current = len(plan['current'])

    if dry_run:
        print(f'Dry run — comparing {archive_root} against {repo_root / "manifest.json"}:')
        _report_plan(archive_root, plan, date_str, verbose=verbose)
        print()
        print(
            f'Plan: {n_added} to add, {n_stock} to update, {n_custom} to back up '
            f'and update, {n_retired} retired, {n_current} already up to date.'
        )
        print('Nothing was written (dry run). Re-run without --dry-run to apply.')
        return Result(exit_code=EXIT_CLEAN, data={
            'dry_run': True, 'added': n_added, 'stock': n_stock,
            'customized': n_custom, 'retired': n_retired, 'current': n_current,
        })

    # Apply. Each action is individually guarded; a single OSError is reported and
    # downgrades the run to a warning rather than aborting partway. Every per-file
    # message is printed AFTER its operation succeeds, and the summary counts only
    # what actually happened — the output never claims a success that did not occur.
    installed_ok: dict[str, str] = {}
    failures: list[str] = []
    failed_paths: set[str] = set()
    n_added_ok = n_stock_ok = n_custom_ok = n_retired_ok = 0
    backups_made = False

    def _copy_in(archive_path: str, src: Path) -> None:
        dest = archive_root / archive_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Write to a sibling temp file and atomically replace the destination so a
        # disk-full or interrupted copy never leaves a truncated tool file behind.
        tmp = dest.with_suffix(dest.suffix + '.fha-tmp')
        try:
            shutil.copy2(src, tmp)
            tmp.replace(dest)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        installed_ok[archive_path] = _sha256_file(dest)

    def _fail(archive_path: str, exc: OSError) -> None:
        failures.append(f'{archive_path}: {exc}')
        failed_paths.add(archive_path)

    for archive_path, src in plan['added']:
        try:
            _copy_in(archive_path, src)
        except OSError as exc:
            _fail(archive_path, exc)
            continue
        n_added_ok += 1
        print(f'Added {archive_path} (new).')

    for archive_path, src in plan['stock']:
        try:
            _copy_in(archive_path, src)
        except OSError as exc:
            _fail(archive_path, exc)
            continue
        n_stock_ok += 1
        print(f'Updated {archive_path} (unchanged from stock).')

    for archive_path, src in plan['customized']:
        dest = archive_root / archive_path
        backup = _unique_backup_path(archive_root, archive_path, date_str)
        try:
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dest), str(backup))
        except OSError as exc:
            _fail(archive_path, exc)
            continue
        try:
            _copy_in(archive_path, src)
        except OSError as exc:
            # The move succeeded but the copy failed; restore so the archive is
            # not left missing the file.
            try:
                shutil.move(str(backup), str(dest))
            except OSError as restore_exc:
                failures.append(
                    f'{archive_path}: copy failed ({exc}) and restore also failed '
                    f'({restore_exc}); your backup is at {backup}'
                )
                failed_paths.add(archive_path)
                continue
            _fail(archive_path, exc)
            continue
        n_custom_ok += 1
        backups_made = True
        print(
            f'Your edited {archive_path} has been backed up to {backup} — '
            f'the new version is now in {archive_path}.'
        )

    for archive_path, _src in plan['retired']:
        dest = archive_root / archive_path
        backup = _unique_backup_path(archive_root, archive_path, date_str)
        try:
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dest), str(backup))
        except OSError as exc:
            _fail(archive_path, exc)
            continue
        n_retired_ok += 1
        backups_made = True
        print(
            f'Moved {archive_path} to {backup} — it is no longer part of the '
            f'plainfile tools (kept, not deleted).'
        )

    if verbose:
        for archive_path, _src in plan['current']:
            print(f'{archive_path} is already up to date.')

    # Rewrite the stamp. Each recorded checksum is an operating file's *installed
    # baseline* — what the next run compares the working copy against:
    #   - newly installed this run → its new on-disk hash;
    #   - already current (untouched, == stock) → its on-disk hash (== stock);
    #   - FAILED this run → keep the PRIOR recorded baseline, never the current
    #     disk bytes. Recording a failed customized file's edited content would
    #     make the next run see disk == recorded and treat the edit as pristine
    #     stock, silently overwriting the human's work; preserving the old
    #     baseline keeps it classified "customized" (and backed up) on retry.
    # Skeleton entries carry over verbatim (update never touches them). Retired
    # files that moved successfully drop out; ones that failed to move are kept so
    # the next run re-detects and retries them.
    new_checksums: dict[str, str] = {}
    old_recorded = (stamp or {}).get('files', {})
    for entry in manifest['files']:
        archive_path = entry['path']
        if entry.get('category') == 'skeleton':
            if archive_path in old_recorded:
                new_checksums[archive_path] = old_recorded[archive_path]
            continue
        if archive_path in installed_ok:
            new_checksums[archive_path] = installed_ok[archive_path]
        elif archive_path in failed_paths:
            if archive_path in old_recorded:
                new_checksums[archive_path] = old_recorded[archive_path]
        else:
            dest = archive_root / archive_path
            if dest.is_file():
                new_checksums[archive_path] = _sha256_file(dest)
    for archive_path, _src in plan['retired']:
        if archive_path in failed_paths and archive_path in old_recorded:
            new_checksums[archive_path] = old_recorded[archive_path]
    try:
        _write_version_stamp(archive_root, _stamp_dict(manifest, new_checksums))
    except OSError as exc:
        failures.append(f'{VERSION_FILE}: {exc}')
        print(
            f'WARNING: could not write {VERSION_FILE}: {exc}. '
            'Your files were updated but the baseline was not recorded. '
            'Run `fha update-tools` again to re-record the state.',
            file=sys.stderr,
        )

    print()
    print(
        f'Done: {n_added_ok} added, {n_stock_ok} updated, {n_custom_ok} backed up '
        f'and updated, {n_retired_ok} retired, {n_current} already up to date.'
    )
    if backups_made:
        print(
            f'Your earlier versions are safe in {archive_root / BACKUP_DIR / date_str} — '
            f'review and delete them once you have reconciled your changes.'
        )
    # Files actually installed this run, plus the rewritten stamp.
    changed = [str(archive_root / p) for p in installed_ok]
    changed.append(str(archive_root / VERSION_FILE))
    update_data = {
        'added': n_added_ok, 'stock': n_stock_ok, 'customized': n_custom_ok,
        'retired': n_retired_ok, 'current': n_current,
    }
    if failures:
        print(file=sys.stderr)
        print(f'{len(failures)} file(s) could not be updated:', file=sys.stderr)
        for f in failures:
            print(f'  {f}', file=sys.stderr)
        print(
            'Close any program using those files (or check file permissions) and '
            'run `fha update-tools` again.',
            file=sys.stderr,
        )
        return Result(ok=False, exit_code=EXIT_WARNINGS, changed=changed,
                      data={**update_data, 'failures': failures})
    return Result(exit_code=EXIT_CLEAN, changed=changed, data=update_data)


def _report_plan(
    archive_root: Path,
    plan: dict,
    date_str: str,
    *,
    verbose: bool,
) -> None:
    """Print the would-do plan in plain English (dry-run preview only).

    Only ever called for `--dry-run`. The live run prints each file's outcome
    from inside the apply loop, after the operation succeeds, so real output
    never claims a success that did not happen (and the backup paths shown here
    are predictions, computed before anything moves).
    """
    for archive_path, _src in plan['added']:
        print(f'Would add {archive_path} (new).')

    for archive_path, _src in plan['stock']:
        print(f'Would update {archive_path} (unchanged from stock).')

    for archive_path, _src in plan['customized']:
        backup = _unique_backup_path(archive_root, archive_path, date_str)
        print(
            f'Would back up your edited {archive_path} to {backup} '
            f'and install the new version.'
        )

    for archive_path, _src in plan['retired']:
        backup = _unique_backup_path(archive_root, archive_path, date_str)
        print(
            f'Would move {archive_path} to {backup} — it is no longer part of the '
            f'plainfile tools (kept, not deleted).'
        )

    if verbose:
        for archive_path, _src in plan['current']:
            print(f'{archive_path} is already up to date.')


def _cmd_update_tools(args: argparse.Namespace) -> int:
    """argparse bridge for `fha update-tools`."""
    repo_arg = getattr(args, 'repo', None)
    if not repo_arg:
        print(
            'ERROR: run this command from inside your archive, with --repo '
            'pointing to your copy of the plainfile tools (the folder that '
            'contains manifest.json). Example:\n'
            '  fha update-tools --repo /path/to/plainfile-tools',
            file=sys.stderr,
        )
        return EXIT_FAILURE

    archive_root = getattr(args, 'root', None)
    if archive_root:
        # An explicit --root must still be an archive (the auto-detect branch
        # below enforces this via find_archive_root). Without this check, a typo
        # like `--root /tmp/typo` would scatter the operating layer into — or
        # create — the wrong directory, since update-tools writes files.
        archive_root = Path(archive_root).resolve()
        if not (archive_root / 'fha.yaml').is_file():
            print(
                f'ERROR: {archive_root} does not look like an archive (no fha.yaml '
                f'there). `fha update-tools` refreshes the tools inside an existing '
                f'archive — point --root at your archive folder (the one containing '
                f'fha.yaml), or use `fha install <new-folder>` to create one.',
                file=sys.stderr,
            )
            return EXIT_FAILURE
    else:
        detected = find_archive_root()
        if detected is None:
            print(
                'ERROR: this does not look like an archive (no fha.yaml found '
                'here or in any parent folder). Run `fha update-tools` from '
                'inside your archive, or add --root PATH pointing at it.',
                file=sys.stderr,
            )
            return EXIT_FAILURE
        archive_root = detected

    repo_root = _resolve_repo_root(repo_arg)
    try:
        return run_update_tools(
            archive_root,
            repo_root,
            dry_run=bool(getattr(args, 'dry_run', False)),
            verbose=bool(getattr(args, 'verbose', False)),
        ).exit_code
    except ScaffoldError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return EXIT_FAILURE


# ── CLI ─────────────────────────────────────────────────────────────────────────

def register(subs: argparse._SubParsersAction) -> None:
    """Register both `install` and `update-tools` onto the main fha parser."""
    p_install = subs.add_parser(
        'install',
        help='Bootstrap a new private archive with the plainfile operating layer.',
        description=(
            'Copy the plainfile tools, rulebooks, and docs into a new archive and '
            'stamp it. Run this once from your clone (or unzipped download) of the '
            'public tools. Afterwards, refresh with `fha update-tools`.'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_install.add_argument(
        'archive_path', metavar='ARCHIVE-PATH',
        help='Folder for your new archive (created if it does not exist).',
    )
    p_install.add_argument(
        '--repo', metavar='PATH',
        help='Your copy of the plainfile tools (folder with manifest.json). '
             'Defaults to the tools you are running from.',
    )
    p_install.add_argument(
        '--dry-run', action='store_true', dest='dry_run',
        help='Preview what would be installed; write nothing.',
    )
    p_install.set_defaults(func=_cmd_install)

    p_update = subs.add_parser(
        'update-tools',
        help='Refresh an archive\'s tools/rulebooks from an updated public clone.',
        description=(
            'Compare your archive against a newer copy of the public tools and '
            'pull in improvements. Never deletes and never overwrites your edits — '
            'anything you customized is backed up first. Run from inside your '
            'archive with --repo pointing at the updated tools.'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_update.add_argument(
        '--repo', metavar='PATH',
        help='Your updated copy of the plainfile tools (folder with manifest.json).',
    )
    p_update.add_argument(
        '--dry-run', action='store_true', dest='dry_run',
        help='Preview the update plan; write nothing.',
    )
    p_update.add_argument(
        '--verbose', action='store_true',
        help='Also list files that are already up to date.',
    )
    p_update.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    p_update.add_argument('--spec-root', metavar='PATH', help='Spec docs root (accepted for CLI consistency).')
    p_update.set_defaults(func=_cmd_update_tools)


def _standalone_main(argv: list[str] | None = None) -> int:
    """Entry point for `python tools/scaffold.py …`.

    Exposes `install` and `update-tools` (mirroring the `fha` surface) plus a
    maintenance-only `write-manifest` that regenerates manifest.json from a repo.
    """
    parser = argparse.ArgumentParser(
        prog='fha scaffold',
        description='Install / update the plainfile operating layer in an archive.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest='command', metavar='COMMAND')
    register(sub)

    p_manifest = sub.add_parser(
        'write-manifest',
        help='(maintainers) Regenerate manifest.json from this repo.',
    )
    p_manifest.add_argument('--repo', metavar='PATH', help='Repo root (default: this tools\' repo).')
    p_manifest.set_defaults(func=_cmd_write_manifest)

    args = parser.parse_args(argv)
    if not getattr(args, 'command', None):
        parser.print_help()
        return EXIT_CLEAN
    return args.func(args) or 0


def _cmd_write_manifest(args: argparse.Namespace) -> int:
    """argparse bridge for the maintenance `write-manifest` command."""
    repo_root = _resolve_repo_root(getattr(args, 'repo', None))
    try:
        path = _write_manifest(repo_root)
    except OSError as exc:
        print(f'ERROR: could not write manifest: {exc}', file=sys.stderr)
        return EXIT_FAILURE
    manifest = json.loads(path.read_text(encoding='utf-8'))
    print(f'Wrote {path} ({len(manifest["files"])} files, '
          f'spec_version {manifest["spec_version"]}).')
    return EXIT_CLEAN


if __name__ == '__main__':
    sys.exit(_standalone_main())
