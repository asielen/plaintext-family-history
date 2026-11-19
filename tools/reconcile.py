#!/usr/bin/env python3
"""
reconcile.py - fha reconcile: heal record inventory paths after files move (TOOLING §9).

  fha reconcile [--root PATH] [--dry-run] [--with-exif]

The archive's law is that folder location is never truth: a documents-root
file carries its S-id in its filename, and the path stored in its source
record's `files:` inventory is a refreshable pointer, not an identity
(SPEC §12.1: "folders are projection; the record, not the path, is the
identity"). But until this tool existed, the pointer could only be refreshed
by hand: drag a filed document into a new subfolder and lint E011 flags the
record until someone edits its YAML. This tool is the one-command heal that
makes free reorganization real - rearrange the documents tree in any file
browser, run `fha reconcile`, and every moved file is re-tied to its record.

How it heals (TOOLING §9): for every source record `files:` entry under the
documents alias whose stored path no longer resolves on disk, the documents
root is searched for a file with the SAME NAME (filed documents are renamed
exactly once, at processing - a moved file keeps its `{slug}_{S-id}` name, so
the basename IS the identity match; this is the documents-side analog of the
photo side's embedded SOURCE: keyword re-match):

  - exactly one file with that name  -> the entry's path is rewritten to the
    new location (previewed under --dry-run, applied otherwise);
  - two or more files with that name -> ambiguous, reported for the human -
    the tool never guesses which one the record means;
  - none                             -> reported missing, with the next step.

A reverse pass reports on-disk files whose filename carries an S-id that no
record inventory lists (TOOLING §9 "log as new"), grouped by source, naming
`fha process --more` as the attach path.

Photos filed on source records (#289) heal from the records' side too, by
one rule (owner decision). A photo is never renamed by fha (SPEC §12.1/§13),
and `fha process` embeds its source's ID as a `SOURCE:` keyword. But a
keyword is not unique to one picture - a back scan carries its front's, and
photo managers copy it onto edits and exports - so a missing photos-alias
`files:` entry is re-tied automatically ONLY when:

  - exactly one unlisted file anywhere under the photos root has the entry's
    exact filename,
  - that file carries the record's own `SOURCE:` keyword and no other
    source's, and
  - no other entry of the same record has that filename (a front and back of
    one name in two folders can trade places).

Every other case - several same-named files, a keyword only under another
name, keywords not read yet, a same-named file carrying a different or an
extra source ID - is one plain report: the entry is missing, the candidates
found (most likely first), and the exact `file:` line to edit. Exit 1;
nothing else is ever written. "Unlisted" is judged by the file itself
(device and inode), not by how a record spells its path, and a heal onto a
file the record already lists is refused while planning, so --dry-run
agrees. Keywords come from the catalog (every `SOURCE:` keyword, where its
row still matches the file's mtime and size, and never after a `roots:` or
`photos_ignore:` change since the catalog was built) or, under --with-exif,
are read from the same-named candidates with exiftool. This works whether or not `fha photoindex` ran
after the move.

The photo catalog itself is `fha photoindex reconcile`'s machinery; when a
catalog exists this tool runs it too (pass-through of --dry-run/--with-exif),
so one command reconciles every file type - the §9 contract. Both passes
apply the one rule above through `_lib.photo_retie_target` and word their
candidates through `_lib.photo_retie_candidates` (#297), so a catalog row is
never moved onto a photo the record pass only reported. Importing
photoindex here follows report.py's orchestrator precedent (a tool whose whole
job is running other tools' engines does import them; ordinary tools still
never do).

A folder inside the documents root that will not open stops the documents
pass entirely (warning, exit 1, the folder named - the photo pass still runs).
`os.walk` and `rglob` both report an unlistable folder as an empty one, and
every conclusion this tool draws comes from one file map: with a hole in it, a
second copy of a name goes unseen and an ambiguous match becomes a confident
rewrite onto the wrong original, while documents that are merely out of sight
are reported gone. Healing nothing is recoverable; re-tying a record to the
wrong file is not obvious to anyone, ever.

Record writes are line-surgical: only the one `file:` line whose value is the
stale path changes, and the rewritten text must still carry the same number of
`file:` lines or the write is refused (file untouched, cause named) - the same
refuse-rather-than-corrupt posture the claims writers take. Working-copy mode
is a clean no-op: assets live on the main machine (SPEC §12.4), so there is
nothing here to reconcile against.

CODE MAP
--------
  _iter_source_records   - yield (path, meta) for every parseable sources/ record
  _display_dir           - a folder's name as the human filed it
  _disk_index            - basename -> [paths] map of every documents-root file
  _plan                  - compute healed/ambiguous/missing/unlisted, no writes
  _file_key              - a file's identity (device, inode), whatever the spelling
  _record_own_sid        - a record's own S-id (id:, else its filename)
  _stale_photo_entries   - photos-alias files: entries not on disk, per record
  _listed_file_keys      - identities of every listed file on disk
  _catalog_source_ids    - alias -> (mtime, size, every S-id) from photos.sqlite
  _read_source_keywords  - exiftool keyword read: every S-id per file, unread files
  _plan_photos           - the photo-record plan: heals under the one rule, reports
  _photo_report          - the one message for an entry that is not re-tied
  _split_file_value      - split a file: line into (unquoted path, comment)
  _rewrite_entry         - line-surgical file: path rewrite inside one record text
  _drop_duplicate_targets - planning step: refuse a re-tie onto a file the record
                           already lists (dry-run and real run alike)
  _apply                 - group heals per record, rewrite, refuse on any drift
  run_reconcile          - engine: both record plans + (unless dry-run) apply
                           + the photo catalog pass
  _cmd_reconcile         - CLI rendering of the Result
  register / _standalone_main - fha subcommand + python tools/reconcile.py entry
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from pathlib import Path, PurePosixPath

import yaml  # noqa: F401 - a missing PyYAML fails here, where fha.py explains it

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import (
    configure_utf8_stdout,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    FhaConfigError,
    Result,
    id_type_of,
    fmt_id_display,
    is_working_copy,
    load_fha_yaml,
    normalize_id,
    parse_filename,
    photoindex_config_drift,
    photo_retie_candidates,
    photo_retie_target,
    path_to_alias,
    read_record,
    read_text_exact,
    reapply_newline,
    resolve_path,
    resolve_root_arg,
    unreadable_dir_recorder,
    walk_files,
    write_text_exact_atomic,
    yaml_inline,
    safe_load_yaml,
)

import photoindex

configure_utf8_stdout()


def _record_filename_sid(rec_path: Path) -> str | None:
    """The S-id carried in a source record's `{slug}_{S-id}.md` filename, or None.

    The filename suffix carries identity even when the YAML body cannot be read,
    so a malformed record's S-id is still recoverable from its name - used to
    reserve that id during the reverse pass.
    """
    parsed = parse_filename(rec_path.name)
    if parsed and parsed.get('id_type') == 'S':
        return normalize_id(parsed['id_str'])
    return None


def _iter_source_records(archive_root: Path, result: Result,
                         malformed_sids: 'set[str] | None' = None):
    """Yield (record_path, meta) for every parseable record under sources/.

    An unparseable record gets one warning naming it and is skipped rather
    than failing the whole run - reconcile's job is healing paths, and a
    record lint already flags must not block healing every other record.

    read_record does NOT raise on malformed YAML: it reports the problem
    through its `parse_errors` field and hands back an empty `meta` (the
    same channel process.py and lint.py read). Yielding that empty meta
    would silently drop the record's real files: inventory - the record's
    documents would then be judged "unlisted" and the human told to attach
    files that are in fact already listed. So parse_errors is checked
    explicitly here and the record skipped BEFORE it can pollute the reverse
    inventory, exactly as if the read had raised.

    Skipping alone is NOT enough for the reverse pass, though: the skipped
    record's aliases never enter `listed_aliases`, so its on-disk file (which
    carries the same S-id in ITS name) would still be reported "unlisted" and
    the human told to attach a file the unreadable record already lists. So the
    skipped record's filename S-id is added to `malformed_sids`, and the reverse
    pass suppresses unlisted conclusions for those ids until the YAML is fixed.
    """
    sources_dir = archive_root / 'sources'
    if not sources_dir.is_dir():
        return
    for rec_path in sorted(sources_dir.rglob('*.md')):
        try:
            rec = read_record(rec_path)
        except Exception:
            if malformed_sids is not None:
                sid = _record_filename_sid(rec_path)
                if sid:
                    malformed_sids.add(sid)
            result.add('warning',
                       f'{rec_path.name} could not be parsed - skipped. '
                       'Run `fha lint` for the specifics.')
            continue
        if rec.get('parse_errors'):
            if malformed_sids is not None:
                sid = _record_filename_sid(rec_path)
                if sid:
                    malformed_sids.add(sid)
            detail = '; '.join(msg for _, msg in rec['parse_errors'])
            result.add('warning',
                       f'{rec_path.name} has malformed YAML ({detail}) - '
                       'skipped, so its files were not checked or healed. Fix '
                       'the record (`fha lint` names the spot), then re-run '
                       '`fha reconcile`.')
            continue
        yield rec_path, rec.get('meta') or {}


def _display_dir(path: Path, documents_root: Path) -> str:
    """Name a folder the way the human sees it in his file browser.

    Relative to the documents root when it is under it (`letters/1890s`), its
    own spelling otherwise - a message naming the wrong folder is worse than a
    long one.
    """
    try:
        return Path(path).relative_to(documents_root).as_posix()
    except ValueError:
        return str(path).replace('\\', '/')


def _disk_index(documents_root: Path, unreadable: list[Path]) -> dict[str, list[Path]]:
    """Map basename -> every file with that name under the documents root.

    One walk, reused for both the heal match (find a moved file by its name)
    and the reverse unlisted pass - the tree is walked exactly once per run.

    Walked with an error seam (`unreadable` collects the folders that would
    not list) because this map decides where a record's `files:` entry gets
    REWRITTEN. A folder that cannot be listed looks empty to `rglob`, and an
    empty-looking folder does three wrong things at once: the moved document
    inside it is reported gone; a second copy of a name hidden there turns an
    ambiguous match into a confident one, so the record is re-tied to the
    wrong file and the run reports a successful heal; and its filed documents
    come back as unlisted "attach me" orphans. `_plan` refuses on a non-empty
    list rather than heal from a partial picture.
    """
    by_name: dict[str, list[Path]] = {}
    for p in walk_files(documents_root, on_error=unreadable_dir_recorder(unreadable)):
        if p.is_file():
            by_name.setdefault(p.name, []).append(p)
    return by_name


def _plan(
    archive_root: Path, fha_config: dict, documents_root: Path,
    records: list, malformed_sids: 'set[str]',
) -> dict:
    """Compute the documents reconcile plan without writing anything.

    `records` and `malformed_sids` come from one `_iter_source_records` pass
    that the caller shares with the photo-record plan, so a malformed record
    is warned about once, not once per pass.

    Returns {'heals': {record_path: [(old_alias, new_alias), ...]},
    'ambiguous': [...], 'missing': [...], 'unlisted': {sid: [alias, ...]},
    'unreadable_dirs': [Path, ...]}.
    Only documents-alias entries are considered (the photos side has its own
    identity carrier and machinery); an entry whose `status:` is
    missing-fixture is a deliberate fixture state, never healed or flagged.

    A documents folder that would not list ends the documents pass right here
    with an empty plan and the folder named: every conclusion below - healed,
    ambiguous, missing, unlisted - is drawn from the file map, and drawing
    them from a map with a hole in it re-ties records to the wrong originals
    and tells the human that documents he still has are gone. The caller
    treats it exactly as it treats an unreachable documents root: warn, heal
    nothing, and carry on to the photo pass.
    """
    unreadable: list[Path] = []
    by_name = _disk_index(documents_root, unreadable)
    if unreadable:
        return {'heals': {}, 'ambiguous': [], 'missing': [], 'unlisted': {},
                'unreadable_dirs': unreadable}
    by_sid: dict[str, list[Path]] = {}
    for paths in by_name.values():
        for p in paths:
            parsed = parse_filename(p)
            if parsed and parsed.get('id_type') == 'S':
                by_sid.setdefault(normalize_id(parsed['id_str']), []).append(p)

    heals: dict[Path, list[tuple[str, str]]] = {}
    ambiguous: list[str] = []
    missing: list[str] = []

    # Pass 1 - collect every listed alias FIRST. The heal logic below
    # excludes already-listed files from S-id fallback matches and the
    # reverse pass keys off this set, so it must be complete before any
    # record is judged (a streaming build would let an early record match a
    # file a later record legitimately lists).
    listed_aliases: set[str] = set()
    for _rec_path, meta in records:
        for entry in (meta.get('files') or []):
            if isinstance(entry, dict) and entry.get('file'):
                listed_aliases.add(str(entry['file']).replace('\\', '/'))

    # Pass 2 - judge each stale entry. A planned heal's NEW alias joins
    # listed_aliases immediately: the reverse pass must treat the post-heal
    # state as the truth (else every successful heal would false-alarm as an
    # unlisted file), and later fallback matches must not grab a file an
    # earlier heal already claimed.
    for rec_path, meta in records:
        for entry in (meta.get('files') or []):
            if not isinstance(entry, dict):
                continue
            alias = str(entry.get('file', '') or '')
            if not alias:
                continue
            # Stored aliases are forward-slash by contract, but a record may
            # carry a Windows-style path (e.g. 'documents\\letters\\scan_S-….pdf').
            # On POSIX, Path('documents\\letters\\x.pdf') is ONE segment whose
            # .name is the whole backslash string, so an un-normalized basename
            # lookup or parse_filename would miss the moved file and report it
            # both missing AND unlisted. Normalize to POSIX separators once, up
            # front, and use it for every basename/S-id parse below. (resolve_path
            # already normalizes internally, so the prefix check and .exists()
            # resolve are safe on the raw alias.)
            alias_posix = alias.replace('\\', '/')
            basename = PurePosixPath(alias_posix).name
            if alias_posix.split('/', 1)[0] != 'documents':
                continue
            if str(entry.get('status', '')) == 'missing-fixture':
                continue
            if resolve_path(alias, fha_config, archive_root).exists():
                continue
            candidates = by_name.get(basename, [])
            # Exclude on-disk files another record already lists: healing onto one
            # would rewrite this entry to another source's original and point two
            # records at the same asset (reconcile would then report success).
            # Mirrors the embedded-ID fallback's listed_aliases filter below.
            unclaimed = [
                c for c in candidates
                if path_to_alias(c, 'documents', fha_config, archive_root)
                   .replace('\\', '/') not in listed_aliases
            ]
            if len(unclaimed) == 1:
                new_alias = path_to_alias(unclaimed[0], 'documents',
                                          fha_config, archive_root)
                heals.setdefault(rec_path, []).append((alias, new_alias))
                listed_aliases.add(new_alias.replace('\\', '/'))
                continue
            if len(unclaimed) >= 2:
                shown = ', '.join(
                    path_to_alias(c, 'documents', fha_config, archive_root)
                    for c in sorted(unclaimed))
                ambiguous.append(
                    f'{rec_path.name}: {basename!r} exists in more than one '
                    f'place ({shown}) - move or rename the extra copy, then re-run.')
                continue
            if candidates:
                # Every same-named file on disk is already listed by another
                # record - a stale or duplicated inventory entry, not a heal.
                # Report the conflict; never rewrite onto a claimed original.
                shown = ', '.join(
                    path_to_alias(c, 'documents', fha_config, archive_root)
                    for c in sorted(candidates))
                ambiguous.append(
                    f'{rec_path.name}: {basename!r} is already listed by another '
                    f'record ({shown}) - reconcile will not point two records at '
                    'the same file. Fix the duplicate inventory entry by hand, '
                    'then re-run.')
                continue
            # No file with that name anywhere. TOOLING §9's contract is
            # re-match by the embedded ID, so fall back to the S-id in the
            # stored filename: a file that was moved AND renamed (renaming a
            # filed original is forbidden, but reality drifts) still carries
            # its identity. Only a file no record lists may claim the match.
            parsed = parse_filename(alias_posix)
            sid_carriers: list[Path] = []
            if parsed and parsed.get('id_type') == 'S':
                sid_carriers = [
                    p for p in by_sid.get(normalize_id(parsed['id_str']), [])
                    if path_to_alias(p, 'documents', fha_config, archive_root)
                       .replace('\\', '/') not in listed_aliases
                ]
            if len(sid_carriers) == 1:
                new_alias = path_to_alias(sid_carriers[0], 'documents',
                                          fha_config, archive_root)
                heals.setdefault(rec_path, []).append((alias, new_alias))
                listed_aliases.add(new_alias.replace('\\', '/'))
            elif sid_carriers:
                shown = ', '.join(
                    path_to_alias(c, 'documents', fha_config, archive_root)
                    for c in sorted(sid_carriers))
                ambiguous.append(
                    f'{rec_path.name}: nothing is named {basename!r} any '
                    f'more, and more than one unlisted file carries its ID '
                    f'({shown}) - move or rename the extras, then re-run.')
            else:
                missing.append(
                    f'{rec_path.name}: {alias!r} is gone - no file with that name '
                    'or its ID anywhere in the documents folder. If it moved '
                    'outside the archive, bring it back; if it is truly gone, '
                    "note that in the record's ## Notes.")

    # Reverse pass (TOOLING §9 "log as new"): on-disk S-id files no record
    # lists - judged against the POST-heal alias set built above.
    unlisted: dict[str, list[str]] = {}
    for sid, paths in by_sid.items():
        if sid in malformed_sids:
            # A source record with this S-id EXISTS but could not be parsed
            # (already warned above). Its inventory is unreadable, so its own
            # on-disk files must NOT be advertised as unlisted "attach me"
            # orphans - that would tell the human to attach a file the
            # unreadable record already lists. Suppress until the YAML is fixed.
            continue
        for p in paths:
            alias = path_to_alias(p, 'documents', fha_config, archive_root)
            if alias.replace('\\', '/') not in listed_aliases:
                unlisted.setdefault(sid, []).append(alias)

    return {'heals': heals, 'ambiguous': ambiguous, 'missing': missing,
            'unlisted': unlisted, 'unreadable_dirs': []}


# -- Photos filed on source records (#289) -------------------------------------

# The exiftool tags this pass reads: the three keyword fields, nothing else.
# photoindex's full catalog read asks for captions, dates, places and face
# regions too; a keyword-only read keeps --with-exif cheap.
_KEYWORD_FIELDS = ['-Keywords', '-Subject', '-HierarchicalSubject']


def _file_key(path: Path):
    """The identity of an on-disk file, whatever spelling reached it.

    (device, inode) where the filesystem has inodes, else the case-normalized
    real path. Two spellings of one file - `photos/./a.jpg`, a different case
    on a case-insensitive disk, NFC against NFD on macOS - give one key, which
    is what "is this photo already listed?" has to compare. None for a path
    that does not exist.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    if st.st_ino:
        return ('inode', st.st_dev, st.st_ino)
    return ('path', os.path.normcase(os.path.realpath(path)))


