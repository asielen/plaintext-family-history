#!/usr/bin/env python3
"""
source.py - fha source: deterministic source-record write-backs (TOOLING §3c sibling).

  fha source note S-id --text TEXT [--dry-run] [--root PATH]
  fha source edit-note S-id --old-text TEXT --text TEXT [--dry-run] [--root PATH]
  fha source extract S-id [--pages RANGES] [--copy LABEL] [--dry-run] [--root PATH]
  fha source clear-keyword S-id --keyword TEXT [--replace-with TEXT] [--file NAME] [--dry-run] [--root PATH]
  fha source set-primary S-id --file NAME [--dry-run] [--root PATH]
  fha source merge KEEPER RETIREE [--dry-run] [--root PATH]

A source's `## Notes` section is the human-written free-text channel SPEC §14
reserves for "the story behind it, context, or where the original is kept" -
until this tool, adding to it meant opening the file by hand. `fha source
note` is the safe one-line way to jot something down without risking the
`## Claims` fence or the frontmatter above it: paste in a sentence from the
phone, on the porch, mid-research-session, and the tool finds the record,
appends the sentence as its own paragraph, and touches nothing else.

This module deliberately opens the `fha source` namespace - future
source-field verbs would live here. Six verbs ship now: `note` (append),
`edit-note` (rewrite one existing paragraph - the workbench's per-entry edit
button; see run_source_edit_note), `extract` (dump a PDF's embedded text
layer into a derived [Page N]-labeled companion - see run_source_extract),
`clear-keyword` (#112: correct a documents-root asset's embedded
Keywords/Subject value - see run_source_clear_keyword), and `set-primary`
(#200: make a photo's front, not its back, the record's main image - see
run_source_set_primary), and `merge` (fold a second record of one piece of
evidence into the first: see run_source_merge and the merge rules below).

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
  from the command line), removes it from whichever keyword field it
  actually lives in, and - with `--replace-with` - adds the correction to
  every keyword field the file's format takes (IPTC Keywords, XMP
  dc:subject, Lightroom's lr:hierarchicalSubject - #252, the rule every fha
  keyword write follows). Scope is
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
- **`set-primary` edits a record's frontmatter, but only its `files:` list
  (#200).** The body-only rule above is for the note verbs. set-primary moves
  `is_primary: true` from one entry to another, one
  `key: value` line at a time, and lets the YAML parse referee the result:
  the rewritten frontmatter must parse to exactly the planned list, with
  every other field and everything below the frontmatter unchanged, or
  nothing is written. A photo is never touched (it is a record fix, not an
  embedded-metadata write).
- **`SOURCE:` is not an ordinary keyword, and case-only corrections must not
  vanish (PR #147 review).** `clear-keyword` refuses a `--keyword`/
  `--replace-with` value matching the SPEC §20 rule 3 SOURCE: shape - that
  keyword is the third link tying a file to its record, not a stray tag this
  verb corrects, and nothing downstream catches a pure removal of it.
  Separately, the "is the replacement already present" duplicate check now
  looks at each field's state AFTER this operation's own removal, not the
  as-read state - otherwise a case-only correction ('margaret hartley' ->
  'Margaret Hartley') sees its own about-to-be-removed old value as a
  case-insensitive "match", skips adding the correction, and the keyword
  vanishes entirely while the command still reports success.
- **`merge` reuses `--more`'s naming and undo, through `_lib`.** A file
  coming over from a merged source is named by the same SPEC §13 rule a
  `fha process --more` attachment is (`_lib.attachment_base`), and its
  keyword goes through the one keyword writer; the byte-exact undo copies
  and the reverse-order undo runner are `_lib`'s too. They moved there from
  process.py so this verb could share them (tools never import tools), and
  process.py keeps its old names as aliases.
- **`merge` refuses before it writes.** Everything a write could fail on
  (both records readable, the people check, every file on disk and its
  keyword read, the keeper's new text re-parsed to exactly the planned
  frontmatter and claims) is checked first, so `--dry-run` gives the same
  answer as the real run. The writes then each push an undo step, and a
  failure part-way runs them in reverse.
- **`merge`'s "different people" rule is the simplest one the evidence
  supports.** The two records' accepted claims each name a set of people
  (`persons:` and `roles:`); when both sets are non-empty and share no one,
  the records are taken to be about different people and the merge is
  refused, naming them. Suggested claims do not count: nobody has confirmed
  them yet.

CODE MAP
--------
  (locate + append are the shared _lib helpers: find_source_record_path finds
   the record; append_paragraph_to_section performs the bounded '## Notes' edit)
  _source_label               - "S-xxxx (title)" for human-facing messages
  run_source_note             - validate, locate, append; returns a _lib.Result
  run_source_edit_note        - rewrite ONE existing Notes paragraph (matched by
                                exact text via _lib.replace_paragraph_in_section)
  _parse_page_ranges          - '1-60,102' -> validated 1-based page numbers
  _copy_label                 - a files: entry's `copy:` label, '' when unlabeled
  _dump_source_pdf            - #280: which PDF an existing dump was made from
  _choose_extract_pdf         - #280: which PDF to read (primary, the uncopied
                                primary, or the --copy one); never a guess
  _measured_pieces            - #281: a page's pieces, minus leaders/URLs/scripts
  _is_suspect / _long_pieces_flag - #281: long or glued pieces; the long sign
  _words_run_together         - #281: the trigger - worth a second reading?
  _suspect_chars / _hopeless_share - #281: suspicious text; 40+ letter pieces
  _agrees                     - #281: the second reading confirms GLUED pieces
  _collapse_layout_text       - #281: second-reader text with its padding removed
  _reading_problem / _same_reading - #281: a second reading changes only spacing
  _clears_suspects            - #281: a second reading clears half the suspects
  _layout_reading             - #281: pypdf layout mode, rotated text kept
  _pdftotext_path / _PdftotextPages / _pdftotext_reading - #281: poppler
                                pdftotext, once per block of pages, when on PATH
  _extract_page_text          - #281: one page: default, then pdftotext or layout
  run_source_extract         - PDF text layer -> derived extracted-text companion
                                (pypdf optional; original never touched)
  _run_exiftool_read_keyword_fields - {'keywords': […], 'subject': […]} raw read
                                (own thin wrapper - tools never import tools)
  _run_exiftool_edit_keyword_fields - remove/add exact (field, value) pairs in
                                one exiftool call, through OriginalBackup
  run_source_clear_keyword    - #112: clear/correct one documents-root asset
                                keyword; returns a _lib.Result
  _files_block_items          - #200: each files: entry's lines, for text surgery
  _set_entry_key              - set one key line of one files: entry in place
  run_source_set_primary      - #200: move is_primary: true to the named
                                photo; returns a _lib.Result
  _MergeRefused               - a merge refusal found before any write
  _merge_read_keywords / _merge_write_keywords - exiftool seams (the _lib
                                keyword reader/writer; tests patch them)
  _accepted_claim_people      - everyone named in a record's accepted claims
  _extend_frontmatter_list    - add items to one frontmatter list, as text
  _merge_claims_text          - copy the retiree's claim items into the keeper's
  _plan_merge_files           - each retiree file: new name, keyword, entry
  _merge_notes_paragraph / _differing_fields - the dated Notes paragraph
  run_source_merge            - fold one source into another; returns a Result
  _emit / _cmd_source_note / _cmd_source_edit_note / _cmd_source_extract /
  _cmd_source_clear_keyword / _cmd_source_set_primary / _cmd_source_merge /
  _make_group_help
  register / _standalone_main
"""

from __future__ import annotations

import argparse
import copy
import datetime
import difflib
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from collections import Counter
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).parent))

import yaml

from _lib import (
    _find_claims_block,
    attachment_base,
    claims_in_text,
    create_section_at_eof,
    discard_undo_copies,
    EMBEDDED_KEYWORD_DOCUMENT_SUFFIXES,
    EXIT_ERRORS,
    ExiftoolUnavailableError,
    files_entry_role,
    frontmatter_key_line_indexes,
    link_field_refs,
    path_to_alias,
    read_embedded_keywords,
    record_display_names,
    restore_undo_copy,
    run_undo_steps,
    slugify,
    SOURCE_FRONTMATTER_KEY_ORDER,
    scan_person_merge_survivors,
    private_fence_spans,
    take_undo_copy,
    without_retired_sid,
    yaml_inline,
    append_file_entry_to_record,
    append_paragraph_to_section,
    claims_edit_problem,
    configure_utf8_stdout,
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    FhaConfigError,
    find_source_record_path,
    fmt_id_display,
    FRONT_RE,
    frontmatter_edit_problem,
    frontmatter_fence_span,
    id_type_of,
    is_valid_id,
    is_working_copy,
    keyword_fields_from_row,
    expected_keyword_fields,
    iptc_charset_plan,
    keyword_tags_for,
    load_fha_yaml,
    normalize_id,
    OriginalBackup,
    parse_frontmatter_strict,
    PHOTO_EXTENSIONS,
    files_entry_is_main,
    parse_media_filename,
    pip_command,
    read_keyword_row,
    read_record,
    read_text_exact,
    reapply_newline,
    replace_paragraph_in_section,
    resolve_path,
    resolve_root_arg,
    Result,
    result_fail,
    variant_role,
    write_embedded_keywords,
    write_text_exact_atomic,
    convert_written_links,
    link_writer,
    plan_rename_links,
    rename_link_writer,
    rename_links_message,
    safe_load_yaml,
    yaml_inline,)

configure_utf8_stdout()

# One '## Notes' line, matched after stripping a CRLF file's trailing '\r'
# (lines come from text.split('\n'), which leaves '\r' attached to the previous
# line - see run_source_note). Used only for the duplicate-heading safety check;
# the actual locate/append goes through the shared _lib section helpers.
_NOTES_HEADING_RE = re.compile(r'^##\s+Notes\s*$')

# The embedded `SOURCE: S-xxxx` identity keyword (SPEC §20 rule 3 - the third
# redundant link tying a source to its file, alongside the filename's own
# `_{S-id}` suffix and the record's files: entry). source.py keeps its own
# copy rather than importing process.py's/lint.py's identical pattern (tools
# never import tools - TOOLING §15); `clear-keyword` uses it to refuse
# touching this specific keyword shape (#147 review) - it corrects an
# ORDINARY stray tag, never the marker that makes a file findable as a source
# in the first place.
_SOURCE_KEYWORD_RE = re.compile(r'^SOURCE:\s*(S-[0-9a-hjkmnp-tv-z]{10})$', re.I)


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
            meta = safe_load_yaml(fm.group(1))
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
            f'{source_id!r} is not a valid source ID. s-ids look like '
            's-2b3c4d5e6f: an s, a dash and 10 characters from '
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
    # A link in the note is written in the archive's link form (SPEC §18).
    new_text = convert_written_links(text_in, new_text, link_writer(archive_root))

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
            f'{source_id!r} is not a valid source ID. s-ids look like '
            's-2b3c4d5e6f: an s, a dash and 10 characters from '
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
    new_lines, reason, err = replace_paragraph_in_section(
        lines, body_start, 'Notes', old_text, text, cr,
        noun='note', file_hint=str(path))
    if new_lines is None:
        result.data['reason'] = reason
        return _refuse('refused', err)
    new_text = '\n'.join(new_lines)
    # A link in the note is written in the archive's link form (SPEC §18).
    new_text = convert_written_links(text_in, new_text, link_writer(archive_root))

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

# The three keyword fields, by the short names `clear-keyword` reports them
# under (`removed_from`/`added_to`) and the exiftool tag each one is (#252).
_KEYWORD_BUCKETS: tuple[tuple[str, str], ...] = (
    ('keywords', 'IPTC:Keywords'),
    ('subject', 'XMP-dc:Subject'),
    ('hierarchical', 'XMP-lr:HierarchicalSubject'),
)


def _buckets_for(path: Path) -> list[str]:
    """The fields a keyword ADDED to this file goes into (its format's set)."""
    tags = keyword_tags_for(path)
    return [bucket for bucket, tag in _KEYWORD_BUCKETS if tag in tags]


def _keyword_leaf(bucket: str, value: str) -> str:
    """The keyword a stored value names: a Lightroom hierarchical value
    (`People|Margaret Hartley`) names its last part."""
    return value.rsplit('|', 1)[-1] if bucket == 'hierarchical' else value


def _run_exiftool_read_keyword_fields(file_path: Path) -> dict[str, list[str]]:
    """Return {'keywords': […], 'subject': […], 'hierarchical': […]} - each
    keyword field's raw values, kept SEPARATE rather than merged into one union.

    `clear-keyword` has to know which field a value actually lives in, to
    report it and to verify the write afterwards field by field. The read is
    `_lib.read_keyword_row`, the one keyword reader (#252). Raises
    RuntimeError if exiftool is missing or its output cannot be parsed - an
    environment problem the caller surfaces, distinct from "no such keyword".
    """
    fields = keyword_fields_from_row(
        read_keyword_row(file_path, command='fha source clear-keyword'))
    return {bucket: list(fields[tag]) for bucket, tag in _KEYWORD_BUCKETS}


