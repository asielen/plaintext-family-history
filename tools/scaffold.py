#!/usr/bin/env python3
"""
scaffold.py - fha install / fha update-tools: vendor the operating layer into a
private archive and keep it current (TOOLING §13c, BUILD.md M9.1-M9.2).

A real family archive is a *separate, private* repository: the user's records
plus a vendored copy of the generic operating layer (the `tools/`, the spec docs,
the agent rulebooks, the human docs). This file is the ritual that copies that
operating layer in, and later refreshes it from an improved public clone -
without ever destroying the human's work.

THE MANIFEST (the package's own packing list)
---------------------------------------------
`manifest.json` at the repo root IS the definition of what belongs in an archive.
Every entry names a destination `path` (archive-relative), a content `sha256`, a
`spec_version`, and a `category`:

  - "operating"  - tools, spec docs, human docs. `fha update-tools` keeps these
                   current; the human may edit any of them and the checksum
                   compare protects the edit on the next update.
  - "skeleton"   - the empty starting structure: `fha.yaml`, the seeded
                   `places/places.yaml`, `inbox/_TEMPLATE.notes.md`, and the
                   `.gitkeep` files that hold the empty record directories.
                   These are written ONCE by `fha install` and never touched by
                   `update-tools`, because `fha.yaml` and `places.yaml` quickly
                   fill with the human's own configuration and data - refreshing
                   them would clobber that. (See the design note in
                   tools/README.md; surfaced as a TOOLING clarification.)

Skeleton entries carry a `src` field (their source path *inside* the public
repo, e.g. `archive-template/fha.yaml`) because their archive `path` strips the
`archive-template/` prefix - the template folder seeds the skeleton but is never
itself copied into an archive. Operating entries omit `src` (source == dest).

The manifest is committed data, regenerated from the repo by `_write_manifest`
(`python tools/scaffold.py write-manifest --repo .`). A regression test
(`tests/test_scaffold.py`) recomputes it and asserts it still matches what is
committed, so a PR that changes a tool but forgets to regenerate fails CI.

`fha install <archive-path> [--repo PATH]`  (run from a public-repo clone)
-------------------------------------------------------------------------
Preflight (Python ≥ 3.10; exiftool on PATH - a friendly heads-up, never a hard
stop), then copy every manifest file into the archive and stamp
`.plaintext-version` (the manifest version + the per-file checksums received).
Works from a git clone OR an unzipped download (`--repo` only needs a directory
containing `manifest.json`; `.git/` is never assumed) - the zip path is
first-class for non-technical users (docs/SETUP_FROM_ZIP.md).

`fha update-tools [--dry-run] --repo PATH`  (run from inside an archive)
-----------------------------------------------------------------------
Compare the public manifest against the archive's `.plaintext-version`, reconcile
only the OPERATING layer, and NEVER destroy anything:

  - new file in the manifest                    → copy it in
  - file unchanged from the stock you installed  → overwrite silently
  - file you customized (checksum differs)       → move yours to
                                                   .plaintext-backup/{date}/,
                                                   install stock, and report it
  - file retired from the manifest upstream      → move to .plaintext-backup/,
                                                   report (never deleted)

The governing principle: the updater only adds, replaces-pristine-with-stock, or
moves-aside-and-reports. The human is always the one who throws things away.

CODE MAP
--------
  Errors / checksums
    ScaffoldError              - friendly, message-carrying failure
    _sha256_bytes/_sha256_file - content checksums (binary, exact)

  Manifest definition + IO
    _operating_files           - the operating-layer file list (repo walk)
    _skeleton_files            - the skeleton file list (archive-template remap)
    generate_manifest          - build the manifest dict from a repo clone
    _write_manifest            - (maintenance) regenerate and write manifest.json
    load_manifest              - read+validate manifest.json from a repo dir
    _resolve_repo_root         - locate the clone/zip dir holding manifest.json

  Version stamp + backups
    _load_version_stamp        - read .plaintext-version (None if absent)
    _stamp_dict                - build a .plaintext-version payload
    _write_version_stamp       - write .plaintext-version
    _unique_backup_path        - collision-free .plaintext-backup/{date}/ path

  Install (M9.1)
    _preflight                 - Python/exiftool checks → (ok, messages)
    run_install                - create skeleton + copy operating layer + stamp
    _cmd_install               - argparse → run_install

  Update (M9.2)
    _plan_update               - classify every file (add/current/stock/customized/retired)
    run_update_tools           - execute (or preview) the plan, rewrite the stamp
    _cmd_update_tools          - argparse → run_update_tools

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
    VENDOR_DIR,
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
# never enters an archive (PRIVACY.md - the *public-repo* "no real data" policy,
# which is contradictory inside a real archive; RELEASE_CHECKLIST.md - the public
# release process; CNAME, manifest.json, .git*, …). TOOLING §13c / BUILD.md M9.1.
# README.md is shipped (project orientation a genealogist benefits from).
#
# The tool-BUILDING docs (the BUILD*.md family, TOOLING_INGESTION/INTERFACE,
# AGENTS_TOOLING) are deliberately NOT shipped: no tool reads them at run time
# and a genealogist operating an archive never needs them - they describe how to
# BUILD the tools, which is a workshop-clone activity. Extending vendored tools
# in place is out of scope; do it in the public repo and re-vendor.
_ROOT_OPERATING_DOCS = (
    'README.md',
    'SPEC.md',
    'TOOLING.md',
    'AGENTS.md',
    'CLAUDE.md',
)

# Root-level launchers that ship into every archive. serve.cmd is the
# double-clickable workbench launcher (plan 17); fha.cmd (Windows) and fha
# (POSIX `/bin/sh`, executable bit carried by shutil.copy2) are the terminal CLI
# shims so a genealogist types `fha <command>` / `./fha <command>` without ever
# naming the tools' path. All three are layout-agnostic - they probe for the
# entrypoint under .fha/tools/ first, then tools/ - so a single vendored file
# works whether the tools live flat or consolidated under .fha/. Both platforms
# ship to every archive: an archive is a portable folder that may well be opened
# on a different OS than the one that installed it. Enumerated like the root docs
# because the repo root also holds furniture that never enters an archive.
_ROOT_LAUNCHERS = (
    'serve.cmd',
    'fha.cmd',
    'fha',
)

# Subtrees walked whole for the operating layer. `.claude/skills/` carries the
# agent's genealogy workflow procedures (process-source, review-claims, …) - the
# "how to operate" an archive, so it ships. `.claude/settings.json` is *not*
# walked: it is this spec-repo's own agent config, not an archive's.
_OPERATING_SUBTREES = ('tools', 'docs', 'design', '.claude/skills')

# The archive subfolder that holds the vendored machinery, so a real archive's
# root reads as the genealogy - not the tooling. The install remaps the movable
# operating subtrees UNDER this prefix; the workshop repo itself stays flat, and
# the manifest's src/path seam records the repo-flat `src` against the archive
# `.fha/…` `path`.
#
# Only the MACHINERY moves: `tools/` (the program) and `design/` (its stylesheet
# and self-hosted fonts). Neither is ever opened by hand.
#
# `docs/` deliberately stays at the archive ROOT, alongside the rulebooks it is
# part of. It is human-facing reading matter, not machinery, and it is one half
# of a two-way link graph: the root rulebooks link to `docs/…`, and the docs link
# back to `../SPEC.md`, `../AGENTS.md`, `../README.md`. Remapping either side
# under `.fha/` breaks every one of those links in an installed archive - a
# `docs/…` link from a root rulebook would resolve to nothing, and `../SPEC.md`
# from a vendored doc would resolve inside `.fha/`, where no rulebook lives.
# Keeping docs at the root costs one visible folder and keeps the archive
# navigable in a plain file browser, with no install-time link rewriting anywhere
# in the install/update engine.
#
# Also deliberately at the archive root: the rulebooks themselves
# (SPEC/TOOLING/AGENTS/README/CLAUDE), the launchers, and `.claude/skills`
# (Claude Code discovers skills at the root). `_VENDOR_DIR` is the shared
# `_lib.VENDOR_DIR` so scaffold, serve, and doctor cannot drift on the name.
_VENDOR_DIR = VENDOR_DIR
_VENDORED_SUBTREES = ('tools', 'design')

# The template folder whose *contents* seed the skeleton. The folder itself is
# never copied into an archive - each file's archive path strips this prefix.
_SKELETON_SRC_DIR = 'archive-template'

# A file under archive-template/ that is repo furniture, not skeleton: it tells a
# human how to start an archive, which the docs/ guides already cover.
_SKELETON_EXCLUDE = {'README.md'}

# Files that live under an operating subtree but are user-owned skeleton seeds:
# install-once, then never touched by `update-tools`. `design/custom.css` is the
# per-archive style hook - stock on install so a genealogist has something to
# customize, and preserved across updates so a hand-edit is not clobbered. It
# rides into `.fha/design/` with the rest of the design package, so site.py's
# `parent.parent/design` read finds it with no special-casing.
# (archive_path, in-repo src path.)
_SKELETON_OVERRIDES: tuple[tuple[str, str], ...] = (
    (f'{_VENDOR_DIR}/design/custom.css', 'design/custom.css'),
)
# Skipped during the operating-file walk BY SRC path (the walk yields
# repo-relative paths): these are seeded once as skeleton, never updated.
_SKELETON_OVERRIDE_SRCS = frozenset(s for _, s in _SKELETON_OVERRIDES)

# The two on-disk footprints of the updater. Both are safe to inspect or delete
# by hand; neither is ever copied or compared as part of the operating layer.
VERSION_FILE = '.plaintext-version'
BACKUP_DIR = '.plaintext-backup'


# ── Errors / checksums ─────────────────────────────────────────────────────────

class ScaffoldError(Exception):
    """A failure with a plain, human-facing message and a next step.

    Raised inside the run_* helpers and caught at the CLI boundary, so the
    non-technical user never sees a traceback - only the message, which always
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
    their cross-links intact in an installed archive.

    The machinery subtrees (tools/, design/) install UNDER .fha/ (see
    _VENDOR_DIR) so the archive root reads as the genealogy, not the tooling;
    their archive path is `.fha/…` while the repo source stays flat, recorded by
    the manifest's src/path seam. The root rulebooks, docs/, the launchers, and
    .claude/skills keep source == archive path at the root - docs/ among them so
    its two-way link graph with the rulebooks survives an install (_VENDOR_DIR).
    """
    out: list[tuple[str, Path]] = []

    for name in _ROOT_OPERATING_DOCS:
        src = repo_root / name
        if src.is_file():
            out.append((name, src))

    for name in _ROOT_LAUNCHERS:
        src = repo_root / name
        if src.is_file():
            out.append((name, src))

    for sub in _OPERATING_SUBTREES:
        base = repo_root / sub
        if not base.is_dir():
            continue
        moved = sub in _VENDORED_SUBTREES
        for p in sorted(base.rglob('*')):
            if not p.is_file():
                continue
            if '__pycache__' in p.parts or p.suffix in ('.pyc', '.pyo'):
                continue
            rel = p.relative_to(repo_root).as_posix()
            # Skip skeleton-override files - they live under an operating
            # subtree but are user-owned (see _SKELETON_OVERRIDES).
            if rel in _SKELETON_OVERRIDE_SRCS:
                continue
            # Vendored subtrees (tools/, design/) install UNDER .fha/ so the
            # archive root stays uncluttered; docs/ and .claude/skills stay at
            # the root (readable documentation / agent-discovered skills).
            archive_path = f'{_VENDOR_DIR}/{rel}' if moved else rel
            out.append((archive_path, p))

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
    # Also seed skeleton overrides that live outside archive-template/ - the
    # per-archive style hook design/custom.css, whose stock copy is the
    # in-repo file.
    for archive_path, src_rel in _SKELETON_OVERRIDES:
        src = repo_root / src_rel
        if src.is_file():
            out.append((archive_path, src))
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

    Not part of the `fha` command surface - invoked by a tool-builder via
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
            f"plaintext tools - the folder that contains manifest.json, SPEC.md, "
            f"and the tools/ folder (a git clone or an unzipped download both work)."
        )
    try:
        manifest = json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as exc:
        raise ScaffoldError(
            f"could not read {path}: {exc}. Re-download the plaintext tools and "
            f"try again."
        ) from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get('files'), list):
        raise ScaffoldError(
            f"{path} is not a valid manifest (expected a 'files' list). "
            f"Re-download the plaintext tools and try again."
        )
    return manifest


def _resolve_repo_root(repo_arg: str | None) -> Path:
    """Resolve the public-repo directory holding manifest.json.

    When --repo is given, use it. Otherwise default to this file's repo (two
    levels up from tools/scaffold.py) - correct for `fha install` run from a
    clone or an unzipped download, where the running tools ARE the source. The
    caller (install vs update) decides whether a default is acceptable; this only
    resolves the path.
    """
    if repo_arg:
        return Path(repo_arg).resolve()
    return Path(__file__).resolve().parents[1]


# ── Version stamp + backups ─────────────────────────────────────────────────────

def _load_version_stamp(archive_root: Path) -> dict | None:
    """Read .plaintext-version; return None if absent, raise on corruption."""
    path = archive_root / VERSION_FILE
    if not path.is_file():
        return None
    try:
        stamp = json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as exc:
        raise ScaffoldError(
            f"{path} is unreadable ({exc}). Delete it and run `fha update-tools` "
            f"again - it will be rewritten (your tool files are not touched by "
            f"reading it)."
        ) from exc
    if not isinstance(stamp, dict):
        return None
    return stamp


def _stamp_dict(manifest: dict, checksums: dict[str, str]) -> dict:
    """Build a .plaintext-version payload from a manifest + the checksums installed."""
    return {
        'manifest_version': manifest.get('manifest_version', MANIFEST_VERSION),
        'spec_version': manifest.get('spec_version', 'unknown'),
        'installed': datetime.datetime.now().isoformat(timespec='seconds'),
        'files': dict(sorted(checksums.items())),
    }


def _write_version_stamp(archive_root: Path, stamp: dict) -> None:
    """Write .plaintext-version (pretty JSON, trailing newline)."""
    path = archive_root / VERSION_FILE
    path.write_text(json.dumps(stamp, indent=2) + '\n', encoding='utf-8')


def _unique_backup_path(archive_root: Path, rel_path: str, date_str: str) -> Path:
    """Compute a collision-free .plaintext-backup/{date}/{rel_path} destination.

    Backups preserve the archive-relative subtree (so a backed-up tools/fha.py
    lands at .plaintext-backup/{date}/tools/fha.py). If that target already
    exists - e.g. two updates the same day each move a re-edited file - a numeric
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

    Plain, friendly guidance - never a traceback or a bare "not found":
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
    file into `archive_path`, then writes `.plaintext-version` recording the
    manifest version and the per-file checksums received. Refuses an archive that
    already carries tools (a `.plaintext-version` or `tools/fha.py`) and points
    the human at `fha update-tools` instead - install is a one-time bootstrap.

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
            f"{archive_path} already has the plaintext tools installed. To refresh "
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
            f"your copy of the plaintext tools is missing {len(missing)} file(s) "
            f"the manifest expects:\n  {listing}{more}\n"
            f"Re-download or re-clone the tools, then run install again."
        )

    # Preflight: refuse to overwrite existing user content in skeleton destinations.
    # The guard above already blocks double-installs; this catches the case where a
    # user hand-started an archive (e.g. wrote fha.yaml or seeded their own README)
    # before running `fha install`.
    # Exception: if a skeleton file is byte-for-byte identical to what install would
    # place (sha256 match) it was left by a partial previous install that never wrote
    # the stamp - safe to overwrite so the user can simply re-run install to finish.
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
        print(f'Dry run - would install into: {archive_path}')
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

    print(f'Installed the plaintext tools into: {archive_path}')
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
      added      - in the manifest, not on disk yet
      current    - on disk and already byte-identical to the new stock (no-op)
      stock      - on disk, unchanged from the stock you installed, stock improved
                   → overwrite silently
      customized - on disk and different from what you installed → back up + install
      retired    - recorded in .plaintext-version but gone from the manifest, and
                   still on disk → move to backup

    Only "operating" files are considered. Skeleton seeds (fha.yaml, places.yaml,
    the template, .gitkeep) are install-once and deliberately untouched here, so
    `update-tools` can never clobber the human's configuration or place data.
    """
    recorded: dict[str, str] = _stamp_file_map(stamp)

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


def _running_from(archive_root: Path) -> bool:
    """True when the tools executing this command live inside `archive_root`.

    Used to warn before an update would retire the very folder it is running
    from. Resolved on both sides so a symlinked or relative invocation still
    compares honestly; any resolution failure answers False (warn about nothing
    rather than block on a path quirk).
    """
    try:
        here = Path(__file__).resolve()
        root = Path(archive_root).resolve()
    except OSError:
        return False
    return root == here or root in here.parents


def _plan_install_once_relocations(
    archive_root: Path,
    manifest: dict,
    stamp: dict | None,
) -> list[tuple[str, str]]:
    """Find install-once files whose archive path moved between vendor layouts.

    Returns [(old_archive_path, new_archive_path), …].

    Install-once seeds (`_SKELETON_OVERRIDES`, today `design/custom.css`) are
    deliberately never touched by `update-tools` - that is what keeps a hand-edit
    safe. But when the layout changes, the seed's archive path changes with it
    (`design/custom.css` -> `.fha/design/custom.css`), and the two halves of the
    normal reconciliation both do the wrong thing with the old copy: the manifest
    no longer lists the old path, so it is classified RETIRED and moved into
    `.plaintext-backup/`, and updates never install skeleton entries, so nothing
    replaces it. An owner who had customized their stylesheet would find it gone
    from the archive - recoverable only by hand out of the backup folder.

    So detect the transition first and MOVE the file to its new path, carrying
    the customization with it. Matched conservatively: only a recorded path that
    the manifest has dropped, whose vendor-prefixed twin the manifest lists as a
    SKELETON entry, and where the destination does not already exist.
    """
    recorded = _stamp_file_map(stamp)
    if not recorded:
        return []
    skeleton_paths = {e['path'] for e in manifest['files']
                      if e.get('category') == 'skeleton'}
    manifest_all_paths = {e['path'] for e in manifest['files']}

    moves: list[tuple[str, str]] = []
    for old in sorted(recorded):
        if old in manifest_all_paths:
            continue
        # Both directions, so a future un-vendoring is handled as well as the
        # flat -> .fha/ move this release performs.
        if old.startswith(f'{VENDOR_DIR}/'):
            new = old[len(VENDOR_DIR) + 1:]
        else:
            new = f'{VENDOR_DIR}/{old}'
        if new not in skeleton_paths:
            continue
        if not (archive_root / old).is_file():
            continue
        if (archive_root / new).exists():
            continue
        moves.append((old, new))
    return moves


def _apply_install_once_relocations(
    archive_root: Path,
    stamp: dict | None,
    moves: list[tuple[str, str]],
    *,
    dry_run: bool,
) -> list[str]:
    """Move relocated install-once files and re-key the stamp in memory.

    The in-memory re-key happens in BOTH modes so the rest of the run - the
    retired scan in `_plan_update` and the skeleton carry-over when the stamp is
    rewritten - sees the file at its new home. That makes a `--dry-run` plan an
    honest preview of the real run rather than one that reports the file retired.
    Returns human-readable failure messages (empty on success); a file that
    cannot be moved is left exactly where it is.
    """
    if not moves:
        return []
    failures: list[str] = []
    applied: list[tuple[str, str]] = []
    for old, new in moves:
        if dry_run:
            applied.append((old, new))
            continue
        dest = archive_root / new
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(archive_root / old), str(dest))
        except OSError as exc:
            failures.append(
                f'{old}: could not move to {new} ({exc}). Your copy is untouched '
                f'at {archive_root / old} - move it to {dest} by hand.')
            continue
        applied.append((old, new))

    if isinstance(stamp, dict) and applied:
        files = _stamp_file_map(stamp)
        for old, new in applied:
            files[new] = files.pop(old, files.get(new, ''))
        stamp['files'] = dict(sorted(files.items()))
    return failures


def run_update_tools(
    archive_root: Path,
    repo_root: Path,
    *,
    dry_run: bool = False,
    verbose: bool = False,
) -> Result:
    """Refresh the operating layer in an existing archive from a public clone.

    Reads the public manifest and the archive's `.plaintext-version`, classifies
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
            f'No {VERSION_FILE} found in {archive_root} - treating existing tool '
            f'files as your own work. Anything different from the new version is '
            f'backed up (never overwritten), not replaced silently.'
        )
        print()

    # Before any normal reconciliation: carry install-once files (design/custom.css)
    # across a vendor-layout change, so a customized stylesheet is never retired
    # into the backup folder with nothing installed in its place.
    relocations = _plan_install_once_relocations(archive_root, manifest, stamp)
    for old, new in relocations:
        prefix = '[dry-run] would move' if dry_run else 'Moved'
        print(f'{prefix} your {old} to {new} (the tools folder layout changed; '
              'your customizations come with it).')
    relocation_failures = _apply_install_once_relocations(
        archive_root, stamp, relocations, dry_run=dry_run)
    for message in relocation_failures:
        print(f'WARNING: {message}', file=sys.stderr)
    if relocations:
        print()

    # An update that also changes the layout retires the old flat tools/ into
    # .plaintext-backup/ and installs fresh ones under .fha/. That is correct but
    # it is the long way round, and if the tools being retired are the ones
    # RUNNING this command, the update is moving the toolchain out from under
    # itself mid-run. `migrate-layout` does the same transition as a plain move,
    # in one step, with nothing to back up - so point at it before starting.
    if _running_from(archive_root) and (archive_root / 'tools').is_dir() \
            and not (archive_root / VENDOR_DIR / 'tools').is_dir():
        print(
            f'NOTE: this archive still keeps its tools at the root, and the new '
            f'layout puts them under {VENDOR_DIR}/. This update can make that '
            f'change, but it does it by backing the old tools up and installing '
            f'new ones - while running from the very folder it is retiring.\n'
            f'      The direct route is one command, run from your workshop copy:\n'
            f'        fha migrate-layout --root "{archive_root}" --dry-run   '
            f'(then without --dry-run)\n'
            f'      Then re-run this update. Continuing anyway is safe for your '
            f'records either way - nothing here touches them.'
        )
        print()

    plan = _plan_update(archive_root, repo_root, manifest, stamp)
    date_str = datetime.date.today().isoformat()

    # A broken/partial clone must fail before any mutation - otherwise a
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
            f"your copy of the plaintext tools is missing {len(missing)} file(s) "
            f"the manifest expects:\n  {listing}{more}\n"
            f"Re-download or re-clone the tools, then run `fha update-tools` again."
        )

    n_added = len(plan['added'])
    n_stock = len(plan['stock'])
    n_custom = len(plan['customized'])
    n_retired = len(plan['retired'])
    n_current = len(plan['current'])

    if dry_run:
        print(f'Dry run - comparing {archive_root} against {repo_root / "manifest.json"}:')
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
    # what actually happened - the output never claims a success that did not occur.
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
            f'Your edited {archive_path} has been backed up to {backup} - '
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
            f'Moved {archive_path} to {backup} - it is no longer part of the '
            f'plaintext tools (kept, not deleted).'
        )

    if verbose:
        for archive_path, _src in plan['current']:
            print(f'{archive_path} is already up to date.')

    # Rewrite the stamp. Each recorded checksum is an operating file's *installed
    # baseline* - what the next run compares the working copy against:
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
    old_recorded = _stamp_file_map(stamp)
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
            f'Your earlier versions are safe in {archive_root / BACKUP_DIR / date_str} - '
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
            f'Would move {archive_path} to {backup} - it is no longer part of the '
            f'plaintext tools (kept, not deleted).'
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
            'pointing to your copy of the plaintext tools (the folder that '
            'contains manifest.json). Example:\n'
            '  fha update-tools --repo /path/to/plaintext-tools',
            file=sys.stderr,
        )
        return EXIT_FAILURE

    archive_root = getattr(args, 'root', None)
    if archive_root:
        # An explicit --root must still be an archive (the auto-detect branch
        # below enforces this via find_archive_root). Without this check, a typo
        # like `--root /tmp/typo` would scatter the operating layer into - or
        # create - the wrong directory, since update-tools writes files.
        archive_root = Path(archive_root).resolve()
        if not (archive_root / 'fha.yaml').is_file():
            print(
                f'ERROR: {archive_root} does not look like an archive (no fha.yaml '
                f'there). `fha update-tools` refreshes the tools inside an existing '
                f'archive - point --root at your archive folder (the one containing '
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


# ── Migrate layout (flat -> .fha/) ───────────────────────────────────────────────

def _stamp_file_map(stamp: dict | None) -> dict[str, str]:
    """Return a stamp's `files` map, or `{}` when it is missing or malformed.

    `.plaintext-version` is a plain JSON file a human can open, edit, and damage.
    A hand-mangled `files` that is valid JSON but not an object (a list, a
    string, null) would otherwise blow up on `.items()` deep inside a MUTATING
    command - the user sees a traceback from `migrate-layout` or `update-tools`
    after folders have already moved, instead of a clear message. Treat any
    non-object shape as "nothing recorded": every operating file is then
    re-checked against the manifest, which is the safe direction (files get
    backed up rather than overwritten), and the command re-stamps cleanly.
    Non-string values are dropped for the same reason - a checksum comparison
    against a list is meaningless.
    """
    if not isinstance(stamp, dict):
        return {}
    files = stamp.get('files')
    if not isinstance(files, dict):
        return {}
    return {k: v for k, v in files.items()
            if isinstance(k, str) and isinstance(v, str)}


def _stale_root_launchers(archive_root: Path) -> list[str]:
    """Name the root launchers that would not survive a move to the `.fha/` layout.

    A pre-`.fha` archive was installed with launchers that name `tools\\fha.py`
    directly and predate `fha.cmd` / `fha` entirely. Moving `tools/` out from
    under them breaks double-clicking `serve.cmd` and leaves no CLI shim at all,
    so the migrator has to say so (and refresh them when it can). "Stale" means
    either absent, or present but with no mention of the vendor folder - the
    current launchers all probe `.fha/tools/` first.
    """
    stale: list[str] = []
    for name in _ROOT_LAUNCHERS:
        path = archive_root / name
        if not path.is_file():
            stale.append(name)
            continue
        try:
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            stale.append(name)
            continue
        if VENDOR_DIR not in text:
            stale.append(name)
    return stale


def _refresh_root_launchers(archive_root: Path, repo_root: Path,
                            names: list[str]) -> tuple[list[str], list[str]]:
    """Copy the layout-agnostic root launchers in from `repo_root`.

    Returns (refreshed, unavailable). A launcher the source copy does not have
    (running from inside the archive being migrated, where `repo_root` is
    `.fha/`, not a workshop clone) lands in `unavailable` so the caller can
    print the exact follow-up command instead of silently leaving it broken.
    An existing launcher is overwritten deliberately: these are generated shims,
    never a place for a hand-edit, and the pre-`.fha` copy is precisely what is
    broken. `copy2` carries the POSIX executable bit for `fha`.
    """
    refreshed: list[str] = []
    unavailable: list[str] = []
    for name in names:
        src = repo_root / name
        if not src.is_file():
            unavailable.append(name)
            continue
        try:
            shutil.copy2(src, archive_root / name)
        except OSError:
            unavailable.append(name)
            continue
        refreshed.append(name)
    return refreshed, unavailable


def _capture_host_installed() -> Path | None:
    """Return the installed browser capture-host manifest path, or None.

    The native-messaging host registered by `fha capture --install-host` holds a
    launcher whose generated command names an ABSOLUTE `<archive>/tools/fha.py`.
    Moving `tools/` leaves that registration pointing at nothing, and browser
    capture silently stops launching - `doctor` does not inspect it, so nothing
    else would ever tell the owner. Detect it so the migrator can name the
    re-registration command. Probing is best-effort and never fatal: capture.py
    owns the per-OS locations, and a failure to import or resolve them simply
    means we fall back to mentioning the command unconditionally.
    """
    try:
        import capture  # local import: only migrate-layout needs it
        for browser in ('chrome', 'edge'):
            try:
                manifest = (capture._native_manifest_dir(browser)
                            / f'{capture._NATIVE_HOST_NAME}.json')
            except Exception:
                continue
            if manifest.is_file():
                return manifest
    except Exception:
        return None
    return None


def run_migrate_layout(archive_root: Path, *, dry_run: bool = False,
                       repo_root: Path | None = None) -> Result:
    """Move an existing FLAT archive's machinery under `.fha/` (one-time).

    A pre-`.fha` archive keeps tools/ and design/ at its root. This moves those
    subtrees into `.fha/` and re-keys the `.plaintext-version` stamp so the flat
    operating paths become `.fha/…` paths. It is a plain file MOVE, so a
    hand-edited tool stays edited (customizations are preserved, not reset).
    `docs/`, the root rulebooks (SPEC/TOOLING/AGENTS/README/CLAUDE), `.claude/`,
    and every record and asset are untouched. Idempotent: an already-migrated
    archive is a clean no-op.

    The root launchers are refreshed as part of the move. A pre-`.fha` archive's
    `serve.cmd` runs `tools\\fha.py` by name and its `fha.cmd`/`fha` shims do not
    exist at all, so moving `tools/` without replacing them would break
    double-clicking `serve.cmd` and leave no CLI entry point. `repo_root`
    supplies the current copies (defaults to the running tools' own repo root);
    any the source cannot supply are reported with the command that installs them.

    Safest run from the workshop against the archive (`fha migrate-layout --root
    ARCHIVE`), so the running tools are not the ones being moved - and so the
    launcher sources are at hand.
    """
    archive_root = Path(archive_root).resolve()
    if repo_root is None:
        repo_root = _resolve_repo_root(None)
    repo_root = Path(repo_root).resolve()
    vendor = archive_root / VENDOR_DIR
    present = [s for s in _VENDORED_SUBTREES if (archive_root / s).is_dir()]
    subtree_list = ', '.join(f'{s}/' for s in _VENDORED_SUBTREES)

    if not present:
        if (vendor / 'tools').is_dir():
            print(f'{archive_root} already uses the {VENDOR_DIR}/ layout - '
                  'nothing to migrate.')
            return Result(exit_code=EXIT_CLEAN, data={'moved': 0})
        raise ScaffoldError(
            f'{archive_root} has no {subtree_list} folder at its root, so there is '
            f'nothing to migrate. Run this from inside a pre-{VENDOR_DIR} archive, '
            'or pass --root PATH pointing at one.')

    # Refuse a half-migrated / ambiguous state rather than guessing.
    conflicts = [s for s in present if (vendor / s).exists()]
    if conflicts:
        raise ScaffoldError(
            f'both a flat and a {VENDOR_DIR}/ copy of {conflicts[0]}/ exist in '
            f'{archive_root} - refusing to guess which is current. Move or remove '
            'one by hand, then re-run.')

    stale_launchers = _stale_root_launchers(archive_root)
    capture_host = _capture_host_installed()

    if dry_run:
        print(f'[dry-run] Would create {VENDOR_DIR}/ under {archive_root} and move into it:')
        for s in present:
            print(f'[dry-run]   {s}/ -> {VENDOR_DIR}/{s}/')
        print(f'[dry-run] Would re-key .plaintext-version '
              f'({subtree_list} -> {VENDOR_DIR}/…).')
        if stale_launchers:
            have = [n for n in stale_launchers if (repo_root / n).is_file()]
            missing = [n for n in stale_launchers if n not in have]
            if have:
                print(f'[dry-run] Would refresh the root launcher(s) '
                      f'{", ".join(have)} so they find the moved tools.')
            if missing:
                print(f'[dry-run] Could NOT refresh {", ".join(missing)} from '
                      f'{repo_root} - `fha update-tools --repo PATH-TO-WORKSHOP` '
                      'would be needed afterwards.')
        if capture_host:
            print(f'[dry-run] Browser capture host found at {capture_host} - it '
                  'would need re-registering afterwards '
                  '(`fha capture --install-host`).')
        print('[dry-run] Records, rulebooks, docs/, and .claude/ stay put. '
              'Nothing was written.')
        return Result(exit_code=EXIT_CLEAN, data={'moved': len(present)})

    moved_done: list[tuple[Path, Path]] = []
    try:
        vendor.mkdir(parents=True, exist_ok=True)
        for s in present:
            src = archive_root / s
            dest = vendor / s
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            moved_done.append((src, dest))
    except OSError as exc:
        # Roll back what did move. A rollback that ITSELF fails leaves the
        # archive genuinely half-moved, so track it and say so - claiming a
        # clean rollback while part of the operating layer sits under .fha/
        # would send the owner to re-run a command that cannot succeed.
        stranded: list[str] = []
        for src, dest in reversed(moved_done):
            try:
                shutil.move(str(dest), str(src))
            except OSError:
                stranded.append(f'{dest} (belongs at {src})')
        if stranded:
            raise ScaffoldError(
                f'could not move the operating layer under {VENDOR_DIR}/: {exc}. '
                f'The rollback did not fully succeed either, so this archive is '
                f'HALF-MIGRATED: {"; ".join(reversed(stranded))}. Nothing was '
                'deleted - move each of those folders back to the path named in '
                'brackets by hand (or leave them and finish the migration by '
                'moving the rest), then re-run. Close anything open in those '
                'folders and check permissions first.') from exc
        raise ScaffoldError(
            f'could not move the operating layer under {VENDOR_DIR}/: {exc}. '
            'Nothing was left half-moved (rolled back). Close anything open in '
            'those folders, check permissions and disk space, then re-run.') from exc

    # Re-key the update stamp: flat operating paths -> .fha/ paths (records and
    # skeleton keys stay as they are). A missing, unreadable, or malformed stamp
    # is non-fatal - the next `update-tools` will re-stamp cleanly - but it must
    # not raise here, after the folders have already moved.
    stamp_path = archive_root / VERSION_FILE
    if stamp_path.is_file():
        try:
            stamp = json.loads(stamp_path.read_text(encoding='utf-8'))
            if isinstance(stamp, dict):
                rekeyed: dict[str, str] = {}
                for key, val in _stamp_file_map(stamp).items():
                    first = key.split('/', 1)[0]
                    rekeyed[f'{VENDOR_DIR}/{key}' if first in _VENDORED_SUBTREES
                            else key] = val
                stamp['files'] = dict(sorted(rekeyed.items()))
                _write_version_stamp(archive_root, stamp)
        except (OSError, ValueError):
            pass

    refreshed, unavailable = _refresh_root_launchers(
        archive_root, repo_root, stale_launchers)

    print(f'Migrated {archive_root} to the {VENDOR_DIR}/ layout:')
    for s in present:
        print(f'  {s}/ -> {VENDOR_DIR}/{s}/')
    if refreshed:
        print(f'  refreshed the root launcher(s): {", ".join(refreshed)}')
    print('Records, the rulebooks (SPEC/TOOLING/AGENTS/README/CLAUDE), docs/, '
          'and .claude/ stayed at the archive root.')
    print('\nNext:')
    step = 1
    if unavailable:
        print(f'  {step}. IMPORTANT: the root launcher(s) {", ".join(unavailable)} '
              f'still point at the old flat layout (or are missing) and were not '
              f'available in {repo_root} to refresh. Run '
              '`fha update-tools --repo PATH-TO-WORKSHOP` to install the current '
              'ones - until then, double-clicking serve.cmd will not start.')
        step += 1
    if capture_host:
        print(f'  {step}. Re-register browser capture: the host at {capture_host} '
              'still launches the tools from their old path. Re-run '
              '`fha capture --install-host` with the same browser and extension '
              'settings you used before.')
        step += 1
    print(f'  {step}. Run `fha doctor` to confirm, then `fha index` to refresh the cache.')
    return Result(exit_code=EXIT_CLEAN,
                  changed=[str(d) for _, d in moved_done]
                          + [str(archive_root / n) for n in refreshed],
                  data={'moved': len(present), 'launchers_refreshed': len(refreshed),
                        'launchers_pending': len(unavailable)})


def _cmd_migrate_layout(args: argparse.Namespace) -> int:
    """argparse bridge for `fha migrate-layout`."""
    root_arg = getattr(args, 'root', None)
    if root_arg:
        archive_root = Path(root_arg).resolve()
        if not (archive_root / 'fha.yaml').is_file():
            print(
                f'ERROR: {archive_root} is not an archive (no fha.yaml). Point '
                '--root at your archive folder (the one containing fha.yaml).',
                file=sys.stderr,
            )
            return EXIT_FAILURE
    else:
        detected = find_archive_root()
        if detected is None:
            print(
                'ERROR: this does not look like an archive (no fha.yaml found here '
                'or in any parent folder). Run `fha migrate-layout` from inside your '
                'archive, or add --root PATH pointing at it.',
                file=sys.stderr,
            )
            return EXIT_FAILURE
        archive_root = detected
    try:
        return run_migrate_layout(
            archive_root,
            dry_run=bool(getattr(args, 'dry_run', False)),
            repo_root=_resolve_repo_root(getattr(args, 'repo', None)),
        ).exit_code
    except ScaffoldError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return EXIT_FAILURE


# ── CLI ─────────────────────────────────────────────────────────────────────────

def register(subs: argparse._SubParsersAction) -> None:
    """Register `install`, `update-tools`, and `migrate-layout` on the fha parser."""
    p_install = subs.add_parser(
        'install',
        help='Bootstrap a new private archive with the plaintext operating layer.',
        description=(
            'Copy the plaintext tools, rulebooks, and docs into a new archive and '
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
        help='Your copy of the plaintext tools (folder with manifest.json). '
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
            'pull in improvements. Never deletes and never overwrites your edits - '
            'anything you customized is backed up first. Run from inside your '
            'archive with --repo pointing at the updated tools.'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_update.add_argument(
        '--repo', metavar='PATH',
        help='Your updated copy of the plaintext tools (folder with manifest.json).',
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
    p_update.set_defaults(func=_cmd_update_tools)

    p_migrate = subs.add_parser(
        'migrate-layout',
        help='One-time: move an older flat archive\'s tools/ and design/ under .fha/.',
        description=(
            'Move an existing archive\'s machinery (tools/, design/) into a '
            'hidden .fha/ folder so the archive root shows only your genealogy '
            'and the documents that explain it. Records, the rulebooks, docs/, '
            'and .claude/ stay at the root; customizations are preserved (it is '
            'a plain file move). The root launchers are refreshed so they find '
            'the moved tools - pass --repo if you are running this from inside '
            'the archive. Idempotent. Safest run from the workshop with --root '
            'pointing at the archive. Preview with --dry-run first.'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_migrate.add_argument(
        '--dry-run', action='store_true', dest='dry_run',
        help='Preview the move; write nothing.',
    )
    p_migrate.add_argument(
        '--repo', metavar='PATH',
        help='Your copy of the plaintext tools, used to refresh the root launchers.',
    )
    p_migrate.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    p_migrate.set_defaults(func=_cmd_migrate_layout)


def _standalone_main(argv: list[str] | None = None) -> int:
    """Entry point for `python tools/scaffold.py …`.

    Exposes `install` and `update-tools` (mirroring the `fha` surface) plus a
    maintenance-only `write-manifest` that regenerates manifest.json from a repo.
    """
    parser = argparse.ArgumentParser(
        prog='fha scaffold',
        description='Install / update the plaintext operating layer in an archive.',
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