def _record_own_sid(rec_path: Path, meta: dict) -> str:
    """The record's own S-id: its `id:`, else the S-id in its filename, else ''."""
    own = normalize_id(str(meta.get('id') or ''))
    if id_type_of(own) != 'S':
        own = _record_filename_sid(rec_path) or ''
    return own


def _stale_photo_entries(
    archive_root: Path, fha_config: dict, records: list,
) -> list[tuple[Path, dict, list[str]]]:
    """Every record's photos-alias `files:` entries whose path is not on disk.

    Returns [(record_path, meta, [stored alias, ...]), ...] for the records
    that have any. The stored spelling is kept, because it is what
    `_rewrite_entry` has to match. An entry marked `status: missing-fixture`
    is a deliberate fixture state and is left out, as the documents pass
    leaves it out.
    """
    stale: list[tuple[Path, dict, list[str]]] = []
    for rec_path, meta in records:
        gone: list[str] = []
        for entry in (meta.get('files') or []):
            if not isinstance(entry, dict):
                continue
            alias = str(entry.get('file', '') or '')
            if not alias or alias.replace('\\', '/').split('/', 1)[0] != 'photos':
                continue
            if str(entry.get('status', '')) == 'missing-fixture':
                continue
            if resolve_path(alias, fha_config, archive_root).exists():
                continue
            gone.append(alias)
        if gone:
            stale.append((rec_path, meta, gone))
    return stale