def _run_exiftool_edit_keyword_fields(
    file_path: Path, *, remove: list[tuple[str, str]], add: list[tuple[str, str]],
    backup: OriginalBackup, report: dict | None = None,
) -> str | None:
    """Remove/add keyword values on one file in a single exiftool call.

    `remove`/`add` entries are `(field, exact_value)` with the field names of
    `_KEYWORD_BUCKETS`. The write is `_lib.write_embedded_keywords`, the one
    keyword writer (#252): a removed value comes out of every keyword field,
    and an added one goes into exactly the fields `add` names - the planner's
    choice, passed through as `add_tags`, so what is previewed, written and
    verified is one plan. Both happen in one call, so there is never a moment
    where the file carries neither the old value nor its correction. The
    writer also takes the `originals_backup:` safety copy first (TOOLING
    §13f). Returns None on success, the failure text (or the backup refusal)
    on a per-file failure; raises RuntimeError only when exiftool is absent.
    """
    tag_of = dict(_KEYWORD_BUCKETS)
    return write_embedded_keywords(
        file_path, add=[value for _, value in add], remove=[value for _, value in remove],
        add_tags=[tag_of[bucket] for bucket, _ in add],
        backup=backup, command='fha source clear-keyword', report=report)


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
    `--replace-with` correction lands in every keyword field the file's
    format takes (#252), and is skipped (not duplicated) in a field that
    already holds it.

    Scope is documents-root only - the source must list a documents-root file
    in `files:` (matching W131's own scope; see that check's docstring in
    lint.py for why a photos-root twin is a separate, undesigned decision).
    `--file NAME` picks among several; with exactly one documents-root file
    listed, it is picked automatically.

    A `--keyword` that is itself the file's own correct SOURCE: identity
    keyword (SPEC §20 rule 3) is always refused - removing it breaks the tie
    to this record. But a WRONG SOURCE: marker (some other source's id,
    embedded by mistake) may be corrected by naming it as `--keyword` and
    passing this source's own marker as `--replace-with` - the one SOURCE:
    edit this command allows, because `fha reconcile` cannot fix it: its
    `_plan` skips any files: entry whose target already exists on disk and
    never edits embedded document metadata at all (reconcile.py), so a
    correctly-named, correctly-filed file carrying the wrong marker would
    otherwise have no tool-mediated repair path. Every other SOURCE: edit
    (removing it outright, or writing a DIFFERENT source's marker over an
    ordinary keyword) stays refused.

    `data`: {'status': 'ok'|'dry-run'|'not-found'|'refused', 'source_id',
    'path', 'removed_from', 'added_to'}. Exit codes: 0 ok/dry-run · 1 record
    or asset not found on disk · 3 refusals (bad id, blank --keyword, a
    target found but not a regular file, no/ambiguous documents-root file, a
    target outside the documents root or belonging to a different source, an
    unauthorized SOURCE: keyword edit (see above), keyword not currently
    present, exiftool or backup failure, or the post-write verification
    finding the change did not actually take).
    """
    result = Result(data={'status': None, 'source_id': None, 'path': None,
                          'removed_from': [], 'added_to': []})

    def _refuse(status: str, message: str, *, next_step: str | None = None) -> Result:
        return result_fail(result, status, message, next_step=next_step)

    if not (is_valid_id(source_id) and id_type_of(source_id) == 'S'):
        return _refuse(
            'refused',
            f'{source_id!r} is not a valid source ID. s-ids look like '
            's-2b3c4d5e6f: an s, a dash and 10 characters from '
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

    # P2 (#147 review, refined by #156 review finding 3): refuse before
    # touching anything if either side of the edit is a SOURCE: identity
    # keyword (SPEC §20 rule 3) - EXCEPT the one narrow repair this command
    # allows: --keyword names a WRONG marker (some other source's id) and
    # --replace-with names THIS source's own correct marker. That repair has
    # no other tool-mediated path - `fha reconcile`'s `_plan` (reconcile.py)
    # skips any files: entry whose target already exists on disk and never
    # edits embedded document metadata at all, so a correctly-named,
    # correctly-filed file carrying the wrong SOURCE: marker would otherwise
    # be stuck refusing in a loop pointing at a command that cannot help it.
    # Every other SOURCE: edit (removing the file's own correct marker, or
    # writing a DIFFERENT source's marker over an ordinary keyword) remains
    # refused - nothing downstream catches a pure removal (the
    # embedded-keyword lint only iterates markers that remain), so this has
    # to refuse here.
    keyword_marker_sid: str | None = None
    m = _SOURCE_KEYWORD_RE.match(keyword_text)
    if m:
        keyword_marker_sid = normalize_id(m.group(1))
    replacement_marker_sid: str | None = None
    if replacement:
        m = _SOURCE_KEYWORD_RE.match(replacement)
        if m:
            replacement_marker_sid = normalize_id(m.group(1))
    marker_correction = (
        keyword_marker_sid is not None and keyword_marker_sid != sid
        and replacement_marker_sid == sid)

    if keyword_marker_sid == sid:
        return _refuse(
            'refused',
            f'{keyword_text!r} is {fmt_id_display(sid)}\'s own SOURCE: '
            'identity keyword - the link (SPEC §20) that ties this file to '
            'its record. clear-keyword will not remove it; doing so would '
            'break that tie.')
    if keyword_marker_sid is not None and not marker_correction:
        return _refuse(
            'refused',
            f'{keyword_text!r} is a SOURCE: identity keyword for a '
            f'different source than {fmt_id_display(sid)} - `fha reconcile` '
            'cannot fix this (it skips any files: entry whose target '
            'already exists on disk, and never edits embedded metadata). '
            'To correct the marker, re-run with --replace-with '
            f'"SOURCE: {fmt_id_display(sid)}" - the one SOURCE: edit '
            'clear-keyword allows.')
    if replacement_marker_sid is not None and not marker_correction:
        return _refuse(
            'refused',
            f'--replace-with {replacement!r} looks like a SOURCE: identity '
            'keyword - clear-keyword will not write one in place of an '
            f'ordinary keyword; that would tie this file to a different '
            f'source than {fmt_id_display(sid)}. If the file is genuinely '
            'misfiled, use `fha reconcile`, not clear-keyword.')

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
    except (OSError, RuntimeError) as e:
        # P2 (#156 review finding 2, round 2): on the Python versions this
        # tool supports, Path.resolve() raises RuntimeError - not OSError -
        # for a symlink loop (the documents root or the listed asset itself),
        # not just for the OSError-shaped failures (missing drive, permission
        # denial) already handled here. An uncaught RuntimeError would escape
        # this engine as a raw traceback instead of the intended plain
        # refusal.
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
        # P2 (#156 review finding 3, round 2): `fha reconcile` cannot repair
        # this - its _plan step skips any inventory entry whose resolved path
        # already exists on disk (tools/reconcile.py), and this refusal is
        # only reached after abs_path.is_file() already succeeded above. That
        # left the user with a suggested command that runs, changes nothing,
        # and the same refusal forever. Name the exact manual fix instead:
        # which record's files: entry is wrong and what it should say.
        return _refuse(
            'refused',
            f"{alias}'s own filename carries {carried}, not "
            f'{fmt_id_display(sid)} - {record_path.name}\'s files: entry '
            f'looks like inventory drift (it names a file that belongs to a '
            f"different source). `fha reconcile` cannot fix this on its own "
            f'(it skips any files: entry whose target already exists on '
            f'disk) - edit the files: entry in {record_path.name} by hand '
            f'so it names {fmt_id_display(sid)}\'s own file instead, then '
            'retry. Nothing was read or changed.')

    try:
        fields = _run_exiftool_read_keyword_fields(abs_path)
    except RuntimeError as e:
        return _refuse('refused', str(e))

    matches: list[tuple[str, str]] = [
        (tag, value)
        for tag in fields
        for value in fields[tag]
        if _keyword_leaf(tag, value).strip().lower() == keyword_text.lower()
    ]
    if not matches:
        current = [v for tag in fields for v in fields[tag]]
        shown = ', '.join(repr(v) for v in current[:10]) if current else '(none)'
        return _refuse(
            'refused',
            f'{alias} does not currently carry the keyword {keyword_text!r} - '
            f'nothing to clear. Its embedded keywords right now: {shown}.')

    # P2 (#147 review): the "is replacement already present" check must look
    # at each field's state AFTER this operation's own removal, not the
    # as-read state - otherwise a case-only correction (replacing 'margaret
    # hartley' with 'Margaret Hartley') finds its own about-to-be-removed old
    # value as a case-insensitive "match", concludes the replacement is
    # already there, and skips adding it - while the removal below still
    # takes the old value out. Net effect: the keyword vanishes entirely and
    # the command still reports success. Excluding the exact values this call
    # is about to remove from the "already present" check fixes that; a
    # replacement equal to some OTHER, unrelated value in the field still
    # correctly gets skipped as a real duplicate.
    # The shared writer takes a removed value out of EVERY field, so the
    # "remaining" state is the read state minus every removed value anywhere.
    remove_values = list(dict.fromkeys(value for _, value in matches))

    # #252: the correction goes into every keyword field the file's format
    # takes - the same rule as every other fha keyword write - except a field
    # that already names it (in any capitals, or as the last part of a
    # Lightroom `People|…` entry), so Lightroom is not handed the person twice.
    # This plan is passed to the writer as-is and is what the preview shows
    # and the check below verifies (#253 review: they used to disagree).
    add: list[tuple[str, str]] = []
    if replacement:
        for tag in _buckets_for(abs_path):
            remaining = [v for v in fields.get(tag, []) if v not in remove_values]
            if not any(_keyword_leaf(tag, v).strip().lower() == replacement.lower()
                       for v in remaining):
                add.append((tag, replacement))
    # An accented correction on a scan whose older IPTC text is Latin-1 (no
    # character-set tag): the writer writes it there in Latin-1, or - when
    # Latin-1 cannot spell it (`Łódź`) - keeps it out of IPTC and leaves the
    # block's old captions untouched (#253 review). Planned here too, so the
    # preview and the check after the write say the same thing.
    iptc_note = None
    if replacement and not replacement.isascii() and any(b == 'keywords' for b, _ in add):
        try:
            plan = iptc_charset_plan(abs_path, [replacement] + remove_values,
                                     command='fha source clear-keyword')
        except RuntimeError:
            plan = None
        if plan is not None and replacement in plan['skip']:
            add = [(b, v) for b, v in add if b != 'keywords']
            iptc_note = plan['note']
    tag_of = dict(_KEYWORD_BUCKETS)
    bucket_of = {tag: bucket for bucket, tag in _KEYWORD_BUCKETS}
    expected_by_tag = expected_keyword_fields(
        {tag_of[b]: list(v) for b, v in fields.items() if b in tag_of},
        [replacement] if add else [], remove_values,
        tuple(tag_of[b] for b, _ in add))
    expected = {bucket_of[tag]: values for tag, values in expected_by_tag.items()}

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
        if iptc_note:
            result.add('info', f'[dry-run] {iptc_note}')
        result.add('info', '[dry-run] No file written. Re-run without --dry-run to apply.')
        return result

    if backup is None:
        backup = OriginalBackup(archive_root, fha_config)
    backup.announce()
    for level, text in backup.drain_messages():
        result.add(level, text)

    write_report: dict = {}
    error = _run_exiftool_edit_keyword_fields(
        abs_path, remove=matches, add=add, backup=backup, report=write_report)
    for level, text in backup.drain_messages():
        result.add(level, text)
    if error is not None and write_report.get('written'):
        # The file WAS written and only the check afterwards failed (#253
        # review: this used to say "Nothing was changed"). Say what is there
        # now, so the next step is taken from the truth.
        result.note_changed(abs_path)
        try:
            now = _run_exiftool_read_keyword_fields(abs_path)
            state = '; '.join(f'{b}: {", ".join(repr(v) for v in vals) or "(empty)"}'
                              for b, vals in now.items())
        except RuntimeError as e:
            state = f'(could not be read back: {e})'
        return _refuse(
            'refused',
            f'exiftool changed {alias}, but {error}. Its keyword fields now hold - '
            f'{state}. Run `fha lint --with-exif` to see what still needs fixing, '
            'then run clear-keyword again for anything left.')
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
    try:
        after_fields = _run_exiftool_read_keyword_fields(abs_path)
    except RuntimeError as e:
        # P2 (#156 review finding 2): the write itself already happened -
        # exiftool returned 0 above - so even though this re-read failed and
        # the change cannot be CONFIRMED, the file on disk was genuinely
        # touched. Record that before returning, the same way the "write
        # landed but didn't fully take" refusal a few lines down already
        # does; otherwise a caller reading Result.as_dict() sees changed: []
        # next to a refusal, which reads as "nothing happened" when
        # something did.
        result.note_changed(abs_path)
        return _refuse(
            'refused',
            f'{alias} was written, but the change could not be verified ({e}). '
            'Run `fha lint --with-exif` to check its current state by hand.')

    # P2 (#156 review finding 1): compared EXACTLY - no .strip(), no
    # case-fold. Two failure modes with the earlier stripped comparison:
    #
    #   1. A stripped comparison treats two DIFFERENT on-file strings that
    #      differ only by surrounding whitespace as "the same value" - a
    #      replacement whose exact text differs from the removed spelling
    #      only by whitespace was mistaken for the old value still sitting
    #      there, and a write that worked exactly as asked was wrongly
    #      refused.
    #   2. When the field held MULTIPLE case variants of the same word (e.g.
    #      both 'margaret hartley' and 'Margaret Hartley') and --replace-with
    #      happens to equal one of them byte-for-byte, that spelling is
    #      correctly removed-then-re-added by design (collapsing duplicates
    #      down to one canonical spelling) - no textual comparison, stripped
    #      or not, can tell that apart from "removal failed" purely by
    #      looking at the string. `add_by_tag` carries the values this call
    #      itself is intentionally putting (back) into each field, so a
    #      matched value that is ALSO one of those intended additions is
    #      excluded from the "still there" check - its presence is expected,
    #      not a leftover.
    # P2 (#156 review finding 1, round 2): a membership check ("is this value
    # present at all") cannot see a MULTIPLICITY problem - when a field
    # already held the exact replacement spelling alongside the noncanonical
    # one being removed (e.g. both 'margaret hartley' and 'Margaret Hartley'),
    # excluding every matched value that's also an intended addition from the
    # "still there" check (the old `not_removed`/`add_by_tag` logic) let a
    # partial write that removed only the noncanonical copy and then added a
    # SECOND copy of the canonical spelling pass verification with a straight
    # duplicate now sitting in the field. Compare the expected FINAL COUNT of
    # every value per tag - original counts, minus every matched removal,
    # plus every intended addition - against what's actually on disk.
    problems = []
    for tag in sorted(set(expected) | set(after_fields)):
        expected_counts = Counter(expected.get(tag, []))
        actual_counts = Counter(after_fields.get(tag, []))
        missing = expected_counts - actual_counts
        extra = actual_counts - expected_counts
        if missing:
            problems.append('is missing ' + ', '.join(
                f'{tag}: {value!r}' + (f' (x{n})' if n > 1 else '')
                for value, n in missing.items()))
        if extra:
            problems.append('still carries an unexpected extra copy of ' + ', '.join(
                f'{tag}: {value!r}' + (f' (x{n})' if n > 1 else '')
                for value, n in extra.items()))
    if problems:
        result.note_changed(abs_path)  # exiftool did write SOMETHING to the file
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
    if iptc_note:
        result.add('info', iptc_note)
    result.add('info',
               'Next: run `fha lint --with-exif` if you want to confirm the '
               'warning is gone.',
               next_step='fha lint --with-exif')
    return result


# ── fha source set-primary (#200) ────────────────────────────────────────────
#
# A record's `files:` list is edited as text, one `key: value` line at a time,
# so everything the edit does not mean to touch (comments, key order, quoting,
# CRLF endings, the ## Claims block) stays byte for byte. The YAML parse is the
# referee: the edit is planned from the parsed list, and the rewritten text
# must parse back to exactly that plan before anything is written.

# `files:` at column zero; the rest of the line decides block vs inline form.
_FILES_KEY_RE = re.compile(r'^files:(?P<rest>.*)$')
# One list item's first line: `  - file: x` (or a bare `-` with its keys below).
_ITEM_START_RE = re.compile(r'^(?P<indent> *)-(?P<gap> +|$)(?P<body>.*)$')
# A `key:` at the start of an item's own key column.
_ITEM_KEY_RE = re.compile(r'^(?P<key>[A-Za-z_][A-Za-z0-9_-]*):(?: |$)')
# The order `fha process` writes an entry's keys in (_render_scaffold_file_entry);
# a key this verb has to ADD goes after the nearest one before it here.
_ENTRY_KEY_ORDER = ('file', 'role', 'copy', 'is_primary')


def _is_true(value) -> bool:
    """`is_primary` as a human or a tool might have written it.

    `fha process` writes `true`; a hand edit may say `yes`, `True` or `"true"`,
    and `read_record` hands every boolean back as the string 'true'. The same
    spellings process.py's bundle hints accept."""
    return value is True or str(value).strip().lower() in ('true', 'yes', '1')


def _entry_alias(entry) -> str | None:
    """The alias path one `files:` item names, slash-normalized, or None."""
    if isinstance(entry, dict) and entry.get('file'):
        return str(entry['file']).replace('\\', '/')
    if isinstance(entry, str) and entry.strip():
        return entry.strip().replace('\\', '/')
    return None


def _first_problem(problems: list) -> str:
    """The first `read_record` parse problem, in words (they are (code, text) pairs)."""
    first = problems[0]
    return str(first[1] if isinstance(first, (list, tuple)) and len(first) > 1 else first)


def _is_image_alias(alias: str | None) -> bool:
    """True for a photos-root alias whose file is an image the photo catalog
    keeps. A metadata companion such as `x-00100.xmp` sits in the photos root
    too, and must never be made a photo's main image (#210 review)."""
    return _is_photos_alias(alias) and PurePosixPath(str(alias)).suffix.lower() in PHOTO_EXTENSIONS


def _is_photos_alias(alias: str | None) -> bool:
    """True for a photos-root alias (`photos/1950/x.tif`). Aliases are written
    with the root's NAME, never its on-disk location (SPEC §12.1), so this is a
    text test - no fha.yaml needed."""
    return bool(alias) and alias.split('/', 1)[0].lower() == 'photos'


def _what_the_file_is(parsed) -> str:
    """A plain description of a non-main scan, for messages: 'the back'."""
    if parsed.part_kind == 'attached':
        return f"the photo's {parsed.attached.replace('-', ' ')}"
    if parsed.part_kind == 'back':
        return 'the back of the photo'
    if parsed.part_kind == 'negative':
        return 'the negative'
    if parsed.part_kind == 'page':
        return f'page {parsed.page_num}'
    if parsed.variant_id:
        return f'print {parsed.variant_id.upper()} (a second copy)'
    if parsed.is_crop:
        return 'a cropped copy'
    if parsed.part_kind == 'bw':
        return 'a black-and-white copy'
    return 'another scan of the photo'


def _files_block_items(lines: list[str]) -> tuple[list[dict] | None, str | None]:
    """Find each `files:` list item's lines in a record's frontmatter.

    Returns (items, None) or (None, a plain reason the list cannot be edited
    as text). Each item is {'start', 'end', 'col', 'keys'}: its first line,
    the line after its last, the column its keys start at, and {key: (first
    line, last line)} for each of its own keys - the last line takes in a
    value that runs onto deeper-indented lines below it.

    Only the block form `fha process` writes is handled. The one-line form
    (`files: [...]`) and an item written as `- {file: x, role: y}` are reported
    rather than rewritten: normalizing them would change lines this verb has
    no business touching. `lines` come from `text.split('\\n')`, so a CRLF
    record's lines still carry their '\\r'.
    """
    span = frontmatter_fence_span(lines)
    if span is None:
        return None, 'its frontmatter (the block between the --- lines at the top) could not be found'
    open_idx, close_idx = span
    key_rows = [i for i in range(open_idx + 1, close_idx)
                if _FILES_KEY_RE.match(lines[i].rstrip('\r'))]
    if not key_rows:
        return None, 'it has no files: list'
    if len(key_rows) > 1:
        return None, 'it has more than one files: line'
    files_row = key_rows[0]
    rest = _FILES_KEY_RE.match(lines[files_row].rstrip('\r')).group('rest').strip()
    if rest and not rest.startswith('#'):
        return None, 'its files: list is written on one line (files: [...])'

    # The block runs until the next top-level key. A `- ` at column zero is
    # still a list item (YAML allows `files:` followed by unindented dashes).
    block_end = close_idx
    for j in range(files_row + 1, close_idx):
        ln = lines[j].rstrip('\r')
        if ln and ln[0] not in ' -#':
            block_end = j
            break

    starts: list[int] = []
    list_indent: int | None = None
    for j in range(files_row + 1, block_end):
        m = _ITEM_START_RE.match(lines[j].rstrip('\r'))
        if not m:
            continue
        indent = len(m.group('indent'))
        if list_indent is None:
            list_indent = indent       # the first dash is the first item
        if indent == list_indent:
            starts.append(j)           # a deeper dash is a nested list, not an item

    items: list[dict] = []
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else block_end
        m = _ITEM_START_RE.match(lines[start].rstrip('\r'))
        body = m.group('body')
        if body.startswith('{'):
            return None, 'one of its files: entries is written on one line ({file: ..., role: ...})'
        if body:
            col = len(m.group('indent')) + 1 + len(m.group('gap'))
        else:
            col = next((len(ln) - len(ln.lstrip(' '))
                        for ln in (x.rstrip('\r') for x in lines[start + 1:end])
                        if ln.strip()), len(m.group('indent')) + 2)
        key_rows_in_item: list[tuple[str, int]] = []
        for j in range(start, end):
            ln = lines[j].rstrip('\r')
            if j == start:
                segment = body
            elif len(ln) - len(ln.lstrip(' ')) == col:
                segment = ln[col:]
            else:
                continue
            km = _ITEM_KEY_RE.match(segment)
            if km:
                key_rows_in_item.append((km.group('key'), j))
        keys: dict[str, tuple[int, int]] = {}
        for k, (name, row) in enumerate(key_rows_in_item):
            stop = key_rows_in_item[k + 1][1] if k + 1 < len(key_rows_in_item) else end
            last = row
            for t in range(row + 1, stop):
                ln = lines[t].rstrip('\r')
                if (ln.strip() and not ln.lstrip().startswith('#')
                        and len(ln) - len(ln.lstrip(' ')) > col):
                    last = t
            keys.setdefault(name, (row, last))
        items.append({'start': start, 'end': end, 'col': col, 'keys': keys})
    return items, None


def _trailing_comment(line: str) -> str:
    """The `# comment` (with the spaces before it) ending a `key: value`
    line, or ''. A '#' inside a quoted value is not a comment, and neither is
    one with no space before it (YAML requires the space)."""
    text = line.rstrip('\r\n')
    colon = text.find(':')
    if colon < 0:
        return ''
    rest = text[colon + 1:]
    quote = None
    for k, ch in enumerate(rest):
        if quote:
            if ch == quote:
                quote = None
        elif ch in '\'"' and not rest[:k].strip():
            quote = ch
        elif ch == '#' and k and rest[k - 1] in ' \t':
            start = k
            while start and rest[start - 1] in ' \t':
                start -= 1
            return rest[start:]
    return ''


def _frontmatter_comments(lines: list[str]) -> list[str]:
    """Every comment in a record's frontmatter, as text, so an edit can
    prove it kept them all (#210 review: a handwritten note on a changed line
    was silently dropped, and the YAML comparison cannot see comments)."""
    span = frontmatter_fence_span(lines)
    if span is None:
        return []
    out = []
    for line in lines[span[0] + 1:span[1]]:
        stripped = line.strip()
        if stripped.startswith('#'):
            out.append(stripped)
        else:
            comment = _trailing_comment(line).strip()
            if comment:
                out.append(comment)
    return out


def _set_entry_key(lines: list[str], item_no: int, key: str, value_text: str,
                   cr: str) -> tuple[list[str] | None, str | None]:
    """Set one key of one `files:` item to `value_text` (already YAML-quoted).

    An existing key line is replaced in place, keeping its own indent (or its
    `  - ` prefix on the item's first line). A missing key is added on a new
    line after the nearest key before it in `_ENTRY_KEY_ORDER`, at the item's
    key column. Re-scans the block each call, so several edits in a row never
    work from stale line numbers. Returns (new lines, None) or (None, reason).

    A handwritten `# comment` on the replaced line is kept (`role: primary  #
    caption by Aunt May` becomes `role: back  # caption by Aunt May`). A value
    running onto further lines that carry comments is refused rather than
    guessed at (#210 review).
    """
    items, problem = _files_block_items(lines)
    if problem:
        return None, problem
    item = items[item_no]
    col = item['col']
    out = list(lines)
    if key in item['keys']:
        row, last = item['keys'][key]
        if any('#' in lines[r] for r in range(row + 1, last + 1)):
            return None, (f'its {key}: value runs onto more lines with a note on them, '
                          'and the note could not be kept safely')
        comment = _trailing_comment(lines[row])
        # The line's own ending, not the file's: one pasted CRLF line in
        # Notes must not turn LF frontmatter lines into CRLF (#210 audit).
        eol = '\r' if lines[row].endswith('\r') else ''
        out[row:last + 1] = [lines[row][:col] + f'{key}: {value_text}' + comment + eol]
        return out, None
    if 'file' not in item['keys']:
        return None, 'one of its files: entries has no file: line where expected'
    earlier = _ENTRY_KEY_ORDER[:_ENTRY_KEY_ORDER.index(key)] if key in _ENTRY_KEY_ORDER else _ENTRY_KEY_ORDER
    anchor = max(item['keys'][k][1] for k in earlier if k in item['keys'])
    eol = '\r' if lines[anchor].endswith('\r') else ''
    out[anchor + 1:anchor + 1] = [' ' * col + f'{key}: {value_text}' + eol]
    return out, None


def run_source_set_primary(
    archive_root: Path, source_id: str, *, file: str, dry_run: bool = False,
) -> Result:
    """Make one photo the main image of a source record; return a Result.

    #200: while the filename bug was live, `fha process` read the plain front
    scan `Smith-Farm-01400.tif` as photo `Smith` with a made-up role
    `farm-01400`, so when the human answered "one" for a front/back pair, the
    record it wrote named the BACK as the main image (`is_primary: true`,
    `role: primary`) and the front as a variant. The parser is fixed, and the
    photo catalog now picks the front by itself - but a record already on disk
    keeps saying the back until someone corrects it. `fha doctor` finds those
    records; this verb is the fix it names, one record at a time, on the
    human's say-so.

    What changes, and only in that record's `files:` list:
      - the named file's entry gets `is_primary: true` (SPEC §14: the one
        main-file mark; its own role line is left as written);
      - the entry that WAS the main image gets `is_primary: false` and the
        role its own filename spells (`variant_role` - `back`, `negative`,
        `page-2`, ...; `variant` when the name spells none), plus `copy: B`-
        style letter when its name carries one and the entry has none.
    Every other entry is left exactly as written. That is the chosen rule for
    the made-up roles the bug left behind: the only one it ever wrote on a
    real pair is the front's, and the front is one of the two entries this
    verb rewrites anyway. Guessing at a third entry's role would be editing a
    line nobody asked about.

    Files are never renamed or touched (photos never are - AGENTS.md), and
    the record's title and filename stay as they are: they were the human's
    or the tool's choice of words, not a fact this verb knows to be wrong.

    `--file` matches an entry by its full alias (`photos/1950/x.tif`) or its
    bare filename, exactly first and then ignoring capitals. Refusals: an
    invalid id, no `--file`, no match, more than one match, a match outside
    the photos root, an entry written in a one-line form this text surgery
    will not rewrite, a record where two or more files other than the named one already
    claim to be the main image (`is_primary: true`), a current main file that is not a photo, or any result that would
    not leave exactly one `is_primary: true`. The rewritten text must parse
    back to exactly the planned list, with every other frontmatter field and
    the whole body unchanged, before it is written; after the write it is
    re-read with `read_record`, and a record that does not read back as
    planned is restored and the run refused.

    `data`: {'status': 'ok'|'dry-run'|'already'|'not-found'|'refused',
    'source_id', 'path', 'primary', 'previous'}. Exit codes: 0 ok/dry-run/
    already, 1 record not found (with the `fha find` next step), 3 refusals.
    """
    result = Result(data={'status': None, 'source_id': None, 'path': None,
                          'primary': None, 'previous': None})

    def _refuse(status: str, message: str, *, next_step: str | None = None) -> Result:
        return result_fail(result, status, message, next_step=next_step)

    if not (is_valid_id(source_id) and id_type_of(source_id) == 'S'):
        return _refuse(
            'refused',
            f'{source_id!r} is not a valid source ID. s-ids look like '
            's-2b3c4d5e6f: an s, a dash and 10 characters from '
            'the archive alphabet.')
    sid = normalize_id(source_id)
    shown_id = fmt_id_display(sid)
    result.data['source_id'] = shown_id

    wanted = (file or '').strip().replace('\\', '/')
    if not wanted:
        return _refuse(
            'refused',
            f'No file was named for {shown_id}. Say which photo should be the '
            f'main image, e.g. `fha source set-primary {shown_id} --file '
            '"Smith-Farm-01400.tif"`.')

    path = find_source_record_path(archive_root, sid)
    if path is None:
        return result_fail(
            result, 'not-found',
            f'No source record found for {shown_id} under '
            f'{archive_root / "sources"} - check the id with `fha find {shown_id}`.',
            exit_code=EXIT_WARNINGS, level='warning',
            next_step='fha find ' + shown_id)
    result.data['path'] = str(path)

    try:
        text_in = read_text_exact(path)
    except OSError as e:
        return _refuse(
            'refused', f'cannot read {path}: {e}',
            next_step='Check the file is not open in another program and try again.')
    label = _source_label(text_in, sid)

    before_meta = parse_frontmatter_strict(text_in)
    if before_meta is None:
        return _refuse(
            'refused',
            f'The details at the top of {path.name} could not be read, so its '
            'file list cannot be changed safely. Run `fha lint` to see what is '
            'wrong there, fix it, then try again. Nothing was written.',
            next_step='fha lint')
    # A record already broken below its frontmatter (an unclosed quote in a
    # claim, say) cannot pass the post-write check, so the real run would
    # refuse and restore it. Refuse now, so a --dry-run never promises a
    # fix the real run cannot make (#210 review).
    existing = read_record(path)
    if existing.get('parse_errors'):
        return _refuse(
            'refused',
            f'{path.name} already has a problem elsewhere in it '
            f'({_first_problem(existing["parse_errors"])}), so its file list will not be '
            'changed until that is fixed. Run `fha lint` to see it, fix it, '
            'then try again. Nothing was written.',
            next_step='fha lint')
    entries = before_meta.get('files')
    if not isinstance(entries, list) or not entries:
        return _refuse(
            'refused',
            f'{label} lists no files, so there is no main image to choose. '
            f'Run `fha find {shown_id}` to see what it holds.')
    aliases = [_entry_alias(e) for e in entries]

    def _matches(fold: bool) -> list[int]:
        target = wanted.casefold() if fold else wanted
        hits = []
        for i, alias in enumerate(aliases):
            if alias is None:
                continue
            names = (alias, PurePosixPath(alias).name)
            if target in ((n.casefold() for n in names) if fold else names):
                hits.append(i)
        return hits

    photo_names = [PurePosixPath(a).name for a in aliases if _is_photos_alias(a)]
    hits = _matches(False) or _matches(True)
    if not hits:
        listed = ', '.join(photo_names) if photo_names else 'none'
        return _refuse(
            'refused',
            f'--file {file!r} does not match any file listed on {label}. '
            f'The photos it lists: {listed}. Name one of those, e.g. '
            f'`fha source set-primary {shown_id} --file '
            f'"{photo_names[0] if photo_names else "front-scan.tif"}"`. '
            'Nothing was written.')
    if len(hits) > 1:
        shown = ', '.join(aliases[i] for i in hits)
        return _refuse(
            'refused',
            f'--file {file!r} matches more than one file listed on {label} '
            f'({shown}). Name the one you mean by its full path, e.g. '
            f'`--file "{aliases[hits[0]]}"`. Nothing was written.')
    target_i = hits[0]
    target_alias = aliases[target_i]
    target_name = PurePosixPath(target_alias).name
    if _is_photos_alias(target_alias) and not _is_image_alias(target_alias):
        return _refuse(
            'refused',
            f'{target_alias} is not an image (it is a {PurePosixPath(target_alias).suffix} '
            'file), so it cannot be the main image of a photo. Nothing was written.')
    if not _is_photos_alias(target_alias):
        return _refuse(
            'refused',
            f'{target_alias} is not in the photos folder. set-primary only '
            'chooses which photo scan is the main image of a photo source; '
            "a document's main file is set when `fha process` files it. "
            'Nothing was written.')
    target_entry = entries[target_i]
    if not isinstance(target_entry, dict):
        return _refuse(
            'refused',
            f'{target_alias} is listed on {label} in a short form (just the '
            'path, with no role: line), so this tool will not rewrite it. '
            f'Open {path} and write it as `- file: {target_alias}` with a '
            '`role:` line under it, then try again. Nothing was written.')

    # The entry that is the main image now: marked `is_primary: true`.
    previous = [i for i, e in enumerate(entries)
                if i != target_i and isinstance(e, dict)
                and _is_true(e.get('is_primary'))]
    if len(previous) > 1:
        shown = ', '.join(aliases[i] for i in previous)
        return _refuse(
            'refused',
            f'{label} already marks more than one file as its main image '
            f'({shown}), so this tool cannot tell which one to step down. Open '
            f'{path}, leave `is_primary: true` on just one file, '
            'run `fha lint`, '
            'then try again. Nothing was written.')
    prev_i = previous[0] if previous else None
    if prev_i is None and _is_true(target_entry.get('is_primary')):
        result.data['status'] = 'already'
        result.data['primary'] = target_alias
        result.add('info', f'{target_name} is already the main image of {label}. '
                           'Nothing to change.')
        return result
    if prev_i is not None and not _is_image_alias(aliases[prev_i]):
        return _refuse(
            'refused',
            f'The main file of {label} is {aliases[prev_i]}, which is not a '
            'photo, so this tool will not replace it with a photo. If that is '
            f'really what you want, change the is_primary: lines in {path} by '
            'hand, then run `fha lint`. Nothing was written.')

    # The plan: what the parsed list must read after the edit, and the
    # line edits that get it there. An edit whose value is already in place
    # is skipped, so a line that is already right is never rewritten.
    planned = copy.deepcopy(entries)
    edits: list[tuple[int, str, str]] = []

    def _plan(i: int, key: str, value, value_text: str) -> None:
        if entries[i].get(key) == value:
            return
        planned[i][key] = value
        edits.append((i, key, value_text))

    _plan(target_i, 'is_primary', True, 'true')
    prev_role = prev_copy = None
    if prev_i is not None:
        parsed = parse_media_filename(PurePosixPath(aliases[prev_i]).stem)
        prev_role = variant_role(parsed) or 'variant'
        _plan(prev_i, 'role', prev_role, yaml_inline(prev_role))
        _plan(prev_i, 'is_primary', False, 'false')
        # A negative's letter is not a print letter (TOOLING §9) - the same
        # rule `fha process` follows when it writes copy:.
        letter = None if parsed.part_kind == 'negative' else parsed.variant_id
        if letter and not entries[prev_i].get('copy'):
            prev_copy = letter
            _plan(prev_i, 'copy', letter, yaml_inline(letter))

    n_primary = sum(1 for e in planned if isinstance(e, dict) and _is_true(e.get('is_primary')))
    if n_primary != 1:
        return _refuse(
            'refused',
            f'Changing {label} this way would leave {n_primary} files marked as '
            'the main image instead of exactly one, so nothing was written. '
            f'Open {path} and check the is_primary: lines, then run `fha lint`.')

    lines = text_in.split('\n')
    cr = '\r' if '\r\n' in text_in else ''
    hand_fix = (f'Open {path} and make the change by hand (is_primary: true '
                f'on {target_name}), then run `fha lint`. '
                'Nothing was written.')
    items, problem = _files_block_items(lines)
    if problem is None and len(items) != len(entries):
        problem = 'its files: list could not be lined up entry by entry'
    if problem:
        return _refuse('refused', f'{path.name} cannot be edited safely: {problem}. {hand_fix}')
    for item_no, key, value_text in edits:
        lines, problem = _set_entry_key(lines, item_no, key, value_text, cr)
        if problem:
            return _refuse('refused', f'{path.name} cannot be edited safely: {problem}. {hand_fix}')
    new_text = '\n'.join(lines)

    # The referee. The rewritten frontmatter must parse to exactly the plan,
    # every other field must be untouched, and so must everything below the
    # frontmatter (## Claims, ## Notes, ...).
    after_meta = parse_frontmatter_strict(new_text)
    old_span = frontmatter_fence_span(text_in.split('\n'))
    new_span = frontmatter_fence_span(lines)
    if (after_meta is None or after_meta.get('files') != planned
            or frontmatter_edit_problem(new_text, before_meta=before_meta,
                                        changed_keys={'files'}) is not None
            or old_span is None or new_span is None
            or text_in.split('\n')[old_span[1]:] != lines[new_span[1]:]
            or _frontmatter_comments(text_in.split('\n')) != _frontmatter_comments(lines)):
        return _refuse(
            'refused',
            f'{path.name} is laid out in a way this tool could not edit '
            f'cleanly (the change did not read back as planned). {hand_fix}')

    prev_name = PurePosixPath(aliases[prev_i]).name if prev_i is not None else None
    result.data['primary'] = target_alias
    result.data['previous'] = aliases[prev_i] if prev_i is not None else None

    def _step_down(verb: str) -> str:
        if prev_i is None:
            return ''
        parsed_prev = parse_media_filename(PurePosixPath(aliases[prev_i]).stem)
        copy_note = f', copy: {prev_copy}' if prev_copy else ''
        return (f'{prev_name} {verb} {_what_the_file_is(parsed_prev)} '
                f'(role: {prev_role}{copy_note}).')

    if dry_run:
        result.data['status'] = 'dry-run'
        result.add('info', f'[dry-run] Would make {target_name} the main image of {label}.')
        if prev_i is not None:
            result.add('info', '[dry-run] ' + _step_down('would be listed as'))
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

    # Read it back the way every other tool will, and put the record back as
    # it was if it does not say what was planned.
    try:
        reread = read_record(path)
        files_back = (reread.get('meta') or {}).get('files') or []
        sound = (
            not reread.get('parse_errors')
            and len(files_back) == len(planned)
            and _is_true(files_back[target_i].get('is_primary'))
            and sum(1 for e in files_back
                    if isinstance(e, dict) and _is_true(e.get('is_primary'))) == 1
            and (prev_i is None or str(files_back[prev_i].get('role')) == prev_role))
    except Exception:
        sound = False
    if not sound:
        try:
            write_text_exact_atomic(path, text_in)
            restored = 'It was put back exactly as it was.'
        except OSError as e:
            restored = (f'Putting it back failed too ({e}) - restore it from your '
                        'backup or git before doing anything else.')
        return _refuse(
            'refused',
            f'{path.name} did not read back as planned after the change. '
            f'{restored} Run `fha lint` to look for the cause.',
            next_step='fha lint')

    result.data['status'] = 'ok'
    result.note_changed(path)
    result.add('info', f'{target_name} is now the main image of {label}.', path=path)
    if prev_i is not None:
        result.add('info', _step_down('is now listed as'))
    result.add('info', 'No photo was renamed or changed - only the record\'s file list.')
    result.add('info',
               'Next: run `fha index` when convenient so search and the '
               'workbench show the change.',
               next_step='fha index')
    return result


# ── fha source merge ─────────────────────────────────────────────────────────
#
# Two source records that turn out to be one piece of evidence (an index entry
# and the document it indexes, or the same record captured from two sites)
# become one: the keeper takes the retiree's files, claims, notes and links,
# lists the retiree's id in `aliases:` so every old link still finds it, and
# the retiree's record file is removed (SPEC §9 "Merged sources").

_MERGE_COMMAND = 'fha source merge'

# Frontmatter lists the keeper gains items in. Everything else on the retiree
# that differs from the keeper is written into the keeper's Notes instead, so
# nothing the retiree's record said is lost.
_MERGE_LIST_KEYS = ('aliases', 'people', 'places', 'external_links', 'ai_passes',
                    'files', 'merged_from')
_MERGE_NOT_NOTED = frozenset({'id', 'created', *_MERGE_LIST_KEYS})

# Copy labels a moved file can take when its role is already taken on the
# keeper (SPEC §13: `{slug}[-{copy}][-{role}]`, copies `b`, `c`, ...).
_MERGE_COPY_LETTERS = 'bcdefghijklmnopqrstuvwxyz'


class _MergeRefused(Exception):
    """A merge that cannot go ahead, found before anything is written.

    Carries the plain message, the next command when there is one, the exit
    code and the `data['status']` word, so `run_source_merge` can turn it
    into its one refusal Result whatever check raised it."""

    def __init__(self, message: str, *, next_step: str | None = None,
                 exit_code: int = EXIT_ERRORS, status: str = 'refused') -> None:
        super().__init__(message)
        self.message = message
        self.next_step = next_step
        self.exit_code = exit_code
        self.status = status


def _merge_read_keywords(file_path: Path) -> list[str]:
    """Every keyword embedded in one file (all keyword fields unioned).

    A thin seam over `_lib.read_embedded_keywords`, the one keyword reader,
    that tests monkeypatch. Raises `ExiftoolUnavailableError` when exiftool is
    missing and RuntimeError when it could not read this file."""
    return read_embedded_keywords(file_path, command=_MERGE_COMMAND)


def _merge_write_keywords(file_path: Path, *, add: list[str], remove: list[str],
                          backup: OriginalBackup) -> str | None:
    """Add and remove keywords in one file through `_lib.write_embedded_keywords`,
    the one keyword writer (every keyword field, one exiftool call, the
    `originals_backup:` copy first). Returns None on success, else the
    failure text. A seam tests monkeypatch."""
    return write_embedded_keywords(file_path, add=add, remove=remove, backup=backup,
                                   command=_MERGE_COMMAND)


def _merge_today() -> datetime.date:
    """Today's date, for the `merged_from:` entry. A seam tests pin."""
    return datetime.date.today()


def _claim_person_ids(claim: dict) -> set[str]:
    """The person ids one claim names, in `persons:` and in `roles:`."""
    ids: set[str] = set()
    refs = list(link_field_refs(claim.get('persons')))
    roles = claim.get('roles')
    if isinstance(roles, dict):
        for value in roles.values():
            refs.extend(link_field_refs(value))
    for ref in refs:
        ref = str(ref).strip()
        if is_valid_id(ref) and id_type_of(ref) == 'P':
            ids.add(normalize_id(ref))
    return ids


def _accepted_claim_people(claims: list) -> set[str]:
    """Everyone named in a record's accepted claims.

    This is the evidence the human has already confirmed. Two records whose
    accepted claims name no person in common are, on that evidence, about
    different people, so `run_source_merge` refuses to make them one."""
    people: set[str] = set()
    for claim in claims:
        if (isinstance(claim, dict)
                and str(claim.get('status') or '').strip().lower() == 'accepted'):
            people |= _claim_person_ids(claim)
    return people


def _yaml_item_lines(item, col: int) -> list[str]:
    """One YAML list item, block style, starting at column `col`.

    A mapping is written one key per line in its own key order (a flow
    `{...}` from `yaml_inline` would sort the keys); every scalar goes through
    `yaml_inline`, the shared quoting rule, so it reads back as the same
    value."""
    if isinstance(item, dict) and item:
        return _yaml_mapping_lines(item, col + 2, first_lead=' ' * col + '- ')
    return [' ' * col + '- ' + yaml_inline(item)]


def _yaml_mapping_lines(mapping: dict, key_col: int, first_lead: str | None = None) -> list[str]:
    """A YAML mapping, block style, its keys at column `key_col`."""
    out: list[str] = []
    for n, (key, value) in enumerate(mapping.items()):
        lead = first_lead if (n == 0 and first_lead is not None) else ' ' * key_col
        if isinstance(value, dict) and value:
            out.append(f'{lead}{key}:')
            out.extend(_yaml_mapping_lines(value, key_col + 2))
        elif isinstance(value, list) and value:
            out.append(f'{lead}{key}:')
            for item in value:
                out.extend(_yaml_item_lines(item, key_col + 2))
        else:
            out.append(f'{lead}{key}: {yaml_inline(value)}')
    return out


_TOP_KEY_RE = re.compile(r'^([A-Za-z_][\w-]*)\s*:(?:\s|$)')


def _extend_frontmatter_list(lines: list[str], key: str, all_items: list,
                             new_items: list, cr: str) -> list[str]:
    """Add `new_items` to the frontmatter list `key`, as text surgery.

    The keeper's frontmatter is edited line by line, like every other record
    writer here, so its comments and key order survive. A block list gets the
    new items after its last item, at the indent its items already use. A
    one-line form (`aliases: [s-…, stem]`) is rewritten as a block holding
    `all_items` (the old items and the new). An absent key is added in SPEC
    §14's key order (`SOURCE_FRONTMATTER_KEY_ORDER`). The caller re-parses the
    whole result and compares it with the planned values before anything is
    written, so a shape this misreads is refused, never saved.
    """
    span = frontmatter_fence_span(lines)
    if span is None:
        raise _MergeRefused('the keeper record lost its frontmatter fences while being '
                            'rewritten, so nothing was written.', exit_code=EXIT_FAILURE)
    start, end = span
    found = frontmatter_key_line_indexes(lines, start + 1, end, key)
    if len(found) > 1:
        raise _MergeRefused(
            f'the keeper record has more than one `{key}:` line, so the merge cannot '
            'tell which one to add to. Run `fha lint` to see it, keep one, then run '
            'the merge again. Nothing was written.', next_step='fha lint')
    if not found:
        order = list(SOURCE_FRONTMATTER_KEY_ORDER)
        rank = order.index(key) if key in order else len(order)
        insert_at = end
        for i in range(start + 1, end):
            m = _TOP_KEY_RE.match(lines[i])
            if m and m.group(1) in order and order.index(m.group(1)) > rank:
                insert_at = i
                break
        block = [f'{key}:'] + [ln for item in all_items for ln in _yaml_item_lines(item, 2)]
        return lines[:insert_at] + [ln + cr for ln in block] + lines[insert_at:]
    at = found[0]
    value_text = lines[at].rstrip('\r').split(':', 1)[1].strip()
    block_end = at + 1
    while block_end < end and (not lines[block_end].strip() or lines[block_end][0].isspace()
                               or lines[block_end].startswith('- ')):
        block_end += 1
    while block_end > at + 1 and not lines[block_end - 1].strip():
        block_end -= 1
    if value_text == '' or value_text.startswith('#'):
        indent = 2
        for ln in lines[at + 1:block_end]:
            stripped = ln.lstrip(' ')
            if stripped.startswith('- ') or stripped.rstrip('\r') == '-':
                indent = len(ln) - len(stripped)
                break
        added = [ln for item in new_items for ln in _yaml_item_lines(item, indent)]
        return lines[:block_end] + [ln + cr for ln in added] + lines[block_end:]
    block = [f'{key}:'] + [ln for item in all_items for ln in _yaml_item_lines(item, 2)]
    return lines[:at] + [ln + cr for ln in block] + lines[block_end:]


def _body_sections(lines: list[str], body_start: int) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """A record body as (preamble lines, [(heading, content lines), ...]).

    Headings are the level-2 `## ` lines the section writers use; the content
    of each runs to the next one. Line endings are dropped from the content
    (the writer adds the keeper's own)."""
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    current: list[str] = preamble
    for ln in lines[body_start:]:
        bare = ln.rstrip('\r')
        if re.match(r'^##\s+\S', bare):
            current = []
            sections.append((bare[2:].strip(), current))
        else:
            current.append(bare)
    return preamble, sections


def _trimmed(content: list[str]) -> list[str]:
    """`content` without blank lines at either end."""
    out = list(content)
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return out


def _is_placeholder(content: list[str]) -> bool:
    """True for an empty section or one holding only `(none yet)`."""
    body = _trimmed(content)
    return not body or (len(body) == 1 and body[0].strip() in ('*(none yet)*', '(none yet)'))


def _outside_fence(content: list[str]) -> list[str]:
    """A `## Claims` section's lines without its fenced ```yaml block."""
    out: list[str] = []
    inside = False
    for ln in content:
        if not inside and ln.strip() == '```yaml':
            inside = True
        elif inside and ln.strip() == '```':
            inside = False
        elif not inside:
            out.append(ln)
    return out


def _claim_item_indent(block: list[str]) -> int | None:
    """The column a claims block's list items start at, or None if it has none."""
    for ln in block:
        stripped = ln.lstrip(' ')
        if stripped.startswith('- ') or stripped.rstrip('\r') == '-':
            return len(ln) - len(stripped)
    return None


def _merge_claims_text(keeper_lines: list[str], retiree_lines: list[str], cr: str) -> list[str]:
    """The keeper's lines with the retiree's claim items added to its `## Claims`.

    The retiree's items are copied as written (comments and all), shifted to
    the keeper's item indent, after the keeper's last claim. A keeper with no
    claims block gets one, before its first `##` section. The caller checks
    the parsed result claim by claim before anything is written."""
    rblock = _find_claims_block(retiree_lines)
    if rblock is None:
        return keeper_lines
    items = _trimmed([ln.rstrip('\r') for ln in retiree_lines[rblock[0] + 1:rblock[1]]])
    if not items:
        return keeper_lines
    kblock = _find_claims_block(keeper_lines)
    want = 0
    if kblock is not None:
        found = _claim_item_indent(keeper_lines[kblock[0] + 1:kblock[1]])
        want = found if found is not None else 0
    have = _claim_item_indent(items) or 0
    shift = want - have
    if shift > 0:
        items = [(' ' * shift + ln) if ln.strip() else ln for ln in items]
    elif shift < 0:
        if any(ln.strip() and not ln.startswith(' ' * -shift) for ln in items):
            raise _MergeRefused(
                "the two records' claims are indented differently, so they cannot be "
                'put into one block safely. Run `fha lint --format-write` to tidy both records, '
                'then run the merge again. Nothing was written.',
                next_step='fha lint --format-write')
        items = [ln[-shift:] if ln.strip() else ln for ln in items]
    if kblock is not None:
        open_fence, close_fence = kblock
        head = keeper_lines[:close_fence]
        if close_fence > open_fence + 1 and head[-1].strip():
            head.append(cr)
        return head + [ln + cr for ln in items] + keeper_lines[close_fence:]
    span = frontmatter_fence_span(keeper_lines)
    body_start = span[1] + 1 if span else 0
    insert_at = next((i for i in range(body_start, len(keeper_lines))
                      if re.match(r'^##\s+\S', keeper_lines[i])), None)
    block = ['## Claims', '```yaml', *items, '```', '']
    if insert_at is None:
        return create_section_at_eof(keeper_lines, 'Claims', '\n'.join(block[1:-1]), cr)
    return keeper_lines[:insert_at] + [ln + cr for ln in block] + keeper_lines[insert_at:]


def _file_key(path: Path) -> str:
    """One file's identity for comparing two `files:` entries: its resolved
    path, with letter case folded where the filesystem folds it (Windows)."""
    try:
        resolved = os.path.realpath(path)
    except (OSError, ValueError):
        resolved = str(path)
    return os.path.normcase(os.path.normpath(resolved))


def _entry_as_dict(entry) -> dict:
    """A `files:` entry as a mapping (a bare string entry names only its file)."""
    if isinstance(entry, dict):
        return dict(entry)
    return {'file': str(entry)}


def _ordered_entry(entry: dict) -> dict:
    """A files: entry with its keys in SPEC §14 order (file, role, copy,
    is_primary, original_filename, then the rest as they were)."""
    order = ('file', 'role', 'copy', 'is_primary', 'original_filename')
    out = {k: entry[k] for k in order if k in entry}
    out.update({k: v for k, v in entry.items() if k not in out})
    return out


def _source_ids_in(keywords: list[str]) -> list[tuple[str, str]]:
    """Every `SOURCE:` keyword in a list, as (the exact keyword, its lowercased id)."""
    out = []
    for kw in keywords or []:
        m = _SOURCE_KEYWORD_RE.match(str(kw).strip())
        if m:
            out.append((str(kw), m.group(1).lower()))
    return out


def _plan_merge_files(
    archive_root: Path, fha_config: dict, *, keeper_sid: str, retiree_sid: str,
    keeper_meta: dict, keeper_path: Path, retiree_meta: dict, warnings: list[str],
) -> list[dict]:
    """Work out, without writing, what happens to each of the retiree's files.

    One plan entry per `files:` entry: where the file is (`src`), what it is
    called afterwards (`dest`; the same for a photo), the keyword change, and
    the keeper `files:` entry it becomes.

    The rules, all from SPEC §12.1/§13 and `fha process --more`:
      - a documents-root file is renamed into the keeper's grammar,
        `{slug}[-{copy}][-{role}]_{keeper}.{ext}`, by the same
        `attachment_base` rule `--more` uses, beside where it is now; its
        first name stays on record as `original_filename:` (the one it
        already had, else the name it carries now);
      - a photos-root file is never renamed;
      - a file that can hold an embedded keyword gets `SOURCE: {keeper}` in
        place of the retiree's; a file carrying a third source's keyword
        refuses the merge, since it belongs to that source;
      - the retiree's main file stops being the main file (the keeper's
        stays), and a file whose role (and copy label) the keeper already
        has takes the next free copy letter, b, c, ..., so two scans of one
        record never share a name or a role.

    Raises `_MergeRefused` for anything that would stop the run part-way;
    `warnings` collects what goes ahead with a caveat (a document whose
    keyword cannot be read because exiftool is not installed).
    """
    entries = retiree_meta.get('files')
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise _MergeRefused(
            f"{fmt_id_display(retiree_sid)}'s files: list could not be read as a list. "
            'Run `fha lint` to see what is wrong there, fix it, then run the merge '
            'again. Nothing was written.', next_step='fha lint')
    keeper_files = keeper_meta.get('files')
    keeper_entries = ([_entry_as_dict(e) for e in keeper_files]
                      if isinstance(keeper_files, list) else [])
    # The same file can be spelled two ways (capitals on Windows, `./`, a
    # doubled slash), so files are compared as the file on disk they name.
    listed = {_file_key(resolve_path(str(e.get('file') or ''), fha_config, archive_root))
              for e in keeper_entries if str(e.get('file') or '').strip()}
    seen: dict[str, str] = {}
    used = {(files_entry_role(e), str(e.get('copy') or '').strip()) for e in keeper_entries}
    planned_dests: set[str] = set()
    keeper_shown = fmt_id_display(keeper_sid)
    # Either id at the end of a name is cut before the keeper's is added, so
    # a new name never carries the keeper's id twice.
    retired = frozenset({retiree_sid, keeper_sid})
    plans: list[dict] = []
    for raw in entries:
        entry = _entry_as_dict(raw)
        alias = str(entry.get('file') or '').strip().replace('\\', '/')
        if not alias:
            raise _MergeRefused(
                f"{fmt_id_display(retiree_sid)} has a files: entry with no file: line. "
                'Fix or remove it by hand, then run the merge again. Nothing was written.')
        root_alias = alias.split('/', 1)[0]
        key = _file_key(resolve_path(alias, fha_config, archive_root))
        if key in listed:
            raise _MergeRefused(
                f'{alias} is listed on both records (perhaps spelled differently). Remove it '
                f'from {fmt_id_display(retiree_sid)} by hand (it belongs on the keeper), then '
                'run the merge again. Nothing was written.')
        if key in seen:
            raise _MergeRefused(
                f'{fmt_id_display(retiree_sid)} lists the same file twice ({seen[key]} and '
                f'{alias}). Remove one of the two entries by hand, then run the merge again. '
                'Nothing was written.')
        seen[key] = alias
        role = files_entry_role(entry)
        copy = str(entry.get('copy') or '').strip()
        new_entry = {k: v for k, v in entry.items() if k != 'is_primary'}
        placeholder = str(entry.get('status') or '').strip().lower() == 'missing-fixture'
        if root_alias not in ('documents', 'photos'):
            raise _MergeRefused(
                f'{alias} is not under the documents or photos folder, so the merge cannot '
                'tell how to re-label it. Run `fha reconcile` to fix where the record '
                'says it is, then run the merge again. Nothing was written.',
                next_step='fha reconcile')
        src = resolve_path(alias, fha_config, archive_root)
        if not placeholder and not src.is_file():
            raise _MergeRefused(
                f'{alias} is listed on {fmt_id_display(retiree_sid)} but is not on disk, '
                'so it cannot be moved over. Run `fha reconcile` to find it, then run the '
                'merge again. Nothing was written.', next_step='fha reconcile')

        # Make the (role, copy) pair unique on the keeper: a second scan of the
        # same face takes the next copy letter, as SPEC §13 names copies.
        def _taken(r: str, c: str) -> bool:
            return (r, c) in used

        if _taken(role, copy):
            copy = next((c for c in _MERGE_COPY_LETTERS if not _taken(role, c)), '')
            if not copy:
                raise _MergeRefused(
                    f'the keeper already has every copy letter in use for role '
                    f"'{role or 'main file'}', so {alias} has no name left. Nothing was written.")
        dest = src
        if root_alias == 'documents' and not placeholder:
            # The name SPEC §13 gives it on the keeper; when another file
            # already has that name, the next free copy letter.
            candidates = [copy] + [c for c in _MERGE_COPY_LETTERS
                                   if c != copy and not _taken(role, c)]
            for candidate in candidates:
                base = attachment_base(without_retired_sid(src, retired), keeper_meta,
                                       keeper_path, keeper_shown, role, candidate or None)
                # The keeper's id is added once, at the end, and nowhere else.
                base = re.sub(rf'[-_]?{re.escape(keeper_sid)}', '', base,
                              flags=re.I).strip('-') or 'source'
                suffix = f'-{slugify(role)}' if role else ''
                if candidate:
                    suffix = f'-{slugify(candidate)}{suffix}'
                dest = src.with_name(f'{base}{suffix}_{keeper_shown}{src.suffix}')
                clash = (_file_key(dest) in planned_dests
                         or (dest.exists() and _file_key(dest) != _file_key(src)))
                if not clash:
                    copy = candidate
                    break
            else:
                raise _MergeRefused(
                    f'every name {alias} could take on the keeper is already used by '
                    'another file. Rename one of them by hand, then run the merge '
                    'again. Nothing was written.')
        used.add((role, copy))
        planned_dests.add(_file_key(dest))
        if copy:
            new_entry['copy'] = copy
        else:
            new_entry.pop('copy', None)
        new_alias = alias if dest == src else path_to_alias(dest, 'documents', fha_config,
                                                            archive_root)
        new_entry['file'] = new_alias
        if dest != src and not str(entry.get('original_filename') or '').strip():
            new_entry['original_filename'] = src.name

        # The keyword: read now, so a refusal comes before any write.
        add: list[str] = []
        remove: list[str] = []
        if not placeholder and (root_alias == 'photos'
                                or src.suffix.lower() in EMBEDDED_KEYWORD_DOCUMENT_SUFFIXES):
            try:
                existing = _merge_read_keywords(src)
            except RuntimeError as e:
                if root_alias == 'photos':
                    missing = isinstance(e, ExiftoolUnavailableError)
                    raise _MergeRefused(
                        f'could not read the keywords inside {alias} ({e}). A photo is tied '
                        'to its source by its SOURCE: keyword alone, so the merge needs to '
                        'change it. '
                        + ('Install exiftool (`fha doctor` checks for it), '
                           if missing else 'Check the file opens, ')
                        + 'then run the merge again. Nothing was written.',
                        next_step='fha doctor')
                warnings.append(
                    f'{alias}: its SOURCE: keyword could not be read ({e}), so it is renamed '
                    'without changing the keyword inside it. Nothing is lost: its new name '
                    f'and the record tie it to {keeper_shown}, and its old id still finds '
                    f'{keeper_shown} through aliases:.')
                existing = None
            if existing is not None:
                found = _source_ids_in(existing)
                strangers = sorted({sid for _, sid in found
                                    if sid not in (keeper_sid, retiree_sid)})
                if strangers:
                    shown = fmt_id_display(strangers[0])
                    raise _MergeRefused(
                        f'{alias} carries SOURCE: {shown} inside the file, so it belongs to '
                        f'that source, not to {fmt_id_display(retiree_sid)}. Run '
                        f'`fha find {shown}` to see that record, and settle which source '
                        'the file belongs to before merging. Nothing was written.',
                        next_step=f'fha find {shown}')
                canonical = f'SOURCE: {keeper_shown}'
                remove = [kw for kw, _ in found if kw != canonical]
                if canonical not in [kw for kw, _ in found]:
                    add = [canonical]
        plans.append({
            'alias': alias, 'new_alias': new_alias, 'src': src, 'dest': dest,
            'root': root_alias, 'entry': _ordered_entry(new_entry),
            'add': add, 'remove': remove, 'placeholder': placeholder,
        })
    return plans


def _merge_notes_paragraph(
    *, retiree_label: str, retiree_shown: str, date: str, n_files: int, n_claims: int,
    differences: list[tuple[str, object]], comments: list[tuple[str, str]] = (),
) -> str:
    """The paragraph the keeper's Notes gains, saying what was merged in and
    keeping whatever the retiree's record said that the keeper's does not:
    fields that differ, and every comment a person wrote in its frontmatter
    (`comments`, as (the key it followed or '', the comment text)), since the
    retiree's record file is about to be removed."""
    files = f'{n_files} file' + ('' if n_files == 1 else 's')
    claims = f'{n_claims} claim' + ('' if n_claims == 1 else 's')
    text = (f'Merged in from {retiree_label} on {date} with `fha source merge`: its {files} '
            f'and {claims} now belong to this source, and links to {retiree_shown} still '
            'find it here.')
    if differences:
        lines = [text, '', f'Where the record of {retiree_shown} said something this one '
                 'does not, it read:']
        for key, value in differences:
            shown = ' '.join(str(value).split()) if isinstance(value, str) else yaml_inline(value)
            lines.append(f'- {key}: {shown}')
        text = '\n'.join(lines)
    if comments:
        lines = [text, '', f'Comments written in the record of {retiree_shown}:']
        for key, comment in comments:
            lines.append(f'- {key}: {comment}' if key else f'- {comment}')
        text = '\n'.join(lines)
    return text


def _frontmatter_comment_pairs(lines: list[str]) -> list[tuple[str, str]]:
    """Each comment in a record's frontmatter as (the key on its line or '',
    the comment without its `#`), in order: the hand-written notes a merge
    must not lose when it removes the record. Read with the same trailing-
    comment rule `set-primary` uses to prove it kept them."""
    span = frontmatter_fence_span(lines)
    if span is None:
        return []
    out: list[tuple[str, str]] = []
    for line in lines[span[0] + 1:span[1]]:
        bare = line.rstrip('\r')
        stripped = bare.strip()
        if stripped.startswith('#'):
            text = stripped.lstrip('#').strip()
            if text:
                out.append(('', text))
            continue
        comment = _trailing_comment(bare).strip().lstrip('#').strip()
        if comment:
            out.append((bare.split(':', 1)[0].strip().lstrip('- ').strip(), comment))
    return out


def _is_source_purpose_block(block: list[str]) -> bool:
    """Is this run of `>` lines the scaffold's purpose block (SPEC §16a)?
    Matched by its signature, as `_lib` matches the person one; any other
    blockquote is the human's own and is kept."""
    return "This source's record" in ' '.join(block)


def _without_purpose_block(preamble: list[str]) -> list[str]:
    """The prose above a record's first section, less the purpose block."""
    out: list[str] = []
    run: list[str] = []
    for ln in preamble + ['']:
        if ln.lstrip().startswith('>'):
            run.append(ln)
            continue
        if run and not _is_source_purpose_block(run):
            out.extend(run)
        run = []
        out.append(ln)
    return _trimmed(out[:-1])


_PRIVATE_TOKEN_RE = re.compile(r'<!--\s*(/?)\s*private\s*-->', re.I)


def _open_fence_depth(text: str) -> int:
    """How many `<!-- private -->` openers are still open at the end of
    `text`, counted as `_lib.private_fence_spans` counts them (a closer with
    no opener is ignored)."""
    depth = 0
    for m in _PRIVATE_TOKEN_RE.finditer(text):
        if m.group(1):
            depth = max(depth - 1, 0)
        else:
            depth += 1
    return depth
_HEADING_LINE_RE = re.compile(r'^##\s+\S', re.M)


def _lift_private_spans(body: str) -> tuple[str, list[str]]:
    """Take out of a record body every private fence a section split would break.

    A `<!-- private -->` ... `<!-- /private -->` span that crosses a `## `
    heading would be cut in two when the sections are appended one by one,
    and the part without its opener would then show in shared output. So such
    a span, and an opener never closed, is lifted out whole, opener to closer,
    for the caller to keep in one place, with its `## ` headings demoted to
    `### ` so it stays inside Notes. An unclosed one gets a closer: it hid
    everything after it in its own record, and must not hide the keeper's
    later sections instead. A span inside one section stays where it is and
    moves with its section intact."""
    pieces: list[str] = []
    kept: list[str] = []
    pos = 0
    for start, end in private_fence_spans(body):
        span = body[start:end]
        still_open = _open_fence_depth(span)
        if not still_open and not _HEADING_LINE_RE.search(span):
            continue
        kept.append(body[pos:start])
        pos = end
        # One closer for each opener still open, counted the way the fence
        # reader counts them: an unclosed outer fence holding a closed inner
        # one, or two openers never closed, end closed in the keeper.
        piece = span.strip('\n') + '\n<!-- /private -->' * still_open
        # Its `## ` headings become `### `: in the keeper's Notes a level-2
        # heading would start a new section inside the fence, and every later
        # Notes append (the retiree's own notes, a later `fha source note`)
        # would land inside the fence and be hidden too.
        pieces.append(_HEADING_LINE_RE.sub(lambda m: '#' + m.group(0), piece))
    kept.append(body[pos:])
    return ''.join(kept), pieces


def _differing_fields(keeper_meta: dict, retiree_meta: dict) -> list[tuple[str, object]]:
    """The retiree's fields, outside the lists that are folded in, whose value
    the keeper does not share (whitespace aside), in the retiree's order."""
    out = []
    for key, value in retiree_meta.items():
        if key in _MERGE_NOT_NOTED or value in (None, '', [], {}):
            continue
        mine = keeper_meta.get(key)
        if isinstance(value, str) and isinstance(mine, str):
            if ' '.join(value.split()) == ' '.join(mine.split()):
                continue
        elif value == mine:
            continue
        out.append((key, value))
    return out


def _union(current: list, extra: list, key_of) -> list:
    """`extra` items not already in `current` (by `key_of`), in order."""
    seen = {key_of(x) for x in current}
    out = []
    for item in extra:
        k = key_of(item)
        if k in seen:
            continue
        seen.add(k)
        out.append(item)
    return out


def _link_key(item) -> str:
    """What makes two `people:`/`places:`/`aliases:` entries the same: the id
    or name they point at, without capitals."""
    refs = link_field_refs([item])
    return (refs[0] if refs else str(item)).strip().lower()


def _url_key(item) -> str:
    """What makes two `external_links:` entries the same: their URL."""
    if isinstance(item, dict):
        return str(item.get('url') or '').strip().rstrip('/').lower()
    return str(item).strip().rstrip('/').lower()


def _as_list(value) -> list:
    """A frontmatter list value as a list (absent or blank reads as empty)."""
    if value is None or value == '':
        return []
    return list(value) if isinstance(value, list) else [value]


def _restricted_marker(meta: dict) -> str | None:
    """A source's `restricted:` value as the exporters read it, or None.

    Absent, empty or false is unrestricted; `true` reads `true`; any other
    value is its own kind, lowercased (`dna`, `by-request`), as packet and
    site tell them apart (SPEC §19)."""
    value = meta.get('restricted')
    if value is None or value is False:
        return None
    text = 'true' if value is True else str(value).strip().lower()
    return None if text in ('', 'false', 'no', 'none') else text


def _privacy_markers(meta: dict) -> dict[str, str]:
    """Every marker on a source record that keeps it out of shared output.

    The three the exporters (site, packet, wikitree, gedcom) honour: the
    `restricted:` value, `source_type: dna` (left out unless asked for even
    with no `restricted:` line), and `rights: publication_ok: false`. Keyed by
    the line a person would add to a record, valued by what it says."""
    out: dict[str, str] = {}
    restricted = _restricted_marker(meta)
    if restricted:
        out['restricted'] = restricted
    if str(meta.get('source_type') or '').strip().lower() == 'dna':
        out['source_type'] = 'dna'
    rights = meta.get('rights')
    if isinstance(rights, dict) and rights.get('publication_ok') is not None:
        if str(rights.get('publication_ok')).strip().lower() in ('false', 'no', '0'):
            out['rights: publication_ok'] = 'false'
    return out


def _protection_gap(keeper_meta: dict, retiree_meta: dict) -> tuple[dict, dict] | None:
    """The two records' privacy markers, (keeper's, retiree's), when they
    disagree in a way the merge must not settle; None when it may go ahead.

    It may when the retiree carries no marker (its files only gain the
    keeper's protection) or when both carry exactly the same ones. Any other
    difference is the owner's decision: `true`, `dna` and `by-request` open
    under different export flags, and no tool can tell which of two
    protections he meant the merged record to have."""
    mine = _privacy_markers(keeper_meta)
    theirs = _privacy_markers(retiree_meta)
    if not theirs or mine == theirs:
        return None
    return mine, theirs


def _markers_shown(markers: dict) -> str:
    """Privacy markers as the lines a person reads in a record, or 'none'."""
    return ', '.join(f'`{k}: {v}`' for k, v in markers.items()) or 'none'


def _read_merge_record(path: Path, shown: str) -> tuple[str, dict, list]:
    """One record's exact text, strict frontmatter and parsed claims, or a refusal.

    Both records must read cleanly before the merge starts: a record that is
    already broken somewhere would make the post-write checks fail, and the
    merge would then refuse after its first write instead of before it."""
    try:
        text = read_text_exact(path)
    except (OSError, UnicodeDecodeError) as e:
        raise _MergeRefused(
            f'cannot read {path.name}: {e}. Check the file is not open in another program, '
            'then run the merge again. Nothing was written.', exit_code=EXIT_FAILURE)
    meta = parse_frontmatter_strict(text)
    rec = read_record(path)
    if meta is None or rec.get('parse_errors'):
        problem = _first_problem(rec.get('parse_errors') or []) or 'its frontmatter does not read'
        raise _MergeRefused(
            f'the record for {shown} ({path.name}) has a problem ({problem}), so it cannot be '
            'merged safely. Run `fha lint` to see it, fix it, then run the merge again. '
            'Nothing was written.', next_step='fha lint')
    claims = claims_in_text(text)
    if claims is None:
        if rec.get('claims'):
            raise _MergeRefused(
                f'the claims of {shown} are not in the usual fenced ```yaml block, so they '
                'cannot be moved safely. Run `fha lint --fix-claims-fence` to fence them, then '
                'run the merge again. Nothing was written.',
                next_step='fha lint --fix-claims-fence')
        claims = []
    return text, meta, claims


def run_source_merge(
    archive_root: Path, fha_config: dict, keeper_id: str, retiree_id: str, *,
    dry_run: bool = False,
) -> Result:
    """Merge the retiree source into the keeper; return a `_lib.Result`.

    SPEC §9 "Merged sources": when two source records prove to be one piece
    of evidence, the keeper keeps the other's s-id in `aliases:`, the other's
    files are attached to the keeper, and the other record is removed. Old
    links and filenames still find the keeper through that alias, and lint
    lists them (W107) for cleanup.

    What moves:
      - files (see `_plan_merge_files`): documents renamed into the keeper's
        grammar, photos never renamed, every embedded `SOURCE:` keyword
        changed to the keeper's;
      - claims, ids unchanged, into the keeper's `## Claims`;
      - `## Notes`, `## Stories` and any other section, appended to the
        keeper's section of the same name, with a dated paragraph in Notes
        saying what was merged and keeping every field (citation, title,
        repository, ...) where the retiree's record said something the
        keeper's does not;
      - `aliases:` (the retiree's id and its own aliases), `people:`,
        `places:`, `external_links:` and `ai_passes:`, without repeats;
      - a `merged_from:` entry (retiree id, title, date, each file's old and
        new name, the claim ids) so the merge can be undone by hand later.

    Refusals (every one before the first write, and the same under
    `--dry-run`): an invalid or repeated id, an id that is not a source,
    either record missing (exit 1) or unreadable, both records holding
    accepted claims whose people do not overlap at all (a merged person
    counts as its survivor; likely two different records, and the people are
    named), a retiree carrying a privacy marker the keeper does not carry the
    same way (`_protection_gap`), a claim id on both, a file listed on both,
    twice on the retiree, in neither asset root or missing from disk, a file
    carrying a third source's keyword, a photo whose keyword cannot be read.

    Transaction: renames, keyword writes, the keeper's rewrite and the
    retiree's removal run in that order, each with its undo step; any
    failure runs the undo steps in reverse and exits 3, saying plainly what
    (if anything) could not be put back. Ctrl+C part-way runs the undo steps
    too, then re-raises: a merge stopped between two files would leave some
    named for one source and some for the other. The plan is reported in the
    future tense, and `Merged ...` is added only once every write succeeded.

    `data`: {'status': 'ok'|'dry-run'|'refused'|'not-found'|'failed',
    'keeper', 'retiree', 'keeper_path', 'retiree_path', 'files': [{'from',
    'to'}], 'claims': [...]}. Exit codes: 0 merged or previewed, 1 a record
    not found, 2 refused, 3 a read or write failed (rolled back).
    """
    result = Result(data={'status': None, 'keeper': None, 'retiree': None,
                          'keeper_path': None, 'retiree_path': None,
                          'files': [], 'claims': []})
    try:
        return _run_source_merge(archive_root, fha_config, keeper_id, retiree_id,
                                 dry_run=dry_run, result=result)
    except _MergeRefused as refusal:
        return result_fail(result, refusal.status, refusal.message,
                           exit_code=refusal.exit_code,
                           level='warning' if refusal.exit_code == EXIT_WARNINGS else 'error',
                           next_step=refusal.next_step)


def _check_merge_ids(keeper_id: str, retiree_id: str) -> tuple[str, str]:
    """Both ids, normalized, or a refusal naming what is wrong with them."""
    example = 'fha source merge s-2b3c4d5e6f s-7q2x9c4m1k'
    for raw in (keeper_id, retiree_id):
        if not is_valid_id(str(raw or '')):
            raise _MergeRefused(
                f'{raw!r} is not a valid id. Source ids look like s-2b3c4d5e6f: an s, a '
                'dash and 10 characters from the archive alphabet. Give the record to '
                f'keep first and the one to fold into it second, e.g. `{example}`.')
        if id_type_of(raw) != 'S':
            raise _MergeRefused(
                f'{fmt_id_display(raw)} is not a source id (source ids start with s-). '
                'Only two sources can be merged here; two people are merged with '
                '`fha confirm merge`.', next_step='fha confirm merge --help')
    keeper, retiree = normalize_id(keeper_id), normalize_id(retiree_id)
    if keeper == retiree:
        raise _MergeRefused(
            f'both ids are {fmt_id_display(keeper)}. Give two different sources: the one '
            f'to keep, then the one to fold into it, e.g. `{example}`.')
    return keeper, retiree


def _run_source_merge(archive_root: Path, fha_config: dict, keeper_id: str,
                      retiree_id: str, *, dry_run: bool, result: Result) -> Result:
    """`run_source_merge`'s body: preflight everything, then write, then report."""
    keeper_sid, retiree_sid = _check_merge_ids(keeper_id, retiree_id)
    keeper_shown, retiree_shown = fmt_id_display(keeper_sid), fmt_id_display(retiree_sid)
    result.data.update(keeper=keeper_shown, retiree=retiree_shown)
    if is_working_copy(archive_root):
        raise _MergeRefused(
            'This is a working copy (it holds no photos or documents), so the files of a '
            'merge cannot be renamed here. Run the merge in the full archive. Nothing was '
            'written.')

    paths = {}
    for sid, shown in ((keeper_sid, keeper_shown), (retiree_sid, retiree_shown)):
        path = find_source_record_path(archive_root, sid)
        if path is None:
            raise _MergeRefused(
                f'No source record found for {shown} under {archive_root / "sources"}. '
                f'Check the id with `fha find {shown}`.',
                exit_code=EXIT_WARNINGS, status='not-found', next_step=f'fha find {shown}')
        paths[sid] = path
    keeper_path, retiree_path = paths[keeper_sid], paths[retiree_sid]
    result.data.update(keeper_path=str(keeper_path), retiree_path=str(retiree_path))

    keeper_text, keeper_meta, keeper_claims = _read_merge_record(keeper_path, keeper_shown)
    retiree_text, retiree_meta, retiree_claims = _read_merge_record(retiree_path, retiree_shown)
    keeper_label = _source_label(keeper_text, keeper_sid)
    retiree_label = _source_label(retiree_text, retiree_sid)

    # ── Refusals about what the two records say ──
    # A claim naming a merged person counts for the survivor (SPEC §9), so a
    # record citing the old p-id is about the same person as one citing the new.
    survivors = scan_person_merge_survivors(archive_root)
    keeper_people = {survivors.get(p, p) for p in _accepted_claim_people(keeper_claims)}
    retiree_people = {survivors.get(p, p) for p in _accepted_claim_people(retiree_claims)}
    if keeper_people and retiree_people and not (keeper_people & retiree_people):
        names = record_display_names(archive_root, keeper_people | retiree_people)

        def _who(ids: set[str]) -> str:
            return ', '.join(f'{names[i]} ({fmt_id_display(i)})' if i in names
                             else fmt_id_display(i) for i in sorted(ids))
        raise _MergeRefused(
            f'these look like two different records. The accepted claims on {keeper_label} '
            f'are about {_who(keeper_people)}; those on {retiree_label} are about '
            f'{_who(retiree_people)}, and no one is on both. If they really are one record, '
            'correct the person on the wrong claims first (`fha claim <c-id> --help` shows '
            'how), then run the merge again. Nothing was written.',
            next_step='fha claim --help')
    gap = _protection_gap(keeper_meta, retiree_meta)
    if gap is not None:
        mine, theirs = gap
        raise _MergeRefused(
            f'the two records are protected differently from shared output: '
            f'{keeper_label} has {_markers_shown(mine)}, and {retiree_label} has '
            f'{_markers_shown(theirs)}. A merge cannot choose between them for you. Decide '
            'which protection the merged record should carry, write those same lines on '
            f'both records ({keeper_path.name} and {retiree_path.name}), then run the merge '
            'again. Nothing was written.')
    keeper_cids = [normalize_id(str(c.get('id'))) for c in keeper_claims
                   if isinstance(c, dict) and c.get('id')]
    retiree_cids = [normalize_id(str(c.get('id'))) for c in retiree_claims
                    if isinstance(c, dict) and c.get('id')]
    if len(retiree_cids) != len([c for c in retiree_claims if isinstance(c, dict)]):
        raise _MergeRefused(
            f'a claim on {retiree_label} has no id, so it could not be traced after the '
            f'merge. Run `fha lint` to see it, give it an id (`fha id mint C`), then run '
            'the merge again. Nothing was written.', next_step='fha lint')
    shared = sorted(set(keeper_cids) & set(retiree_cids))
    if shared:
        raise _MergeRefused(
            f'claim {fmt_id_display(shared[0])} is on both records. Remove the duplicate from '
            f'{retiree_path.name} by hand, then run the merge again. Nothing was written.',
            next_step='fha lint')

    # ── The files ──
    warnings: list[str] = []
    plans = _plan_merge_files(
        archive_root, fha_config, keeper_sid=keeper_sid, retiree_sid=retiree_sid,
        keeper_meta=keeper_meta, keeper_path=keeper_path, retiree_meta=retiree_meta,
        warnings=warnings)

    # ── The keeper's new text, built and checked before anything is written ──
    today = _merge_today()
    cr = '\r' if '\r\n' in keeper_text else ''
    lines = keeper_text.split('\n')
    retiree_aliases = [retiree_shown, *_as_list(retiree_meta.get('aliases'))]
    additions = {
        'aliases': _union(_as_list(keeper_meta.get('aliases')), retiree_aliases, _link_key),
        'people': _union(_as_list(keeper_meta.get('people')),
                         _as_list(retiree_meta.get('people')), _link_key),
        'places': _union(_as_list(keeper_meta.get('places')),
                         _as_list(retiree_meta.get('places')), _link_key),
        'external_links': _union(_as_list(keeper_meta.get('external_links')),
                                 _as_list(retiree_meta.get('external_links')), _url_key),
        'ai_passes': _union(_as_list(keeper_meta.get('ai_passes')),
                            _as_list(retiree_meta.get('ai_passes')), repr),
        'files': [p['entry'] for p in plans],
        'merged_from': [*_as_list(retiree_meta.get('merged_from')), {
            'source': retiree_shown,
            'title': str(retiree_meta.get('title') or ''),
            'date': today,
            'files': [{'from': p['alias'], 'to': p['new_alias']} for p in plans],
            'claims': [fmt_id_display(c) for c in retiree_cids],
        }],
    }
    planned_meta = dict(keeper_meta)
    for key in _MERGE_LIST_KEYS:
        new_items = additions[key]
        if not new_items:
            continue
        all_items = _as_list(keeper_meta.get(key)) + new_items
        lines = _extend_frontmatter_list(lines, key, all_items, new_items, cr)
        planned_meta[key] = all_items

    lines = _merge_claims_text(lines, retiree_text.split('\n'), cr)

    span = frontmatter_fence_span(lines)
    body_start = span[1] + 1
    retiree_lines = retiree_text.split('\n')
    rspan = frontmatter_fence_span(retiree_lines)
    retiree_body = '\n'.join(ln.rstrip('\r') for ln in retiree_lines[rspan[1] + 1:])
    # A private fence that a section split would cut in two is kept whole.
    retiree_body, private_pieces = _lift_private_spans(retiree_body)
    preamble, sections = _body_sections(retiree_body.split('\n'), 0)
    paragraph = _merge_notes_paragraph(
        retiree_label=retiree_label, retiree_shown=retiree_shown, date=today.isoformat(),
        n_files=len(plans), n_claims=len(retiree_cids),
        differences=_differing_fields(keeper_meta, retiree_meta),
        comments=_frontmatter_comment_pairs(retiree_lines))
    lines, _, _ = append_paragraph_to_section(lines, body_start, 'Notes', paragraph, cr)
    for piece in private_pieces:
        lines, _, _ = append_paragraph_to_section(lines, body_start, 'Notes', piece, cr)
    # Prose above the first section is the human's, except the scaffold's
    # purpose block, so it is kept in Notes too.
    loose = _without_purpose_block(preamble)
    if any(ln.strip() for ln in loose):
        lines, _, _ = append_paragraph_to_section(
            lines, body_start, 'Notes', '\n'.join(loose), cr)
    moved_sections = []
    for heading, content in sections:
        if heading == 'Claims':
            # Prose under the heading but outside the claims fence is kept too.
            content = _outside_fence(content)
            heading = 'Notes'
        if _is_placeholder(content):
            continue
        lines, _, _ = append_paragraph_to_section(
            lines, body_start, heading, '\n'.join(_trimmed(content)), cr)
        if heading not in moved_sections:
            moved_sections.append(heading)
    new_text = '\n'.join(lines)

    after_meta = parse_frontmatter_strict(new_text)
    if after_meta != planned_meta:
        changed = sorted(k for k in set(planned_meta) | set(after_meta or {})
                         if (after_meta or {}).get(k) != planned_meta.get(k))
        raise _MergeRefused(
            f'the details at the top of {keeper_path.name} could not be rewritten safely '
            f'(the {", ".join(changed) or "frontmatter"} field would not read back as '
            'planned), so nothing was written. Run `fha lint --format-write` to tidy it, then run '
            'the merge again.', exit_code=EXIT_FAILURE, next_step='fha lint --format-write')
    after_claims = claims_in_text(new_text)
    if after_claims != keeper_claims + retiree_claims:
        raise _MergeRefused(
            f"the claims could not be put into {keeper_path.name}'s ## Claims block safely, "
            'so nothing was written. Run `fha lint --format-write` to tidy both records, then run the '
            'merge again.', exit_code=EXIT_FAILURE, next_step='fha lint --format-write')
    # People, places and notes the merge adds link in the archive's link
    # form (SPEC §18); the claims block keeps the ID form.
    new_text = convert_written_links(keeper_text, new_text,
                                     link_writer(archive_root, fha_config))
    # With links: obsidian, every link to the removed record and every claim
    # link to a claim that moves is pointed at the keeper's file across the
    # archive (SPEC §18). Planned before anything is written, so the preview
    # counts what the write does; the keeper's own links join its one write.
    link_edits = plan_rename_links(
        archive_root,
        rename_link_writer(archive_root, fha_config, [retiree_sid],
                           survivors={retiree_sid: keeper_sid},
                           claim_sources={c: keeper_sid for c in retiree_cids}),
        pending={keeper_path: new_text}, gone=[retiree_path])
    for edit in link_edits:
        if edit.path == keeper_path:
            new_text = edit.after
    other_link_edits = [e for e in link_edits if e.path != keeper_path]
    links_target = f"the kept record's file ({keeper_shown})"
    links_step = rename_links_message(link_edits, dry_run=dry_run, target=links_target,
                                      step=True)

    # ── The preview: the same lines for --dry-run and for the real run ──
    result.data['files'] = [{'from': p['alias'], 'to': p['new_alias']} for p in plans]
    result.data['claims'] = [fmt_id_display(c) for c in retiree_cids]
    # Future tense: the plan is said before anything is written, so a run
    # that fails part-way never shows a line claiming it was done.
    head = (f'[dry-run] Would merge {retiree_label} into {keeper_label}. The merge would:'
            if dry_run else
            f'Merging {retiree_label} into {keeper_label}. The merge will:')
    result.add('info', head)
    for p in plans:
        keyword = (f', and set its SOURCE: keyword to {keeper_shown}'
                   if p['add'] or p['remove'] else '')
        if p['placeholder']:
            result.add('info', f'  list {p["alias"]} (a placeholder, not on disk) on '
                               f'{keeper_shown}')
        elif p['dest'] != p['src']:
            result.add('info', f'  rename {p["alias"]} to {p["dest"].name}{keyword}')
        elif p['root'] == 'photos':
            result.add('info', f'  leave photo {p["alias"]} where it is (photos are never '
                               f'renamed){keyword}')
        else:
            result.add('info', f'  keep the name of {p["alias"]}{keyword}')
    if retiree_cids:
        result.add('info', f'  move {len(retiree_cids)} claim(s), ids unchanged: '
                           + ', '.join(fmt_id_display(c) for c in retiree_cids))
    gained = []
    for key, one, many in (('people', 'person', 'people'), ('places', 'place', 'places'),
                           ('external_links', 'web link', 'web links'),
                           ('ai_passes', 'AI pass', 'AI passes')):
        n = len(additions[key])
        if n:
            gained.append(f'{n} {one if n == 1 else many}')
    result.add('info', f'  keep {retiree_shown} in the aliases: of {keeper_shown} (old links '
                       'still find it) and add a merged_from: entry'
                       + (', and add ' + ', '.join(gained) if gained else ''))
    result.add('info', f"  write in the Notes of {keeper_shown} what was merged"
                       + (' and where the two records differ'
                          if _differing_fields(keeper_meta, retiree_meta) else '')
                       + ('; add ' + ', '.join(moved_sections) + f' from {retiree_shown} to '
                          f'the same sections of {keeper_shown}' if moved_sections else ''))
    result.add('info', f'  remove the record {retiree_path.name}')
    if links_step:
        result.add('info', f'  {links_step}')
    for text in warnings:
        result.add('warning', text)
    if dry_run:
        result.data['status'] = 'dry-run'
        result.add('info', 'Nothing was written. Run the same command without --dry-run '
                           'to merge.')
        return result

    # ── Write: every step has an undo, run in reverse on any failure ──
    undo: list = []
    undo_copies: list[Path] = []
    backup = OriginalBackup(archive_root, fha_config)
    keyword_files = [p['dest'] for p in plans if p['add'] or p['remove']]
    if keyword_files:
        backup.announce(keyword_files)
    # A failed undo step names its file as it is called on disk once the
    # undo has finished: a later step may already have renamed it back.
    names: dict[str, tuple[Path, Path]] = {}

    def _file_tag(src: Path, dest: Path) -> str:
        tag = f'\x00{len(names)}\x00'
        names[tag] = (src, dest)
        return tag

    def _named_now(failed: list[str]) -> list[str]:
        out = []
        for text in failed:
            for tag, (src, dest) in names.items():
                now = dest if dest.exists() or not src.exists() else src
                text = text.replace(tag, now.name)
            out.append(text)
        return out

    try:
        for p in plans:
            src, dest = p['src'], p['dest']
            if dest != src:
                src.rename(dest)
                undo.append((f'rename {dest.name} back to {src.name}',
                             lambda s=src, d=dest: d.rename(s)))
            if p['add'] or p['remove']:
                if p['root'] == 'documents':
                    # A document is put back byte for byte (exiftool appends
                    # to a PDF, so removing a keyword is not the inverse).
                    copy = take_undo_copy(dest)
                    undo_copies.append(copy)
                    undo.append((f'restore the original bytes of {_file_tag(src, dest)}',
                                 lambda c=copy, d=dest: _restore_or_say_where(c, d)))
                else:
                    # A photo's inverse is the opposite keyword write: photos
                    # are never byte-copied aside (they can be very large).
                    # Registered first: undoing a write that never happened
                    # is a no-op.
                    undo.append((f'put the SOURCE: keyword of {_file_tag(src, dest)} back',
                                 lambda d=dest, a=p['add'], r=p['remove']:
                                 _raise_on(_merge_write_keywords(d, add=r, remove=a,
                                                                 backup=backup))))
                err = _merge_write_keywords(dest, add=p['add'], remove=p['remove'],
                                            backup=backup)
                if err is not None:
                    raise RuntimeError(f'could not change the SOURCE: keyword in {dest.name}: '
                                       f'{err}')
        write_text_exact_atomic(keeper_path, new_text)
        undo.append((f'restore {keeper_path.name}',
                     lambda: write_text_exact_atomic(keeper_path, keeper_text)))
        for edit in other_link_edits:
            # Registered once the write has landed: an atomic write that
            # failed left the file as it was, and putting back a file that
            # could not be written (read-only, held open) would fail again
            # and be reported as damage that is not there.
            write_text_exact_atomic(edit.path, edit.after)
            undo.append((f'restore the links in {edit.path.name}',
                         lambda e=edit: write_text_exact_atomic(e.path, e.before)))
        retiree_path.unlink()
        undo.append((f'put {retiree_path.name} back',
                     lambda: write_text_exact_atomic(retiree_path, retiree_text)))
    except (KeyboardInterrupt, SystemExit):
        # Ctrl+C part-way is put back too: a merge stopped between files
        # would leave some files named for one source and some for the other.
        failed = _named_now(run_undo_steps(undo))
        if not failed:
            discard_undo_copies(undo_copies)
        else:
            # The exception goes on up, so the Result never reaches the
            # screen: what could not be put back is said here, in the same
            # words the ordinary failure uses, or nobody would know.
            print('ERROR: ' + _undo_failed_text('the merge was stopped (Ctrl+C)', failed),
                  file=sys.stderr)
        raise
    except Exception as e:
        failed = _named_now(run_undo_steps(undo))
        if not failed:
            discard_undo_copies(undo_copies)
        for level, text in backup.drain_messages():
            result.add(level, text)
        if failed:
            return result_fail(result, 'failed', _undo_failed_text(f'the merge failed ({e})',
                                                                   failed),
                               next_step='fha doctor')
        return result_fail(
            result, 'failed',
            f'The merge failed ({e}), and everything was put back as it was. Nothing changed.',
            next_step='fha doctor')
    discard_undo_copies(undo_copies)
    for level, text in backup.drain_messages():
        result.add(level, text)
    result.data['status'] = 'ok'
    moved = sum(1 for p in plans if p['dest'] != p['src'] or p['add'] or p['remove'])
    result.add('info', f'Merged {retiree_label} into {keeper_label}: {len(plans)} file(s) '
                       f'moved over ({moved} renamed or re-keyworded), {len(retiree_cids)} '
                       f'claim(s) moved, and {retiree_path.name} removed.')
    links_line = rename_links_message(link_edits, dry_run=False, target=links_target)
    if links_line:
        result.add('info', links_line)
    for p in plans:
        if p['dest'] != p['src'] or p['add'] or p['remove']:
            result.note_changed(p['dest'])
    for edit in other_link_edits:
        result.note_changed(edit.path)
    result.note_changed(keeper_path)
    result.note_changed(retiree_path)
    photos = any(p['root'] == 'photos' and (p['add'] or p['remove']) for p in plans)
    result.add('info',
               'Next: run `fha index` so search, lint and the workbench see the merge'
               + (', then `fha photoindex` so the photo catalog reads the new keywords'
                  if photos else '')
               + f'. `fha lint` then lists any link that still names {retiree_shown} (W107), '
               'for cleanup when convenient.',
               next_step='fha index')
    return result


def _undo_failed_text(what_happened: str, failed: list[str]) -> str:
    """What a merge says when putting things back could not finish: each step
    left undone (a document's step names where its original bytes are kept)
    and the commands that help. One wording for a failure and for Ctrl+C."""
    return (f'{what_happened[0].upper()}{what_happened[1:]}, and putting things back could '
            'not finish. Could not undo: ' + '; '.join(failed) + '. Run `fha doctor` to see '
            'what is off; `fha reconcile` re-ties a renamed file to its record.')


def _restore_or_say_where(undo_copy: Path, path: Path) -> None:
    """Put a document's original bytes back; on failure, say where they are kept."""
    try:
        restore_undo_copy(undo_copy, path)
    except OSError as e:
        raise RuntimeError(f'{e}; its original bytes are kept at {undo_copy}') from e


def _raise_on(err: str | None) -> None:
    """Turn a keyword writer's failure text into an exception, for an undo step."""
    if err is not None:
        raise RuntimeError(err)


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
        copy_label=getattr(args, 'copy_label', None),
        # The engine never prints; this is how a long pdftotext run gets
        # announced on the terminal before it starts, not after.
        progress=lambda text: print(text, flush=True),
        dry_run=bool(getattr(args, 'dry_run', False))))


