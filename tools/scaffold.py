#!/usr/bin/env python3
"""
scaffold.py - fha install / fha update-tools: vendor the operating layer into a
private archive and keep it current (TOOLING §13c, BUILD.md M9.1-M9.2).

A real family archive is a *separate, private* repository: the user's records
plus a vendored copy of the generic operating layer (the `tools/`, the spec docs,
the agent rulebooks, the human docs, the capture browser extension). This file is
the ritual that copies that operating layer in, and later refreshes it from an
improved public clone - without ever destroying the human's work.

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

A folder in the clone that will not list REFUSES the regeneration
(`generate_manifest` raises ScaffoldError). `rglob` reports an unlistable
folder as an empty one, and a short packing list is not a smaller download:
`update-tools` reads a file the list does not name as retired upstream and
moves the installed copy aside in every archive that updates. One re-run of
`write-manifest` is the cheap side of that trade.

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
    _repo_relative             - a clone folder's name without the local path
    _walk_repo_files           - the repo walk WITH an unreadable-folder seam
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

  External asset roots (#124) - documents/photos/inbox skeleton folders skipped
  or pruned when `roots:` points them outside the archive, or relocated when
  `roots:` renames one but keeps it inside (external_asset_roots itself lives
  in _lib.py, shared with any tool that needs the same question)
    _external_root_skeleton_paths - install-time: manifest paths NOT to copy
    _internal_root_renames      - alias -> its current folder, for a rename
                                 that stays inside the archive (TOOLING §13c)
    _remap_skeleton_path        - rewrite a skeleton path's alias segment
                                 through _internal_root_renames
    _placeholder_only_scaffold_litter - recursive, byte-for-byte: does a
                                 placeholder folder hold nothing but this
                                 install's own shipped bytes?
    _alias_seed_shas            - an alias's own skeleton checksums, preferring
                                 this archive's OWN installed baseline (its
                                 .plaintext-version stamp) over today's manifest
    _prune_external_root_placeholders - update-time: remove (or preview removing)
                                 an already-installed placeholder that just went
                                 external and holds nothing but scaffold litter;
                                 reports any removal that failed
    _prune_orphaned_literal_root_folders - update-time: same treatment for the
                                 literal alias folder an INTERNAL rename orphaned
    _restore_pruned_placeholders - best-effort undo of a prune whose removal
                                 the stamp write then failed to record

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
import os
import re
import shutil
import sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    ASSET_ROOT_ALIASES,
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    VENDOR_DIR,
    FhaConfigError,
    Result,
    configure_utf8_stdout,
    external_asset_roots,
    find_archive_root,
    get_roots,
    load_fha_yaml,
    pip_command,
    roots_change_orphans,
    unreadable_dir_recorder,
    walk_files,
    yaml_available,
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
#
# Docs are split by AUDIENCE, and the split is expressed through LOCATION (owner
# decision 2026-08-16). The two an archive owner needs on day one -
# GETTING_STARTED.md (what to do) and CHEATSHEET.md (the one printable page) -
# sit at the archive root where they cannot be missed; the rest of the owner's
# manual stays in `docs/` and is linked from those two. They live at the REPO
# root too, not under docs/: there is no install-time link rewriting anywhere in
# this file, so a doc whose repo depth differs from its archive depth would have
# relative links that resolve in exactly one of the two contexts.
#
# README.md is deliberately NOT shipped. It is repo-facing - badges, a milestone
# roadmap, contributing, and links to example-archive/, quickstart-template/,
# archive-template/ and obsidian-templater/, none of which an archive receives -
# so an owner opening it got a page of dead links about someone else's project.
# What an owner genuinely needed from it (what the archive is, the record types,
# the two-repo relationship, backup, Obsidian) is folded into GETTING_STARTED.md
# and CHEATSHEET.md instead. An installed archive has no root README.md; the
# entry point is GETTING_STARTED.md, whose name says what to do with it.
#
# The tool-BUILDING docs (the BUILD*.md family, TOOLING_INGESTION/INTERFACE,
# AGENTS_TOOLING) are likewise NOT shipped: no tool reads them at run time
# and a genealogist operating an archive never needs them - they describe how to
# BUILD the tools, which is a workshop-clone activity. Extending vendored tools
# in place is out of scope; do it in the public repo and re-vendor.
_ROOT_OPERATING_DOCS = (
    'GETTING_STARTED.md',
    'CHEATSHEET.md',
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
# Paths whose executable bit is a REQUIREMENT of the file, not a property of
# whatever copy of the repo we happen to be installing from. A workshop unzipped
# from a download has no Unix modes at all, so its `fha` arrives 0644 - and
# deriving "should this be executable?" from that source mode then concludes no,
# installs a launcher nobody can run, and leaves the repair pass agreeing there
# is nothing to repair. The requirement belongs to the archive contract.
_MUST_BE_EXECUTABLE = frozenset({'fha'})

# Files whose LINE ENDINGS are part of the archive contract, for the same reason
# the executable bit is: the requirement belongs to the file, not to whichever
# copy of the repo it arrives from. A GitHub ZIP holds the repository blobs, so
# `.cmd` launchers extract with LF whatever `.gitattributes` says - and shutil
# preserves those bytes straight into the archive. `goto` labels and the
# parenthesised block in serve.cmd are exactly what misbehaves in an LF-only
# batch file, so the copy is normalized rather than trusted.
_MUST_BE_CRLF = frozenset({'fha.cmd', 'serve.cmd'})


def _clear_stale_temp(tmp: Path) -> None:
    """Remove a leftover temp path, refusing to follow it if it is a link.

    `lstat` semantics throughout: a symlink is unlinked (which removes the LINK,
    never its target), and anything else left over from an interrupted run is
    removed too so the copy starts from nothing. A directory in the way is left
    alone - the copy will fail loudly, which is the right outcome for a state
    nothing here created.
    """
    if tmp.is_symlink():
        tmp.unlink()
        return
    if tmp.exists() and not tmp.is_dir():
        tmp.unlink()


def _normalize_crlf(dest: Path) -> None:
    """Rewrite `dest` with CRLF endings, in place. Raises OSError on failure.

    Byte-level and idempotent: split on universal newlines, rejoin with CRLF, so
    a file that is already CRLF is rewritten identically and a mixed one is made
    consistent.

    Deliberately NOT guarded. A first version swallowed read/write errors on the
    grounds that the copy had already succeeded - which is true and beside the
    point: what it leaves behind is an LF batch file that Windows may not run,
    checksummed and reported as a good install. The callers already know how to
    report a file they could not write, so the error goes to them. (Fourth time
    in this review that "nothing is lost" hid a real consequence; the copy
    landing is not the same as the file working.)
    """
    raw = dest.read_bytes()
    fixed = b'\r\n'.join(raw.replace(b'\r\n', b'\n').split(b'\n'))
    if fixed != raw:
        dest.write_bytes(fixed)

_ROOT_LAUNCHERS = (
    'serve.cmd',
    'fha.cmd',
    'fha',
)

# Subtrees walked whole for the operating layer. `.claude/skills/` carries the
# agent's genealogy workflow procedures (process-source, review-claims, …) - the
# "how to operate" an archive, so it ships. `.claude/settings.json` is *not*
# walked: it is this spec-repo's own agent config, not an archive's.
# `browser-companion/` is the capture extension - a front-end tool exactly like
# the serve workbench, so it ships ready to use (owner decision 2026-07-26): the
# extension has no build step, so its source tree IS the loadable artifact and
# vendoring it gives every archive a chrome://extensions "Load unpacked" target
# with no workshop clone. Its dev furniture stays home (_VENDOR_EXCLUDE_*).
_OPERATING_SUBTREES = ('tools', 'docs', 'design', 'browser-companion',
                       '.claude/skills')

# The archive subfolder that holds the vendored machinery, so a real archive's
# root reads as the genealogy - not the tooling. The install remaps the movable
# operating subtrees UNDER this prefix; the workshop repo itself stays flat, and
# the manifest's src/path seam records the repo-flat `src` against the archive
# `.fha/…` `path`.
#
# Only the MACHINERY moves: `tools/` (the program), `design/` (its stylesheet
# and self-hosted fonts), and `browser-companion/` (the capture extension - the
# one folder a human DOES open by hand, exactly once, to point
# chrome://extensions "Load unpacked" at `.fha/browser-companion/`; its README
# rides along and says so).
#
# `docs/` deliberately stays at the archive ROOT, alongside the rulebooks it is
# part of. It is human-facing reading matter, not machinery, and it is one half
# of a two-way link graph: the root docs and rulebooks link to `docs/…`, and the
# docs link back to `../SPEC.md`, `../AGENTS.md`, `../GETTING_STARTED.md`.
# Remapping either side under `.fha/` breaks every one of those links in an
# installed archive - a
# `docs/…` link from a root rulebook would resolve to nothing, and `../SPEC.md`
# from a vendored doc would resolve inside `.fha/`, where no rulebook lives.
# Keeping docs at the root costs one visible folder and keeps the archive
# navigable in a plain file browser, with no install-time link rewriting anywhere
# in the install/update engine.
#
# The same rule is why GETTING_STARTED.md and CHEATSHEET.md live at the REPO
# root and not under docs/: repo path == install path, so their relative links
# mean one thing in both places. A doc whose two depths differ can only be
# link-correct in one of them, and nothing here rewrites links to fix it.
#
# Also deliberately at the archive root: the rulebooks themselves
# (SPEC/TOOLING/AGENTS/CLAUDE), the two owner entry docs, the launchers, and
# `.claude/skills` (Claude Code discovers skills at the root). `_VENDOR_DIR` is
# the shared `_lib.VENDOR_DIR` so scaffold, serve, and doctor cannot drift on
# the name.
_VENDOR_DIR = VENDOR_DIR
_VENDORED_SUBTREES = ('tools', 'design', 'browser-companion')

# Dev-only furniture inside a vendored subtree that never enters an archive:
# the extension's node test-suite, its capture-bundle fixtures, the npm test
# manifest, and the hand-test walkthrough. What ships is exactly what the
# browser loads unpacked - manifest.json, src/, icons/ - plus the README that
# says how to load it. (Repo-relative src paths / path prefixes.)
#
# browser-companion/README.md is on this list because it is the PROJECT readme:
# it is addressed to whoever works on the extension, and it links to
# ../TOOLING_INGESTION.md, ../tests/test_browser_companion.py, test-bundle/ and
# ANCESTRY-AUTOFETCH-TEST.md - every one of which the installer deliberately
# leaves behind. Shipping it gave an archive owner a guide full of dead links
# and a test command that cannot run. The owner's copy is README-ARCHIVE.md,
# remapped onto README.md by _VENDOR_RENAMES below.
# Tool-generated caches that appear inside an operating subtree after an
# ordinary dev session. Never package content, wherever they turn up.
_DEV_CACHE_DIRS = frozenset({'__pycache__', '.pytest_cache'})

_VENDOR_EXCLUDE_PREFIXES = ('browser-companion/tests/',
                            'browser-companion/test-bundle/')
_VENDOR_EXCLUDE_FILES = frozenset({
    'browser-companion/package.json',
    'browser-companion/ANCESTRY-AUTOFETCH-TEST.md',
    'browser-companion/README.md',
})

# Files that ship under a DIFFERENT name than they carry in the repo, because
# the archive wants a different document at that spot than the project does.
# (repo src -> archive path.) The one case is the capture extension's README:
# an owner opening `.fha/browser-companion/README.md` gets plain instructions
# for loading the add-on and filing what it captures, with no link that leads
# outside an installed archive. The project README stays home (excluded above).
_VENDOR_RENAMES: dict[str, str] = {
    'browser-companion/README-ARCHIVE.md':
        f'{VENDOR_DIR}/browser-companion/README.md',
}

# Individual docs that are PROJECT documentation rather than owner documentation,
# and so belong with the machinery rather than in the archive's readable `docs/`.
# `DESIGN.md` is the visual language for whoever builds the templates; `SITE_PLAN`
# is an explicit roadmap - work not yet done, which has no business presenting
# itself as guidance in someone's family archive. The rest of `docs/` is the
# manual an owner reaches for when something is wrong, which is exactly when a
# hidden folder helps least, so it stays at the root.
_VENDORED_DOCS = frozenset({
    'docs/DESIGN.md',
    'docs/SITE_PLAN.md',
})

# The template folder whose *contents* seed the skeleton. The folder itself is
# never copied into an archive - each file's archive path strips this prefix.
_SKELETON_SRC_DIR = 'archive-template'

# A file under archive-template/ that is repo furniture, not skeleton: it tells a
# human how to start an archive, which GETTING_STARTED.md already covers. Without
# this exclusion its archive path would be a bare `README.md` at the archive
# root - putting back the very file the operating layer deliberately stopped
# shipping, and pointing the owner at a page about the public project.
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

def _repo_relative(path: Path, repo_root: Path) -> str:
    """A folder's name as it reads in the clone - 'tools', not /Users/….

    Keeps the refusal message portable between the maintainer's machine and a
    bug report; a folder somehow outside the clone keeps its own spelling,
    because naming it wrongly is worse than naming it long.
    """
    try:
        return Path(path).relative_to(repo_root).as_posix()
    except ValueError:
        return str(path).replace('\\', '/')


def _walk_repo_files(base: Path, unreadable: list[Path]):
    """Every file under `base`, sorted, recording folders that would not list.

    The manifest is the package's packing list, and `update-tools` treats a
    file the list does not name as RETIRED UPSTREAM - it moves that file aside
    in every archive that updates. So a folder this walk cannot open does not
    merely make a short manifest: it eventually takes those tools out of
    working archives, from a `write-manifest` run that reported success.
    `rglob` cannot tell an unreadable folder from an empty one, so the walk
    goes through `walk_files` with a recorder and `generate_manifest` refuses
    on a non-empty list.
    """
    return sorted(
        p for p in walk_files(base, on_error=unreadable_dir_recorder(unreadable))
        if p.is_file()
    )


def _operating_files(repo_root: Path, unreadable: list[Path]) -> list[tuple[str, Path]]:
    """Yield (archive_path, source_path) for every operating-layer file.

    The operating layer is the generic, regenerable glue a genealogist needs to
    operate an archive: the root rulebooks + the two owner entry docs
    (GETTING_STARTED.md, CHEATSHEET.md), everything under tools/
    (minus Python bytecode caches), everything under docs/, the capture
    extension under browser-companion/ (minus its dev furniture - it is a
    front-end tool like the serve workbench, shipped ready to load unpacked),
    and the agent's workflow skills under .claude/skills/. docs/ is included
    whole rather than cherry-picked: BUILD.md M9.1 names five docs as the floor
    ("must ship into every archive"), but the whole folder is generic
    human-facing documentation with no family data, and a directory rule
    auto-covers future docs and keeps their cross-links intact in an installed
    archive.

    The machinery subtrees (tools/, design/, browser-companion/) install UNDER
    .fha/ (see _VENDOR_DIR) so the archive root reads as the genealogy, not the
    tooling; their archive path is `.fha/…` while the repo source stays flat,
    recorded by the manifest's src/path seam. The root rulebooks, docs/, the
    launchers, and .claude/skills keep source == archive path at the root -
    docs/ among them so its two-way link graph with the rulebooks survives an
    install (_VENDOR_DIR).
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
        for p in _walk_repo_files(base, unreadable):
            # Dev debris, not package content. `.pytest_cache/` is gitignored
            # but a walk still finds it, so on any machine that had run the
            # test suite `write-manifest` quietly added four cache files to
            # the packing list - a manifest that differs by who generated it,
            # and a pytest cache vendored into every archive.
            if _DEV_CACHE_DIRS.intersection(p.parts) or p.suffix in ('.pyc', '.pyo'):
                continue
            rel = p.relative_to(repo_root).as_posix()
            # Skip skeleton-override files - they live under an operating
            # subtree but are user-owned (see _SKELETON_OVERRIDES).
            if rel in _SKELETON_OVERRIDE_SRCS:
                continue
            # Skip dev-only furniture inside a vendored subtree (the
            # extension's tests/fixtures - see _VENDOR_EXCLUDE_*).
            if (rel in _VENDOR_EXCLUDE_FILES
                    or rel.startswith(_VENDOR_EXCLUDE_PREFIXES)):
                continue
            # Vendored subtrees (tools/, design/) install UNDER .fha/ so the
            # archive root stays uncluttered; owner-facing docs/ and
            # .claude/skills stay at the root (readable documentation /
            # agent-discovered skills). Individual project docs are vendored too.
            # A handful of files ship under a different name than they carry
            # here (_VENDOR_RENAMES) because the archive wants a different
            # document at that spot than the project does.
            archive_path = _VENDOR_RENAMES.get(rel) or (
                f'{_VENDOR_DIR}/{rel}'
                if moved or rel in _VENDORED_DOCS else rel)
            out.append((archive_path, p))

    return out


