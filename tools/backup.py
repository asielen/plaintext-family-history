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

Safety posture: the archive tree is only ever read; the one in-tree mutation
is the `.cache/` stamp.  The destination must resolve OUTSIDE the archive root
and every mapped asset root (a zip inside the tree would be swept into the
next backup, or into an asset scan).  After writing, every member's CRC is
verified (`ZipFile.testzip`); on any write or verify failure the partial zip
is deleted and the run exits 3 - a backup that might be corrupt and says
nothing is worse than no backup.  Dry-run is byte-for-byte side-effect-free
(the destination folder is not even created).

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
malformed fha.yaml, or a write/verify failure (partial zip deleted).  There
is no exit-1 arm: a partial or suspect backup is never a warning, it is a
failure.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
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
)

configure_utf8_stdout()

# ── CODE MAP ──────────────────────────────────────────────────────────────────
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
#  Execution
#    _write_zip               - write entries into the zip (test seam)
#    _verify_zip              - CRC-check every member via testzip (test seam)
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


def _norm(path: Path | str) -> str:
    """Normalize a path for containment comparison (resolved + case-folded).

    fha.yaml root values may differ from the on-disk casing on Windows, and a
    symlinked destination must not be able to smuggle itself inside the tree,
    so every comparison resolves symlinks first and then case-folds.
    """
    return os.path.normcase(str(Path(path).resolve()))


def _inside(child: Path | str, parent: Path | str) -> bool:
    """True when `child` is `parent` or lies anywhere under it (post-resolve).

    The trailing separator is stripped before comparing so a parent at a
    filesystem root ('D:\\', '/') still matches - 'd:\\' + os.sep would be a
    double separator no child path ever starts with.
    """
    c = _norm(child).rstrip(os.sep)
    p = _norm(parent).rstrip(os.sep)
    return c == p or c.startswith(p + os.sep)


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
        if _inside(dest, folder):
            return (
                f'backup destination {dest} is inside {label} ({folder}) - '
                f'backups must live outside the archive so they survive it '
                f'(that path came from {source_desc}). '
                f'Try `--to <a folder outside the archive>` or set '
                f'`backup: path:` in fha.yaml.'
            )
    return None