def _add_extract_arguments(sub: argparse._SubParsersAction) -> None:
    """Register the extract verb on a group subparser (shared by both mains)."""
    x = sub.add_parser(
        'extract',
        help="Dump a source PDF's embedded text layer into a derived companion file.",
        description=('Reads the source\'s PDF (its primary file, or the only PDF '
                     'it lists; when a second digitization is attached as '
                     'primary:LABEL, the one without a label unless --copy '
                     'names another) and writes '
                     'the embedded text layer, page by page, into a '
                     '[Page N]-labeled companion the assistant can mine like a '
                     'transcript, in seconds instead of reading hundreds of pages '
                     'by eye. The PDF itself is never touched. Needs the pypdf '
                     f'helper package ({pip_command("pypdf")}).'),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    x.add_argument('source_id', metavar='s-id',
                   help='The source whose PDF to extract (e.g. s-2b3c4d5e6f).')
    x.add_argument('--pages', metavar='RANGES',
                   help='Only these pages, e.g. `1-60` or `1-60,102,200-210` '
                        '(default: every page).')
    x.add_argument('--copy', metavar='LABEL', dest='copy_label',
                   help='Read the PDF whose files: entry carries this copy: label '
                        '(e.g. `familysearch`, the label given to '
                        '`fha process --more FILE primary:familysearch`) instead '
                        'of the unlabeled primary.')
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

  fha source clear-keyword s-2b3c4d5e6f --keyword "Margaret Hartley"
  fha source clear-keyword s-2b3c4d5e6f --keyword "Margaret Hartley" \\
      --replace-with "Margaret Cole"

Reads the file's keyword fields (IPTC Keywords, XMP dc:subject and
Lightroom's hierarchical keywords), matches --keyword case-insensitively, and
removes the exact on-file value from every field it sits in, so a keyword
typed slightly differently on the command line refuses instead of silently
doing nothing. --replace-with writes the correction into every keyword field
the file takes, skipping one that already names it (a Lightroom "People|..."
entry counts). The preview shows exactly what each field will hold.
Documents-root only (`fha lint --with-exif` W131 finds candidates);
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
    ck.add_argument('source_id', metavar='s-id',
                    help='The source whose documents-root file to correct.')
    ck.add_argument('--keyword', metavar='TEXT', required=True,
                    help='The keyword to remove, exactly as it reads (matched '
                         'case-insensitively against the file).')
    ck.add_argument('--replace-with', metavar='TEXT', dest='replace_with',
                    help='Add this keyword in place of the one removed, in every '
                         'keyword field that does not already name it. Omit to just '
                         'clear it.')
    ck.add_argument('--file', metavar='NAME',
                    help='Which documents-root file to correct, when the source '
                         'lists more than one (a filename or its files: alias).')
    ck.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS,
                    help='Archive root (auto-detected if omitted).')
    ck.add_argument('--dry-run', action='store_true', dest='dry_run',
                    help='Preview the change without writing.')
    ck.set_defaults(func=_cmd_source_clear_keyword)