def _listed_file_keys(archive_root: Path, fha_config: dict, records: list) -> set:
    """The identity (`_file_key`) of every file any record lists that exists.

    "Unlisted" is judged against this set, so a record that spells a path
    oddly still owns its file.
    """
    keys = set()
    for _rec_path, meta in records:
        for entry in (meta.get('files') or []):
            if not isinstance(entry, dict) or not entry.get('file'):
                continue
            try:
                key = _file_key(resolve_path(str(entry['file']), fha_config, archive_root))
            except FhaConfigError:
                continue
            if key is not None:
                keys.add(key)
    return keys


def _source_ids_in(keywords) -> frozenset:
    """Every S-id a list of keywords names as `SOURCE: S-...`."""
    out = set()
    for kw in keywords or []:
        m = photoindex._SOURCE_KEYWORD_RE.match(str(kw).strip())
        if m:
            out.add(normalize_id(m.group(1)))
    return frozenset(out)


def _catalog_source_ids(archive_root: Path) -> dict:
    """alias -> (mtime, size, S-ids) for every live row of the photo catalog.

    Read-only (`mode=ro`), so a preview can never create or change the cache.
    The S-ids come from every `SOURCE:` keyword in `photo_keywords` - a file
    can carry two, and `photos.source_id` keeps only the first - with
    `source_id` as the fallback for a row with no keyword rows. Empty when
    there is no catalog or it cannot be read; the catalog pass reports that.
    """
    db_path = archive_root / '.cache' / 'photos.sqlite'
    if not db_path.is_file():
        return {}
    try:
        conn = sqlite3.connect(f'{db_path.resolve().as_uri()}?mode=ro', uri=True)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute('SELECT path, mtime, size, source_id FROM photos').fetchall()
        keywords: dict[str, list[str]] = {}
        for path, kw in conn.execute('SELECT path, keyword FROM photo_keywords'):
            keywords.setdefault(str(path), []).append(str(kw))
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    out = {}
    for path, mtime, size, sid in rows:
        if not path or photoindex._is_missing_key(str(path)):
            continue
        sids = _source_ids_in(keywords.get(str(path)))
        if not sids and sid:
            sids = frozenset({normalize_id(str(sid))})
        out[str(path)] = (mtime, size, sids)
    return out


