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
`fha process --more` as the attach path. The photos side of the same drift is
`fha photoindex reconcile`'s machinery; when a photo catalog exists this tool
runs it too (pass-through of --dry-run/--with-exif), so one command reconciles
every file type - the §9 contract. Importing photoindex here follows
report.py's orchestrator precedent (a tool whose whole job is running other
tools' engines does import them; ordinary tools still never do).

Record writes are line-surgical: only the one `file:` line whose value is the
stale path changes, and the rewritten text must still carry the same number of
`file:` lines or the write is refused (file untouched, cause named) - the same
refuse-rather-than-corrupt posture the claims writers take. Working-copy mode
is a clean no-op: assets live on the main machine (SPEC §12.4), so there is
nothing here to reconcile against.

CODE MAP
--------
  _iter_source_records   - yield (path, meta) for every parseable sources/ record
  _disk_index            - basename -> [paths] map of every documents-root file
  _plan                  - compute healed/ambiguous/missing/unlisted, no writes
  _split_file_value      - split a file: line into (unquoted path, comment)
  _rewrite_entry         - line-surgical file: path rewrite inside one record text
  _apply                 - group heals per record, rewrite, refuse on any drift
  run_reconcile          - engine: plan + (unless dry-run) apply + photos side
  _cmd_reconcile         - CLI rendering of the Result
  register / _standalone_main - fha subcommand + python tools/reconcile.py entry
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path, PurePosixPath

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import (
    EXIT_FAILURE,
    EXIT_WARNINGS,
    FhaConfigError,
    Result,
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
    write_text_exact_atomic,
    yaml_inline,
)

import photoindex


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


def _disk_index(documents_root: Path) -> dict[str, list[Path]]:
    """Map basename -> every file with that name under the documents root.

    One walk, reused for both the heal match (find a moved file by its name)
    and the reverse unlisted pass - the tree is walked exactly once per run.
    """
    by_name: dict[str, list[Path]] = {}
    for p in documents_root.rglob('*'):
        if p.is_file():
            by_name.setdefault(p.name, []).append(p)
    return by_name


def _plan(
    archive_root: Path, fha_config: dict, documents_root: Path, result: Result,
) -> dict:
    """Compute the reconcile plan without writing anything.

    Returns {'heals': {record_path: [(old_alias, new_alias), ...]},
    'ambiguous': [...], 'missing': [...], 'unlisted': {sid: [alias, ...]}}.
    Only documents-alias entries are considered (the photos side has its own
    identity carrier and machinery); an entry whose `status:` is
    missing-fixture is a deliberate fixture state, never healed or flagged.
    """
    by_name = _disk_index(documents_root)
    by_sid: dict[str, list[Path]] = {}
    for paths in by_name.values():
        for p in paths:
            parsed = parse_filename(p)
            if parsed and parsed.get('id_type') == 'S':
                by_sid.setdefault(normalize_id(parsed['id_str']), []).append(p)

    heals: dict[Path, list[tuple[str, str]]] = {}
    ambiguous: list[str] = []
    missing: list[str] = []

    # Pass 1 - collect every record and every listed alias FIRST. The heal
    # logic below excludes already-listed files from S-id fallback matches and
    # the reverse pass keys off this set, so it must be complete before any
    # record is judged (a streaming build would let an early record match a
    # file a later record legitimately lists).
    malformed_sids: set[str] = set()
    records = list(_iter_source_records(archive_root, result, malformed_sids))
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
            'unlisted': unlisted}


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
        path = yaml.safe_load(value_repr)
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
    """Engine: heal documents-side inventory drift, then the photo catalog.

    Returns a Result whose data carries {'status', 'healed', 'ambiguous',
    'missing', 'unlisted'} counts. Exit code follows the messages: clean run
    or clean heal is 0; anything left needing a human (ambiguous names,
    genuinely missing files, unlisted S-id files) is a warning, exit 1.
    Working-copy mode no-ops cleanly - the assets live on the main machine.
    """
    result = Result(data={'status': 'ok', 'healed': 0, 'ambiguous': 0,
                          'missing': 0, 'unlisted': 0})

    if is_working_copy(archive_root):
        result.data['status'] = 'working-copy'
        result.add('info',
                   'This is a working copy - the actual files live on the main '
                   'machine, so there is nothing to reconcile here. Run '
                   '`fha reconcile` on the main archive.')
        return _finalize(result)

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
        plan = _plan(archive_root, fha_config, documents_root, result)
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
            result.data['healed'] = _apply(archive_root, heals, result)

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

        if heal_count and not dry_run:
            result.add('info',
                       'Run `fha index` so searches see the new locations, and '
                       '`fha lint` to confirm everything is tied down.')
        elif not (heal_count or ambiguous or missing or unlisted):
            result.add('info', 'Documents all tied to their records - nothing to heal.')

    # Photos side (TOOLING §9: one command reconciles every file type). Only
    # when a catalog exists - an archive that never built one should not fail.
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
        if photo_status in ('absent', 'unreadable'):
            # The catalog exists on disk but photoindex refused it (corrupt,
            # or an older schema). Keyed off status, not ok alone - the
            # unreachable-root case below is also ok=False but is a warning.
            result.add('error',
                       f'photos: the photo catalog (.cache/photos.sqlite) is '
                       f'{photo_status} - photo paths not checked. Rebuild it '
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
                   'No photo catalog (.cache/photos.sqlite) - photo paths not '
                   'checked. Run `fha photoindex` first if you want them covered.')

    return _finalize(result)


# -- CLI ----------------------------------------------------------------------

_CLI_DESCRIPTION = (
    'Re-tie moved files to their records. Rearrange the documents folder any '
    'way you like, then run this: every filed document is found again by the '
    'ID in its name and its record updated. Photos are reconciled too when a '
    'photo catalog exists. Preview with --dry-run first.'
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
                   help='Photos side: also read embedded keywords to re-match '
                        'moved photos (needs exiftool; see fha photoindex reconcile)')
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
                        help='Photos side: also read embedded keywords to re-match '
                             'moved photos (needs exiftool)')
    args = parser.parse_args(argv)
    return _cmd_reconcile(args)


if __name__ == '__main__':
    raise SystemExit(_standalone_main())