def _cmd_source_set_primary(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args, command='fha source set-primary')
    if archive_root is None:
        return EXIT_FAILURE
    # No fha.yaml load: the edit is to the record's own text, and a photos-root
    # alias is recognized by its root NAME ('photos/...'), never by where that
    # root lives on disk.
    return _emit(run_source_set_primary(
        archive_root, source_id=args.source_id, file=args.file,
        dry_run=bool(getattr(args, 'dry_run', False))))


_SET_PRIMARY_DESCRIPTION = """Choose which photo is the main image of a source (its front, not its back).

  fha source set-primary s-2b3c4d5e6f --file "Smith-Farm-01400.tif" --dry-run
  fha source set-primary s-2b3c4d5e6f --file "Smith-Farm-01400.tif"

Changes only the source record's files: list: the named photo becomes the
main image (is_primary: true), and the photo that was the main
image is listed by what its name says it is (role: back, say). No photo is
renamed or touched, and the record's title and filename stay as they are.
`fha doctor` names the records whose main image is a photo's back. Preview
first with --dry-run."""


def _add_set_primary_arguments(sub: argparse._SubParsersAction) -> None:
    """Register the set-primary verb on a group subparser (shared by both mains)."""
    sp = sub.add_parser(
        'set-primary',
        help="Choose which photo is a source's main image (e.g. its front, not its back).",
        description=_SET_PRIMARY_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sp.add_argument('source_id', metavar='s-id',
                    help='The source whose main image to change (e.g. s-2b3c4d5e6f).')
    sp.add_argument('--file', metavar='NAME', required=True,
                    help='The photo to make the main image: its filename, or its '
                         'full files: path when two photos share a name.')
    sp.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS,
                    help='Archive root (auto-detected if omitted).')
    sp.add_argument('--dry-run', action='store_true', dest='dry_run',
                    help='Preview the change without writing.')
    sp.set_defaults(func=_cmd_source_set_primary)


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