def _read_source_keywords(paths: list[Path]) -> tuple[dict[Path, frozenset], list[Path]]:
    """Read every SOURCE: keyword of each file with exiftool.

    Returns ({path: S-ids}, [paths that could not be read]). A file exiftool
    returned no row for, or a row carrying its own `Error` field, is unread,
    never "carries no keyword". Reads only the keyword tags, through
    photoindex's `_run_exiftool` (the one exiftool seam), which raises
    RuntimeError when exiftool is missing or broken.
    """
    found: dict[Path, frozenset] = {}
    unread: list[Path] = []
    batch_size = photoindex._EXIFTOOL_BATCH_SIZE
    for start in range(0, len(paths), batch_size):
        batch = paths[start:start + batch_size]
        rows = {
            Path(row['SourceFile']).resolve(): row
            for row in photoindex._run_exiftool(batch, _KEYWORD_FIELDS)
            if row.get('SourceFile')
        }
        for p in batch:
            row = rows.get(p.resolve())
            if row is None or row.get('Error'):
                unread.append(p)
                continue
            found[p] = _source_ids_in(photoindex._extract_keywords(row))
    return found, unread


def _plan_photos(
    archive_root: Path, fha_config: dict, records: list, with_exif: bool,
) -> dict:
    """Plan the photo-record heals (#289) without writing anything.

    Returns {'heals': {record_path: [(old, new), ...]}, 'reports': [...],
    'notices': [(level, text), ...], 'stale': int}.

    The one rule (owner decision): a missing photos-alias entry is re-tied
    automatically ONLY when exactly one unlisted file under the photos root
    has the entry's exact filename, that file carries the record's own
    `SOURCE:` keyword and no other source's, and no other entry of the same
    record has that filename (a front and back of one name in two folders can
    trade places, and the front would then pass for the back). A keyword is
    not unique to one picture - a back scan carries its front's, and photo
    managers copy it onto edits and exports - so the name has to agree too.

    Every other case is one plain report: the entry is missing, here are the
    candidates found (most likely first), edit this `file:` line. Nothing
    else is ever written.

    Keywords come from the catalog where its row still matches the file's
    mtime and size (the scan's own test for "unchanged"), and never when
    fha.yaml has changed since the catalog was built; under --with-exif,
    the same-named candidates the catalog does not cover are read with
    exiftool. A folder under the photos root that will not list stops the
    pass: the file hiding there could be a second same-named copy. Walks the
    photos root only when some entry is missing.

    Raises RuntimeError for a malformed `photos_ignore:` (from the shared
    walker); the caller reports it once for both photo passes.
    """
    plan = {'heals': {}, 'reports': [], 'notices': [], 'stale': 0}
    stale = _stale_photo_entries(archive_root, fha_config, records)
    plan['stale'] = sum(len(aliases) for _rec, _meta, aliases in stale)
    if not stale:
        return plan

    photos_root = resolve_path('photos', fha_config, archive_root)
    if not photos_root.is_dir():
        plan['notices'].append((
            'warning',
            f'The photos folder is not reachable at {photos_root} - '
            f'{plan["stale"]} filed photo(s) no longer at their listed path '
            'were not looked for. If it lives on an external drive, plug it '
            'in; if the location changed, update roots: in fha.yaml.'))
        return plan

    unreadable: list = []
    on_disk = photoindex._on_disk_aliases(photos_root, fha_config, archive_root, unreadable)
    if unreadable:
        shown = ', '.join(sorted(alias for alias, _p in unreadable)[:5])
        plan['notices'].append((
            'warning',
            f'{len(unreadable)} folder(s) inside the photos folder could not be '
            f'opened, so no filed photo was re-tied: {shown}. A copy of a photo '
            'sitting in one of them could make a match that looks certain '
            'wrong. Reconnect the drive (or restore your access to the folder), '
            'then run `fha reconcile` again.'))
        return plan

    listed = _listed_file_keys(archive_root, fha_config, records)
    unlisted = {a: p for a, p in on_disk.items() if _file_key(p) not in listed}
    by_name: dict[str, list[str]] = {}
    for alias in unlisted:
        by_name.setdefault(PurePosixPath(alias).name, []).append(alias)

    # After a roots: (or photos_ignore:) change the catalog's rows describe
    # the old library; a file in the new one that matches an old row on
    # alias, mtime and size must not inherit its keyword. Every candidate is
    # then unread: --with-exif reads it, or the entry is reported.
    drifted = photoindex_config_drift(archive_root, fha_config)
    catalog = {} if drifted else _catalog_source_ids(archive_root)
    sids_of: dict[str, frozenset] = {}
    for alias, p in unlisted.items():
        row = catalog.get(alias)
        if row is None:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if row[0] == st.st_mtime and row[1] == st.st_size:
            sids_of[alias] = row[2]

    # Under --with-exif, read the same-named candidates the catalog does not
    # cover - the only files whose keywords can decide a heal.
    exif_note = ''
    if with_exif:
        wanted = sorted({a for _rec, _meta, aliases in stale for alias in aliases
                         for a in by_name.get(PurePosixPath(alias.replace('\\', '/')).name, [])
                         if a not in sids_of})
        if wanted:
            try:
                found, _unread = _read_source_keywords([unlisted[a] for a in wanted])
            except RuntimeError as e:
                if shutil.which('exiftool') is None:
                    exif_note = 'exiftool is not installed'
                else:
                    exif_note = str(e)
                plan['notices'].append((
                    'warning',
                    f'Could not read the keywords inside the photos ({exif_note}). '
                    'Run `fha doctor` to check exiftool, then run '
                    '`fha reconcile --with-exif` again.'))
            else:
                alias_of = {unlisted[a]: a for a in wanted}
                for p, sids in found.items():
                    sids_of[alias_of[p]] = sids

    carriers_by_sid: dict[str, list[str]] = {}
    for alias, sids in sids_of.items():
        for sid in sids:
            carriers_by_sid.setdefault(sid, []).append(alias)

    claims: dict[str, list[tuple[Path, str]]] = {}
    for rec_path, meta, aliases in stale:
        own = _record_own_sid(rec_path, meta)
        names = [PurePosixPath(str(e.get('file', '')).replace('\\', '/')).name
                 for e in (meta.get('files') or []) if isinstance(e, dict)]
        for alias in aliases:
            name = PurePosixPath(alias.replace('\\', '/')).name
            same = by_name.get(name, [])
            if photo_retie_target(same, own, sids_of) and names.count(name) == 1:
                claims.setdefault(same[0], []).append((rec_path, alias))
                continue
            plan['reports'].append(_photo_report(
                rec_path, alias, own, same, sids_of,
                carriers_by_sid.get(own, []) if own else [], with_exif))
    for cand, claimants in sorted(claims.items()):
        if len(claimants) == 1:
            rec_path, alias = claimants[0]
            plan['heals'].setdefault(rec_path, []).append((alias, cand))
            continue
        # Two records with the same id: neither gets the file.
        for rec_path, alias in claimants:
            plan['reports'].append(_photo_report(
                rec_path, alias, '', [cand], sids_of, [], with_exif))
    return plan