def _zip_target(dest_dir: Path, root_name: str) -> Path:
    """Return today's zip path, uniquified so a re-run never overwrites.

    `{root-name}-backup_{YYYY-MM-DD}.zip`; a same-day second run appends
    `_2`, `_3`, … - an existing backup is never destroyed by making another.
    """
    stamp = datetime.date.today().isoformat()
    candidate = dest_dir / f'{root_name}-backup_{stamp}.zip'
    n = 2
    while candidate.exists():
        candidate = dest_dir / f'{root_name}-backup_{stamp}_{n}.zip'
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
    excluded_dir_norms: frozenset[str] = frozenset(),
    excluded_top_dirs: frozenset[str] = frozenset(),
    excluded_top_files: frozenset[str] = frozenset(),
) -> list[tuple[Path, str, int]]:
    """Walk `base` and return sorted (abs_path, arcname, size) entries.

    Arcnames are posix-form relative paths (the plan's Windows-long-path watch
    item), prefixed with `arc_prefix` when zipping an asset root (an external
    root uses its alias, an internal one its real in-archive relative path).  Directory pruning happens against resolved+case-folded paths
    so an exclusion from fha.yaml matches regardless of stored casing.  A file
    whose size cannot be read is kept with size 0 rather than dropped - if it
    is truly unreadable the zip write fails loudly later, which beats a backup
    that silently omitted it.  Empty directories are not recorded: they carry
    no data, and the spec's skeleton ships `.gitkeep` placeholders.
    """
    entries: list[tuple[Path, str, int]] = []
    if not base.is_dir():
        return entries
    for dirpath, dirnames, filenames in os.walk(base):
        d = Path(dirpath)
        at_top = (d == base)
        keep = []
        for name in sorted(dirnames):
            if at_top and name in excluded_top_dirs:
                continue
            if excluded_dir_norms and _norm(d / name) in excluded_dir_norms:
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

    Sizes are computed here, at plan time, so the dry-run and the assets note
    both print real numbers.  Asset roots are walked in sorted-alias order and
    entries are deduplicated by absolute path, so a root nested inside another
    (SPEC §12.4's `inbox: C:/Photos/_inbox` example) is zipped once, under the
    first alias that reaches it.
    """
    asset_roots = _asset_roots(archive_root, fha_config)
    internal_asset_norms = frozenset(
        _norm(p) for p in asset_roots.values() if _inside(p, archive_root)
    )

    entries = _walk_files(
        archive_root,
        arc_prefix='',
        excluded_dir_norms=internal_asset_norms,
        excluded_top_dirs=frozenset(_EXCLUDED_DIRS),
        excluded_top_files=frozenset(_EXCLUDED_FILES),
    )

    skipped_roots: list[tuple[str, str, int]] = []
    included_roots: list[tuple[str, str, bool]] = []
    if include_assets:
        seen = {_norm(p) for p, _arc, _s in entries}
        for alias, root in asset_roots.items():
            if not root.is_dir():
                skipped_roots.append((alias, str(root), 0))
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
            for p, arc, size in _walk_files(root, arc_prefix=prefix):
                key = _norm(p)
                if key in seen:
                    continue
                seen.add(key)
                entries.append((p, arc, size))
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


# ── Execution ─────────────────────────────────────────────────────────────────

def _write_zip(zip_path: Path, entries: list[tuple[Path, str, int]]) -> None:
    """Write every planned entry into the zip (deflated, posix arcnames).

    Kept as its own function so the failure-injection tests can seam it; any
    exception propagates to run_backup, which deletes the partial file.
    """
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
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
) -> Result:
    """Compute the backup plan and (unless dry-run) write + verify the zip.

    The engine half of the TOOLING §1 split: returns a Result, prints nothing.
    Result.data carries {'status': 'ok'|'dry-run'|'working-copy'|
    'bad-destination'|'name-collision'|'write-failed', 'zip_path', 'files',
    'bytes', 'assets_included', 'skipped_roots', 'folders', 'excluded'};
    `changed` lists the zip and the stamp on a live run, nothing on dry-run.
    `bytes` is the finished zip's on-disk size (the number a human compares
    against free disk space); the per-folder plan sizes are content bytes.

    Failure posture: a duplicate in-zip name is refused BEFORE anything is
    written (exit 3, data.status='name-collision') - extraction would
    silently keep one copy, so the plan itself is the failure.  Any write or
    verify problem deletes the partial zip (the unlink is registered before
    the first write, so an interrupted run leaves nothing behind) and
    returns exit 3.  A stamp-write failure after a
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
                  'bytes': 0, 'assets_included': False, 'skipped_roots': []},
        ).add('warning', msg)

    dest_dir, source_desc = _resolve_destination(archive_root, fha_config, to)
    if dest_dir is None:
        return Result(
            ok=False,
            exit_code=EXIT_FAILURE,
            data={'status': 'bad-destination', 'zip_path': None, 'files': 0,
                  'bytes': 0, 'assets_included': include_assets, 'skipped_roots': []},
        ).add('error', f'ERROR: {source_desc}')

    conflict = _destination_conflict(dest_dir, archive_root, fha_config, source_desc)
    if conflict:
        return Result(
            ok=False,
            exit_code=EXIT_FAILURE,
            data={'status': 'bad-destination', 'zip_path': None, 'files': 0,
                  'bytes': 0, 'assets_included': include_assets, 'skipped_roots': []},
        ).add('error', f'ERROR: {conflict}')

    plan = _plan_backup(archive_root, fha_config, include_assets)
    entries = plan['entries']

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
                  'folders': plan['folders'], 'excluded': plan['excluded']},
        ).add('error', (
            f'ERROR: this backup was NOT written: {len(collisions)} file '
            f'name(s) would collide inside the zip (for example {example}), '
            f'and unzipping a zip with duplicate names silently keeps only '
            f'one copy - a backup tool never guesses which. '
            f'Cause: {"; ".join(causes)}. '
            f'Fix: rename that archive folder, or point the `roots:` line in '
            f'fha.yaml somewhere else, then re-run `fha backup`.'
        ))

    total_bytes = sum(size for _p, _arc, size in entries)
    zip_path = _zip_target(dest_dir, archive_root.name)

    result = Result(data={
        'status': 'dry-run' if dry_run else 'ok',
        'zip_path': str(zip_path),
        'files': len(entries),
        'bytes': total_bytes,
        'assets_included': include_assets,
        'skipped_roots': plan['skipped_roots'],
        'folders': plan['folders'],
        'excluded': plan['excluded'],
    })

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
        for alias, path, _est in plan['skipped_roots']:
            result.add('info', (
                f'NOTE: the {alias} root ({path}) is not reachable right now, so no '
                f'{alias} files were added. Run `fha doctor` to check your roots, '
                f'then re-run `fha backup --include-assets`.'
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
    # fails partway (disk full, permission) can still leave a partial file.
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        _write_zip(zip_path, entries)
        verify_error = _verify_zip(zip_path)
        if verify_error:
            raise OSError(verify_error)
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        try:
            zip_path.unlink(missing_ok=True)
        except OSError:
            pass
        return Result(
            ok=False,
            exit_code=EXIT_FAILURE,
            data={'status': 'write-failed', 'zip_path': str(zip_path),
                  'files': 0, 'bytes': 0, 'assets_included': include_assets,
                  'skipped_roots': plan['skipped_roots']},
        ).add('error', (
            f'ERROR: backup failed and the partial file was removed: {exc}. '
            f'Nothing to clean up - fix the cause and re-run `fha backup`.'
        ))

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
