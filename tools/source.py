#!/usr/bin/env python3
"""
source.py - fha source: deterministic source-record write-backs (TOOLING §3c sibling).

  fha source note S-id --text TEXT [--dry-run] [--root PATH]
  fha source edit-note S-id --old-text TEXT --text TEXT [--dry-run] [--root PATH]
  fha source extract S-id [--pages RANGES] [--dry-run] [--root PATH]
  fha source clear-keyword S-id --keyword TEXT [--replace-with TEXT] [--file NAME] [--dry-run] [--root PATH]

A source's `## Notes` section is the human-written free-text channel SPEC §14
reserves for "the story behind it, context, or where the original is kept" -
until this tool, adding to it meant opening the file by hand. `fha source
note` is the safe one-line way to jot something down without risking the
`## Claims` fence or the frontmatter above it: paste in a sentence from the
phone, on the porch, mid-research-session, and the tool finds the record,
appends the sentence as its own paragraph, and touches nothing else.

This module deliberately opens the `fha source` namespace - future
source-field verbs would live here. Four verbs ship now: `note` (append),
`edit-note` (rewrite one existing paragraph - the workbench's per-entry edit
button; see run_source_edit_note), `extract` (dump a PDF's embedded text
layer into a derived [Page N]-labeled companion - see run_source_extract),
and `clear-keyword` (#112: correct a documents-root asset's embedded
Keywords/Subject value - see run_source_clear_keyword).

DESIGN RULES (why the code looks the way it does)
-------------------------------------------------
- **Locate by scanning, never the index.** The record is found by walking
  `sources/` for the `_{S-id}.md` filename suffix (the shared
  `_lib.find_source_record_path`, sibling of `find_person_record_path`). A
  stale or absent `.cache/index.sqlite` can never block or misdirect the
  write - the same rule `fha claim` and `fha person set-living` follow.
- **The edit is text surgery, bounded to one section.** Only lines strictly
  between the `## Notes` heading and the next `##` heading (or end of file)
  ever change; the frontmatter, the `## Claims` fence, and any later section
  (`## Stories`, etc.) are never touched by construction - the insertion point
  is always inside those bounds, never outside them. The locate/append itself
  is the shared `_lib.append_paragraph_to_section` engine (the same one
  `fha person edit`/`note` uses); reading and writing go through
  `read_text_exact`/`write_text_exact_atomic` so a CRLF-authored record churns only
  the lines the edit adds.
- **Append-only, always.** The new text always lands as a new blank-line-
  separated paragraph at the END of the section; an existing note is never
  edited, reordered, or removed - this is a human-written audit trail, and
  `fha source note` never pretends to be its author's voice (no AI marker is
  written here because the human types the words; a future automated writer
  into this section would need its own marker per AGENTS.md rule 5).
- **A malformed record still gets a home for the note.** A record whose
  `## Notes` heading is missing (hand-edited, or from a very old scaffold)
  gets the heading created at the end of the file rather than refusing - the
  "forgiving, not fussy" rule (AGENTS.md) applies to a hand-made record same
  as a hand-made note.
- **`status: superseded` is not a reason to refuse.** Unlike a merged person
  tombstone (which forks the truth if edited), a superseded source is still
  the source that was seen and superseding it does not erase its history -
  notes keep landing on it as an audit trail. No status field is consulted
  here at all.
- **A cheap regression guard, not a full rewrite verifier.** Because the
  insertion is bounded to the Notes section by construction, the `## Claims`
  block cannot normally be touched - but as belt-and-braces (the same
  instinct behind `_lib.claims_edit_problem`), the block is checked before
  and after the edit and the write refuses if the edit somehow broke a
  previously-sound block. A source with no Claims block at all (SPEC §14:
  "just taking notes? DELETE this whole ## Claims block") is not penalized -
  the check only fires on a REGRESSION (sound -> broken), never on a
  Claims-less record staying Claims-less.
- **Success exits 0.** The "run `fha index` when convenient" reminder is
  advice text on a clean exit (source Notes text feeds `notes_fts`, SPEC
  §16/TOOLING §2), never a warning exit - a successful write is not a warning.
- **`clear-keyword` is the tool-mediated fix for a stray embedded keyword
  (#112).** AGENTS.md rule 3: the only allowed original-file changes are
  the spec'd rename/refile and "embedded metadata writes performed through
  fha tools" - never a hand edit. Before this verb there was a write path
  for a documents-root file's keywords (`fha process` embeds `SOURCE:` on
  first processing) but no CORRECTION path, so a keyword that turned out
  wrong - most often a leftover tag from before the file entered the
  archive - had no sanctioned way back out. `clear-keyword` finds the exact
  on-file spelling of `--keyword` (exiftool's list `-=` removal is an exact
  value match, so the text is read back off the file rather than trusted
  from the command line), removes it from whichever of Keywords/Subject it
  actually lives in, and - with `--replace-with` - adds the correction to
  that SAME field, never a new one the file did not already carry. Scope is
  documents-root only, matching lint's W131 (the check that finds these):
  see that check's docstring in lint.py for why a photos-root twin is a
  separate, undesigned decision. Uses the same `_lib.OriginalBackup` safety-
  copy discipline (TOOLING §13f) every other embedded write in this codebase
  follows - `fha process`'s SOURCE: embed, `fha photoindex tag-person`/
  `set-summary` - so a fifth call site does not quietly skip the one
  original-asset protection the other four share.
- **The asset picked has to be verified, not just located (PR #147 review).**
  A `files:` entry is a hand-editable trust boundary, so before any exiftool
  call `run_source_clear_keyword` re-derives the documents root and refuses a
  resolved target that lands outside it (a `..`/doubled-slash alias can
  otherwise escape a naive prefix check), refuses a directory target (exiftool
  happily writes into every file inside one), and refuses unless the file's
  OWN filename embeds this same source's S-id (SPEC §13's `_{S-id}` suffix -
  the same convention process.py's `_filename_has_source_id` checks) so
  inventory drift never edits a different source's file under this one's
  name. A missing asset also now reports `ok: False` (it used to leave the
  default `True` standing next to `status: not-found`) - a headless caller
  reading `Result.as_dict()` must never mistake that for success.
- **Exiftool exiting 0 is not proof the write landed (PR #147 review).**
  After a successful-looking exiftool call, `run_source_clear_keyword`
  re-reads the file's Keywords/Subject fields and confirms the removal and
  any addition actually took, rather than declaring success from the exit
  code alone - a race between the pre-write read and the write itself (e.g.
  something else touching the same file) can otherwise leave exiftool
  reporting success while nothing really changed.

CODE MAP
--------
  (locate + append are the shared _lib helpers: find_source_record_path finds
   the record; append_paragraph_to_section performs the bounded '## Notes' edit)
  _source_label               - "S-xxxx (title)" for human-facing messages
  run_source_note             - validate, locate, append; returns a _lib.Result
  run_source_edit_note        - rewrite ONE existing Notes paragraph (matched by
                                exact text via _lib.replace_paragraph_in_section)
  _parse_page_ranges          - '1-60,102' -> validated 1-based page numbers
  run_source_extract          - PDF text layer -> derived extracted-text companion
                                (pypdf optional; original never touched; M11.5)
  _run_exiftool_read_keyword_fields - {'keywords': […], 'subject': […]} raw read
                                (own thin wrapper - tools never import tools)
  _run_exiftool_edit_keyword_fields - remove/add exact (field, value) pairs in
                                one exiftool call, through OriginalBackup
  run_source_clear_keyword    - #112: clear/correct one documents-root asset
                                keyword; returns a _lib.Result
  _emit / _cmd_source_note / _cmd_source_edit_note / _cmd_source_extract /
  _cmd_source_clear_keyword / _make_group_help
  register / _standalone_main
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).parent))

import yaml

from _lib import (
    append_file_entry_to_record,
    append_paragraph_to_section,
    BackupRefused,
    claims_edit_problem,
    configure_utf8_stdout,
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    FhaConfigError,
    find_source_record_path,
    fmt_id_display,
    format_exiftool_error,
    FRONT_RE,
    frontmatter_fence_span,
    id_type_of,
    is_valid_id,
    is_working_copy,
    load_fha_yaml,
    normalize_id,
    OriginalBackup,
    pip_command,
    read_record,
    read_text_exact,
    reapply_newline,
    replace_paragraph_in_section,
    resolve_path,
    resolve_root_arg,
    Result,
    result_fail,
    write_text_exact_atomic,
    yaml_inline,)

configure_utf8_stdout()

# One '## Notes' line, matched after stripping a CRLF file's trailing '\r'
# (lines come from text.split('\n'), which leaves '\r' attached to the previous
# line - see run_source_note). Used only for the duplicate-heading safety check;
# the actual locate/append goes through the shared _lib section helpers.
_NOTES_HEADING_RE = re.compile(r'^##\s+Notes\s*$')


def _source_label(text: str, sid: str) -> str:
    """Return "S-xxxx (title)" for messages, falling back to the bare id.

    Best-effort only: a source with unparseable frontmatter still gets its
    note appended (this tool never touches frontmatter, so it does not need
    the frontmatter to be well-formed) - the label just degrades to the id
    alone.
    """
    fm = FRONT_RE.match(text)
    if fm:
        try:
            meta = yaml.safe_load(fm.group(1))
        except yaml.YAMLError:
            meta = None
        if isinstance(meta, dict):
            title = str(meta.get('title') or '').strip()
            if title:
                return f'{fmt_id_display(sid)} ({title})'
    return fmt_id_display(sid)


# ── The engine ────────────────────────────────────────────────────────────────

def run_source_note(
    archive_root: Path, source_id: str, *, text: str, dry_run: bool = False,
) -> Result:
    """Append one paragraph to a source record's `## Notes` section; return a Result.

    `data` is {'status': 'ok'|'dry-run'|'not-found'|'refused', 'source_id',
    'path'}; `changed` names the record on a live write. Exit codes: 0 for
    ok/dry-run, 1 for not-found (with the `fha find` next step), 3 for every
    refusal (invalid id, blank text, unreadable/unwritable file, or the
    Claims-block regression guard).

    Validation happens before any read; the section-bounding + Claims regression
    guard happen before any write; the file is either extended by one paragraph
    or left completely untouched.
    """
    result = Result(data={'status': None, 'source_id': None, 'path': None})

    def _refuse(status: str, message: str, *, next_step: str | None = None) -> Result:
        # Delegates to the shared _lib.result_fail (exit 3 / error-level) so the
        # refusal shape stays identical to confirm/claim/person's builders.
        return result_fail(result, status, message, next_step=next_step)

    if not (is_valid_id(source_id) and id_type_of(source_id) == 'S'):
        return _refuse(
            'refused',
            f'{source_id!r} is not a valid source ID. S-ids look like '
            'S-2b3c4d5e6f - an S followed by a dash and 10 characters from '
            'the archive alphabet.')
    sid = normalize_id(source_id)
    result.data['source_id'] = fmt_id_display(sid)

    note_body = (text or '').strip()
    if not note_body:
        return _refuse(
            'refused',
            f'No note text was given for {fmt_id_display(sid)} - nothing to '
            f'add. Run `fha source note {fmt_id_display(sid)} --text '
            '"your note here"`.')

    path = find_source_record_path(archive_root, sid)
    if path is None:
        return result_fail(
            result, 'not-found',
            f'No source record found for {fmt_id_display(sid)} under '
            f'{archive_root / "sources"} - check the id with '
            f'`fha find {fmt_id_display(sid)}`.',
            exit_code=EXIT_WARNINGS, level='warning',
            next_step='fha find ' + fmt_id_display(sid))
    result.data['path'] = str(path)

    try:
        text_in = read_text_exact(path)
    except OSError as e:
        return _refuse(
            'refused', f'cannot read {path}: {e}',
            next_step='Check the file is not open in another program and try again.')

    label = _source_label(text_in, sid)

    lines = text_in.split('\n')
    bounds = frontmatter_fence_span(lines)
    body_start = (bounds[1] + 1) if bounds is not None else 0

    heading_matches = [
        i for i in range(body_start, len(lines))
        if _NOTES_HEADING_RE.match(lines[i].rstrip('\r'))
    ]
    if len(heading_matches) > 1:
        return _refuse(
            'refused',
            f'{path.name} has more than one ## Notes heading, so the right '
            f'one to add to cannot be chosen safely. Open {path} and remove '
            'the extra heading by hand, then run `fha lint`. Nothing was written.')

    cr = '\r' if '\r\n' in text_in else ''
    # Strip any stray CR from the incoming --text so the shared appender (which
    # re-applies the record's own line ending) never doubles it into '\r\r'.
    paragraph = '\n'.join(ln.rstrip('\r') for ln in note_body.split('\n'))

    new_lines, _created, _old_content = append_paragraph_to_section(
        lines, body_start, 'Notes', paragraph, cr)
    new_text = '\n'.join(new_lines)

    # Belt-and-braces (see module docstring): the insertion is bounded to the
    # Notes section by construction, so this should never fire - but a
    # regression (sound Claims block -> broken) refuses rather than writes.
    # A Claims-less source (before_problem already not None) never trips it.
    before_problem = claims_edit_problem(text_in)
    after_problem = claims_edit_problem(new_text)
    if before_problem is None and after_problem is not None:
        return _refuse(
            'refused',
            f'Refusing to add the note to {label}: the edit would leave the '
            f'## Claims block broken ({after_problem}). Nothing was written. '
            f'This should not happen - open {path} and check it by hand, '
            'then run `fha lint`.')

    if dry_run:
        result.data['status'] = 'dry-run'
        result.add('info', f'[dry-run] Would add a note to {label}.')
        for dline in difflib.unified_diff(
            text_in.splitlines(), new_text.splitlines(),
            fromfile=f'{path} (before)', tofile=f'{path} (after)', lineterm='',
        ):
            result.add('info', dline)
        result.add('info', '[dry-run] No file written. Re-run without --dry-run to apply.')
        return result

    try:
        # Atomic: a source record is often the sole copy of its evidence, so a
        # mid-write failure must leave it untouched, not truncated.
        write_text_exact_atomic(path, reapply_newline(new_text, text_in))
    except OSError as e:
        return _refuse(
            'refused',
            f'cannot write {path}: {e}. Check the file is not open elsewhere '
            'and the folder is writable, then retry.')

    result.data['status'] = 'ok'
    result.note_changed(path)
    result.add('info', f'Added a note to {label}.', path=path)
    result.add('info',
               'Next: run `fha index` when convenient so search sees the new note.',
               next_step='fha index')
    return result


def run_source_edit_note(
    archive_root: Path, source_id: str, *, old_text: str, text: str,
    dry_run: bool = False,
) -> Result:
    """Replace ONE existing entry of a source's `## Notes` append-log; return
    a Result.

    The surgical counterpart of `run_source_note` (same shape as
    `person.run_edit_note`): the entry is identified by its EXACT current
    text, matched by the shared `_lib.replace_paragraph_in_section` - no
    match, or an ambiguous one, is a plain refusal and nothing is written.
    An empty replacement is refused too: removals stay a deliberate hand
    edit, the same nothing-ever-lost instinct as the appender. The Claims
    regression guard from `run_source_note` applies unchanged."""
    result = Result(data={'status': None, 'source_id': None, 'path': None})

    def _refuse(status: str, message: str, *, next_step: str | None = None) -> Result:
        return result_fail(result, status, message, next_step=next_step)

    if not (is_valid_id(source_id) and id_type_of(source_id) == 'S'):
        return _refuse(
            'refused',
            f'{source_id!r} is not a valid source ID. S-ids look like '
            'S-2b3c4d5e6f - an S followed by a dash and 10 characters from '
            'the archive alphabet.')
    sid = normalize_id(source_id)
    result.data['source_id'] = fmt_id_display(sid)

    if not (old_text or '').strip():
        return _refuse(
            'refused',
            'no entry was named - --old-text (the entry\'s current text) was empty.')
    if not (text or '').strip():
        return _refuse(
            'refused',
            'the replacement text was empty. To remove a note entirely, edit the '
            'record file itself - this tool only rewrites notes, never deletes them.')

    path = find_source_record_path(archive_root, sid)
    if path is None:
        return result_fail(
            result, 'not-found',
            f'No source record found for {fmt_id_display(sid)} under '
            f'{archive_root / "sources"} - check the id with '
            f'`fha find {fmt_id_display(sid)}`.',
            exit_code=EXIT_WARNINGS, level='warning',
            next_step='fha find ' + fmt_id_display(sid))
    result.data['path'] = str(path)

    try:
        text_in = read_text_exact(path)
    except OSError as e:
        return _refuse(
            'refused', f'cannot read {path}: {e}',
            next_step='Check the file is not open in another program and try again.')

    label = _source_label(text_in, sid)

    lines = text_in.split('\n')
    bounds = frontmatter_fence_span(lines)
    body_start = (bounds[1] + 1) if bounds is not None else 0

    heading_matches = [
        i for i in range(body_start, len(lines))
        if _NOTES_HEADING_RE.match(lines[i].rstrip('\r'))
    ]
    if len(heading_matches) > 1:
        return _refuse(
            'refused',
            f'{path.name} has more than one ## Notes heading, so the right '
            f'one to edit cannot be chosen safely. Open {path} and remove '
            'the extra heading by hand, then run `fha lint`. Nothing was written.')

    cr = '\r' if '\r\n' in text_in else ''
    new_lines, err = replace_paragraph_in_section(
        lines, body_start, 'Notes', old_text, text, cr)
    if err is not None:
        return _refuse('refused', err)
    new_text = '\n'.join(new_lines)

    before_problem = claims_edit_problem(text_in)
    after_problem = claims_edit_problem(new_text)
    if before_problem is None and after_problem is not None:
        return _refuse(
            'refused',
            f'Refusing to edit the note on {label}: the edit would leave the '
            f'## Claims block broken ({after_problem}). Nothing was written. '
            f'This should not happen - open {path} and check it by hand, '
            'then run `fha lint`.')

    if dry_run:
        result.data['status'] = 'dry-run'
        result.add('info', f'[dry-run] Would rewrite one note on {label}; '
                           'the rest of ## Notes is untouched.')
        for dline in difflib.unified_diff(
            text_in.splitlines(), new_text.splitlines(),
            fromfile=f'{path} (before)', tofile=f'{path} (after)', lineterm='',
        ):
            result.add('info', dline)
        result.add('info', '[dry-run] No file written. Re-run without --dry-run to apply.')
        return result

    try:
        # Atomic: a source record is often the sole copy of its evidence, so a
        # mid-write failure must leave it untouched, not truncated.
        write_text_exact_atomic(path, reapply_newline(new_text, text_in))
    except OSError as e:
        return _refuse(
            'refused',
            f'cannot write {path}: {e}. Check the file is not open elsewhere '
            'and the folder is writable, then retry.')

    result.data['status'] = 'ok'
    result.note_changed(path)
    result.add('info', f'Rewrote one note on {label}.', path=path)
    result.add('info',
               'Next: run `fha index` when convenient so search sees the change.',
               next_step='fha index')
    return result


# ── exiftool seams (#112) ────────────────────────────────────────────────────
#
# source.py keeps its own thin exiftool wrappers rather than importing
# process.py's or photoindex.py's (tools never import tools - TOOLING §15).
# Tests monkeypatch these two functions the same way test_process.py already
# monkeypatches process._run_exiftool_read_keywords/_run_exiftool_embed_source.

def _run_exiftool_read_keyword_fields(file_path: Path) -> dict[str, list[str]]:
    """Return {'keywords': […], 'subject': […]} - each field's raw values,
    kept SEPARATE rather than merged into one union.

    `clear-keyword` has to know which field a value actually lives in: exiftool's
    list `-=` removal operator needs both the exact value AND the exact tag, and
    a correction (`--replace-with`) must land in the SAME field the stray value
    came from rather than silently growing a field the file never carried.
    Raises RuntimeError if exiftool is missing or its output cannot be parsed -
    an environment problem the caller surfaces, distinct from "no such keyword".
    """
    cmd = ['exiftool', '-j', '-Keywords', '-Subject', str(file_path)]
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, encoding='utf-8')
    except FileNotFoundError as e:
        raise RuntimeError(format_exiftool_error('fha source clear-keyword')) from e
    if proc.returncode != 0:
        raise RuntimeError(f'exiftool failed reading {file_path.name}: {proc.stderr.strip()}')
    try:
        rows = json.loads(proc.stdout or '[]')
    except json.JSONDecodeError as e:
        raise RuntimeError(f'exiftool returned invalid JSON: {e}') from e
    out: dict[str, list[str]] = {'keywords': [], 'subject': []}
    if not rows:
        return out
    row = rows[0]
    for key, bucket in (('Keywords', 'keywords'), ('Subject', 'subject')):
        val = row.get(key)
        if val is None:
            continue
        for v in (val if isinstance(val, list) else [val]):
            out[bucket].append(str(v))
    return out


def _run_exiftool_edit_keyword_fields(
    file_path: Path, *, remove: list[tuple[str, str]], add: list[tuple[str, str]],
    backup: OriginalBackup,
) -> str | None:
    """Remove/add exact (field, value) pairs on one file in a single exiftool call.

    `remove`/`add` entries are `('keywords' | 'subject', exact_value)`. `-=` is
    exiftool's exact-value list-remove (the same technique `fha process`'s
    SOURCE: rollback uses); `+=` is the matching list-append. One call does
    both so a remove+replace lands atomically - never a moment where the file
    carries neither the old nor the new value because a second exiftool
    invocation failed in between.

    `backup` is the run's safety-copy policy (`_lib.OriginalBackup`, TOOLING
    §13f) - the same discipline `fha process`'s SOURCE: embed and `fha
    photoindex tag-person`/`set-summary` already follow for every other write
    into an original asset. Returns None on success, the stderr text (or the
    backup refusal text) on a per-file failure; raises RuntimeError only when
    exiftool itself is absent.
    """
    try:
        backup.ensure(file_path)
    except BackupRefused as e:
        return str(e)
    args = ([f'-{tag}-={value}' for tag, value in remove]
            + [f'-{tag}+={value}' for tag, value in add])
    cmd = ['exiftool'] + args + ['-overwrite_original_in_place', str(file_path)]
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, encoding='utf-8')
    except FileNotFoundError as e:
        raise RuntimeError(format_exiftool_error('fha source clear-keyword')) from e
    return None if proc.returncode == 0 else proc.stderr.strip()


def run_source_clear_keyword(
    archive_root: Path, fha_config: dict, source_id: str, *, keyword: str,
    replace_with: str | None = None, file: str | None = None,
    dry_run: bool = False, backup: OriginalBackup | None = None,
) -> Result:
    """Clear (or correct) one embedded Keywords/Subject value on a source's
    documents-root asset; return a Result.

    #112: a documents-root TIFF was found carrying a stray embedded `dc:subject`
    keyword naming a person with no connection to the document - a leftover
    from an earlier cataloguing pass (Lightroom, commonly) before the file
    ever entered the archive. AGENTS.md rule 3 restricts original-file changes
    to spec'd renames and "embedded metadata writes performed through fha
    tools" - but until this verb there was no tool-mediated way to correct a
    documents-root file's keyword once a wrong one turned up, so a stray tag
    like that one had no sanctioned way back out short of a hand edit. This
    is that correction path; `fha lint --with-exif` (W131) is what finds the
    candidates in the first place.

    The exact on-file spelling of `--keyword` is read back off the file
    (case-insensitive match) rather than trusted from the command line,
    because exiftool's `-=` list-remove needs an exact value: a keyword typed
    slightly differently on the command line would otherwise silently fail to
    remove anything while still reporting a plausible-looking command. A
    `--replace-with` correction lands in the SAME Keywords/Subject field the
    stray value was found in, never a new one the file did not already carry,
    and is skipped (not duplicated) if that field already holds it.

    Scope is documents-root only - the source must list a documents-root file
    in `files:` (matching W131's own scope; see that check's docstring in
    lint.py for why a photos-root twin is a separate, undesigned decision).
    `--file NAME` picks among several; with exactly one documents-root file
    listed, it is picked automatically.

    `data`: {'status': 'ok'|'dry-run'|'not-found'|'refused', 'source_id',
    'path', 'removed_from', 'added_to'}. Exit codes: 0 ok/dry-run · 1 record
    or asset not found on disk (or found but not a regular file) · 3 refusals
    (bad id, blank --keyword, no/ambiguous documents-root file, a target
    outside the documents root or belonging to a different source, keyword
    not currently present, exiftool or backup failure, or the post-write
    verification finding the change did not actually take).
    """
    result = Result(data={'status': None, 'source_id': None, 'path': None,
                          'removed_from': [], 'added_to': []})

    def _refuse(status: str, message: str, *, next_step: str | None = None) -> Result:
        return result_fail(result, status, message, next_step=next_step)

    if not (is_valid_id(source_id) and id_type_of(source_id) == 'S'):
        return _refuse(
            'refused',
            f'{source_id!r} is not a valid source ID. S-ids look like '
            'S-2b3c4d5e6f - an S followed by a dash and 10 characters from '
            'the archive alphabet.')
    sid = normalize_id(source_id)
    result.data['source_id'] = fmt_id_display(sid)

    keyword_text = (keyword or '').strip()
    if not keyword_text:
        return _refuse(
            'refused',
            f'No keyword was given for {fmt_id_display(sid)} - nothing to clear. '
            f'Run `fha source clear-keyword {fmt_id_display(sid)} --keyword '
            '"the exact text"`.')
    replacement = (replace_with or '').strip() or None

    record_path = find_source_record_path(archive_root, sid)
    if record_path is None:
        return result_fail(
            result, 'not-found',
            f'No source record found for {fmt_id_display(sid)} under '
            f'{archive_root / "sources"} - check the id with '
            f'`fha find {fmt_id_display(sid)}`.',
            exit_code=EXIT_WARNINGS, level='warning',
            next_step='fha find ' + fmt_id_display(sid))

    try:
        rec = read_record(record_path)
    except Exception:
        return _refuse(
            'refused',
            f'{record_path.name} could not be parsed - run `fha lint` for the '
            'specifics, fix the record, then retry.')
    if rec.get('parse_errors'):
        detail = '; '.join(msg for _, msg in rec['parse_errors'])
        return _refuse(
            'refused',
            f'{record_path.name} has malformed YAML ({detail}) - run `fha lint` '
            'for the exact spot, fix the record, then re-run.')
    meta = rec.get('meta') or {}

    entries = [e for e in (meta.get('files') or []) if isinstance(e, dict)]
    doc_entries = [
        e for e in entries
        if str(e.get('file', '')).replace('\\', '/').split('/', 1)[0].lower() == 'documents'
    ]
    if file:
        wanted = file.strip().lower()
        doc_entries = [
            e for e in doc_entries
            # Normalize to forward slashes before taking .name - a Windows-
            # authored alias ('documents\\deed_x.tif') has no separator
            # `Path` recognizes on POSIX, so an un-normalized .name would
            # return the whole string there and never match a bare filename
            # (the same normalization `run_source_extract` applies to its
            # own alias-directory arithmetic, for the identical reason).
            if wanted in (str(e.get('file', '')).lower(),
                         PurePosixPath(str(e.get('file', '')).replace('\\', '/')).name.lower())
        ]
        if not doc_entries:
            return _refuse(
                'refused',
                f'--file {file!r} does not match any documents-root file listed '
                f'on {fmt_id_display(sid)}. Run `fha find {fmt_id_display(sid)}` '
                'to see its inventory.')
    if not doc_entries:
        return _refuse(
            'refused',
            f"{fmt_id_display(sid)} lists no documents-root file - clear-keyword "
            "only corrects a documents-root asset's embedded keywords today. "
            "(The photos root has its own tools for embedded keywords, "
            "`fha photoindex`.)")
    if len(doc_entries) > 1:
        shown = ', '.join(str(e.get('file')) for e in doc_entries)
        return _refuse(
            'refused',
            f'{fmt_id_display(sid)} lists more than one documents-root file '
            f'({shown}) - clear-keyword cannot tell which one to correct. '
            'Name one with --file NAME.')

    alias = str(doc_entries[0].get('file'))
    abs_path = resolve_path(alias, fha_config, archive_root)
    result.data['path'] = alias

    # P1 (#147 review): the doc_entries filter above only checks that the
    # alias STARTS WITH 'documents' as text - a hand-edited entry like
    # 'documents/../../outside.tif' or 'documents//tmp/outside.tif' passes
    # that check, but resolve_path joins the alias onto the configured
    # documents root with plain path arithmetic and does not itself guard
    # against a '..' or doubled separator carrying the result outside that
    # root. Resolve both sides and refuse before touching the filesystem at
    # all if the target does not actually land beneath the documents root -
    # otherwise clear-keyword would read, back up, and rewrite an unrelated
    # file the record never named.
    documents_root = resolve_path('documents', fha_config, archive_root)
    try:
        resolved_target = abs_path.resolve()
        resolved_documents_root = documents_root.resolve()
    except OSError as e:
        return _refuse(
            'refused',
            f'{alias} could not be resolved to a real path ({e}). Check the '
            f'files: entry in {record_path.name} and try again.')
    if (resolved_target != resolved_documents_root
            and resolved_documents_root not in resolved_target.parents):
        return _refuse(
            'refused',
            f'{alias} resolves outside the configured documents folder '
            f'({resolved_documents_root}) - this looks like a hand-edited '
            f'files: entry gone wrong (a `..` segment, or a doubled slash). '
            f'Fix the entry in {record_path.name} by hand, then retry. '
            'Nothing was read or changed.')

    # P1 (#147 review): a files: entry can point at a folder rather than one
    # file (e.g. 'documents/deeds'), and exiftool accepts a directory operand
    # and applies its write arguments to every file inside it - especially
    # dangerous with originals_backup unset, where nothing is copied first.
    # Require a regular file before reading or writing any metadata.
    if abs_path.is_dir():
        return _refuse(
            'refused',
            f'{alias} is a folder, not a file - clear-keyword only edits one '
            f"document's metadata at a time and refuses to touch every file "
            f'inside a folder. Check the files: entry in {record_path.name}; '
            'if it should name one specific file, fix it there and retry.')
    if not abs_path.is_file():
        return result_fail(
            result, 'not-found',
            f'{alias} is not on disk - if it moved within the documents '
            'folder, `fha reconcile` re-ties it; if it lives on an external '
            'drive, plug it in.',
            exit_code=EXIT_WARNINGS, level='warning')

    # P1 (#147 review): confirm the file this would edit actually IS the
    # requested source's own asset before reading or writing anything.
    # Inventory drift - a hand-edited files: entry that still points at a
    # filename since reassigned to a different source, or a copy-pasted entry
    # that never matched - can point one source's clear-keyword at a
    # DIFFERENT source's file. The filename itself carries the answer: a
    # processed documents-root file's name ends `_{S-id}` (SPEC §13, the same
    # convention process.py's _filename_has_source_id checks, and the same
    # pattern _SID_SUFFIX_RE below already parses for `extract`), so compare
    # that embedded id against the source this command was actually asked to
    # correct rather than trusting the record's own files: entry blindly.
    filename_match = _SID_SUFFIX_RE.search(abs_path.stem)
    filename_sid = normalize_id(filename_match.group(1)) if filename_match else None
    if filename_sid != sid:
        carried = fmt_id_display(filename_sid) if filename_sid else 'no source id at all'
        return _refuse(
            'refused',
            f"{alias}'s own filename carries {carried}, not "
            f'{fmt_id_display(sid)} - {record_path.name}\'s files: entry '
            f'looks like inventory drift (it names a file that belongs to a '
            f"different source), and editing it would change the WRONG "
            'document\'s keywords. Run `fha reconcile` to re-tie sources to '
            'their actual files, then retry.')

    try:
        fields = _run_exiftool_read_keyword_fields(abs_path)
    except RuntimeError as e:
        return _refuse('refused', str(e))

    matches: list[tuple[str, str]] = [
        (tag, value)
        for tag in ('keywords', 'subject')
        for value in fields[tag]
        if value.strip().lower() == keyword_text.lower()
    ]
    if not matches:
        current = fields['keywords'] + fields['subject']
        shown = ', '.join(repr(v) for v in current[:10]) if current else '(none)'
        return _refuse(
            'refused',
            f'{alias} does not currently carry the keyword {keyword_text!r} - '
            f'nothing to clear. Its embedded keywords right now: {shown}.')

    add: list[tuple[str, str]] = []
    if replacement:
        tags_with_match = sorted({tag for tag, _ in matches})
        for tag in tags_with_match:
            if not any(v.strip().lower() == replacement.lower() for v in fields[tag]):
                add.append((tag, replacement))

    if dry_run:
        result.data['status'] = 'dry-run'
        removed_desc = ', '.join(f'{tag}: {value!r}' for tag, value in matches)
        result.add('info', f'[dry-run] Would remove {removed_desc} from {alias}.')
        if add:
            added_desc = ', '.join(f'{tag}: {value!r}' for tag, value in add)
            result.add('info', f'[dry-run] Would add {added_desc}.')
        elif replacement:
            result.add('info',
                       f'[dry-run] {replacement!r} is already present where it '
                       'would be added - nothing more to write.')
        result.add('info', '[dry-run] No file written. Re-run without --dry-run to apply.')
        return result

    if backup is None:
        backup = OriginalBackup(archive_root, fha_config)
    backup.announce()
    for level, text in backup.drain_messages():
        result.add(level, text)

    error = _run_exiftool_edit_keyword_fields(
        abs_path, remove=matches, add=add, backup=backup)
    for level, text in backup.drain_messages():
        result.add(level, text)
    if error is not None:
        return _refuse(
            'refused',
            f'exiftool could not update {alias}: {error}. Nothing was changed.')

    # P2 (#147 review): exiftool's exit code says the CALL succeeded, not that
    # the field actually ended up in the state this command asked for - a
    # race (something else touched the file's metadata between the read above
    # and this write) can leave exiftool reporting success while a value it
    # was told to remove is still there, or one it was told to add never
    # landed. Re-read the fields and confirm both sides actually took before
    # declaring success; a caller reading Result.as_dict() must never see
    # ok: true for a write that did not really happen.
    #
    # Compared EXACTLY (stripped, not case-folded): a case-only correction
    # removes one exact spelling and adds a DIFFERENT exact spelling that
    # happens to be case-insensitively "the same word" (e.g. 'margaret
    # hartley' -> 'Margaret Hartley') - a case-insensitive check here would
    # see the freshly-added value as proof the old one was never removed and
    # wrongly refuse a write that worked exactly as asked.
    try:
        after_fields = _run_exiftool_read_keyword_fields(abs_path)
    except RuntimeError as e:
        return _refuse(
            'refused',
            f'{alias} was written, but the change could not be verified ({e}). '
            'Run `fha lint --with-exif` to check its current state by hand.')
    not_removed = [
        (tag, value) for tag, value in matches
        if any(v.strip() == value.strip() for v in after_fields.get(tag, []))
    ]
    not_added = [
        (tag, value) for tag, value in add
        if not any(v.strip() == value.strip() for v in after_fields.get(tag, []))
    ]
    if not_removed or not_added:
        result.note_changed(abs_path)  # exiftool did write SOMETHING to the file
        problems = []
        if not_removed:
            problems.append('still carries ' + ', '.join(
                f'{tag}: {value!r}' for tag, value in not_removed))
        if not_added:
            problems.append('is missing ' + ', '.join(
                f'{tag}: {value!r}' for tag, value in not_added))
        return _refuse(
            'refused',
            f'exiftool reported success, but re-reading {alias} shows it '
            + ' and '.join(problems) + ' - something else may have changed '
            'the file at the same moment. Run `fha lint --with-exif` to see '
            'its current state, then retry.')

    result.data['status'] = 'ok'
    result.data['removed_from'] = sorted({tag for tag, _ in matches})
    result.data['added_to'] = sorted({tag for tag, _ in add})
    result.note_changed(abs_path)
    removed_desc = ', '.join(f'{tag}: {value!r}' for tag, value in matches)
    result.add('info', f'Removed {removed_desc} from {alias}.', path=abs_path)
    if add:
        added_desc = ', '.join(f'{tag}: {value!r}' for tag, value in add)
        result.add('info', f'Added {added_desc}.')
    result.add('info',
               'Next: run `fha lint --with-exif` if you want to confirm the '
               'warning is gone.',
               next_step='fha lint --with-exif')
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

