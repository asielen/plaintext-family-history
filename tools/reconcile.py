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

Photos filed on source records (#289) heal from the records' side too. A photo
is never renamed by fha (SPEC §12.1/§13), so its name is not its identity; the
`SOURCE:` keyword `fha process` embedded in it is. But a keyword is not unique
to one picture: a back scan carries the front's keyword, and a photo manager
copies it onto every edit and export it makes. So the keyword only narrows the
search, and the name decides. For every photos-alias `files:` entry whose path
no longer resolves, the photos root is searched for files carrying one of the
record's S-ids (its `id:`, plus S-ids in `aliases:` that are no other live
record's `id:`) as that keyword. Every keyword a file carries counts: from the
catalog's `photo_keywords` rows, trusted only for a file whose stored mtime and
size still match, or read with exiftool under --with-exif. Then:

  - EXACTLY ONE carrier with the entry's own name, listed by no record
                                                     -> heal (the only
    automatic re-tie there is - owner policy);
  - two or more such carriers (the one keeping most of the old folder path
    is named first, but an export can keep it too), a candidate that carries
    a second source's ID, one candidate wanted by two entries, or a photo of
    the same name inside a folder `photos_ignore:` skips (its names are
    listed, never its keywords read - the original may be sitting there)
                                                     -> ambiguous, reported;
  - no carrier with that name, but carriers under other names
                                                     -> a PROPOSAL naming
    them and the exact `file:` line to edit, never applied (owner policy:
    a keyword-only match is never written automatically);
  - a carrier any record already lists               -> never taken;
  - nothing else carries it                          -> reported missing.

"Listed" is decided by the file itself (device and inode), not by how a
record spells the path, so `photos/./a.jpg` or a case or Unicode-form drift
cannot make a listed photo look free. The writer re-checks the same thing:
`_apply` refuses a heal whose target the record already lists.

A photo whose keyword this run could not learn (not catalogued, changed since
the scan, or unreadable) might be another carrier, so a same-named one blocks
the heal and the entry is reported with the command that would let reconcile
look (`--with-exif`), or, with no exiftool, the exact `file:` line to edit. A
folder that will not list, or a symlinked folder the walk does not follow,
stops the pass with nothing healed and nothing called gone. The pass also
checks the other direction: a listed photo still on disk whose keywords are
known, lack the record's S-id, while an unlisted photo carries it - the old
path now holds a different picture - is reported with the `file:` line, never
healed; its message names a step that clears it (the exact exiftool line
that writes the keyword into the listed photo, or moving the copy out of the
photos folder). None of this depends on the catalog's history: it works
whether or not `fha photoindex` ran after the move.

The photo catalog itself is `fha photoindex reconcile`'s machinery; when a
catalog exists this tool runs it too (pass-through of --dry-run/--with-exif),
so one command reconciles every file type - the §9 contract. Importing
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
  _record_source_ids     - the S-ids a record answers to, and alias clashes
  _stale_photo_entries   - photos-alias files: entries not on disk, per record
  _listed_files          - file identity -> records listing it; present photo entries
  _read_catalog          - per-alias S-id sets + stored fingerprints from photos.sqlite
  _read_source_keywords  - exiftool keyword read: every S-id per file, unread files
  _learn_keywords        - which S-ids each given photo carries, and which are unknown
  _tail_match            - how many path components two aliases share at the end
                           (orders a list of candidates; never picks one)
  _ignored_photo_names   - name -> [alias] of photos inside photos_ignore: folders
  _plan_photos           - the photo-record plan: heals, ambiguous, proposals,
                           missing, unverified, reused paths
  _split_file_value      - split a file: line into (unquoted path, comment)
  _rewrite_entry         - line-surgical file: path rewrite inside one record text
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
    is_catalogued_photo_name,
    is_working_copy,
    load_fha_yaml,
    normalize_id,
    parse_filename,
    path_to_alias,
    read_record,
    read_text_exact,
    reapply_newline,
    resolve_path,
    resolve_root_arg,
    shell_quote,
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
# regions too; a keyword-only read is what keeps a --with-exif run over a
# large library from costing a full scan.
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


def _record_source_ids(
    rec_path: Path, meta: dict, live_ids: 'dict[str, Path]',
) -> tuple[set[str], list[str]]:
    """The S-ids a source record answers to, and the alias S-ids refused.

    Its `id:` (or, with none, the S-id in its filename), plus every S-id in
    `aliases:` - a merged source keeps its retired id there, and photos filed
    under that id still carry it. An alias that is some OTHER live record's
    `id:` is refused and returned in the second list: honouring it would let
    this record take that record's photos, which is how one source would
    quietly steal another's picture.
    """
    own = normalize_id(str(meta.get('id') or ''))
    if id_type_of(own) != 'S':
        own = _record_filename_sid(rec_path) or ''
    sids = {own} if own else set()
    raw = meta.get('aliases')
    aliases = [raw] if isinstance(raw, str) else (raw if isinstance(raw, list) else [])
    clashes: list[str] = []
    for value in aliases:
        sid = normalize_id(str(value)) if value is not None else ''
        if id_type_of(sid) != 'S' or sid == own:
            continue
        owner = live_ids.get(sid)
        if owner is not None and owner != rec_path:
            clashes.append(sid)
            continue
        sids.add(sid)
    return sids, clashes


def _stale_photo_entries(
    archive_root: Path, fha_config: dict, records: list,
) -> list[tuple[Path, dict, list[dict]]]:
    """Every record's photos-alias `files:` entries whose path is not on disk.

    Returns [(record_path, meta, [entry, ...]), ...] for the records that have
    any; each entry is the record's own dict, so its stored spelling (what
    `_rewrite_entry` must match), `role:` and `copy:` travel together. An
    entry marked `status: missing-fixture` is a deliberate fixture state and
    is left out, as the documents pass leaves it out.
    """
    stale: list[tuple[Path, dict, list[dict]]] = []
    for rec_path, meta in records:
        gone: list[dict] = []
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
            gone.append(entry)
        if gone:
            stale.append((rec_path, meta, gone))
    return stale


def _listed_files(archive_root: Path, fha_config: dict, records: list) -> tuple[dict, list]:
    """Who lists which file, by the file's identity; and the photo entries on disk.

    Returns ({file key: {record_path, ...}}, [(record_path, meta, alias,
    absolute Path), ...]). The first answers "is this candidate listed?" for
    every spelling a record might use. The second is every photos-alias
    entry whose file is present - the input of the reused-path check.
    """
    listed: dict = {}
    present: list = []
    for rec_path, meta in records:
        for entry in (meta.get('files') or []):
            if not isinstance(entry, dict) or not entry.get('file'):
                continue
            alias = str(entry['file'])
            try:
                path = resolve_path(alias, fha_config, archive_root)
            except FhaConfigError:
                continue
            key = _file_key(path)
            if key is None:
                continue
            listed.setdefault(key, set()).add(rec_path)
            if (alias.replace('\\', '/').split('/', 1)[0] == 'photos'
                    and path.is_file() and is_catalogued_photo_name(path.name)):
                present.append((rec_path, meta, alias, path))
    return listed, present


def _source_ids_in(keywords) -> frozenset:
    """Every S-id a list of keywords names as `SOURCE: S-…`."""
    out = set()
    for kw in keywords or []:
        m = photoindex._SOURCE_KEYWORD_RE.match(str(kw).strip())
        if m:
            out.add(normalize_id(m.group(1)))
    return frozenset(out)


def _read_catalog(archive_root: Path) -> dict:
    """What the photo catalog knows: {'rows': {alias: (mtime, size, S-ids)},
    'fingerprints': {alias: fingerprint}}.

    Read-only (`mode=ro`), so a preview can never create or change the cache.
    S-ids come from every `SOURCE:` keyword in `photo_keywords` - a file can
    carry two, and `photos.source_id` keeps only the first - with
    `source_id` as a fallback for a row with no keyword rows. Fingerprints are
    kept for every row, `MISSING:` ones under the path they had, so a
    proposal can say whether a candidate is the picture last seen at the old
    path. Empty when there is no catalog or it cannot be read; the catalog
    pass reports that itself.
    """
    empty = {'rows': {}, 'fingerprints': {}}
    db_path = archive_root / '.cache' / 'photos.sqlite'
    if not db_path.is_file():
        return empty
    try:
        conn = sqlite3.connect(f'{db_path.resolve().as_uri()}?mode=ro', uri=True)
    except sqlite3.Error:
        return empty
    try:
        try:
            rows = conn.execute(
                'SELECT path, mtime, size, source_id, fingerprint FROM photos').fetchall()
        except sqlite3.Error:
            rows = [(*r, None) for r in conn.execute(
                'SELECT path, mtime, size, source_id FROM photos').fetchall()]
        keywords: dict[str, list[str]] = {}
        for path, kw in conn.execute('SELECT path, keyword FROM photo_keywords'):
            keywords.setdefault(str(path), []).append(str(kw))
    except sqlite3.Error:
        return empty
    finally:
        conn.close()
    out = {'rows': {}, 'fingerprints': {}}
    for path, mtime, size, sid, fingerprint in rows:
        if not path:
            continue
        path = str(path)
        if fingerprint:
            out['fingerprints'][photoindex._live_alias(path)] = fingerprint
        if photoindex._is_missing_key(path):
            continue
        sids = _source_ids_in(keywords.get(path))
        if not sids and sid:
            sids = frozenset({normalize_id(str(sid))})
        out['rows'][path] = (mtime, size, sids)
    return out


def _read_source_keywords(
    paths: list[Path], progress=None,
) -> tuple[dict[Path, frozenset], list[Path]]:
    """Read every SOURCE: keyword of each file with exiftool.

    Returns ({path: S-ids}, [paths that could not be read]). A file exiftool
    returned no row for, or a row carrying its own `Error` field, is unread,
    never "carries no keyword": an unread file might be another copy of the
    photo. Reads only the keyword tags (`_KEYWORD_FIELDS`), through
    photoindex's `_run_exiftool`, the one exiftool seam, which raises
    RuntimeError when exiftool is missing or broken. `progress(done, total)`
    is called after each batch of a read big enough to take a while.
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
        if progress is not None and len(paths) > batch_size:
            progress(min(start + batch_size, len(paths)), len(paths))
    return found, unread


def _learn_keywords(
    catalog: dict, files: dict[str, Path], with_exif: bool, progress=None,
    already: 'dict[str, frozenset] | None' = None,
) -> dict:
    """Which S-ids each of `files` (alias -> Path) carries.

    `already` holds what this run has read before, so no file is read twice.
    The catalog answers for a file whose row still matches its mtime and
    size - the incremental scan's own test for "unchanged". Under --with-exif
    the rest is read with exiftool. Returns {'known': {alias: S-ids},
    'unknown': [alias, ...], 'exif_error': str or None}.
    """
    known: dict[str, frozenset] = {}
    todo: list[str] = []
    for alias, p in files.items():
        if already and alias in already:
            known[alias] = already[alias]
            continue
        row = catalog['rows'].get(alias)
        if row is not None:
            try:
                st = p.stat()
            except OSError:
                todo.append(alias)
                continue
            if row[0] == st.st_mtime and row[1] == st.st_size:
                known[alias] = row[2]
                continue
        todo.append(alias)
    exif_error = None
    if with_exif and todo:
        try:
            found, unread_paths = _read_source_keywords([files[a] for a in todo], progress)
        except RuntimeError as e:
            exif_error = str(e)
        else:
            alias_of = {files[a]: a for a in todo}
            for p, sids in found.items():
                known[alias_of[p]] = sids
            todo = [alias_of[p] for p in unread_paths]
    return {'known': known, 'unknown': sorted(todo), 'exif_error': exif_error}


def _tail_match(a: str, b: str) -> int:
    """How many path components alias `a` and alias `b` share at the end.

    `photos/front/0001.jpg` against `photos/2020/front/0001.jpg` shares two,
    against `photos/2020/back/0001.jpg` one. It only ORDERS the candidates a
    message lists and names the likeliest: an export folder can keep more of
    the old path than the original does, so a longer tail is never evidence
    enough to re-tie on (owner policy - one same-named carrier or no heal).
    """
    pa = a.replace('\\', '/').split('/')
    pb = b.replace('\\', '/').split('/')
    n = 0
    while n < min(len(pa), len(pb)) and pa[-1 - n] == pb[-1 - n]:
        n += 1
    return n


def _ignored_photo_names(photos_root: Path, ignored: list) -> dict[str, list[str]]:
    """name -> [alias, ...] for every photo inside what `photos_ignore:` skips.

    `ignored` is the walker's own list of what it pruned (folders) or skipped
    (files). Only names are listed here - no keyword is read, so even a huge
    excluded export costs one directory walk, and only when an entry is
    stale. An ignored folder is a hole in the search exactly like one that
    will not open: the original may be sitting in it while a same-named copy
    elsewhere looks like the only match.
    """
    out: dict[str, list[str]] = {}

    def note(p: Path) -> None:
        if not is_catalogued_photo_name(p.name):
            return
        try:
            alias = f'photos/{p.relative_to(photos_root).as_posix()}'
        except ValueError:
            alias = p.name
        out.setdefault(p.name, []).append(alias)

    for item in ignored:
        if item.is_dir() and not item.is_symlink():
            for dirpath, _dirs, files in os.walk(item):
                for f in files:
                    note(Path(dirpath) / f)
        elif item.is_file():
            note(item)
    return {name: sorted(aliases) for name, aliases in out.items()}


def _plan_photos(
    archive_root: Path, fha_config: dict, records: list, with_exif: bool,
    progress=None,
) -> dict:
    """Plan the photo-record heals (#289) without writing anything.

    Returns {'heals': {record_path: [(old, new), ...]}, 'ambiguous': [...],
    'proposed': [...], 'missing': [...], 'unverified': [...], 'reused': [...],
    'notices': [(level, text), ...], 'stale': int}: `notices` are run-wide
    lines, the lists are one line per entry. The rules are in the module
    docstring; the order they run in matters:

      1. Same-name matches for EVERY record, before anything else, so a
         record judged early cannot take a file a later record's own entry
         matches by name; a candidate wanted by two entries is refused for
         both.
      2. Keyword-only matches, afterwards and only as proposals.
      3. The reused-path check, against the carriers nothing took.

    Walks the photos root only when some entry is stale or some listed photo
    looks wrong (keywords known and lacking its record's S-id), so a clean
    run costs one stat per filed photo plus catalog lookups.

    Raises RuntimeError for a malformed `photos_ignore:` (from the shared
    walker); the caller reports it once for both photo passes.
    """
    plan = {'heals': {}, 'ambiguous': [], 'proposed': [], 'missing': [],
            'unverified': [], 'reused': [], 'notices': [], 'stale': 0}
    stale = _stale_photo_entries(archive_root, fha_config, records)
    plan['stale'] = sum(len(entries) for _rec, _meta, entries in stale)
    listed, present = _listed_files(archive_root, fha_config, records)

    live_ids: dict[str, Path] = {}
    for rec_path, meta in records:
        own = normalize_id(str(meta.get('id') or ''))
        if id_type_of(own) == 'S':
            live_ids.setdefault(own, rec_path)
    sids_of_record: dict[Path, set[str]] = {}
    clash_reported: set[Path] = set()

    def record_sids(rec_path, meta):
        if rec_path not in sids_of_record:
            sids, clashes = _record_source_ids(rec_path, meta, live_ids)
            sids_of_record[rec_path] = sids
            if clashes and rec_path not in clash_reported:
                clash_reported.add(rec_path)
                shown = ', '.join(c.upper() for c in sorted(clashes))
                owners = ', '.join(sorted(live_ids[c].name for c in clashes))
                plan['notices'].append((
                    'warning',
                    f'{rec_path.name}: its aliases: list {shown}, which is the '
                    f'id: of {owners} - ignored when looking for this record\'s '
                    'photos, so it cannot take that record\'s pictures. If it '
                    'was added by mistake, remove it from aliases:, then run '
                    '`fha lint`.'))
        return sids_of_record[rec_path]

    catalog = _read_catalog(archive_root)

    # Listed photos still on disk: learn their keywords now (the catalog
    # usually knows them), to spot an old path that a different picture took.
    present_files: dict[str, Path] = {}
    for _rec, _meta, _alias, path in present:
        try:
            present_files[path_to_alias(path, 'photos', fha_config, archive_root)] = path
        except OSError:
            continue
    present_kw = _learn_keywords(catalog, present_files, with_exif, progress)
    suspects: list[tuple[Path, dict, str, set[str], frozenset]] = []
    for rec_path, meta, alias, path in present:
        try:
            key_alias = path_to_alias(path, 'photos', fha_config, archive_root)
        except OSError:
            continue
        carried = present_kw['known'].get(key_alias)
        sids = record_sids(rec_path, meta)
        if carried is not None and sids and not (carried & sids):
            suspects.append((rec_path, meta, alias, sids, carried))

    if not (stale or suspects):
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
    linked: list = []
    ignored: list = []
    on_disk = photoindex._on_disk_aliases(
        photos_root, fha_config, archive_root, unreadable, linked_dirs=linked,
        ignored_paths=ignored)
    blind = sorted({alias for alias, _p in unreadable} | {alias for alias, _p in linked})
    if blind:
        plan['notices'].append((
            'warning',
            f'{len(blind)} folder(s) inside the photos folder could not be '
            f'looked inside (unreadable, or a shortcut/symbolic link to another '
            f'place): {", ".join(blind[:5])}. A copy of a photo sitting in one '
            'of them could make a match that looks certain wrong, so no filed '
            'photo was re-tied and none was called gone. Reconnect the drive or '
            'restore your access to the folder; a linked folder should be moved '
            'into the photos folder for real. Then run `fha reconcile` again.'))
        return plan

    ignored_by_name = _ignored_photo_names(photos_root, ignored) if stale else {}
    keywords = _learn_keywords(catalog, on_disk, with_exif, progress,
                               already=present_kw['known'])
    known, unknown = keywords['known'], keywords['unknown']
    exif_error = keywords['exif_error'] or present_kw['exif_error']
    if exif_error:
        if shutil.which('exiftool') is None:
            cause, fix = 'exiftool is not installed', 'Install exiftool'
        else:
            cause, fix = exif_error, 'Run `fha doctor` to check exiftool'
        plan['notices'].append((
            'warning',
            f'Could not read the keywords inside the photos ({cause}), so only '
            'photos already in an up-to-date photo catalog could be matched. '
            f'{fix}, then run `fha reconcile --with-exif` again.'))

    carriers_by_sid: dict[str, list[str]] = {}
    for alias, sids in known.items():
        for sid in sids:
            carriers_by_sid.setdefault(sid, []).append(alias)
    unknown_by_name: dict[str, list[str]] = {}
    for alias in unknown:
        unknown_by_name.setdefault(PurePosixPath(alias).name, []).append(alias)

    keys: dict[str, object] = {}
    taken: set[str] = set()

    def listers(alias):
        if alias not in keys:
            keys[alias] = _file_key(on_disk[alias])
        return listed.get(keys[alias], set())

    def free(aliases):
        return sorted(a for a in aliases if not listers(a) and a not in taken)

    def carriers(sids):
        return sorted({a for sid in sids for a in carriers_by_sid.get(sid, [])})

    def label(sids):
        return ', '.join(sorted(s.upper() for s in sids)) or 'its source ID'

    def edit_hint(rec_path, alias):
        return (f'Edit the `file: {alias}` line in {rec_path.name} to the right '
                'one, then run `fha lint`.')

    def why_unknown(rec_path, alias, same_named):
        if exif_error or (not with_exif and shutil.which('exiftool') is None):
            return (f'{rec_path.name}: {alias!r} is not on disk, and the keywords '
                    'that would say where it went could not be read. Edit the '
                    f'`file: {alias}` line in {rec_path.name} by hand to the '
                    "photo's new path under photos/, or, once exiftool works "
                    '(`fha doctor` checks it), run `fha reconcile --with-exif`.')
        if with_exif:
            shown = ', '.join((same_named or unknown)[:5])
            return (f'{rec_path.name}: {alias!r} is not on disk, and '
                    f'{len(same_named or unknown)} photo(s) that may be it could '
                    f'not be read just now ({shown}). Close any program using '
                    'them, then run `fha reconcile --with-exif` again.')
        if same_named:
            what = (f'{", ".join(same_named)} has the same name, but its keywords '
                    'have not been read')
        else:
            what = (f'{len(unknown)} photo(s) are not in the photo catalog or '
                    'changed since it was built, and one of them may be it')
        return (f'{rec_path.name}: {alias!r} is not on disk, and {what}. Run '
                '`fha reconcile --with-exif` so it can read the SOURCE: keyword '
                'inside each photo.')

    ignore_note = ''
    if photoindex._photos_ignore_patterns(fha_config):
        ignore_note = (' (folders photos_ignore: skips were not searched - if you '
                       'moved it into one, move it back out or edit the record by hand)')

    # 1. Same-name matches, across every record first.
    claims: dict[str, list[tuple[Path, str]]] = {}
    unresolved: dict[Path, tuple[dict, list[str]]] = {}
    for rec_path, meta, entries in stale:
        sids = record_sids(rec_path, meta)
        for entry in entries:
            alias = str(entry['file'])
            name = PurePosixPath(alias.replace('\\', '/')).name
            if not is_catalogued_photo_name(name):
                plan['missing'].append(
                    f'{rec_path.name}: {alias!r} is not on disk, and it is not a '
                    'photo, so there is no keyword to find it by. If you moved it '
                    f'within the photos folder, edit the `file: {alias}` line in '
                    f'{rec_path.name} to its new path by hand.')
                continue
            hidden = ignored_by_name.get(name, [])
            if hidden:
                folders = sorted({str(PurePosixPath(h).parent) for h in hidden})
                elsewhere = free(carriers(sids))
                seen = (f'; outside it, {", ".join(elsewhere[:5])} carr'
                        f'{"ies" if len(elsewhere) == 1 else "y"} {label(sids)}'
                        if elsewhere else '')
                plan['ambiguous'].append(
                    f'{rec_path.name}: a photo named {name!r} sits in '
                    f'{", ".join(folders)}, which photos_ignore: skips{seen} - '
                    'reconcile does not look inside ignored folders, so it cannot '
                    'tell which copy the record means. If the one in the ignored '
                    'folder is it, move it out of that folder and re-run; '
                    f'otherwise edit the `file: {alias}` line in {rec_path.name} '
                    'to the right photo, then run `fha lint`.')
                continue
            same = [a for a in free(carriers(sids)) if PurePosixPath(a).name == name]
            unknown_same = free(unknown_by_name.get(name, []))
            if not same:
                if unknown_same:
                    plan['unverified'].append(why_unknown(rec_path, alias, unknown_same))
                else:
                    unresolved.setdefault(rec_path, (meta, []))[1].append(alias)
                continue
            if unknown_same:
                plan['unverified'].append(why_unknown(rec_path, alias, unknown_same))
                continue
            if len(same) >= 2:
                # Owner policy: one same-named carrier or no heal. The tail
                # only orders the list and names the likeliest one.
                ranked = sorted(same, key=lambda a: (-_tail_match(alias, a), a))
                tails = [_tail_match(alias, a) for a in ranked]
                hint = ''
                if tails[0] > tails[1]:
                    hint = (f' ({ranked[0]} keeps the most of the old folder path, '
                            'but an exported copy can too)')
                plan['ambiguous'].append(
                    f'{rec_path.name}: more than one photo named {name!r} carries '
                    f'{label(sids)} ({", ".join(ranked)}){hint} - reconcile will not '
                    f'guess which one the record means. {edit_hint(rec_path, alias)}')
                continue
            cand = same[0]
            if len(known.get(cand, ())) >= 2:
                plan['ambiguous'].append(
                    f'{rec_path.name}: {cand} has the right name but carries more '
                    f'than one source ID ({label(known[cand])}) - reconcile will not '
                    'decide which record it belongs to. If it is this record\'s '
                    f'photo, edit the `file: {alias}` line in {rec_path.name} to '
                    f'`file: {cand}`, then run `fha lint`.')
                continue
            claims.setdefault(cand, []).append((rec_path, alias))
    for cand, claimants in sorted(claims.items()):
        if len(claimants) == 1:
            rec_path, alias = claimants[0]
            plan['heals'].setdefault(rec_path, []).append((alias, cand))
            taken.add(cand)
            continue
        names = ', '.join(sorted({r.name for r, _a in claimants}))
        for rec_path, alias in claimants:
            plan['ambiguous'].append(
                f'{rec_path.name}: {cand} matches more than one missing entry '
                f'({names}) - reconcile will not choose between them. '
                f'{edit_hint(rec_path, alias)}')

    # 2. Keyword-only matches: proposals, never heals (owner policy).
    for rec_path, (meta, aliases) in unresolved.items():
        sids = record_sids(rec_path, meta)
        remaining = free(carriers(sids))
        for alias in aliases:
            if remaining:
                old = alias.replace('\\', '/')
                old_fp = catalog['fingerprints'].get(old)
                details = []
                for cand in remaining[:5]:
                    note = ''
                    if len(known.get(cand, ())) >= 2:
                        note = f' (it also carries {label(known[cand] - sids)})'
                    elif (old_fp and photoindex._is_real_fingerprint(old_fp)
                          and photoindex._photo_fingerprint(on_disk[cand]) == old_fp):
                        note = (' (the same picture the photo catalog last saw '
                                'at the old path)')
                    details.append(f'{cand}{note}')
                more = f' and {len(remaining) - 5} more' if len(remaining) > 5 else ''
                unread = ''
                if unknown:
                    unread = (f' {len(unknown)} photo(s) have not been read, so there '
                              'may be others; `fha reconcile --with-exif` reads them.')
                plan['proposed'].append(
                    f'{rec_path.name}: nothing named '
                    f'{PurePosixPath(old).name!r} is left, but {", ".join(details)}{more} '
                    f'carr{"ies" if len(remaining) == 1 else "y"} {label(sids)} - '
                    'a back scan, an edited copy or an export can carry the same '
                    'keyword, so reconcile does not re-tie on the keyword alone. If '
                    f'one of them is this photo renamed, edit the `file: {alias}` '
                    f'line in {rec_path.name} to point at it, then run `fha lint`.'
                    f'{unread}')
            elif unknown:
                plan['unverified'].append(why_unknown(rec_path, alias, []))
            else:
                all_carriers = carriers(sids)
                others = sorted(a for a in all_carriers if listers(a) - {rec_path})
                own = sorted(a for a in all_carriers if rec_path in listers(a))
                if others:
                    plan['ambiguous'].append(
                        f'{rec_path.name}: the photo(s) carrying {label(sids)} '
                        f'({", ".join(others)}) are already listed by another '
                        'record - reconcile will not point two records at one '
                        'photo. Fix the duplicate files: entry by hand, then run '
                        '`fha lint`.')
                    continue
                if own:
                    whose = (f'no other photo in the photos folder carries '
                             f'{label(sids)} (only this record\'s own {", ".join(own)})')
                else:
                    whose = f'no photo in the photos folder carries {label(sids)}'
                plan['missing'].append(
                    f'{rec_path.name}: {alias!r} is gone - {whose}{ignore_note}. '
                    'If it moved outside the photos folder, bring it back; if it '
                    "is truly gone, note that in the record's ## Notes.")

    # 3. An old path a different picture now holds.
    # Every branch names a step that clears the warning for good. fha has no
    # command that re-embeds a keyword into an already-listed photo (`fha
    # process --more` on it would list it twice), so the re-embed step is the
    # exiftool line itself, which keeps its own `_original` copy of the file.
    for rec_path, _meta, alias, sids, carried in suspects:
        elsewhere = free(carriers(sids))
        if not elsewhere:
            continue
        found = (f'{rec_path.name}: {alias} is on disk but does not carry '
                 f'{label(sids)}, while {", ".join(elsewhere[:5])} does - a '
                 'different picture may have taken the old path after the photo '
                 f'moved. If so, edit the `file: {alias}` line in {rec_path.name} '
                 'to the photo that carries the keyword.')
        if carried:
            clear = (f' The listed file carries {label(carried)} instead, so it '
                     "looks like another source's photo; if it really is this "
                     f"record's, move {elsewhere[0]} out of the photos folder to "
                     'clear this.')
        else:
            sid = sorted(sids)[0].upper()
            keyword = shell_quote(f'SOURCE: {sid}')
            target = shell_quote(alias.replace('\\', '/'))
            clear = (' If the listed file is the right photo, clear this by '
                     f'writing the keyword into it (run from the archive folder: '
                     f'`exiftool -Keywords+={keyword} -XMP-dc:Subject+={keyword} '
                     f'{target}`, then `fha photoindex`), or by moving '
                     f'{elsewhere[0]} out of the photos folder.')
        plan['reused'].append(f'{found}{clear} Nothing was changed.')
    return plan


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
           result: Result, fha_config: 'dict | None' = None) -> int:
    """Apply the planned rewrites, one record at a time, refusing on drift.

    With `fha_config`, a heal whose target is a file the record ALREADY lists
    (however that entry spells it) is refused and the record left untouched:
    re-tying a stale entry onto it would list one file twice, and the second
    entry would hide that the first one's real file is still lost. The
    planners exclude listed files already; this is the writer's own check,
    so no planner can get it wrong.

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
        if fha_config is not None:
            duplicate = _already_listed_target(archive_root, fha_config, rec_path, pairs)
            if duplicate:
                new_alias, spelled = duplicate
                result.add('warning',
                           f'{rec_path.name}: {new_alias} is already listed in this '
                           f'record (as {spelled}) - re-tying another entry to it '
                           'would list one file twice. Record left untouched; fix '
                           'the entry by hand, then run `fha lint`.')
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
    dry_run: bool = False, with_exif: bool = False, progress=None,
) -> Result:
    """Engine: heal moved documents and filed photos on their records, then
    the photo catalog.

    `progress(done, total)`, when given, is called while a long --with-exif
    keyword read runs; the engine itself never prints.

    Returns a Result whose data carries {'status', 'healed', 'ambiguous',
    'missing', 'unlisted'} counts for the documents pass and {'photos_healed',
    'photos_ambiguous', 'photos_proposed', 'photos_missing',
    'photos_unverified', 'photos_reused'} for the photo-record pass (#289). Exit code follows the messages: clean run or
    clean heal is 0; anything left needing a human (ambiguous names, genuinely
    missing files, unlisted S-id files, a photo whose keyword could not be
    read) is a warning, exit 1. Working-copy mode no-ops cleanly - the assets
    live on the main machine.

    Both record plans are computed before anything is written, and the
    photo-record plan reads the catalog before the catalog pass moves any of
    its rows, so a --dry-run preview names exactly the rewrites a real run
    makes.
    """
    result = Result(data={'status': 'ok', 'healed': 0, 'ambiguous': 0,
                          'missing': 0, 'unlisted': 0, 'photos_healed': 0,
                          'photos_ambiguous': 0, 'photos_proposed': 0,
                          'photos_missing': 0, 'photos_unverified': 0,
                          'photos_reused': 0})

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
        photo_plan = _plan_photos(archive_root, fha_config, records, with_exif, progress)
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
            heals, ambiguous = plan['heals'], plan['ambiguous']
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
                result.data['healed'] = _apply(archive_root, heals, result, fha_config)
                records_healed += result.data['healed']

            for line in ambiguous:
                result.add('warning', line)
            for line in missing:
                result.add('warning', line)
            for sid, aliases in sorted(unlisted.items()):
                shown = ', '.join(sorted(aliases))
                result.add('warning',
                           f'{shown} carries {sid.upper()} but that record does not list it - '
                           'attach it with `fha process <primary-file> --more FILE role`, '
                           'or add a files: entry to the record.')

            if not (heal_count or ambiguous or missing or unlisted):
                result.add('info', 'Documents all tied to their records - nothing to heal.')

    # Photos filed on source records (#289). Planned in full before any of
    # its rewrites land, from the catalog as it stood before the catalog pass
    # below moves rows, so the preview and the real run agree.
    if photo_plan is not None:
        photo_heals = photo_plan['heals']
        photo_heal_count = sum(len(v) for v in photo_heals.values())
        result.data.update({
            'photos_healed': photo_heal_count,
            'photos_ambiguous': len(photo_plan['ambiguous']),
            'photos_proposed': len(photo_plan['proposed']),
            'photos_missing': len(photo_plan['missing']),
            'photos_unverified': len(photo_plan['unverified']),
            'photos_reused': len(photo_plan['reused']),
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
            photo_records_healed = _apply(archive_root, photo_heals, result, fha_config)
            result.data['photos_healed'] = photo_records_healed
            records_healed += photo_records_healed
        for line in (*photo_plan['ambiguous'], *photo_plan['proposed'],
                     *photo_plan['missing'], *photo_plan['unverified'],
                     *photo_plan['reused']):
            result.add('warning', line)
        if not (photo_plan['stale'] or photo_plan['notices'] or photo_plan['reused']):
            # Says only what was checked: every listed photo path still opens.
            # Whether each is the RIGHT picture is the reused-path check's
            # question, and that one speaks up when it finds a reason to.
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
    'photos folder is found again by the SOURCE: keyword inside it (from the '
    'photo catalog, or read with --with-exif), and the photo catalog is '
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
    def progress(done: int, total: int) -> None:
        print(f'Reading photo keywords: {done} of {total}...', file=sys.stderr, flush=True)

    result = run_reconcile(
        archive_root, fha_config,
        dry_run=bool(getattr(args, 'dry_run', False)),
        with_exif=bool(getattr(args, 'with_exif', False)),
        progress=progress,
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
