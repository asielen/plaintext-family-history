#!/usr/bin/env python3
"""
person.py - fha person: deterministic person-field write-backs (TOOLING §3c).

  fha person set-living <P-id> true|false|unknown [--dry-run] [--root PATH]

The `living:` flag is the switch every privacy decision hangs on: `fha site`,
`fha gedcom`, and `fha packet` all redact (or refuse) around it, and `unknown`
is treated as living - the safe default (SPEC §9, §19). Until this tool, the
flag could only be changed by hand-editing a person record's YAML frontmatter.
`fha person set-living` is the safe one-line switch: when someone dies, or a
"person" born in 1850 is obviously not living, the human (or a skill acting on
the human's explicit yes) flips the flag with one command.

This module deliberately opens the `fha person` namespace - future person-field
verbs (set-name, restricted-flag flips, ...) would live here - but only
`set-living` ships now.

DESIGN RULES (why the code looks the way it does)
-------------------------------------------------
- **Locate by scanning, never the index.** The record is found by walking
  `people/` for the `_{P-id}.md` filename suffix (`_lib.find_person_record_path`),
  so a stale or absent `.cache/index.sqlite` can never block or misdirect the
  write - the same rule `fha claim` follows. Stubs and curated profiles are
  both editable; generated companion files are never candidates.
- **The edit is text surgery, not a YAML round-trip.** Only the one `living:`
  line changes (or is inserted); key order, hand comments - including a
  trailing comment on the `living:` line itself - and every other byte
  survive. Reading and writing go through `read_text_exact`/`write_text_exact`
  so a CRLF-authored record churns only the edited line.
- **Refuse rather than guess.** Before anything is written the rewritten
  frontmatter is re-parsed (`_frontmatter_edit_problem`, the frontmatter twin
  of `_lib.claims_edit_problem`): it must parse, `living` must equal the
  target, `id` must be unchanged, and no other field may appear, disappear, or
  change value. Any failure - including a `living:`-lookalike line the editor
  cannot own with certainty - is a plain refusal with nothing written.
- **A merged tombstone is never edited.** Readers resolve through
  `merged_into` (SPEC §9), so writing the flag on both sides would fork the
  truth; the refusal names the surviving record to edit instead.
- **Nothing flips the flag automatically.** Accepting a `death` claim does NOT
  touch `living:` - the flag is a privacy judgment and judgments are the
  human's. The review-claims skill may OFFER this command after an accepted
  death claim, and runs it only on the human's yes.
- **Success exits 0.** The "run `fha index` when convenient" reminder is
  advice text on a clean exit, never a warning exit (the plan-01 posture: a
  successful write is not a warning).

CODE MAP
--------
  _normalize_living          - bool/str/None -> 'true'/'false'/'unknown'/other/None
  _find_frontmatter_bounds   - the --- pair as line indexes (process.py pattern)
  _key_line_indexes          - column-0 `key:` lines between the bounds
  _replace_living_line       - swap only the value, keep any trailing comment
  _frontmatter_edit_problem  - pre-write re-parse guard (refuse, never mangle)
  run_set_living             - validate, locate, edit; returns a _lib.Result
  _emit / _cmd_set_living / register / _standalone_main
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
    configure_utf8_stdout,
    find_person_record_path,
    fmt_id_display,
    id_type_of,
    is_valid_id,
    normalize_id,
    read_text_exact,
    reapply_newline,
    resolve_root_arg,
    write_text_exact,
)

configure_utf8_stdout()

# The closed vocabulary of the living flag (SPEC §9: true | false | unknown).
LIVING_VALUES = ('true', 'false', 'unknown')

# One `living:` value line: key at column 0, optional spacing, the value, and an
# optional trailing `# comment` that must survive the rewrite. `[^#]*?` treats
# the first `#` as the comment start - close enough for a field whose only
# legal values are bare true/false/unknown, and the pre-write guard re-parses
# the result anyway.
_LIVING_LINE_RE = re.compile(r'^(living:)([ \t]*)([^#]*?)([ \t]*)(#.*?)?(\r?)$')


def _normalize_living(value: object) -> str | None:
    """Collapse a parsed frontmatter `living` value to its comparable form.

    YAML reads `living: true` as a Python bool and `living: unknown` as a
    string; hand edits may carry stray case or whitespace. None means the key
    is absent (or explicitly null) - callers treat that as "needs writing",
    never as equal to any target value.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value).strip().lower()