def _cmd_source_merge(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args, command='fha source merge')
    if archive_root is None:
        return EXIT_FAILURE
    # strict=True: the files are found through the configured roots, so a
    # malformed fha.yaml must refuse rather than send every rename to the
    # internal documents/ folder.
    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE
    return _emit(run_source_merge(
        archive_root, fha_config, args.keeper, args.retiree,
        dry_run=bool(getattr(args, 'dry_run', False))))


_MERGE_DESCRIPTION = """Make two source records for one piece of evidence into one.

  fha source merge s-2b3c4d5e6f s-7q2x9c4m1k --dry-run
  fha source merge s-2b3c4d5e6f s-7q2x9c4m1k

The first id is the record to keep, the second the one folded into it: an
index entry and the document it indexes, or one record saved from two sites.
The second record's files join the first (documents are renamed for it,
photos keep their names), with their SOURCE: keyword changed to match; its
claims move over with their ids; its notes, people, places, web links and AI
passes are added; and its record file is removed. Its id stays in the kept
record's aliases:, so every old link still finds it, and a merged_from:
entry lists what moved.

Refused, with nothing written, when the two records' accepted claims are
about different people. Preview first with --dry-run, then run `fha index`."""


def _add_merge_arguments(sub: argparse._SubParsersAction) -> None:
    """Register the merge verb on a group subparser (shared by both mains)."""
    m = sub.add_parser(
        'merge',
        help='Make two source records for one piece of evidence into one.',
        description=_MERGE_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    m.add_argument('keeper', metavar='keeper',
                   help='The source to keep (e.g. s-2b3c4d5e6f).')
    m.add_argument('retiree', metavar='retiree',
                   help='The source folded into it and then removed (e.g. s-7q2x9c4m1k).')
    m.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS,
                   help='Archive root (auto-detected if omitted).')
    m.add_argument('--dry-run', action='store_true', dest='dry_run',
                   help='Preview the merge without writing.')
    m.set_defaults(func=_cmd_source_merge)