def _photo_report(
    rec_path: Path, alias: str, own: str, same: list[str],
    sids_of: dict, carriers: list[str], with_exif: bool,
) -> str:
    """The one message for a missing photo entry that is not re-tied.

    Names the entry, the candidates found (`_lib.photo_retie_candidates`,
    the wording the catalog pass shares) and the exact `file:` line to edit.
    A candidate whose keywords were not read says so and names
    `--with-exif`, the flag that reads them.
    """
    found = photo_retie_candidates(
        own, same, sids_of, carriers,
        unread_hint='' if with_exif else '`fha reconcile --with-exif` reads it')
    return (f'{rec_path.name}: {alias!r} is missing from disk and was not re-tied. '
            f'{found}Edit the `file: {alias}` line in {rec_path.name} to the '
            "photo's new path under photos/, then run `fha lint`.")


def _split_file_value(raw: str) -> tuple[str, str]:
    """Split a `file:` line's value region into (path, trailing_comment).

    `raw` is everything on the line after the `file:` key. The stored path may
    be a bare scalar or a YAML-quoted scalar, optionally trailed by a ` #...`
    comment. The scan tracks quote state so a `#` INSIDE quotes stays part of
    the path: a valid alias like `documents/Box #3/letter.pdf` is only legal
    when quoted, and the earlier `value.split('#')[0]` truncated it at the
    first hash - the same class of bug this whole fix targets, just on the
    read side. `path` is the unquoted value (via the real YAML loader, so
    escapes resolve correctly); `trailing_comment` is the untouched comment
    region (its `#...`), preserved so a hand-written note survives the rewrite.
    """
    s = raw.strip()
    if not s:
        return '', ''
    if s[0] in ('"', "'"):
        quote = s[0]
        i = 1
        while i < len(s):
            c = s[i]
            if quote == '"' and c == '\\':
                i += 2                      # backslash escape in a double quote
                continue
            if c == quote:
                if quote == "'" and i + 1 < len(s) and s[i + 1] == "'":
                    i += 2                  # '' is an escaped single quote
                    continue
                i += 1
                break
            i += 1
        value_repr, trailing = s[:i], s[i:]
    else:
        # Bare scalar: a YAML comment must be whitespace-preceded, so a `#`
        # touching non-space text is data, not a comment.
        cut = len(s)
        for j in range(1, len(s)):
            if s[j] == '#' and s[j - 1] in (' ', '\t'):
                cut = j
                break
        value_repr, trailing = s[:cut].rstrip(), s[cut:]
    try:
        path = safe_load_yaml(value_repr)
    except Exception:
        path = value_repr
    if not isinstance(path, str):
        path = value_repr
    comment = trailing.strip()
    return path, comment


def _rewrite_entry(text: str, old_alias: str, new_alias: str) -> tuple[str, bool]:
    """Rewrite ONE `file:` line's stale path in a record's text, surgically.

    Matches a line whose `file:` VALUE equals the stored path exactly (after
    unquoting and dropping any trailing comment) - never substring containment,
    which would let a stale 'x.pdf' rewrite grab a valid 'x.pdf.txt' sidecar
    entry and corrupt it while reporting success. The healed value is re-emitted
    through `yaml_inline`, the one shared scalar-quoting rule every surgical
    writer uses, so a new path carrying YAML-significant characters (a ` #`
    comment marker, a leading `-`, a `: `) is quoted and reads back whole. A
    raw string splice, by contrast, wrote `documents/Box #3/letter.pdf`
    UNQUOTED, and the next parse silently truncated it at ` #` to
    `documents/Box`, detaching the source from its document while reporting the
    heal as a success. The `- ` list marker, indentation, and any trailing
    comment survive untouched; only the value token changes. Returns
    (new_text, changed).
    """
    lines = text.split('\n')
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('- file:'):
            key = '- file:'
        elif stripped.startswith('file:'):
            key = 'file:'
        else:
            continue
        value, comment = _split_file_value(stripped[len(key):])
        if value == old_alias:
            indent = line[:len(line) - len(line.lstrip())]
            new_line = f'{indent}{key} {yaml_inline(new_alias)}'
            if comment:
                new_line = f'{new_line}  {comment}'
            lines[i] = new_line
            return '\n'.join(lines), True
    return text, False