def _emit(result: Result) -> int:
    for msg in result.messages:
        stream = sys.stderr if msg.level == 'error' else sys.stdout
        prefix = 'ERROR: ' if msg.level == 'error' else ''
        print(f'{prefix}{msg.text}', file=stream)
    return result.exit_code


def _cmd_source_extract(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args, command='fha source extract')
    if archive_root is None:
        return EXIT_FAILURE
    # strict=True: extraction resolves the PDF's alias through the configured
    # roots, so a malformed fha.yaml must REFUSE, not degrade to {} and silently
    # discard external document-root mappings. Permissive load would then resolve
    # the alias against the internal documents/ skeleton and, if a same-named PDF
    # happened to sit there, extract that unrelated file's text against this
    # source - reading the wrong evidence.
    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE
    return _emit(run_source_extract(
        archive_root, fha_config, source_id=args.source_id,
        pages=getattr(args, 'pages', None),
        dry_run=bool(getattr(args, 'dry_run', False))))


def _add_extract_arguments(sub: argparse._SubParsersAction) -> None:
    """Register the extract verb on a group subparser (shared by both mains)."""
    x = sub.add_parser(
        'extract',
        help="Dump a source PDF's embedded text layer into a derived companion file.",
        description=('Reads the source\'s PDF (its primary file, or the only PDF '
                     'it lists) and writes '
                     'the embedded text layer, page by page, into a '
                     '[Page N]-labeled companion the assistant can mine like a '
                     'transcript - seconds instead of reading hundreds of pages '
                     'by eye. The PDF itself is never touched. Needs the pypdf '
                     f'helper package ({pip_command("pypdf")}).'),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    x.add_argument('source_id', metavar='S-id',
                   help='The source whose PDF to extract (e.g. S-2b3c4d5e6f).')
    x.add_argument('--pages', metavar='RANGES',
                   help='Only these pages, e.g. `1-60` or `1-60,102,200-210` '
                        '(default: every page).')
    x.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS,
                   help='Archive root (auto-detected if omitted).')
    x.add_argument('--dry-run', action='store_true', dest='dry_run',
                   help='Preview the dump (page coverage, destination) without writing.')
    x.set_defaults(func=_cmd_source_extract)


