#!/usr/bin/env python3
"""
normalize_links.py - fha normalize-links: settle citations to their canonical form.

  fha normalize-links            Preview the rewrites (dry run - writes nothing)
  fha normalize-links --dry-run  The same preview, named explicitly (the flag the
                                 operating instructions tell agents to always pass)
  fha normalize-links --write    Apply them, showing the same diff

`--dry-run --write` together is refused (exit 2): pick one - preview or write.

This is the ONE explicit, previewed rewrite pass over a human's citation prose
(SPEC §3 "resolve always; rewrite only on purpose"). It is deliberately separate
from the Formatter, which never rewrites prose beyond trailing whitespace
(TOOLING §3) - and citations are prose. Nothing here ever runs silently: the
default is a dry run, a real write needs `--write`, and a human stem is never
dropped (it stays in the record's `aliases:`, which is exactly what lets the
shortened link keep resolving).

Three rewrites, all toward the stable, rename-proof, ID-carrying form:
  (a) a legacy single-bracket `[S-…]` prose cite        → `[[S-…]]`
  (b) a resolved human stem/name in prose `[[grandmas-album]]` / `[[Ken Smith]]`
      → `[[S-…|grandmas-album]]` / `[[P-…|Ken Smith]]`  (ID load-bearing, the
      human's text preserved as display)
  (c) a resolved frontmatter name-link `people: ["[[Ken Smith]]"]`
      → `["[[P-…|Ken Smith]]"]`                          (the human graph surface,
      settled to a stable P-id target)

An AMBIGUOUS name (two "John Smith"s) is never guessed - it is reported and left
exactly as written for the human (or Obsidian autocomplete) to pin to an ID.

The claims ```yaml block and bare-ID frontmatter lists are NEVER touched: those
are structured data, not prose (SPEC §8/§14 - "the claims block stays bare").

Follows the headless-core Result contract: `run_normalize_links` computes and
returns a `Result`; `_cmd_normalize_links` renders it and returns the exit code.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    FRONT_RE,
    LEGACY_TOKEN_RE,
    WIKILINK_RE,
    FhaConfigError,
    Result,
    alias_clashes,
    archive_relative,
    build_alias_map,
    fmt_id_display,
    format_yaml_dependency_error,
    id_type_of,
    is_generated_file,
    is_template_file,
    load_fha_yaml,
    normalize_id,
    read_record,
    read_text_exact,
    resolve_ref,
    resolve_root_arg,
    undecodable_file_recorder,
    write_text_exact_atomic,
)

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

# Fenced code blocks (the ```yaml claims block, any ``` example) are structured
# data, not prose - split them out so a prose rewrite never edits a bare ID
# inside them.
import re

_FENCE_RE = re.compile(r'(```.*?```)', re.S)


# ── Record scan → alias map ───────────────────────────────────────────────────

def _scan_records(
    archive_root: Path,
    *,
    on_decode_error=None,
    on_places_error=None,
) -> list[dict]:
    """Collect the identity + alias surface of every record, for the resolve map
    and clash check: persons (id/name/variants/stems), sources (id/stems), and
    places (id/name/alt_names).

    A whole-archive scan, same shape as `stubs.py`'s unresolved-reference walk:
    a file saved in another encoding (cp1252, a Windows editor's default) must
    not blind the rest of the scan (#68). `on_decode_error`, when given, is
    handed to every `read_record` call here (people and sources both) and the
    file is skipped rather than crashed on; the caller
    (`run_normalize_links`) is the one with somewhere to put the aggregated
    report, so it owns the list this callback feeds. Omitted, the old
    crash-on-decode behaviour is unchanged - matching `read_record`'s own
    opt-in contract.

    **What a skip here costs, and why the caller must be told.** This function
    does not merely feed the resolve map; it feeds `alias_clashes` too, and a
    clash is what STOPS a rewrite. A record missing from this scan cannot
    collide with anything, so a name that two records really share reads as
    belonging to exactly one - and `--write` then pins the human's
    `[[John Smith]]` to whichever John Smith happened to decode. That is the
    one thing this verb promises never to do ("an AMBIGUOUS name is never
    guessed", SPEC §7), and unlike a crash it is silent and on disk. So every
    gap in this scan is reported UP, and `run_normalize_links` refuses to
    write while any remains.

    `on_places_error` is the same channel for `places.yaml`, which is not a
    record and so has no `read_record` seam: one unreadable or unparseable
    file drops the archive's ENTIRE place layer out of the map at once, which
    is the same clash-losing hole in its widest form (a person and a town both
    called Florence stop colliding). Called with `(path, reason)`. Omitted, the
    old silent `except Exception` behaviour is unchanged."""
    records: list[dict] = []

    people_root = archive_root / 'people'
    if people_root.is_dir():
        for path in people_root.rglob('*.md'):
            if is_template_file(path):
                continue
            rec = read_record(path, on_decode_error=on_decode_error)
            if rec['undecodable']:
                continue
            meta = rec['meta']
            rid = normalize_id(str(meta.get('id', '')))
            if rid.startswith('p-'):
                records.append({
                    'id': rid,
                    'name': meta.get('name'),
                    'name_variants': meta.get('name_variants') or [],
                    'aliases': meta.get('aliases') or [],
                    'status': meta.get('status'),
                })

    sources_root = archive_root / 'sources'
    if sources_root.is_dir():
        for path in sources_root.rglob('*.md'):
            if is_template_file(path):
                continue
            rec = read_record(path, on_decode_error=on_decode_error)
            if rec['undecodable']:
                continue
            meta = rec['meta']
            rid = normalize_id(str(meta.get('id', '')))
            if rid.startswith('s-'):
                records.append({'id': rid, 'aliases': meta.get('aliases') or []})

    places_path = archive_root / 'places' / 'places.yaml'
    if places_path.exists() and yaml is not None:
        places = None
        try:
            places = yaml.safe_load(places_path.read_text(encoding='utf-8'))
        except UnicodeDecodeError:
            if on_places_error is not None:
                on_places_error(places_path, 'it is not saved as UTF-8 text')
        except OSError as e:
            if on_places_error is not None:
                on_places_error(places_path, f'it could not be opened ({e.strerror or e})')
        except Exception:
            # Deliberately not the parser's own message: it is a stack-shaped
            # note about flow sequences, and `fha lint` already reports this
            # file's parse error as E010 with the line. What matters HERE is
            # only that the place layer is missing, which is what the caller
            # reports.
            if on_places_error is not None:
                on_places_error(places_path, 'its YAML would not parse '
                                             '(`fha lint` names the line, E010)')
        for place in places or []:
            if not isinstance(place, dict):
                continue
            rid = normalize_id(str(place.get('id', '')))
            if rid.startswith('l-'):
                records.append({
                    'id': rid,
                    'name': place.get('name'),
                    'alt_names': place.get('alt_names') or [],
                })

    return records


# ── Rewriting ─────────────────────────────────────────────────────────────────

def _rewrite_wikilinks(
    text: str,
    alias_map: dict[str, str],
    clashes: dict[str, list[str]],
) -> tuple[str, int, list[str]]:
    """Rewrite resolved name/stem `[[ ]]` links to the canonical `[[ID|display]]`
    form. ID-target links are already canonical and left alone; an ambiguous name
    is collected (for the caller to report) and left exactly as written."""
    edits = 0
    ambiguous: list[str] = []

    def repl(m: re.Match) -> str:
        nonlocal edits
        target = m.group(1).strip()
        if id_type_of(target):
            return m.group(0)   # already an ID link - canonical, leave it
        resolved = resolve_ref(target, alias_map)
        if resolved:
            display = (m.group(3) or target).strip()
            edits += 1
            return f'[[{fmt_id_display(resolved)}|{display}]]'
        if target.lower() in clashes:
            ambiguous.append(target)
        return m.group(0)

    return WIKILINK_RE.sub(repl, text), edits, ambiguous


def _rewrite_prose(
    text: str,
    alias_map: dict[str, str],
    clashes: dict[str, list[str]],
) -> tuple[str, int, list[str]]:
    """Prose rewrites: resolved name/stem links, then legacy single-bracket IDs."""
    text, edits, ambiguous = _rewrite_wikilinks(text, alias_map, clashes)

    def upgrade_legacy(m: re.Match) -> str:
        return f'[[{fmt_id_display(normalize_id(m.group(1)))}]]'

    new_text, n = LEGACY_TOKEN_RE.subn(upgrade_legacy, text)
    edits += n
    return new_text, edits, ambiguous


# Matches a `people:` or `places:` YAML key plus all its continuation lines
# (both flow-style `people: ["[[X]]"]` and block-style `  - "[[X]]"` items).
# Only these two fields carry name-links that should be normalised; text fields
# like citation:, provenance:, title: must never be rewritten.
_FM_PERSON_PLACE_RE = re.compile(
    r'^((?:people|places):[^\n]*(?:\n[ \t]+[^\n]*)*)',
    re.M,
)


def _rewrite_frontmatter(
    fm_text: str,
    alias_map: dict[str, str],
    clashes: dict[str, list[str]],
) -> tuple[str, int, list[str]]:
    """Rewrite wikilinks inside `people:` and `places:` frontmatter blocks only.
    All other fields (citation:, title:, aliases:, provenance: …) are untouched."""
    total_edits = 0
    all_ambiguous: list[str] = []

    def repl(m: re.Match) -> str:
        nonlocal total_edits
        new_block, e, a = _rewrite_wikilinks(m.group(1), alias_map, clashes)
        total_edits += e
        all_ambiguous.extend(a)
        return new_block

    new_fm = _FM_PERSON_PLACE_RE.sub(repl, fm_text)
    return new_fm, total_edits, all_ambiguous


def normalize_text(
    text: str,
    alias_map: dict[str, str],
    clashes: dict[str, list[str]],
) -> tuple[str, int, list[str]]:
    """Normalize one record's text, region by region:

      - frontmatter: only the `people:` and `places:` list blocks (so text fields
        like citation:, provenance:, title:, aliases: are never touched, and
        bare-ID `people: [P-…]` lists without wikilinks are left alone);
      - body prose: name-link upgrade + legacy single-bracket upgrade, but NOT
        inside ```fenced``` blocks - the claims YAML stays bare.
    """
    fm = FRONT_RE.match(text)
    if fm:
        fm_text, body = text[:fm.end()], text[fm.end():]
        new_fm, e_fm, a_fm = _rewrite_frontmatter(fm_text, alias_map, clashes)
    else:
        new_fm, body, e_fm, a_fm = '', text, 0, []

    out: list[str] = []
    edits = e_fm
    ambiguous = list(a_fm)
    for i, part in enumerate(_FENCE_RE.split(body)):
        if i % 2 == 1:          # odd parts are fenced blocks - structured data
            out.append(part)
            continue
        new_part, e, a = _rewrite_prose(part, alias_map, clashes)
        out.append(new_part)
        edits += e
        ambiguous += a

    return new_fm + ''.join(out), edits, ambiguous


def _record_files(archive_root: Path):
    """Yield every prose-bearing record file (people/, sources/, notes/),
    skipping `_TEMPLATE.*` teaching templates (not records) and GENERATED
    companion files (those must only be regenerated by tools, AGENTS.md §4).

    GENERATED ownership is judged by `_lib.is_generated_file` - the first
    NON-BLANK line, BOM tolerated - the same rule lint and views apply. The
    old byte-0 check here rewrote prose inside any generated file that merely
    began with a blank line (round-2 finding 12)."""
    for sub in ('people', 'sources', 'notes'):
        base = archive_root / sub
        if base.is_dir():
            for path in sorted(base.rglob('*.md')):
                if is_template_file(path):
                    continue
                if is_generated_file(path):
                    continue
                yield path


# ── Run / compute ─────────────────────────────────────────────────────────────

def run_normalize_links(
    archive_root: Path,
    fha_config: dict,
    *,
    write: bool = False,
) -> Result:
    """Compute (and, with write=True, apply) the citation normalization, returning
    a Result. `data` carries `files_changed`, `edits`, the per-file unified
    `diffs`, the `ambiguous` names that were left for a human to pin, and
    `unreadable` - the archive-relative files this run could not read.

    **The resolve map has to be complete before anything is written.** A record
    the scan could not read is absent from `alias_clashes` too, so a name that
    two records really share looks unambiguous, and `--write` pins the human's
    `[[John Smith]]` to whichever John Smith happened to decode (see
    `_scan_records`). That is the one thing this verb promises never to do -
    "an AMBIGUOUS name is never guessed" - and unlike a crash it is silent and
    on disk. Nothing inside this run can tell WHICH names went wrong, because
    the record that would have said so is the missing one, so `--write` refuses
    outright while any gap remains: exit 2, nothing touched.

    The preview still runs and still prints its diff - it writes nothing, and
    seeing the shape of the problem is how the human decides what to fix - but
    it says plainly that what it shows cannot be trusted until the named files
    can be read. This is the same reasoning `_lib.read_record`'s docstring
    gives for keeping the whole report opt-in: an unreadable record answered as
    an empty one is worse than a loud stop, and this verb WRITES.

    A file skipped only by the REWRITE walk (a `notes/` file, say) is the
    harmless case and never blocks the write: it is left exactly as written,
    like any other file with nothing to change.
    """
    # Two lists, because they answer two different questions. `unreadable` is
    # "what should this run tell the human about?" - every file, either pass,
    # de-duplicated, since a people/ record is walked by BOTH the alias scan
    # and the rewrite walk and must earn one report, not two. `map_gaps` is
    # "may this run write at all?", and only the alias scan feeds it.
    unreadable: list[Path] = []
    map_gaps: list[Path] = []
    places_gaps: list[tuple[Path, str]] = []
    note_unreadable = undecodable_file_recorder(unreadable)
    note_map_gap = undecodable_file_recorder(map_gaps)

    def on_scan_decode_error(path: Path) -> None:
        note_unreadable(path)
        note_map_gap(path)

    def on_places_error(path: Path, reason: str) -> None:
        note_unreadable(path)
        if not any(p == path for p, _ in places_gaps):
            places_gaps.append((path, reason))

    records = _scan_records(
        archive_root,
        on_decode_error=on_scan_decode_error,
        on_places_error=on_places_error,
    )
    alias_map = build_alias_map(records)
    clashes = alias_clashes(records)

    if write and (map_gaps or places_gaps):
        return _refuse_incomplete_map(archive_root, map_gaps, places_gaps)

    result = Result()
    result.data['diffs'] = {}
    files_changed = 0
    total_edits = 0
    ambiguous_seen: set[str] = set()

    for path in _record_files(archive_root):
        try:
            # Byte-faithful read: the default translates CRLF to LF, and the
            # matching default write would then re-translate on the way out, so
            # tidying one citation in a CRLF-authored record rewrote every line
            # ending in it. This verb touches every record in the archive at
            # once, so that churn is archive-wide.
            original = read_text_exact(path)
        except OSError:
            continue
        except UnicodeDecodeError:
            # A third #68 site beyond the two `_scan_records` was built for:
            # `_record_files` walks people/, sources/ AND notes/ for the
            # actual rewrite, wider than `_scan_records`'s alias-map scan
            # (people/sources only). `read_text_exact` has no
            # `on_decode_error` seam (it is byte-preserving, not the
            # frontmatter parser), so the decode is caught here directly.
            #
            # It feeds `note_unreadable` and NOT `note_map_gap`, and the
            # difference is the whole point: a file this walk cannot read is
            # simply left as its author wrote it, which is the same outcome as
            # a file with nothing to normalize - harmless, and no reason to
            # refuse the write. A file the ALIAS scan could not read is the
            # dangerous one, because it silently unmakes a clash. Sharing the
            # `unreadable` recorder still means a people/ or sources/ record
            # both passes touch earns one report, not two.
            note_unreadable(path)
            continue
        new_text, edits, ambiguous = normalize_text(original, alias_map, clashes)
        rel = archive_relative(path, archive_root)

        for name in ambiguous:
            if name.lower() not in ambiguous_seen:
                ambiguous_seen.add(name.lower())
                ids = ', '.join(fmt_id_display(i) for i in clashes.get(name.lower(), []))
                result.add('warning', f"{rel}: '{name}' is ambiguous - it names {ids}. "
                           'Left unchanged; pin it to one ID by hand.')

        if new_text == original:
            continue
        files_changed += 1
        total_edits += edits
        diff = ''.join(difflib.unified_diff(
            original.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=rel, tofile=rel,
        ))
        result.data['diffs'][rel] = diff
        result.add('info', f'{rel}: {edits} citation(s) to normalize', path=path)
        if write:
            # Atomic: --write walks every record in the archive, so a failure
            # midway (disk full on a large tree) would otherwise truncate
            # whichever record it happened to be holding open. No
            # `reapply_newline` needed - `normalize_text` is regex substitution
            # over the untranslated text, so `new_text` already carries the
            # record's own line endings verbatim.
            write_text_exact_atomic(path, new_text)
            result.note_changed(path)

    result.data['files_changed'] = files_changed
    result.data['edits'] = total_edits
    result.data['written'] = write
    # The docstring has always promised `ambiguous`; now it is actually there,
    # so a headless consumer can read the names a human still has to pin
    # without scraping the warning lines. `unreadable` is the same courtesy for
    # the files below.
    result.data['ambiguous'] = sorted(ambiguous_seen)
    result.data['unreadable'] = [archive_relative(p, archive_root) for p in unreadable]

    if files_changed == 0:
        result.add('info', 'All citations are already in canonical form - nothing to normalize.')
    elif write:
        result.add('info', f'Normalized {total_edits} citation(s) across {files_changed} file(s).')
    elif map_gaps or places_gaps:
        # The same count, without the `--write` invitation: the warning below
        # explains that `--write` refuses while the map has a hole in it, and a
        # printed `next:` is a command to be copied (TOOLING §2), so it must
        # never be one this run already knows will refuse.
        result.add('info',
                   f'{total_edits} citation(s) across {files_changed} file(s) look '
                   'normalizable, but see the warning below before trusting that.')
    else:
        result.add('info',
                   f'{total_edits} citation(s) across {files_changed} file(s) can be normalized. '
                   'Re-run with --write to apply.',
                   next_step='fha normalize-links --write')

    if unreadable:
        # Reached only on a preview (a `--write` with a map gap has already
        # refused above), or on a write whose only unreadable files were in the
        # rewrite walk. Both halves are stated, because they are what the
        # human's next command depends on.
        result.add(
            'warning',
            f'{_name_files(unreadable, archive_root)} Each was skipped rather '
            'than stopping the run. Anything written in one of them is left '
            'exactly as it was; nothing about the file was changed. It is only '
            'saved in an older encoding (a Windows editor defaults to one, '
            'commonly cp1252) - open it and save it again choosing UTF-8 (in '
            'Notepad: Save As, then pick UTF-8 from the Encoding menu), then '
            'run `fha normalize-links` again. (`fha lint` reports the same '
            'files as W128.)')

    if map_gaps or places_gaps:
        # A preview: say why it cannot be trusted, and that `--write` will
        # refuse, BEFORE the human reads a diff and reaches for it. The cause
        # ("not saved as UTF-8") is already in the warning above, so this one
        # states only the consequence.
        result.add(
            'warning',
            f'{_map_gap_reason(archive_root, map_gaps, places_gaps, with_cause=False)} '
            'Two people who really share a name stop looking like two people, '
            'so this preview may show a name being pinned to an ID that is only '
            'the one that happened to be readable. `--write` refuses until '
            'those file(s) can be read - fix them first, then preview again.',
            next_step='fha lint')

    if ambiguous_seen or unreadable:
        result.ok = False
        result.exit_code = EXIT_WARNINGS
    return result


def _name_files(paths: list, archive_root: Path) -> str:
    """"N file(s) are not saved as UTF-8 text ...: a, b, c." - up to five names.

    The one sentence every report below opens with, spelled the way `fha index`
    and `fha lint`'s W128 spell it, so the same condition reads the same from
    whichever command the human happened to run.
    """
    shown = ', '.join(archive_relative(p, archive_root) for p in paths[:5])
    if len(paths) > 5:
        shown += f' and {len(paths) - 5} more'
    return (f'{len(paths)} file(s) are not saved as UTF-8 text, so this run '
            f'could not read them: {shown}.')


def _map_gap_reason(
    archive_root: Path,
    map_gaps: list,
    places_gaps: list,
    *,
    with_cause: bool = True,
) -> str:
    """Why the resolve map is known to be incomplete, in the human's terms.

    Records and `places.yaml` are named separately because the remedies differ
    (re-save a record's encoding vs. repair a file whose problem may not be a
    decoding one at all), and a `places.yaml` failure is the wider hole: it
    takes every place out of the map at once.

    `with_cause=False` drops the "N file(s) are not saved as UTF-8 text" half
    for the preview warning, which follows a message that has already said it -
    one condition, said once, however many messages it earns.
    """
    parts: list[str] = []
    if map_gaps:
        if with_cause:
            parts.append(
                f'{_name_files(map_gaps, archive_root)} What those records call '
                'themselves - their names, aliases and stems - is missing from '
                'the resolve map this run built.')
        else:
            named = ', '.join(archive_relative(p, archive_root) for p in map_gaps[:5])
            if len(map_gaps) > 5:
                named += f' and {len(map_gaps) - 5} more'
            parts.append(
                f'What the record(s) in {named} call themselves - their names, '
                'aliases and stems - is missing from the resolve map this run '
                'built.')
    for path, reason in places_gaps:
        parts.append(
            f'{archive_relative(path, archive_root)} was not read because '
            f'{reason}, so EVERY place is missing from the resolve map this '
            'run built.')
    return ' '.join(parts)


def _refuse_incomplete_map(
    archive_root: Path,
    map_gaps: list,
    places_gaps: list,
) -> Result:
    """The `--write` refusal when the resolve map is known to be incomplete.

    An error, not a warning (TOOLING §1: exit 2 is "errors found"), and it
    returns before a single file is opened for rewriting - so `changed` is
    empty and `written` is False, which is exactly what happened.
    """
    result = Result(ok=False, exit_code=EXIT_ERRORS)
    result.data.update({
        'diffs': {}, 'files_changed': 0, 'edits': 0, 'written': False,
        'ambiguous': [],
        'unreadable': (
            [archive_relative(p, archive_root) for p in map_gaps]
            + [archive_relative(p, archive_root) for p, _ in places_gaps]),
    })
    result.add(
        'error',
        f'{_map_gap_reason(archive_root, map_gaps, places_gaps)} A name two '
        'records really share would look like it belongs to just one, and this '
        'command would pin your `[[Name]]` to whichever record it could read - '
        'exactly the guess it exists not to make. Nothing was written. Fix the '
        'file(s) above - a record saved in an older encoding (commonly cp1252 '
        'from a Windows editor) is re-saved as UTF-8: open it, Save As, pick '
        'UTF-8 - then run `fha normalize-links` to preview again. `fha lint` '
        'names the same files (W128), and `fha normalize-links` on its own '
        'previews without writing anything, so it is safe to run meanwhile.',
        next_step='fha lint')
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

def _emit(result: Result, show_diff: bool) -> int:
    if show_diff:
        for rel, diff in result.data.get('diffs', {}).items():
            if diff:
                sys.stdout.write(diff)
                if not diff.endswith('\n'):
                    sys.stdout.write('\n')
    for msg in result.messages:
        stream = sys.stderr if msg.level == 'error' else sys.stdout
        prefix = 'ERROR: ' if msg.level == 'error' else ''
        print(f'{prefix}{msg.text}', file=stream)
        if msg.next_step:
            print(f'  next: {msg.next_step}', file=stream)
    return result.exit_code


def _cmd_normalize_links(args: argparse.Namespace) -> int:
    # `--dry-run` names the default preview explicitly (AGENTS.md tells agents
    # to always pass it before any mutating operation, so it must parse here);
    # combined with `--write` the request is contradictory - refuse plainly.
    if getattr(args, 'dry_run', False) and getattr(args, 'write', False):
        print('ERROR: --dry-run and --write cannot be combined - pick one: '
              '--dry-run previews the changes (the default), --write applies them.',
              file=sys.stderr)
        return EXIT_ERRORS
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE
    result = run_normalize_links(archive_root, fha_config, write=bool(getattr(args, 'write', False)))
    return _emit(result, show_diff=not bool(getattr(args, 'quiet', False)))


# User-facing --help text (the module docstring stays developer-facing).
_CLI_DESCRIPTION = """\
Tidy the citations and cross-links in your prose to the archive's [[ ]] form.

  fha normalize-links            Preview the rewrites (dry run - writes nothing)
  fha normalize-links --write    Apply them, showing the same diff

Runs as a preview by default; a real write needs --write. Your original names
are kept as aliases, so the shortened links still resolve."""


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        'normalize-links',
        help='Settle prose citations to the canonical [[ID]] / [[ID|name]] form',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root')
    p.add_argument('--write', action='store_true',
                   help='Apply the rewrites (default is a dry-run preview)')
    p.add_argument('--dry-run', action='store_true', dest='dry_run',
                   help='Preview without writing (already the default; accepted '
                        'so the always-preview habit works here too)')
    p.add_argument('--quiet', action='store_true',
                   help='Suppress the per-file diff (show only the summary)')
    p.set_defaults(func=_cmd_normalize_links)


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha normalize-links',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--root', metavar='PATH')
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--dry-run', action='store_true', dest='dry_run')
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args(argv)
    return _cmd_normalize_links(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