def _apply(archive_root: Path, heals: dict[Path, list[tuple[str, str]]],
           result: Result) -> int:
    """Apply the planned rewrites, one record at a time, refusing on drift.

    A heal onto a file the record already lists never reaches here: the
    planning step (`_drop_duplicate_targets`) refuses it for --dry-run and the
    real run alike.

    The refuse-rather-than-corrupt guard: the rewritten text must contain the
    same number of `file:` lines as before, and every planned old path must
    actually have been found. Any mismatch leaves that record untouched and
    reports it BY NAME - a half-healed record is worse than a stale one, and
    a silent skip is worse than either, because the human stops looking.

    Every record write here (both the heal and the round-trip restore) goes
    through write_text_exact_atomic, never a truncating write: a moved-file
    heal must never be able to leave the source record - often the only copy
    of that ancestor - half-written when the disk fills or the process dies
    mid-write. The atomic writer only ever leaves the old bytes or the new
    bytes on disk, so a raised OSError means the original still stands and the
    'nothing healed' message it triggers is always literally true.

    Returns the number of entries actually rewritten (the caller reports this
    as the healed count, never the planned count).
    """
    def _file_line_count(text: str) -> int:
        return sum(1 for ln in text.split('\n')
                   if ln.strip().startswith(('file:', '- file:')))

    applied = 0
    for rec_path, pairs in sorted(heals.items()):
        try:
            before = read_text_exact(rec_path)
        except OSError as e:
            result.add('warning', f'cannot read {rec_path.name}: {e} - skipped.')
            continue
        after = before
        ok = True
        for old_alias, new_alias in pairs:
            after, changed = _rewrite_entry(after, old_alias, new_alias)
            if not changed:
                result.add('warning',
                           f'{rec_path.name}: could not find the line for '
                           f'{old_alias!r} - record left untouched. Fix it by '
                           'hand or run `fha lint` for the shape.')
                ok = False
                break
        if not ok:
            continue
        if _file_line_count(after) != _file_line_count(before):
            result.add('warning',
                       f'{rec_path.name}: the rewrite would have changed the '
                       'shape of its files: list - record left untouched. Fix '
                       'the entry by hand (`fha lint` names the spot).')
            continue
        try:
            # Atomic sibling-temp replacement, never a truncating write: the
            # record is often the SOLE copy of that ancestor, so a write that
            # dies mid-stream (disk full, interrupted) must leave the original
            # bytes intact, not a half-written file. write_text_exact_atomic
            # raises only when the target was never touched, so the `- skipped`
            # message below is always true - the record still reads as `before`.
            write_text_exact_atomic(rec_path, reapply_newline(after, before))
        except OSError as e:
            result.add('warning',
                       f'{rec_path.name}: could not be written ({e}) - left as it '
                       'was, nothing healed. Free up disk space or fix the '
                       'permissions, then re-run `fha reconcile`.')
            continue
        # Round-trip guard: re-read the record we just wrote and confirm every
        # healed path parses back to exactly the alias we intended. A value
        # that needed quoting but slipped through unquoted (e.g. a folder named
        # `Box #3`) would report exit 0 and count as healed while YAML had
        # actually truncated it at ` #` - a silent source-to-document detach.
        # Verifying against the real parser is the only honest proof the write
        # says what it means. On any mismatch the file is restored to its
        # pre-heal bytes (never left half-written) and reported by name.
        reparsed = read_record(rec_path)
        healed_aliases = {
            str(e.get('file', '')).replace('\\', '/')
            for e in (reparsed.get('meta', {}).get('files') or [])
            if isinstance(e, dict)
        }
        intended = {new.replace('\\', '/') for _old, new in pairs}
        if reparsed.get('parse_errors') or not intended <= healed_aliases:
            try:
                # Restore is atomic too: recovering from a bad round-trip must
                # not itself be able to truncate the record. A failed restore
                # here leaves the just-written (but wrong) text in place, which
                # the error below tells the human to fix from backup or git.
                write_text_exact_atomic(rec_path, before)
            except OSError as e:
                result.add('error',
                           f'{rec_path.name}: a heal did not read back correctly '
                           f'AND restoring the original failed ({e}) - the record '
                           'may be half-written. Restore it from backup or git, '
                           'then run `fha lint`.')
                continue
            result.add('warning',
                       f'{rec_path.name}: the re-tied path did not read back '
                       'correctly (likely a folder or filename containing a '
                       "'#' or ':') - record left as it was, nothing healed. "
                       'Fix the entry by hand, then run `fha lint`.')
            continue
        result.note_changed(rec_path)
        applied += len(pairs)
        for old_alias, new_alias in pairs:
            result.add('info', f'Re-tied {old_alias} -> {new_alias} ({rec_path.name})')
    return applied


def _drop_duplicate_targets(
    archive_root: Path, fha_config: dict, heals: dict, result: Result,
) -> dict:
    """The planned heals minus every record whose re-tie targets a file that
    record already lists under another entry (however it is spelled).

    Shared planning step for both passes, run before the dry-run preview and
    before `_apply`, so the preview and the real run refuse the same thing
    with the same warning and exit code. Re-tying a stale entry onto a file
    the record already lists would list one file twice and hide that the
    stale entry's own file is still lost.
    """
    kept = {}
    for rec_path, pairs in heals.items():
        duplicate = _already_listed_target(archive_root, fha_config, rec_path, pairs)
        if duplicate is None:
            kept[rec_path] = pairs
            continue
        new_alias, spelled = duplicate
        result.add('warning',
                   f'{rec_path.name}: {new_alias} is already listed in this '
                   f'record (as {spelled}) - re-tying another entry to it would '
                   'list one file twice. Record left untouched; fix the entry by '
                   'hand, then run `fha lint`.')
    return kept


def _already_listed_target(
    archive_root: Path, fha_config: dict, rec_path: Path, pairs: list,
) -> 'tuple[str, str] | None':
    """(new alias, existing spelling) when a heal target is a file the record
    already lists under another entry, else None. Compared by file identity
    (`_file_key`), so `photos/./a.jpg` and `photos/a.jpg` are one file."""
    try:
        meta = read_record(rec_path).get('meta') or {}
    except Exception:
        return None
    olds = {old.replace('\\', '/') for old, _new in pairs}
    existing: dict = {}
    for entry in (meta.get('files') or []):
        if not isinstance(entry, dict) or not entry.get('file'):
            continue
        spelled = str(entry['file'])
        if spelled.replace('\\', '/') in olds:
            continue
        try:
            key = _file_key(resolve_path(spelled, fha_config, archive_root))
        except FhaConfigError:
            continue
        if key is not None:
            existing.setdefault(key, spelled)
    for _old, new in pairs:
        try:
            key = _file_key(resolve_path(new, fha_config, archive_root))
        except FhaConfigError:
            continue
        if key is not None and key in existing:
            return new, existing[key]
    return None


def _finalize(result: Result) -> Result:
    """Set the exit code from the collected messages (Result does not derive
    it): any error is 3, anything left needing a human is 1, else 0."""
    if any(m.level == 'error' for m in result.messages):
        result.ok = False
        result.exit_code = EXIT_FAILURE
    elif any(m.level == 'warning' for m in result.messages):
        result.exit_code = EXIT_WARNINGS
    return result