def _cmd_source_clear_keyword(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args, command='fha source clear-keyword')
    if archive_root is None:
        return EXIT_FAILURE
    # strict=True, matching extract above: this resolves a documents-root
    # alias through the configured roots to find the file to write into, so a
    # malformed fha.yaml must refuse rather than silently fall back to the
    # internal documents/ skeleton and edit the wrong file's metadata.
    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE
    return _emit(run_source_clear_keyword(
        archive_root, fha_config, source_id=args.source_id,
        keyword=args.keyword, replace_with=getattr(args, 'replace_with', None),
        file=getattr(args, 'file', None),
        dry_run=bool(getattr(args, 'dry_run', False))))


_CLEAR_KEYWORD_DESCRIPTION = """\
Clear or correct one embedded keyword on a source's documents-root file.

  fha source clear-keyword S-2b3c4d5e6f --keyword "Margaret Hartley"
  fha source clear-keyword S-2b3c4d5e6f --keyword "Margaret Hartley" \\
      --replace-with "Margaret Cole"

Reads the file's own Keywords/Subject values, matches --keyword case-
insensitively, and removes the EXACT on-file value from wherever it actually
sits (IPTC Keywords or XMP dc:subject) - so a keyword typed slightly
differently on the command line refuses instead of silently doing nothing.
--replace-with corrects it in place, in the same field, rather than adding a
new one. Documents-root only (`fha lint --with-exif` W131 finds candidates);
--file NAME picks among several files listed on the source. Preview first
with --dry-run."""