def _make_group_help(parser: argparse.ArgumentParser):
    """Bare `fha source` prints the group help and exits 2 (a verb is required)."""
    def _cmd(args: argparse.Namespace) -> int:
        parser.print_help()
        return 2
    return _cmd


# User-facing --help text (the module docstring stays developer-facing).
# ── fha source extract ───────────────────────────────────────────────

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


def _copy_label(entry: dict) -> str:
    """A files: entry's `copy:` label as text, '' when it has none.

    `fha process --more FILE primary:familysearch` writes `copy: familysearch`
    (SPEC §13 "Copies: `b`, `c`, `negative`..."): a second digitization of
    the same evidence. A blank or null value is no
    label at all, and YAML may hand back a number for `copy: 2`, hence str().
    """
    value = entry.get('copy')
    if value is None:
        return ''
    return str(value).strip()


def _choose_extract_pdf(
    pdf_entries: list[dict], sid_display: str, copy_label: str | None,
) -> tuple[dict | None, str | None]:
    """Pick the one PDF entry to read, or (None, plain refusal).

    Never a guess: text dumped from the wrong PDF would anchor claims to the
    wrong pages. The order (#280):

    - `--copy LABEL` given: the PDF whose `copy:` label matches (ignoring
      capitals). An unknown label refuses, listing the labels there are.
    - Otherwise the main-file PDF (`is_primary: true`, SPEC §14). Of several
      so marked, the one with NO `copy:` label is the book's main copy and is
      read. Only when that still leaves none or several is it a refusal, and
      the refusal names `--copy`.
    - No primary: the single PDF the inventory lists; several refuse.
    """
    labels = sorted({_copy_label(e) for e in pdf_entries if _copy_label(e)},
                    key=str.casefold)
    label_list = ', '.join(labels)
    example = (f'`fha source extract {sid_display} --copy {labels[0]}`'
               if labels else '')

    if copy_label is not None:
        want = copy_label.strip().casefold()
        if not want:
            return None, ('--copy needs a label, e.g. `--copy familysearch`'
                          + (f' (this source\'s labelled PDFs: {label_list})'
                             if labels else '') + '.')
        matches = [e for e in pdf_entries if _copy_label(e).casefold() == want]
        if len(matches) > 1:
            primaries = [e for e in matches if files_entry_is_main(e)]
            if len(primaries) == 1:
                matches = primaries
        if len(matches) == 1:
            return matches[0], None
        if not matches:
            if labels:
                return None, (f'{sid_display} has no PDF labelled copy: {copy_label.strip()} - '
                              f'its labelled PDF copies are: {label_list}. '
                              f'Re-run with one of them, e.g. {example}.')
            return None, (f'{sid_display} has no PDF labelled copy: {copy_label.strip()} - '
                          'none of its PDFs carries a copy: label. Re-run '
                          f'without --copy: `fha source extract {sid_display}`.')
        shown = ', '.join(str(e.get('file')) for e in matches)
        return None, (f'{sid_display} has more than one PDF labelled copy: '
                      f'{copy_label.strip()} ({shown}), so --copy cannot tell them '
                      'apart. Give one of them a different copy: label in the '
                      f'record, then re-run `fha source extract {sid_display} '
                      f'--copy LABEL`.')

    primary_pdfs = [e for e in pdf_entries if files_entry_is_main(e)]
    if len(primary_pdfs) > 1:
        unlabelled = [e for e in primary_pdfs if not _copy_label(e)]
        if len(unlabelled) == 1:
            return unlabelled[0], None
        if not unlabelled:
            return None, (f'{sid_display} has {len(primary_pdfs)} PDFs marked '
                          'as the main file and every one carries a copy: label '
                          f'({label_list}), so none is the main copy. Name the '
                          f'one to read, e.g. {example}.')
        shown = ', '.join(str(e.get('file')) for e in unlabelled)
        return None, (f'{sid_display} has {len(unlabelled)} PDFs marked as the '
                      f'main file with no copy: label ({shown}), so extraction '
                      'cannot tell which is the main copy and will not guess. '
                      'Add a copy: label (e.g. `copy: b`) to the files: entry '
                      'of every one but the main copy, and `is_primary: true` '
                      'on the main copy alone - then '
                      f're-run `fha source extract {sid_display}`'
                      + (f', or read a labelled copy now with {example}'
                         if labels else '') + '.')
    if primary_pdfs:
        return primary_pdfs[0], None
    if len(pdf_entries) == 1:
        return pdf_entries[0], None
    if not pdf_entries:
        return None, (f'{sid_display} lists no PDF in its files: inventory - this '
                      'extraction reads PDF text layers only. For an image scan, '
                      'read it with vision (the process-source page-window '
                      'doctrine).')
    shown = ', '.join(str(e.get('file')) for e in pdf_entries)
    return None, (f'{sid_display} lists more than one PDF ({shown}) and none is '
                  'marked as the main file - add `is_primary: true` to the one to '
                  'extract in the record, then re-run'
                  + (f', or pick one by its copy: label, e.g. {example}'
                     if labels else '') + '.')


