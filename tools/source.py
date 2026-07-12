#!/usr/bin/env python3
"""
source.py - fha source: deterministic source-record write-backs (TOOLING §3c sibling).

  fha source note S-id --text TEXT [--dry-run] [--root PATH]

A source's `## Notes` section is the human-written free-text channel SPEC §14
reserves for "the story behind it, context, or where the original is kept" -
until this tool, adding to it meant opening the file by hand. `fha source
note` is the safe one-line way to jot something down without risking the
`## Claims` fence or the frontmatter above it: paste in a sentence from the
phone, on the porch, mid-research-session, and the tool finds the record,
appends the sentence as its own paragraph, and touches nothing else.

This module deliberately opens the `fha source` namespace - future
source-field verbs would live here - but only `note` ships now (mirrors
person.py's `set-living`-only opening).

DESIGN RULES (why the code looks the way it does)
-------------------------------------------------
- **Locate by scanning, never the index.** The record is found by walking
  `sources/` for the `_{S-id}.md` filename suffix (`_find_source_record_path`,
  a local sibling of `_lib.find_person_record_path` - `_lib.py` has no source
  equivalent of that helper yet, and this build is scoped to leave `_lib.py`
  untouched, so the scan lives here rather than being lifted). A stale or
  absent `.cache/index.sqlite` can never block or misdirect the write - the
  same rule `fha claim` and `fha person set-living` follow.
- **The edit is text surgery, bounded to one section.** Only lines strictly
  between the `## Notes` heading and the next `##` heading (or end of file)
  ever change; the frontmatter, the `## Claims` fence, and any later section
  (`## Stories`, etc.) are never touched by construction - the insertion point
  is always inside those bounds, never outside them. Reading and writing go
  through `read_text_exact`/`write_text_exact` so a CRLF-authored record
  churns only the lines the edit adds.
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
  _find_source_record_path   - scan sources/ for one S-id's record (local
                                sibling of _lib.find_person_record_path)
  _source_label               - "S-xxxx (title)" for human-facing messages
  _is_blank_line / _strip_trailing_blank_lines - blank-line helpers shared by
                                the two insertion branches below
  _notes_section_bounds       - locate the '## Notes' heading and its section
                                end (next '##' heading, or end of file)
  _append_note_lines          - build the edited line list (both branches:
                                heading present / heading missing)
  run_source_note             - validate, locate, edit; returns a _lib.Result
  _emit / _cmd_source_note / _make_group_help / register / _standalone_main
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
    claims_edit_problem,
    configure_utf8_stdout,
    fmt_id_display,
    frontmatter_fence_span,
    id_type_of,
    is_valid_id,
    normalize_id,
    parse_filename,
    read_text_exact,
    reapply_newline,
    resolve_root_arg,
    write_text_exact,
)

configure_utf8_stdout()

# One '## Notes' (or any '## Heading') line, matched after stripping a
# CRLF file's trailing '\r' (lines come from text.split('\n'), which leaves
# '\r' attached to the previous line - see run_source_note).
_NOTES_HEADING_RE = re.compile(r'^##\s+Notes\s*$')
_HEADING_LINE_RE = re.compile(r'^##\s+\S')


def _find_source_record_path(archive_root: Path, source_id: str) -> Path | None:
    """Scan `sources/` for one S-id's record file, or None.

    The source sibling of `_lib.find_person_record_path`: identity is the
    `_{S-id}.md` filename suffix (via the shared `_lib.parse_filename`), so a
    stale or absent index never blocks or misdirects a write. Kept local
    (rather than lifted into `_lib.py`) only because this build is scoped to
    leave `_lib.py` untouched - a natural follow-up would move it next to
    `find_person_record_path` for `fha find`/`fha claim` to share too.
    """
    target = normalize_id(source_id)
    sources_dir = Path(archive_root) / 'sources'
    if not sources_dir.is_dir():
        return None
    for path in sorted(sources_dir.rglob('*.md')):
        parsed = parse_filename(path)
        if not parsed or parsed.get('id_str') != target:
            continue
        if parsed.get('id_type') == 'S' and not parsed.get('is_companion'):
            return path
    return None


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


def _is_blank_line(line: str) -> bool:
    """True for an empty line, or a CRLF file's carriage-return-only line."""
    return line.rstrip('\r').strip() == ''


def _strip_trailing_blank_lines(lines: list[str]) -> list[str]:
    """Drop trailing blank lines from a copy of `lines`."""
    out = list(lines)
    while out and _is_blank_line(out[-1]):
        out.pop()
    return out