def _add_clear_keyword_arguments(sub: argparse._SubParsersAction) -> None:
    """Register the clear-keyword verb on a group subparser (shared by both mains)."""
    ck = sub.add_parser(
        'clear-keyword',
        help="Clear or correct one embedded keyword on a source's documents-root file.",
        description=_CLEAR_KEYWORD_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ck.add_argument('source_id', metavar='S-id',
                    help='The source whose documents-root file to correct.')
    ck.add_argument('--keyword', metavar='TEXT', required=True,
                    help='The keyword to remove, exactly as it reads (matched '
                         'case-insensitively against the file).')
    ck.add_argument('--replace-with', metavar='TEXT', dest='replace_with',
                    help='Add this keyword in place of the one removed, in the '
                         'same field. Omit to just clear it.')
    ck.add_argument('--file', metavar='NAME',
                    help='Which documents-root file to correct, when the source '
                         'lists more than one (a filename or its files: alias).')
    ck.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS,
                    help='Archive root (auto-detected if omitted).')
    ck.add_argument('--dry-run', action='store_true', dest='dry_run',
                    help='Preview the change without writing.')
    ck.set_defaults(func=_cmd_source_clear_keyword)


def _cmd_source_note(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args, command='fha source note')
    if archive_root is None:
        return EXIT_FAILURE
    return _emit(run_source_note(
        archive_root, source_id=args.source_id, text=args.text,
        dry_run=bool(getattr(args, 'dry_run', False))))