def _dump_source_pdf(dump_path: Path) -> str | None:
    """The PDF filename an extracted-text dump's header says it was made from.

    The header line is `Machine-extracted from the embedded text layer of
    NAME.pdf ...` (written by run_source_extract). None when the dump is
    missing, unreadable, or hand-edited past recognition - the caller then
    says nothing about it.
    """
    try:
        with open(dump_path, encoding='utf-8', errors='replace') as fh:
            head = fh.read(4000)
    except OSError:
        return None
    # Parsed up to the fixed text that follows the name, so a name with
    # spaces, or with `.pdf` inside it, comes back whole.
    m = re.search(r'embedded text layer of (.+?\.pdf)(?: \(copy: [^)\n]*\))? by '
                  r'`fha source extract`', head, re.I)
    return m.group(1) if m else None


# #281: pages whose words run together. Deciding about a page takes two
# separate questions, and they get separate rules:
#
# 1. Is the default reading worth a second look? (`_words_run_together`,
#    the TRIGGER.) Cheap and deliberately eager - a false alarm costs only a
#    second reading. Two signs, either of which fires it:
#    - Long pieces (`_long_pieces_flag`): more than a fifth of the page's
#      characters in pieces longer than 15. Run-together words make pieces
#      of two words or a whole line (in a narrow column, a whole line is
#      only 20-40 letters); ordinary prose keeps nearly all its characters
#      in shorter ones.
#    - Glued pieces: more than a tenth of the pieces carry a capital straight
#      after a small letter, `,;:` with a letter right after it, or two
#      letters against a digit ("masHart", "ley,son" - kerning inside a word
#      read as a gap while the real gaps were lost; "Abbott12" - an index's
#      name run into its page number). Names and ordinals do this too
#      (McDonald, FamilySearch, 21st), which agreement then clears.
# 2. What does the page really say? (`_extract_page_text`.) The page is read
#    once more, by the best reader there is: poppler's pdftotext when it is
#    installed (falling back to pypdf's layout mode when it fails or its
#    reading cannot be used), else layout mode. That reading is accepted when it clears at least half the
#    default reading's suspicious text and is not still made of 40-letter
#    runs (`_clears_suspects`), and changes nothing but spacing
#    (`_reading_problem`). An occasional word the second reader still breaks
#    where the layer kerns ("Hart ley") is accepted - better than
#    "ThomasHartley".
#    - Not accepted, the page is plain only when the second reading agrees
#      on the GLUED pieces (it keeps McDonald whole) and the page is not a
#      layer without spaces - a long piece no reader splits is no evidence
#      that it is a word (`_agrees`). This is what keeps ordinary pages of
#      names plain.
#    - Otherwise the default reading is kept and the page is warned about.
_RUN_TOGETHER_MIN_CHARS = 100
_RUN_TOGETHER_LONG_PIECE = 15
_RUN_TOGETHER_SHARE = 0.20
_GLUED_PIECE_RE = re.compile(r'[a-z][A-Z]|[A-Za-z][,;:][A-Za-z]|[A-Za-z]{2}\d|\d[A-Za-z]{2}')
_GLUED_SHARE = 0.10
_HOPELESS_PIECE = 40
_HOPELESS_SHARE = 0.10
# Pieces that are long without any missing space, left out before measuring:
# dot leaders and rules (`(.)\1{4,}` - a contents page's "Preface.......iii"),
# web addresses and email addresses.
_REPEAT_RUN_RE = re.compile(r'(.)\1{4,}')
_URL_OR_EMAIL_RE = re.compile(r'^(?:[a-z][a-z0-9+.-]*://|www\.)|@[^@\s]+\.[a-z]', re.I)
# Scripts written without spaces between words (Thai, Lao, Tibetan,
# Myanmar, Khmer, Chinese, Japanese): their pieces are spaced correctly as
# they stand, so they are left out too.
_UNSPACED_SCRIPT_RANGES = (
    (0x0E00, 0x0EFF), (0x0F00, 0x0FFF), (0x1000, 0x109F), (0x1780, 0x17FF),
    (0x2E80, 0x9FFF), (0xF900, 0xFAFF), (0xFF00, 0xFFEF),
)


def _is_unspaced_script(ch: str) -> bool:
    o = ord(ch)
    # Latin, Cyrillic, Greek and the rest all sit below the first range.
    return o >= 0x0E00 and any(lo <= o <= hi for lo, hi in _UNSPACED_SCRIPT_RANGES)


def _measured_pieces(text: str) -> list[str]:
    """The whitespace-separated pieces of a page that can show a missing space.

    Line breaks count as separators (a one-name-per-line index is spaced
    fine for search). Runs of one repeated character split a piece and are
    dropped; pieces mostly punctuation, web or email addresses, and pieces
    mostly in a script written without spaces are left out.
    """
    kept: list[str] = []
    for raw in text.split():
        for piece in _REPEAT_RUN_RE.sub(' ', raw).split():
            alnum = sum(1 for ch in piece if ch.isalnum())
            if alnum * 2 < len(piece):
                continue
            if _URL_OR_EMAIL_RE.search(piece):
                continue
            if sum(1 for ch in piece if _is_unspaced_script(ch)) * 2 > len(piece):
                continue
            kept.append(piece)
    return kept


def _is_suspect(piece: str) -> bool:
    """A piece that might hold more than one word: long, or glued."""
    return len(piece) > _RUN_TOGETHER_LONG_PIECE or bool(_GLUED_PIECE_RE.search(piece))


def _long_pieces_flag(pieces: list[str]) -> bool:
    """The long-piece sign: over a fifth of the characters in pieces longer than 15."""
    total = sum(len(p) for p in pieces)
    long_chars = sum(len(p) for p in pieces if len(p) > _RUN_TOGETHER_LONG_PIECE)
    return long_chars > total * _RUN_TOGETHER_SHARE


def _words_run_together(text: str) -> bool:
    """The trigger: True when a page's text is worth a second reading.

    See the comment block above for the measure and why it only triggers.
    Short pages (under 100 measured characters) are never flagged.
    """
    pieces = _measured_pieces(text)
    if sum(len(p) for p in pieces) < _RUN_TOGETHER_MIN_CHARS:
        return False
    if _long_pieces_flag(pieces):
        return True
    glued = sum(1 for p in pieces if _GLUED_PIECE_RE.search(p))
    return glued > len(pieces) * _GLUED_SHARE


def _suspect_chars(text: str) -> int:
    """How many measured characters sit in suspicious (long or glued) pieces."""
    return sum(len(p) for p in _measured_pieces(text) if _is_suspect(p))


def _hopeless_share(text: str) -> float:
    """Share of measured characters in pieces of more than 40 letters and digits."""
    pieces = _measured_pieces(text)
    total = sum(len(p) for p in pieces)
    if not total:
        return 0.0
    long_lines = sum(len(p) for p in pieces
                     if sum(1 for ch in p if ch.isalnum()) > _HOPELESS_PIECE)
    return long_lines / total


