#!/usr/bin/env python3
"""
source.py - fha source: deterministic source-record write-backs (TOOLING §3c sibling).

  fha source note S-id --text TEXT [--dry-run] [--root PATH]
  fha source edit-note S-id --old-text TEXT --text TEXT [--dry-run] [--root PATH]

A source's `## Notes` section is the human-written free-text channel SPEC §14
reserves for "the story behind it, context, or where the original is kept" -
until this tool, adding to it meant opening the file by hand. `fha source
note` is the safe one-line way to jot something down without risking the
`## Claims` fence or the frontmatter above it: paste in a sentence from the
phone, on the porch, mid-research-session, and the tool finds the record,
appends the sentence as its own paragraph, and touches nothing else.

This module deliberately opens the `fha source` namespace - future
source-field verbs would live here. Two verbs ship now: `note` (append) and
`edit-note` (rewrite one existing paragraph - the workbench's per-entry edit
button; see run_source_edit_note).

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
  _emit / _cmd_source_note / _cmd_source_edit_note / _make_group_help
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
    EXIT_FAILURE,
    EXIT_WARNINGS,
    FRONT_RE,
    Result,
    append_paragraph_to_section,
    claims_edit_problem,
    configure_utf8_stdout,
    find_source_record_path,
    fmt_id_display,
    frontmatter_fence_span,
    id_type_of,
    is_valid_id,
    normalize_id,
    read_text_exact,
    reapply_newline,
    replace_paragraph_in_section,
    resolve_root_arg,
    result_fail,
    write_text_exact,
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
_CLI_DESCRIPTION = """\
Update a source record directly - the deterministic source-field write-backs.

  fha source note S-2b3c4d5e6f --text "..."
  fha source edit-note S-2b3c4d5e6f --old-text "..." --text "..."

note appends a hand-written paragraph to a source's ## Notes section;
edit-note rewrites one existing paragraph there (named by its exact current
text) and leaves the rest untouched."""

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
        help='Source-record write-backs: note (append) and edit-note (rewrite one)',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    sub = p.add_subparsers(dest='source_command', metavar='SUBCOMMAND')
    _add_note_arguments(sub)
    _add_edit_note_arguments(sub)
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
    parser.set_defaults(func=_make_group_help(parser))
    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == '__main__':
    sys.exit(_standalone_main())