def _cmd_source_edit_note(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args, command='fha source edit-note')
    if archive_root is None:
        return EXIT_FAILURE
    return _emit(run_source_edit_note(
        archive_root, source_id=args.source_id, old_text=args.old_text,
        text=args.text, dry_run=bool(getattr(args, 'dry_run', False))))


def _make_group_help(parser: argparse.ArgumentParser):
    """Bare `fha source` prints the group help and exits 2 (a verb is required)."""
    def _cmd(args: argparse.Namespace) -> int:
        parser.print_help()
        return 2
    return _cmd


# User-facing --help text (the module docstring stays developer-facing).
# ── fha source extract (M11.5) ───────────────────────────────────────────────

_EXTRACT_ROLE = 'extracted-text'
_NO_TEXT_PLACEHOLDER = '(no text layer on this page - read it with vision)'
_EXTRACT_ERROR_PLACEHOLDER = '(text extraction FAILED on this page - a parser error, not confirmed absence)'
# A filed document's name ends `_{S-id}.{ext}` (SPEC §13); the derived file
# slots its role suffix in front of the id, same grammar --more uses.
_SID_SUFFIX_RE = re.compile(r'_(S-[0-9a-hjkmnp-tv-z]{10})$', re.I)


def _parse_page_ranges(spec: str, page_count: int) -> tuple[list[int] | None, str | None]:
    """'1-5,12' -> sorted 1-based page numbers, or (None, plain problem).

    Ranges are inclusive and 1-based - the way a human reads the PDF viewer's
    page counter - and validated against the real page count so a typo'd
    range is a named refusal, never a silent empty selection.
    """
    pages: set[int] = set()
    for part in str(spec).split(','):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r'(\d+)(?:-(\d+))?', part)
        if not m:
            return None, (f'--pages {spec!r} is not a page list - write it like '
                          '`12`, `1-60`, or `1-60,102,200-210`.')
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else lo
        if lo < 1 or hi < lo:
            return None, (f'--pages {spec!r} has an impossible range ({part}) - '
                          'pages count from 1 and ranges run low-high.')
        if hi > page_count:
            return None, (f'--pages asks for page {hi}, but the PDF has only '
                          f'{page_count} page(s).')
        pages.update(range(lo, hi + 1))
    if not pages:
        return None, ('--pages selected nothing - write it like `1-60` or '
                      'leave it off to extract every page.')
    return sorted(pages), None