def _notes_section_bounds(
    lines: list[str], body_start: int,
) -> tuple[int, int] | None:
    """Return (heading_index, section_end_index) for the '## Notes' heading.

    `body_start` is the first line after the frontmatter fence (0 when there
    is none - a malformed record still gets its notes appended, per the
    forgiving-input rule). `section_end_index` is the first line at or after
    `heading_index + 1` that starts a NEW '##' heading (so a following
    `## Stories` section is never touched), or `len(lines)` when '## Notes'
    runs to the end of the file. Returns None when no top-level '## Notes'
    heading exists in the body at all - the caller then creates one.
    """
    heading_idx = None
    for i in range(body_start, len(lines)):
        if _NOTES_HEADING_RE.match(lines[i].rstrip('\r')):
            heading_idx = i
            break
    if heading_idx is None:
        return None
    end = len(lines)
    for j in range(heading_idx + 1, len(lines)):
        if _HEADING_LINE_RE.match(lines[j].rstrip('\r')):
            end = j
            break
    return heading_idx, end


def _append_note_lines(
    lines: list[str], body_start: int, note_lines: list[str], cr: str,
) -> list[str]:
    """Return the full edited line list with `note_lines` appended.

    Two branches, both bounded so nothing outside the '## Notes' section (or,
    when absent, nothing before a brand-new one at end of file) ever changes:

      1. **Heading present** - the new paragraph lands after any existing
         section content (one blank line separating them, matching the
         file's other section-break style), with a trailing blank line kept
         before whatever follows ('## Stories', or end of file).
      2. **Heading absent** - a malformed record (or a very old scaffold)
         gets '## Notes' created at the end of the file, one blank line
         after the existing content, with the new paragraph directly under
         the fresh heading (matching `process.py`'s own scaffold style: the
         heading is immediately followed by its first paragraph, no blank
         line between the two).

    `cr` is `'\\r'` for a CRLF-authored record, else `''` - appended to every
    NEWLY inserted line (blank separators included) so `reapply_newline`'s
    "already carries CRLF" no-op does not leave the new lines as stray bare
    LF inside an otherwise CRLF file (existing lines from `lines` already
    carry their own `\\r` from the `text.split('\\n')` that produced them).

    One wrinkle `cr` alone does not cover: a blank line that lands as the
    VERY LAST element of the returned list must be `''`, never `cr`. Every
    other blank line is followed by more list elements, so joining with
    `'\\n'` gives it its own trailing `\\n` (turning a bare `cr` into a
    correct `\\r\\n`); the last element gets no such newline after it, so a
    bare trailing `'\\r'` would sit dangling with nothing after it - and a
    plain `.read_text()` (universal newlines) reads a lone `\\r` as its OWN
    line break, silently adding a phantom blank line the next time the file
    is read the ordinary way. `''` as the true last element reproduces the
    same "file ends with one newline" shape `text.split('\\n')` always
    produces, CRLF or not.
    """
    bounds = _notes_section_bounds(lines, body_start)
    if bounds is not None:
        heading_idx, end = bounds
        section = lines[heading_idx + 1:end]
        kept = _strip_trailing_blank_lines(section)
        tail = lines[end:]
        if kept:
            new_section = kept + [cr] + note_lines
        else:
            new_section = list(note_lines)
        # A blank line before a following heading is a real mid-file line
        # (more elements - the tail - follow it); at true end of file the
        # trailing element must be '' (see the dangling-`\r` note above).
        new_section = new_section + ([cr] if tail else [''])
        return lines[:heading_idx + 1] + new_section + tail

    body = _strip_trailing_blank_lines(lines)
    if body:
        body = body + [cr]   # blank line before the new heading - more follows it
    return body + [f'## Notes{cr}'] + note_lines + ['']   # true end of file


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

    def _refuse(status: str, message: str) -> Result:
        result.ok = False
        result.exit_code = EXIT_FAILURE
        result.data['status'] = status
        result.add('error', message)
        return result

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

    path = _find_source_record_path(archive_root, sid)
    if path is None:
        result.ok = False
        result.exit_code = EXIT_WARNINGS
        result.data['status'] = 'not-found'
        result.add('warning',
                   f'No source record found for {fmt_id_display(sid)} under '
                   f'{archive_root / "sources"} - check the id with '
                   f'`fha find {fmt_id_display(sid)}`.',
                   next_step='fha find ' + fmt_id_display(sid))
        return result
    result.data['path'] = str(path)

    try:
        text_in = read_text_exact(path)
    except OSError as e:
        return _refuse('refused', f'cannot read {path}: {e}')

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
    note_lines = [ln.rstrip('\r') + cr for ln in note_body.split('\n')]

    new_lines = _append_note_lines(lines, body_start, note_lines, cr)
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

Today this group has one verb, note: append a hand-written paragraph to a
source's ## Notes section. Future source-field verbs will live here too."""

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


def register(subs: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register 'source' onto the main fha parser."""
    p = subs.add_parser(
        'source',
        help='Source-record write-backs: note (append to ## Notes)',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    sub = p.add_subparsers(dest='source_command', metavar='SUBCOMMAND')
    _add_note_arguments(sub)
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
    parser.set_defaults(func=_make_group_help(parser))
    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == '__main__':
    sys.exit(_standalone_main())