def _skeleton_files(repo_root: Path, unreadable: list[Path]) -> list[tuple[str, Path]]:
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
    for p in _walk_repo_files(base, unreadable):
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


def _external_root_skeleton_paths(
    fha_config: dict, archive_root: Path, entries: list[dict],
) -> set[str]:
    """Manifest paths to skip because their alias is a configured external root.

    A genealogist who keeps documents/photos on an external drive (SPEC
    §12.4 - a supported, expected setup) gets a `documents/` or `photos/`
    folder inside the archive that will never hold anything: `roots:` already
    points the real files elsewhere, so the internal placeholder is dead
    weight with no purpose from day one (#124). Matched by alias/first path
    segment rather than by filename (`.gitkeep` today), so any future
    skeleton seed placed under `documents/`, `photos/`, or `inbox/` is
    skipped the same way, for the same reason.

    Only `category: skeleton` entries are ever eligible - the operating layer
    (tools, docs) never lives under these alias paths, but the check is
    explicit rather than assumed.
    """
    external = external_asset_roots(fha_config, archive_root)
    if not external:
        return set()
    skip: set[str] = set()
    for entry in entries:
        if entry.get('category') != 'skeleton':
            continue
        if entry['path'].split('/', 1)[0] in external:
            skip.add(entry['path'])
    return skip


def _internal_root_renames(fha_config: dict, archive_root: Path) -> dict[str, str]:
    """Alias -> its current folder name, for a `roots:` value that RENAMES an
    internal alias but keeps it inside the archive (TOOLING §13c, #124).

    `roots: documents: archive-docs` does not go external - the internal-folder
    concept still applies, just under a different name - so this alias's
    skeleton entries (`documents/.gitkeep`, `inbox/_TEMPLATE.notes.md`, …)
    belong at `archive-docs/.gitkeep`, not at the literal `documents/` the
    alias happens to be named after. Every caller that reads or writes a
    skeleton entry under an asset-root alias must remap through this (install
    placement, `_plan_update`'s never-delivered check, and the stamp rewrite in
    `run_update_tools`) or the two disagree about where the seed lives and the
    literal, purposeless `documents/` folder this feature exists to avoid comes
    back through the gap.

    Only returns an alias whose folder ends up a plain relative path that
    stays contained under the archive root (`_contained_relative` - the same
    guard the manifest's own paths are checked against) and actually differs
    from the alias's own name; a redundant `documents: documents` needs no
    remap, and anything this cannot vouch for (a `..`-bearing relative value,
    or an absolute value that does not actually resolve inside the archive)
    falls back to the literal `documents/` placeholder rather than guess at a
    destination the rest of the manifest-safety machinery cannot verify.

    A `roots:` value can also be a VALID ABSOLUTE path that happens to
    resolve INSIDE the archive (`documents: /home/me/archive/archive-docs`,
    with `archive_root` = `/home/me/archive`) - `external_asset_roots`
    already classifies that correctly as internal (it resolves inside), but
    the folder it actually names is `archive-docs`, not the string you get by
    stripping the leading `/` (`home/me/archive/archive-docs` - a bogus
    relative path this function used to hand back before ever checking
    whether the value was absolute at all, #124 review round 3, finding 1).
    So an absolute value is detected and resolved for REAL - the same way
    `resolve_path`/`external_asset_roots` do - before any string surgery,
    never by stripping characters off it.
    """
    roots = get_roots(fha_config)
    if not isinstance(roots, dict):
        return {}
    external = external_asset_roots(fha_config, archive_root)
    archive_root_resolved = Path(archive_root).resolve()
    renames: dict[str, str] = {}
    for alias in ASSET_ROOT_ALIASES:
        if alias not in roots or alias in external:
            continue
        raw = str(roots[alias]).strip()
        if not raw:
            continue
        if os.path.isabs(raw):
            # Resolved the real way (matches resolve_path/external_asset_roots,
            # which is how this alias got classified "internal" in the first
            # place) rather than by stripping the leading slash off the text.
            try:
                value = Path(raw).resolve().relative_to(archive_root_resolved).as_posix()
            except (ValueError, OSError, RuntimeError):
                continue  # cannot establish where inside the archive this is
        else:
            value = raw.replace('\\', '/').strip('/')
        if not value or value == alias or not _contained_relative(value):
            continue
        renames[alias] = value
    return renames


def _remap_skeleton_path(archive_path: str, renames: dict[str, str]) -> str:
    """Rewrite a skeleton manifest path's alias (first) segment through `renames`.

    A no-op for any path whose first segment isn't a renamed alias - which is
    every operating-layer path and every skeleton seed that isn't under
    `documents/`, `photos/`, or `inbox/` (`fha.yaml`, `places/places.yaml`,
    `sources/.gitkeep`, …), since `renames` only ever has ASSET_ROOT_ALIASES keys.
    """
    if not renames:
        return archive_path
    alias, sep, rest = archive_path.partition('/')
    renamed = renames.get(alias)
    if renamed is None:
        return archive_path
    return f'{renamed}{sep}{rest}' if sep else renamed


def _placeholder_only_scaffold_litter(
    folder: Path, archive_root: Path, seed_shas: dict[str, str],
) -> bool | None:
    """Whether `folder` (an alias's placeholder) holds nothing but this install's
    OWN shipped skeleton bytes - recursively - so pruning it destroys nothing.

    Returns True (safe to remove), False (real or unrecognized content - never
    touch it), or None (could not tell - a folder or file this walk could not
    read - which the caller treats the same as False: leave it alone, say
    nothing rather than guess).

    A genealogist can lose real work here in two ways a name-only check cannot
    see (the finding that made this function recursive and content-checking
    rather than name-and-dotfile-only):
      (a) she edited a shipped seed IN PLACE - `inbox/_TEMPLATE.notes.md` keeps
          its name but is no longer the tools' own bytes. Matching by filename
          alone would still call it litter and destroy her notes with the rest
          of the folder.
      (b) a HIDDEN subfolder (name starts with `.`) holds real files. The old
          check only inspected the alias folder's own top-level entries, so
          anything nested inside a dotted subdirectory was invisible to it and
          got swept away by the same `rmtree` that removed genuine litter.

    So every FILE found anywhere under `folder` - at any depth, dotted or not -
    must be a recognized skeleton entry for this alias (`seed_shas`, keyed by
    archive-relative path) AND byte-identical to the sha256 the manifest itself
    recorded for it (the manifest's own per-entry checksum, the same field
    `install`'s conflict preflight already trusts) - a name match is no longer
    enough. Every DIRECTORY found is fine on its own; it only ever disqualifies
    the folder through the files (or unreadable entries) inside it, so a
    directory that turns out to be genuinely empty (recursively) never blocks
    the prune - it is exactly as disposable as an empty alias folder would be.
    """
    try:
        entries = sorted(folder.iterdir(), key=lambda p: p.name)
    except OSError:
        return None  # can't tell what's in there - leave it, say nothing
    for entry in entries:
        try:
            is_dir = entry.is_dir()
        except OSError:
            return None
        if is_dir:
            verdict = _placeholder_only_scaffold_litter(entry, archive_root, seed_shas)
            if verdict is not True:
                return verdict  # None (unreadable) or False (real content) - propagate
            continue
        rel = entry.relative_to(archive_root).as_posix()
        want_sha = seed_shas.get(rel)
        if want_sha is None:
            return False  # not one of this alias's own shipped files - real content
        try:
            got_sha = _sha256_file(entry)
        except OSError:
            return None
        if got_sha != want_sha:
            return False  # same name, different bytes - a human edited this seed
    return True