def run_source_extract(
    archive_root: Path, fha_config: dict, source_id: str,
    pages: str | None = None, dry_run: bool = False,
) -> Result:
    """Extract a source PDF's embedded text layer into a derived companion file.

    The why (usage-feedback item 1, owner approval 2026-07-23): many archived
    PDFs - archive.org county histories above all - carry an embedded text
    layer, and reading 600 pages by vision costs hours that a text dump costs
    seconds. The dump becomes a `derived: true` inventory entry with
    `role: extracted-text` beside the original (SPEC §14 models derived
    companions; SPEC §12.1: the original is never touched - content, name, or
    location), labeled `[Page N]` in the ORIGINAL's pagination so claims
    anchored `anchor: "page N"` (SPEC §8.4) line up in any copy. Stage B then
    mines the dump the way mine-transcript works a transcript.

    Honesty rule: a page with no text layer gets a placeholder line, never
    silent emptiness; a PDF with NO text on any selected page is a refusal
    with the vision/OCR next step, and no file is written - an empty derived
    file would read as "nothing on these pages," which is exactly wrong.

    pypdf is the borrowed engine (SPEC §5: "borrow the hard engines") and an
    OPTIONAL dependency like Pillow - absent, the verb refuses with the
    install command, nothing else degrades.

    `data`: {'status': 'ok'|'already'|'dry-run'|'not-found'|'refused'|
    'no-text'|'failed', 'source_id', 'extract_path', 'pages_selected',
    'pages_with_text'}. Exit codes: 0 ok/already/dry-run · 1 record or asset
    not found, working-copy, no text layer · 3 refusals (pypdf absent, bad
    id, ambiguous PDF, destination exists, unwritable).
    """
    result = Result(data={'status': None, 'source_id': None, 'extract_path': None,
                          'pages_selected': 0, 'pages_with_text': 0})

    if not (is_valid_id(source_id) and id_type_of(source_id) == 'S'):
        return result_fail(result, 'refused',
                           f'{source_id!r} is not a valid source ID - S-ids look '
                           'like S-fa1234567b.')
    sid = normalize_id(source_id)
    result.data['source_id'] = fmt_id_display(sid)

    # Working-copy check BEFORE the optional-import check, on purpose: the PDF
    # asset is not on a working-copy machine at all, so installing pypdf there
    # could never make extraction succeed. Refusing with the install command
    # first would send the user down a dead end - the actionable next step is
    # "run this on the main archive," and it must win over the pypdf refusal.
    if is_working_copy(archive_root):
        result.data['status'] = 'not-found'
        result.exit_code = EXIT_WARNINGS
        result.add('warning',
                   'This is a working copy - the PDF itself lives on the main '
                   'machine, so there is nothing here to extract from. Run this '
                   'on the main archive.')
        return result

    try:
        from pypdf import PdfReader
    except ImportError:
        return result_fail(result, 'refused',
                           'Reading PDF text needs the pypdf helper package, which '
                           'is not installed. Install it once with '
                           f'`{pip_command("pypdf")}`, then re-run.')

    record_path = find_source_record_path(archive_root, sid)
    if record_path is None:
        result.data['status'] = 'not-found'
        result.exit_code = EXIT_WARNINGS
        result.add('warning',
                   f'No source record {fmt_id_display(sid)} found under '
                   f'{archive_root / "sources"}.',
                   next_step=f'fha find {fmt_id_display(sid)}')
        return result

    try:
        rec = read_record(record_path)
    except Exception:
        return result_fail(result, 'refused',
                           f'{record_path.name} could not be parsed - run `fha lint` '
                           'for the specifics, fix the record, then retry.')

    # read_record does NOT raise on malformed YAML: it reports the problem
    # through parse_errors and hands back empty or partial meta (the same
    # channel reconcile.py's _iter_source_records reads). Selecting a PDF from
    # that half-read metadata is a trap - a damaged record with an unreadable
    # files: block would fall through to the misleading "lists no PDF" refusal
    # instead of naming the real cause. So refuse early on parse_errors, before
    # inspecting files: or choosing a PDF, and point at `fha lint`.
    if rec.get('parse_errors'):
        detail = '; '.join(msg for _, msg in rec['parse_errors'])
        return result_fail(result, 'refused',
                           f'{record_path.name} has malformed YAML ({detail}) - '
                           'run `fha lint` for the exact spot, fix the record, '
                           'then re-run `fha source extract`.')
    meta = rec.get('meta') or {}

    entries = [e for e in (meta.get('files') or []) if isinstance(e, dict)]
    if any(str(e.get('role', '')) == _EXTRACT_ROLE for e in entries):
        existing = next(e for e in entries if str(e.get('role', '')) == _EXTRACT_ROLE)
        result.data['status'] = 'already'
        result.add('info',
                   f'{fmt_id_display(sid)} already has an extracted-text companion '
                   f'({existing.get("file")}). To re-extract, delete that file and '
                   'its files: entry first - extraction never overwrites.')
        return result

    # The PDF to read: the primary entry when it is a PDF, else the single
    # non-derived PDF in the inventory. Never a derived file (extracting an
    # extraction is meaningless), never a guess between several.
    pdf_entries = [
        e for e in entries
        if str(e.get('file', '')).lower().endswith('.pdf')
        and str(e.get('derived', '')).lower() not in ('true', '1')
    ]
    primary_pdfs = [e for e in pdf_entries if str(e.get('role', '')) == 'primary']
    if len(primary_pdfs) > 1:
        # Two PDFs both marked role: primary is a hand-edit mistake, not a
        # choice the tool may make for the user - silently taking the first
        # could attach text dumped from the WRONG PDF, which then anchors
        # claims to the wrong pages. Refuse and name both so the fix is obvious.
        shown = ', '.join(str(e.get('file')) for e in primary_pdfs)
        return result_fail(result, 'refused',
                           f'{fmt_id_display(sid)} marks more than one PDF as '
                           f'role: primary ({shown}) - extraction cannot tell which '
                           'one to read. Leave exactly one PDF marked role: primary '
                           'in the record, then re-run.')
    if primary_pdfs:
        pdf_entry = primary_pdfs[0]
    elif len(pdf_entries) == 1:
        pdf_entry = pdf_entries[0]
    elif not pdf_entries:
        return result_fail(result, 'refused',
                           f'{fmt_id_display(sid)} lists no PDF in its files: '
                           'inventory - this extraction reads PDF text layers only. '
                           'For an image scan, read it with vision '
                           '(the process-source page-window doctrine).')
    else:
        shown = ', '.join(str(e.get('file')) for e in pdf_entries)
        return result_fail(result, 'refused',
                           f'{fmt_id_display(sid)} lists more than one PDF ({shown}) '
                           'and none is role: primary - mark the one to extract as '
                           'primary in the record, then re-run.')

    alias = str(pdf_entry.get('file'))
    pdf_path = resolve_path(alias, fha_config, archive_root)
    if not pdf_path.exists():
        result.data['status'] = 'not-found'
        result.exit_code = EXIT_WARNINGS
        result.add('warning',
                   f'{alias} is not on disk - if it moved within the documents '
                   'folder, `fha reconcile` re-ties it; if it lives on an external '
                   'drive, plug it in.')
        return result

    try:
        reader = PdfReader(str(pdf_path))
        if getattr(reader, 'is_encrypted', False):
            return result_fail(result, 'refused',
                               f'{pdf_path.name} is password-protected - remove the '
                               'password with a PDF tool (save an unlocked copy '
                               'OUTSIDE the archive, then attach it with `fha process '
                               '--more`), or read the pages with vision instead. The '
                               'original stays as it is.')
        page_count = len(reader.pages)
    except Exception as e:
        return result_fail(result, 'refused',
                           f'{pdf_path.name} could not be opened as a PDF ({e}) - '
                           'check the file opens in a PDF viewer; if it is '
                           'damaged, restore it from a backup.')
    if page_count == 0:
        return result_fail(result, 'refused',
                           f'{pdf_path.name} has no pages at all - the file may be '
                           'damaged. Check it opens in a PDF viewer; if not, restore '
                           'it from a backup.')

    if pages is not None:
        selected, problem = _parse_page_ranges(pages, page_count)
        if problem is not None:
            return result_fail(result, 'refused', problem)
    else:
        selected = list(range(1, page_count + 1))
    result.data['pages_selected'] = len(selected)

    # Destination: beside the original, role-suffixed per the §13 grammar.
    stem = pdf_path.stem
    m = _SID_SUFFIX_RE.search(stem)
    base = stem[:m.start()] if m else stem
    extract_name = f'{base}-{_EXTRACT_ROLE}_{fmt_id_display(sid)}.md'
    extract_path = pdf_path.parent / extract_name
    result.data['extract_path'] = str(extract_path)
    if extract_path.exists():
        return result_fail(result, 'refused',
                           f'{extract_name} already exists beside the PDF - '
                           'extraction never overwrites. Delete it first if you '
                           'want a fresh dump.')

    parts: list[str] = [
        f'# Extracted text - {meta.get("title") or fmt_id_display(sid)} '
        f'[{fmt_id_display(sid)}]',
        '',
        f'Machine-extracted from the embedded text layer of {pdf_path.name} by '
        '`fha source extract`. A derived working copy - the PDF stays the '
        "original; page numbers are the PDF's own, so claim anchors "
        '(`anchor: "page N"`) line up in any copy.',
        '',
    ]
    pages_with_text = 0
    empty_pages: list[int] = []
    error_pages: list[int] = []
    for page_no in selected:
        parts.append(f'[Page {page_no}]')
        try:
            text = (reader.pages[page_no - 1].extract_text() or '').strip()
        except Exception as page_exc:
            # A parser failure on ONE page is not "this page has no text" - it is
            # an unknown. Recording it as empty would let a pypdf error masquerade
            # as confirmed absence and quietly drop real evidence, so it is tracked
            # separately, marked distinctly in the dump, and surfaced as a warning.
            error_pages.append(page_no)
            parts.append(f'{_EXTRACT_ERROR_PLACEHOLDER} ({page_exc})')
            parts.append('')
            continue
        if text:
            pages_with_text += 1
            parts.append(text)
        else:
            empty_pages.append(page_no)
            parts.append(_NO_TEXT_PLACEHOLDER)
        parts.append('')
    result.data['pages_with_text'] = pages_with_text
    result.data['error_pages'] = error_pages

    if pages_with_text == 0:
        if error_pages:
            # Not a scanned-image PDF - the extractor FAILED. Refuse rather than
            # write a dump that labels every page as empty: a parser error read
            # as confirmed absence would send the human away from real evidence.
            shown = (', '.join(str(p) for p in error_pages[:10])
                     + (' …' if len(error_pages) > 10 else ''))
            result.data['status'] = 'extract-error'
            result.exit_code = EXIT_WARNINGS
            result.add('warning',
                       f'{pdf_path.name}: text extraction FAILED on every selected '
                       f'page ({shown}) - a parser error, not confirmed absence of '
                       'text, so nothing was written (an empty dump would wrongly '
                       'read as "nothing on these pages"). The PDF may be damaged or '
                       'use an unsupported encoding; try repairing/re-saving it, or '
                       'read the pages with vision.')
            return result
        result.data['status'] = 'no-text'
        result.exit_code = EXIT_WARNINGS
        result.add('warning',
                   f'{pdf_path.name} has no embedded text layer on any of the '
                   f'{len(selected)} selected page(s) - it is a scanned-image '
                   'PDF. Nothing was written (an empty dump would read as '
                   '"nothing on these pages", which is wrong). Read the pages '
                   'with vision instead (the process-source page-window '
                   'doctrine), or OCR the PDF first.')
        return result

    # The dump lives beside the PDF, so its alias is the PDF's alias directory
    # plus the new filename. Normalize separators to forward slashes FIRST:
    # a stored alias may use Windows backslashes ('documents\census\x.pdf'),
    # and on POSIX Path() does not treat '\' as a separator, so Path(alias).parent
    # would be '.' and the recorded entry would collapse to a BARE filename.
    # Indexing then resolves that from the archive root and cannot find the dump.
    # PurePosixPath on the already-normalized alias keeps the full
    # 'documents/.../<name>' form on every platform and for every root segment.
    alias_dir = PurePosixPath(alias.replace('\\', '/')).parent
    entry_file = str(alias_dir / extract_name)
    entry_lines = [
        f'  - file: {yaml_inline(entry_file)}',
        f'    role: {_EXTRACT_ROLE}',
        '    derived: true',
    ]

    coverage = (f'{pages_with_text} of {len(selected)} selected page(s) carry text'
                + (f' (no text layer on: '
                   f'{", ".join(str(p) for p in empty_pages[:10])}'
                   f'{" …" if len(empty_pages) > 10 else ""})' if empty_pages else '')
                + (f' (extraction FAILED on: '
                   f'{", ".join(str(p) for p in error_pages[:10])}'
                   f'{" …" if len(error_pages) > 10 else ""})' if error_pages else ''))

    if dry_run:
        result.data['status'] = 'dry-run'
        result.add('info', f'[dry-run] Would write {extract_name} - {coverage}.')
        result.add('info', f'[dry-run] Would add a files: entry (role: '
                           f'{_EXTRACT_ROLE}, derived: true) to {record_path.name}.')
        result.add('info', '[dry-run] Nothing written. Re-run without --dry-run to apply.')
        return result

    # Write atomically (temp file + os.replace) rather than straight into
    # extract_path: a mid-write failure - the disk fills, the process is killed -
    # must not leave a partial dump behind. A truncating write would, and the
    # existence guard above would then refuse the very retry this failure message
    # tells the user to make, stranding them on an incomplete, uninventoried file
    # they have to hunt down and delete by hand. The atomic helper leaves the
    # destination either absent or complete, never torn.
    try:
        write_text_exact_atomic(extract_path, '\n'.join(parts))
    except OSError as e:
        return result_fail(result, 'failed',
                           f'could not write {extract_name}: {e} - check the folder '
                           'is writable, then retry (nothing was left behind).')

    before: str | None = None
    try:
        before = read_text_exact(record_path)
        after = append_file_entry_to_record(before, entry_lines)
        # Atomic, matching the extract write above: the record is often the sole
        # copy of its evidence, so a mid-write failure must leave it untouched.
        write_text_exact_atomic(record_path, reapply_newline(after, before))
    except Exception as e:
        # Roll back BOTH sides of the partial write. The stray dump's cleanup
        # must NEVER be able to skip the record restore: on Windows a virus
        # scanner or backup tool can hold the just-written dump open and make
        # unlink() raise, and the record - possibly left half-written by the
        # truncating write above - is the one that MUST come back. So guard the
        # unlink on its own and press on to the restore either way.
        stray_dump: Path | None = None
        try:
            extract_path.unlink(missing_ok=True)
        except OSError:
            stray_dump = extract_path

        # The atomic write above leaves the record either fully updated or
        # untouched, never torn - but if it landed the new entry before some
        # LATER step raised, restore the pristine text we still hold (the
        # attach_more discipline), also atomically, and only claim a clean
        # rollback when the restore actually landed.
        restored = before is None
        if before is not None:
            try:
                write_text_exact_atomic(record_path, before)
                restored = True
            except OSError:
                restored = False

        problems: list[str] = []
        if stray_dump is not None:
            problems.append(
                f'the extracted-text file {stray_dump.name} was written but could '
                'not be removed (a virus scanner or backup tool may be holding it '
                f'open) - delete {stray_dump} by hand once nothing is using it')
        if not restored:
            problems.append(
                f'the record {record_path.name} could not be restored, so it may be '
                'left partially written - restore it from git or your last backup '
                'before doing anything else')

        if not problems:
            return result_fail(result, 'failed',
                               f'could not add the files: entry to {record_path.name} '
                               f'({e}) - nothing was kept (the dump and the record '
                               'edit were both rolled back).')
        return result_fail(result, 'failed',
                           f'could not add the files: entry to {record_path.name} '
                           f'({e}), and cleanup did not fully finish: '
                           + '; '.join(problems) + '.')

    result.data['status'] = 'ok'
    result.note_changed(extract_path)
    result.note_changed(record_path)
    result.add('info', f'Wrote {extract_name} - {coverage}.')
    result.add('info', f'Added a files: entry (role: {_EXTRACT_ROLE}, derived: true) '
                       f'to {record_path.name}.')
    if error_pages:
        # Some pages carried text, so the dump is worth keeping - but a page the
        # extractor errored on is marked FAILED (not empty) in it, and the run
        # exits nonzero so the failure is not mistaken for confirmed absence.
        shown = (', '.join(str(p) for p in error_pages[:10])
                 + (' …' if len(error_pages) > 10 else ''))
        result.exit_code = EXIT_WARNINGS
        result.add('warning',
                   f'Text extraction FAILED on page(s) {shown} - marked as errors '
                   'in the dump (not "no text"). Read those pages with vision; do '
                   'not treat them as blank.')
    result.add('info',
               'Next: mine the dump like a transcript, anchoring every claim '
               '`anchor: "page N"`; run `fha index` so search sees the text.',
               next_step='fha index')
    return result