def _find_frontmatter_bounds(lines: list[str]) -> tuple[int, int] | None:
    """Return (open, close) line indexes of the frontmatter `---` pair, or None.

    The record text is split on '\\n', so a CRLF-authored file leaves a '\\r'
    on every line - the match tolerates it. The opening fence must be line 0
    (the same anchor `_lib.FRONT_RE` uses), so prose that merely contains a
    `---` rule can never be mistaken for frontmatter.
    """
    if not lines or not re.fullmatch(r'---\s*', lines[0]):
        return None
    for i in range(1, len(lines)):
        if re.fullmatch(r'---\s*', lines[i]):
            return 0, i
    return None


def _key_line_indexes(lines: list[str], start: int, end: int, key: str) -> list[int]:
    """Indexes of column-0 `key:` lines between two bounds (exclusive).

    Column 0 is what makes a line a TOP-LEVEL mapping key: a `living:` inside a
    nested mapping or a block scalar is indented, and a commented-out line
    starts with '#' - neither matches. (A column-0 lookalike inside a multi-line
    quoted scalar CAN match; that is why the caller refuses on more than one
    candidate and the pre-write guard re-parses the result.)
    """
    pattern = re.compile(rf'{re.escape(key)}:(?=\s|$)')
    return [i for i in range(start, end) if pattern.match(lines[i])]


def _replace_living_line(line: str, value: str) -> str:
    """Rewrite one `living:` line to the new value, preserving a trailing comment.

    Only the value between the colon and any `# comment` changes; the key, the
    comment, and a CRLF ending all survive. Author spacing after the colon is
    normalized to one space (the value's width changes anyway, so alignment
    cannot be preserved exactly).
    """
    m = _LIVING_LINE_RE.match(line)
    if m is None:  # caller matched the key already; this is belt-and-braces
        return line
    comment = m.group(5)
    cr = m.group(6)
    if comment:
        sep = m.group(4) or '  '
        return f'living: {value}{sep}{comment}{cr}'
    return f'living: {value}{cr}'


def _frontmatter_edit_problem(
    new_text: str, *, expect_living: str, before_meta: dict,
) -> str | None:
    """Vet the rewritten record's frontmatter BEFORE it is written.

    The frontmatter twin of `_lib.claims_edit_problem`: the edit is text
    surgery (to preserve comments and key order), so the price is that a bad
    rewrite could leave YAML that no longer parses - which would hide the
    person's identity fields from every tool until a human repairs the file.
    Re-parse the rewritten frontmatter and require that (a) it parses as a
    mapping, (b) `living` equals the target, (c) `id` is unchanged, and
    (d) every other top-level field is present and value-identical. The
    value-identity check is deliberately stronger than a key-set compare: it
    catches a `living:` lookalike inside a multi-line quoted scalar, where
    replacing the line silently rewrites ANOTHER field's value.

    Returns None when the rewrite is sound, else a short plain-language
    description of what would break; the caller refuses and writes nothing.
    """
    fm = FRONT_RE.match(new_text)
    if fm is None:
        return 'the frontmatter block (its --- fences) would be missing'
    try:
        meta = yaml.safe_load(fm.group(1))
    except yaml.YAMLError:
        return 'the frontmatter would no longer read as YAML'
    if not isinstance(meta, dict):
        return 'the frontmatter would no longer read as a set of fields'
    if _normalize_living(meta.get('living')) != expect_living:
        return (f'the living flag would read {meta.get("living")!r} '
                f'instead of {expect_living!r}')
    before_id = normalize_id(str(before_meta.get('id') or ''))
    after_id = normalize_id(str(meta.get('id') or ''))
    if before_id != after_id:
        return 'the id: field would change'
    before_keys = set(before_meta) - {'living'}
    after_keys = set(meta) - {'living'}
    if before_keys != after_keys:
        return 'another frontmatter field would appear or disappear'
    for key in before_keys:
        if meta.get(key) != before_meta.get(key):
            return f'the {key!r} field would change value'
    return None