def _alias_seed_shas(
    alias: str, manifest: dict, recorded: dict[str, str],
) -> dict[str, str]:
    """Literal manifest path -> the checksum THIS ARCHIVE actually received for
    `alias`'s own skeleton entries, preferring the archive's own installed
    baseline over the current manifest (#124 review round 3, finding 3).

    A litter check that compares on-disk bytes must compare them against what
    was really shipped to THIS archive, not against what today's release
    would ship: if a skeleton seed's content changed upstream between this
    archive's install and the run doing the comparing, the two can legitimately
    differ even though nothing local ever touched the file. Getting the
    baseline wrong cuts both ways - an untouched OLD seed reads as "real
    content" (never pruned) when compared against a NEW release's checksum,
    while a human edit that happens to coincidentally match the NEW release's
    stock bytes reads as "still pristine" (safe to prune) even though it
    genuinely differs from what this archive was actually given.

    `recorded` is the archive's OWN `.plaintext-version` stamp (`_stamp_file_map`)
    - the ground truth for what it received. Only a path the stamp never
    recorded at all - a skeleton entry first delivered to this archive during
    THIS very run, with nothing yet to compare against - falls back to the
    current manifest's checksum, since there is no earlier baseline to prefer.
    """
    return {
        e['path']: recorded.get(e['path'], e.get('sha256'))
        for e in manifest['files']
        if e.get('category') == 'skeleton'
        and e['path'].split('/', 1)[0] == alias
    }


def _prune_external_root_placeholders(
    archive_root: Path, fha_config: dict, manifest: dict, recorded: dict[str, str],
    *, dry_run: bool = False,
) -> tuple[list[str], list[tuple[str, str]], dict[str, bytes]]:
    """Remove (or preview removing) an internal placeholder that just went external.

    An archive installed with the default internal `roots:` already has its
    `documents/`/`photos/`/`inbox/` folder on disk; if the human later
    re-points one of those at an external drive, the folder's purpose
    disappears but nothing before this function ever cleaned it up (#124).

    Only a folder holding NOTHING BUT this install's own shipped bytes,
    verified recursively and byte-for-byte (`_placeholder_only_scaffold_litter`,
    compared against `recorded` - this archive's own `.plaintext-version`
    baseline, via `_alias_seed_shas` - not blindly against today's manifest,
    #124 review round 3, finding 3), is a candidate - matching this project's
    own safety rule (TOOLING: never destroy something that might be a human's
    data without being sure). One real file, an edited seed, a subfolder with
    real content nested inside it (however deep, however hidden), or anything
    this walk could not read takes the whole folder out of consideration: it
    is left exactly as it is - not removed, not flagged as customized, not
    backed up. `fha update-tools` narrates that decision but never acts on it;
    the human decides what to do with a folder that surprised the tools.

    Returns `(removed, failed)`:
      - `removed` - the alias names actually removed (or, under `dry_run`,
        that WOULD be removed). The caller uses this both to narrate the
        action and to drop the matching skeleton entries from the rewritten
        stamp - unrecorded is what lets a later revert to an internal root
        recreate the placeholder (see the matching guard in `_plan_update`).
      - `failed` - `(alias, reason)` pairs for a folder that WAS eligible to
        remove but whose `shutil.rmtree` itself failed (locked file, denied
        permission) - never silently swallowed (#124 review): the caller
        surfaces these so `fha update-tools` reports a non-clean exit instead
        of claiming a cleanup that did not happen. Always empty under
        `dry_run`, which never calls `rmtree`.
      - `snapshots` - archive-relative path -> the exact bytes read from disk
        immediately before this run's `rmtree`, for every file under an alias
        actually removed (never populated under `dry_run`, which removes
        nothing). This is the ONLY chance to capture what THIS run actually
        took away: if a skeleton seed changed upstream since this archive's
        last update, those bytes can already differ from what today's repo
        would hand back. `_restore_pruned_placeholders` uses this snapshot to
        put back precisely what was here if the stamp write recording the
        removal then fails - not the current release's source, which is what
        the (still unwritten) OLD stamp's checksum for this path would no
        longer describe (#124 review round 4, finding 1).
    """
    removed: list[str] = []
    failed: list[tuple[str, str]] = []
    snapshots: dict[str, bytes] = {}
    for alias in sorted(external_asset_roots(fha_config, archive_root)):
        folder = archive_root / alias
        if not folder.is_dir():
            continue
        # This alias's own skeleton entries, keyed by the FULL archive-relative
        # path (not just the filename), with the checksum THIS ARCHIVE actually
        # received for each (`_alias_seed_shas`) - what
        # `_placeholder_only_scaffold_litter` verifies every file it finds
        # against, byte for byte.
        seed_shas = _alias_seed_shas(alias, manifest, recorded)
        if not _placeholder_only_scaffold_litter(folder, archive_root, seed_shas):
            continue  # real content, an edited seed, or unreadable - not ours to touch
        if not dry_run:
            # Snapshot every file this prune is about to remove, in memory,
            # while it is still readable - the exact bytes the stamp's
            # checksum for this path was recorded against, whatever today's
            # repo happens to ship now.
            for file_path in folder.rglob('*'):
                if not file_path.is_file():
                    continue
                try:
                    rel = file_path.relative_to(archive_root).as_posix()
                    snapshots[rel] = file_path.read_bytes()
                except OSError:
                    pass  # best-effort - a later restore just can't cover this one
            try:
                shutil.rmtree(folder)
            except OSError as exc:
                failed.append((alias, str(exc)))
                continue
        removed.append(alias)
    return removed, failed, snapshots


