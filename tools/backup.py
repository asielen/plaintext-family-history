#!/usr/bin/env python3
"""
backup.py - fha backup: a dated zip snapshot of the archive.

  fha backup [--to PATH] [--include-assets] [--dry-run] [--root PATH]

One command that copies the whole archive into a single dated zip file next to
(never inside) the archive, so a dead disk costs nothing.  Restoring needs no
tool at all: a backup is just your files, zipped - unzip it anywhere and the
archive is back.  There is deliberately no restore verb (TOOLING §13e).

Plain files zip trivially - that is the payoff of the whole design.  Over
"zip the folder yourself" this command adds exactly three things:

  1. It knows what NOT to include: rebuildable caches (`.cache/`), rebuildable
     deliverables (`generated/`, `out/`), git's own history (`.git/`), and the
     machine-local WORKING_COPY marker.  Everything else in the tree rides
     along - the walk subtracts exclusions rather than enumerating includes,
     so a folder the human added by hand is never silently skipped (backup
     errs toward inclusion).
  2. It knows where the assets really live: the photos/documents roots are
     resolved through fha.yaml `roots:` (never hardcoded - they are often
     external and often tens of GB).  The default run is records-only and says
     so in plain words every time; `--include-assets` zips each EXTERNAL root
     under its alias name, and a root mapped INSIDE the archive at a different
     path (`roots: photos: media/photos`) under its real relative path, so an
     unzip restores exactly the layout the zipped fha.yaml describes.
     An `inbox/` that resolves inside the archive root is always included
     (staged material is irreplaceable); an inbox mapped outside the root is
     treated like the other asset roots.
  3. It leaves a stamp - `.cache/last_backup.json` - so `fha doctor` can
     report the real last-backup date instead of a platitude.  The stamp
     lives in `.cache/` because it is a statement about THIS copy on THIS
     machine (the same rationale that keeps WORKING_COPY out of fha.yaml,
     TOOLING §13d); losing it merely makes doctor over-remind, never
     under-remind.  It is also excluded from the zip itself, so a RESTORED
     archive honestly reports "no backup recorded" and prompts a fresh one.

A folder that will not open stops the backup.  `os.walk` swallows the OSError
from a directory it cannot list and moves on, so a folder whose permissions
changed - or an external drive that unmounted mid-run - looks exactly like an
empty folder, and the zip comes out missing it while the run says "backup
verified".  That is the one failure this command must never produce: the
human keeps the file for years believing it is his archive and finds out at
the only moment recovery is impossible.  So the walk carries an error seam
(`_lib.unreadable_dir_recorder`) and a run that could not read everything is
REFUSED before anything is written - a refused backup costs one re-run, an
incomplete one that looks complete costs the archive.  `--allow-incomplete`
is the escape for a folder that genuinely cannot be restored (a drive that is
gone for good), and it does not merely warn: the zip is NAMED
`…-INCOMPLETE.zip` and carries a `BACKUP_INCOMPLETE.txt` member listing what
was not read.  A warning scrolls past; the artifact outlives the terminal.

Three things count as "did not read it", not one.  A folder that raises on
listing is the obvious one.  The other two get to the same place by a route
`onerror` never sees, and both were reported as complete backups before:

  - A mapped asset root that is not a directory at all, on a run that asked
    for the assets (`--include-assets`).  An external drive that is simply
    not plugged in makes `/Volumes/PhotoDrive` vanish, and vanishing is not
    an error `os.walk` can raise because `os.walk` is never called.  A root
    the human NAMED in fha.yaml's `roots:` is one he asked to pack, so its
    absence refuses like any unreadable folder.  A root that exists only as
    the spec default (an archive with no `photos/` folder and no `roots:`
    line) is not something he asked for and is still just skipped.
  - A subfolder that is a symbolic link.  `os.walk` defaults to
    `followlinks=False` and drops such a subtree in silence - nothing listed,
    nothing handed to `onerror`.  These are recorded as unread rather than
    followed; `_walk_files` carries the reasoning.

The one honest gap left is a mount point that still exists as an empty folder
while its drive is away: nothing on this side can tell that from a folder that
really is empty.  So an included asset root that contributed no files at all
gets a plain warning naming the possibility, not a refusal - refusing every
archive that has yet to file its first photo would teach the human to reach
for `--allow-incomplete` by reflex, which costs more than it saves.

Safety posture: the archive tree is only ever read; the one in-tree mutation
is the `.cache/` stamp.  The destination must resolve OUTSIDE the archive root
and every mapped asset root (a zip inside the tree would be swept into the
next backup, or into an asset scan).  After writing, every member's CRC is
verified (`ZipFile.testzip`); on any write or verify failure the partial zip
is deleted and the run exits 3 - a backup that might be corrupt and says
nothing is worse than no backup.  The same cleanup holds for ANY exception
mid-write, Ctrl-C included: the partial zip is deleted before the exception
propagates.  Dry-run is byte-for-byte side-effect-free (the destination
folder is not even created).

Working-copy mode: a records-only backup RUNS (backup reads the tree and
writes outside it - nothing in the §13d asset-mutating refusal class), with an
honest note that the main archive is the copy needing the real backup.
`--include-assets` is REFUSED in WC mode (warning-level: ok=True, exit 0,
data.status='working-copy') - an "asset backup" that silently contains no
assets is the worst possible output for a backup tool.

Exit codes: 0 = backup written + verified, or dry-run plan printed, or the WC
--include-assets refusal; 2 = argparse-level bad invocation only; 3 = root
unresolvable, destination inside the archive/an asset root, a duplicate
in-zip name (refused before anything is written - extraction of a zip with
duplicate members silently keeps one copy, and a backup tool never guesses),
a folder that was not read - would not open, is a symbolic link, or is a
requested asset root that is not there (refused the same way, unless
--allow-incomplete), malformed fha.yaml, or a write/verify failure (partial
zip deleted).  There is no exit-1 arm: a partial or suspect backup is never a
warning, it is a failure.  An --allow-incomplete run exits 0 - the human
asked for exactly the zip he got, and what is missing from it travels inside
the zip and in its name rather than in an exit code.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import unicodedata
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    EXIT_CLEAN,
    EXIT_FAILURE,
    FhaConfigError,
    Message,
    Result,
    configure_utf8_stdout,
    get_roots,
    is_working_copy,
    load_fha_yaml,
    resolve_path,
    resolve_root_arg,
    unreadable_dir_recorder,
)

configure_utf8_stdout()

# ── CODE MAP ──────────────────────────────────────────────────────────────────
#
#  Which of two names is one folder
#    _norm                    - one absolute, symlink-resolved spelling (a STRING)
#    _fold                    - a name with case and Unicode spelling flattened
#    _file_id                 - (device, inode): the filesystem's own answer
#    _same_path               - samefile, with a string fallback for absent paths
#    _inside                  - exact containment, by climbing resolved parents
#    _contains                - 'exact' | 'name-fold' | None, for the destination
#
#  Destination
#    _resolve_destination     - --to flag > fha.yaml backup:path > sibling folder
#    _destination_conflict    - refuse a destination inside the archive/asset roots
#    _zip_target              - dated zip name, never overwriting (suffix _2, _3, …)
#
#  Planning (pure reads)
#    _asset_roots             - alias -> resolved Path for every excluded asset root
#    _walk_files              - one tree walk -> (abs_path, arcname, size) entries
#    _plan_backup             - the full include/exclude plan + size estimates
#    _arcname_collisions      - names claimed by 2+ files (run_backup refuses)
#    _fmt_size                - human-readable byte counts for the notes
#
#  A folder that was not read
#    _display_dir             - name a folder the way the human filed it
#    _unread_causes           - the cause + fix sentences, one per kind of miss
#    _incomplete_notice       - the text that rides INSIDE an incomplete zip
#
#  Execution
#    _write_zip               - write entries into the zip (test seam)
#    _verify_zip              - CRC-check every member via testzip (test seam)
#    _discard_partial         - best-effort unlink of a failed/interrupted zip
#    _write_stamp             - .cache/last_backup.json, the doctor stamp
#
#  Engine / interface
#    run_backup               - compute + execute; returns a _lib.Result
#    _cmd_backup              - the only layer that renders the Result
#    register / _run_backup / _standalone_main - CLI wiring
# ──────────────────────────────────────────────────────────────────────────────

# Top-level names excluded from the records walk, each with the plain-language
# reason the output states (a silent exclusion in a backup tool is a trust bug).
_EXCLUDED_DIRS = {
    '.cache': 'rebuildable databases - fha index and fha photoindex regenerate them',
    'generated': 'rebuildable deliverables - fha site and fha views regenerate them',
    'out': 'rebuildable exports - fha packet writes here',
    '.git': 'git history is its own backup channel; zipping it doubles the size for no restore value',
}
_EXCLUDED_FILES = {
    'WORKING_COPY': 'machine-local working-copy marker - it must never travel',
}

_RESTORE_LINE = ("To restore: unzip this file. That's the whole procedure - "
                 'a backup is just your files.')

_STAMP_NAME = 'last_backup.json'

# The member an --allow-incomplete zip carries so the caveat travels with the
# artifact. Named in capitals and sorted to the top of any file listing: the
# person who opens this zip may be someone else, years from now.
_NOTICE_NAME = 'BACKUP_INCOMPLETE.txt'


def _norm(path: Path | str) -> str:
    """One absolute, symlink-resolved spelling of a path.  A string, no more.

    `./x` and `x` are the same file and a symlink is whatever it points at;
    that is as far as tidying a STRING can get you.  `os.path.normcase` is
    kept because it is genuinely right on Windows, where the OS folds case
    itself - but on macOS and Linux it hands the path straight back, so this
    is a normaliser and NEVER an identity test.  (It used to be described as
    "case-folded" and used as one, which meant a destination spelled
    `/Users/x/Archive` was judged outside a root spelled `/Users/x/archive` -
    one folder on any ordinary Mac - and the backup was written inside the
    tree it was backing up.)

    Identity goes through `_same_path` and `_contains`; sameness of two files
    that both exist goes through `_file_id`.  This string is used only where a
    wrong match would DROP a file from the zip - the entry de-duplication in
    `_plan_backup` - because there the blunt direction is the harmful one and
    two hard links to one file are two files a backup must both keep.
    """
    return os.path.normcase(str(Path(path).resolve()))


def _fold(name: str) -> str:
    """One filename with its case and its Unicode spelling flattened away.

    Two things make one name look like two.  A case-insensitive volume - the
    default on macOS and on Windows - stores `Archive` and hands it back to
    whoever asks for `archive`.  And HFS+ keeps its directory entries
    decomposed (NFD), so a name typed as NFC comes back off the disk as a
    different Python string with the same accents.  NFC-normalising settles
    the second, `casefold` the first.

    Deliberately more eager than any filesystem's own folding table, and used
    in exactly one place (the destination check) where matching too much costs
    the human one sentence and matching too little costs him the archive.
    """
    return unicodedata.normalize('NFC', name).casefold()


def _file_id(path: Path | str):
    """The filesystem's own name for a file or folder: (device, inode).

    The one identity that survives every way a path can be spelled - a case
    variant on a folding volume, an NFD/NFC accent, a symlink, a hard link, a
    second mount of the same disk.  None of those are visible to a string.

    Falls back to the resolved string when the path cannot be stat'd, and when
    the filesystem reports inode 0 (some network and Windows filesystems do):
    a key that collided for every file would be worse than no key at all, and
    a string can never collide with a real (device, inode) pair.
    """
    try:
        st = os.stat(path)
    except OSError:
        return _norm(path)
    return (st.st_dev, st.st_ino) if st.st_ino else _norm(path)


def _same_path(a: Path | str, b: Path | str) -> bool:
    """Do these two names point at one file or folder, as the filesystem sees it?

    `os.path.samefile` compares (device, inode) - the filesystem's own answer,
    correct on a case-insensitive volume, through a symlink, through a hard
    link and across two mounts of one disk.  It needs both paths to exist and
    raises OSError when one does not, which is not a problem to work around: a
    folder that is not there cannot be the folder in hand.  The resolved-string
    comparison stays as the fallback so two spellings of one absent path still
    compare equal.
    """
    try:
        return os.path.samefile(a, b)
    except OSError:
        return _norm(a) == _norm(b)


def _inside(child: Path | str, parent: Path | str) -> bool:
    """True when `child` is `parent` or lies anywhere under it, exactly.

    Answered by identity, not by string prefix: two spellings of one folder
    are one folder, and a prefix test reads them as two.  Resolve both sides,
    take the prefix test as a fast POSITIVE (both sides are resolved by then,
    so a match is real), then climb `child`'s parents comparing each against
    `parent` with `_same_path`.

    Walking up is also what answers for a path that does not exist yet - a
    backup destination usually does not - because the climb reaches the folder
    that will hold it, and a file is inside `parent` exactly when the nearest
    existing folder above it is.

    Exact on purpose.  Callers here use the answer to decide whether an asset
    root is internal (and then take `relative_to`, which is string-based) and
    whether a mapped inbox rides the records walk; in both places a match that
    the strings do not support would either crash or quietly subtract a folder
    from the backup.  The blunt, fold-anything arm lives in `_contains`, which
    only the destination guard uses.
    """
    target = Path(child).resolve()
    base = Path(parent).resolve()
    t = str(target).rstrip(os.sep)
    b = str(base).rstrip(os.sep)
    if t == b or t.startswith(b + os.sep):
        return True
    cur = target
    while True:
        if _same_path(cur, base):
            return True
        if cur.parent == cur:
            return False
        cur = cur.parent


def _contains(parent: Path | str, child: Path | str) -> str | None:
    """How `parent` contains `child`: 'exact', 'name-fold', or None.

    The destination guard is the one place where the two mistakes are not the
    same size.  Saying "inside" about a folder that is really outside costs the
    human one sentence and a different folder name; saying "outside" about a
    folder that is really inside writes his backup into the archive it is
    backing up, where the next backup sweeps it in and a dead disk takes both.

    So this asks twice.  `_inside` is the exact answer.  Failing that, the
    same climb runs over case- and Unicode-folded spellings, which is what a
    case-insensitive volume would say if this program could ask it - and it
    can't, portably, without writing to the volume.  A folded-only match is
    reported as 'name-fold' rather than 'exact' so the refusal can say
    something true on BOTH kinds of disk: on a case-sensitive one those really
    are two folders, and a message insisting otherwise would be a dead end.
    """
    if _inside(child, parent):
        return 'exact'
    base = _fold(str(Path(parent).resolve()).rstrip(os.sep))
    cur = Path(child).resolve()
    while True:
        if _fold(str(cur).rstrip(os.sep)) == base:
            return 'name-fold'
        if cur.parent == cur:
            return None
        cur = cur.parent


def _fmt_size(n: int) -> str:
    """Human-readable size (KB/MB/GB) for the notes and the dry-run plan."""
    size = float(n)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024 or unit == 'TB':
            if unit == 'B':
                return f'{int(size)} {unit}'
            return f'{size:.1f} {unit}'
        size /= 1024
    return f'{int(n)} B'


# ── Destination ───────────────────────────────────────────────────────────────

def _resolve_destination(
    archive_root: Path, fha_config: dict, to: str | None,
) -> tuple[Path | None, str]:
    """Pick the backup folder: --to flag > fha.yaml `backup: path:` > sibling.

    The default is a sibling folder next to the archive root
    (`{root-name}-backups/`) because it is visible in the same file-browser
    window as the archive itself - the human can SEE their backups exist.
    The fha.yaml key gets the same tolerance as `roots:` values (absolute
    used as-is, relative joined to the archive root); `--to` is a normal CLI
    path (relative to the current directory).

    Returns (path, source-description) - the description names where the
    choice came from so guard refusals can say which setting to change.  A
    `backup:` key whose shape is not understood returns (None, error-text)
    rather than silently falling back to the default: a misread config must
    not quietly change where backups land.
    """
    if to:
        p = Path(to)
        base = p if p.is_absolute() else Path.cwd() / p
        return base.resolve(), 'the --to flag'

    cfg = fha_config.get('backup')
    if cfg is not None:
        if isinstance(cfg, str):
            val = cfg
        elif isinstance(cfg, dict) and isinstance(cfg.get('path'), str):
            val = cfg['path']
        else:
            return None, (
                'the backup: setting in fha.yaml was not understood - write it as:\n'
                'backup:\n'
                '  path: D:/ArchiveBackups\n'
                'or remove it to use the default folder beside your archive.'
            )
        base = Path(val) if os.path.isabs(val) else archive_root / val
        return base.resolve(), 'the backup: path setting in fha.yaml'

    return (
        (archive_root.parent / f'{archive_root.name}-backups').resolve(),
        'the default folder beside your archive',
    )


def _destination_conflict(
    dest: Path, archive_root: Path, fha_config: dict, source_desc: str,
) -> str | None:
    """Refuse a destination that resolves inside the archive or an asset root.

    A zip inside the tree gets swept into the next backup (backups of backups,
    growing forever) or into an asset scan - the `fha packet` out-path guard
    precedent, applied without an `out/` exemption because backups must be
    reachable when the archive's own disk is dead.  Symlinks were resolved by
    the caller so a link cannot smuggle the destination inside.  Returns the
    plain-language refusal (cause + the exact fix), or None when safe.
    """
    protected: list[tuple[str, Path]] = [('your archive', archive_root)]
    for alias in sorted(set(get_roots(fha_config)) | {'photos', 'documents'}):
        protected.append((f'your {alias} root', resolve_path(alias, fha_config, archive_root)))
    for label, folder in protected:
        how = _contains(folder, dest)
        if how == 'exact':
            return (
                f'backup destination {dest} is inside {label} ({folder}) - '
                f'backups must live outside the archive so they survive it '
                f'(that path came from {source_desc}). '
                f'Try `--to <a folder outside the archive>` or set '
                f'`backup: path:` in fha.yaml.'
            )
        if how == 'name-fold':
            # Spelled the same but for capitals or accents. On this machine
            # they may genuinely be two folders; on a Mac or a Windows PC -
            # and on the next computer these files are copied to - they are
            # one, and the backup would land inside the archive. Say exactly
            # that rather than asserting a containment the paths do not show.
            return (
                f'backup destination {dest} is spelled the same as {label} '
                f'({folder}) apart from capital letters or accents (that path '
                f'came from {source_desc}). On most Macs and on every Windows '
                f'PC those are ONE folder, so the backup would be written '
                f'inside the archive it is backing up - where the next backup '
                f'would sweep it in, and a dead disk would take both. Give the '
                f'backup a clearly different folder: `--to <a folder outside '
                f'the archive>`, or set `backup: path:` in fha.yaml.'
            )
    return None


def _zip_target(dest_dir: Path, root_name: str, incomplete: bool = False) -> Path:
    """Return today's zip path, uniquified so a re-run never overwrites.

    `{root-name}-backup_{YYYY-MM-DD}.zip`; a same-day second run appends
    `_2`, `_3`, … - an existing backup is never destroyed by making another.

    An `incomplete` run gets `-INCOMPLETE` in the NAME.  The filename is the
    only part of a backup anyone reads while scrolling a folder of them years
    later, so a zip that is short of the archive says so there, not just in a
    message the terminal has long since eaten.
    """
    stamp = datetime.date.today().isoformat()
    mark = '-INCOMPLETE' if incomplete else ''
    candidate = dest_dir / f'{root_name}-backup_{stamp}{mark}.zip'
    n = 2
    while candidate.exists():
        candidate = dest_dir / f'{root_name}-backup_{stamp}{mark}_{n}.zip'
        n += 1
    return candidate


# ── Planning ──────────────────────────────────────────────────────────────────

def _asset_roots(archive_root: Path, fha_config: dict) -> dict[str, Path]:
    """Resolve every asset root the default backup excludes: alias -> Path.

    Covers each alias in fha.yaml `roots:` plus the spec's `photos` and
    `documents` defaults, resolved through `resolve_path` (never hardcoded -
    AGENTS_TOOLING config-surface check).  Two carve-outs:

      - `inbox` that resolves INSIDE the archive root is not an asset root
        here: staged material is irreplaceable, so it rides the records walk.
        An inbox mapped outside the root is excluded/included like photos.
      - a root that resolves to the archive root itself (or contains it) is
        ignored: excluding it would exclude everything, and backup errs
        toward inclusion on a pathological config.
    """
    aliases = set(get_roots(fha_config)) | {'photos', 'documents'}
    roots: dict[str, Path] = {}
    for alias in sorted(aliases):
        resolved = resolve_path(alias, fha_config, archive_root).resolve()
        if alias == 'inbox' and _inside(resolved, archive_root):
            continue
        if _inside(archive_root, resolved):
            continue
        roots[alias] = resolved
    return roots


def _walk_files(
    base: Path,
    arc_prefix: str,
    excluded_dir_ids: frozenset = frozenset(),
    excluded_top_dirs: frozenset[str] = frozenset(),
    excluded_top_files: frozenset[str] = frozenset(),
    on_error=None,
    link_dirs: list[Path] | None = None,
) -> list[tuple[Path, str, int]]:
    """Walk `base` and return sorted (abs_path, arcname, size) entries.

    Arcnames are posix-form relative paths (the plan's Windows-long-path watch
    item), prefixed with `arc_prefix` when zipping an asset root (an external
    root uses its alias, an internal one its real in-archive relative path).  Directory pruning asks the filesystem
    which folder is which ((device, inode) via `_file_id`), so a `roots:` line
    spelled with different capitals or accents than the folder on disk still
    prunes the right one - a string comparison there let an internal photos
    root be walked twice, and two entries claiming one in-zip name is a
    refusal that costs the whole backup.  A file
    whose size cannot be read is kept with size 0 rather than dropped - if it
    is truly unreadable the zip write fails loudly later, which beats a backup
    that silently omitted it.  Empty directories are not recorded: they carry
    no data, and the spec's skeleton ships `.gitkeep` placeholders.

    `on_error` is `os.walk`'s error seam (`_lib.unreadable_dir_recorder`), and
    a backup passes it for every walk whose files go INTO the zip.  Without
    it, a folder that will not list is indistinguishable from an empty one and
    its contents leave the zip in silence - the failure this whole command is
    written against.  It is deliberately NOT passed for the size ESTIMATE of a
    root nobody asked to include: an unreadable corner of a skipped photo
    library makes one printed number low, which is no reason to refuse a
    records backup.

    `link_dirs` collects any subfolder that is a SYMBOLIC LINK, and passing it
    is the second half of the same promise.  `os.walk` defaults to
    `followlinks=False`, which does not merely decline to follow the link - it
    skips the subtree without a sound.  `onerror` never fires, because no
    listing was ever attempted, so a linked `photos/2019` left the zip exactly
    the way an unreadable folder used to: silently.

    Recorded rather than followed, and the trade-off is real either way.
    FOLLOWING (with a `(st_dev, st_ino)` loop guard, the way
    `find_duplicate_media.py`'s `walk_covering` does) would put the files in
    the zip - but it packs content from OUTSIDE the root under a name inside
    it, and on restore the link comes back as a real folder full of copies.
    A tool whose whole promise is "restore = unzip" must not hand back a tree
    shaped differently from the one it was given, and it must not decide on
    its own to pack whatever is on the far end of a link.  RECORDING keeps the
    refusal honest and loud: the folder is named, `--allow-incomplete` still
    writes the rest, and the notice inside the zip tells a restore what it
    still has to fetch.  It is also the direction the archive's own rules
    already point (AGENTS.md "No symlinks" for the archive tree) - though the
    reason it is recorded for EXTERNAL asset roots too, where that rule does
    not reach, is the restore shape, not the archive convention.
    """
    entries: list[tuple[Path, str, int]] = []
    if not base.is_dir():
        return entries
    for dirpath, dirnames, filenames in os.walk(base, onerror=on_error):
        d = Path(dirpath)
        at_top = (d == base)
        keep = []
        for name in sorted(dirnames):
            if at_top and name in excluded_top_dirs:
                continue
            if excluded_dir_ids and _file_id(d / name) in excluded_dir_ids:
                continue
            # After the exclusions, never before: a mapped asset root reached
            # through a link is EXCLUDED from this walk on purpose, and calling
            # a deliberate exclusion an unread folder would refuse every
            # records-only backup of an archive whose `photos` entry is a link.
            if link_dirs is not None and (d / name).is_symlink():
                if (d / name) not in link_dirs:
                    link_dirs.append(d / name)
                continue
            keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            if at_top and name in excluded_top_files:
                continue
            p = d / name
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            rel = p.relative_to(base).as_posix()
            arcname = f'{arc_prefix}/{rel}' if arc_prefix else rel
            entries.append((p, arcname, size))
    return entries


def _plan_backup(
    archive_root: Path, fha_config: dict, include_assets: bool,
) -> dict:
    """Compute the full include/exclude plan without touching anything.

    Returns a dict (the same shape lands in Result.data so a headless caller
    reads the plan a dry-run prints):

      entries        [(abs_path, arcname, size), …] - what goes in the zip
      folders        {top-level arc segment: {'files': n, 'bytes': n}}
      excluded       [(name, reason), …] - only exclusions that exist on disk
      skipped_roots  [(alias, str(path), est_bytes), …] - asset roots left out
      included_roots [(alias, str(path), external?), …] - asset roots zipped in
      unreadable_dirs [Path, …] - folders this walk did not read, so whatever
                     is filed in them is missing from `entries` and nothing
                     else here can tell (run_backup refuses on a non-empty
                     list).  Three kinds, all with the same consequence:
                     one that would not list, one that is a symbolic link,
                     and a requested asset root that is not there at all
      link_dirs      [Path, …] - the symbolic-link subset of the above
      missing_roots  [(alias, str(path)), …] - the requested-asset-root subset
      empty_roots    [(alias, str(path)), …] - included asset roots that DID
                     open and held no files (a warning, never a refusal - an
                     unmounted mount point and an empty folder are the same
                     thing from here)

    Sizes are computed here, at plan time, so the dry-run and the assets note
    both print real numbers.  Asset roots are walked in sorted-alias order and
    entries are deduplicated by absolute path, so a root nested inside another
    (SPEC §12.4's `inbox: C:/Photos/_inbox` example) is zipped once, under the
    first alias that reaches it.  That de-duplication is the one place a
    resolved STRING is still the right key: a wrong match here drops a file
    from the zip, and two hard links to one file are two files the human
    filed and expects back.
    """
    asset_roots = _asset_roots(archive_root, fha_config)
    # EVERY asset root, not just the internal ones.  An external root cannot be
    # reached by walking the archive tree - except through a symbolic link, and
    # that is exactly the case where the records walk must recognise it as the
    # asset root it deliberately leaves out rather than as an unread folder.
    # `_file_id` stats through the link, so the two spellings answer as one.
    asset_root_ids = frozenset(_file_id(p) for p in asset_roots.values())

    # One recorder for every walk that feeds the zip, so the refusal names all
    # of them at once whichever tree they were in.
    unreadable_dirs: list[Path] = []
    on_error = unreadable_dir_recorder(unreadable_dirs)
    link_dirs: list[Path] = []
    missing_roots: list[tuple[str, str]] = []
    empty_roots: list[tuple[str, str]] = []

    entries = _walk_files(
        archive_root,
        arc_prefix='',
        excluded_dir_ids=asset_root_ids,
        excluded_top_dirs=frozenset(_EXCLUDED_DIRS),
        excluded_top_files=frozenset(_EXCLUDED_FILES),
        on_error=on_error,
        link_dirs=link_dirs,
    )

    # An alias the human WROTE in fha.yaml `roots:` is a folder he told the
    # archive about; `photos`/`documents` also exist as spec defaults for every
    # archive, whether or not there is anything there.  The difference decides
    # whether a root that is not on disk is a failure or a non-event.
    configured_aliases = set(get_roots(fha_config))

    skipped_roots: list[tuple[str, str, int]] = []
    included_roots: list[tuple[str, str, bool]] = []
    if include_assets:
        seen = {_norm(p) for p, _arc, _s in entries}
        for alias, root in asset_roots.items():
            if not root.is_dir():
                skipped_roots.append((alias, str(root), 0))
                if alias in configured_aliases:
                    # The human asked to pack this root and it is not there -
                    # the unplugged-drive case the whole refusal exists for.
                    # An unreadable SUBFOLDER of it already refuses; the root
                    # itself vanishing used to exit 0 with `complete: True`.
                    missing_roots.append((alias, str(root)))
                    if root not in unreadable_dirs:
                        unreadable_dirs.append(root)
                continue
            internal = _inside(root, archive_root)
            # An internal mapped root keeps its REAL relative path in the zip
            # (media/photos/..., not photos/...): the zipped fha.yaml still
            # maps `photos: media/photos`, so re-homing the files under the
            # alias would make a 'verified' backup whose unzip puts the
            # assets where the restored config does not look.  External
            # roots have no in-archive path, so they pack under the alias
            # name and the restore note explains the wrinkle.
            prefix = root.relative_to(archive_root).as_posix() if internal else alias
            included_roots.append((alias, str(root), not internal))
            found_here = 0
            for p, arc, size in _walk_files(root, arc_prefix=prefix,
                                            on_error=on_error,
                                            link_dirs=link_dirs):
                found_here += 1
                key = _norm(p)
                if key in seen:
                    continue
                seen.add(key)
                entries.append((p, arc, size))
            if not found_here:
                # It opened and held nothing.  That is either a library with
                # no files in it yet or a mount point whose drive is away, and
                # from here the two are the same folder.  Say so; do not
                # refuse (see the module docstring).
                empty_roots.append((alias, str(root)))
    else:
        for alias, root in asset_roots.items():
            if not root.is_dir():
                continue
            est = sum(size for _p, _arc, size in _walk_files(root, arc_prefix=alias))
            skipped_roots.append((alias, str(root), est))

    folders: dict[str, dict[str, int]] = {}
    for _p, arc, size in entries:
        top = arc.split('/', 1)[0]
        bucket = folders.setdefault(top, {'files': 0, 'bytes': 0})
        bucket['files'] += 1
        bucket['bytes'] += size

    # The link subtrees join the same list the refusal reads.  `link_dirs` is
    # kept alongside only so the message can name the right cause and the right
    # fix; every consumer downstream - the refusal, the zip's name, the notice
    # member, `complete`, the doctor stamp - asks one question, "what did this
    # walk not read", and gets one answer.
    for p in link_dirs:
        if p not in unreadable_dirs:
            unreadable_dirs.append(p)

    excluded: list[tuple[str, str]] = []
    for name, reason in _EXCLUDED_DIRS.items():
        if (archive_root / name).is_dir():
            excluded.append((f'{name}/', reason))
    for name, reason in _EXCLUDED_FILES.items():
        if (archive_root / name).exists():
            excluded.append((name, reason))

    return {
        'entries': entries,
        'folders': folders,
        'excluded': excluded,
        'skipped_roots': skipped_roots,
        'included_roots': included_roots,
        'unreadable_dirs': unreadable_dirs,
        'link_dirs': link_dirs,
        'missing_roots': missing_roots,
        'empty_roots': empty_roots,
    }


def _arcname_collisions(
    entries: list[tuple[Path, str, int]],
) -> dict[str, list[Path]]:
    """Map arcname -> source paths for every in-zip name claimed by 2+ files.

    Zip members are identified by name alone: two entries with the same
    arcname both write fine and both pass CRC verification (testzip checks
    integrity, not uniqueness), but extraction silently keeps only one copy -
    data loss wearing a 'verified' badge.  The known route here is an
    archive-internal top-level folder named like a mapped root's alias (a
    real `photos/` folder plus `roots: photos:` pointing at an external
    library, with --include-assets).  run_backup refuses to write anything
    when this returns a non-empty dict: a backup tool never guesses which
    copy the human meant to keep.
    """
    by_arc: dict[str, list[Path]] = {}
    for path, arcname, _size in entries:
        by_arc.setdefault(arcname, []).append(path)
    return {arc: paths for arc, paths in by_arc.items() if len(paths) > 1}


# ── A folder that was not read ────────────────────────────────────────────────

def _display_dir(path: Path, archive_root: Path) -> str:
    """Name a folder the way the human filed it - 'people/003 Hartley'.

    Archive-relative when the folder is inside the archive; an external asset
    root has no in-archive path, so it keeps its own (forward-slashed)
    spelling.  Naming a folder wrongly is worse than naming it long.
    """
    try:
        return Path(path).relative_to(archive_root).as_posix()
    except ValueError:
        return str(path).replace('\\', '/')


def _unread_causes(plan: dict, archive_root: Path) -> list[str]:
    """One cause-and-fix sentence for each KIND of folder the walk did not read.

    Three routes land in `unreadable_dirs` and they do not share a fix.  A
    message that offers "reconnect the drive" for a symbolic link, or "check
    the folder's permissions" for a root that was never on this machine, names
    the wrong cause - and a message that blames the wrong cause is a defect in
    its own right (AGENTS.md, the next-step rule).  So the refusal keeps one
    headline and appends only the sentences that apply to this run.
    """
    links = plan.get('link_dirs') or []
    missing = plan.get('missing_roots') or []
    link_ids = {_file_id(p) for p in links}
    missing_ids = {_file_id(Path(path)) for _alias, path in missing}
    plain = [p for p in plan.get('unreadable_dirs') or []
             if _file_id(p) not in link_ids and _file_id(p) not in missing_ids]

    causes: list[str] = []
    if plain:
        causes.append(
            'A folder that will not open is usually one whose permissions '
            'changed, or a drive or network share that is not connected: '
            'reconnect it (or restore your access), then run `fha backup` '
            'again.'
        )
    for alias, path in missing:
        causes.append(
            f'Your {alias} folder is not there at all: fha.yaml says your '
            f'{alias} live in {path} (the `roots: {alias}:` line), and nothing '
            f'is at that path right now - so not one {alias} file could be '
            f'packed. Plug that drive in, or point that line at where the '
            f'folder really is, then run `fha backup --include-assets` again.'
        )
    if links:
        named = ', '.join(_display_dir(p, archive_root) for p in links[:5])
        if len(links) > 5:
            named += f' and {len(links) - 5} more'
        causes.append(
            f'{len(links)} of these are shortcuts (symbolic links) pointing at '
            f'a folder somewhere else: {named}. A backup does not follow a '
            f'shortcut - unzipping would hand you a real folder full of copies '
            f'where your shortcut used to be, which is not the archive you had. '
            f'Move the real folder here, or leave the shortcut and back up what '
            f'it points at separately.'
        )
    return causes


def _incomplete_notice(shown: list[str], archive_root: Path) -> str:
    """The plain-language text written INSIDE an --allow-incomplete zip.

    A warning on the terminal is gone by the afternoon; this zip may be opened
    in ten years by someone who was not in the room.  So the fact that it is
    short of the archive travels with the file itself - in the member name, in
    this text, and in the zip's own filename - and it says which folders, so a
    restore knows exactly what it still has to find elsewhere.
    """
    lines = [
        'THIS BACKUP IS INCOMPLETE.',
        '',
        f'It was made from {archive_root.name} on '
        f'{datetime.datetime.now().isoformat(timespec="seconds")} with the',
        '--allow-incomplete option, because these folders could not be opened '
        'and so',
        'nothing filed in them is inside this zip:',
        '',
    ]
    lines.extend(f'  {name}' for name in shown)
    lines.extend([
        '',
        'Everything else in the archive is here and unpacks normally.',
        'If you still have those folders somewhere, copy them in after '
        'unzipping,',
        'or make a fresh `fha backup` once they can be read again.',
    ])
    return '\n'.join(lines) + '\n'


# ── Execution ─────────────────────────────────────────────────────────────────

def _write_zip(
    zip_path: Path,
    entries: list[tuple[Path, str, int]],
    notice: tuple[str, str] | None = None,
) -> None:
    """Write every planned entry into the zip (deflated, posix arcnames).

    Kept as its own function so the failure-injection tests can seam it; any
    exception propagates to run_backup, which deletes the partial file.

    `notice` is an optional (arcname, text) member written first, used for the
    incomplete-backup notice so the caveat is inside the artifact rather than
    only on a terminal nobody will read again.
    """
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        if notice is not None:
            zf.writestr(notice[0], notice[1])
        for path, arcname, _size in entries:
            zf.write(path, arcname)


def _verify_zip(zip_path: Path) -> str | None:
    """CRC-check every member; return a plain cause on failure, None when good.

    `ZipFile.testzip()` reads every member and returns the first bad name.
    Verification is not optional: a backup that might be corrupt and says
    nothing is worse than no backup.
    """
    try:
        with zipfile.ZipFile(zip_path) as zf:
            bad = zf.testzip()
    except (OSError, zipfile.BadZipFile) as exc:
        return f'the zip could not be read back ({exc})'
    if bad is not None:
        return f'the file {bad!r} failed its integrity check inside the zip'
    return None


def _discard_partial(zip_path: Path) -> None:
    """Best-effort removal of a partial zip after a failed/interrupted write.

    A cleanup failure must never mask the original error, so OSError here is
    swallowed - the worst case is a leftover partial file, which is exactly
    the state cleanup was trying to prevent, not a new failure to report.
    """
    try:
        zip_path.unlink(missing_ok=True)
    except OSError:
        pass


def _write_stamp(archive_root: Path, stamp: dict) -> Path:
    """Write `.cache/last_backup.json`, the fact `fha doctor` reports.

    `.cache/` is git-ignored and machine-local - exactly right for a statement
    about this copy on this machine (TOOLING §13d rationale).  Creating the
    folder here is safe: `.cache/` is disposable by contract.
    """
    cache_dir = archive_root / '.cache'
    cache_dir.mkdir(parents=True, exist_ok=True)
    stamp_path = cache_dir / _STAMP_NAME
    stamp_path.write_text(json.dumps(stamp, indent=2), encoding='utf-8')
    return stamp_path


# ── Engine ────────────────────────────────────────────────────────────────────

def run_backup(
    archive_root: Path,
    fha_config: dict,
    *,
    to: str | None = None,
    include_assets: bool = False,
    dry_run: bool = False,
    allow_incomplete: bool = False,
) -> Result:
    """Compute the backup plan and (unless dry-run) write + verify the zip.

    The engine half of the TOOLING §1 split: returns a Result, prints nothing.
    Result.data carries the SAME keys on every status - {'status':
    'ok'|'dry-run'|'working-copy'|'bad-destination'|'name-collision'|
    'unreadable-folders'|'write-failed', 'zip_path', 'files', 'bytes',
    'assets_included', 'skipped_roots', 'folders', 'excluded',
    'unreadable_dirs', 'complete'} - so a headless consumer never
    needs a per-arm guard; arms that never reached planning carry empty
    values.  `changed` lists the zip and the stamp on a live run, nothing
    on dry-run.
    `bytes` is the finished zip's on-disk size (the number a human compares
    against free disk space); the per-folder plan sizes are content bytes.

    Failure posture: a duplicate in-zip name is refused BEFORE anything is
    written (exit 3, data.status='name-collision') - extraction would
    silently keep one copy, so the plan itself is the failure.  A folder the
    walk did not read is refused the same way (exit 3,
    data.status='unreadable-folders') - one that would not list, one that is a
    symbolic link, and a `roots:`-mapped asset root that is not on disk on an
    --include-assets run all count, because all three end with files missing
    from the zip and nothing else able to tell.  The plan is short of the
    archive, and only the plan knows it.  The cost is asymmetric and not
    close - a refused backup costs one re-run, a backup that quietly lacks a
    branch of the family costs the archive, and it costs it on the day the
    disk dies.
    `allow_incomplete=True` (the human's explicit `--allow-incomplete`) writes
    it anyway, marked in the zip's name and in a `BACKUP_INCOMPLETE.txt`
    member, for the case where the folder is never coming back and a partial
    backup beats none.  Any write or verify problem deletes the partial zip
    (the unlink is registered before the first write, so an interrupted run
    leaves nothing behind) and returns exit 3.  A stamp-write failure after a
    verified zip is reported as a warning message but stays exit 0: the thing
    the human asked for - a verified backup - exists; only doctor's memory of
    it is degraded, and doctor over-reminding is the safe direction.
    """
    archive_root = archive_root.resolve()
    wc_mode = is_working_copy(archive_root)

    if wc_mode and include_assets:
        msg = (
            'This is a working copy - it has no photo or document files, so '
            '--include-assets would produce an asset backup with no assets in it. '
            'Run `fha backup --include-assets` on your main archive; a records-only '
            '`fha backup` still works here.'
        )
        return Result(
            ok=True,
            exit_code=EXIT_CLEAN,
            data={'status': 'working-copy', 'zip_path': None, 'files': 0,
                  'bytes': 0, 'assets_included': False, 'skipped_roots': [],
                  'folders': {}, 'excluded': [], 'unreadable_dirs': [],
                  'complete': True},
        ).add('warning', msg)

    dest_dir, source_desc = _resolve_destination(archive_root, fha_config, to)
    if dest_dir is None:
        return Result(
            ok=False,
            exit_code=EXIT_FAILURE,
            data={'status': 'bad-destination', 'zip_path': None, 'files': 0,
                  'bytes': 0, 'assets_included': include_assets, 'skipped_roots': [],
                  'folders': {}, 'excluded': [], 'unreadable_dirs': [],
                  'complete': True},
        ).add('error', f'ERROR: {source_desc}')

    conflict = _destination_conflict(dest_dir, archive_root, fha_config, source_desc)
    if conflict:
        return Result(
            ok=False,
            exit_code=EXIT_FAILURE,
            data={'status': 'bad-destination', 'zip_path': None, 'files': 0,
                  'bytes': 0, 'assets_included': include_assets, 'skipped_roots': [],
                  'folders': {}, 'excluded': [], 'unreadable_dirs': [],
                  'complete': True},
        ).add('error', f'ERROR: {conflict}')

    plan = _plan_backup(archive_root, fha_config, include_assets)
    entries = plan['entries']
    shown_unreadable = [
        _display_dir(p, archive_root) for p in plan['unreadable_dirs']]

    collisions = _arcname_collisions(entries)
    if collisions:
        alias_roots = {alias: path for alias, path, _ext in plan['included_roots']}
        causes = []
        for top in sorted({arc.split('/', 1)[0] for arc in collisions}):
            if top in alias_roots:
                causes.append(
                    f"your archive has its own folder named '{top}/' AND fha.yaml "
                    f"maps a {top} root ({alias_roots[top]}, the `roots: {top}:` "
                    f"line) - with --include-assets both would unpack to '{top}/'"
                )
            else:
                causes.append(
                    f"two different files would both unpack into '{top}/'"
                )
        example = sorted(collisions)[0]
        return Result(
            ok=False,
            exit_code=EXIT_FAILURE,
            data={'status': 'name-collision', 'zip_path': None, 'files': 0,
                  'bytes': 0, 'assets_included': include_assets,
                  'skipped_roots': plan['skipped_roots'],
                  'folders': plan['folders'], 'excluded': plan['excluded'],
                  'unreadable_dirs': shown_unreadable, 'complete': not shown_unreadable},
        ).add('error', (
            f'ERROR: this backup was NOT written: {len(collisions)} file '
            f'name(s) would collide inside the zip (for example {example}), '
            f'and unzipping a zip with duplicate names silently keeps only '
            f'one copy - a backup tool never guesses which. '
            f'Cause: {"; ".join(causes)}. '
            f'Fix: rename that archive folder, or point the `roots:` line in '
            f'fha.yaml somewhere else, then re-run `fha backup`.'
        ))

    # A folder the walk did not read - it would not list, it is a shortcut, or
    # it is a whole asset root that is not there. Refused before anything is
    # written, and in --dry-run too, which must preview the run it would really
    # be. The message leads with what it means rather than with the fault,
    # because the human's question is never "what is errno 13", it is "do I
    # have a backup".
    causes = _unread_causes(plan, archive_root)
    if shown_unreadable and not allow_incomplete:
        listed = ', '.join(shown_unreadable[:5])
        if len(shown_unreadable) > 5:
            listed += f' and {len(shown_unreadable) - 5} more'
        return Result(
            ok=False,
            exit_code=EXIT_FAILURE,
            data={'status': 'unreadable-folders', 'zip_path': None, 'files': 0,
                  'bytes': 0, 'assets_included': include_assets,
                  'skipped_roots': plan['skipped_roots'],
                  'folders': plan['folders'], 'excluded': plan['excluded'],
                  'unreadable_dirs': shown_unreadable, 'complete': False},
        ).add('error', (
            f'ERROR: no backup was written. {len(shown_unreadable)} folder(s) '
            f'were not read, so anything filed in them would have been '
            f'missing from the zip without a word: {listed}. A backup you '
            f'cannot trust is worse than none - the day you need it is the day '
            f'you find out. '
            + ' '.join(causes) +
            f' If that folder is gone for good and you want the rest '
            f'backed up anyway, run `fha backup --allow-incomplete` - it '
            f'writes a zip named ...-INCOMPLETE.zip that says inside it what '
            f'is missing.'
        ))

    total_bytes = sum(size for _p, _arc, size in entries)
    zip_path = _zip_target(dest_dir, archive_root.name, bool(shown_unreadable))

    result = Result(data={
        'status': 'dry-run' if dry_run else 'ok',
        'zip_path': str(zip_path),
        'files': len(entries),
        'bytes': total_bytes,
        'assets_included': include_assets,
        'skipped_roots': plan['skipped_roots'],
        'folders': plan['folders'],
        'excluded': plan['excluded'],
        'unreadable_dirs': shown_unreadable,
        'complete': not shown_unreadable,
    })

    if shown_unreadable:
        listed = ', '.join(shown_unreadable[:5])
        if len(shown_unreadable) > 5:
            listed += f' and {len(shown_unreadable) - 5} more'
        result.add('warning', (
            f'INCOMPLETE BACKUP (you asked for one with --allow-incomplete): '
            f'{len(shown_unreadable)} folder(s) were not read, so '
            f'nothing filed in them is in this zip: {listed}. The zip is named '
            f'...-INCOMPLETE.zip and carries a {_NOTICE_NAME} note listing '
            f'those folders, so whoever unpacks it knows. '
            + ' '.join(causes) +
            f' When they can be read again, run `fha backup` for a complete one.'
        ))

    if dry_run:
        result.add('info', f'DRY RUN - nothing written. The backup would be: {zip_path}')
    if wc_mode:
        result.add('info', (
            'NOTE: this is a working copy - it has no photo/document files; '
            'your main archive is the copy that needs the real backup.'
        ))

    plan_lines = [f'{len(entries)} file(s), {_fmt_size(total_bytes)}, from {archive_root}:']
    for top in sorted(plan['folders']):
        bucket = plan['folders'][top]
        plan_lines.append(
            f'  {top:<24} {bucket["files"]} file(s), {_fmt_size(bucket["bytes"])}'
        )
    result.add('info', '\n'.join(plan_lines))

    if plan['excluded']:
        left_out = ['Left out (rebuildable or machine-local):']
        left_out.extend(f'  {name:<24} {reason}' for name, reason in plan['excluded'])
        result.add('info', '\n'.join(left_out))

    if plan['skipped_roots'] and not include_assets:
        roots_text = '; '.join(
            f'{alias} root: {path}, ~{_fmt_size(est)}'
            for alias, path, est in plan['skipped_roots']
        )
        result.add('info', (
            f'NOTE: your photos and documents are NOT in this backup '
            f'({roots_text}). Run `fha backup --include-assets` to include them, '
            f'or back those folders up separately - the `fha doctor` reminder '
            f'lists every path a full backup must cover.'
        ))
    if include_assets and plan['skipped_roots']:
        missing_aliases = {alias for alias, _p in plan.get('missing_roots') or []}
        for alias, path, _est in plan['skipped_roots']:
            if alias in missing_aliases:
                continue   # already refused, or named in the incomplete warning
            result.add('info', (
                f'NOTE: the {alias} root ({path}) is not reachable right now, so no '
                f'{alias} files were added. Run `fha doctor` to check your roots, '
                f'then re-run `fha backup --include-assets`.'
            ))
    # An asset root that DID open and held nothing.  No seam fired and none
    # could: an unmounted mount point still looks like an empty folder from
    # here.  So this is said plainly and the backup still counts as complete -
    # refusing every archive that has not filed its first photo would train the
    # human to reach for --allow-incomplete, which is the reflex this whole
    # feature is trying not to create.
    for alias, path in plan.get('empty_roots') or []:
        result.add('warning', (
            f'NOTE: your {alias} folder ({path}) opened but has no files in it '
            f'at all, so nothing from it is in this zip. An empty folder and a '
            f'drive that is not plugged in look exactly the same from here. If '
            f'your {alias} really do live somewhere else, connect that drive (or '
            f'fix the `roots: {alias}:` line in fha.yaml), then run '
            f'`fha backup --include-assets` again.'
        ))
    external_included = [(a, p) for a, p, ext in plan.get('included_roots', []) if ext]
    if external_included:
        names = ' and '.join(alias for alias, _p in external_included)
        result.add('info', (
            f'Your {names} files live outside the archive folder; in this zip they '
            f'sit inside it (under their own folder names). If you restore, either '
            f'move them back where they were and keep fha.yaml as-is, or leave them '
            f'inside the restored folder and delete the `roots:` mapping from fha.yaml.'
        ))

    if dry_run:
        result.add('info', _RESTORE_LINE)
        return result

    # Live run. Register the cleanup path before the first write: a write that
    # fails partway (disk full, permission, Ctrl-C) can still leave a partial
    # file.  The typed arm turns expected failures into a plain message +
    # exit 3; the BaseException arm keeps the no-partial-zip promise for
    # everything else (KeyboardInterrupt included) and lets it propagate.
    notice = None
    if shown_unreadable:
        # The notice member is named around any archive file that already
        # claims the name: overwriting a real file of the human's would be a
        # data loss committed by the very feature that exists to prevent one.
        taken = {arc for _p, arc, _s in entries}
        notice_name = _NOTICE_NAME
        n = 2
        while notice_name in taken:
            notice_name = f'{Path(_NOTICE_NAME).stem}_{n}.txt'
            n += 1
        notice = (notice_name,
                  _incomplete_notice(shown_unreadable, archive_root))

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        _write_zip(zip_path, entries, notice)
        verify_error = _verify_zip(zip_path)
        if verify_error:
            raise OSError(verify_error)
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        _discard_partial(zip_path)
        return Result(
            ok=False,
            exit_code=EXIT_FAILURE,
            data={'status': 'write-failed', 'zip_path': str(zip_path),
                  'files': 0, 'bytes': 0, 'assets_included': include_assets,
                  'skipped_roots': plan['skipped_roots'],
                  'folders': plan['folders'], 'excluded': plan['excluded'],
                  'unreadable_dirs': shown_unreadable, 'complete': not shown_unreadable},
        ).add('error', (
            f'ERROR: backup failed and the partial file was removed: {exc}. '
            f'Nothing to clean up - fix the cause and re-run `fha backup`.'
        ))
    except BaseException:
        # Ctrl-C or anything unforeseen: delete the partial zip, then let
        # the exception travel.  The promise is 'no partial zip survives an
        # interrupted run', not 'every failure becomes a Result'.
        _discard_partial(zip_path)
        raise

    zip_bytes = zip_path.stat().st_size
    result.data['bytes'] = zip_bytes
    result.note_changed(zip_path)
    # The success headline leads the report (built only after verification).
    result.messages.insert(0, Message(
        'info',
        f'backup verified: {len(entries)} file(s), {_fmt_size(zip_bytes)} -> {zip_path}',
    ))

    stamp = {
        'date': datetime.datetime.now().isoformat(timespec='seconds'),
        'zip': str(zip_path),
        'files': len(entries),
        'bytes': zip_bytes,
        'assets_included': include_assets,
        # `fha doctor` reads this stamp and is where a human checks whether he
        # is covered. "Last backup: 3 days ago" over an incomplete zip would
        # be the same false reassurance one layer further out, so the stamp
        # carries the caveat and doctor prints it.
        'complete': not shown_unreadable,
        'unreadable_dirs': shown_unreadable,
    }
    try:
        stamp_path = _write_stamp(archive_root, stamp)
        result.note_changed(stamp_path)
    except OSError as exc:
        result.add('warning', (
            f'The backup succeeded, but the reminder note could not be written '
            f'({exc}) - `fha doctor` will keep saying "no backup recorded". '
            f'Nothing else is affected.'
        ))

    result.add('info', _RESTORE_LINE)
    return result


# ── Interface ─────────────────────────────────────────────────────────────────

def _cmd_backup(result: Result) -> int:
    """Render a backup Result: errors to stderr, the plan and notes to stdout."""
    for msg in result.messages:
        text = msg.text
        if msg.next_step:
            text = f'{text} Next: {msg.next_step}'
        if msg.level == 'error':
            print(text, file=sys.stderr)
        else:
            print(text)
    return result.exit_code


# User-facing --help text (the module docstring stays developer-facing).
_CLI_DESCRIPTION = """\
Copy your whole archive into one dated zip file, kept OUTSIDE the archive.

  fha backup                      records-only zip in a folder beside the archive
  fha backup --include-assets     also pack the photos/documents roots
  fha backup --to D:/Backups      choose where the zip goes
  fha backup --dry-run            show the plan; write nothing

If any folder cannot be read - it will not open, it is a shortcut to somewhere
else, or (with --include-assets) your whole photos or documents folder is not
there - no zip is written at all: a backup missing part of your archive would
look exactly like a good one. Reconnect the drive or fix the folder and run it
again; --allow-incomplete writes it anyway, clearly marked, for a folder that
is gone for good.

Photos and documents are NOT included unless you pass --include-assets (they
are often huge and often live on another drive - the output names them every
time). To restore a backup: unzip it. That's the whole procedure."""


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register 'backup' onto the main fha parser."""
    p = subparsers.add_parser(
        'backup',
        help='Zip the archive into a dated backup beside it (restore = unzip).',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(p)
    p.set_defaults(func=_run_backup)


def _add_arguments(p: argparse.ArgumentParser) -> None:
    """Shared flag set for the subcommand and the standalone parser."""
    p.add_argument('--root', metavar='PATH', help='Archive root')
    p.add_argument('--to', metavar='PATH',
                   help='Folder to write the zip into (default: a folder named '
                        '{archive}-backups beside the archive; fha.yaml '
                        '`backup: path:` also sets this)')
    p.add_argument('--include-assets', action='store_true',
                   help='Also pack the photos/documents roots into the zip '
                        '(default: records only)')
    p.add_argument('--dry-run', action='store_true',
                   help='Print the full plan and write nothing')
    p.add_argument('--allow-incomplete', action='store_true',
                   help='Write the backup even if a folder could not be read '
                        '(the zip is named ...-INCOMPLETE.zip and lists what '
                        'is missing inside it)')


def _run_backup(args: argparse.Namespace) -> int:
    """CLI shim: resolve the root, load config strictly, run, render."""
    archive_root = resolve_root_arg(args, command='fha backup')
    if archive_root is None:
        return EXIT_FAILURE
    try:
        # Strict load: a malformed fha.yaml silently read as {} would resolve
        # the wrong asset roots and exclude (or include) the wrong things.
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return EXIT_FAILURE
    result = run_backup(
        archive_root, fha_config,
        to=args.to, include_assets=args.include_assets, dry_run=args.dry_run,
        allow_incomplete=getattr(args, 'allow_incomplete', False),
    )
    return _cmd_backup(result)


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha backup',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    args = parser.parse_args(argv)
    return _run_backup(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