def run_reconcile(
    archive_root: Path, fha_config: dict, *,
    dry_run: bool = False, with_exif: bool = False,
) -> Result:
    """Engine: heal moved documents and filed photos on their records, then
    the photo catalog.

    Returns a Result whose data carries {'status', 'healed', 'ambiguous',
    'missing', 'unlisted'} counts for the documents pass and
    {'photos_healed', 'photos_reported'} for the photo-record pass (#289).
    Exit code follows the messages: clean run or clean heal is 0; anything
    left needing a human (ambiguous names, genuinely missing files, unlisted
    S-id files, a filed photo reported rather than re-tied) is a warning,
    exit 1. Working-copy mode no-ops cleanly - the assets
    live on the main machine.

    Both record plans are computed before anything is written, and the
    photo-record plan reads the catalog before the catalog pass moves any of
    its rows, so a --dry-run preview names exactly the rewrites a real run
    makes.
    """
    result = Result(data={'status': 'ok', 'healed': 0, 'ambiguous': 0,
                          'missing': 0, 'unlisted': 0, 'photos_healed': 0,
                          'photos_reported': 0})

    if is_working_copy(archive_root):
        result.data['status'] = 'working-copy'
        result.add('info',
                   'This is a working copy - the actual files live on the main '
                   'machine, so there is nothing to reconcile here. Run '
                   '`fha reconcile` on the main archive.')
        return _finalize(result)

    malformed_sids: set[str] = set()
    records = list(_iter_source_records(archive_root, result, malformed_sids))
    records_healed = 0
    photo_records_healed = 0

    try:
        photo_plan = _plan_photos(archive_root, fha_config, records, with_exif)
    except RuntimeError as e:
        # A malformed photos_ignore: (the shared walker refuses it) stops
        # both photo passes, which walk the same root by the same rule; say it
        # once, as a plain error, beside whatever the documents pass found.
        photo_plan = None
        result.add('error', f'photos: {e}')

    documents_root = resolve_path('documents', fha_config, archive_root)
    if not documents_root.is_dir():
        # An unplugged external drive must not read as "everything vanished" -
        # the same posture photoindex reconcile takes for its root. This is a
        # warning, NOT an early return: the photo pass below is independent (its
        # own root and catalog), and TOOLING §9's one-command contract means an
        # offline documents drive must not also silence the photo reconciliation.
        result.add('warning',
                   f'The documents folder is not reachable at {documents_root} - '
                   'documents not checked. If it lives on an external drive, plug '
                   'it in; if the location changed, update roots: in fha.yaml. '
                   '(The photo pass below still ran.)')
    else:
        plan = _plan(archive_root, fha_config, documents_root, records, malformed_sids)
        if plan['unreadable_dirs']:
            # Same posture as the unreachable-documents-root arm above: say so,
            # heal nothing, and let the photo pass run. Healing from a file map
            # with a hole in it would re-tie a record to the wrong original and
            # call documents "gone" that are sitting in the folder that would
            # not open - both reported as a successful run.
            shown_dirs = ', '.join(sorted(
                _display_dir(d, documents_root) for d in plan['unreadable_dirs'])[:5])
            result.add('warning',
                       f'{len(plan["unreadable_dirs"])} folder(s) inside the '
                       f'documents folder could not be opened, so no document '
                       f'was re-tied and nothing here was reported missing: '
                       f'{shown_dirs}. Anything filed in them is invisible to this '
                       f'check, and re-tying records without seeing it could '
                       f'point a record at the wrong file. Reconnect the drive '
                       f'(or restore your access to the folder), then run '
                       f'`fha reconcile` again. (The photo pass below still ran.)')
        else:
            heals = _drop_duplicate_targets(archive_root, fha_config, plan['heals'], result)
            ambiguous = plan['ambiguous']
            missing, unlisted = plan['missing'], plan['unlisted']
            heal_count = sum(len(v) for v in heals.values())
            result.data.update({'healed': heal_count, 'ambiguous': len(ambiguous),
                                'missing': len(missing), 'unlisted': len(unlisted)})

            if dry_run:
                for rec_path, pairs in sorted(heals.items()):
                    for old_alias, new_alias in pairs:
                        result.add('info',
                                   f'[dry-run] Would re-tie {old_alias} -> {new_alias} '
                                   f'({rec_path.name})')
            else:
                # Report what actually landed, not what was planned - a refused
                # rewrite must not inflate the healed count.
                result.data['healed'] = _apply(archive_root, heals, result)
                records_healed += result.data['healed']

            for line in ambiguous:
                result.add('warning', line)
            for line in missing:
                result.add('warning', line)
            for sid, aliases in sorted(unlisted.items()):
                shown = ', '.join(sorted(aliases))
                result.add('warning',
                           f'{shown} carries {fmt_id_display(sid)} but that record does not list it - '
                           'attach it with `fha process <primary-file> --more FILE role`, '
                           'or add a files: entry to the record.')

            if not (heal_count or ambiguous or missing or unlisted):
                result.add('info', 'Documents all tied to their records - nothing to heal.')

    # Photos filed on source records (#289). Planned in full before any of
    # its rewrites land, from the catalog as it stood before the catalog pass
    # below moves rows, so the preview and the real run agree.
    if photo_plan is not None:
        photo_heals = _drop_duplicate_targets(
            archive_root, fha_config, photo_plan['heals'], result)
        photo_heal_count = sum(len(v) for v in photo_heals.values())
        result.data.update({
            'photos_healed': photo_heal_count,
            'photos_reported': len(photo_plan['reports']),
        })
        for level, text in photo_plan['notices']:
            result.add(level, text)
        if dry_run:
            for rec_path, pairs in sorted(photo_heals.items()):
                for old_alias, new_alias in pairs:
                    result.add('info',
                               f'[dry-run] Would re-tie {old_alias} -> {new_alias} '
                               f'({rec_path.name})')
        else:
            photo_records_healed = _apply(archive_root, photo_heals, result)
            result.data['photos_healed'] = photo_records_healed
            records_healed += photo_records_healed
        for line in photo_plan['reports']:
            result.add('warning', line)
        if not (photo_plan['stale'] or photo_plan['notices']):
            # Says only what was checked: every listed photo path still opens.
            result.add('info', 'No filed photo is missing from disk - nothing to re-tie.')

    if records_healed:
        # Next-step order follows what each command reads: `fha index` and
        # `fha photoindex` read the records just rewritten; lint reads disk.
        refresh = ('`fha index` and `fha photoindex`' if photo_records_healed
                   else '`fha index`')
        result.add('info',
                   f'Run {refresh} so searches see the new locations, and '
                   '`fha lint` to confirm everything is tied down.')

    # Photo catalog side (TOOLING §9: one command reconciles every file type).
    # Only when a catalog exists - an archive that never built one should not
    # fail. Skipped after a malformed photos_ignore:, already reported above
    # (the catalog pass would only refuse it a second time).
    if photo_plan is None:
        return _finalize(result)
    if (archive_root / '.cache' / 'photos.sqlite').exists():
        try:
            photo_result = photoindex.run_reconcile(
                archive_root, fha_config, with_exif=with_exif, dry_run=dry_run)
        except RuntimeError as e:
            # A malformed photos_ignore: (or any other refusal photoindex
            # raises for its own config) must land as a plain error line in
            # this front door's summary, beside the documents-side results -
            # never as a traceback that hides them.
            result.add('error', f'photos: {e}')
            return _finalize(result)
        for msg in photo_result.messages:
            result.add(msg.level, f'photos: {msg.text}',
                       next_step=getattr(msg, 'next_step', None))
        for p in photo_result.changed:
            result.note_changed(Path(p))
        # The photoindex engine reports through its data dict (its own CLI
        # renders it); summarize here so this front door is never silent
        # about work it did or found - and never mistakes a failure for a
        # clean catalog.
        d = photo_result.data or {}
        photo_status = str(d.get('status') or '')
        waiting = d.get('content_waiting') or {}
        if waiting.get('unread'):
            # Matching by picture is gated until every photo has been read
            # (#220): say why no photo was matched that way.
            result.add('info', 'photos: ' + photoindex._content_waiting_message(waiting))
        for text in d.get('proposals') or []:
            # A catalog row the one rule would not re-tie, while a photo
            # with its name or keyword exists (#297): the same report the
            # record pass gives, never a silent move onto that photo.
            result.add('warning', f'photos: {text}')
        if d.get('content_unchecked'):
            # Some new photos could not be read, so photoindex took no
            # content match at all this run (#220 audit F2) - say so.
            result.add('warning',
                       f"photos: {len(d['content_unchecked'])} new photo(s) could not be "
                       'read just now, so none was matched by its content this run. '
                       'Close any program using them, then run `fha reconcile` again.')
        if photo_status in ('absent', 'unreadable', 'old-schema'):
            # The catalog exists on disk but photoindex refused it (corrupt,
            # or a schema too old to upgrade in place - a schema-1 catalog
            # IS upgraded by photoindex reconcile itself, #220 audit F9).
            # Keyed off status, not ok alone - the unreachable-root case
            # below is also ok=False but is a warning.
            state = 'out of date' if photo_status == 'old-schema' else photo_status
            result.add('error',
                       f'photos: the photo catalog (.cache/photos.sqlite) is '
                       f'{state} - photo paths not checked. Rebuild it '
                       'with `fha photoindex`, then re-run `fha reconcile`.')
        elif not photo_result.messages:
            if not d.get('root_found', True):
                result.add('warning',
                           'photos: the photos folder is not reachable - photo '
                           'paths not checked. Plug in the drive or fix roots: '
                           'in fha.yaml.')
            else:
                rematched = len(d.get('rematched') or [])
                photo_missing = len(d.get('missing') or [])
                new_count = int(d.get('new_count') or 0)
                if rematched or photo_missing or new_count:
                    level = 'warning' if (photo_missing or new_count) else 'info'
                    verb = 'would be re-tied' if dry_run else 're-tied'
                    prefix = '[dry-run] ' if dry_run else ''
                    # Say what each number counts. "0 re-tied, N new" was read
                    # as "no photos are filed yet" (#36) - but these are CATALOG
                    # rows, and a filed photo is a source record's `files:`
                    # entry, an entirely separate question this line does not
                    # answer. Not saying so once led to narrowing roots: on
                    # the strength of this line and orphaning every filed photo.
                    result.add(level,
                               f'photos: {prefix}{rematched} catalog row(s) {verb}, '
                               f'{photo_missing} catalog row(s) missing on disk, '
                               f'{new_count} on-disk file(s) not yet catalogued - run '
                               '`fha photoindex reconcile` for the per-file detail. '
                               '(These count the photo CATALOG, not which photos are '
                               'filed on source records - `fha lint` E011 covers that.)')
                else:
                    result.add('info',
                               'photos: catalog matches the photo folder - nothing to heal.')
    else:
        result.add('info',
                   'No photo catalog (.cache/photos.sqlite) yet, so there was no '
                   'catalog to reconcile (filed photos above were still checked). '
                   'Run `fha photoindex` to build one.')

    return _finalize(result)