# ── The engine ────────────────────────────────────────────────────────────────

def run_set_living(
    archive_root: Path, person_id: str, value: str, dry_run: bool = False,
) -> Result:
    """Set one person record's `living:` flag; return a Result.

    `data` is {'status': 'ok'|'already'|'dry-run'|'not-found'|'merged'|'refused',
    'person_id', 'path', 'old', 'new'}; `changed` names the record on a live
    write. `old` is the record's normalized value before the edit (None when
    the key was absent). Exit codes: 0 for ok/already/dry-run, 1 for
    not-found (with the `fha find` next step), 3 for every refusal (invalid
    id, merged tombstone, guard failure, unreadable/unwritable file).

    Validation happens before any read, the pre-write guard before any write;
    the file is either updated in exactly one line or untouched.
    """
    result = Result(data={
        'status': None, 'person_id': None, 'path': None, 'old': None, 'new': None,
    })

    def _refuse(status: str, message: str) -> Result:
        result.ok = False
        result.exit_code = EXIT_FAILURE
        result.data['status'] = status
        result.add('error', message)
        return result

    val = str(value).strip().lower()
    if val not in LIVING_VALUES:
        return _refuse(
            'refused',
            f'{value!r} is not a living value. Use one of: true, false, unknown '
            f'- e.g. `fha person set-living {person_id} false` for someone who '
            'has passed away. (unknown is treated as living - the safe default.)')
    result.data['new'] = val

    if not (is_valid_id(person_id) and id_type_of(person_id) == 'P'):
        return _refuse(
            'refused',
            f'{person_id!r} is not a valid person ID. P-ids look like P-2b3c4d5e6f '
            '- a P followed by a dash and 10 characters from the archive alphabet.')
    pid = normalize_id(person_id)
    result.data['person_id'] = fmt_id_display(pid)

    path = find_person_record_path(archive_root, pid)
    if path is None:
        result.ok = False
        result.exit_code = EXIT_WARNINGS
        result.data['status'] = 'not-found'
        result.add('warning',
                   f'No person record found for {fmt_id_display(pid)} under '
                   f'{archive_root / "people"} - check the id with '
                   f'`fha find {fmt_id_display(pid)}`.',
                   next_step='fha find ' + fmt_id_display(pid))
        return result
    result.data['path'] = str(path)

    try:
        text = read_text_exact(path)
    except OSError as e:
        return _refuse('refused', f'cannot read {path}: {e}')

    fm = FRONT_RE.match(text)
    if fm is None:
        return _refuse(
            'refused',
            f'{path.name} has no frontmatter block (the header between --- lines '
            f'at the top of the file), so there is nowhere safe to write the flag. '
            f'Open {path} and add the header by hand, then run `fha lint`. '
            'Nothing was written.')
    try:
        before_meta = yaml.safe_load(fm.group(1))
    except yaml.YAMLError:
        before_meta = None
    if not isinstance(before_meta, dict):
        return _refuse(
            'refused',
            f'the header of {path.name} does not read as YAML, so editing it '
            f'automatically could make things worse. Open {path}, fix the header '
            'by hand (run `fha lint` to see the problem line), then retry. '
            'Nothing was written.')

    name = str(before_meta.get('name') or '').strip()
    label = f'{fmt_id_display(pid)} ({name})' if name else fmt_id_display(pid)

    # A merged tombstone is never edited: readers resolve THROUGH merged_into
    # (SPEC §9), so writing the flag here would fork the truth between the
    # tombstone and the surviving record.
    if str(before_meta.get('status') or '') == 'merged':
        result.exit_code = EXIT_FAILURE
        result.ok = False
        result.data['status'] = 'merged'
        survivor = normalize_id(str(before_meta.get('merged_into') or ''))
        if survivor and is_valid_id(survivor):
            result.add('error',
                       f'{label} was merged into {fmt_id_display(survivor)} - this record '
                       'is a tombstone that readers resolve through, so the flag lives on '
                       'the surviving record. Set it there: '
                       f'`fha person set-living {fmt_id_display(survivor)} {val}`.')
        else:
            result.add('error',
                       f'{label} is a merged tombstone, but its merged_into: pointer is '
                       'missing or malformed, so the surviving record cannot be named. '
                       f'Find it with `fha find {fmt_id_display(pid)}`, then set the flag '
                       'on the survivor. Nothing was written.')
        return result

    old = _normalize_living(before_meta.get('living'))
    result.data['old'] = old

    if 'living' in before_meta and old == val:
        result.data['status'] = 'already'
        result.add('info', f'{label} is already living: {val} - nothing to change.')
        return result

    lines = text.split('\n')
    bounds = _find_frontmatter_bounds(lines)
    if bounds is None:  # FRONT_RE matched, so this cannot normally happen
        return _refuse(
            'refused',
            f'could not locate the frontmatter fences in {path.name} to edit '
            f'safely. Open {path} and set living: {val} by hand, then run `fha lint`. '
            'Nothing was written.')
    start, end = bounds

    key_lines = _key_line_indexes(lines, start + 1, end, 'living')
    new_lines = list(lines)
    if len(key_lines) > 1:
        return _refuse(
            'refused',
            f'{path.name} has more than one top-level living: line in its header, '
            'so the right one to edit cannot be chosen safely. Open '
            f'{path} and fix the duplicate by hand, then run `fha lint`. '
            'Nothing was written.')
    if key_lines and 'living' not in before_meta:
        # A column-0 lookalike (e.g. inside a multi-line quoted scalar) with no
        # real top-level field behind it: editing that line would rewrite some
        # OTHER field's value. The guard below would also catch this, but the
        # direct refusal names the actual situation.
        return _refuse(
            'refused',
            f'{path.name} has a living: line that belongs to another field\'s '
            'value, not a real living field, so it cannot be edited safely. '
            f'Open {path} and add a top-level living: {val} line by hand, then '
            'run `fha lint`. Nothing was written.')
    if key_lines:
        new_lines[key_lines[0]] = _replace_living_line(lines[key_lines[0]], val)
    elif 'living' in before_meta:
        # The field parses but owns no column-0 line (a one-line `{...}` header
        # or similar exotic shape) - refuse rather than guess where to write.
        return _refuse(
            'refused',
            f'the living field in {path.name} is not written as its own line, so '
            f'it cannot be edited safely. Open {path} and set living: {val} by '
            'hand, then run `fha lint`. Nothing was written.')
    else:
        # Key absent (legal for a hand-made record): insert in the stub scaffold's
        # field order - right after name:, else just before the closing ---.
        cr = '\r' if lines[start].endswith('\r') else ''
        name_lines = _key_line_indexes(lines, start + 1, end, 'name')
        insert_at = (name_lines[0] + 1) if name_lines else end
        new_lines.insert(insert_at, f'living: {val}{cr}')

    new_text = '\n'.join(new_lines)
    problem = _frontmatter_edit_problem(new_text, expect_living=val,
                                        before_meta=before_meta)
    if problem is not None:
        return _refuse(
            'refused',
            f'Refusing to change {label}: {problem}, so saving could corrupt the '
            f'record. Nothing was written. Open {path} and set living: {val} by '
            'hand, then run `fha lint` to check it.')

    old_display = old if 'living' in before_meta else '(absent)'
    if dry_run:
        result.data['status'] = 'dry-run'
        result.add('info', f'[dry-run] Would set {label} living: {old_display} -> {val}.')
        for dline in difflib.unified_diff(
            text.splitlines(), new_text.splitlines(),
            fromfile=f'{path} (before)', tofile=f'{path} (after)', lineterm='',
        ):
            result.add('info', dline)
        result.add('info', '[dry-run] No file written. Re-run without --dry-run to apply.')
        return result

    try:
        write_text_exact(path, reapply_newline(new_text, text))
    except OSError as e:
        return _refuse(
            'refused',
            f'cannot write {path}: {e}. Check the file is not open elsewhere and '
            'the folder is writable, then retry.')

    result.data['status'] = 'ok'
    result.note_changed(path)
    result.add('info', f'{label} is now living: {val}.', path=path)
    if val == 'false':
        result.add('info',
                   "Exports may now include this person's name and facts "
                   '(the site, GEDCOM, and packets all follow this flag).')
    elif val == 'true':
        result.add('info',
                   'This person will now be redacted from every export '
                   '(the site, GEDCOM, and packets all follow this flag).')
    else:
        result.add('info',
                   'unknown is treated as living - the safe default - so this '
                   'person will be redacted from every export (the site, GEDCOM, '
                   'and packets all follow this flag).')
    result.add('info',
               'Next: run `fha index` when convenient so queries see the change.',
               next_step='fha index')
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