_CLI_DESCRIPTION = """\
Update a source record directly - the deterministic source-field write-backs.

  fha source note S-2b3c4d5e6f --text "..."
  fha source edit-note S-2b3c4d5e6f --old-text "..." --text "..."
  fha source extract S-2b3c4d5e6f [--pages 1-60]
  fha source clear-keyword S-2b3c4d5e6f --keyword "..." [--replace-with "..."]

note appends a hand-written paragraph to a source's ## Notes section;
edit-note rewrites one existing paragraph there (named by its exact current
text) and leaves the rest untouched; extract dumps the source PDF's embedded
text layer into a derived [Page N]-labeled companion file (needs the pypdf
helper package); clear-keyword removes (or corrects) one embedded keyword on
a documents-root file, the tool-mediated fix for a stray tag `fha lint
--with-exif` (W131) finds."""

_NOTE_DESCRIPTION = """\
Add a note to a source - appended to the end of its ## Notes section.

  fha source note S-2b3c4d5e6f --text "Found in Grandma's cedar chest, 2024."

Always append-only: an existing note is never edited or removed, and nothing
outside ## Notes changes (the frontmatter and ## Claims are never touched).
A source marked superseded still accepts notes - the audit trail stays open.
Preview the change first with --dry-run."""


def _add_note_arguments(sub: argparse._SubParsersAction) -> None:
    """Register the note verb on a group subparser (shared by both mains)."""
    n = sub.add_parser(
        'note',
        help="Append a paragraph to a source record's ## Notes section.",
        description=_NOTE_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    n.add_argument('source_id', metavar='S-id',
                   help='The source to update (e.g. S-2b3c4d5e6f).')
    n.add_argument('--text', metavar='TEXT', required=True,
                   help='The note to add, in your own words.')
    n.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS,
                   help='Archive root (auto-detected if omitted).')
    n.add_argument('--dry-run', action='store_true', dest='dry_run',
                   help='Preview the change without writing.')
    n.set_defaults(func=_cmd_source_note)