# -- CLI ----------------------------------------------------------------------

_CLI_DESCRIPTION = (
    'Re-tie moved files to their records. Rearrange the documents folder any '
    'way you like, then run this: every filed document is found again by the '
    'ID in its name and its record updated. A filed photo moved within the '
    'photos folder is found again by its name and the SOURCE: keyword inside '
    'it (from the photo catalog, or read with --with-exif), and the photo catalog is '
    'reconciled too when one exists. Preview with --dry-run first.'
)


def _cmd_reconcile(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    # Load fha.yaml STRICTLY: reconcile's whole job is comparing the documents/
    # photos roots against what records list, and those roots may live outside
    # the archive via `roots:` mappings. A permissive load turns a malformed
    # fha.yaml into {}, which silently discards those mappings - reconcile would
    # then scan the empty internal `documents/` skeleton and report every real
    # file "missing" (or heal against unrelated internal files). Failing loudly
    # here is the only safe posture; the message names the fix.
    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        print('Fix fha.yaml (or run `fha doctor` for a check), then re-run '
              '`fha reconcile`.', file=sys.stderr)
        return EXIT_FAILURE
    result = run_reconcile(
        archive_root, fha_config,
        dry_run=bool(getattr(args, 'dry_run', False)),
        with_exif=bool(getattr(args, 'with_exif', False)),
    )
    for msg in result.messages:
        stream = sys.stderr if msg.level == 'error' else sys.stdout
        prefix = 'ERROR: ' if msg.level == 'error' else ''
        print(f'{prefix}{msg.text}', file=stream)
    return result.exit_code


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        'reconcile',
        help='Re-tie moved documents (and photos) to their records',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root')
    p.add_argument('--dry-run', action='store_true', dest='dry_run',
                   help='Preview every re-tie without writing anything')
    p.add_argument('--with-exif', action='store_true', dest='with_exif',
                   help='Photos: read the SOURCE: keyword inside photos the '
                        'catalog does not cover, to find moved photos (needs '
                        'exiftool; see fha photoindex reconcile)')
    p.set_defaults(func=_cmd_reconcile)


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha reconcile',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--root', metavar='PATH', help='Archive root')
    parser.add_argument('--dry-run', action='store_true', dest='dry_run',
                        help='Preview every re-tie without writing anything')
    parser.add_argument('--with-exif', action='store_true', dest='with_exif',
                        help='Photos: read the SOURCE: keyword inside photos the '
                             'catalog does not cover, to find moved photos '
                             '(needs exiftool)')
    args = parser.parse_args(argv)
    return _cmd_reconcile(args)


if __name__ == '__main__':
    raise SystemExit(_standalone_main())