def _emit(result: Result) -> int:
    for msg in result.messages:
        stream = sys.stderr if msg.level == 'error' else sys.stdout
        prefix = 'ERROR: ' if msg.level == 'error' else ''
        print(f'{prefix}{msg.text}', file=stream)
    return result.exit_code


def _cmd_set_living(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args, command='fha person set-living')
    if archive_root is None:
        return EXIT_FAILURE
    return _emit(run_set_living(
        archive_root, person_id=args.person_id, value=args.value,
        dry_run=bool(getattr(args, 'dry_run', False))))


def _make_group_help(parser: argparse.ArgumentParser):
    """Bare `fha person` prints the group help and exits 2 (a verb is required)."""
    def _cmd(args: argparse.Namespace) -> int:
        parser.print_help()
        return 2
    return _cmd


# User-facing --help text (the module docstring stays developer-facing).
_CLI_DESCRIPTION = """\
Update a person's record directly - the deterministic person-field write-backs.

  fha person set-living <P-id> true|false|unknown

Today this group has one verb, set-living: mark a person as living, passed
away, or unknown. Living (and unknown) people are kept out of the shareable
site, GEDCOM exports, and packets; marking someone false lets exports include
them. Future person-field verbs will live here too."""

_SET_LIVING_DESCRIPTION = """\
Mark one person as living, passed away, or unknown - the privacy switch.

  fha person set-living P-2b3c4d5e6f false    Passed away - exports may include them
  fha person set-living P-2b3c4d5e6f true     Living - redacted from every export
  fha person set-living P-2b3c4d5e6f unknown  Not sure - treated as living (safe default)

Changes exactly one line of the person's record and touches nothing else.
Preview the change first with --dry-run."""