_EDIT_NOTE_DESCRIPTION = """\
Rewrite ONE existing ## Notes paragraph - the rest of the section is untouched.

  fha source edit-note S-2b3c4d5e6f \\
      --old-text "Found in the cedar chest." --text "Found in Grandma's cedar chest, 2024."

The note is named by its exact current text (--old-text); if that text is not
found (someone edited the file since), or appears more than once, nothing is
written and the message says so. Deleting a note stays a hand edit to the
record file - this only rewrites. Preview first with --dry-run."""


def _add_edit_note_arguments(sub: argparse._SubParsersAction) -> None:
    """Register the edit-note verb on a group subparser (shared by both mains)."""
    en = sub.add_parser(
        'edit-note',
        help="Rewrite one existing paragraph of a source's ## Notes.",
        description=_EDIT_NOTE_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    en.add_argument('source_id', metavar='S-id',
                    help='The source whose note is being corrected.')
    en.add_argument('--old-text', metavar='TEXT', required=True, dest='old_text',
                    help="The note's current text, exactly as it stands.")
    en.add_argument('--text', metavar='TEXT', required=True,
                    help='The corrected note text.')
    en.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS,
                    help='Archive root (auto-detected if omitted).')
    en.add_argument('--dry-run', action='store_true', dest='dry_run',
                    help='Preview the change without writing.')
    en.set_defaults(func=_cmd_source_edit_note)


def register(subs: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register 'source' onto the main fha parser."""
    p = subs.add_parser(
        'source',
        help='Source-record write-backs: note (append), edit-note (rewrite one), '
             'extract (PDF text layer), clear-keyword (fix a stray keyword)',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    sub = p.add_subparsers(dest='source_command', metavar='SUBCOMMAND')
    _add_note_arguments(sub)
    _add_edit_note_arguments(sub)
    _add_extract_arguments(sub)
    _add_clear_keyword_arguments(sub)
    p.set_defaults(func=_make_group_help(p))
    return p


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha source',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    sub = parser.add_subparsers(dest='source_command', metavar='SUBCOMMAND')
    _add_note_arguments(sub)
    _add_edit_note_arguments(sub)
    _add_extract_arguments(sub)
    _add_clear_keyword_arguments(sub)
    parser.set_defaults(func=_make_group_help(parser))
    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == '__main__':
    sys.exit(_standalone_main())