def _agrees(default: str, layout: str | None) -> bool:
    """True when layout mode confirms the default reading's pieces are real.

    A suspicious piece that layout mode finds whole inside one of its own
    pieces is a real word both readers see (McDonald stays McDonald, even as
    "McDonald12" when a footnote figure lands against it; a German page's
    Kirchenbuchduplikate stays one word); at most a tenth may be split.

    But agreement is no evidence on a page whose LINES have no spaces: a text
    layer with none at all reads the same in every reader. So a page never
    agrees when most of its lines are one piece longer than 15 characters (a
    wide page or a narrow column with its spaces gone), or when more than a
    tenth of it sits in pieces over 40 letters and digits.
    """
    if layout is None or _hopeless_share(default) > _HOPELESS_SHARE:
        return False
    lines = [line.split() for line in default.splitlines() if line.strip()]
    one_long_piece = sum(1 for words in lines
                         if len(words) == 1 and len(words[0]) > _RUN_TOGETHER_LONG_PIECE)
    if one_long_piece * 2 > len(lines):
        return False
    # A piece holds no spaces, so a hit in the space-joined text lies
    # inside one piece: one substring test per suspect, not a scan of pieces.
    joined = ' '.join(layout.split())
    suspects = [p for p in _measured_pieces(default) if _is_suspect(p)]
    split = sum(1 for piece in suspects if piece not in joined)
    return split <= max(1, len(suspects) // 10)


def _collapse_layout_text(text: str) -> str:
    """Second-reader text with the page geometry's padding taken out.

    Layout mode reproduces the printed page with spaces and blank lines, so a
    word gap becomes a run of spaces and a margin a run of empty lines (and
    pdftotext ends a page with a form feed). Each run of spaces becomes one
    space and each line is trimmed; line breaks stay (they are the page's
    lines), with runs of blank lines kept to one.
    """
    text = text.replace('\f', '\n')
    lines = [re.sub(r'[ \t]+', ' ', line).strip() for line in text.splitlines()]
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()


# A second reading must keep the default reading's order this closely.
_SAME_ORDER_RATIO = 0.995
# A hyphen ending a line: pdftotext joins the word back ("Hart-" / "ley"
# becomes "Hartley"), the other readers keep it. Not a change of text.
_LINE_END_HYPHEN_RE = re.compile(r'-[ \t]*\r?\n')


def _reading_problem(default: str, other: str) -> str | None:
    """'changed' when a second reading is more than the default re-spaced, else None.

    Both readings come from the same glyphs, so a second reader may add the
    spaces the default lost and nothing else. It must not drop, add or
    reorder text (pypdf's layout mode leaves out rotated text unless told
    not to; rotated text it keeps, or a page read straight across two
    columns, comes back interleaved). Line-end hyphens are taken out of both
    readings first, since pdftotext rejoins hyphenated words. Then:

    - Letters and digits match exactly, counted, after Unicode compatibility
      folding (so the ligature "fi" glyph and "fi" are the same). Losing one
      year or initial is losing evidence, so there is no tolerance here.
    - Other characters (punctuation, soft hyphens) may differ by at most
      0.5% of the page, never fewer than 2: the readers decode a stray mark
      differently, nothing more.
    - Order, across the whole page with spaces removed: identical letter
      sequences pass at once; otherwise difflib's similarity of the two
      letter sequences must reach 0.995, which a moved block of lines or an
      interleaved column fails.

    Whether a reading still breaks a word where the layer kerns ("Hart ley")
    is not judged: such a reading is still far better for search than the
    run-together default, and is accepted.
    """
    def _fold(text: str) -> str:
        text = _LINE_END_HYPHEN_RE.sub('', text)
        return ''.join(unicodedata.normalize('NFKC', text).split())

    d, o = _fold(default), _fold(other)
    if d == o:
        return None
    d_alnum = ''.join(ch for ch in d if ch.isalnum())
    o_alnum = ''.join(ch for ch in o if ch.isalnum())
    if Counter(d_alnum) != Counter(o_alnum):
        return 'changed'
    d_marks = Counter(ch for ch in d if not ch.isalnum())
    o_marks = Counter(ch for ch in o if not ch.isalnum())
    tolerance = max(2, len(d) // 200)
    if (sum((d_marks - o_marks).values()) > tolerance
            or sum((o_marks - d_marks).values()) > tolerance):
        return 'changed'
    if d_alnum != o_alnum:
        ratio = difflib.SequenceMatcher(None, d_alnum, o_alnum, autojunk=False).ratio()
        if ratio < _SAME_ORDER_RATIO:
            return 'changed'
    return None


def _same_reading(default: str, other: str) -> bool:
    """True when a second reading is the default one with only its spacing changed."""
    return _reading_problem(default, other) is None


def _clears_suspects(default: str, other: str | None) -> bool:
    """True when `other` leaves at most half the default reading's suspicious text.

    Relative on purpose: a page of McDonalds keeps its glued-looking names in
    every reading, so an absolute "still glued" test would refuse a reading
    that spaced the page perfectly. What matters is that the second reading
    split what the default ran together - and that what is left is not runs
    of 40 letters and more with no spaces at all.
    """
    if not other or _hopeless_share(other) > _HOPELESS_SHARE:
        return False
    return _suspect_chars(other) * 2 <= _suspect_chars(default)


# pypdf logs "Rotated text discovered..." from layout mode through its
# logger; the reading comparison is what decides about rotated text, so that
# line is quieted during the retry rather than reaching the user's screen
# unexplained. The lock makes save/set/restore of the shared logger's level
# safe when two threads extract at once (the workbench can).
_PYPDF_LOG_LOCK = threading.Lock()


def _layout_reading(page) -> str | None:
    """pypdf's layout-mode reading of a page, rotated text kept; None on error."""
    with _PYPDF_LOG_LOCK:
        pypdf_logger = logging.getLogger('pypdf')
        old_level = pypdf_logger.level
        pypdf_logger.setLevel(logging.ERROR)
        try:
            return _collapse_layout_text(
                page.extract_text(extraction_mode='layout',
                                  layout_mode_strip_rotated=False) or '')
        except Exception:
            # Only a second try: its failure leaves the first read standing.
            return None
        finally:
            pypdf_logger.setLevel(old_level)


def _pdftotext_path() -> str | None:
    """Where poppler's pdftotext is, or None when it is not installed."""
    return shutil.which('pdftotext')


class _PdftotextPages:
    """Poppler's pdftotext reading of the selected pages, run once per block.

    An OPTIONAL external binary (AGENTS_TOOLING guardrails), used only when
    it is on PATH; absent, slow or failing, a page reads as None and keeps
    what pypdf gave. The selected pages are taken as contiguous blocks
    (`--pages 1-60,200` is two), and a block is read in one process the
    first time one of its pages needs it, split on the form feed pdftotext
    ends each page with. One time budget covers the whole extraction - a
    minute plus two seconds per selected page - so a slow or stuck
    pdftotext can never hold the run up for long. Plain mode, not
    `-layout`: plain mode follows the text's reading order (a two-column
    page comes back column by column), which is exactly what layout mode
    gets wrong on a squeezed two-column page. The path is passed as an
    argument, never through a shell. `calls` and `seconds` report what it
    did, for the run's output.
    """

    _SECONDS_BASE = 60
    _SECONDS_PER_PAGE = 2

    # A block this long gets a "this may take a while" line first.
    _ANNOUNCE_PAGES = 10

    def __init__(self, pdf_path: Path, selected: list[int], progress=None) -> None:
        self.pdf_path = pdf_path
        self.progress = progress
        self.blocks: list[tuple[int, int]] = []
        for page_no in sorted(set(selected)):
            if self.blocks and page_no == self.blocks[-1][1] + 1:
                self.blocks[-1] = (self.blocks[-1][0], page_no)
            else:
                self.blocks.append((page_no, page_no))
        self.budget = self._SECONDS_BASE + self._SECONDS_PER_PAGE * len(set(selected))
        self.seconds = 0.0
        self.calls = 0                  # successful runs
        self.failures: list[tuple[int, int, str]] = []
        self._read: dict[tuple[int, int], list[str] | None] = {}

    def page(self, page_no: int) -> str | None:
        block = next((b for b in self.blocks if b[0] <= page_no <= b[1]), None)
        if block is None:
            return None
        if block not in self._read:
            self._read[block] = self._run(*block)
        pages = self._read[block]
        if pages is None:
            return None
        return _collapse_layout_text(pages[page_no - block[0]])

    def _run(self, first: int, last: int) -> list[str] | None:
        exe = _pdftotext_path()
        if exe is None:
            return None
        remaining = self.budget - self.seconds
        if remaining <= 0:
            self.failures.append((first, last, 'its time budget for this run was spent'))
            return None
        if self.progress is not None and last - first + 1 >= self._ANNOUNCE_PAGES:
            try:
                self.progress(f'Reading pages {first}-{last} again with pdftotext, for '
                              'pages whose words ran together (this can take a minute)...')
            except Exception:
                pass                  # a caller's printer failing is not a page failing
        started = time.monotonic()
        try:
            done = subprocess.run(
                [exe, '-q', '-enc', 'UTF-8', '-f', str(first), '-l', str(last),
                 str(self.pdf_path), '-'],
                capture_output=True, check=False, timeout=remaining)
        except subprocess.TimeoutExpired:
            self.failures.append((first, last, 'it ran out of time'))
            return None
        except (OSError, subprocess.SubprocessError) as e:
            self.failures.append((first, last, f'it could not run: {e}'))
            return None
        finally:
            self.seconds += time.monotonic() - started
        if done.returncode != 0:
            self.failures.append((first, last, f'it stopped with error code {done.returncode}'))
            return None
        output = done.stdout.decode('utf-8', errors='replace')
        if not output.replace('\f', '').strip():
            self.failures.append((first, last, 'it returned no text'))
            return None
        pages = output.split('\f')
        # One form feed per page, so one more part than pages (the last
        # empty). Any other count means the pages cannot be told apart.
        if len(pages) == last - first + 2 and not pages[-1].strip():
            self.calls += 1
            return pages[:-1]
        self.failures.append((first, last, 'its pages could not be told apart'))
        return None


def _pdftotext_reading(pdf_path: Path, page_no: int) -> str | None:
    """Poppler's pdftotext reading of one page, or None (see _PdftotextPages)."""
    return _PdftotextPages(pdf_path, [page_no]).page(page_no)


def _extract_page_text(page, second_reader=None) -> tuple[str, str]:
    """One page's text and how it was read.

    Returns (text, how), how being 'plain' (pypdf's default reading),
    'pdftotext' or 'layout' (a second reading that restored lost spaces),
    'reordered' (words run together; the second reader spaced them but
    changed the text's order or dropped some, so it was not used) or
    'squashed' (words run together and the second reader could not space
    them).

    pypdf's default mode first. When the trigger fires, the page is read
    once more, as the comment block above sets out: by `second_reader()`
    (pdftotext, when the caller has it) or, when there is none, or it fails
    or its reading cannot be used, by pypdf's layout mode - which places each word by where it
    sits on the page rather than by font widths, and so recovers the spaces
    on OCR layers that squeeze each word with horizontal scaling (`Tz`:
    pypdf's default mode measures the word without that scaling, so the
    real gap looks like an overlap and no space goes in).

    An error from the default read propagates: that is the caller's
    "extraction FAILED" case. An error from a second reader never does - it
    is optional help, and the page keeps what pypdf read.
    """
    text = (page.extract_text() or '').strip()
    if not _words_run_together(text):
        return text, 'plain'

    def _verdict(reading: str | None) -> str:
        if not _clears_suspects(text, reading):
            return 'unspaced'
        return 'changed' if _reading_problem(text, reading) else 'ok'

    changed = False
    agreed = False
    if second_reader is not None:
        try:
            other = second_reader()
        except Exception:
            other = None
        if other:
            verdict = _verdict(other)
            if verdict == 'ok':
                return other, 'pdftotext'
            changed = verdict == 'changed'
    # No pdftotext, or it failed or could not be used on this page: layout.
    layout = _layout_reading(page)
    verdict = _verdict(layout)
    if verdict == 'ok':
        return layout, 'layout'
    changed = changed or verdict == 'changed'
    if verdict != 'changed' and _agrees(text, layout):
        agreed = True
    if agreed and not changed:
        return text, 'plain'
    return text, 'reordered' if changed else 'squashed'


def run_source_extract(
    archive_root: Path, fha_config: dict, source_id: str,
    pages: str | None = None, dry_run: bool = False, copy_label: str | None = None,
    progress=None,
) -> Result:
    """Extract a source PDF's embedded text layer into a derived companion file.

    The why (usage-feedback item 1, owner approval 2026-07-23): many archived
    PDFs - archive.org county histories above all - carry an embedded text
    layer, and reading 600 pages by vision costs hours that a text dump costs
    seconds. The dump becomes a `derived: true` inventory entry with
    `role: extracted-text` beside the original (SPEC §14 models derived
    companions; SPEC §12.1: the original is never touched - content, name, or
    location), labeled `[Page N]` in the ORIGINAL's pagination so claims
    anchored `anchor: "page N"` (SPEC §8.4) point at that PDF's pages. Stage B then
    mines the dump the way mine-transcript works a transcript.

    Honesty rule: a page with no text layer gets a placeholder line, never
    silent emptiness; a PDF with NO text on any selected page is a refusal
    with the vision/OCR next step, and no file is written - an empty derived
    file would read as "nothing on these pages," which is exactly wrong.

    pypdf is the borrowed engine (SPEC §5: "borrow the hard engines") and an
    OPTIONAL dependency like Pillow - absent, the verb refuses with the
    install command, nothing else degrades.

    Which PDF: `_choose_extract_pdf` (#280) - the primary, the unlabelled
    primary when a second digitization is attached as `primary:COPY`, or the
    one `copy_label` (`--copy`) names.

    Words, not a run-on string (#281): some OCR text layers place every word
    on its own and squeeze it with horizontal scaling, and pypdf's default
    mode then drops every space ("ThomasHartley,sonofEdward"), which no
    phrase search can match. Each page is checked (`_words_run_together`) and
    read once more, by poppler's `pdftotext` when it is installed, else by
    pypdf's layout mode (`_extract_page_text`); a page no reader can space is still
    written (its text is still evidence) but named at the top of the dump and
    in a warning, because `fha find --text` will miss phrases on it.

    `data`: {'status': 'ok'|'already'|'dry-run'|'not-found'|'refused'|
    'no-text'|'extract-error'|'failed', 'source_id', 'extract_path',
    'pages_selected', 'pages_with_text', 'error_pages', 'squashed_pages'
    (every page whose words still run together), 'reordered_pages' (those
    of them a second reader spaced but reordered), 'layout_pages',
    'pdftotext_pages'}. `progress`, when given, is called with a
    plain line before a long pdftotext run (the engine itself never prints).
    Exit codes: 0 ok/already/dry-run · 1 record or asset
    not found, working-copy, no text layer, and a dump (written, or previewed
    by --dry-run) with pages that failed or whose words ran together · 3
    refusals (pypdf absent, bad id, ambiguous PDF or unknown --copy label,
    destination exists, unwritable).
    """
    result = Result(data={'status': None, 'source_id': None, 'extract_path': None,
                          'pages_selected': 0, 'pages_with_text': 0,
                          'error_pages': [], 'squashed_pages': [],
                          'reordered_pages': [], 'layout_pages': [],
                          'pdftotext_pages': []})

    if not (is_valid_id(source_id) and id_type_of(source_id) == 'S'):
        return result_fail(result, 'refused',
                           f'{source_id!r} is not a valid source ID. s-ids look '
                           'like s-fa1234567b.')
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
    # One extracted-text dump per source, whichever copy it came from.
    existing = next((e for e in entries if str(e.get('role', '')) == _EXTRACT_ROLE),
                    None)

    # The PDF to read (see _choose_extract_pdf): never a derived file
    # (extracting an extraction is meaningless), never a guess between several.
    # With --copy the choice is made BEFORE the "already extracted" answer, so
    # a mistyped label is named rather than hidden behind a clean exit; without
    # it, an already-extracted source answers "already" as it always has.
    pdf_entries = [
        e for e in entries
        if str(e.get('file', '')).lower().endswith('.pdf')
        and str(e.get('derived', '')).lower() not in ('true', '1')
    ]
    pdf_entry = None
    if existing is None or copy_label is not None:
        pdf_entry, problem = _choose_extract_pdf(pdf_entries, fmt_id_display(sid),
                                                 copy_label)
        if problem is not None:
            return result_fail(result, 'refused', problem)

    if existing is not None:
        result.data['status'] = 'already'
        made_from = ''
        if pdf_entry is not None:
            source_pdf = _dump_source_pdf(
                resolve_path(str(existing.get('file')), fha_config, archive_root))
            wanted = PurePosixPath(str(pdf_entry.get('file')).replace('\\', '/')).name
            if source_pdf is not None and source_pdf.lower() != wanted.lower():
                made_from = (f' It was made from {source_pdf}, not from the '
                             f'copy: {copy_label.strip()} PDF ({wanted}).')
        result.add('info',
                   f'{fmt_id_display(sid)} already has an extracted-text companion '
                   f'({existing.get("file")}).{made_from} A source keeps one; to '
                   're-extract, delete that file and its files: entry first - '
                   'extraction never overwrites.')
        return result

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

    # Destination: beside the original, role-suffixed per the §13 grammar
    # `{slug}[-{copy}][-{role}]_{S-id}`: the PDF's own `-primary` role gives
    # way to `-extracted-text`, and a copy label stays.
    stem = pdf_path.stem
    m = _SID_SUFFIX_RE.search(stem)
    base = stem[:m.start()] if m else stem
    # A file named `-primary` (an older `--more FILE primary:COPY`) keeps
    # that name; only its dump drops the word, whatever the record says now.
    if base.lower().endswith('-primary') and (
            files_entry_is_main(pdf_entry)
            or not str(pdf_entry.get('role') or '').strip()):
        base = base[:-len('-primary')]
    used_label = _copy_label(pdf_entry)
    if used_label:
        label_part = re.sub(r'[^a-z0-9]+', '-', used_label.lower()).strip('-')
        if label_part and not base.lower().endswith(f'-{label_part}'):
            base = f'{base}-{label_part}'
    extract_name = f'{base}-{_EXTRACT_ROLE}_{fmt_id_display(sid)}.md'
    extract_path = pdf_path.parent / extract_name
    result.data['extract_path'] = str(extract_path)
    if extract_path.exists():
        return result_fail(result, 'refused',
                           f'{extract_name} already exists beside the PDF - '
                           'extraction never overwrites. Delete it first if you '
                           'want a fresh dump.')

    parts: list[str] = []
    pages_with_text = 0
    empty_pages: list[int] = []
    error_pages: list[int] = []
    squashed_pages: list[int] = []
    reordered_pages: list[int] = []
    layout_pages: list[int] = []
    pdftotext_pages: list[int] = []
    have_pdftotext = _pdftotext_path() is not None
    pdftotext_reader = _PdftotextPages(pdf_path, selected, progress)
    for page_no in selected:
        parts.append(f'[Page {page_no}]')
        second_reader = ((lambda n=page_no: pdftotext_reader.page(n))
                         if have_pdftotext else None)
        try:
            text, how = _extract_page_text(reader.pages[page_no - 1], second_reader)
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
            if how in ('squashed', 'reordered'):
                squashed_pages.append(page_no)
                if how == 'reordered':
                    reordered_pages.append(page_no)
            elif how == 'layout':
                layout_pages.append(page_no)
            elif how == 'pdftotext':
                pdftotext_pages.append(page_no)
        else:
            empty_pages.append(page_no)
            parts.append(_NO_TEXT_PLACEHOLDER)
        parts.append('')
    result.data['pages_with_text'] = pages_with_text
    result.data['error_pages'] = error_pages
    result.data['squashed_pages'] = squashed_pages
    result.data['reordered_pages'] = reordered_pages
    result.data['layout_pages'] = layout_pages
    result.data['pdftotext_pages'] = pdftotext_pages
    if pdftotext_reader.calls:
        # The engine reports after the fact (it does not print); a run that
        # leaned on pdftotext says so, with how long it took.
        result.add('info', f'pdftotext read {pdftotext_reader.calls} block(s) of pages '
                           f'in {pdftotext_reader.seconds:.0f}s for the pages whose '
                           'words ran together.')
    for first, last, why in pdftotext_reader.failures:
        span = f'page {first}' if first == last else f'pages {first}-{last}'
        result.add('info', f'pdftotext failed on {span} ({why}); pypdf read those '
                           'pages instead.')

    def _page_list(nums: list[int]) -> str:
        return (', '.join(str(p) for p in nums[:10])
                + (' …' if len(nums) > 10 else ''))

    header: list[str] = [
        f'# Extracted text - {meta.get("title") or fmt_id_display(sid)} '
        f'[{fmt_id_display(sid)}]',
        '',
        f'Machine-extracted from the embedded text layer of {pdf_path.name}'
        + (f' (copy: {used_label})' if used_label else '')
        + ' by `fha source extract`. A derived working copy - the PDF stays the '
        "original. Page numbers are this PDF's own, so claim anchors "
        '(`anchor: "page N"`) point at its pages; another copy of the same '
        'work may number its pages differently.',
        '',
    ]
    if squashed_pages:
        # At the top, where a reader (or the assistant mining the dump) meets
        # it before trusting a search that came back empty. Every page is
        # listed here (the dump is the lasting note; the console line trims).
        header += [
            f'Words run together on page(s) '
            f'{", ".join(str(p) for p in squashed_pages)}: the '
            "PDF's text layer gives no spaces between words there, so "
            '`fha find --text` will miss phrases on those pages. Read them in '
            'the PDF itself (or with vision) rather than trusting an empty search.',
            '',
        ]
    parts = header + parts

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

    # The same warnings on a --dry-run as on the real run, with the same exit:
    # a preview that hides a problem the write would report is no preview.
    page_warnings: list[str] = []
    if error_pages:
        # Some pages carried text, so the dump is worth keeping - but a page the
        # extractor errored on is marked FAILED (not empty) in it, and the run
        # exits nonzero so the failure is not mistaken for confirmed absence.
        page_warnings.append(
            f'Text extraction FAILED on page(s) {_page_list(error_pages)} - marked '
            'as errors in the dump (not "no text"). Read those pages with vision; '
            'do not treat them as blank.')
    if squashed_pages:
        # The dump is still worth keeping (its text is still evidence for the
        # assistant to mine), but a search for a phrase on these pages would
        # come back empty with no hint why - so it is a warning exit, the same
        # convention as a page that FAILED, and noted atop the dump.
        unspaced = [p for p in squashed_pages if p not in reordered_pages]
        reordered_why = ('a second reading spaced the words but changed their '
                         'order (it read across columns, or moved or dropped '
                         'text), so it was not used')
        unspaced_why = 'a second reading could not recover the spaces'
        if reordered_pages and unspaced:
            why = [f'on page(s) {_page_list(reordered_pages)} {reordered_why}',
                   f'on page(s) {_page_list(unspaced)} {unspaced_why}']
        else:
            why = [reordered_why if reordered_pages else unspaced_why]
        page_warnings.append(
            f'Words run together on page(s) {_page_list(squashed_pages)} (the '
            "PDF's text layer gives no spaces between words there; "
            + '; '.join(why) + '). The text is kept and the dump says so at the '
            'top, but `fha find --text` will miss phrases on those pages - read '
            'them in the PDF itself, or with vision'
            + ('' if have_pdftotext else
               ". Installing poppler's pdftotext gives a re-run one more reader "
               'to try' + ('' if dry_run else
                           ' (delete the dump and its files: entry first)'))
            + '.')

    coverage = (f'{pages_with_text} of {len(selected)} selected page(s) carry text'
                + (f' (no text layer on: '
                   f'{", ".join(str(p) for p in empty_pages[:10])}'
                   f'{" …" if len(empty_pages) > 10 else ""})' if empty_pages else '')
                + (f' (extraction FAILED on: '
                   f'{", ".join(str(p) for p in error_pages[:10])}'
                   f'{" …" if len(error_pages) > 10 else ""})' if error_pages else '')
                + (f' (words run together on: {_page_list(squashed_pages)})'
                   if squashed_pages else '')
)

    if dry_run:
        result.data['status'] = 'dry-run'
        result.add('info', f'[dry-run] Would write {extract_name} - {coverage}.')
        result.add('info', f'[dry-run] Would add a files: entry (role: '
                           f'{_EXTRACT_ROLE}, derived: true) to {record_path.name}.')
        result.add('info', '[dry-run] Nothing written. Re-run without --dry-run to apply.')
        for text in page_warnings:
            result.exit_code = EXIT_WARNINGS
            result.add('warning', f'[dry-run] {text}')
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
    for text in page_warnings:
        result.exit_code = EXIT_WARNINGS
        result.add('warning', text)
    result.add('info',
               'Next: mine the dump like a transcript, anchoring every claim '
               '`anchor: "page N"`; run `fha index` so search sees the text.',
               next_step='fha index')
    return result


_CLI_DESCRIPTION = """\
Update a source record directly, one field or section at a time, or merge
two records of one piece of evidence into one.

  fha source note s-2b3c4d5e6f --text "..."
  fha source edit-note s-2b3c4d5e6f --old-text "..." --text "..."
  fha source extract s-2b3c4d5e6f [--pages 1-60] [--copy LABEL]
  fha source clear-keyword s-2b3c4d5e6f --keyword "..." [--replace-with "..."]
  fha source set-primary s-2b3c4d5e6f --file "front-scan.tif"
  fha source merge s-2b3c4d5e6f s-7q2x9c4m1k

note appends a hand-written paragraph to a source's ## Notes section;
edit-note rewrites one existing paragraph there (named by its exact current
text) and leaves the rest untouched; extract dumps the source PDF's embedded
text layer into a derived [Page N]-labeled companion file (needs the pypdf
helper package); clear-keyword removes (or corrects) one embedded keyword on
a documents-root file, the tool-mediated fix for a stray tag `fha lint
--with-exif` (W131) finds; set-primary chooses which photo is a source's main
image (its front, not its back), the fix `fha doctor` names; merge folds the
second source into the first (files, claims, notes) and removes its record."""

_NOTE_DESCRIPTION = """\
Add a note to the end of a source's ## Notes section.

  fha source note s-2b3c4d5e6f --text "Found in Grandma's cedar chest, 2024."

Always append-only: an existing note is never edited or removed, and nothing
outside ## Notes changes (the frontmatter and ## Claims are never touched).
A source marked superseded still accepts notes.
Preview the change first with --dry-run."""


def _add_note_arguments(sub: argparse._SubParsersAction) -> None:
    """Register the note verb on a group subparser (shared by both mains)."""
    n = sub.add_parser(
        'note',
        help="Append a paragraph to a source record's ## Notes section.",
        description=_NOTE_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    n.add_argument('source_id', metavar='s-id',
                   help='The source to update (e.g. s-2b3c4d5e6f).')
    n.add_argument('--text', metavar='TEXT', required=True,
                   help='The note to add, in your own words.')
    n.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS,
                   help='Archive root (auto-detected if omitted).')
    n.add_argument('--dry-run', action='store_true', dest='dry_run',
                   help='Preview the change without writing.')
    n.set_defaults(func=_cmd_source_note)


_EDIT_NOTE_DESCRIPTION = """\
Rewrite one existing ## Notes paragraph; the rest of the section is untouched.

  fha source edit-note s-2b3c4d5e6f \\
      --old-text "Found in the cedar chest." --text "Found in Grandma's cedar chest, 2024."

The note is named by its exact current text (--old-text): give the whole
paragraph, word for word; one sentence of it is not enough. If --old-text is
only part of a note, is not found, or appears more than once, nothing is
written and the message says which. This command only rewrites; deleting a
note stays a hand edit to the record file. Preview first with --dry-run."""


def _add_edit_note_arguments(sub: argparse._SubParsersAction) -> None:
    """Register the edit-note verb on a group subparser (shared by both mains)."""
    en = sub.add_parser(
        'edit-note',
        help="Rewrite one existing paragraph of a source's ## Notes.",
        description=_EDIT_NOTE_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    en.add_argument('source_id', metavar='s-id',
                    help='The source whose note is being corrected.')
    en.add_argument('--old-text', metavar='TEXT', required=True, dest='old_text',
                    help="The note's current text: the whole paragraph, exactly as it stands.")
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
        help='Change a source record: note (append), edit-note (rewrite one), '
             'extract (PDF text layer), clear-keyword (fix a stray keyword), '
             'set-primary (choose the main image), merge (two records into one)',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    sub = p.add_subparsers(dest='source_command', metavar='SUBCOMMAND')
    _add_note_arguments(sub)
    _add_edit_note_arguments(sub)
    _add_extract_arguments(sub)
    _add_clear_keyword_arguments(sub)
    _add_set_primary_arguments(sub)
    _add_merge_arguments(sub)
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
    _add_set_primary_arguments(sub)
    _add_merge_arguments(sub)
    parser.set_defaults(func=_make_group_help(parser))
    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == '__main__':
    sys.exit(_standalone_main())