def _add_set_living_arguments(sub: argparse._SubParsersAction) -> None:
    """Register the set-living verb on a group subparser (shared by both mains)."""
    sl = sub.add_parser(
        'set-living',
        help="Set a person's living flag (true/false/unknown) - drives export privacy.",
        description=_SET_LIVING_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sl.add_argument('person_id', metavar='P-id',
                    help='The person to update (e.g. P-2b3c4d5e6f).')
    sl.add_argument('value', metavar='true|false|unknown', type=str.lower,
                    choices=LIVING_VALUES,
                    help='The new value. unknown is treated as living (the safe default).')
    sl.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS,
                    help='Archive root (auto-detected if omitted).')
    sl.add_argument('--dry-run', action='store_true', dest='dry_run',
                    help='Preview the one-line change without writing.')
    sl.set_defaults(func=_cmd_set_living)


def register(subs: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register 'person' onto the main fha parser."""
    p = subs.add_parser(
        'person',
        help="Person-record write-backs: set-living (the living/privacy flag)",
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    sub = p.add_subparsers(dest='person_command', metavar='SUBCOMMAND')
    _add_set_living_arguments(sub)
    p.set_defaults(func=_make_group_help(p))
    return p


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha person',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    sub = parser.add_subparsers(dest='person_command', metavar='SUBCOMMAND')
    _add_set_living_arguments(sub)
    parser.set_defaults(func=_make_group_help(parser))
    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == '__main__':
    sys.exit(_standalone_main())
