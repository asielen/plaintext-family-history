#!/usr/bin/env python3
"""
source.py - fha source: deterministic source-record write-backs (TOOLING §3c sibling).

  fha source note S-id --text TEXT [--dry-run] [--root PATH]
  fha source edit-note S-id --old-text TEXT --text TEXT [--dry-run] [--root PATH]
  fha source extract S-id [--pages RANGES] [--dry-run] [--root PATH]

A source's `## Notes` section is the human-written free-text channel SPEC §14
reserves for "the story behind it, context, or where the original is kept" -
until this tool, adding to it meant opening the file by hand. `fha source
note` is the safe one-line way to jot something down without risking the
`## Claims` fence or the frontmatter above it: paste in a sentence from the
phone, on the porch, mid-research-session, and the tool finds the record,
appends the sentence as its own paragraph, and touches nothing else.

This module deliberately opens the `fha source` namespace - future
source-field verbs would live here. Three verbs ship now: `note` (append),
`edit-note` (rewrite one existing paragraph - the workbench's per-entry edit
button; see run_source_edit_note), and `extract` (dump a PDF's embedded text
layer into a derived [Page N]-labeled companion - see run_source_extract).

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
  `read_text_exact`/`write_text_exact` so a CRLF-authored record churns only
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
  _emit / _cmd_source_note / _cmd_source_edit_note / _cmd_source_extract / _make_group_help
  register / _standalone_main
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import yaml

from _lib import (
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    FRONT_RE,
    Result,
    append_file_entry_to_record,
    append_paragraph_to_section,
    claims_edit_problem,
    configure_utf8_stdout,
    find_source_record_path,
    fmt_id_display,
    frontmatter_fence_span,
    id_type_of,
    is_valid_id,
    is_working_copy,
    load_fha_yaml,
    normalize_id,
    read_record,
    read_text_exact,
    reapply_newline,
    replace_paragraph_in_section,
    resolve_path,
    resolve_root_arg,
    result_fail,
    write_text_exact,
    yaml_inline,
)

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
        write_text_exact(path, reapply_newline(new_text, text_in))
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
        write_text_exact(path, reapply_newline(new_text, text_in))
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
    return _emit(run_source_extract(
        archive_root, load_fha_yaml(archive_root), source_id=args.source_id,
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
                     'helper package (python -m pip install pypdf).'),
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

    try:
        from pypdf import PdfReader
    except ImportError:
        return result_fail(result, 'refused',
                           'Reading PDF text needs the pypdf helper package, which '
                           'is not installed. Install it once with '
                           '`python -m pip install pypdf`, then re-run.')

    if is_working_copy(archive_root):
        result.data['status'] = 'not-found'
        result.exit_code = EXIT_WARNINGS
        result.add('warning',
                   'This is a working copy - the PDF itself lives on the main '
                   'machine, so there is nothing here to extract from. Run this '
                   'on the main archive.')
        return result

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
        meta = read_record(record_path).get('meta') or {}
    except Exception:
        return result_fail(result, 'refused',
                           f'{record_path.name} could not be parsed - run `fha lint` '
                           'for the specifics, fix the record, then retry.')

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
    for page_no in selected:
        try:
            text = (reader.pages[page_no - 1].extract_text() or '').strip()
        except Exception:
            text = ''
        parts.append(f'[Page {page_no}]')
        if text:
            pages_with_text += 1
            parts.append(text)
        else:
            empty_pages.append(page_no)
            parts.append(_NO_TEXT_PLACEHOLDER)
        parts.append('')
    result.data['pages_with_text'] = pages_with_text

    if pages_with_text == 0:
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

    entry_lines = [
        f'  - file: {yaml_inline(str(Path(alias).parent / extract_name).replace(chr(92), "/"))}',
        f'    role: {_EXTRACT_ROLE}',
        '    derived: true',
    ]

    coverage = (f'{pages_with_text} of {len(selected)} selected page(s) carry text'
                + (f' (no text layer on: '
                   f'{", ".join(str(p) for p in empty_pages[:10])}'
                   f'{" …" if len(empty_pages) > 10 else ""})' if empty_pages else ''))

    if dry_run:
        result.data['status'] = 'dry-run'
        result.add('info', f'[dry-run] Would write {extract_name} - {coverage}.')
        result.add('info', f'[dry-run] Would add a files: entry (role: '
                           f'{_EXTRACT_ROLE}, derived: true) to {record_path.name}.')
        result.add('info', '[dry-run] Nothing written. Re-run without --dry-run to apply.')
        return result

    try:
        extract_path.write_text('\n'.join(parts), encoding='utf-8')
    except OSError as e:
        return result_fail(result, 'failed',
                           f'could not write {extract_name}: {e} - check the folder '
                           'is writable, then retry.')

    before: str | None = None
    try:
        before = read_text_exact(record_path)
        after = append_file_entry_to_record(before, entry_lines)
        write_text_exact(record_path, reapply_newline(after, before))
    except Exception as e:
        extract_path.unlink(missing_ok=True)
        # write_text_exact is a truncating write, so a mid-write failure can
        # leave the RECORD partial - restore the pristine text we still hold
        # (the attach_more discipline) and only claim a clean rollback when
        # the restore actually landed.
        restored = before is None
        if before is not None:
            try:
                write_text_exact(record_path, before)
                restored = True
            except OSError:
                restored = False
        if restored:
            return result_fail(result, 'failed',
                               f'could not add the files: entry to {record_path.name} '
                               f'({e}) - nothing was kept (the dump and the record '
                               'edit were both rolled back).')
        return result_fail(result, 'failed',
                           f'could not add the files: entry to {record_path.name} '
                           f'({e}) - AND the record could not be restored, so it may '
                           'be left partially written. Restore it from git or your '
                           'last backup before doing anything else.')

    result.data['status'] = 'ok'
    result.note_changed(extract_path)
    result.note_changed(record_path)
    result.add('info', f'Wrote {extract_name} - {coverage}.')
    result.add('info', f'Added a files: entry (role: {_EXTRACT_ROLE}, derived: true) '
                       f'to {record_path.name}.')
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

note appends a hand-written paragraph to a source's ## Notes section;
edit-note rewrites one existing paragraph there (named by its exact current
text) and leaves the rest untouched; extract dumps the source PDF's embedded
text layer into a derived [Page N]-labeled companion file (needs the pypdf
helper package)."""

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
             'extract (PDF text layer)',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    sub = p.add_subparsers(dest='source_command', metavar='SUBCOMMAND')
    _add_note_arguments(sub)
    _add_edit_note_arguments(sub)
    _add_extract_arguments(sub)
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
    parser.set_defaults(func=_make_group_help(parser))
    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == '__main__':
    sys.exit(_standalone_main())