def _prune_orphaned_literal_root_folders(
    archive_root: Path, fha_config: dict, manifest: dict, recorded: dict[str, str],
    *, dry_run: bool = False,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Remove (or preview removing) the literal alias folder an INTERNAL rename
    just orphaned (TOOLING §13c, #124 review round 3, finding 2).

    `roots: documents: archive-docs` moves an internal alias's placeholder to
    `archive-docs/.gitkeep` (`_internal_root_renames`, install time or a later
    `_plan_update`) - but an archive that was already installed at the literal
    `documents/` folder before the rename (or that was installed by a version
    which always seeds the literal name first) still has `documents/.gitkeep`
    sitting there afterward, since nothing before this function ever cleaned
    it up. TOOLING §13c promises the placeholder lives ONLY at the renamed
    folder, not both places at once - a literal `documents/` folder with
    nothing in it but the shipped seed is exactly as purposeless, for exactly
    the same reason, as an internal placeholder whose alias went external
    (`_prune_external_root_placeholders`), so it gets the identical treatment.

    Only a literal folder holding nothing but this install's own shipped
    bytes, verified recursively and byte-for-byte against what THIS ARCHIVE
    actually received (`_placeholder_only_scaffold_litter` + `_alias_seed_shas`,
    same as the external-root prune), is ever removed - real content, an
    edited seed, or anything unreadable leaves the folder alone, unremoved and
    unflagged, matching every other prune in this file.

    An alias that is CURRENTLY external is left to `_prune_external_root_placeholders`
    instead (it prunes the literal `alias` folder too, by a different route),
    so this function only ever considers a rename that stays inside the
    archive. Returns `(removed, failed)` in the same shape as
    `_prune_external_root_placeholders`.
    """
    renames = _internal_root_renames(fha_config, archive_root)
    removed: list[str] = []
    failed: list[tuple[str, str]] = []
    for alias in sorted(renames):
        folder = archive_root / alias
        if not folder.is_dir():
            continue
        seed_shas = _alias_seed_shas(alias, manifest, recorded)
        if not _placeholder_only_scaffold_litter(folder, archive_root, seed_shas):
            continue  # real content, an edited seed, or unreadable - not ours to touch
        if not dry_run:
            try:
                shutil.rmtree(folder)
            except OSError as exc:
                failed.append((alias, str(exc)))
                continue
        removed.append(alias)
    return removed, failed


def _restore_pruned_placeholders(
    archive_root: Path, pruned_skeleton_paths: set[str], snapshots: dict[str, bytes],
) -> list[str]:
    """Best-effort undo of a prune whose transition the stamp failed to record (#124).

    `_prune_external_root_placeholders` removes a placeholder folder and the
    caller then drops its skeleton entries from the NEW stamp before writing
    it - but if that write itself fails, the OLD stamp (still on disk,
    unchanged) still lists those entries as delivered, while the folder is now
    actually gone. Left alone, that split survives: a retry finds no folder
    left to prune, carries the old stamp entries forward untouched, and if the
    alias is later pointed back inside the archive, `_plan_update` reads the
    stale stamp as "already delivered" and never recreates the placeholder -
    even though it is not really there. Restoring the exact bytes this run
    just removed keeps disk and stamp in agreement again: both describe the
    pre-prune state, so the ordinary revert-to-internal path in `_plan_update`
    keeps working the next time it is actually asked to.

    `snapshots` (from `_prune_external_root_placeholders`) is the bytes this
    run actually read off disk right before removing them - NOT today's repo
    copy. If a skeleton seed changed upstream between this archive's install
    and the run doing the pruning, the two can legitimately differ even though
    nothing local ever touched the file; restoring from the repo instead of
    the snapshot would silently re-seed the placeholder with content the OLD
    (still-recorded) stamp checksum does not describe, and the next run would
    then read the restored file as human-edited and leave the now-purposeless
    folder in place forever (#124 review round 4, finding 1). A path with no
    captured snapshot (a read failure during the prune itself, vanishingly
    rare) is reported as still-missing rather than guessed at.

    Returns the paths it could NOT restore (empty on full success) so the
    caller can fold them into the run's failure report - a restore that itself
    fails is a real divergence between disk and the stamp, not something to
    paper over.
    """
    still_missing: list[str] = []
    for path in sorted(pruned_skeleton_paths):
        data = snapshots.get(path)
        if data is None:
            still_missing.append(path)
            continue
        dest = archive_root / path
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        except OSError:
            still_missing.append(path)
    return still_missing


def generate_manifest(repo_root: Path, spec_version: str | None = None) -> dict:
    """Build the manifest dict from a public-repo clone.

    Walks the operating-layer and skeleton file sets, checksums each file, and
    returns the JSON-serializable manifest. Entries are sorted by archive path so
    the committed manifest.json has a stable, diff-friendly order. `spec_version`
    defaults to the value parsed from SPEC.md's "**Version X.Y …**" line.

    Refuses (ScaffoldError) when any folder in the clone would not list. A
    packing list is a claim that this is everything; a walk that skipped a
    subtree cannot make it, and the consequence is not a smaller download -
    `update-tools` reads an absent entry as "retired upstream" and moves the
    installed copy aside in every archive. Better a maintainer re-runs one
    command.
    """
    repo_root = Path(repo_root).resolve()
    if spec_version is None:
        spec_version = _read_spec_version(repo_root)

    unreadable: list[Path] = []
    file_sets = (
        ('operating', _operating_files(repo_root, unreadable)),
        ('skeleton', _skeleton_files(repo_root, unreadable)),
    )
    if unreadable:
        shown = ', '.join(sorted(
            _repo_relative(p, repo_root) for p in unreadable)[:5])
        raise ScaffoldError(
            f'The packing list was NOT written: {len(unreadable)} folder(s) in '
            f'{repo_root} could not be opened, so the files in them would have '
            f'been left off it: {shown}. An archive updating from a list that '
            f'is missing them would set those files aside as retired. Fix the '
            f'folder (permissions, or a drive that is not connected), then run '
            f'`python tools/scaffold.py write-manifest --repo .` again.'
        )

    entries: list[dict] = []
    for category, pairs in file_sets:
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


def _contained_relative(value: str) -> bool:
    """True when `value` is a plain relative path that cannot escape its root.

    Manifest paths are joined onto the archive root (`path`) and the workshop
    root (`src`) and then written to or read from. A manifest is a DOWNLOADED
    file - truncated, hand-edited, or hostile - so a value like `../../.ssh` or
    `/etc/cron.d/x` would let install and update reach outside the two folders
    the human pointed them at, which is the one thing a packing list must never
    be able to do.

    Checked as text rather than by resolving: resolution depends on what exists
    on this machine, and the answer must not. Rejected are absolute paths,
    anything with a `..` segment, Windows drive letters and UNC prefixes (a
    POSIX `Path` treats `C:\\x` as a harmless relative name, so the same
    manifest would be contained on one OS and not on another), and backslash
    separators, which manifest paths never use.
    """
    if not value or value.startswith(('/', '\\')) or ':' in value:
        return False
    if '\\' in value:
        return False
    parts = value.split('/')
    # No `.`, no `..`, no empty segment ANYWHERE - including a trailing one.
    # `same` and `same/` are lexically different and land on the same file, so
    # allowing the alias lets two entries claim one destination without the
    # duplicate check seeing it. Refusing the alias is simpler than teaching
    # every later comparison to normalize.
    return not ({'..', '.', ''} & set(parts))


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
    # Validate every ENTRY too, not just the list around them. A hand-edited or
    # truncated manifest can hold a null, a bare string, or an entry with no
    # `path`, and each consumer reaches for `entry['path']` / `entry.get(...)`.
    # Catching it here - at the single door every command reads the manifest
    # through - turns what would be an AttributeError or KeyError raised
    # mid-operation, potentially after files have already moved, into one plain
    # refusal before anything is touched.
    seen_paths: dict[str, int] = {}
    for position, entry in enumerate(manifest['files']):
        if not isinstance(entry, dict) or not isinstance(entry.get('path'), str) \
                or not entry['path']:
            raise ScaffoldError(
                f"{path} has a damaged entry at position {position} (every entry "
                f"must be an object with a 'path'). Re-download the plaintext "
                f"tools and try again - this file is the packing list the install "
                f"and update commands read, and a partial one cannot be trusted."
            )
        # `path` alone is not the whole contract. The optional fields are read
        # just as unconditionally once a command gets going: `repo_root /
        # entry['src']` raises TypeError on a null, and `category` / `sha256`
        # reach comparisons that quietly misbehave rather than refuse. Each one
        # that is present must be a string, checked at the same single door -
        # otherwise the failure surfaces as a traceback mid-install instead of a
        # plain refusal before anything is written.
        # Presence, not value: `entry.get('src', fallback)` returns None when the
        # key is THERE holding a null - the fallback only covers a missing key -
        # so an explicit null is precisely the case that reaches `repo_root /
        # None`. Treating it as "absent" here would let the reported crash
        # straight through.
        # One archive file, one entry. Two entries naming the same destination
        # install both (last writer wins) while the stamp records a single
        # checksum, and every later update classifies them independently - so the
        # file alternates between the two sources on each run, for as long as the
        # archive lives.
        if entry['path'] in seen_paths:
            raise ScaffoldError(
                f"{path} lists {entry['path']!r} twice (positions "
                f"{seen_paths[entry['path']]} and {position}). Each archive file "
                f"must have exactly one source and one lifecycle, or updates "
                f"would flip it between them. Re-download the plaintext tools."
            )
        seen_paths[entry['path']] = position

        # Containment, before any command joins these onto a root. `path` is
        # written to inside the archive and `src` is read from inside the
        # workshop; neither may point outside the folder the human named.
        for field in ('path', 'src'):
            value = entry.get(field)
            if isinstance(value, str) and value and not _contained_relative(value):
                raise ScaffoldError(
                    f"{path} has an unsafe entry at position {position}: "
                    f"'{field}' is {value!r}, which points outside the folder it "
                    f"belongs to. Install and update only ever write inside your "
                    f"archive and read inside your copy of the tools, so this "
                    f"packing list is refused. Re-download the plaintext tools."
                )
        # `category` decides who manages the file for the rest of its life:
        # `_plan_update` handles `operating` and `skeleton` and silently ignores
        # anything else. A missing or misspelled value therefore installs
        # normally and then becomes permanently unmanaged - never updated, never
        # retired, never reported - which is worse than a refusal because nothing
        # ever surfaces it.
        if entry.get('category') not in ('operating', 'skeleton'):
            raise ScaffoldError(
                f"{path} has an entry at position {position} whose 'category' is "
                f"{entry.get('category')!r}; it must be 'operating' or "
                f"'skeleton'. Anything else would be copied in and then never "
                f"managed again by updates. Re-download the plaintext tools."
            )
        for field in ('src', 'category', 'sha256'):
            if field not in entry:
                continue
            value = entry[field]
            if not isinstance(value, str) or not value:
                raise ScaffoldError(
                    f"{path} has a damaged entry at position {position}: "
                    f"'{field}' must be text, but it is {value!r}. Re-download "
                    f"the plaintext tools and try again - this file is the "
                    f"packing list the install and update commands read, and a "
                    f"partial one cannot be trusted."
                )
    return manifest


def _refuse_directory_destinations(archive_root: Path, files: list[dict]) -> None:
    """Refuse before any mutation if a manifest path names an existing directory.

    Containment (see `_contained_relative`) keeps manifest paths inside the
    archive, but inside is not the same as harmless: `people` is a contained,
    perfectly ordinary-looking path, and it is where the human's records live.
    An entry naming it is handled as a FILE by everything downstream - update
    classifies the directory as customized, moves the whole tree into
    `.plaintext-backup/`, writes a file in its place, and exits 0; install copies
    into it. Either way the archive's own records are the collateral.

    This is the check that matters most in this file, because it is the only one
    standing between a damaged packing list and the genealogy itself - and the
    records are the thing the tools exist to protect, not the tools.
    """
    # A symlink is the other way a contained path reaches outside. `is_dir()`
    # follows links, so a `notes/.gitkeep` symlinked to something external looks
    # like an ordinary missing file here, and `shutil.copy2` then writes THROUGH
    # it - replacing a file outside the archive and exiting 0. Ancestors count
    # too: a symlinked `notes/` puts every path beneath it outside.
    #
    # Checked with lstat semantics (`is_symlink`), never by resolving, and only
    # over ancestors that already exist - the ones install would descend into.
    links: list[str] = []
    for entry in files:
        rel = PurePosixPath(entry['path'])
        for depth in range(1, len(rel.parts) + 1):
            here = archive_root / Path(*rel.parts[:depth])
            if here.is_symlink():
                links.append(f"{entry['path']} (via {'/'.join(rel.parts[:depth])})"
                             if depth < len(rel.parts) else entry['path'])
                break
            if not here.exists():
                break
    if links:
        listing = '\n  '.join(sorted(set(links))[:10])
        more = '' if len(set(links)) <= 10 else f'\n  …and {len(set(links)) - 10} more'
        raise ScaffoldError(
            f"{archive_root} has symbolic link(s) where tool files belong:\n  "
            f"{listing}{more}\n"
            f"Writing through a link would change whatever it points at, which "
            f"may be outside your archive entirely. Nothing has been changed. "
            f"Replace the link(s) with real files or remove them, then re-run."
        )

    clashes = [
        entry['path'] for entry in files
        if (archive_root / entry['path']).is_dir()
    ]
    if clashes:
        listing = '\n  '.join(sorted(clashes)[:10])
        more = '' if len(clashes) <= 10 else f'\n  …and {len(clashes) - 10} more'
        raise ScaffoldError(
            f"this copy of the plaintext tools has a packing list naming "
            f"{len(clashes)} path(s) that are FOLDERS in {archive_root}:\n  "
            f"{listing}{more}\n"
            f"Those are almost certainly your own folders - records, photos, "
            f"notes - and treating them as tool files would move them aside. "
            f"Nothing has been changed. Re-download the plaintext tools; if the "
            f"problem persists, this manifest is damaged and should not be used."
        )


def _refuse_remapped_root_conflicts(
    archive_root: Path, files: list[dict], renames: dict[str, str],
) -> None:
    """Refuse before any mutation if a REMAPPED destination cannot actually be
    written - either because it collides with another manifest destination
    (#124 review round 3, finding 6), or because an ancestor segment already
    exists ON DISK as something other than a folder (#124 review round 4,
    finding 2).

    A conflicting internal remap like `documents: fha.yaml/assets` is
    syntactically valid and stays inside the archive - `_contained_relative`
    passes it - and is invisible to `_refuse_directory_destinations`, which
    only ever checks the manifest's UNREMAPPED literal paths against what
    already exists on disk before this run touches anything. But it makes the
    documents seed's remapped destination (`fha.yaml/assets/.gitkeep`) require
    `fha.yaml` to be a DIRECTORY - the exact path another manifest entry (the
    real config file itself) needs to write as a plain FILE. Nothing needs to
    exist on disk yet for this to be a genuine conflict: it is a property of
    the computed destination SET alone, so it is checked before the install
    loop copies a single byte, rather than discovered mid-loop with a
    half-installed archive and no stamp - the documents seed creating
    `fha.yaml/` as a directory, a later copy step failing to write the real
    config through it, and a retry then failing the same way because
    `fha.yaml` is now a directory, not a writable file.

    A second, distinct way a remap can be unwritable: `documents:
    occupied/assets` where `occupied` is not another manifest destination at
    all, but something already SITTING in the archive - a plain file left
    over from anything else entirely. Nothing above catches this, because both
    that check and `_refuse_directory_destinations` only ever compare manifest
    paths against each other or against their OWN literal (unremapped)
    destination - never a remapped destination's ANCESTORS against the real
    filesystem. Left unchecked, install proceeds to copy several files before
    finally reaching the remapped seed and failing its `mkdir` against the
    existing file, by which point the archive is a partial install with no
    stamp. Checked here too, before anything is written.
    """
    dest_paths = [
        _remap_skeleton_path(e['path'], renames) if e.get('category') == 'skeleton'
        else e['path']
        for e in files
    ]
    dest_set = set(dest_paths)
    conflicts: set[str] = set()
    for dest in dest_paths:
        parts = PurePosixPath(dest).parts
        for depth in range(1, len(parts)):
            ancestor = '/'.join(parts[:depth])
            if ancestor in dest_set:
                conflicts.add(ancestor)
                conflicts.add(dest)
    if conflicts:
        listing = '\n  '.join(sorted(conflicts)[:10])
        more = '' if len(conflicts) <= 10 else f'\n  …and {len(conflicts) - 10} more'
        raise ScaffoldError(
            f"fha.yaml's roots: maps two different tool files to the same path once "
            f"the rename is applied - one needs it to be a FOLDER, another needs it "
            f"to be that exact FILE:\n  {listing}{more}\n"
            f"Nothing has been written. Point roots: at a folder that does not "
            f"collide with another tool file, then re-run install."
        )

    blocked: set[str] = set()
    for dest in dest_set:
        parts = PurePosixPath(dest).parts
        for depth in range(1, len(parts)):
            ancestor = archive_root / Path(*parts[:depth])
            if ancestor.exists() and not ancestor.is_dir():
                blocked.add('/'.join(parts[:depth]))
    if blocked:
        listing = '\n  '.join(sorted(blocked)[:10])
        more = '' if len(blocked) <= 10 else f'\n  …and {len(blocked) - 10} more'
        raise ScaffoldError(
            f"fha.yaml's roots: renames a tool file to a path that runs through "
            f"{len(blocked)} existing file(s) in {archive_root}, where a FOLDER "
            f"needs to be created instead:\n  {listing}{more}\n"
            f"Nothing has been written. Point roots: at a folder that does not "
            f"collide with something already there, then re-run install."
        )


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


def _seed_roots_stamp(archive_root: Path) -> None:
    """Remember the archive's current `roots:` so W121 has a baseline (#36).

    The roots-change check compares fha.yaml against the mapping the tools last
    ran with; with no baseline there is nothing to compare, and the first
    `fha index` would silently accept whatever it finds - including a value
    already narrowed into the trap. Install and update-tools are the two
    moments an archive first meets this build, so they seed the stamp from the
    fha.yaml as it stands. Best-effort: a malformed fha.yaml is doctor's job.
    """
    try:
        roots_change_orphans(archive_root, load_fha_yaml(archive_root))
    except Exception:
        pass


def _write_version_stamp(archive_root: Path, stamp: dict) -> None:
    """Write .plaintext-version (pretty JSON, trailing newline), atomically.

    Written to a sibling temp file and then replaced, so a full disk or an
    interrupted write can never leave a TRUNCATED stamp behind. That matters more
    than it looks: a half-written stamp is invalid JSON, and `_load_version_stamp`
    refuses an unreadable stamp outright - so the next `update-tools` stops with a
    "delete it and re-run" message instead of the clean automatic re-stamp the
    migration contract promises. Either the old stamp survives intact or the new
    one lands whole; there is no in-between state.
    """
    path = archive_root / VERSION_FILE
    tmp = path.with_name(path.name + '.fha-tmp')
    _clear_stale_temp(tmp)          # same deterministic-temp hazard as _copy_in
    try:
        tmp.write_text(json.dumps(stamp, indent=2) + '\n', encoding='utf-8')
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _hide_vendor_dir(archive_root: Path) -> None:
    """Give `.fha/` the Windows hidden attribute (no-op elsewhere).

    A leading dot hides a folder on macOS and Linux by convention, but means
    nothing to Windows: File Explorer shows a dot-folder like any other. Since
    the whole point of vendoring is that the archive root reads as the genealogy
    rather than the machinery, Windows needs the actual FILE_ATTRIBUTE_HIDDEN bit
    set - otherwise the folder every guide calls "hidden" sits in plain view for
    the users least likely to shrug it off.

    Best-effort and never fatal: a filesystem that cannot carry the attribute
    (a FAT/exFAT stick, a network share, a WSL mount) leaves the folder visible,
    which is cosmetic. Called wherever `.fha/` comes into being.
    """
    if os.name != 'nt':
        return
    vendor = archive_root / VENDOR_DIR
    if not vendor.is_dir():
        return
    try:
        import ctypes
        _FILE_ATTRIBUTE_HIDDEN = 0x02
        ctypes.windll.kernel32.SetFileAttributesW(  # type: ignore[attr-defined]
            str(vendor), _FILE_ATTRIBUTE_HIDDEN)
    except Exception:
        pass


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
      - PyYAML missing → an advisory message; install proceeds (`install`/
        `update-tools` are deliberately usable before it is present - fha.py
        intercepts them ahead of the bulk import that needs it - but that also
        means neither command can yet tell whether `fha.yaml`'s `roots:` points
        documents/photos/inbox outside the archive (#124): say so plainly
        rather than silently treat "can't check" as "nothing configured").
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
    if not yaml_available():
        messages.append(
            "PyYAML is not installed, so this run could not check fha.yaml's "
            "roots: for a documents/photos/inbox folder pointed outside the "
            f"archive - install it with `{pip_command('pyyaml')}`, then run "
            "`fha update-tools` again to have it checked."
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

    Skips the internal `documents/`/`photos/`/`inbox/` skeleton placeholder for
    any alias the fha.yaml about to be installed already points outside the
    archive (`_external_root_skeleton_paths`, #124) - an archive kept on an
    external drive never gets a purposeless empty stub for it. An alias that
    RENAMES but stays inside the archive (`documents: archive-docs`) still gets
    its placeholder, just at the renamed folder (`_internal_root_renames`,
    TOOLING §13c) rather than the literal `documents/` it would otherwise land
    at unused - unless that rename would put the placeholder somewhere it
    structurally CONFLICTS with another tool file's own destination (one
    needing a folder, the other that same path as a file), or runs through a
    path segment that already exists on disk as a plain file; either is
    refused before anything is written, not discovered mid-install with a
    half-installed archive (`_refuse_remapped_root_conflicts`, #124 review
    round 3, finding 6; round 4, finding 2).

    Returns a `Result` (Result == int, so callers/tests comparing against EXIT_*
    keep working): EXIT_CLEAN on success (even with the exiftool/PyYAML
    advisories), EXIT_FAILURE on a preflight failure, with the copied files and
    version stamp listed in `changed` (empty under --dry-run).  The install
    narration is printed inline.  Raises ScaffoldError for the caller to print.
    """
    archive_path = Path(archive_path).resolve()
    manifest = load_manifest(repo_root)

    python_ok, advisories = _preflight()
    if not python_ok:
        for m in advisories:
            print(f'ERROR: {m}', file=sys.stderr)
        return Result(ok=False, exit_code=EXIT_FAILURE)

    _refuse_directory_destinations(archive_path, manifest['files'])

    already = archive_path / VERSION_FILE
    if already.is_file():
        raise ScaffoldError(
            f"{archive_path} already has the plaintext tools installed. To refresh "
            f"them with improvements from the public repo, run from inside that "
            f"archive:\n  fha update-tools --repo \"{repo_root}\""
        )
    # An unstamped archive that nonetheless has a flat `tools/fha.py` is a
    # pre-`.fha` toolset. Installing over it would put stock files under
    # `.fha/tools/`, exit 0, and leave the owner's flat copies as inert
    # duplicates the launcher never reaches - their customizations silently
    # switched off by a command that reported success. There is deliberately no
    # migration path, so this is the owner's call to make, not ours to guess.
    legacy_entry = archive_path / 'tools' / 'fha.py'
    if legacy_entry.is_file() and not (archive_path / VENDOR_DIR / 'tools').is_dir():
        raise ScaffoldError(
            f"{archive_path} already holds a copy of the tools at "
            f"{Path('tools') / 'fha.py'}, from a layout this version no longer "
            f"uses (the tools now live in {VENDOR_DIR}/). Installing on top would "
            f"leave those files in place but unused - including any edits you "
            f"made to them.\n"
            f"Move or delete the old tools/ folder yourself, then re-run install. "
            f"If you customized anything in it (a stylesheet in design/, say), "
            f"copy that across afterwards - nothing here will do it for you."
        )
    # A half-written `.fha/tools/` without a stamp means a previous install was
    # interrupted before it could write one.  Allow re-running install to finish.

    # Skip the internal documents/photos/inbox placeholder for any alias
    # `roots:` already points outside the archive (#124) - it would never
    # hold anything. Read from the SOURCE fha.yaml about to be installed
    # (archive-template/fha.yaml), not the destination: a fresh archive has
    # nothing at archive_path/fha.yaml yet, and the ONLY fha.yaml install
    # ever WRITES there is this same source's bytes - the conflict-preflight
    # below refuses outright rather than installing over a destination copy
    # that differs from it (see `_acceptable`). So whatever is on disk right
    # now, the config this install will actually end up running with is
    # always this one.
    #
    # A SYNTAX ERROR in that template is not "nothing configured" - the
    # permissive `load_fha_yaml` used elsewhere in this file cannot tell the
    # two apart, returning `{}` for both, and install would then copy the
    # broken bytes into the new archive anyway (the README's own guidance is
    # to edit this very template), laying out default placeholders while
    # leaving every subsequent archive command unable to read the config it
    # just received. `run_update_tools` already reads ITS OWN fha.yaml
    # strictly for exactly this reason (#124 review round 3, finding 5) -
    # fresh install needs the same discipline for the TEMPLATE it is about to
    # copy (#124 review round 4, finding 3), and needs it before anything is
    # written, not discovered afterward with a half-installed archive. Skipped
    # only when PyYAML itself is missing, which `_preflight` already reported
    # above as its own advisory; a strict load with no PyYAML would raise for
    # that same underlying reason and muddy the two messages together.
    if yaml_available():
        try:
            install_fha_config = load_fha_yaml(repo_root / _SKELETON_SRC_DIR, strict=True)
        except FhaConfigError as exc:
            raise ScaffoldError(
                f"{repo_root / _SKELETON_SRC_DIR / 'fha.yaml'} has a problem and "
                f"could not be read: {exc} This file ships as the starting "
                f"fha.yaml for every new archive, so install cannot safely "
                f"continue until it is fixed. Nothing has been written."
            ) from exc
    else:
        install_fha_config = {}
    skip_paths = _external_root_skeleton_paths(install_fha_config, archive_path, manifest['files'])
    # A `roots:` value that RENAMES an alias but keeps it inside the archive
    # (`documents: archive-docs`) is not external - TOOLING §13c promises its
    # placeholder still gets created, just at the renamed folder rather than
    # the literal `documents/` the alias is named after. `_plan_update` and
    # `run_update_tools`'s stamp rewrite apply the same map later, so every
    # run agrees on where this alias's skeleton entries actually live.
    renames = _internal_root_renames(install_fha_config, archive_path)

    # Validate every source exists BEFORE writing anything, so a broken/partial
    # clone fails cleanly instead of leaving a half-installed archive.
    files = [e for e in manifest['files'] if e['path'] not in skip_paths]

    # A structurally CONFLICTING remap (`documents: fha.yaml/assets`, which
    # would need `fha.yaml` to be both a folder and the real config file), or
    # one whose ancestor is already an existing file on disk, must be caught
    # before anything is written, not discovered mid-loop with a
    # half-installed archive and no stamp (#124 review round 3, finding 6;
    # round 4, finding 2).
    _refuse_remapped_root_conflicts(archive_path, files, renames)

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
    #
    # Checked for EVERY category, not just skeleton: the documented "copy this
    # template, then point install at it" path puts template bytes at operating
    # destinations as well as skeleton ones, and install used to copy over them
    # with no backup and exit 0 while the template's own README promised that
    # starting to edit makes installation stop.
    #
    # Three byte patterns are acceptable at a destination: what the manifest
    # predicts, what THIS COPY of the source actually holds, and what the
    # TEMPLATE ships there (a pristine hand-copy - the documented path, which
    # must keep working even when the template's copy of a file differs from the
    # installed one, since comparing against stock alone would refuse exactly the
    # copy the guide tells people to make).
    #
    # The middle one is what makes an interrupted install resumable. The manifest
    # is generated in a git checkout, where .gitattributes materializes the .cmd
    # launchers as CRLF, while a download packaged from the repository blobs can
    # carry LF - so an install that copied such a file and died before writing
    # the stamp would, on the next run, refuse the very bytes it had just put
    # there. Same root as recording the installed hash in the stamp: what is on
    # disk is decided by the source in hand, not by the manifest's prediction.
    def _acceptable(entry: dict) -> set[str]:
        ok = {entry.get('sha256')}
        src_copy = repo_root / entry.get('src', entry['path'])
        if src_copy.is_file():
            try:
                ok.add(_sha256_file(src_copy))
            except OSError:
                pass          # unreadable source is caught by the preflight below
        template_copy = repo_root / _SKELETON_SRC_DIR / entry["path"]
        if template_copy.is_file():
            try:
                ok.add(_sha256_file(template_copy))
            except OSError:
                pass
        return ok

    conflicts: list[str] = []
    unreadable: list[str] = []
    for entry in files:
        dest_path = (_remap_skeleton_path(entry['path'], renames)
                     if entry.get('category') == 'skeleton' else entry['path'])
        target = archive_path / dest_path
        if Path(dest_path).name == '.gitkeep' or not target.is_file():
            continue
        try:
            here = _sha256_file(target)
        except OSError as exc:
            # A file we cannot read is a file we cannot prove is safe to
            # overwrite, so it belongs with the other preflight refusals - not
            # as an OSError escaping into `_cmd_install`, which catches only
            # ScaffoldError and would hand the owner a traceback.
            unreadable.append(f"{dest_path} ({exc})")
            continue
        if here not in _acceptable(entry):
            conflicts.append(dest_path)
    if unreadable:
        listing = '\n  '.join(unreadable[:10])
        more = '' if len(unreadable) <= 10 else f'\n  …and {len(unreadable) - 10} more'
        raise ScaffoldError(
            f"{archive_path} contains file(s) install cannot read, so it cannot "
            f"tell whether overwriting them would destroy your work:\n  "
            f"{listing}{more}\n"
            "Check their permissions (or move them aside), then re-run install."
        )
    if conflicts:
        listing = '\n  '.join(conflicts[:10])
        more = '' if len(conflicts) <= 10 else f'\n  …and {len(conflicts) - 10} more'
        raise ScaffoldError(
            f"{archive_path} already contains files that install would overwrite:\n  "
            f"{listing}{more}\n"
            "Move or rename them first, then re-run install."
        )

    skipped_aliases = sorted({p.split('/', 1)[0] for p in skip_paths})

    if dry_run:
        print(f'Dry run - would install into: {archive_path}')
        print(f'  {len(files)} file(s) from {repo_root / "manifest.json"}')
        skel = sum(1 for e in files if e.get('category') == 'skeleton')
        print(f'  ({skel} skeleton file(s), {len(files) - skel} operating-layer file(s))')
        print(f'  and write {archive_path / VERSION_FILE}')
        if skipped_aliases:
            print(f'  Would skip the internal {", ".join(skipped_aliases)}/ folder(s) - '
                  f'fha.yaml already points them outside this archive.')
        for alias, renamed in sorted(renames.items()):
            print(f'  Would place the {alias} placeholder at {renamed}/ - '
                  f'fha.yaml roots: already renames it there.')
        for m in advisories:
            print(f'\nNote: {m}')
        print('\nNothing was written (dry run). Re-run without --dry-run to install.')
        return Result(exit_code=EXIT_CLEAN, data={'dry_run': True, 'file_count': len(files)})

    checksums: dict[str, str] = {}
    changed: list[str] = []
    try:
        archive_path.mkdir(parents=True, exist_ok=True)
        for entry in files:
            dest_path = (_remap_skeleton_path(entry['path'], renames)
                         if entry.get('category') == 'skeleton' else entry['path'])
            src = repo_root / entry.get('src', entry['path'])
            dest = archive_path / dest_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            _clear_stale_temp(dest)     # never write through a leftover link
            shutil.copy2(src, dest)
            if entry['path'] in _MUST_BE_CRLF:
                _normalize_crlf(dest)
            if os.name != 'nt' and entry['path'] in _MUST_BE_EXECUTABLE:
                # copy2 carries the source mode, which a zip-sourced workshop
                # does not have. Set it from the contract instead, so a fresh
                # install always yields a launcher that runs.
                dest.chmod(dest.stat().st_mode | 0o111)
            # Hash the DESTINATION, not the manifest's prediction and not the
            # source. The stamp is the baseline every later update compares the
            # working copy against, so it has to describe the bytes that are
            # really on disk. Those can differ from the manifest legitimately:
            # manifest.json is generated in a git checkout, where .gitattributes
            # materializes `.cmd` launchers as CRLF, while a download packaged
            # from the repository blobs can carry LF. Recording the predicted
            # hash there would make the very next release that touches a launcher
            # call the owner's untouched file "customized" and back it up - the
            # exact churn pinning the line endings was meant to end, arriving
            # through a different door. Recorded under `dest_path` (the renamed
            # folder, for a skeleton entry whose alias `roots:` renames) so
            # later updates look for it where it actually landed.
            checksums[dest_path] = _sha256_file(dest)
            changed.append(str(dest))
        _write_version_stamp(archive_path, _stamp_dict(manifest, checksums))
        changed.append(str(archive_path / VERSION_FILE))
        _seed_roots_stamp(archive_path)
        _hide_vendor_dir(archive_path)
    except OSError as exc:
        raise ScaffoldError(
            f"could not finish installing into {archive_path}: {exc}. "
            f"Check that you can write there and have enough disk space, then run "
            f"install again."
        ) from exc

    print(f'Installed the plaintext tools into: {archive_path}')
    print(f'  {len(files)} file(s) copied; recorded in {archive_path / VERSION_FILE}')
    if skipped_aliases:
        print(f'  Skipped the internal {", ".join(skipped_aliases)}/ folder(s) - '
              f'fha.yaml roots: already points them outside this archive, so an '
              f'empty placeholder here would serve no purpose.')
    for alias, renamed in sorted(renames.items()):
        print(f'  Placed the {alias} placeholder at {renamed}/ - fha.yaml roots: '
              f'already renames it there.')
    print('\nNext steps:')
    print(f'  1. Edit {archive_path / "fha.yaml"} to point at your photos and documents.')
    print(f'  2. Open the archive in your AI agent and start filing inbox/ items.')
    print(f'  3. Tell your AI agent you\'d like to do the setup interview, so it can learn who '
          f'you are and number the tree correctly.')
    print(f'  4. Run `fha doctor` from inside the archive to check everything is set up.')
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

def _restore_exec_bits(archive_root: Path, repo_root: Path,
                       manifest: dict, *,
                       dry_run: bool = False) -> tuple[list[str], list[str]]:
    """Give back the executable bit to files the repo ships executable (POSIX).

    `shutil.copy2` preserves mode, so a fresh install is fine - but the bit can
    be lost afterwards without the bytes changing at all: copied off a Windows
    machine, restored from a zip that drops Unix modes, synced through a service
    that does not carry them. `_plan_update` compares CHECKSUMS, so it calls such
    a launcher "current", copies nothing, and `./fha` keeps failing with
    "Permission denied" no matter how many times the owner runs update-tools.

    So this is deliberately independent of the plan: mode is repaired whenever it
    is wrong, including for files nothing else touched this run. Only ever ADDS
    execute where the repo has it, and only where read permission already exists
    (mirroring the source's own bits); never removes a permission, and never
    touches content. A no-op on Windows, where the bit does not exist.

    Returns (repaired, failures). A chmod that fails is NOT swallowed: an owner
    who can write the stamp but does not own the launcher would otherwise get a
    clean exit 0 while `./fha` keeps refusing to run.

    `dry_run` detects without repairing, and the preview goes through this same
    function on purpose: a chmod is a real mutation, and a preview that returned
    "0 changes" before silently performing one is the one thing --dry-run must
    never do. Sharing the detection means the two cannot drift apart.
    """
    if os.name == 'nt':
        return [], []
    notes: list[str] = []
    problems: list[str] = []
    for entry in manifest['files']:
        if entry.get('category') != 'operating':
            continue
        src = repo_root / entry.get('src', entry['path'])
        dest = archive_root / entry['path']
        try:
            if not src.is_file() or not dest.is_file():
                continue
            src_mode = src.stat().st_mode
            exec_bits = src_mode & 0o111
            if entry['path'] in _MUST_BE_EXECUTABLE:
                # Not negotiable, and not inferred from the source copy: a zip
                # workshop ships this 0644 and would otherwise teach every
                # archive that the launcher is not meant to be runnable.
                exec_bits = 0o111
            if not exec_bits:
                continue
            dest_mode = dest.stat().st_mode
            if dest_mode & 0o111 == exec_bits:
                continue
            if not dry_run:
                dest.chmod(dest_mode | exec_bits)
        except OSError as exc:
            problems.append(
                f"{entry['path']}: is missing the permission that makes it "
                f'runnable, and it could not be restored ({exc}). Until it is, '
                f"running ./{entry['path']} fails with \"Permission denied\". "
                f"Fix it with: chmod +x \"{dest}\"")
            continue
        notes.append(entry['path'])
    return notes, problems


def _plan_update(
    archive_root: Path,
    repo_root: Path,
    manifest: dict,
    stamp: dict | None,
    fha_config: dict,
    *,
    roots_config_readable: bool = True,
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

    `fha_config` is this archive's CURRENT fha.yaml (not the one it was
    installed with) - used to keep a still-external documents/photos/inbox
    alias from getting its `.gitkeep` silently re-added below (#124), and to
    check a still-renamed-but-internal alias's delivery status at the folder
    it actually lives at rather than the literal alias name (`renames_now` -
    the same map `run_install` and `run_update_tools`'s stamp rewrite use, so
    all three agree on where a renamed alias's skeleton entries live).
    Pruning an already-installed placeholder that just WENT external is a
    separate step (`_prune_external_root_placeholders`, run by the caller).

    `roots_config_readable=False` (fha.yaml could not be parsed at all this
    run - PyYAML missing, or a genuine syntax error, #124 review round 3,
    finding 5) means `fha_config` is a `{}` fallback that must NOT be trusted
    as "nothing configured": treating it that way is exactly what would let an
    already-pruned external placeholder get silently RECREATED the moment its
    real `roots:` value cannot be read. So every documents/photos/inbox alias
    is withheld from delivery below, the same way a genuinely-external one
    already is - no scaffolding mutation is planned for any of them until the
    config can be read again.
    """
    recorded: dict[str, str] = _stamp_file_map(stamp)
    external_now = external_asset_roots(fha_config, archive_root)
    renames_now = _internal_root_renames(fha_config, archive_root)
    # See the `roots_config_readable=False` note above - every alias is
    # treated as off-limits for delivery, exactly like a genuinely external
    # one, whenever this run could not actually read what `roots:` says.
    withheld_from_delivery = (
        external_now if roots_config_readable else set(ASSET_ROOT_ALIASES)
    )

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

    # Skeleton seeds that are NEW upstream and absent here.
    #
    # Install-once means "never overwrite", not "never deliver". A seed added to
    # the manifest after this archive was created would otherwise never arrive:
    # `.gitattributes` is the case in hand - without it a Windows checkout with
    # `core.autocrlf` rewrites the `fha` launcher to CRLF, which /bin/sh cannot
    # run and which breaks the launcher's recorded checksum, so every later
    # update reports the stock launcher as customized.
    #
    # The stamp is what makes this safe: a seed the stamp never recorded has
    # never been delivered to this archive, so writing it cannot overwrite an
    # edit and cannot resurrect a file the owner deliberately deleted (that one
    # IS recorded). Absent from disk and absent from the stamp - both, or it is
    # left alone.
    # Delivery of a never-recorded seed rests entirely on the stamp being able to
    # say "this was never delivered here". Without a usable stamp it cannot say
    # anything - and `recorded` being empty then reads as "nothing was ever
    # delivered", which is the opposite of the truth for an archive that has been
    # running for years. The owner who deleted `custom.css` on purpose, or who
    # followed the corruption advice to delete the stamp, would get every deleted
    # seed silently recreated. So: no usable stamp, no delivery.
    can_prove_never_delivered = bool(recorded)
    for entry in manifest['files'] if can_prove_never_delivered else []:
        if entry.get('category') != 'skeleton':
            continue
        manifest_path = entry['path']
        # A documents/photos/inbox seed whose alias is CURRENTLY external
        # (#124) is never re-added: that folder has no purpose in this
        # archive's configuration, install-time skipped it on purpose (or
        # `_prune_external_root_placeholders` already removed it), and
        # without this guard it would come right back on every single
        # update - a `.gitkeep` this loop cannot tell apart from a
        # genuinely-never-delivered seed otherwise. Re-pointing the alias
        # back inside the archive drops it out of `external_now`, and the
        # very next update recreates the placeholder through this same path.
        # Also true, deliberately, of every alias while `roots:` could not be
        # read at all this run (`withheld_from_delivery` - finding 5): a
        # `{}` fallback config must never be mistaken for "nothing configured".
        if manifest_path.split('/', 1)[0] in withheld_from_delivery:
            continue
        # A still-renamed-but-internal alias (`documents: archive-docs`) is
        # checked, and delivered, at the folder it actually lives at - not the
        # literal alias name nobody ever wrote anything to (#124, TOOLING §13c).
        archive_path = _remap_skeleton_path(manifest_path, renames_now)
        if archive_path in recorded or (archive_root / archive_path).exists():
            continue
        # Planned even when the source is MISSING, deliberately. Dropping it
        # here hid a broken clone: the run reported zero additions, rewrote the
        # stamp and exited 0 while never delivering the seed. The missing-source
        # preflight inspects planned entries, so keeping it in the plan is what
        # turns a silent omission into the refusal that already exists.
        plan['added'].append((archive_path, repo_root / entry.get('src', manifest_path)))

    # Retired: a path the stamp recorded but the manifest no longer lists at all
    # (skeleton paths stay listed, so user data is never flagged retired). Move
    # only if it still exists; an already-removed file needs nothing. A renamed
    # skeleton entry's recorded key is the RENAMED path (#124), so both names
    # count as manifest-known - otherwise a still-current renamed placeholder
    # would misread as a retired file the moment it is on the stamp at all.
    manifest_all_paths: set[str] = set()
    for e in manifest['files']:
        manifest_all_paths.add(e['path'])
        if e.get('category') == 'skeleton':
            manifest_all_paths.add(_remap_skeleton_path(e['path'], renames_now))

    # A documents/photos/inbox alias renamed MORE THAN ONCE leaves its EARLIER
    # rename target recorded on the stamp too (`inbox: old-inbox`, later
    # changed to `inbox: new-inbox`) - a folder name that is neither the
    # literal alias nor the CURRENT rename target, so `manifest_all_paths`
    # above never recognizes it. `old-inbox/_TEMPLATE.notes.md` was a genuine
    # install-once seed at ITS OWN time, and the owner may have written real
    # notes into it since - exactly the kind of content this project's safety
    # philosophy says must never be swept into `.plaintext-backup/` on a mere
    # path-absence check that never even looks at the bytes (#124 review
    # round 3, finding 4). There is no persisted history of every past rename
    # value to consult, so this recognizes the SHAPE instead of the name: any
    # recorded path whose portion after its first segment matches the portion
    # after the alias in one of THIS alias's own skeleton entries is a
    # plausible current or former home for that skeleton identity, whatever
    # its first segment happens to be - and is therefore never eligible for
    # the unverified sweep below, no matter how many renames separate it from
    # today's config. (The litter-VERIFIED prunes -
    # `_prune_external_root_placeholders`, `_prune_orphaned_literal_root_folders`
    # - are the only things allowed to remove a folder like this, and only
    # after proving byte-for-byte that nothing but the shipped seed is in it.)
    alias_skeleton_tails: set[str] = set()
    for e in manifest['files']:
        if e.get('category') != 'skeleton':
            continue
        seg0, sep, rest = e['path'].partition('/')
        if sep and seg0 in ASSET_ROOT_ALIASES:
            alias_skeleton_tails.add(rest)

    retired: list[str] = []
    for archive_path in recorded:
        if archive_path in manifest_all_paths:
            continue
        _seg0, _sep, rest = archive_path.partition('/')
        if _sep and rest in alias_skeleton_tails:
            continue  # a current or former documents/photos/inbox home - never swept here
        if (archive_root / archive_path).exists():
            retired.append(archive_path)

    # Legacy layout relics found ON DISK, whether or not a stamp records them.
    #
    # An archive assembled by hand-copying `tools/` has no `.plaintext-version`,
    # so the recorded-path scan above finds nothing to retire. The update would
    # then install a complete stock toolset under `.fha/tools/` and leave the
    # flat one sitting there - and because the launcher prefers `.fha/tools/`,
    # the owner's customized flat tools would silently stop taking effect while
    # the run exits 0, having just promised that anything differing would be
    # backed up. Same shadowing risk for a stamp that has drifted from disk.
    #
    # Narrow by construction: only files under a VENDORED subtree root, only
    plan['retired'].extend((archive_path, None) for archive_path in sorted(retired))
    return plan


def _prune_emptied_dirs(archive_root: Path, retired_paths: list[str], *,
                        dry_run: bool = False) -> list[str]:
    """Remove directories left empty by retiring the files inside them.

    Only directories that actually held a retired file are considered, deepest
    first so a nested tree collapses in one pass. `rmdir` fails on a non-empty
    directory, which is the safety property: a folder still holding anything at
    all - a record, a stray note, a file this run failed to move - is left
    exactly where it is. The archive root is never a candidate.
    """
    candidates: set[Path] = set()
    for rel in retired_paths:
        parent = (archive_root / rel).parent
        while parent != archive_root and archive_root in parent.parents:
            candidates.add(parent)
            parent = parent.parent
    pruned: list[str] = []
    # Deepest-first, and the preview has to model the CASCADE the live pass gets
    # for free: once `tools/templates/` is removed, `tools/` may be empty too.
    # Judging each directory against the disk alone reports the child and misses
    # the parent, so predictions accumulate and count as already-gone.
    going = {archive_root / q for q in retired_paths}
    for directory in sorted(candidates, key=lambda q: len(q.parts), reverse=True):
        if dry_run:
            if directory.is_dir() and not any(
                    q for q in directory.iterdir() if q not in going):
                pruned.append(directory.relative_to(archive_root).as_posix())
                going.add(directory)
            continue
        try:
            directory.rmdir()
        except OSError:
            continue      # not empty, or not ours to remove - leave it
        pruned.append(directory.relative_to(archive_root).as_posix())
    return pruned


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

    Also removes an already-installed documents/photos/inbox placeholder
    folder whose alias just went external in fha.yaml, but ONLY if nothing but
    this install's own shipped bytes - verified recursively and byte-for-byte
    against what THIS ARCHIVE actually received, never against today's
    manifest and never by name alone (`_placeholder_only_scaffold_litter` +
    `_alias_seed_shas`, #124 review round 3 finding 3) - is inside it; real
    content, an edited seed, or a folder this run could not fully read is left
    completely alone (`_prune_external_root_placeholders`, #124). The literal
    alias folder an INTERNAL rename orphans (`documents: archive-docs` leaving
    `documents/` behind) gets the identical litter-verified treatment
    (`_prune_orphaned_literal_root_folders`, finding 2). A folder that WAS
    eligible but failed to remove (locked file, permission denied), and a
    `.plaintext-version` write that failed right after a successful prune
    (disk and the stamp are put back in agreement by restoring what was just
    removed), both surface as a reported failure rather than a silent no-op.
    An alias renamed but kept inside the archive (`documents: archive-docs`)
    gets its placeholder at the renamed folder, matching `run_install` and
    `_plan_update` (TOOLING §13c). A recorded path that is a current OR FORMER
    documents/photos/inbox home (an alias renamed more than once) is never
    swept into `.plaintext-backup/` by the generic retired-file path, which
    never checks bytes at all - only the litter-verified prunes above may
    remove one (finding 4). Without PyYAML, or with a fha.yaml that fails to
    parse at all, this whole check withholds every alias from delivery,
    pruning, and re-pruning and is reported, not silently treated as "nothing
    configured" (#124 review; finding 5 for the parse-failure case) - every
    OTHER part of this run still applies normally.

    Returns a `Result` (Result == int, so callers/tests comparing against EXIT_*
    keep working): EXIT_CLEAN on a clean update or dry run, EXIT_WARNINGS when
    one or more files could not be updated OR the #124 external-root check
    could not run (PyYAML missing, fha.yaml unparseable, or a placeholder
    failed to remove or restore), with the files actually installed (plus the
    rewritten stamp) listed in `changed` (empty under --dry-run). The update
    narration is printed inline. Raises ScaffoldError on any can't-run condition.
    """
    archive_root = Path(archive_root).resolve()
    manifest = load_manifest(repo_root)
    stamp = _load_version_stamp(archive_root)

    # This archive HAS an fha.yaml (that is what makes it an archive -
    # `find_archive_root`), so a missing PyYAML here is never "there was
    # nothing to check" - it is "roots: could not be read this run", and any
    # documents/photos/inbox alias pointed outside the archive silently keeps
    # its now-purposeless placeholder, or an already-pruned one silently never
    # gets recreated after a revert. Unlike `install` (whose usual roots: is
    # the template's own internal defaults), a REAL archive's fha.yaml is far
    # more likely to actually carry a customized `roots:` by this point, so
    # this is reported as a warning-level gap, not just a printed note (#124
    # review) - `fha update-tools`'s primary job (refreshing the operating
    # layer) still runs and still succeeds; only this one feature is skipped,
    # and the exit status says so rather than claiming a clean, complete run.
    yaml_missing = not yaml_available()
    yaml_missing_note = (
        f"PyYAML is not installed, so this run could not check fha.yaml's "
        f"roots: for a documents/photos/inbox folder pointed outside the "
        f"archive - the placeholder skip/prune (#124) was not checked this "
        f"run. Install it with `{pip_command('pyyaml')}`, then run "
        f"`fha update-tools` again."
    )

    # A SYNTAX ERROR in fha.yaml is a different failure than "PyYAML is not
    # installed", and the permissive `load_fha_yaml` used everywhere else in
    # this file cannot tell the two apart from "nothing configured" - it
    # returns `{}` for all three. Read that as "no roots:" and an external
    # alias whose placeholder was already correctly pruned gets it silently
    # RECREATED the moment an unrelated edit happens to break the YAML syntax
    # elsewhere in the file (#124 review round 3, finding 5). So this one
    # check gets its OWN strict read - the same `strict=True` `doctor.py`
    # already uses to fail loudly rather than silently drop configured roots -
    # specifically to tell "genuinely broken" apart from "genuinely empty".
    # Skipped when PyYAML itself is missing: `yaml_missing` already covers
    # that case with its own note, and a strict load would raise for the same
    # underlying reason, muddying the two into one message.
    config_broken = False
    config_broken_note = ''
    fha_config: dict = {}
    if yaml_missing:
        fha_config = {}
    else:
        try:
            fha_config = load_fha_yaml(archive_root, strict=True)
        except FhaConfigError as exc:
            config_broken = True
            config_broken_note = (
                f"{archive_root / 'fha.yaml'} has a problem and could not be read, "
                f"so this run could not check its roots: for a documents/photos/"
                f"inbox folder pointed outside the archive - the placeholder "
                f"skip/prune (#124) was not checked this run. {exc} Fix fha.yaml, "
                f"then run `fha update-tools` again."
            )
    # Either gap withholds every documents/photos/inbox alias from delivery,
    # pruning, and re-pruning below (`roots_config_readable`) - a `{}`
    # fallback must never be mistaken for "nothing configured".
    roots_config_readable = not (yaml_missing or config_broken)

    # The same refusal `install` makes, for the same reason - and it has to be
    # here too, because this is the command an owner of a hand-copied archive is
    # far more likely to reach for. Without a stamp there is nothing recording
    # the flat files, so they are never classified as retired: the run would
    # install `.fha/tools/`, refresh the launchers to prefer it, write a stamp
    # and exit 0, leaving the owner's customized `tools/` and `design/custom.css`
    # sitting in place, unbacked-up and no longer read by anything. There is no
    # migration path by design, so reconciling the two copies is the owner's
    # call - but it must be an informed one, not a silent switch-off.
    _refuse_directory_destinations(archive_root, manifest['files'])

    # A flat archive is refused, full stop - no condition on the stamp.
    #
    # This guard has been wrong three times, and each time because I fixed the
    # CONDITION instead of asking what it is for. Round 19 keyed it on "no
    # stamp"; round 23 on "no usable file map". Both let through the case that
    # matters most: an archive installed by the PREVIOUS release, which has a
    # perfectly valid populated stamp AND flat tools. For that archive the
    # retire-and-add path silently performs the very layout conversion this
    # project decided not to build - removing the flat tool tree, activating a
    # stock `.fha/design/custom.css`, and leaving the owner's styling only in
    # the backup folder, on an exit 0.
    #
    # The actual question is about the LAYOUT, which the stamp does not describe:
    # are the tools flat, with nothing vendored? Then this version cannot update
    # in place, whatever the stamp says.
    if (archive_root / 'tools' / 'fha.py').is_file() \
            and not (archive_root / VENDOR_DIR / 'tools').is_dir():
        raise ScaffoldError(
            f"{archive_root} keeps its tools at {Path('tools') / 'fha.py'}, from "
            f"a layout this version no longer uses - they now live in "
            f"{VENDOR_DIR}/. Updating in place would retire your flat tools/ into "
            f"{BACKUP_DIR}/ and install a fresh copy under {VENDOR_DIR}/, which "
            f"is a layout change, not an update: your customized "
            f"design/custom.css would be replaced by the stock one and survive "
            f"only as a backup.\n"
            f"There is no automatic conversion, by design. Move tools/ and "
            f"design/ under {VENDOR_DIR}/ yourself (keeping your edits), or start "
            f"a fresh archive with `fha install` and copy your records across - "
            f"then this command works normally again."
        )

    if stamp is None:
        print(
            f'No {VERSION_FILE} found in {archive_root} - treating existing tool '
            f'files as your own work. Anything different from the new version is '
            f'backed up (never overwritten), not replaced silently.'
        )
        print()

    plan = _plan_update(archive_root, repo_root, manifest, stamp, fha_config,
                        roots_config_readable=roots_config_readable)
    date_str = datetime.date.today().isoformat()
    # This archive's OWN installed baseline - what `_alias_seed_shas` compares
    # a placeholder's on-disk bytes against (finding 3), rather than blindly
    # against today's manifest.
    recorded_checksums = _stamp_file_map(stamp)

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

    plan_added_paths = [ap for ap, _src in plan['added']]
    n_added = len(plan['added'])
    n_stock = len(plan['stock'])
    n_custom = len(plan['customized'])
    n_retired = len(plan['retired'])
    n_current = len(plan['current'])

    if dry_run:
        print(f'Dry run - comparing {archive_root} against {repo_root / "manifest.json"}:')
        _report_plan(archive_root, plan, date_str, verbose=verbose)
        print()
        would_prune = _prune_emptied_dirs(
            archive_root, [ap for ap, _src in plan['retired']], dry_run=True)
        for gone in would_prune:
            print(f'[dry-run] would remove the now-empty folder {gone}/ '
                  f'(everything in it is being retired).')
        would_prune_external, _would_fail_external, _would_snapshot = _prune_external_root_placeholders(
            archive_root, fha_config, manifest, recorded_checksums, dry_run=True)
        for alias in would_prune_external:
            print(f'[dry-run] would remove the now-purposeless {alias}/ folder '
                  f'(fha.yaml roots: already points {alias} outside this archive, '
                  f'and nothing but the install placeholder is inside it).')
        would_prune_orphaned, _would_fail_orphaned = _prune_orphaned_literal_root_folders(
            archive_root, fha_config, manifest, recorded_checksums, dry_run=True)
        for alias in would_prune_orphaned:
            print(f'[dry-run] would remove the now-orphaned {alias}/ folder '
                  f'(fha.yaml roots: already renames {alias} elsewhere inside this '
                  f'archive, and nothing but the install placeholder is inside it).')
        would_repair, _ = _restore_exec_bits(
            archive_root, repo_root, manifest, dry_run=True)
        for repaired in would_repair:
            print(f'[dry-run] would restore the executable permission on '
                  f'{repaired} so it can be run directly again (its contents '
                  f'are already up to date).')
        if yaml_missing:
            print(f'[dry-run] {yaml_missing_note}')
        if config_broken:
            print(f'[dry-run] {config_broken_note}')
        print(
            f'Plan: {n_added} to add, {n_stock} to update, {n_custom} to back up '
            f'and update, {n_retired} retired, {n_current} already up to date'
            + (f', {len(would_repair)} permission(s) to restore'
               if would_repair else '') + '.'
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
            # The temp path is deterministic, so it is as attackable as the
            # destination: a `.fha-tmp` symlink left lying there would have
            # copy2 write THROUGH it (clobbering whatever it points at, possibly
            # outside the archive) and then `replace` would move the link itself
            # into the live tool path. Round 26 guarded manifest destinations and
            # their ancestors and stopped there - the temp siblings are the same
            # hazard by the same mechanism.
            _clear_stale_temp(tmp)
            shutil.copy2(src, tmp)
            # Fix the line endings BEFORE the swap, so the destination is only
            # ever replaced by a file that is already correct - a failure here
            # leaves the previous launcher in place rather than a half-right new
            # one, and reaches `_fail` like any other unwritable file.
            if archive_path in _MUST_BE_CRLF:
                _normalize_crlf(tmp)
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

    # An update can add files under `.fha/`, whose parent directories are
    # created implicitly by the copy - so ask for the hidden attribute here too.
    # Idempotent, and a no-op off Windows.
    if (archive_root / VENDOR_DIR).is_dir():
        _hide_vendor_dir(archive_root)

    manifest_all_paths = {e['path'] for e in manifest['files']}
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

    # Independent of the plan: a launcher whose bytes are current can still have
    # lost its executable bit, and no amount of re-running would have fixed it.
    repaired_modes, mode_failures = _restore_exec_bits(
        archive_root, repo_root, manifest)
    for repaired in repaired_modes:
        print(f'Restored the executable permission on {repaired} so it can be '
              f'run directly again.')
    failures.extend(mode_failures)

    # Retiring files one by one leaves their directories behind, empty. After a
    # flat -> .fha/ update that means a hollow `tools/` and `design/` still
    # sitting in the archive root - exactly the clutter the vendored layout
    # exists to remove, and indistinguishable at a glance from tools that failed
    # to move. Prune the husks, deepest first; rmdir refuses a non-empty
    # directory, so this can only ever remove what retirement emptied.
    _prune_emptied_dirs(archive_root,
                        [ap for ap, _src in plan['retired']
                         if ap not in failed_paths])

    # A documents/photos/inbox placeholder that just went external (roots:
    # re-pointed since install) and still holds nothing but scaffold litter is
    # purposeless dead weight (#124) - remove it the same way a retired file's
    # emptied directory is removed above, just triggered by the CURRENT
    # fha.yaml rather than by what retired this run.
    pruned_external, prune_failed, pruned_snapshots = _prune_external_root_placeholders(
        archive_root, fha_config, manifest, recorded_checksums)
    for alias in pruned_external:
        print(f'Removed the now-purposeless {alias}/ folder - fha.yaml roots: '
              f'already points {alias} outside this archive.')
    for alias, reason in prune_failed:
        # An eligible placeholder that FAILED to remove (locked file, denied
        # permission) must not exit clean and say nothing - the promised
        # cleanup silently did not happen, and the owner has no way to know
        # unless this run tells her (#124 review: never silently swallow a
        # failed mutation). Folded into the same `failures` list every other
        # per-file mutation failure reports through, so it contributes to the
        # warning exit status and the closing "could not be updated" summary.
        failures.append(
            f'{alias}/: could not remove the now-purposeless folder ({reason}). '
            f'fha.yaml roots: still points {alias} outside this archive.')

    # The literal alias folder an INTERNAL rename just orphaned (`documents:
    # archive-docs` leaving `documents/` behind) is exactly as purposeless,
    # for the same reason, and gets the identical litter-verified treatment
    # (#124 review round 3, finding 2).
    pruned_orphaned, prune_orphaned_failed = _prune_orphaned_literal_root_folders(
        archive_root, fha_config, manifest, recorded_checksums)
    for alias in pruned_orphaned:
        print(f'Removed the now-orphaned {alias}/ folder - fha.yaml roots: '
              f'already renames {alias} elsewhere inside this archive.')
    for alias, reason in prune_orphaned_failed:
        failures.append(
            f'{alias}/: could not remove the now-orphaned folder ({reason}). '
            f'fha.yaml roots: still renames {alias} elsewhere inside this archive.')

    if yaml_missing:
        # The rest of this run (refreshing the operating layer) still applies
        # normally - only the #124 external-root check was skipped - but that
        # is a real, reportable gap, not a silent no-op: fold it into the same
        # warning-status reporting every other per-run failure uses.
        failures.append(yaml_missing_note)
    if config_broken:
        # Same reasoning as yaml_missing above, for a fha.yaml that IS
        # readable by the interpreter but does not parse as valid YAML
        # (#124 review round 3, finding 5).
        failures.append(config_broken_note)
    # Its skeleton entries (documents/.gitkeep, …) must NOT be carried over in
    # the stamp rewrite below - "recorded" is what stops the seed-delivery
    # loop in `_plan_update` from ever offering it back, so a removed
    # placeholder has to become "never delivered" again, the same as a fresh
    # install that skipped it outright. That is also exactly what lets a
    # later revert to an internal root recreate it.
    pruned_skeleton_paths = {
        e['path'] for e in manifest['files']
        if e.get('category') == 'skeleton'
        and e['path'].split('/', 1)[0] in pruned_external
    }

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
    renames_now = _internal_root_renames(fha_config, archive_root)
    for entry in manifest['files']:
        manifest_path = entry['path']
        if entry.get('category') == 'skeleton':
            # A still-renamed-but-internal alias's entries are recorded under
            # the RENAMED path, not the literal alias name - the same map
            # `run_install` and `_plan_update` use, so every run agrees on
            # where this alias's skeleton files actually live (#124, TOOLING
            # §13c). A no-op for every entry that isn't under a renamed alias.
            archive_path = _remap_skeleton_path(manifest_path, renames_now)
            if archive_path in pruned_skeleton_paths:
                # Just removed above because its alias went external and held
                # only scaffold litter - leave it OUT of the stamp entirely
                # (see the comment where pruned_skeleton_paths is built).
                continue
            # A seed delivered THIS RUN must be recorded, or install-once is not
            # kept: unrecorded means "never delivered", so a later run would
            # re-deliver it - restoring a file the owner had deliberately
            # deleted. Otherwise carry the existing baseline verbatim; updates
            # never touch a seed that is already there.
            if archive_path in installed_ok:
                new_checksums[archive_path] = installed_ok[archive_path]
            elif archive_path in old_recorded:
                new_checksums[archive_path] = old_recorded[archive_path]
            else:
                # First stamp for a hand-copied archive: a seed already sitting
                # on disk has plainly been delivered, so record it as-is. Without
                # this it stays unrecorded, and a later deliberate deletion would
                # be undone by the next run - the install-once contract broken by
                # the very absence of memory this restamp exists to fix. Contents
                # are never touched, only observed.
                dest = archive_root / archive_path
                if dest.is_file():
                    try:
                        new_checksums[archive_path] = _sha256_file(dest)
                    except OSError as exc:
                        # Nothing is lost - and that is not the same as no
                        # consequence. An unrecorded seed is precisely what lets
                        # a later deliberate deletion be undone by a future run,
                        # so say so rather than leaving the gap silent.
                        failures.append(
                            f'{archive_path}: is on disk but could not be read '
                            f'({exc}), so this run could not record that you '
                            f'already have it. Until a run can read it, deleting '
                            f'it may not stick. Check its permissions and re-run.')
            continue
        # Operating-layer paths are never alias-remapped (only skeleton entries
        # under documents/photos/inbox ever are), so this branch uses the
        # manifest's literal path as-is.
        archive_path = manifest_path
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
        if pruned_skeleton_paths:
            # The OLD stamp - still on disk, since the write above just
            # failed - still lists this alias's skeleton entries as
            # delivered, but their folder was just removed a few lines up.
            # Left alone, that split survives a retry (nothing left to prune,
            # so it carries the stale "delivered" entries forward again) and
            # a later revert to an internal root would then read the stale
            # stamp as "already there" and never recreate the placeholder it
            # promises (#124 review). Restoring the exact bytes this run just
            # removed (the snapshot taken right before `rmtree`, NOT today's
            # repo copy - #124 review round 4, finding 1) puts disk back in
            # agreement with the stamp that is actually on disk, so the
            # ordinary revert-to-internal path keeps working.
            still_missing = _restore_pruned_placeholders(
                archive_root, pruned_skeleton_paths, pruned_snapshots)
            # An alias counts as restored only if EVERY one of its skeleton
            # paths came back - a partial restore leaves the folder in a state
            # nothing here can vouch for, so it is reported as still-missing
            # below rather than narrated as a clean restore.
            missing_aliases = {p.split('/', 1)[0] for p in still_missing}
            pruned_aliases = {p.split('/', 1)[0] for p in pruned_skeleton_paths}
            restored = sorted(pruned_aliases - missing_aliases)
            if restored:
                # Not pruned any more by the time this run ends - correct
                # the narration/data below to say so.
                pruned_external = [a for a in pruned_external if a not in restored]
                print(
                    f'Restored {", ".join(restored)}/ so it matches the baseline '
                    f'that is still recorded - run `fha update-tools` again to '
                    f'retry removing it.',
                    file=sys.stderr,
                )
            if still_missing:
                failures.append(
                    f"{', '.join(sorted(still_missing))}: could not be restored after "
                    f"the {VERSION_FILE} write failed, so disk and the recorded "
                    f"baseline may now disagree. Run `fha update-tools` again."
                )
    _seed_roots_stamp(archive_root)

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
    # A restored executable bit is a mutation this run made, even though no bytes
    # moved - so it belongs in the audit a headless caller reads, not only in the
    # narration a human sees.
    changed.extend(str(archive_root / p) for p in repaired_modes
                   if p not in installed_ok)
    # A removed placeholder folder is a mutation too, for the same reason a
    # restored executable bit is - a headless caller's audit should see it.
    changed.extend(str(archive_root / alias) for alias in pruned_external)
    changed.append(str(archive_root / VERSION_FILE))
    update_data = {
        'added': n_added_ok, 'stock': n_stock_ok, 'customized': n_custom_ok,
        'retired': n_retired_ok, 'current': n_current,
        'external_roots_pruned': pruned_external,
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
    command - the user sees a traceback from `update-tools`
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


def register(subs: argparse._SubParsersAction) -> None:
    """Register `install` and `update-tools` on the fha parser."""
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
    except ScaffoldError as exc:
        # A folder that would not list (generate_manifest's refusal). Plain
        # message, no traceback - the maintainer reads the same voice the
        # archive owner does.
        print(f'ERROR: {exc}', file=sys.stderr)
        return EXIT_FAILURE
    except OSError as exc:
        print(f'ERROR: could not write manifest: {exc}', file=sys.stderr)
        return EXIT_FAILURE
    manifest = json.loads(path.read_text(encoding='utf-8'))
    print(f'Wrote {path} ({len(manifest["files"])} files, '
          f'spec_version {manifest["spec_version"]}).')
    return EXIT_CLEAN


if __name__ == '__main__':
    sys.exit(_standalone_main())
